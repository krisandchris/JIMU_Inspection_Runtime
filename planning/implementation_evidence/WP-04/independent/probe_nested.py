"""Reopen a model-backed Tool with a journal-owned child invocation, or reject a forgery."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_model import (
    LoopbackChat,
    Response,
    accept_independent,
    capacity_source,
    digest,
    prior,
)
from sqlalchemy import text

from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.compiler import ReferenceSlot, SourceIdentity
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.nodes.tool_execution import (
    InvokedToolStep,
    ObjectImageView,
    ToolNodeInput,
    ToolNodeOutput,
    ToolPlanRunSummary,
)
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import ModelInvocationRef, ToolExecutionRef
from jimu_runtime.domain.runtime.invocations import FallbackProviderRoute
from jimu_runtime.domain.runtime.tool_executions import (
    ToolArtifactRecordRef,
    ToolArtifactRef,
)
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCallLimits
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
    NestedModelRequest,
    RemoteToolWorker,
    ToolImplementation,
    ToolWorkerRegistry,
    ToolWorkerResponse,
)
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow.mock_context import snapshot_ref
from jimu_runtime.workflow.mock_support import artifact
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.worker import (
    AdapterRejected,
    RetryableNodeFailure,
    WorkerStep,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_capabilities import ToolOwnershipPin
from jimu_runtime.workflow.worker_nodes import NodeResult, invocation_envelope, invoke
from jimu_runtime.workflow.worker_records import ToolMessage
from jimu_runtime.workflow.worker_source import WorkerSource
from tests.fixtures.wp02.compiler.scenario import (
    DEPLOYMENT,
    LIMITS,
    approve_source_bytes,
    capability_snapshot,
)
from tests.fixtures.wp03.nodes.scenario import parking_source
from tests.fixtures.wp03.pins.support import accepted_scenario, evaluated_fallback
from tests.fixtures.wp03.postgres.support import isolated_postgres

AUXILIARY = ModelCapability.VISION_AUXILIARY.value
VERSION = "1.0.0"


def nested_source():
    source = parking_source()
    rows = []
    for item in source.artifacts:
        if (
            item.kind is ArtifactKind.MODEL_BINDING
            and item.artifact_id == ModelCapability.PROFILE_DISCOVERY.value
        ):
            doc = json.loads(item.content)
            doc.update(model_binding_id=AUXILIARY, capability=AUXILIARY)
            doc.pop("prompt_template_ref")
            rows.append(
                replace(
                    item,
                    artifact_id=AUXILIARY,
                    origin=SourceIdentity(
                        artifact_id=f"model_binding:{AUXILIARY}", version=VERSION
                    ),
                    content=json.dumps(doc).encode(),
                    reference_slots=tuple(
                        slot
                        for slot in item.reference_slots
                        if slot.location != ("prompt_template_ref",)
                    ),
                )
            )
        elif item.kind is ArtifactKind.TOOL:
            doc = json.loads(item.content)
            doc.update(provider_kind="remote_service", model_binding_ref=None)
            item = replace(
                item,
                content=json.dumps(doc).encode(),
                reference_slots=(
                    *(
                        replace(
                            slot,
                            target=(ArtifactKind.POLICY, "remote_resources", VERSION),
                        )
                        if slot.location == ("resource_requirements_ref",)
                        else slot
                        for slot in item.reference_slots
                    ),
                    ReferenceSlot(
                        location=("model_binding_ref",),
                        target=(ArtifactKind.MODEL_BINDING, AUXILIARY, VERSION),
                    ),
                ),
            )
        elif item.kind is ArtifactKind.POLICY and item.artifact_id == "evidence":
            doc = json.loads(item.content)
            doc["policy"]["original_media_required"] = False
            item = replace(item, content=json.dumps(doc).encode())
        elif item.kind is ArtifactKind.POLICY and item.artifact_id == "execution":
            doc = json.loads(item.content)
            doc["policy"].update(
                timeout_ms=5000,
                input_byte_limit=4096,
                max_attempts=2,
                retryable_error_classes=["timeout"],
                retry_backoff_ms=[20],
            )
            item = replace(item, content=json.dumps(doc).encode())
        rows.append(item)
    return capacity_source(replace(source, artifacts=tuple(rows)))


def with_nested_protocol(original: CapabilitySnapshot) -> CapabilitySnapshot:
    roles = []
    for row in original.runtime_protocols:
        roles.append(row)
        if row.role == ModelCapability.PROFILE_DISCOVERY:
            roles.append(
                row.model_copy(update={"role": ModelCapability.VISION_AUXILIARY})
            )
    deployments = []
    for registration in original.model_deployments:
        discovery = next(
            contract
            for contract in registration.contracts
            if contract.capability == ModelCapability.PROFILE_DISCOVERY
        )
        deployments.append(
            registration.model_copy(
                update={
                    "contracts": (
                        *registration.contracts,
                        discovery.model_copy(
                            update={"capability": ModelCapability.VISION_AUXILIARY}
                        ),
                    ),
                }
            )
        )
    return original.model_copy(
        update={
            "runtime_protocols": tuple(roles),
            "model_deployments": tuple(deployments),
        }
    )


def nested_route_source(root):
    base = root / "nested-baseline"
    base.mkdir()
    source = nested_source()
    capabilities = with_nested_protocol(capability_snapshot())
    baseline = accepted_scenario(base, source=source, host=capabilities)
    fallback, reviewed = evaluated_fallback(baseline.resolve(), capabilities)
    policy_names = {"provider", "fallback"}
    switched = {
        item.artifact_id: item
        for item in fallback.artifacts
        if item.kind is ArtifactKind.POLICY and item.artifact_id in policy_names
    }
    source = approve_source_bytes(
        replace(
            source,
            artifacts=tuple(
                switched.get(row.artifact_id, row)
                if row.kind is ArtifactKind.POLICY
                else row
                for row in source.artifacts
            ),
        )
    )
    return source, reviewed


async def run(app, root: Path, *, forged: bool) -> None:
    source, compile_caps = nested_route_source(root)
    now = datetime.now(UTC)
    window = {
        "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
        "valid_until": datetime(2100, 1, 1, tzinfo=UTC),
    }

    class Host(prior.ParkingIntake):
        async def capabilities(self, scope):
            regular = await super().capabilities(scope)
            return regular.model_copy(
                update={
                    "runtime_protocols": tuple(
                        row.model_copy(update=window)
                        for row in compile_caps.runtime_protocols
                    ),
                    "model_deployments": tuple(
                        row.model_copy(update=window)
                        for row in compile_caps.model_deployments
                    ),
                    "model_reviews": tuple(
                        row.model_copy(
                            update={
                                "metadata": row.metadata.model_copy(update=window),
                            }
                        )
                        for row in compile_caps.model_reviews
                    ),
                }
            )

    peers, endpoints, current_states = [], [], []
    async with AsyncExitStack() as stack:
        for i, row in enumerate(compile_caps.model_deployments):
            peer = LoopbackChat()
            selected = row.identity
            peer.model_version = selected.model_version
            peer.deployment_version = selected.deployment_version
            if i or forged:
                peer.late_continue.set()
            peers.append(peer)
            server = await asyncio.start_server(peer.handle, host="127.0.0.1", port=0)
            await stack.enter_async_context(server)
            endpoints.append(
                ChatEndpoint(
                    endpoint_id=f"independent-auxiliary-{i}",
                    endpoint_version=VERSION,
                    provider_id=selected.provider_id,
                    deployment_id=selected.deployment_id,
                    provider_kind=selected.provider_kind,
                    model_id="independent-vision",
                    url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions",
                    protocol="openai_chat_completions",
                    allow_plaintext_loopback=True,
                    limits=HttpLimits(
                        timeout_ms=2200 if i or forged else 350,
                        io_timeout_ms=2100,
                        request_bytes=4096,
                        response_bytes=2048,
                        image_bytes=1024,
                        max_retry_after_seconds=1,
                    ),
                )
            )

        async def freeze_models(_scope, caps):
            registrations = []
            for i, row in enumerate(caps.model_deployments):
                owner = DeploymentOwnership(
                    deployment=row.identity,
                    owner_ref=TrustPin(
                        identity=f"nested-owner-{i}",
                        version=VERSION,
                        sha256=digest(f"nested independent owner {i}".encode()),
                    ),
                    endpoint_descriptor_ref=endpoints[i].descriptor_pin,
                    model_id=endpoints[i].model_id,
                    version_evidence=ReviewReportRef(
                        report_id=f"independent-actual-vision-{i}",
                        version=VERSION,
                        sha256=digest(f"verified loopback deployment {i}".encode()),
                        review_registry_id="independent-reviews",
                    ),
                )
                registrations.append(
                    DeploymentOwnershipRegistration(
                        ownership=owner,
                        valid_from=now - timedelta(hours=1),
                        valid_until=now + timedelta(hours=1),
                        revoked_at=None,
                    )
                )
            current_states.append(
                CurrentRoutingState(capabilities=caps, ownership=tuple(registrations))
            )
            return tuple(row.ownership for row in registrations)

        tool_proof = TrustPin(
            identity="independent-nested-worker",
            version=VERSION,
            sha256=digest(b"independent nested worker implementation"),
        )

        async def freeze_tools(_scope, resolved):
            return tuple(
                ToolOwnershipPin(
                    tool_ref=pin.tool_ref,
                    implementation_version=pin.implementation_version,
                    implementation_proof_ref=tool_proof,
                )
                for pin in resolved.pins.tool_pins
            )

        budget = prior.BUDGET.model_copy(
            update={
                "default": prior.BUDGET.default.model_copy(
                    update={"retryable_error_classes": ("timeout",)}
                ),
            }
        )
        accepted, preview, intake_host, manager, store = await accept_independent(
            app,
            root / "nested",
            "nested-forged" if forged else "nested-fallback",
            source=source,
            ownership=freeze_models,
            tool_ownership=freeze_tools,
            budget=budget,
            compile_capabilities=compile_caps,
            host_factory=Host,
        )
        ctx = preview.context_input()
        host = prior.WorkerInputs(
            intake_host,
            WorkerSource(
                media=preview.scope.media,
                device=ctx.device_ref,
                camera=ctx.camera_ref,
                fixture_deployment_id=DEPLOYMENT,
            ),
        )
        scope, execution = accepted.task.idempotency_scope, accepted.execution
        actor = ActorRef(
            actor_kind="service",
            actor_id="independent-auxiliary",
            identity_registry_id="independent-registry",
        )
        step_store = StepAttemptStore(app, actor=actor)
        journal = DispatchJournal(app)
        inventory = HostInventory(
            "independent-nested",
            3000,
            0,
            (
                ResourceScope("tool", "remote", 1, pool_or_quota_id="fixture.quota"),
                ResourceScope("model", "remote", 1, pool_or_quota_id="fixture.quota"),
            ),
            (RemoteQuota("fixture.quota", 2, RemoteCapacity.LOCAL_TRANSPORT),),
        )
        ledger = root / "nested-resources.db"
        ResourceAdmission.provision(ledger, inventory)
        observations, adapter_errors, returned_steps = [], [], []

        async def current_models(_scope):
            return current_states[0]

        async def conversion(dispatch):
            if dispatch.invoke_model is None:
                raise AssertionError("trusted worker has no bound model port")
            answer = await dispatch.invoke_model(
                NestedModelRequest(prompt="independent late response")
            )
            invocation = answer.invocation
            assert invocation is not None
            observations.append(invocation)
            if forged:
                invocation = invocation.model_copy(
                    update={"model_invocation_id": "invented-by-tool"}
                )
            records = (invocation,)
            refs = tuple(
                ToolArtifactRef(
                    artifact_ref=ToolArtifactRecordRef(
                        record_kind=RuntimeRecordKind.MODEL_INVOCATION,
                        record_id=item.model_invocation_id,
                    ),
                    digest=digest(canonical_bytes(item.model_dump(mode="json"))),
                )
                for item in records
            )
            normalized = {
                "diagnostics": [],
                "evidence_refs": [],
                "evidence": [],
                "output_states": [],
            }
            if answer.error is not None:
                assert answer.error.error_class == "timeout"
                normalized.update(
                    status="failed", error=answer.error.model_dump(mode="json")
                )
            else:
                assert answer.content == b'{"profile_hits":[]}'
                normalized.update(
                    status="succeeded",
                    payload={"kind": "bool", "value": True},
                    output_states=[
                        {
                            "output_name": "outside",
                            "fact_status": "known",
                            "evidence_refs": [],
                        }
                    ],
                )
            return ToolWorkerResponse(
                json.dumps(normalized).encode(),
                VERSION,
                tool_proof,
                nested_invocations=records,
                invocation_refs=refs,
            )

        async def invoke_tool(claimed):
            obj = claimed.previous.object_record(claimed.branch)
            profile = next(
                item.definition
                for item in claimed.opened.profiles
                if item.rules.profile_ref == obj.profile_ref
            )
            inputs = ToolNodeInput(
                schema_version=VERSION,
                envelope=invocation_envelope(
                    claimed.opened, claimed.branch, claimed.node, claimed.attempt
                ),
                object_record=obj,
                image_view=ObjectImageView(roi=obj.bbox, transforms=()),
                context_snapshot_ref=snapshot_ref(claimed.previous.snapshot()),
                tool_plan_ref=profile.tool_plan_ref,
                validated_observation_fact_refs=(),
            )
            plan = claimed.opened.scope.resolve(
                profile.tool_plan_ref, ToolPlanDefinition
            )
            (step,) = plan.steps
            tool = claimed.opened.scope.resolve(step.tool_ref, ToolDefinition)
            binding = claimed.opened.scope.resolve(tool.model_binding_ref, ModelBinding)
            contract = ChatContract(
                binding.input_schema_ref,
                binding.output_schema_ref,
                Response,
                response_schema=claimed.opened.resolved.catalogue.resolve(
                    binding.output_schema_ref
                ).canonical_bytes,
            )
            resources = ResourceAdmission(ledger, inventory)
            gateway = CapabilityGateway(
                app,
                journal,
                resources,
                HttpModelTransport(tuple(endpoints), (contract,), max_concurrency=1),
                current_models,
                ModelCallLimits(
                    node=2, operation=2, deployment=1, retryable=("timeout",)
                ),
            )
            requirement = tool.implementation_requirement
            implementation = ToolImplementation(
                requirement.capability_id,
                requirement.contract_version,
                VERSION,
                tool_proof,
                tool.provider_kind,
                tool.input_schema_ref,
                tool.output_schema_ref,
                "remote",
                RemoteToolWorker(resources, "tool", "nested-tool", conversion),
            )
            adapter = ToolExecutionAdapter(
                ToolExecutor(ToolWorkerRegistry((implementation,))),
                step_store,
                journal,
                intake_host.capabilities,
                model_gateway=gateway,
                model_resource_scope=lambda *_: "model",
            )
            winner = await adapter.execute_step(
                claimed, inputs, step.step_id, {"image": "stable source RGB"}
            )
            returned_steps.append(winner)
            if winner.selection is None:
                raise AdapterRejected("independent_tool_no_selected_output")
            result, selection = winner.result, winner.selection
            tool_ref = ToolExecutionRef(
                record_kind=RuntimeRecordKind.TOOL_EXECUTION,
                record_id=result.execution.tool_execution_id,
            )
            artifacts = (
                artifact(
                    RuntimeRecordKind.TOOL_EXECUTION,
                    tool_ref.record_id,
                    result.execution,
                ),
                *(
                    artifact(RuntimeRecordKind.FACT, row.fact_id, row)
                    for row in result.facts
                ),
                *(
                    artifact(
                        RuntimeRecordKind.MODEL_INVOCATION, row.model_invocation_id, row
                    )
                    for row in result.invocations
                ),
            )
            start = claimed.attempt.started_at
            assert start is not None
            end = datetime.now(UTC)
            output = ToolNodeOutput(
                schema_version=VERSION,
                envelope=SucceededNodeOutput(
                    schema_version=VERSION,
                    node_id=NodeId.N05,
                    node_run_id=claimed.node.node_run_id,
                    attempt_id=claimed.attempt.attempt_id,
                    trace=claimed.node.trace,
                    output_artifacts=tuple(item.pin for item in artifacts),
                    started_at=start,
                    finished_at=end,
                    duration_ms=(end - start) // timedelta(milliseconds=1),
                    status=NodeRunStatus.SUCCEEDED,
                ),
                summary=ToolPlanRunSummary(
                    tool_plan_ref=inputs.tool_plan_ref,
                    object_ref=result.execution.object_ref,
                    status="succeeded",
                    steps=(
                        InvokedToolStep(
                            disposition="invoked",
                            step_id=step.step_id,
                            tool_execution_ref=tool_ref,
                        ),
                    ),
                    validation_issues=(),
                ),
                tool_fact_refs=result.execution.generated_fact_refs,
                evidence_refs=result.execution.evidence_refs,
            )
            message = ToolMessage(
                schema_version=VERSION,
                inputs=inputs,
                output=output,
                executions=(result.execution,),
                facts=result.facts,
                authorities=(selection,),
                nested_invocations=result.invocations,
            )
            return NodeResult(
                message,
                (*artifacts, *message.authority_artifacts),
                tuple(
                    ModelInvocationRef(
                        record_kind=RuntimeRecordKind.MODEL_INVOCATION,
                        record_id=item.model_invocation_id,
                    )
                    for item in result.invocations
                ),
            )

        async def worker_adapter(claimed):
            if claimed.node.node_id is not NodeId.N05:
                return await asyncio.to_thread(
                    invoke,
                    claimed.opened,
                    claimed.previous,
                    claimed.branch,
                    claimed.node,
                    claimed.attempt,
                )
            try:
                return await invoke_tool(claimed)
            except (AdapterRejected, RetryableNodeFailure) as error:
                adapter_errors.append(str(error))
                raise

        async def worker_tick(owner: str):
            worker = WorkflowWorker(
                app,
                manager,
                store,
                host=host,
                bundle_limits=LIMITS,
                invoke_claimed_node=worker_adapter,
            )
            return await worker.run_once(
                scope, execution.execution_id, owner=owner, lease_ms=9000
            )

        actions = []
        try:
            for _ in range(12):
                status = await worker_tick(
                    "independent-nested-recovery"
                    if WorkerStep.RETRY_SCHEDULED in actions
                    else "independent-nested-first"
                )
                actions.append(status)
                if status is WorkerStep.RETRY_SCHEDULED:
                    await asyncio.sleep(0.08)
                if status is WorkerStep.FINALIZED:
                    break
        finally:
            peers[0].late_continue.set()
        async with app.connect() as connection:
            rows = (
                await connection.execute(
                    text("""
                SELECT i.record->'target'->>'kind' AS kind, i.operation_id, i.dispatch_id,
                       i.attempt_id, i.lease_owner, r.record->'error'->>'error_class' AS error_class,
                       r.record->'execution'->>'status' AS tool_status,
                       r.record->'error'->>'code' AS error_code
                FROM jimu_workflow.dispatch_intents i
                JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
                WHERE i.execution_id=:eid ORDER BY i.recorded_at
            """),
                    {"eid": execution.execution_id},
                )
            ).all()
            statuses = (
                await connection.execute(
                    text("""
                SELECT c.attempt_status, c.tool_execution_id, c.child_attempt_id, c.record
                FROM jimu_workflow.tool_step_completions c
                WHERE c.execution_id=:eid ORDER BY c.record #>> '{result,execution,started_at}'
            """),
                    {"eid": execution.execution_id},
                )
            ).all()
        prior.emit(
            "nested_forger_rejected" if forged else "nested_fallback_across_children",
            physical_posts=[len(peer.requests) for peer in peers],
            node_steps=[value.value for value in actions],
            errors=adapter_errors,
            dispatches=[tuple(row) for row in rows],
            step_completions=[tuple(row)[:3] for row in statuses],
        )
        if forged:
            assert [len(peer.requests) for peer in peers] == [1, 0]
            assert len(observations) == 1 and all(
                row.attempt_status != "succeeded" for row in statuses
            )
            assert all(row.dispatch_id != "invented-by-tool" for row in rows)
            assert not any(row.selection is not None for row in returned_steps)
            assert all(row.tool_status != "succeeded" for row in rows), (
                "unaudited Tool invocation was retained as a successful Tool execution"
            )
            return

        assert [len(peer.requests) for peer in peers] == [1, 1]
        assert (
            WorkerStep.RETRY_SCHEDULED in actions
            and actions[-1] is WorkerStep.FINALIZED
        )
        assert len(statuses) == 2 and [row.attempt_status for row in statuses] == [
            "failed",
            "succeeded",
        ]
        first, second = observations
        assert first.outcome.error.error_class == "timeout"
        assert isinstance(second.route_trace, FallbackProviderRoute)
        assert (
            second.route_trace.trigger_invocation_ref.record_id
            == first.model_invocation_id
        )
        assert second.route_trace.evaluation_refs == (
            compile_caps.model_reviews[0].metadata.report_ref,
        )
        assert [row.child_attempt_id for row in statuses] == [
            first.attempt_ref.record_id,
            second.attempt_ref.record_id,
        ]
        nested = [
            row
            for row in rows
            if row.dispatch_id
            in {first.model_invocation_id, second.model_invocation_id}
        ]
        assert len(nested) == 2 and nested[0].operation_id == nested[1].operation_id
        assert nested[0].lease_owner == "independent-nested-first"
        assert nested[1].lease_owner == "independent-nested-recovery"
        reader = WorkflowWorker(app, manager, store, host=host, bundle_limits=LIMITS)
        final = await reader.read_finalization(scope, execution.execution_id)
        assert final is not None and final.coverage.coverage_status.value == "complete"


async def main() -> None:
    forged = sys.argv[1:] == ["forged"]
    with (
        isolated_postgres() as database,
        TemporaryDirectory(prefix="wp04-nested-") as root,
    ):
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        try:
            await apply_migrations(owner, database.target)
            await run(app, Path(root), forged=forged)
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
