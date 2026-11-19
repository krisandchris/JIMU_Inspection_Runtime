"""Run installed composition against actual PostgreSQL, HTTP and CPU child processes."""

import asyncio
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, AttemptStatus, NodeRunStatus
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.journal_records import ToolDispatchResult
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow import worker as worker_module
from jimu_runtime.workflow.jobs import NODE_OUTPUT_KEY
from jimu_runtime.workflow.worker import (
    AdapterRejected,
    ClaimedNode,
    WorkerSourceError,
    WorkerStep,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_inspection import (
    InspectionNodes,
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_records import MESSAGE_KEY, ObservationMessage
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05.perception import perception_fixture

loopback_only = http_support.loopback_only

_BBOX = [40, 23, 52, 33]
_ANSWERS = {
    "vehicle_type": "electric_bicycle",
    "parking_state": "parked",
    "parking_position": "over_boundary",
}


@pytest.mark.parametrize(
    "candidate_case",
    [
        "usable",
        "transport_failure",
        "storage_failure",
        "missing_geometry",
        "observation_exhausted",
    ],
)
def test_configured_worker_executes_signed_parking_scene(
    tmp_path: Path,
    loopback_only: set[int],
    candidate_case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_indexes: list[str] = []
    read_types: list[EvidenceType] = []
    original_sources = InspectionNodes._decision_sources
    original_read = InspectionNodes._read_evidence

    def check_source_pins(nodes: InspectionNodes, claimed: ClaimedNode):
        originals = tuple(
            row
            for row in claimed.evidence
            if row.evidence_type is EvidenceType.ORIGINAL_IMAGE
        )
        assert originals
        asset = originals[0]
        substituted = asset.model_copy(update={"producer_version": "9.9.9"})
        with pytest.raises(
            AdapterRejected, match="inspection_evidence_snapshot_mismatch"
        ):
            original_sources(
                nodes,
                replace(
                    claimed,
                    evidence=tuple(
                        substituted if row == asset else row for row in claimed.evidence
                    ),
                ),
            )
        source_indexes.append(claimed.attempt.attempt_id)
        return original_sources(nodes, claimed)

    async def count_read(nodes, claimed, row):
        read_types.append(row.evidence_type)
        if (
            candidate_case == "missing_geometry"
            and row.evidence_type is EvidenceType.GEOMETRY
        ):
            if read_types.count(EvidenceType.GEOMETRY) == 1:
                return None
        return await original_read(nodes, claimed, row)

    monkeypatch.setattr(InspectionNodes, "_decision_sources", check_source_pins)
    monkeypatch.setattr(InspectionNodes, "_read_evidence", count_read)

    if candidate_case == "storage_failure":
        persist = LocalMediaStore.stage_model_response

        async def interrupt_candidate_write(
            media, scope, execution_id, dispatch_id, receipt
        ):
            if receipt.parsed_content is not None and "categories" in json.loads(
                receipt.parsed_content
            ):
                raise OSError("fixture evidence write failed")
            return await persist(media, scope, execution_id, dispatch_id, receipt)

        monkeypatch.setattr(
            LocalMediaStore, "stage_model_response", interrupt_candidate_write
        )

    async def run() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )
        captured: list[tuple[str, bytes]] = []
        calls: Counter[str] = Counter()

        async def peer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await http_support.read_request(reader)
            body = json.loads(request.body)
            schema = body["response_format"]["json_schema"]["schema"]
            name = schema["$id"].split(":")[-2]
            calls[name] += 1
            if (
                name == "candidate_prediction-result"
                and candidate_case == "transport_failure"
            ):
                writer.close()
                return
            reply = {
                "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
                "object_grounding-result": {"boxes": [{"xyxy": _BBOX}]},
                "object_observation-result": _ANSWERS,
                "candidate_prediction-result": {"categories": []},
            }[name]
            if name == "object_observation-result" and (
                calls[name] == 1 or candidate_case == "observation_exhausted"
            ):
                reply = {**_ANSWERS, "object_id": "outside.claimed.object"}
            raw = http_support.chat_body(model=body["model"], content=json.dumps(reply))
            captured.append((name, raw))
            writer.write(
                http_support.response(
                    raw,
                    headers=(
                        (b"X-Request-ID", f"inspection-{len(captured)}".encode()),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
                from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
                    ACTOR,
                )

                media = fixture.media_store
                assert media is not None
                accepted = fixture.accepted
                scope, eid = (
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                )
                catalogue = fixture.scene.execution.catalogue
                definitions = tuple(
                    require_definition(catalogue, item.ref, ToolDefinition)
                    for item in catalogue.of_kind(ArtifactKind.TOOL)
                )
                tool_registry = ToolWorkerRegistry(
                    synthetic_cpu_implementations(
                        fixture.resources,
                        "cpu",
                        "inspection-tools",
                        definitions,
                        fixture.tool_proofs,
                        synthetic_image_digests=frozenset(
                            {accepted.task.media_ref.sha256}
                        ),
                    )
                )
                tool_bridge = ToolExecutionAdapter(
                    ToolExecutor(tool_registry),
                    StepAttemptStore(fixture.application, actor=ACTOR),
                    fixture.journal,
                    fixture.host.capabilities,
                )
                tools = GeometryNodeAdapter(media, tool_bridge)
                capture_reads = 0

                async def capture_registry(request_scope, task):
                    nonlocal capture_reads
                    capture_reads += 1
                    source = await fixture.host.worker_source(request_scope, task)
                    if capture_reads > 1:
                        raise AssertionError(
                            "a completed N01 must reopen the durable capture revisions"
                        )
                    return source.device, source.camera

                host = VerifiedMediaWorkerHost(
                    media, fixture.host.capabilities, capture_registry
                )

                def open_worker():
                    return inspection_worker(
                        fixture.application,
                        fixture.releases,
                        fixture.store,
                        host=host,
                        bundle_limits=LIMITS,
                        media=media,
                        gateway=fixture.gateway,
                        execute_tools=tools.execute,
                        model_resource_scope_id="model",
                    )

                dispositions = []
                for index in range(20):
                    step = await open_worker().run_once(
                        scope, eid, owner=f"inspection-{index}", lease_ms=10000
                    )
                    dispositions.append(step)
                    if index == 0:
                        assert step is WorkerStep.APPLIED
                        legacy = WorkflowWorker(
                            fixture.application,
                            fixture.releases,
                            fixture.store,
                            host=host,
                            bundle_limits=LIMITS,
                        )
                        with pytest.raises(
                            WorkerSourceError,
                            match="worker_message_requires_configured_reader",
                        ):
                            await legacy.run_once(
                                scope, eid, owner="legacy", lease_ms=10000
                            )
                    if step is WorkerStep.FINALIZED:
                        break
                    if step in {WorkerStep.IDLE, WorkerStep.RETRY_SCHEDULED}:
                        await asyncio.sleep(0.025)
                final = await open_worker().read_finalization(scope, eid)
                assert final is not None, tuple(dispositions)
                assert capture_reads == 1
                assert WorkerStep.RETRY_SCHEDULED in dispositions
                assert WorkerStep.STAGED in dispositions
                assert len(source_indexes) == 2 and len(set(source_indexes)) == 1
                assert EvidenceType.ORIGINAL_IMAGE in read_types
                assert EvidenceType.MASK in read_types
                assert EvidenceType.MODEL_REQUEST not in read_types
                (decision,) = final.decisions
                if candidate_case in {"missing_geometry", "observation_exhausted"}:
                    assert decision.result.value == "uncertain", (
                        decision.model_dump_json()
                    )
                    assert not final.coverage.matches
                    if candidate_case == "missing_geometry":
                        assert read_types.count(EvidenceType.GEOMETRY) == 1
                else:
                    assert decision.result.value == "violation", (
                        decision.model_dump_json()
                    )
                    assert len(final.coverage.matches) == 1
                assert calls == {
                    "profile_discovery-result": 1,
                    "object_grounding-result": 1,
                    "object_observation-result": 2,
                    "candidate_prediction-result": 1,
                }

                async with fixture.application.connect() as connection:
                    locked = await repository.get_execution(connection, scope, eid)
                    assert locked is not None
                    state = await load_records(connection, scope, locked)
                if candidate_case == "storage_failure":
                    message = next(
                        row
                        for key, row in state.messages.items()
                        if key.node_id is NodeId.N06
                    )
                    invoice = message.invocation
                    assert invoice is not None and invoice.outcome.status == "succeeded"
                    stored = await media.read_model_response(
                        scope, eid, invoice.model_invocation_id, late=False
                    )
                    assert (
                        stored.receipt.response_evidence_error
                        == "model.response_evidence_store_unavailable"
                    )
                    assert stored.normalized is None
                    assert (
                        message.draft.candidate.failure_code
                        == stored.receipt.response_evidence_error
                    )
                inputs = state.inputs(decision.object_ref.record_id)
                assert inputs.observation is not None
                if candidate_case == "observation_exhausted":
                    assert inputs.observation.authority is None
                    assert inputs.observation.batch.outcome == "parse_error"
                    assert inputs.observation.batch.retry_of_batch_ref is not None
                    assert not inputs.observation.facts
                    assert dispositions.count(WorkerStep.RETRY_SCHEDULED) == 1
                    return
                assert inputs.observation.authority is not None
                assert inputs.observation.batch.retry_of_batch_ref is not None
                observation_node = next(
                    node
                    for key, node in state.runs.items()
                    if key.node_id is NodeId.N04
                )
                old, current = observation_node.attempt_history
                assert current.attempt_ref == inputs.observation.batch.attempt_ref
                async with fixture.application.connect() as connection:
                    first_batch = await repository.read_output(
                        connection,
                        scope,
                        eid,
                        observation_node.node_run_id,
                        old.attempt_ref.record_id,
                        MESSAGE_KEY,
                        ObservationMessage,
                    )
                assert first_batch is not None and first_batch.authority is None
                assert (
                    first_batch.durable_request_evidence
                    == inputs.observation.durable_request_evidence
                )
                for request_asset in first_batch.durable_request_evidence:
                    content = await media.read_observation_request(
                        scope,
                        eid,
                        request_asset.evidence_id,
                        request_asset.storage,
                        request_asset.sha256,
                        request_asset.byte_size,
                    )
                    assert sha256_digest(content) == request_asset.sha256
                    assert not await media.cleanup_unreferenced_observation_request(
                        scope, eid, request_asset.evidence_id
                    )
                assert (
                    inputs.tool is not None
                    and inputs.tool.output.summary.status == "succeeded"
                )
                assert len(inputs.tool.executions) == 3
                for execution in inputs.tool.executions:
                    row = await fixture.journal.get_dispatch(
                        scope, eid, execution.tool_execution_id
                    )
                    assert row is not None and row.send_admitted
                    assert row.intent.input_digest != row.intent.request_digest
                    (recorded,) = row.results
                    assert not recorded.late
                    assert isinstance(recorded.result, ToolDispatchResult)
                    assert recorded.result.acknowledgement == "tool_process"
                    provisional = recorded.result.execution
                    assert (
                        provisional is not None
                        and provisional.authority_observation is not None
                    )
                    assert (
                        provisional.authority_observation.arrival_qualification
                        == "unresolved"
                    )
                    assert execution.authority_observation is not None
                    assert (
                        execution.authority_observation.arrival_qualification
                        == "on_time"
                    )
                    assert (
                        provisional.model_copy(
                            update={
                                "authority_observation": execution.authority_observation
                            }
                        ).stable_json()
                        == execution.stable_json()
                    )
                tool = inputs.tool.executions[-1]
                output = await media.read_tool_sidecar(
                    scope, eid, tool.tool_execution_id, "geometry"
                )
                assert output.asset in inputs.tool.evidence
                assert len(output.content) > 0
                actual = await media.read_model_response(
                    scope,
                    eid,
                    inputs.observation.invocation.model_invocation_id,
                    late=False,
                )
                received = [
                    data
                    for name, data in captured
                    if name == "object_observation-result"
                ]
                assert actual.raw == received[-1]
                assert actual.receipt.response_digest == sha256_digest(received[-1])
                assert json.loads(actual.normalized) == _ANSWERS
                n06 = next(
                    node
                    for key, node in state.runs.items()
                    if key.node_id is NodeId.N06
                )
                expected = {row.evidence_id for row in inputs.selected_evidence()}
                assert expected == {
                    pin.ref.record_id
                    for pin in n06.upstream_inputs
                    if pin.ref.record_kind.value == "evidence"
                }
                assert (
                    await open_worker().read_finalization(
                        scope.model_copy(update={"client_id": "another.client"}), eid
                    )
                    is None
                )

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["cancellation", "invalid_recovery"])
def test_observation_retains_late_or_corrupt_attempt_disposition(
    tmp_path: Path,
    loopback_only: set[int],
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        received = asyncio.Event()
        cancelled = asyncio.Event()
        tool_called = asyncio.Event()

        async def peer(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await http_support.read_request(reader)
            body = json.loads(request.body)
            schema = body["response_format"]["json_schema"]["schema"]
            name = schema["$id"].split(":")[-2]
            if name == "object_observation-result":
                received.set()
                await asyncio.wait_for(cancelled.wait(), timeout=3)
            reply = {
                "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
                "object_grounding-result": {"boxes": [{"xyxy": _BBOX}]},
                "object_observation-result": _ANSWERS,
            }[name]
            raw = http_support.chat_body(model=body["model"], content=json.dumps(reply))
            writer.write(
                http_support.response(
                    raw,
                    headers=(
                        (b"X-Request-ID", b"late-cancel-peer"),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
                accepted = fixture.accepted
                scope, eid = (
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                )
                media = fixture.media_store
                assert media is not None

                async def unexpected_tool(claimed: ClaimedNode):
                    tool_called.set()
                    raise AssertionError("cancellation must prevent queued tool work")

                worker = inspection_worker(
                    fixture.application,
                    fixture.releases,
                    fixture.store,
                    host=fixture.host,
                    bundle_limits=LIMITS,
                    media=media,
                    gateway=fixture.gateway,
                    execute_tools=unexpected_tool,
                    model_resource_scope_id="model",
                )
                for _ in range(3):
                    assert (
                        await worker.run_once(
                            scope, eid, owner="timed-worker", lease_ms=10000
                        )
                        is WorkerStep.APPLIED
                    )

                if mode == "invalid_recovery":

                    async def invalid_retry(*_):
                        raise ValueError(
                            "configured fixture retry input disagrees with its exact prior Attempt"
                        )

                    monkeypatch.setattr(
                        worker_module, "read_observation_retry", invalid_retry
                    )
                    assert (
                        await worker.run_once(
                            scope, eid, owner="timed-worker", lease_ms=10000
                        )
                        is WorkerStep.APPLIED
                    )
                    async with fixture.application.connect() as connection:
                        locked = await repository.get_execution(connection, scope, eid)
                        assert locked is not None
                        state = await load_records(connection, scope, locked)
                        obs = next(
                            node
                            for key, node in state.runs.items()
                            if key.node_id is NodeId.N04
                        )
                        (link,) = obs.attempt_history
                        attempt = await repository.get_attempt(
                            connection,
                            scope,
                            eid,
                            obs.node_run_id,
                            link.attempt_ref.record_id,
                        )
                        outcome = await repository.read_output(
                            connection,
                            scope,
                            eid,
                            obs.node_run_id,
                            link.attempt_ref.record_id,
                            NODE_OUTPUT_KEY,
                            FailedNodeOutput,
                        )
                        assert (
                            attempt is not None
                            and attempt.status is AttemptStatus.FAILED
                        )
                        assert outcome is not None
                        assert (
                            outcome.error.code
                            == "worker_provider_worker_recovered_inputs_invalid"
                        )
                        assert not outcome.error.retryable
                    assert not received.is_set()
                    assert not tool_called.is_set()
                    return

                pending = asyncio.create_task(
                    worker.run_once(scope, eid, owner="timed-worker", lease_ms=10000)
                )
                try:
                    await asyncio.wait_for(received.wait(), timeout=3)
                    assert await worker.request_cancel(scope, eid)
                finally:
                    cancelled.set()
                assert await pending is WorkerStep.LATE_RECORDED

                async with fixture.application.connect() as connection:
                    locked = await repository.get_execution(connection, scope, eid)
                    assert locked is not None
                    state = await load_records(connection, scope, locked)
                    (tool,) = tuple(
                        node
                        for key, node in state.runs.items()
                        if key.node_id is NodeId.N05
                    )
                    assert tool.status is NodeRunStatus.CANCELLED
                    assert not tool.attempt_history
                    assert not state.tool_step_nodes
                    assert all(key.node_id is not NodeId.N06 for key in state.runs)
                    assert not tool_called.is_set()
                    obs = next(
                        node
                        for key, node in state.runs.items()
                        if key.node_id is NodeId.N04
                    )
                    (attempt,) = obs.attempt_history
                    row = (
                        await connection.execute(
                            text("""
                                SELECT response_key FROM jimu_workflow.late_responses
                                WHERE project_id = :project AND client_id = :client
                                AND execution_id = :execution AND node_run_id = :node
                            """),
                            {
                                "project": scope.project_id,
                                "client": scope.client_id,
                                "execution": eid,
                                "node": obs.node_run_id,
                            },
                        )
                    ).scalar_one()
                    batch = await repository.read_output(
                        connection,
                        scope,
                        eid,
                        obs.node_run_id,
                        attempt.attempt_ref.record_id,
                        f"late.artifact.{row}",
                        ObservationBatch,
                    )
                    assert batch is not None
                    assert batch.arrival_qualification == "late"
                    assert batch.outcome == "complete"
                    assert len(batch.question_results) == 3
                    assert all(key.node_id is not NodeId.N04 for key in state.messages)
                response = await media.read_model_response(
                    scope, eid, batch.model_invocation_ref.record_id, late=True
                )
                assert json.loads(response.normalized) == _ANSWERS
                invoice = response.receipt.invocation
                assert invoice is not None
                assert len(invoice.evidence_refs) == 2
                for ref in invoice.evidence_refs:
                    assert not await media.cleanup_unreferenced_observation_request(
                        scope, eid, ref.record_id
                    )
                assert await worker.read_finalization(scope, eid) is None

    asyncio.run(run())
