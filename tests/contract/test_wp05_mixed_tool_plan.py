"""Exercise the configured mixed N05 DAG with real fenced CPU child attempts."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch

import pytest

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    DIRECTORY,
    LIMITS,
    VERSION,
    _extract_slots,
    load_source,
    with_source_provenance,
)
from jimu_runtime.bundle.compiler import BundleCompileInput
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, ExecutionStatus, NodeRunStatus
from jimu_runtime.domain.enums import RuntimeRecordKind as K
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ToolPlanCompletionRecord,
    UninvokedToolStep,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.replay import ExecutionRef, ReplayExecutionSource
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import (
    GeometryInput,
    synthetic_cpu_implementations,
)
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.replay.artifact_source import load_historical_source
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import (
    StepAttempt,
    StepAttemptStore,
    load_parent_step_history,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.jobs import CompletionDisposition
from jimu_runtime.workflow.mock_support import artifact
from jimu_runtime.workflow.worker import (
    RetryableNodeFailure,
    WorkerStep,
    WorkflowWorker,
    _failed_node,
)
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_records import ToolMessage
from jimu_runtime.workflow.worker_state import load_records
from jimu_runtime.workflow.worker_validation import check_result
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05 import perception

loopback_only = http_support.loopback_only
_INDEPENDENT = "wheel_segmentation"
_DEPENDENT = "tactile_paving_segmentation"
_GUARDED_STEP = "wheel_boundary_geometry"
_GUARD_POLICY = "skip_when_unparked"
_PLAN = "nonmotor_vehicle_geometry"


def _mixed_source(*, guard_only: bool) -> BundleCompileInput:
    source = load_source()
    artifacts = []
    for item in source.artifacts:
        if guard_only and item.kind is ArtifactKind.TOOL_PLAN:
            data = json.loads(
                (DIRECTORY / "source" / "tool_plan" / f"{_PLAN}.json").read_bytes()
            )
            (step,) = [
                step for step in data["steps"] if step["step_id"] == _GUARDED_STEP
            ]
            step["skip_policy_ref"] = {"$pin": ["policy", _GUARD_POLICY, VERSION]}
            slots = []
            data = _extract_slots(data, slots)
            item = replace(
                item, content=json.dumps(data).encode(), reference_slots=tuple(slots)
            )
        elif (
            guard_only
            and item.kind is ArtifactKind.POLICY
            and item.artifact_id == "skip"
        ):
            guard = json.loads(item.content)
            rule = json.loads(
                (
                    DIRECTORY
                    / "source"
                    / "rule"
                    / "nonmotor_vehicle_illegal_parking.json"
                ).read_bytes()
            )
            guard["policy_id"] = _GUARD_POLICY
            guard["policy"]["condition_ast"] = rule["condition_ast"]["arguments"][0]
            slots = []
            guard = _extract_slots(guard, slots)
            artifacts.append(
                replace(
                    item,
                    artifact_id=_GUARD_POLICY,
                    path=f"policies/{_GUARD_POLICY}.json",
                    content=json.dumps(guard).encode(),
                    origin=replace(item.origin, artifact_id="mixed-guard-policy"),
                    reference_slots=tuple(slots),
                )
            )
        elif guard_only and item.kind is ArtifactKind.RULE:
            data = json.loads(
                (
                    DIRECTORY
                    / "source"
                    / "rule"
                    / "nonmotor_vehicle_illegal_parking.json"
                ).read_bytes()
            )
            data["optional_fact_selectors"].append(
                data["required_fact_selectors"].pop()
            )
            slots = []
            data = _extract_slots(data, slots)
            item = replace(
                item, content=json.dumps(data).encode(), reference_slots=tuple(slots)
            )
        elif not guard_only and item.kind is ArtifactKind.TOOL_PLAN:
            data = json.loads(item.content)
            (step,) = [row for row in data["steps"] if row["step_id"] == _DEPENDENT]
            binding = {
                "source": {
                    "source_kind": "fact",
                    "selector": {
                        "scope": "object",
                        "fact_key": "parking_state",
                        "fact_schema_ref": {
                            "$pin": ["fact_schema", "parking_state", VERSION]
                        },
                        "field_path": [],
                        "producer_selector": {
                            "producer_kind": "vlm_question",
                            "question_ref": {
                                "$pin": ["question", "parking_state", VERSION]
                            },
                            "output_name": "primary",
                        },
                    },
                },
                "target": {
                    "schema_ref": {
                        "$pin": ["tool_input_schema", f"{_DEPENDENT}-input", VERSION],
                        "schema_ref": True,
                    },
                    "field_path": ["parking_state"],
                },
                "required": True,
                "on_missing": "block",
            }
            step["input_bindings"].append(binding)
            slots = []
            data = _extract_slots(data, slots)
            item = replace(
                item,
                content=json.dumps(data).encode(),
                reference_slots=(*item.reference_slots, *slots),
            )
        elif not guard_only and item.artifact_id == f"{_DEPENDENT}-input":
            data = json.loads(item.content)
            data["properties"]["parking_state"] = {
                "type": "object",
                "properties": {
                    "kind": {"const": "enum"},
                    "value": {"type": "string", "enum": ["parked", "unparked"]},
                },
                "required": ["kind", "value"],
                "additionalProperties": False,
            }
            data["required"].append("parking_state")
            item = replace(item, content=json.dumps(data).encode())
        artifacts.append(item)
    return with_source_provenance(replace(source, artifacts=tuple(artifacts)))


def _no_invocations_source(*, empty: bool) -> BundleCompileInput:
    source = load_source()
    plan = json.loads(
        (DIRECTORY / "source" / "tool_plan" / f"{_PLAN}.json").read_bytes()
    )
    rule = json.loads(
        (
            DIRECTORY / "source" / "rule" / "nonmotor_vehicle_illegal_parking.json"
        ).read_bytes()
    )
    (condition, _) = rule["condition_ast"]["arguments"]
    rule["condition_ast"] = condition
    rule["required_fact_selectors"].pop()
    plan["steps"] = [] if empty else plan["steps"][:2]
    artifacts = []
    for item in source.artifacts:
        if item.kind is ArtifactKind.TOOL_PLAN and item.artifact_id == _PLAN:
            data = plan
        elif item.kind is ArtifactKind.RULE:
            data = rule
        elif (
            item.kind is ArtifactKind.POLICY
            and item.artifact_id == "skip"
            and not empty
        ):
            data = json.loads(item.content)
            data["policy"]["condition_ast"] = condition
        else:
            artifacts.append(item)
            continue
        slots = []
        data = _extract_slots(data, slots)
        artifacts.append(
            replace(
                item, content=json.dumps(data).encode(), reference_slots=tuple(slots)
            )
        )
    return with_source_provenance(replace(source, artifacts=tuple(artifacts)))


@pytest.mark.parametrize(
    "scenario", ["empty", "all_skipped", "unknown_guard", "historical"]
)
def test_n05_no_invocations_persists_exact_completion_and_unblocks_decision(
    tmp_path, loopback_only, scenario
):
    async def run():
        calls = []
        retried_attempts = []
        commit = WorkflowWorker._commit

        def check_submission(
            opened, previous, branch, node, attempt, result, **options
        ):
            message = result.message
            if (
                isinstance(message, ToolMessage)
                and message.completion_artifact is not None
            ):
                obj = message.inputs.object_record
                legacy = artifact(K.OBJECT, obj.object_id, obj)
                legacy_result = None
                for prior in ((), (legacy,)):
                    wire = message.model_dump(mode="json")
                    wire["output"]["envelope"]["output_artifacts"] = [
                        item.pin.model_dump(mode="json") for item in prior
                    ]
                    decoded = ToolMessage.model_validate_json(json.dumps(wire))
                    assert decoded.completion_artifact is None
                    legacy_result = NodeResult(decoded, prior)
                    with pytest.raises(
                        ValueError, match="may only persist its pinned summary"
                    ):
                        check_result(
                            opened,
                            previous,
                            branch,
                            node,
                            attempt,
                            legacy_result,
                            **options,
                        )
                if scenario == "historical":
                    # Seed the artifact-bearing format previously permitted for a
                    # custom adapter. The unmodified durable job commit still runs.
                    assert legacy_result is not None
                    return legacy_result
            return check_result(
                opened, previous, branch, node, attempt, result, **options
            )

        async def commit_and_retry(jobs, claim, result, *, configured):
            disposition = await commit(jobs, claim, result, configured=configured)
            if (
                isinstance(result.message, ToolMessage)
                and result.message.completion_artifact is not None
            ):
                assert disposition is CompletionDisposition.APPLIED
                assert (
                    await commit(jobs, claim, result, configured=configured)
                    is CompletionDisposition.DUPLICATE
                )
                retried_attempts.append(result.message.inputs.envelope.attempt_id)
            return disposition

        async def peer(reader, writer):
            incoming = await http_support.read_request(reader)
            body = json.loads(incoming.body)
            role = body["response_format"]["json_schema"]["schema"]["$id"].split(":")[
                -2
            ]
            calls.append(role)
            reply = {
                "candidate_prediction-result": {"categories": []},
                "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
                "object_grounding-result": {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
                "object_observation-result": {
                    "vehicle_type": "electric_bicycle",
                    **(
                        {"parking_state": "unparked"}
                        if scenario != "unknown_guard"
                        else {}
                    ),
                    "parking_position": "outside_parking_area",
                },
            }[role]
            writer.write(
                http_support.response(
                    http_support.chat_body(
                        model=body["model"], content=json.dumps(reply)
                    ),
                    headers=(
                        (b"X-Request-ID", b"no-invoked-tools"),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            with patch.object(
                perception,
                "load_source",
                return_value=_no_invocations_source(
                    empty=scenario in {"empty", "historical"}
                ),
            ):
                async with perception.perception_fixture(
                    tmp_path, url, with_media=True
                ) as fixture:
                    media = fixture.media_store
                    assert media is not None
                    accepted = fixture.accepted
                    scope = accepted.task.idempotency_scope
                    eid = accepted.execution.execution_id
                    ledger = StepAttemptStore(fixture.application, actor=ACTOR)
                    bridge = ToolExecutionAdapter(
                        ToolExecutor(ToolWorkerRegistry(())),
                        ledger,
                        fixture.journal,
                        fixture.host.capabilities,
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
                            execute_tools=GeometryNodeAdapter(media, bridge).execute,
                            model_resource_scope_id="model",
                        )

                    with (
                        patch.object(
                            ledger,
                            "begin",
                            side_effect=AssertionError("unexpected tool dispatch"),
                        ) as dispatch,
                        patch.object(
                            WorkflowWorker, "_commit", staticmethod(commit_and_retry)
                        ),
                        patch(
                            "jimu_runtime.workflow.worker.check_result",
                            side_effect=check_submission,
                        ),
                    ):
                        for _ in range(12):
                            worker = open_worker()
                            await worker.run_once(
                                scope, eid, owner="no-tools", lease_ms=10000
                            )
                            if await worker.read_finalization(scope, eid) is not None:
                                break
                            await asyncio.sleep(0.03)
                        else:
                            pytest.fail("expected terminal execution")
                        dispatch.assert_not_called()

                    async with fixture.application.connect() as connection:
                        locked = await repository.get_execution(connection, scope, eid)
                        assert locked is not None
                        state = await load_records(connection, scope, locked)
                        (obj,) = state.inputs(None).grounding.objects
                        node = state.runs[BranchKey(NodeId.N05, obj.object_id)]
                        tools = state.inputs(obj.object_id).tool
                        assert tools is not None
                        assert not (
                            tools.executions
                            or tools.facts
                            or tools.authorities
                            or tools.evidence
                        )
                        assert not state.tool_step_nodes
                        completion = tools.completion_artifact
                        if scenario == "unknown_guard":
                            assert node.status is NodeRunStatus.FAILED
                            assert locked.execution.status in {
                                ExecutionStatus.FAILED,
                                ExecutionStatus.PARTIAL_FAILED,
                            }
                            assert completion is None
                            assert not retried_attempts
                            assert not node.output_refs
                            assert {
                                step.disposition for step in tools.output.summary.steps
                            } == {"blocked"}
                            assert {
                                step.reason_code for step in tools.output.summary.steps
                            } == {"geometry.step_guard_unknown"}
                            return
                        assert node.status is NodeRunStatus.SUCCEEDED
                        assert (
                            state.runs[BranchKey(NodeId.N06, obj.object_id)].status
                            is NodeRunStatus.SUCCEEDED
                        )
                        assert calls.count("candidate_prediction-result") == 1
                        historical = await load_historical_source(
                            connection,
                            scope,
                            ReplayExecutionSource(
                                execution_ref=ExecutionRef(
                                    record_kind=K.EXECUTION, record_id=eid
                                ),
                                task_id=locked.execution.task_id,
                                pins=locked.execution.pins,
                            ),
                        )
                        if scenario == "historical":
                            assert completion is None
                            assert not retried_attempts
                            prior = artifact(K.OBJECT, obj.object_id, obj)
                            assert node.output_refs == (prior.ref,)
                            assert any(
                                pin.ref == prior.ref and pin.digest == prior.pin.digest
                                for pin in historical.artifacts
                            )
                            return
                        assert completion is not None
                        assert node.output_refs == (completion.ref,)
                        assert tools.output.envelope.output_artifacts == (
                            completion.pin,
                        )
                        aid = tools.inputs.envelope.attempt_id
                        assert retried_attempts == [aid]
                        stored = await repository.read_output(
                            connection,
                            scope,
                            eid,
                            node.node_run_id,
                            aid,
                            f"{K.TOOL_PLAN_RUN_SUMMARY.value}.{aid}",
                            ToolPlanCompletionRecord,
                        )
                        assert stored == completion.record
                        assert (
                            stored.summary.tool_plan_ref == tools.inputs.tool_plan_ref
                        )
                        assert stored.summary.object_ref.record_id == obj.object_id
                        assert stored.summary.steps == tools.output.summary.steps
                        manifest = tools.model_dump(mode="json")
                        manifest["output"]["envelope"]["output_artifacts"][0][
                            "digest"
                        ] = "0" * 64
                        with pytest.raises(
                            ValueError, match="exact summary as the sole output"
                        ):
                            ToolMessage.model_validate_json(json.dumps(manifest))
                        assert any(
                            pin.ref == completion.ref
                            and pin.digest == completion.pin.digest
                            for pin in historical.artifacts
                        )
                        if scenario == "empty":
                            assert not stored.summary.steps
                            assert not tools.observation_input_artifacts
                        else:
                            assert len(stored.summary.steps) == 2
                            assert {
                                step.disposition for step in stored.summary.steps
                            } == {"skipped"}
                            assert {
                                step.reason_code for step in stored.summary.steps
                            } == {"geometry.step_guard_false"}
                            assert tools.observation_input_artifacts

    asyncio.run(run())


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "observation_failure",
        "restart",
        "guard_true",
        "guard_false",
        "guard_unknown",
        "early_child_failure",
    ],
)
def test_mixed_n05_executes_early_and_settles_from_exact_n04(
    tmp_path, loopback_only, scenario
):
    async def run():
        guard_only = scenario.startswith("guard_")
        waiting_step = _GUARDED_STEP if guard_only else _DEPENDENT
        observation_started = asyncio.Event()
        release_observation = asyncio.Event()
        independent_selected = asyncio.Event()
        dispatches = []
        recovered_execution = None
        failures = []

        async def peer(reader, writer):
            incoming = await http_support.read_request(reader)
            body = json.loads(incoming.body)
            role = body["response_format"]["json_schema"]["schema"]["$id"].split(":")[
                -2
            ]
            reply = {
                "candidate_prediction-result": {"categories": []},
                "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
                "object_grounding-result": {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
                "object_observation-result": {
                    "vehicle_type": "electric_bicycle",
                    "parking_state": "parked",
                    "parking_position": "outside_parking_area",
                },
            }[role]
            if role == "object_observation-result":
                observation_started.set()
                await release_observation.wait()
                if scenario in {"observation_failure", "guard_unknown"}:
                    reply = {"invalid_field": True}
                elif scenario == "guard_false":
                    reply["parking_state"] = "unparked"
            writer.write(
                http_support.response(
                    http_support.chat_body(
                        model=body["model"], content=json.dumps(reply)
                    ),
                    headers=(
                        (b"X-Request-ID", b"mixed-n05"),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            with patch.object(
                perception,
                "load_source",
                return_value=_mixed_source(guard_only=guard_only),
            ):
                async with perception.perception_fixture(
                    tmp_path, url, with_media=True
                ) as fixture:
                    media = fixture.media_store
                    assert media is not None
                    accepted = fixture.accepted
                    scope = accepted.task.idempotency_scope
                    eid = accepted.execution.execution_id
                    catalogue = fixture.scene.execution.catalogue
                    definitions = tuple(
                        require_definition(catalogue, item.ref, ToolDefinition)
                        for item in catalogue.of_kind(ArtifactKind.TOOL)
                    )
                    ledger = StepAttemptStore(fixture.application, actor=ACTOR)
                    registry = ToolWorkerRegistry(
                        synthetic_cpu_implementations(
                            fixture.resources,
                            "cpu",
                            "mixed-n05",
                            definitions,
                            fixture.tool_proofs,
                            synthetic_image_digests=frozenset(
                                {accepted.task.media_ref.sha256}
                            ),
                        )
                    )
                    bridge = ToolExecutionAdapter(
                        ToolExecutor(registry),
                        ledger,
                        fixture.journal,
                        fixture.host.capabilities,
                    )
                    geometry = GeometryNodeAdapter(media, bridge)
                    original_step = GeometryNodeAdapter._step
                    original_begin = ledger.begin
                    materialize = GeometryInput._materialize

                    async def inject_early_failure(adapter, dispatch, payload):
                        if (
                            scenario == "early_child_failure"
                            and dispatch.origin.step_id == _INDEPENDENT
                        ):
                            raise ValueError("fixture rejects early Tool evidence")
                        return await materialize(adapter, dispatch, payload)

                    async def record_physical_child(
                        claim, inputs, step, digest, **options
                    ):
                        result = await original_begin(
                            claim, inputs, step, digest, **options
                        )
                        if isinstance(result, StepAttempt):
                            dispatches.append(step.step_id)
                        return result

                    async def step_checkpoint(
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
                        nonlocal recovered_execution
                        await original_step(
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
                        if step.step_id == _INDEPENDENT:
                            execution = state.completions[_INDEPENDENT].result.execution
                            assert (
                                state.completions[_INDEPENDENT].selection is not None
                            ) == (scenario != "early_child_failure")
                            if recovered_execution is None:
                                recovered_execution = execution
                                independent_selected.set()
                                if scenario == "restart":
                                    raise RetryableNodeFailure("transport")
                            else:
                                assert execution == recovered_execution

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

                    async def poll(owner):
                        return await open_worker().run_once(
                            scope, eid, owner=owner, lease_ms=10000
                        )

                    async def load():
                        async with fixture.application.connect() as connection:
                            locked = await repository.get_execution(
                                connection, scope, eid
                            )
                            assert locked is not None
                            return await load_records(connection, scope, locked)

                    def record_failure(node, attempt, error):
                        if node.node_id is NodeId.N05 and not isinstance(
                            error, RetryableNodeFailure
                        ):
                            failures.append(error)
                        return _failed_node(node, attempt, error)

                    with (
                        patch.object(GeometryNodeAdapter, "_step", step_checkpoint),
                        patch.object(ledger, "begin", record_physical_child),
                        patch.object(
                            GeometryInput, "_materialize", inject_early_failure
                        ),
                        patch(
                            "jimu_runtime.workflow.worker._failed_node",
                            side_effect=record_failure,
                        ),
                    ):
                        for _ in range(3):
                            assert await poll("setup") is WorkerStep.APPLIED
                        n04 = asyncio.create_task(poll("observation"))
                        n05 = None
                        try:
                            await asyncio.wait_for(
                                observation_started.wait(), timeout=4
                            )
                            n05 = asyncio.create_task(poll("tools"))
                            await asyncio.wait_for(
                                independent_selected.wait(), timeout=4
                            )
                            assert dispatches[0] == _INDEPENDENT
                            assert waiting_step not in dispatches
                            assert not n04.done()
                            if scenario == "restart":
                                assert await n05 is WorkerStep.RETRY_SCHEDULED
                            else:
                                assert await n05 is WorkerStep.WAITING_FOR_OBSERVATION
                        finally:
                            release_observation.set()
                            await n04
                            if n05 is not None:
                                await n05
                        for _ in range(12):
                            state = await load()
                            (obj,) = state.inputs(None).grounding.objects
                            branch = state.inputs(obj.object_id)
                            if branch.tool is not None:
                                break
                            await poll("replacement")
                            await asyncio.sleep(0.03)
                    tools = branch.tool
                    assert tools is not None, failures
                    assert tools.inputs.object_record == obj
                    steps = {item.step_id: item for item in tools.output.summary.steps}
                    assert isinstance(steps[_INDEPENDENT], InvokedToolStep)
                    assert recovered_execution in tools.executions
                    assert all(
                        pin.ref.record_kind is not K.FACT
                        for pin in tools.inputs.envelope.upstream_artifacts
                    )
                    if scenario in {"guard_false", "guard_unknown"}:
                        assert isinstance(steps[waiting_step], UninvokedToolStep)
                        outcome = "skipped" if scenario == "guard_false" else "blocked"
                        assert steps[waiting_step].disposition == outcome
                        assert (
                            steps[waiting_step].reason_code
                            == f"geometry.step_guard_{scenario.removeprefix('guard_')}"
                        )
                        assert waiting_step not in dispatches
                        assert all(
                            isinstance(steps[step_id], InvokedToolStep)
                            for step_id in (_INDEPENDENT, _DEPENDENT)
                        )
                        assert bool(tools.observation_input_artifacts) == (
                            scenario == "guard_false"
                        )
                        return
                    if scenario == "observation_failure":
                        assert isinstance(steps[_DEPENDENT], UninvokedToolStep)
                        assert (
                            steps[_DEPENDENT].reason_code
                            == "geometry.step_input_unavailable"
                        )
                        assert not tools.observation_input_artifacts
                        assert dispatches == [_INDEPENDENT]
                        assert (
                            state.runs[BranchKey(NodeId.N04, obj.object_id)].status
                            is NodeRunStatus.FAILED
                        )
                        return
                    assert isinstance(steps[waiting_step], InvokedToolStep)
                    expected = [_INDEPENDENT, _DEPENDENT, "wheel_boundary_geometry"]
                    if scenario == "early_child_failure":
                        expected.pop()
                        assert recovered_execution.status == "failed"
                        assert isinstance(steps[_GUARDED_STEP], UninvokedToolStep)
                        assert (
                            steps[_GUARDED_STEP].reason_code
                            == "geometry.dependency_unavailable"
                        )
                        assert all(
                            selection.step_id != _INDEPENDENT
                            for selection in tools.authorities
                        )
                    assert sorted(dispatches) == sorted(expected)
                    assert (
                        branch.observation is not None
                        and branch.observation.authority is not None
                    )
                    assert not tools.inputs.validated_observation_fact_refs
                    assert tools.inputs.observation_authority_ref is None
                    (fact,) = [
                        item
                        for item in branch.observation.facts
                        if item.fact_key == "parking_state"
                    ]
                    assert {
                        pin.ref.record_id for pin in tools.observation_input_artifacts
                    } == {fact.fact_id, branch.observation.authority.selection_id}
                    async with fixture.application.connect() as connection:
                        history = await load_parent_step_history(
                            connection, scope, tools.inputs
                        )
                    assert history is not None
                    assert history.parent_attempt.attempt_number == 2
                    children = {
                        entry.execution.invocation_origin.step_id: entry
                        for entry in history.entries
                    }
                    if scenario == "early_child_failure":
                        assert children[_INDEPENDENT].selection is None
                        assert children[
                            _INDEPENDENT
                        ].original_parent_attempt.observation_dependency_deferred
                    assert children[waiting_step].selection is not None, children[
                        waiting_step
                    ].execution.attempt_result
                    assert (
                        children[_INDEPENDENT].child_node.upstream_inputs
                        == history.parent_node.upstream_inputs
                    )
                    assert [
                        pin.ref.record_id
                        for pin in children[waiting_step].child_node.upstream_inputs
                        if pin.ref.record_kind
                        in {K.FACT, K.AUTHORITATIVE_RESULT_SELECTION}
                    ] == [fact.fact_id, branch.observation.authority.selection_id]
                    child = children[waiting_step]
                    bad_upstream = tuple(
                        pin.model_copy(update={"digest": "0" * 64})
                        if pin.ref.record_kind is K.FACT
                        else pin
                        for pin in child.child_node.upstream_inputs
                    )
                    forged_child = replace(
                        child,
                        child_node=child.child_node.model_copy(
                            update={"upstream_inputs": bad_upstream}
                        ),
                    )
                    forged_history = replace(
                        history,
                        entries=tuple(
                            forged_child if entry is child else entry
                            for entry in history.entries
                        ),
                    )
                    with pytest.raises(
                        ValueError, match="complete original child result"
                    ):
                        forged_history.resolved_executions(tools.inputs, tools.facts)

    asyncio.run(run())
