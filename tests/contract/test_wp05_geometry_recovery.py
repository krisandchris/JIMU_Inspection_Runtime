"""Real child ledger recovery separates old audit records from publishable bytes."""

import asyncio
import json
from unittest.mock import patch

import pytest
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus
from jimu_runtime.domain.nodes.tool_execution import InvokedToolStep
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.tools.executor import ToolExecutor, _tool_input_refs
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import (
    StepAttemptStore,
    _StepInput,
    load_parent_step_history,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.worker import RetryableNodeFailure, WorkerStep
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05.perception import perception_fixture

loopback_only = http_support.loopback_only


@pytest.mark.parametrize("failure", ["original_lost", "geometry_lost", "legacy"])
def test_n05_preserves_audit_without_publishing_unverified_results(
    tmp_path, loopback_only, failure
):
    async def run():
        async def peer(reader, writer):
            incoming = await http_support.read_request(reader)
            body = json.loads(incoming.body)
            role = body["response_format"]["json_schema"]["schema"]["$id"].split(":")[
                -2
            ]
            reply = {
                "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
                "object_grounding-result": {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
                "object_observation-result": {
                    "vehicle_type": "electric_bicycle",
                    "parking_state": "parked",
                    "parking_position": "outside_parking_area",
                },
                "candidate_prediction-result": {"categories": []},
            }[role]
            writer.write(
                http_support.response(
                    http_support.chat_body(
                        model=body["model"], content=json.dumps(reply)
                    ),
                    headers=(
                        (b"X-Request-ID", b"wp05-audit"),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
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
                store = StepAttemptStore(fixture.application, actor=ACTOR)
                registry = ToolWorkerRegistry(
                    synthetic_cpu_implementations(
                        fixture.resources,
                        "cpu",
                        "geometry-audit",
                        definitions,
                        fixture.tool_proofs,
                        synthetic_image_digests=frozenset(
                            {accepted.task.media_ref.sha256}
                        ),
                    )
                )
                bridge = ToolExecutionAdapter(
                    ToolExecutor(registry),
                    store,
                    fixture.journal,
                    fixture.host.capabilities,
                )
                geometry = GeometryNodeAdapter(media, bridge)
                base_step = GeometryNodeAdapter._step
                base_read = LocalMediaStore.read_tool_sidecar
                initial_begin, initial_complete = store.begin, store.complete
                cut = False
                lost_asset = None

                async def old_begin(claim, inputs, step, digest, **kwargs):
                    if (
                        failure == "legacy"
                        and not cut
                        and step.step_id == "wheel_boundary_geometry"
                    ):
                        # Reproduce the old writer while retaining real DB-owned steps and receipts.
                        kwargs.pop("ancestor_evidence")
                        kwargs.pop("ancestor_tools")
                        with patch.object(
                            _StepInput,
                            "has_required_ancestor_inputs",
                            return_value=True,
                        ):
                            return await initial_begin(
                                claim, inputs, step, digest, **kwargs
                            )
                    return await initial_begin(claim, inputs, step, digest, **kwargs)

                async def old_complete(handle, result):
                    tool = result.execution
                    if (
                        failure == "legacy"
                        and not cut
                        and tool.tool_ref.artifact_id == "wheel_boundary_geometry"
                    ):
                        tool = tool.model_copy(
                            update={
                                "input_refs": _tool_input_refs(handle.inputs.envelope)
                            }
                        )
                        result = result.model_copy(update={"execution": tool})
                    return await initial_complete(handle, result)

                async def step_hook(
                    adapter,
                    claimed,
                    inputs,
                    facts,
                    original,
                    region,
                    state,
                    step,
                    definition,
                    failures,
                ):
                    nonlocal cut
                    await base_step(
                        adapter,
                        claimed,
                        inputs,
                        facts,
                        original,
                        region,
                        state,
                        step,
                        definition,
                        failures,
                    )
                    cut_step = {
                        "original_lost": "wheel_segmentation",
                        "legacy": "wheel_boundary_geometry",
                    }.get(failure)
                    if not cut and step.step_id == cut_step:
                        assert state.completions[step.step_id].selection is not None
                        cut = True
                        if failure == "original_lost":
                            (
                                tmp_path / "media" / original.asset.storage.object_key
                            ).unlink()
                        raise RetryableNodeFailure("transport")

                async def read_hook(
                    owner,
                    read_scope,
                    execution_id,
                    dispatch_id,
                    slot,
                    *,
                    expected_asset=None,
                ):
                    nonlocal lost_asset
                    if (
                        failure == "geometry_lost"
                        and slot == "geometry"
                        and lost_asset is None
                    ):
                        assert expected_asset is not None
                        lost_asset = expected_asset
                        (
                            tmp_path / "media" / expected_asset.storage.object_key
                        ).unlink()
                    return await base_read(
                        owner,
                        read_scope,
                        execution_id,
                        dispatch_id,
                        slot,
                        expected_asset=expected_asset,
                    )

                async def capture_registry(request_scope, task):
                    source = await fixture.host.worker_source(request_scope, task)
                    return source.device, source.camera

                def open_worker():
                    return inspection_worker(
                        fixture.application,
                        fixture.releases,
                        fixture.store,
                        host=VerifiedMediaWorkerHost(
                            media, fixture.host.capabilities, capture_registry
                        ),
                        bundle_limits=LIMITS,
                        media=media,
                        gateway=fixture.gateway,
                        execute_tools=geometry.execute,
                        model_resource_scope_id="model",
                    )

                tool = None
                with (
                    patch.object(GeometryNodeAdapter, "_step", step_hook),
                    patch.object(LocalMediaStore, "read_tool_sidecar", read_hook),
                    patch.object(store, "begin", old_begin),
                    patch.object(store, "complete", old_complete),
                ):
                    for index in range(20):
                        step = await open_worker().run_once(
                            scope, eid, owner=f"audit-{index}", lease_ms=10000
                        )
                        async with fixture.application.connect() as connection:
                            locked = await repository.get_execution(
                                connection, scope, eid
                            )
                            assert locked is not None
                            state = await load_records(connection, scope, locked)
                        tool = next(
                            (
                                item
                                for item in state.messages.values()
                                if item.kind == "tool"
                            ),
                            None,
                        )
                        if tool is not None:
                            break
                        if step in {WorkerStep.IDLE, WorkerStep.RETRY_SCHEDULED}:
                            await asyncio.sleep(0.04)

                assert tool is not None
                assert tool.output.envelope.status is NodeRunStatus.FAILED
                assert tool.output.envelope.output_artifacts == ()
                retained = 1 if failure == "original_lost" else 3
                assert len(tool.executions) == len(tool.authorities) == retained
                assert (
                    sum(
                        isinstance(step, InvokedToolStep)
                        for step in tool.output.summary.steps
                    )
                    == retained
                )
                branch = state.inputs(tool.inputs.object_record.object_id)
                facts, selections = branch.selected_facts()
                assert not {fact.fact_id for fact in facts} & {
                    fact.fact_id for fact in tool.facts
                }
                assert not set(selections) & set(tool.authorities)
                if lost_asset is not None:
                    assert lost_asset in tool.evidence
                    assert lost_asset not in branch.selected_evidence()
                async with fixture.application.connect() as connection:
                    restored = await load_parent_step_history(
                        connection, scope, tool.inputs
                    )
                    ledger = (
                        await connection.execute(
                            text("""
                                SELECT step_id, (record::jsonb ? 'ancestor_inputs')
                                FROM jimu_workflow.tool_step_attempts
                                WHERE project_id = :project AND client_id = :client AND execution_id = :execution
                            """),
                            {
                                "project": scope.project_id,
                                "client": scope.client_id,
                                "execution": eid,
                            },
                        )
                    ).all()
                assert len(ledger) == retained
                if failure == "legacy":
                    assert ("wheel_boundary_geometry", False) in ledger
                assert restored is not None
                assert {item.selection for item in restored.entries} == set(
                    tool.authorities
                )
                if failure != "geometry_lost":
                    assert restored.parent_attempt.attempt_number == 2

    asyncio.run(run())
