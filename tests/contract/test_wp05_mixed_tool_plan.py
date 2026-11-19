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
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus
from jimu_runtime.domain.enums import RuntimeRecordKind as K
from jimu_runtime.domain.nodes.tool_execution import InvokedToolStep, UninvokedToolStep
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import (
    GeometryInput,
    synthetic_cpu_implementations,
)
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import (
    StepAttempt,
    StepAttemptStore,
    load_parent_step_history,
)
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.worker import RetryableNodeFailure, WorkerStep, _failed_node
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_state import load_records
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
