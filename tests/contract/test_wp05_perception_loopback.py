"""Run the configured perception branch using the signed scene and real I/O."""

import asyncio
import base64
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definitions.models import ModelBindingContentRef
from jimu_runtime.domain.enums import FactStatus, NodeRunStatus
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.perception import GroundingOutcome
from jimu_runtime.domain.values import EnumValue
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.inspection.decision import build_profile_gate
from jimu_runtime.inspection.perception import (
    verify_discovery_rejection,
    verify_observation_result,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.worker import (
    ClaimedNode,
    WorkerStep,
    WorkflowAdapters,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_records import (
    ContextMessage,
    DiscoveryMessage,
    GroundingMessage,
    ObservationMessage,
)
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05.perception import IMAGE, perception_fixture

loopback_only = http_support.loopback_only

_ANSWERS = {
    "vehicle_type": "electric_bicycle",
    "parking_state": "parked",
    "parking_position": "over_boundary",
}
_GROUNDING = {"boxes": [{"xyxy": [2, 3, 18, 20]}, {"xyxy": [20, 2, 45, 40]}]}


@pytest.mark.parametrize(
    "case",
    (
        "multiple_objects",
        "extra_slot_retry",
        "duplicate_slot_retry",
        "receipt_mismatch_retry",
        "no_hits",
        "unregistered_profile",
        "no_target",
        "invalid_box",
        "bad_original",
    ),
)
def test_signed_perception_through_worker(
    tmp_path: Path, loopback_only: set[int], case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        wire: list[tuple[str, bytes, bytes]] = []
        observed: list[NodeResult] = []
        counts: Counter[str] = Counter()

        async def peer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await http_support.read_request(reader)
            body = json.loads(request.body)
            text, image = body["messages"][0]["content"]
            assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == IMAGE
            content_schema = body["response_format"]["json_schema"]["schema"]
            identifier = content_schema["$id"].split(":")[-2]
            counts[identifier] += 1
            assert "synthetic.scene.image" in text["text"]
            if identifier == "profile_discovery-result":
                assert json.loads(text["text"].splitlines()[-1]) == {
                    "eligible_profile_ids": ["nonmotor_vehicle"],
                }
                hits = {
                    "no_hits": [],
                    "unregistered_profile": ["outside-catalogue"],
                }.get(case, ["nonmotor_vehicle"])
                content = json.dumps({"profile_hits": hits})
            elif identifier == "object_grounding-result":
                assert json.loads(text["text"].splitlines()[-1]) == {
                    "profile_id": "nonmotor_vehicle",
                }
                variants = {
                    "no_target": {"boxes": []},
                    "invalid_box": {"boxes": [{"xyxy": [0, 0, 65, 5]}]},
                }
                content = json.dumps(variants.get(case, _GROUNDING))
            else:
                assert identifier == "object_observation-result"
                _, model_input, metadata = text["text"].splitlines()
                assert json.loads(model_input)["bbox"] in _GROUNDING["boxes"]
                assert set(json.loads(metadata)["questions"]) == set(_ANSWERS)
                current = counts[identifier]
                if case == "multiple_objects" and current == 1:
                    content = '{"vehicle_type":"insufficient_visibility","parking_state":"bad"}'
                elif case == "extra_slot_retry" and current == 1:
                    content = json.dumps({**_ANSWERS, "object_id": "foreign.object"})
                elif case == "duplicate_slot_retry" and current == 1:
                    content = '{"vehicle_type":"bicycle","parking_state":"parked","parking_state":"unparked"}'
                else:
                    content = json.dumps(_ANSWERS)
            response = http_support.chat_body(model=body["model"], content=content)
            wire.append((identifier, request.body, response))
            writer.write(
                http_support.response(
                    response,
                    headers=(
                        (b"X-Request-ID", f"wp05-{len(wire)}".encode()),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url) as fixture:
                adapter = fixture.adapter
                if case == "receipt_mismatch_retry":
                    transport = fixture.gateway._transport
                    physical_dispatch = transport.dispatch

                    async def dispatch_with_inconsistent_slots(request):
                        result = await physical_dispatch(request)
                        if (
                            request.route.operation.node_run.node_id == "N04"
                            and counts["object_observation-result"] == 1
                        ):
                            return replace(
                                result, missing_question_slots=("parking_state",)
                            )
                        return result

                    monkeypatch.setattr(
                        transport, "dispatch", dispatch_with_inconsistent_slots
                    )
                if case == "bad_original":

                    async def wrong_bytes(scope):
                        return VerifiedImage(
                            b"x" * len(IMAGE), "image/png", scope.media.media_ref.sha256
                        )

                    adapter = replace(adapter, load_image=wrong_bytes)

                async def execute(claimed: ClaimedNode) -> NodeResult:
                    witness = claimed.previous.observation_retry
                    if witness is not None:
                        committed = witness.message.output
                        assert isinstance(committed, FailedNodeOutput)
                        forged = committed.model_copy(
                            update={
                                "error": committed.error.model_copy(
                                    update={"retryable": False}
                                )
                            }
                        )
                        unqualified = replace(
                            witness,
                            message=witness.message.model_copy(
                                update={"output": forged}
                            ),
                        )
                        earlier_wires = len(wire)
                        with pytest.raises(
                            ValueError, match="foreign_or_nonretryable_observation"
                        ):
                            await adapter.execute(
                                replace(
                                    claimed,
                                    previous=replace(
                                        claimed.previous, observation_retry=unqualified
                                    ),
                                ),
                                fixture.gateway,
                            )
                        assert len(wire) == earlier_wires
                    result = await adapter.execute(claimed, fixture.gateway)
                    observed.append(result)
                    return result

                async def validate_observation(
                    claimed: ClaimedNode, message: ObservationMessage
                ) -> None:
                    discovery = claimed.previous.discovery
                    assert discovery is not None
                    (selection,) = tuple(
                        row
                        for row in discovery.selections
                        if row.selection_id
                        == message.batch.question_selection_ref.record_id
                    )
                    invocation = message.invocation
                    provider_id = invocation.provider_request_id
                    assert provider_id is not None
                    _, request_bytes, response_bytes = wire[
                        int(provider_id.removeprefix("wp05-")) - 1
                    ]
                    assert sha256_digest(request_bytes) == invocation.request_digest
                    receipt = await fixture.journal.get_dispatch(
                        claimed.claim.scope,
                        claimed.claim.execution_id,
                        invocation.model_invocation_id,
                    )
                    assert receipt is not None
                    (recorded,) = receipt.results
                    assert recorded.result.response_digest == sha256_digest(
                        response_bytes
                    )
                    normalized = (
                        json.loads(response_bytes)["choices"][0]["message"][
                            "content"
                        ].encode()
                        if invocation.outcome.status == "succeeded"
                        else None
                    )
                    slot_receipt = (
                        None
                        if recorded.result.normalized_response_digest is None
                        else (
                            recorded.result.invalid_question_slots,
                            recorded.result.missing_question_slots,
                        )
                    )
                    verify_observation_result(
                        claimed.opened.scope,
                        message,
                        claimed.previous.snapshot(),
                        selection,
                        normalized,
                        failure=recorded.result.error,
                        retry_witness=claimed.previous.observation_retry,
                        slot_receipt=slot_receipt,
                    )
                    witnessed = claimed.previous.observation_retry
                    if witnessed is not None:
                        with pytest.raises(
                            ValueError, match="observation_input_pin_mismatch"
                        ):
                            verify_observation_result(
                                claimed.opened.scope,
                                message,
                                claimed.previous.snapshot(),
                                selection,
                                normalized,
                            )
                        different_reason = message.inputs.retry.model_copy(
                            update={"reason": "changed-reason"}
                        )
                        tampered_retry = message.model_copy(
                            update={
                                "inputs": message.inputs.model_copy(
                                    update={"retry": different_reason}
                                )
                            }
                        )
                        with pytest.raises(
                            ValueError, match="observation_input_pin_mismatch"
                        ):
                            verify_observation_result(
                                claimed.opened.scope,
                                tampered_retry,
                                claimed.previous.snapshot(),
                                selection,
                                normalized,
                                retry_witness=witnessed,
                            )
                    if isinstance(message.output, FailedNodeOutput):
                        for update in (
                            {"retryable": False},
                            {"error_class": "timeout"},
                        ):
                            forged = message.output.model_copy(
                                update={
                                    "error": message.output.error.model_copy(
                                        update=update
                                    )
                                }
                            )
                            altered = message.model_copy(update={"output": forged})
                            with pytest.raises(
                                ValueError, match="observation_failure_pin_mismatch"
                            ):
                                verify_observation_result(
                                    claimed.opened.scope,
                                    altered,
                                    claimed.previous.snapshot(),
                                    selection,
                                    normalized,
                                    failure=recorded.result.error,
                                    retry_witness=witnessed,
                                    slot_receipt=slot_receipt,
                                )
                    elif normalized is not None:
                        with pytest.raises(
                            ValueError, match="joint_slot_receipt_mismatch"
                        ):
                            verify_observation_result(
                                claimed.opened.scope,
                                message,
                                claimed.previous.snapshot(),
                                selection,
                                normalized,
                                retry_witness=witnessed,
                                slot_receipt=(("foreign-slot",), ()),
                            )
                    if (
                        case == "multiple_objects"
                        and message.batch.outcome == "complete"
                    ):
                        original = message.batch.question_results[1]
                        forged = original.model_copy(
                            update={
                                "primary_fact": original.primary_fact.model_copy(
                                    update={
                                        "value": EnumValue(
                                            kind="enum", value="unparked"
                                        )
                                    }
                                ),
                            }
                        )
                        altered = message.model_copy(
                            update={
                                "batch": message.batch.model_copy(
                                    update={
                                        "question_results": (
                                            message.batch.question_results[0],
                                            forged,
                                            message.batch.question_results[2],
                                        ),
                                    }
                                ),
                            }
                        )
                        with pytest.raises(
                            ValueError, match="observation_fact_pin_mismatch"
                        ):
                            verify_observation_result(
                                claimed.opened.scope,
                                altered,
                                claimed.previous.snapshot(),
                                selection,
                                normalized,
                            )
                    for asset in message.durable_request_evidence:
                        assert (
                            sha256_digest(
                                (tmp_path / asset.storage.object_key).read_bytes()
                            )
                            == asset.sha256
                        )

                async def validate_perception(claimed, message):
                    # The stock fixture has a single binding; semantic rejection still
                    # requires the exact normalized wire from its acknowledged receipt.
                    if not isinstance(message, DiscoveryMessage):
                        assert not message.undispatched_profiles
                        return
                    for gap in message.group_failures:
                        (invocation,) = tuple(
                            row
                            for row in message.invocations
                            if row.model_invocation_id == gap.dispatch_id
                        )
                        assert invocation.provider_request_id is not None
                        _, raw_request, raw_response = wire[
                            int(invocation.provider_request_id.removeprefix("wp05-"))
                            - 1
                        ]
                        assert sha256_digest(raw_request) == invocation.request_digest
                        await fixture.gateway.verify_model_diagnostic(
                            claimed,
                            gap.dispatch_id,
                            ModelBindingContentRef.model_validate(
                                gap.binding_ref.model_dump()
                            ),
                            gap.profile_refs,
                            gap.boundary,
                            None,
                            gap.model_invocation_ref,
                        )
                        actual = await fixture.journal.get_dispatch(
                            claimed.claim.scope,
                            claimed.claim.execution_id,
                            gap.dispatch_id,
                        )
                        assert actual is not None and actual.results[
                            0
                        ].result.response_digest == sha256_digest(raw_response)
                        content = json.loads(raw_response)["choices"][0]["message"][
                            "content"
                        ].encode()
                        verify_discovery_rejection(claimed.opened.scope, gap, content)

                async def validate_decision(claimed, message):
                    pytest.fail("this test ends at N04")

                def worker() -> WorkflowWorker:
                    return WorkflowWorker(
                        fixture.application,
                        fixture.releases,
                        fixture.store,
                        host=fixture.host,
                        bundle_limits=LIMITS,
                        adapters=WorkflowAdapters(
                            invoke=execute,
                            profile_gate=build_profile_gate,
                            validate_observation=validate_observation,
                            validate_decision=validate_decision,
                            validate_perception=validate_perception,
                            verify_invocation=fixture.gateway.verify_invocation,
                        ),
                    )

                accepted = fixture.accepted
                scope, execution_id = (
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                )

                async def step() -> WorkerStep:
                    return await worker().run_once(
                        scope, execution_id, owner="perception-test", lease_ms=10000
                    )

                assert await step() is WorkerStep.APPLIED
                context = observed[0].message
                assert (
                    isinstance(context, ContextMessage) and context.snapshot is not None
                )
                assert context.snapshot.device_ref.version == "1.0.2"
                assert context.snapshot.camera_ref.version == "1.0.3"
                (parking_area,) = context.snapshot.values
                assert parking_area.field_path == ("parking_area",)
                assert parking_area.provenance.source.value == "region_definition"
                assert await step() is WorkerStep.APPLIED
                if case == "bad_original":
                    assert len(observed) == 1 and not wire
                    return
                discovery = observed[1].message
                assert isinstance(discovery, DiscoveryMessage)
                if case in ("no_hits", "unregistered_profile"):
                    assert discovery.output.outcome == (
                        "no_hits" if case == "no_hits" else "failed"
                    )
                    assert not discovery.selections and len(wire) == 1
                    return
                assert len(discovery.selections) == 1
                assert await step() is WorkerStep.APPLIED
                grounding = observed[2].message
                assert isinstance(grounding, GroundingMessage)
                (localized,) = grounding.records
                if case in ("no_target", "invalid_box"):
                    assert localized.outcome is (
                        GroundingOutcome.NO_TARGET
                        if case == "no_target"
                        else GroundingOutcome.FAILED
                    )
                    assert not grounding.objects and len(wire) == 2
                    return

                assert localized.outcome is GroundingOutcome.OBJECTS
                assert len(grounding.objects) == 2
                assert len({item.object_id for item in grounding.objects}) == 2
                assert all(
                    obj.effective_question_refs
                    == discovery.selections[0].effective_question_refs
                    for obj in grounding.objects
                )
                first_disposition = await step()
                observations = []

                def capture() -> list[ObservationMessage]:
                    return [
                        row.message
                        for row in observed
                        if isinstance(row.message, ObservationMessage)
                    ]

                if case == "multiple_objects":
                    assert first_disposition is WorkerStep.APPLIED
                    # An independent ToolPlan may finish between the two observations.
                    for _ in range(len(grounding.objects) * 2):
                        if len(capture()) == len(grounding.objects):
                            break
                        assert await step() is WorkerStep.APPLIED
                    observations = capture()
                    assert len(observations) == 2 and len(wire) == 4
                    first, second = observations
                    assert first.batch.outcome == "partial"
                    assert [fact.status for fact in first.facts] == [
                        FactStatus.UNKNOWN,
                        FactStatus.ERROR,
                        FactStatus.ERROR,
                    ]
                    assert (
                        first.facts[0].reason.reason_code == "insufficient_visibility"
                    )
                    assert (
                        first.facts[1].reason.reason_code
                        == "perception.question_invalid"
                    )
                    assert (
                        first.facts[2].reason.reason_code
                        == "perception.question_missing"
                    )
                    assert all("value" not in fact.model_dump() for fact in first.facts)
                    assert second.batch.outcome == "complete"
                    assert (
                        first.invocation.request_digest
                        != second.invocation.request_digest
                    )
                    assert first.batch.object_ref != second.batch.object_ref
                    with pytest.raises(ValueError, match="Object Attempt"):
                        first.inputs.check_batch(
                            first.batch, (second.invocation,), None
                        )
                    with pytest.raises(
                        ValueError, match="Object, invocation and batch Attempt"
                    ):
                        first.batch.model_validate(
                            {
                                **first.batch.model_dump(),
                                "question_results": (
                                    second.batch.question_results[0],
                                    *first.batch.question_results[1:],
                                ),
                            }
                        )
                else:
                    assert first_disposition is WorkerStep.RETRY_SCHEDULED
                    (failed,) = capture()
                    if case == "receipt_mismatch_retry":
                        assert failed.batch.outcome == "association_error"
                        assert (
                            failed.output.error.code
                            == "perception.joint_slot_receipt_mismatch"
                        )
                    else:
                        assert failed.batch.outcome == "parse_error"
                    assert failed.batch.question_results == ()
                    assert (
                        failed.authority is None
                        and failed.output.status is NodeRunStatus.FAILED
                    )
                    await asyncio.sleep(0.03)
                    # Every step creates a new WorkflowWorker. The prior batch is read from PostgreSQL.
                    for _ in range(4):
                        result = await step()
                        if any(
                            row.batch.retry_of_batch_ref is not None
                            for row in capture()
                        ):
                            assert result is WorkerStep.APPLIED
                            break
                    observations = capture()
                    (retry,) = [
                        row
                        for row in observations
                        if row.batch.retry_of_batch_ref == failed.batch.batch_ref
                    ]
                    assert (
                        retry.authority is not None
                        and retry.authority.reason == "explicit_retry_resolution"
                    )
                    assert retry.batch.attempt_ref != failed.batch.attempt_ref
                    assert retry.batch.object_ref == failed.batch.object_ref
                    assert (
                        retry.invocation.request_digest
                        == failed.invocation.request_digest
                    )
                    assert (
                        retry.invocation.model_invocation_id
                        != failed.invocation.model_invocation_id
                    )
                    assert (
                        retry.inputs.retry is not None
                        and retry.inputs.retry.reason == failed.output.error.code
                    )

                assert len(
                    {row.invocation.model_invocation_id for row in observations}
                ) == len(observations)
                for row in observations:
                    invocation = row.invocation
                    receipt = await fixture.journal.get_dispatch(
                        scope, execution_id, invocation.model_invocation_id
                    )
                    assert receipt is not None and receipt.send_admitted
                    assert receipt.attempt_id == row.batch.attempt_ref.record_id
                    assert receipt.intent.target.object_ref == row.batch.object_ref
                    assert receipt.intent.request_digest == invocation.request_digest
                    assert (
                        row.inputs.compiled_prompt_evidence_ref,
                        row.inputs.contract_snapshot.response_schema_evidence_ref,
                    ) == tuple(
                        item
                        for item in invocation.evidence_refs
                        if item.record_id.startswith("request.")
                    )
                    request_digests = {sha256_digest(raw) for _, raw, _ in wire}
                    assert invocation.request_digest in request_digests
                    for asset in row.durable_request_evidence:
                        assert (
                            sha256_digest(
                                (tmp_path / asset.storage.object_key).read_bytes()
                            )
                            == asset.sha256
                        )

    asyncio.run(run())


@pytest.mark.parametrize("stage", ("discovery", "grounding"))
def test_unusable_model_groups_preserve_disjoint_profile_work(
    tmp_path: Path,
    loopback_only: set[int],
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    from jimu_runtime.domain.definition_enums import ModelCapability
    from jimu_runtime.gateway.bridge import ModelCallFailure

    async def run() -> None:
        seen: list[tuple[str, str]] = []
        results: list[NodeResult] = []

        async def peer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            inbound = await http_support.read_request(reader)
            body = json.loads(inbound.body)
            text = body["messages"][0]["content"][0]["text"]
            params = json.loads(text.splitlines()[-1])
            if "eligible_profile_ids" in params:
                (profile,) = params["eligible_profile_ids"]
                finding = (
                    "foreign.profile"
                    if stage == "discovery" and profile.endswith("secondary")
                    else profile
                )
                content = {"profile_hits": [finding]}
                seen.append(("discovery", profile))
            else:
                profile = params["profile_id"]
                box = (
                    [0, 0, 65, 4]
                    if stage == "grounding" and profile.endswith("secondary")
                    else [0, 0, 4, 4]
                )
                content = {"boxes": [{"xyxy": box}]}
                seen.append(("grounding", profile))
            raw = http_support.chat_body(
                model=body["model"], content=json.dumps(content)
            )
            writer.write(
                http_support.response(
                    raw,
                    headers=(
                        (b"X-Request-ID", f"grouped-{len(seen)}".encode()),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(
                tmp_path, url, with_media=True, multi_profile=True
            ) as fixture:
                gateway, media = fixture.gateway, fixture.media_store
                assert media is not None
                original_plan = gateway._plan

                def plan(claimed, policy, operation, *args):
                    target = (
                        ModelCapability.PROFILE_DISCOVERY
                        if stage == "discovery"
                        else ModelCapability.OBJECT_GROUNDING
                    )
                    if target in operation.purposes and any(
                        item.artifact_id.endswith("tertiary")
                        for item in operation.profile_refs
                    ):
                        raise ModelCallFailure("model.grouped_fixture_unavailable")
                    return original_plan(claimed, policy, operation, *args)

                monkeypatch.setattr(gateway, "_plan", plan)

                async def execute(claimed):
                    result = await fixture.adapter.execute(claimed, gateway)
                    results.append(result)
                    return result

                async def verify_perception(claimed, message):
                    if isinstance(message, DiscoveryMessage):
                        gaps = message.group_failures
                    else:
                        gaps = message.undispatched_profiles
                    for gap in gaps:
                        is_discovery = isinstance(message, DiscoveryMessage)
                        ref = gap.model_invocation_ref if is_discovery else None
                        profiles = (
                            gap.profile_refs if is_discovery else (gap.profile_ref,)
                        )
                        wire_success = ref is not None and any(
                            row.model_invocation_id == ref.record_id
                            and row.outcome.status == "succeeded"
                            for row in message.invocations
                        )
                        await gateway.verify_model_diagnostic(
                            claimed,
                            gap.dispatch_id,
                            ModelBindingContentRef.model_validate(
                                gap.binding_ref.model_dump()
                            ),
                            profiles,
                            gap.boundary,
                            None if wire_success else gap.error.code,
                            ref,
                        )
                        if wire_success:
                            stored = await media.read_model_response(
                                claimed.claim.scope,
                                claimed.claim.execution_id,
                                gap.dispatch_id,
                                late=False,
                            )
                            verify_discovery_rejection(
                                claimed.opened.scope, gap, stored.normalized
                            )

                async def unused(*_):
                    pytest.fail("this test ends at N03")

                accepted = fixture.accepted
                scope, eid = (
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                )
                worker = WorkflowWorker(
                    fixture.application,
                    fixture.releases,
                    fixture.store,
                    host=fixture.host,
                    bundle_limits=LIMITS,
                    adapters=WorkflowAdapters(
                        invoke=execute,
                        profile_gate=build_profile_gate,
                        validate_perception=verify_perception,
                        verify_invocation=gateway.verify_invocation,
                        validate_observation=unused,
                        validate_decision=unused,
                    ),
                )
                for _ in range(3):
                    assert (
                        await worker.run_once(
                            scope, eid, owner="grouped-profile-test", lease_ms=10000
                        )
                        is WorkerStep.APPLIED
                    )
                _, discovered, grounded = (result.message for result in results)
                assert isinstance(discovered, DiscoveryMessage)
                assert isinstance(grounded, GroundingMessage)
                if stage == "discovery":
                    assert discovered.output.outcome == "partial"
                    assert [
                        hit.profile_ref.artifact_id
                        for hit in discovered.output.profile_hits
                    ] == ["nonmotor_vehicle"]
                    assert len(discovered.group_failures) == 2
                    by_profile = {
                        row.profile_refs[0].artifact_id: row
                        for row in discovered.group_failures
                    }
                    bad = by_profile["nonmotor_vehicle_secondary"]
                    assert bad.error.code == "perception.invalid_profile_proposal"
                    assert (
                        bad.model_invocation_ref is not None
                        and bad.boundary == "acknowledged"
                    )
                    bad_invocation_id = bad.model_invocation_ref.record_id
                    blocked = by_profile["nonmotor_vehicle_tertiary"]
                    assert (
                        blocked.model_invocation_ref is None
                        and blocked.boundary == "not_dispatched"
                    )
                    assert len(seen) == 3
                else:
                    assert (
                        discovered.output.outcome == "hits"
                        and len(discovered.output.profile_hits) == 3
                    )
                    assert len(grounded.records) == 2 and len(grounded.objects) == 1
                    localized = {
                        row.profile_ref.artifact_id: row.outcome
                        for row in grounded.records
                    }
                    assert localized == {
                        "nonmotor_vehicle": GroundingOutcome.OBJECTS,
                        "nonmotor_vehicle_secondary": GroundingOutcome.FAILED,
                    }
                    (bad_invocation,) = tuple(
                        item
                        for item in grounded.invocations
                        if item.profile_refs[0].artifact_id
                        == "nonmotor_vehicle_secondary"
                    )
                    bad_invocation_id = bad_invocation.model_invocation_id
                    (blocked,) = grounded.undispatched_profiles
                    assert (
                        blocked.profile_ref.artifact_id == "nonmotor_vehicle_tertiary"
                        and blocked.boundary == "not_dispatched"
                    )
                    assert len(seen) == 5
                async with fixture.application.connect() as connection:
                    execution = await repository.get_execution(connection, scope, eid)
                    assert execution is not None
                    persisted = await load_records(connection, scope, execution)
                    for node, expected in (
                        (NodeId.N02, discovered),
                        (NodeId.N03, grounded),
                    ):
                        actual = persisted.messages[BranchKey(node)]
                        assert actual.worker_format == "configured-v1"
                        assert actual == expected.model_copy(
                            update={"worker_format": "configured-v1"}
                        )
                failed_wire = await fixture.journal.get_dispatch(
                    scope, eid, bad_invocation_id
                )
                assert failed_wire is not None and failed_wire.send_admitted
                (acknowledged,) = failed_wire.results
                assert (
                    not acknowledged.late
                    and acknowledged.result.boundary == "acknowledged"
                )
                stored_bad = await media.read_model_response(
                    scope, eid, bad_invocation_id, late=False
                )
                assert stored_bad.raw is not None and stored_bad.normalized is not None
                assert (
                    sha256_digest(stored_bad.raw) == acknowledged.result.response_digest
                )
                receipt = await fixture.journal.get_dispatch(
                    scope, eid, blocked.dispatch_id
                )
                assert receipt is not None and not receipt.send_admitted
                assert receipt.attempt_id == blocked.attempt_id
                assert (
                    receipt.results[0].result.error.code
                    == "model.grouped_fixture_unavailable"
                )
                assert all(
                    profile != "nonmotor_vehicle_tertiary"
                    for node, profile in seen
                    if node == stage
                )

    asyncio.run(run())
