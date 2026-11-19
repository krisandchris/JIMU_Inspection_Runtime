"""Exercise real loopback HTTP and supervised CPU tools through WorkflowWorker."""

import asyncio
import json
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ResolvedTaskScope
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind, AttemptStatus, ExecutionStatus
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
)
from jimu_runtime.domain.policies import ResourceRequirementsArtifact
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    ModelInvocationRef,
    ObjectRef,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.invocations import FallbackProviderRoute
from jimu_runtime.gateway.bridge import (
    CapabilityGateway,
    ModelCall,
    ModelCallLimits,
    require_model_output,
)
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.gateway.http import (
    ChatContract,
    ChatEndpoint,
    HttpLimits,
    HttpModelTransport,
)
from jimu_runtime.gateway.journal import DispatchJournal
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentOwnership,
    DeploymentOwnershipRegistration,
)
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import (
    IsolatedCPUWorker,
    ToolImplementation,
    ToolWorkerRegistry,
)
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.intake import IntakeService
from jimu_runtime.workflow.mock_context import snapshot_ref
from jimu_runtime.workflow.mock_support import VERSION, K, artifact
from jimu_runtime.workflow.scheduling import JobBudget, NodeBudgetOverride
from jimu_runtime.workflow.worker import (
    AdapterRejected,
    ClaimedNode,
    WorkerStep,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_capabilities import (
    ToolOwnershipPin,
    read_historical_capabilities,
)
from jimu_runtime.workflow.worker_nodes import (
    NodeResult,
    authority_artifact,
    discovery_inputs,
    invocation_envelope,
    invoke,
)
from jimu_runtime.workflow.worker_records import DiscoveryMessage, ToolMessage
from jimu_runtime.workflow.worker_state import load_attempts, load_records
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    TOOL,
    approve_source_bytes,
    capability_snapshot,
)
from tests.fixtures.wp02.releases.support import FixtureHost
from tests.fixtures.wp03.intake.support import migrated_application
from tests.fixtures.wp03.nodes.scenario import REGION, parking_source
from tests.fixtures.wp03.pins.support import activate_scenario
from tests.fixtures.wp03.worker.support import (
    SCHEDULING,
    FixtureWorkerHost,
    open_worker_fixture,
    worker_submission,
)
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp04.bridge_fallback import FALLBACK, PROOF, fallback_admission

loopback_only = http_support.loopback_only

_CPU_SCRIPT = r"""
import json
import pathlib
import sys

import pytest

lane = json.load(sys.stdin)["image"]
counter = pathlib.Path(sys.argv[1])
previous = counter.read_text().splitlines() if counter.exists() else []
with counter.open("a") as out:
    out.write(lane + "\n")
base = {"evidence": [], "evidence_refs": [], "diagnostics": [], "output_states": []}
if lane == "diagnostic" and lane not in previous:
    result = {"status": "failed", "error": {
        "error_class": "transport", "code": "fixture_tool_transient", "diagnostic_refs": []
    }}
else:
    result = {"status": "succeeded", "payload": {"kind": "bool", "value": True}}
    base["output_states"] = [{"output_name": "outside", "fact_status": "known", "evidence_refs": []}]
print(json.dumps({**base, **result}))
"""


def _fixture_source():
    source = parking_source()
    artifacts = []
    for entry in source.artifacts:
        body = json.loads(entry.content)
        slots = entry.reference_slots
        if entry.kind is ArtifactKind.TOOL_PLAN:
            other = deepcopy(body["steps"][0])
            other.update(step_id="diagnostic", fact_output_mappings=[])
            body["steps"].append(other)
            slots = (
                *slots,
                *(
                    replace(slot, location=("steps", 1, *slot.location[2:]))
                    for slot in slots
                    if slot.location[:2] == ("steps", 0)
                ),
            )
        elif entry.kind is ArtifactKind.POLICY:
            if entry.artifact_id == "evidence":
                body["policy"]["original_media_required"] = False
            if entry.artifact_id == "execution":
                body["policy"].update(
                    timeout_ms=4000,
                    max_attempts=2,
                    retry_backoff_ms=[25],
                    retryable_error_classes=["transport"],
                )
            if entry.artifact_id in ("resources", "remote_resources"):
                body["policy"].update(
                    input_byte_limit=4096, output_byte_limit=4096, timeout_ms=4000
                )
        artifacts.append(
            replace(entry, content=json.dumps(body).encode(), reference_slots=slots)
        )
    return approve_source_bytes(replace(source, artifacts=tuple(artifacts)))


