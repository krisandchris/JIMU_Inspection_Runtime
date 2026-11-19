"""Read-only postcondition checks on the issued Task and immutable storage."""

import io
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cases import CATEGORY, INSIDE, OUTSIDE, QUESTION_IDS, Box, Frame
from loopback import ParkingProvider
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.runtime.decisions import (
    BlockedRuleOutcome,
    EvaluatedRuleOutcome,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.inspection.media import LocalMediaStore, MediaError
from jimu_runtime.tools.step_attempts import StepCompletion
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.worker_records import (
    NODE_MESSAGE,
    ContextMessage,
    DecisionMessage,
    GroundingMessage,
    ObservationMessage,
    ToolMessage,
)

_GOLD = Path(__file__).with_name("gold.json")
_SLOT_BY_STEP = {
    "wheel_segmentation": "wheel_mask",
    "tactile_paving_segmentation": "tactile_paving_mask",
    "wheel_boundary_geometry": "geometry",
}
_BOX_NAMES = {OUTSIDE: "outside", INSIDE: "inside"}


def require_equal(label: str, observed: object, expected: object) -> None:
    if observed != expected:
        raise AssertionError(f"{label} observed {observed!r}, expected {expected!r}")


@dataclass(frozen=True, slots=True)
class AuditScope:
    scope: IdempotencyScope
    execution_id: str

    def params(self) -> dict[str, str]:
        return {
            "p": self.scope.project_id,
            "c": self.scope.client_id,
            "e": self.execution_id,
        }


async def _rows(connection: AsyncConnection, scope: AuditScope, query: str):
    return (await connection.execute(text(query), scope.params())).mappings().all()


async def check_dispatches(
    con: AsyncConnection,
    scope: AuditScope,
    provider: ParkingProvider,
    store: LocalMediaStore,
) -> dict[str, int]:
    assert not provider.errors, provider.errors
    rows = await _rows(
        con,
        scope,
        """
        SELECT i.dispatch_id, i.attempt_id, i.node_run_id, i.record AS intent, r.record AS receipt, r.late
        FROM jimu_workflow.dispatch_intents AS i
        JOIN jimu_workflow.dispatch_results AS r USING (project_id, client_id, execution_id, dispatch_id)
        WHERE (i.project_id, i.client_id, i.execution_id) = (:p, :c, :e)
    """,
    )
    by_wire_digest = defaultdict(list)
    physical_models = [
        row for row in rows if row["intent"]["target"]["kind"] == "model"
    ]
    for row in physical_models:
        by_wire_digest[row["intent"]["request_digest"]].append(row)
    assert len(physical_models) == len(provider.requests)
    seen = set()
    for reply in provider.replies:
        matches = [
            match
            for match in by_wire_digest[reply.request.request_digest]
            if match["dispatch_id"] not in seen
            and not match["late"]
            and (
                reply.body is None
                or match["receipt"]["provider_request_id"] == reply.request.request_id
            )
        ]
        assert len(matches) == 1, (reply.request, matches)
        (row,) = matches
        seen.add(row["dispatch_id"])
        assert row["receipt"]["response_digest"] == reply.response_digest
        if reply.body is None:
            assert row["receipt"]["error"]["error_class"] == "timeout"
        invocation = row["receipt"]["invocation"]
        if invocation is not None:
            assert (
                invocation["execution_id"],
                invocation["node_run_ref"]["record_id"],
                invocation["attempt_ref"]["record_id"],
            ) == (scope.execution_id, row["node_run_id"], row["attempt_id"])
            assert invocation["request_digest"] == reply.request.request_digest
            if reply.request.object_id is not None:
                assert invocation["object_ref"]["record_id"] == reply.request.object_id
        stored = await store.read_model_response(
            scope.scope, scope.execution_id, row["dispatch_id"], late=False
        )
        if row["receipt"].get("response_bytes_omission_reason") is None:
            assert stored.raw == reply.body
        else:
            assert stored.raw is None
            assert row["receipt"]["response_bytes_omission_reason"] == (
                "incomplete" if reply.body is None else "unvalidated"
            )
        if (
            reply.body is None
            or reply.status != 200
            or row["receipt"]["error"] is not None
        ):
            assert stored.normalized is None
        else:
            wire = json.loads(reply.body)
            assert (
                stored.normalized == wire["choices"][0]["message"]["content"].encode()
            )
    return dict(Counter(request.role for request in provider.requests))


def _independent_mask(
    frame: Frame, box: Box | None, color: tuple[int, int, int]
) -> bytes:
    with Image.open(io.BytesIO(frame.content)) as image:
        pixels = image.get_flattened_data()
        width = image.width
        return bytes(
            255
            if sample in {color, (24, 192, 64)}
            and (
                box is None
                or (
                    box[0] <= (index % width) < box[2]
                    and box[1] <= (index // width) < box[3]
                )
            )
            else 0
            for index, sample in enumerate(pixels)
        )


async def check_tool_evidence(
    con: AsyncConnection,
    scope: AuditScope,
    store: LocalMediaStore,
    frame: Frame,
    boxes: dict[str, Box],
    expected_geometry: dict[str, str],
) -> tuple[dict[str, object], dict[str, object]]:
    rows = await _rows(
        con,
        scope,
        """
        SELECT completion.record::text AS completion, receipt.record AS receipt,
               intent.record AS intent, intent.attempt_id, intent.node_run_id, intent.dispatch_id
        FROM jimu_workflow.tool_step_completions AS completion
        JOIN jimu_workflow.dispatch_intents AS intent
          ON (intent.project_id, intent.client_id, intent.execution_id, intent.attempt_id, intent.dispatch_id)
           = (completion.project_id, completion.client_id, completion.execution_id, completion.child_attempt_id, completion.tool_execution_id)
        JOIN jimu_workflow.dispatch_results AS receipt
          ON (receipt.project_id, receipt.client_id, receipt.execution_id, receipt.dispatch_id)
           = (intent.project_id, intent.client_id, intent.execution_id, intent.dispatch_id)
        WHERE (completion.project_id, completion.client_id, completion.execution_id) = (:p, :c, :e)
          AND NOT receipt.late
    """,
    )
    assert len(rows) == 3 * len(boxes)
    sidecars: dict[str, dict[str, object]] = defaultdict(dict)
    known_assets = {}
    for row in rows:
        completion = StepCompletion.model_validate_json(row["completion"])
        tool = completion.result.execution
        assert tool.actual_implementation_version == "1.0.0"
        assert tool.tool_execution_id == row["dispatch_id"]
        assert tool.attempt_ref.record_id == row["attempt_id"]
        assert tool.node_run_ref.record_id == row["node_run_id"]
        assert row["receipt"]["acknowledgement"] == "tool_process"
        assert row["receipt"]["response_digest"] is not None
        assert tool.status == "succeeded" and completion.selection is not None
        assert completion.selection.selected_attempt_id == tool.attempt_ref.record_id
        assert tool.object_ref is not None
        box = boxes[tool.object_ref.record_id]
        assert completion.result.facts
        slot = _SLOT_BY_STEP[completion.selection.step_id]
        verified = await store.read_tool_sidecar(
            scope.scope, scope.execution_id, tool.tool_execution_id, slot
        )
        assert sha256(verified.content).hexdigest() == verified.asset.sha256
        assert verified.asset.spatial.original_media_ref.sha256 == frame.digest
        assert verified.asset.spatial.transforms[0].source.frame == "original_image"
        assert verified.asset.spatial.coordinate_system.dimensions.width == 64
        assert verified.asset.spatial.coordinate_system.dimensions.height == 48
        assert verified.asset.scope.object_ref.record_id == tool.object_ref.record_id
        assert verified.asset.evidence_id not in known_assets
        known_assets[verified.asset.evidence_id] = verified
        order = sidecars[tool.object_ref.record_id]
        assert slot not in order
        order[slot] = verified
        if slot == "geometry":
            geometry = json.loads(verified.content)
            assert geometry["original_sha256"] == frame.digest
            assert geometry["parking_area"]["bbox"]["xyxy"] == [8, 8, 40, 40]
            assert geometry["relation"] == expected_geometry.get(
                _BOX_NAMES[box],
                "outside_parking_area" if box == OUTSIDE else "inside_parking_area",
            )
            continue
        color = (16, 174, 208) if slot == "wheel_mask" else (224, 192, 32)
        with Image.open(io.BytesIO(verified.content)) as mask:
            assert mask.mode == "L" and mask.size == (64, 48)
            assert mask.tobytes() == _independent_mask(
                frame, box if slot == "wheel_mask" else None, color
            )
    assert set(sidecars) == set(boxes)
    for object_slots in sidecars.values():
        assert set(object_slots) == set(_SLOT_BY_STEP.values())
        geometry = json.loads(object_slots["geometry"].content)
        for slot in ("wheel_mask", "tactile_paving_mask"):
            both = object_slots[slot]
            assert geometry["inputs"][slot] == {
                "evidence_id": both.asset.evidence_id,
                "sha256": both.asset.sha256,
            }
    return dict(known_assets), dict(sidecars)


async def audit(
    con: AsyncConnection,
    task: AcceptedTask,
    final: ExecutionFinalization,
    provider: ParkingProvider,
    frame: Frame,
    store: LocalMediaStore,
    name: str,
) -> dict[str, object]:
    # This read occurs only after the real product has committed finalization.
    gold = json.loads(_GOLD.read_bytes())["cases"][name]
    scope = AuditScope(task.idempotency_scope, final.execution_id)
    original = await store.verified_task_original(scope.scope, task)
    require_equal("original media bytes", original.content == frame.content, True)
    wire = await check_dispatches(con, scope, provider, store)
    require_equal("actual POST count by role", wire, gold["post"])
    coverage = final.coverage
    require_equal("Object count", coverage.object_count, gold["objects"])
    require_equal("Task-level verdict", coverage.summary_result.value, gold["summary"])
    require_equal(
        "discovery outcome", coverage.discovery_outcome.value, gold["discovery"]
    )
    if gold["grounding"] is not None:
        require_equal(
            "grounding outcome",
            [item.grounding_outcome.value for item in coverage.profiles],
            [gold["grounding"]],
        )
    if "coverage" in gold:
        require_equal(
            "coverage classification", coverage.coverage_status.value, gold["coverage"]
        )
    require_equal(
        "mandatory scan strategy",
        coverage.mandatory_rule_coverage.scan,
        "all_mandatory",
    )
    require_equal("coverage matches", len(coverage.matches), gold["match_count"])

    messages = await _rows(
        con,
        scope,
        """
        SELECT record::text AS record, output_digest
        FROM jimu_workflow.produced_outputs
        WHERE (project_id, client_id, execution_id) = (:p, :c, :e) AND output_kind = 'worker_message'
    """,
    )
    parsed = []
    for row in messages:
        assert sha256(row["record"].encode()).hexdigest() == row["output_digest"]
        parsed.append(NODE_MESSAGE.validate_json(row["record"]))
    boxes = {
        obj.object_id: tuple(map(int, obj.bbox.xyxy))
        for msg in parsed
        if isinstance(msg, GroundingMessage)
        for obj in msg.objects
    }
    require_equal("persisted Object IDs", len(boxes), gold["objects"])
    assert all(
        ask.object_id in boxes and boxes[ask.object_id] == ask.box
        for ask in provider.requests
        if ask.object_id is not None
    )
    selected = defaultdict(list)
    all_observations = []
    for msg in parsed:
        if isinstance(msg, ObservationMessage):
            all_observations.append(msg)
            if msg.authority is not None:
                selected[msg.batch.object_ref.record_id].append(msg)
    require_equal(
        "Observation selection count by Object",
        {oid: len(rows) for oid, rows in selected.items()},
        dict.fromkeys(boxes, 1),
    )
    for oid, (observation,) in selected.items():
        (batch, selector) = observation.batch, observation.authority
        assert selector is not None
        require_equal(
            "exact selected Observation Attempt",
            selector.selected_attempt_id,
            batch.attempt_ref.record_id,
        )
        require_equal(
            "configured Question slots",
            {item.question_ref.artifact_id for item in batch.question_results},
            QUESTION_IDS,
        )
        evidence_rows = observation.durable_request_evidence
        assert len(evidence_rows) == 2 and not observation.request_evidence
        stored_requests = {
            row.evidence_id: (
                row,
                await store.read_observation_request(
                    scope.scope,
                    scope.execution_id,
                    row.evidence_id,
                    row.storage,
                    row.sha256,
                    row.byte_size,
                ),
            )
            for row in evidence_rows
        }
        selected_asset, _ = next(iter(stored_requests.values()))
        mismatched = scope.scope.model_copy(update={"client_id": "wp05.wrong-scope"})
        try:
            await store.read_observation_request(
                mismatched,
                scope.execution_id,
                selected_asset.evidence_id,
                selected_asset.storage,
                selected_asset.sha256,
                selected_asset.byte_size,
            )
        except MediaError:
            pass
        else:
            raise AssertionError(
                "foreign client loaded another Task's request evidence"
            )
        for row, raw_bytes in stored_requests.values():
            assert raw_bytes is not None
            assert (
                len(raw_bytes) == row.byte_size
                and sha256(raw_bytes).hexdigest() == row.sha256
            )
        (actual_request,) = [
            request
            for request in provider.requests
            if request.role == "observation"
            and request.request_id == observation.invocation.provider_request_id
            and request.request_digest == observation.invocation.request_digest
        ]
        assert actual_request.object_id == oid
        schema_ref = observation.inputs.contract_snapshot.response_schema_evidence_ref
        schema, body = stored_requests[schema_ref.record_id]
        assert (
            schema.sha256 == observation.inputs.contract_snapshot.response_schema_sha256
        )
        assert (
            sha256(
                json.dumps(
                    json.loads(body), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            == actual_request.response_contract_digest
        )
        prompt_ref = observation.inputs.compiled_prompt_evidence_ref
        prompt, _ = stored_requests[prompt_ref.record_id]
        assert prompt.sha256 == actual_request.prompt_digest
        assert {
            ref.record_id for ref in observation.invocation.evidence_refs
        } >= stored_requests.keys()
        side = _BOX_NAMES[boxes[oid]]
        expected = gold.get("question_status", {}).get(side, {})
        reasons = gold.get("question_reason", {}).get(side, {})
        for item in batch.question_results:
            fact = item.primary_fact
            assert (
                fact.object_id == oid
                and fact.attempt_id == selector.selected_attempt_id
            )
            if fact.fact_key in expected:
                require_equal(
                    f"{_BOX_NAMES[boxes[oid]]} {fact.fact_key}",
                    fact.status.value,
                    expected[fact.fact_key],
                )
            if fact.status.value in {"unknown", "error"}:
                fields = fact.model_dump(mode="json")
                assert "value" not in fields
                if fact.fact_key in reasons:
                    require_equal(
                        f"{side} {fact.fact_key} reason",
                        fields["reason"]["reason_code"],
                        reasons[fact.fact_key],
                    )
    if gold.get("whole_batch_retry"):
        failed = [msg for msg in all_observations if msg.authority is None]
        assert len(failed) == 1 and len(all_observations) == 2
        original_batch = failed[0].batch
        assert original_batch.outcome in {"association_error", "parse_error"}
        assert not original_batch.question_results
        (new,) = [msg for msg in all_observations if msg.authority is not None]
        assert new.inputs.retry.previous_batch_ref == original_batch.batch_ref
        assert new.batch.attempt_ref != original_batch.attempt_ref

    known_sidecars, by_object = await check_tool_evidence(
        con, scope, store, frame, boxes, gold.get("geometry", {})
    )
    wrong_scope = scope.scope.model_copy(update={"client_id": "wp05.wrong-scope"})
    if known_sidecars:
        tool = next(
            row
            for msg in parsed
            if isinstance(msg, ToolMessage)
            for row in msg.executions
        )
        try:
            await store.read_tool_sidecar(
                wrong_scope,
                scope.execution_id,
                tool.tool_execution_id,
                _SLOT_BY_STEP[tool.invocation_origin.step_id],
            )
        except MediaError:
            pass
        else:
            raise AssertionError("foreign client loaded another Task's Tool evidence")

    if len(boxes) > 1:
        first_obj, other_obj = tuple(by_object)
        wheel_execution = next(
            execution
            for msg in parsed
            if isinstance(msg, ToolMessage)
            for execution in msg.executions
            if execution.object_ref.record_id == first_obj
            and execution.invocation_origin.step_id == "wheel_segmentation"
        )
        try:
            await store.read_tool_sidecar(
                scope.scope,
                scope.execution_id,
                wheel_execution.tool_execution_id,
                "wheel_mask",
                expected_asset=by_object[other_obj]["wheel_mask"].asset,
            )
        except MediaError as error:
            assert str(error) == "tool_sidecar_bound_asset_mismatch"
        else:
            raise AssertionError(
                "cross-Object Tool evidence was accepted as an exact ancestor"
            )

    selected_facts = {
        fact.fact_id: fact
        for msg in parsed
        if isinstance(msg, ToolMessage)
        or (isinstance(msg, ObservationMessage) and msg.authority is not None)
        for fact in msg.facts
    }
    selection_by_id = {
        authority.selection_id: authority
        for msg in parsed
        for authority in (
            msg.authorities
            if isinstance(msg, ToolMessage)
            else (msg.authority,)
            if isinstance(msg, ObservationMessage) and msg.authority
            else ()
        )
    }

    authority_data = await _rows(
        con,
        scope,
        """
        SELECT authority.node_run_id, nodes.node_id, authority.attempt_id, attempts.status
        FROM jimu_workflow.authority_outputs AS authority
        JOIN jimu_workflow.node_runs AS nodes USING (project_id, client_id, execution_id, node_run_id)
        JOIN jimu_workflow.attempt_results AS attempts USING
            (project_id, client_id, execution_id, node_run_id, attempt_id)
        WHERE (authority.project_id, authority.client_id, authority.execution_id) = (:p, :c, :e)
    """,
    )
    assert len({row["node_run_id"] for row in authority_data}) == len(authority_data)
    assert all(row["status"] == "succeeded" for row in authority_data)
    require_equal(
        "published N06 authority rows",
        sum(row["node_id"] == "N06" for row in authority_data),
        len(boxes),
    )

    decisions = {}
    for decision in final.decisions:
        oid = decision.object_ref.record_id
        decisions[_BOX_NAMES[boxes[oid]]] = decision.result.value
        assert (
            decision.release_id == task.release_id
            and decision.bundle_digest == task.bundle_digest
        )
        (evaluated,) = decision.rule_evaluations
        assert evaluated.rule.rule_ref.artifact_id == CATEGORY
        result = evaluated.result
        assert isinstance(result, (EvaluatedRuleOutcome, BlockedRuleOutcome))
        guard = result.guard
        if decision.result.value in {"violation", "no_violation"}:
            assert isinstance(result, EvaluatedRuleOutcome)
            assert guard.outcome == "complete"
            assert {proof.selector.fact_key for proof in guard.fact_proofs} == {
                "parking_state",
                "wheel_boundary_relation",
            }
            object_verification = by_object[oid]
            assert {
                sidecar.asset.evidence_id for sidecar in object_verification.values()
            } <= {ref.record_id for ref in guard.required_evidence_refs}
            for proof in guard.fact_proofs:
                fact = selected_facts[proof.fact_ref.record_id]
                selection = selection_by_id[proof.authority_selection_ref.record_id]
                assert fact.object_id == selection.object_id == oid
                assert fact.attempt_id == selection.selected_attempt_id
                assert fact.fact_key == proof.selector.fact_key
                assert any(
                    ref.record_id == fact.fact_id
                    for ref in selection.selected_fact_refs
                )
            assert result.matched == (decision.result.value == "violation")
        else:
            assert isinstance(result, BlockedRuleOutcome) and guard.reasons
        message = next(
            msg
            for msg in parsed
            if isinstance(msg, DecisionMessage)
            and msg.draft.inputs.object_record.object_id == oid
        )
        if name == "candidate_not_configured":
            assert message.draft.candidate is None and message.invocation is None
        elif name in {"candidate_unavailable", "candidate_timeout"}:
            assert (
                message.draft.candidate is None
                or not message.draft.candidate.categories
            )
        else:
            assert (
                message.draft.candidate is not None
                and not message.draft.candidate.categories
            )
    require_equal("Object-level verdicts", decisions, gold["decisions"])
    context = [msg.snapshot for msg in parsed if isinstance(msg, ContextMessage)]
    assert len(context) == 1 and context[0] is not None
    return {
        "post": wire,
        "summary": coverage.summary_result.value,
        "coverage": coverage.coverage_status.value,
        "decisions": decisions,
        "objects": len(boxes),
        "tool_sidecars": len(known_sidecars),
        "authority_count": len(authority_data),
        "context": context[0].context_snapshot_id,
    }