class _DiscoveryReply(ContractModel):
    profile_hits: list[str]


async def _run_synthetic(claimed: ClaimedNode) -> NodeResult:
    return await asyncio.to_thread(
        invoke,
        claimed.opened,
        claimed.previous,
        claimed.branch,
        claimed.node,
        claimed.attempt,
    )


async def _discovery_with_invocation(claimed, provider_output, invocation):
    candidate = await _run_synthetic(claimed)
    message = candidate.message
    assert isinstance(message, DiscoveryMessage) and invocation is not None
    assert provider_output.profile_hits == [
        hit.profile_ref.artifact_id for hit in message.output.profile_hits
    ]
    ref = ModelInvocationRef(
        record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
    )
    selections = tuple(
        row.model_copy(update={"discovery_model_invocation_ref": ref})
        for row in message.selections
    )
    hits = tuple(
        row.model_copy(update={"model_invocation_ref": ref})
        for row in message.output.profile_hits
    )
    records = (
        artifact(K.MODEL_INVOCATION, invocation.model_invocation_id, invocation),
        *(
            artifact(K.QUESTION_SELECTION_RECORD, row.selection_id, row)
            for row in selections
        ),
    )
    outcome = message.output.model_copy(
        update={
            "profile_hits": hits,
            "model_invocation_refs": (ref,),
            "envelope": message.output.envelope.model_copy(
                update={"output_artifacts": tuple(row.pin for row in records)}
            ),
        }
    )
    return NodeResult(
        DiscoveryMessage(
            schema_version=VERSION,
            inputs=message.inputs,
            output=outcome,
            selections=selections,
            invocations=(invocation,),
        ),
        records,
        (ref,),
    )


async def _tool_node(claimed, tool_adapter, step_ids, *, expect_rejection=False):
    opened, prior = claimed.opened, claimed.previous
    envelope = invocation_envelope(
        opened, claimed.branch, claimed.node, claimed.attempt
    )
    obj = prior.object_record(claimed.branch)
    plan = next(
        item.definition.tool_plan_ref
        for item in opened.profiles
        if item.rules.profile_ref == obj.profile_ref
    )
    inputs = ToolNodeInput(
        schema_version=VERSION,
        envelope=envelope,
        object_record=obj,
        image_view=ObjectImageView(roi=obj.bbox, transforms=()),
        context_snapshot_ref=snapshot_ref(prior.snapshot()),
        tool_plan_ref=plan,
        validated_observation_fact_refs=(),
    )
    bound = opened.scope.resolve(plan, ToolPlanDefinition)
    completed = []
    for step in bound.steps:
        proof = await tool_adapter.execute_step(
            claimed, inputs, step.step_id, {"image": step.step_id}
        )
        completed.append(proof)
        step_ids.setdefault(step.step_id, []).append(
            proof.result.execution.tool_execution_id
        )
        if expect_rejection:
            assert proof.selection is None
            assert proof.result.execution.status == "not_dispatched"
            assert (
                proof.result.execution.attempt_result.error.code
                == "tool_live_registration_revoked"
            )
            raise AdapterRejected("fixture_tool_live_registration_revoked")
        assert proof.selection is not None

    executions = tuple(item.result.execution for item in completed)
    facts = tuple(fact for item in completed for fact in item.result.facts)
    records = (
        *(artifact(K.TOOL_EXECUTION, row.tool_execution_id, row) for row in executions),
        *(artifact(K.FACT, row.fact_id, row) for row in facts),
    )
    at = claimed.attempt.started_at
    assert at is not None
    call = opened.scope.check_call(envelope, NodeId.N05, at)
    refs = tuple(
        ToolExecutionRef(record_kind=K.TOOL_EXECUTION, record_id=row.tool_execution_id)
        for row in executions
    )
    output = ToolNodeOutput(
        schema_version=VERSION,
        envelope=call.output(records),
        summary=ToolPlanRunSummary(
            tool_plan_ref=plan,
            object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
            status="succeeded",
            steps=tuple(
                InvokedToolStep(
                    disposition="invoked", step_id=step.step_id, tool_execution_ref=ref
                )
                for step, ref in zip(bound.steps, refs, strict=True)
            ),
            validation_issues=(),
        ),
        tool_fact_refs=tuple(
            ref for item in executions for ref in item.generated_fact_refs
        ),
        evidence_refs=(),
    )
    selections = tuple(
        item.selection for item in completed if item.selection is not None
    )
    return NodeResult(
        ToolMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=output,
            executions=executions,
            facts=facts,
            authorities=selections,
        ),
        (*records, *(authority_artifact(item) for item in selections)),
    )


async def repository_state(app, scope, execution_id):
    async with app.connect() as connection:
        locked = await repository.get_execution(connection, scope, execution_id)
        assert locked is not None
        return locked.execution


@pytest.mark.parametrize(
    "case",
    (
        "normal",
        "fallback",
        "revoked",
        "cancelled",
        "pre_send_restart",
        "stale_history",
        "cancel_before_wire",
        "cancel_after_admission",
        "revoke_before_wire",
        "tool_revoked_after_admission",
        "tool_cancel_after_admission",
        "hard_budget",
    ),
)
def test_worker_real_model_and_step_authority_survive_retry(
    tmp_path: Path, loopback_only: set[int], case: str
) -> None:
    fallback = case in {"fallback", "stale_history"}
    expect_no_wire = case in {
        "cancel_before_wire",
        "cancel_after_admission",
        "revoke_before_wire",
    }
    cancellation_at = asyncio.Event()
    wait = asyncio.Event()
    revoke = asyncio.Event()

    class WorkerShutdown(BaseException):
        pass

    class RestartTransport(HttpModelTransport):
        async def dispatch(self, request):
            stopped_round = {"pre_send_restart": 1, "stale_history": 2}.get(case)
            if request.route.attempt.attempt_number == stopped_round:
                raise WorkerShutdown
            if case in {"cancel_before_wire", "revoke_before_wire"}:
                original = request.pre_send_allowed
                assert original is not None

                async def intercept():
                    if case == "cancel_before_wire":
                        cancellation_at.set()
                        await wait.wait()
                    else:
                        revoke.set()
                    return await original()

                request = replace(request, pre_send_allowed=intercept)
            return await super().dispatch(request)

    async def run() -> None:
        source, admission = _fixture_source(), capability_snapshot()
        if fallback:
            source, admission = fallback_admission(source)

        class LiveFixtureHost(FixtureWorkerHost):
            async def capabilities(self, scope):
                current = await super().capabilities(scope)
                if not fallback:
                    return current
                original = current.model_deployments[0]
                return current.model_copy(
                    update={
                        "model_deployments": tuple(
                            row.model_copy(
                                update={
                                    "valid_from": original.valid_from,
                                    "valid_until": original.valid_until,
                                }
                            )
                            for row in admission.model_deployments
                        ),
                        "model_reviews": admission.model_reviews,
                    }
                )

        async with migrated_application() as app:
            store, release, host = open_worker_fixture(
                tmp_path, LiveFixtureHost(FixtureHost())
            )
            activate_scenario(
                tmp_path / "admission",
                store,
                release,
                host.release_host,
                source,
                admission,
                number=1,
            )
            host.release_host.trusted_scope = ResolvedTaskScope(
                device_id="rover-01",
                camera_id="front-camera",
                region_id=REGION,
                scene_type=host.release_host.trusted_scope.scene_type,
            )
            requests = []
            endpoints = ()
            state = None
            script = tmp_path / "cpu_tool.py"
            script.write_text(_CPU_SCRIPT)
            proof = TrustPin(
                identity="cpu-fixture",
                version=VERSION,
                sha256=sha256_digest(script.read_bytes()),
            )
            counter = tmp_path / "cpu-dispatches"
            expected = next(
                item
                for item in _fixture_source().artifacts
                if item.kind is ArtifactKind.PROFILE
            ).artifact_id

            async def answer(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                nonlocal state
                try:
                    headers = (
                        (await reader.readuntil(b"\r\n\r\n"))
                        .decode("ascii")
                        .split("\r\n")
                    )
                    length = int(
                        next(
                            line.split(": ")[1]
                            for line in headers
                            if line.lower().startswith("content-length: ")
                        )
                    )
                    request = json.loads(await reader.readexactly(length))
                    requests.append(request)
                    assert state is not None
                    if fallback and len(requests) == 1:
                        writer.write(http_support.response(b"{}", status=503))
                        await writer.drain()
                        return
                    if case == "revoked":
                        state = state.model_copy(
                            update={
                                "ownership": tuple(
                                    row.model_copy(
                                        update={"revoked_at": datetime.now(UTC)}
                                    )
                                    for row in state.ownership
                                ),
                            }
                        )
                    elif case == "cancelled":
                        assert await worker().request_cancel(scope, eid)
                    deployed = next(
                        row.ownership.deployment
                        for row in state.ownership
                        if row.ownership.model_id == request["model"]
                    )
                    response = json.dumps(
                        {
                            "model": request["model"],
                            "choices": [
                                {
                                    "index": 0,
                                    "finish_reason": "stop",
                                    "message": {
                                        "role": "assistant",
                                        "content": json.dumps(
                                            {"profile_hits": [expected]}
                                        ),
                                    },
                                }
                            ],
                        }
                    ).encode()
                    writer.write(
                        (
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                            f"Content-Length: {len(response)}\r\nX-Request-ID: fixture-observed-1\r\n"
                            f"X-Jimu-Deployment-Version: {deployed.deployment_version}\r\n"
                            f"X-Jimu-Model-Version: {deployed.model_version}\r\n\r\n"
                        ).encode()
                        + response
                    )
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            async with http_support.http_peer(loopback_only, answer) as url:

                async def freeze_model(_scope, caps):
                    nonlocal endpoints, state
                    endpoints = tuple(
                        ChatEndpoint(
                            endpoint_id=f"loopback-model-{i}",
                            endpoint_version=VERSION,
                            provider_id=row.identity.provider_id,
                            deployment_id=row.identity.deployment_id,
                            provider_kind=row.identity.provider_kind,
                            model_id=f"wp04-vision-{i}",
                            url=url,
                            protocol="openai_chat_completions",
                            allow_plaintext_loopback=True,
                            limits=HttpLimits(
                                timeout_ms=3500,
                                io_timeout_ms=3000,
                                request_bytes=4096,
                                response_bytes=2048,
                                image_bytes=1024,
                                max_retry_after_seconds=1,
                            ),
                        )
                        for i, row in enumerate(caps.model_deployments)
                    )
                    ownership = tuple(
                        DeploymentOwnership(
                            deployment=row.identity,
                            owner_ref=proof,
                            endpoint_descriptor_ref=endpoint.descriptor_pin,
                            model_id=endpoint.model_id,
                            version_evidence=PROOF if fallback else None,
                        )
                        for row, endpoint in zip(
                            caps.model_deployments, endpoints, strict=True
                        )
                    )
                    at = datetime.now(UTC)
                    state = CurrentRoutingState(
                        capabilities=caps,
                        ownership=tuple(
                            DeploymentOwnershipRegistration(
                                ownership=item,
                                valid_from=at - timedelta(hours=1),
                                valid_until=at + timedelta(hours=1),
                                revoked_at=None,
                            )
                            for item in ownership
                        ),
                    )
                    return ownership

                async def freeze_tools(_scope, resolved):
                    return tuple(
                        ToolOwnershipPin(
                            tool_ref=pin.tool_ref,
                            implementation_version=pin.implementation_version,
                            implementation_proof_ref=proof,
                        )
                        for pin in resolved.pins.tool_pins
                    )

                intake = IntakeService(
                    app,
                    release,
                    store,
                    host=host,
                    bundle_limits=LIMITS,
                    scheduling_limits=(
                        SCHEDULING.model_copy(
                            update={
                                "overrides": (
                                    NodeBudgetOverride(
                                        node_id=NodeId.N02,
                                        budget=JobBudget(
                                            max_attempts=3,
                                            timeout_ms=10000,
                                            retry_backoff_ms=(25, 25),
                                            retryable_error_classes=(
                                                "transport",
                                                "resource",
                                            ),
                                            retry_on_lease_loss=True,
                                        ),
                                    ),
                                )
                            }
                        )
                        if case in {"stale_history", "hard_budget"}
                        else SCHEDULING
                    ),
                    max_submission_bytes=64 * 1024,
                    historical_ownership=freeze_model,
                    historical_tool_ownership=freeze_tools,
                )
                given = worker_submission("wp04-bridge")
                accepted = await intake.submit(
                    given, principal=host.release_host.task_client
                )
                assert (
                    await intake.submit(given, principal=host.release_host.task_client)
                    == accepted
                )
                scope, eid = (
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                )
                assert endpoints and state is not None
                resources = ResourceAdmission.provision(
                    tmp_path / "resources.db",
                    HostInventory(
                        "bridge-fixture",
                        6000,
                        2,
                        (
                            ResourceScope("cpu", "cpu", 2),
                            ResourceScope(
                                "model", "remote", 2, pool_or_quota_id="fixture.quota"
                            ),
                        ),
                        (
                            RemoteQuota(
                                "fixture.quota",
                                1 if expect_no_wire else 2,
                                RemoteCapacity.PROVIDER_INFLIGHT
                                if expect_no_wire
                                else RemoteCapacity.LOCAL_TRANSPORT,
                            ),
                        ),
                    ),
                )
                journal = DispatchJournal(app)
                step_store = StepAttemptStore(
                    app,
                    actor=ActorRef(
                        actor_kind="service",
                        actor_id="fixture",
                        identity_registry_id="fixture",
                    ),
                )
                step_ids = {}
                model_receipt = None
                model_requirements = None

                registry_reads = 0

                async def current_model(_scope):
                    nonlocal registry_reads
                    registry_reads += 1
                    if case == "cancel_after_admission" and registry_reads == 2:
                        cancellation_at.set()
                        await wait.wait()
                    if revoke.is_set():
                        return state.model_copy(
                            update={
                                "ownership": tuple(
                                    row.model_copy(
                                        update={"revoked_at": datetime.now(UTC)}
                                    )
                                    for row in state.ownership
                                ),
                            }
                        )
                    return state

                tool_registry_reads = 0

                async def current_tools(_scope) -> CapabilitySnapshot:
                    nonlocal tool_registry_reads
                    assert state is not None
                    tool_registry_reads += 1
                    if (
                        case == "tool_cancel_after_admission"
                        and tool_registry_reads == 2
                    ):
                        cancellation_at.set()
                        await wait.wait()
                    if (
                        case == "tool_revoked_after_admission"
                        and tool_registry_reads == 2
                    ):
                        caps = state.capabilities
                        return caps.model_copy(
                            update={
                                "capabilities": tuple(
                                    row.model_copy(
                                        update={"revoked_at": datetime.now(UTC)}
                                    )
                                    for row in caps.capabilities
                                ),
                            }
                        )
                    return state.capabilities

                async def run_node(claimed: ClaimedNode) -> NodeResult:
                    nonlocal model_receipt, model_requirements
                    if claimed.node.node_id is NodeId.N02:
                        source = discovery_inputs(
                            claimed.opened,
                            claimed.previous,
                            invocation_envelope(
                                claimed.opened,
                                claimed.branch,
                                claimed.node,
                                claimed.attempt,
                            ),
                        )
                        (entry,) = source.bindings
                        model = claimed.opened.scope.resolve(
                            entry.discovery_binding_ref, ModelBinding
                        )
                        assert endpoints
                        model_requirements = claimed.opened.scope.resolve(
                            model.resource_requirements_ref,
                            ResourceRequirementsArtifact,
                        ).policy
                        gateway = CapabilityGateway(
                            app,
                            journal,
                            resources,
                            RestartTransport(
                                endpoints,
                                (
                                    ChatContract(
                                        model.input_schema_ref,
                                        model.output_schema_ref,
                                        _DiscoveryReply,
                                        response_schema=claimed.opened.resolved.catalogue.resolve(
                                            model.output_schema_ref
                                        ).canonical_bytes,
                                    ),
                                ),
                                max_concurrency=2,
                            ),
                            current_model,
                            ModelCallLimits(
                                node=3
                                if case == "stale_history"
                                else 2
                                if fallback or case == "pre_send_restart"
                                else 1,
                                operation=3
                                if case == "stale_history"
                                else 2
                                if fallback or case == "pre_send_restart"
                                else 1,
                                deployment=2 if case == "pre_send_restart" else 1,
                                retryable=("transport", "unavailable"),
                            ),
                        )
                        outcome = await gateway.invoke_model(
                            claimed,
                            ModelCall(
                                binding_ref=entry.discovery_binding_ref,
                                purposes=(ModelCapability.PROFILE_DISCOVERY,),
                                profile_refs=(entry.profile_ref,),
                                prompt="wp04 fixture discovery",
                                resource_scope_id="model",
                            ),
                        )
                        if case == "hard_budget":
                            assert require_model_output(outcome)
                            outcome = await gateway.invoke_model(
                                claimed,
                                ModelCall(
                                    binding_ref=entry.discovery_binding_ref,
                                    purposes=(ModelCapability.PROFILE_DISCOVERY,),
                                    profile_refs=(entry.profile_ref,),
                                    prompt="wp04 second logical discovery operation",
                                    resource_scope_id="model",
                                ),
                            )
                        if outcome.invocation is not None:
                            model_receipt = outcome.invocation.model_invocation_id
                        payload = require_model_output(outcome)
                        assert outcome.invocation is not None
                        return await _discovery_with_invocation(
                            claimed,
                            _DiscoveryReply.model_validate_json(payload),
                            outcome.invocation,
                        )
                    if claimed.node.node_id is NodeId.N05:
                        (pin,) = claimed.opened.resolved.pins.tool_pins
                        definition = claimed.opened.scope.resolve(
                            pin.tool_ref, ToolDefinition
                        )
                        requirement = definition.implementation_requirement
                        capability = ToolImplementation(
                            capability_id=requirement.capability_id,
                            contract_version=requirement.contract_version,
                            version=pin.implementation_version,
                            proof=proof,
                            provider_kind=definition.provider_kind,
                            input_schema_ref=definition.input_schema_ref,
                            output_schema_ref=definition.output_schema_ref,
                            resource_class="cpu",
                            worker=IsolatedCPUWorker(
                                resources,
                                (sys.executable, str(script), str(counter)),
                                "cpu",
                                "fixture-step",
                                pin.implementation_version,
                                proof,
                                script,
                            ),
                        )
                        return await _tool_node(
                            claimed,
                            ToolExecutionAdapter(
                                ToolExecutor(ToolWorkerRegistry((capability,))),
                                step_store,
                                journal,
                                current_tools,
                            ),
                            step_ids,
                            expect_rejection=case == "tool_revoked_after_admission",
                        )
                    return await _run_synthetic(claimed)

                async def no_swallow(claimed):
                    try:
                        return await run_node(claimed)
                    except AssertionError as error:
                        raise BaseException("fixture assertion failed") from error
                    except (AttributeError, TypeError, ValueError) as error:
                        if case in {
                            "revoked",
                            "cancelled",
                            "stale_history",
                            "revoke_before_wire",
                            "tool_revoked_after_admission",
                            "hard_budget",
                        } and isinstance(error, AdapterRejected):
                            if (
                                case != "tool_revoked_after_admission"
                                or error.code
                                == "fixture_tool_live_registration_revoked"
                            ):
                                raise
                        raise BaseException("fixture adapter failed") from error

                def worker():
                    return WorkflowWorker(
                        app,
                        release,
                        store,
                        host=host,
                        bundle_limits=LIMITS,
                        invoke_claimed_node=no_swallow,
                    )

                if case in {
                    "cancel_before_wire",
                    "cancel_after_admission",
                    "tool_cancel_after_admission",
                }:
                    tool_cancelled = case == "tool_cancel_after_admission"
                    for index in range(4 if tool_cancelled else 1):
                        assert (
                            await worker().run_once(
                                scope,
                                eid,
                                owner=f"before-cancellation-{index}",
                                lease_ms=8000,
                            )
                            is WorkerStep.APPLIED
                        )
                    polling = asyncio.create_task(
                        worker().run_once(
                            scope, eid, owner="cancelled-worker", lease_ms=8000
                        )
                    )
                    async with asyncio.timeout(5):
                        await cancellation_at.wait()
                        polling.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await polling
                    async with app.connect() as connection:
                        dispatch_id = (
                            await connection.execute(
                                text("""
                                SELECT dispatch_id FROM jimu_workflow.dispatch_intents
                                WHERE execution_id = :execution_id
                                AND record->'target'->>'kind' = :kind
                            """),
                                {
                                    "execution_id": eid,
                                    "kind": "tool" if tool_cancelled else "model",
                                },
                            )
                        ).scalar_one()
                    receipt = await journal.get_dispatch(scope, eid, dispatch_id)
                    assert receipt is not None
                    (seen,) = receipt.results
                    assert seen.result.boundary == "not_dispatched"
                    assert seen.result.error.error_class == "cancelled"
                    assert (
                        await journal.usage(scope, eid, receipt.node_run_id)
                    ).node_sends == 0
                    assert len(requests) == int(tool_cancelled) and not counter.exists()
                    if not tool_cancelled:
                        assert model_requirements is not None
                        spare = resources.reserve(
                            model_requirements, "model", owner_id="next-owner"
                        )
                        assert resources.release_reserved(spare)
                    return

                steps = []
                for sequence in range(12):
                    # Construct a fresh Worker each time, including after the first N05 failure.
                    try:
                        step = await worker().run_once(
                            scope,
                            eid,
                            owner=f"worker-{sequence}",
                            lease_ms=600
                            if case in {"pre_send_restart", "stale_history"}
                            else 8000,
                        )
                    except BaseExceptionGroup as failure:
                        if failure.subgroup(WorkerShutdown) is None or case not in {
                            "pre_send_restart",
                            "stale_history",
                        }:
                            raise
                        await asyncio.sleep(0.7)
                        continue
                    steps.append(step)
                    if case in {"revoked", "cancelled"} and model_receipt is not None:
                        end = await repository_state(app, scope, eid)
                        if end.status is ExecutionStatus.RUNNING:
                            await worker().run_once(
                                scope, eid, owner="reconcile", lease_ms=8000
                            )
                            end = await repository_state(app, scope, eid)
                        assert model_receipt not in {
                            ref.record_id for ref in end.model_invocation_refs
                        }
                        assert end.status is (
                            ExecutionStatus.CANCELLED
                            if case == "cancelled"
                            else ExecutionStatus.FAILED
                        )
                        actual = await journal.get_dispatch(scope, eid, model_receipt)
                        assert actual is not None and len(requests) == 1
                        assert actual.results[0].result.boundary == "acknowledged"
                        assert actual.results[0].late is (case == "cancelled")
                        assert not counter.exists()
                        return
                    if step is WorkerStep.FINALIZED:
                        break
                    if step is WorkerStep.RETRY_SCHEDULED:
                        await asyncio.sleep(0.05)
                assert steps[-1] is WorkerStep.FINALIZED, steps
                if case == "hard_budget":
                    assert model_receipt is not None and len(requests) == 1
                    assert WorkerStep.RETRY_SCHEDULED not in steps
                    denied = await journal.get_dispatch(scope, eid, model_receipt)
                    assert denied is not None
                    (receipt,) = denied.results
                    assert receipt.result.error.code == "model.node_budget_exhausted"
                    assert receipt.result.boundary == "not_dispatched"
                    assert (
                        await journal.usage(scope, eid, denied.node_run_id)
                    ).node_sends == 1
                    assert not counter.exists()
                    return
                if case == "tool_revoked_after_admission":
                    assert len(requests) == 1
                    (tool_dispatch,) = step_ids[TOOL]
                    denied = await journal.get_dispatch(scope, eid, tool_dispatch)
                    assert denied is not None and denied.send_admitted
                    (receipt,) = denied.results
                    assert receipt.result.error.code == "tool_live_registration_revoked"
                    assert receipt.result.boundary == "not_dispatched"
                    assert not counter.exists()
                    assert WorkerStep.RETRY_SCHEDULED not in steps
                    assert await worker().read_finalization(scope, eid) is not None
                    return
                if case == "revoke_before_wire":
                    assert model_receipt is not None
                    record = await journal.get_dispatch(scope, eid, model_receipt)
                    assert record is not None and len(record.results) == 1
                    (seen,) = record.results
                    assert seen.result.boundary == "not_dispatched"
                    assert seen.result.error.code == "http_presend_authorization_lost"
                    assert not requests and not counter.exists()
                    assert model_requirements is not None
                    spare = resources.reserve(
                        model_requirements, "model", owner_id="next-owner"
                    )
                    assert resources.release_reserved(spare)
                    assert (
                        await repository_state(app, scope, eid)
                    ).status is ExecutionStatus.FAILED
                    return
                assert WorkerStep.RETRY_SCHEDULED in steps, steps
                if case == "stale_history":
                    assert model_receipt is not None
                    latest = await repository_state(app, scope, eid)
                    assert latest.status is ExecutionStatus.FAILED
                    first = await journal.get_dispatch(scope, eid, model_receipt)
                    assert first is not None
                    history = await journal.get_operation(
                        scope,
                        eid,
                        first.node_run_id,
                        first.intent.operation_id,
                    )
                    assert len(history) == 3 and len(requests) == 1
                    assert any(row.send_admitted and not row.results for row in history)
                    assert any(
                        result.result.boundary == "not_dispatched"
                        and result.result.error.code
                        == "model.newer_dispatch_unresolved"
                        for row in history
                        for result in row.results
                    )
                    assert not counter.exists()
                    return
                assert counter.read_text().splitlines() == [
                    TOOL,
                    "diagnostic",
                    "diagnostic",
                ]
                assert len(requests) == (2 if fallback else 1)
                if fallback:
                    assert [item["model"] for item in requests] == [
                        "wp04-vision-0",
                        "wp04-vision-1",
                    ]
                assert step_ids[TOOL][0] == step_ids[TOOL][1]
                assert model_receipt is not None
                receipt = await journal.get_dispatch(scope, eid, model_receipt)
                assert (
                    receipt is not None
                    and receipt.results[0].result.boundary == "acknowledged"
                )
                assert receipt.results[0].result.response_digest is not None
                if case == "pre_send_restart":
                    history = await journal.get_operation(
                        scope, eid, receipt.node_run_id, receipt.intent.operation_id
                    )
                    assert len(history) == 2 and len(requests) == 1
                    assert any(row.send_admitted and not row.results for row in history)
                if fallback:
                    route = receipt.intent.target.route_trace
                    assert isinstance(route, FallbackProviderRoute)
                    assert route.selected_deployment_id == FALLBACK
                    first = await journal.get_dispatch(
                        scope, eid, route.trigger_invocation_ref.record_id
                    )
                    assert first is not None and first.attempt_id != receipt.attempt_id
                    assert first.results[0].result.error.error_class == "unavailable"
                async with app.connect() as connection:
                    locked = await repository.get_execution(connection, scope, eid)
                    assert locked is not None
                    assert (
                        await read_historical_capabilities(
                            connection, scope, locked.execution
                        )
                        is not None
                    )
                    records = await load_records(connection, scope, locked)
                    attempts = await load_attempts(connection, scope, records)
                    tool_message = next(
                        item
                        for item in records.messages.values()
                        if isinstance(item, ToolMessage)
                    )
                assert len(records.tool_step_nodes) == 3
                assert len({run.node_run_id for run in records.runs.values()}) == 6
                assert len(tool_message.authorities) == 2
                assert len(tool_message.facts) == 1
                assert all(
                    any(
                        attempt.attempt_id == chosen.selected_attempt_id
                        and attempt.status is AttemptStatus.SUCCEEDED
                        for attempt in attempts
                    )
                    for chosen in tool_message.authorities
                )
                assert any(
                    attempt.status is AttemptStatus.FAILED for attempt in attempts
                )
                assert await worker().read_finalization(scope, eid) is not None
                for actual in tool_message.executions:
                    dispatch = await journal.get_dispatch(
                        scope, eid, actual.tool_execution_id
                    )
                    assert (
                        dispatch is not None
                        and dispatch.results[0].result.boundary == "acknowledged"
                    )
                    assert dispatch.results[0].result.response_digest is not None

    asyncio.run(run())
