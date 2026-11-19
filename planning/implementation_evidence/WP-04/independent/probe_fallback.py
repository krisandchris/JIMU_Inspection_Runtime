"""A real loopback timeout and independently reviewed target across Worker claims."""

import asyncio
from contextlib import AsyncExitStack
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

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.invocations import FallbackProviderRoute
from jimu_runtime.gateway.bridge import (
    CapabilityGateway,
    ModelCall,
    ModelCallLimits,
    require_model_output,
)
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
from jimu_runtime.workers.resource_inventory import (
    HostInventory,
    RemoteCapacity,
    RemoteQuota,
    ResourceScope,
)
from jimu_runtime.workers.resources import ResourceAdmission
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.worker import WorkerStep, WorkflowWorker
from jimu_runtime.workflow.worker_nodes import invoke
from jimu_runtime.workflow.worker_source import WorkerSource
from tests.fixtures.wp02.compiler.scenario import DEPLOYMENT, LIMITS
from tests.fixtures.wp03.nodes.scenario import parking_source
from tests.fixtures.wp03.pins.support import accepted_scenario, evaluated_fallback
from tests.fixtures.wp03.postgres.support import isolated_postgres


async def run(app, root):
    base_path = root / "baseline"
    base_path.mkdir()
    baseline = accepted_scenario(base_path, source=capacity_source(parking_source()))
    source, old = evaluated_fallback(baseline.resolve(), baseline.host)
    now = datetime.now(UTC)
    window = {
        "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
        "valid_until": datetime(2100, 1, 1, tzinfo=UTC),
    }

    class Host(prior.ParkingIntake):
        async def capabilities(self, scope):
            assert scope == self.scope
            regular = await super().capabilities(scope)
            return regular.model_copy(
                update={
                    "model_deployments": tuple(
                        item.model_copy(update=window) for item in old.model_deployments
                    ),
                    "model_reviews": tuple(
                        item.model_copy(
                            update={
                                "metadata": item.metadata.model_copy(update=window),
                            }
                        )
                        for item in old.model_reviews
                    ),
                }
            )

    endpoints = []
    peers = []
    states = []

    async with AsyncExitStack() as stack:
        for i, row in enumerate(old.model_deployments):
            identity = row.identity
            peer = LoopbackChat()
            peers.append(peer)
            peer.model_version = identity.model_version
            peer.deployment_version = identity.deployment_version
            if i:
                peer.late_continue.set()
            server = await asyncio.start_server(peer.handle, host="127.0.0.1", port=0)
            await stack.enter_async_context(server)
            endpoints.append(
                ChatEndpoint(
                    endpoint_id=f"independent-fallback-endpoint-{i}",
                    endpoint_version="1.0.0",
                    provider_id=identity.provider_id,
                    provider_kind=identity.provider_kind,
                    deployment_id=identity.deployment_id,
                    model_id="independent-vision",
                    url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions",
                    protocol="openai_chat_completions",
                    allow_plaintext_loopback=True,
                    limits=HttpLimits(
                        timeout_ms=350 if not i else 2200,
                        io_timeout_ms=2200,
                        request_bytes=4096,
                        response_bytes=2048,
                        image_bytes=1024,
                        max_retry_after_seconds=3,
                    ),
                )
            )

        async def ownership(_scope, state):
            rows = tuple(
                DeploymentOwnership(
                    deployment=deployment.identity,
                    owner_ref=TrustPin(
                        identity=f"independent-model-owner-{i}",
                        version="1.0.0",
                        sha256=digest(f"reviewed trusted deployment {i}".encode()),
                    ),
                    endpoint_descriptor_ref=endpoints[i].descriptor_pin,
                    model_id=endpoints[i].model_id,
                    version_evidence=ReviewReportRef(
                        report_id=f"independent-version-{i}",
                        version="1.0.0",
                        sha256=digest(f"local exact version proof {i}".encode()),
                        review_registry_id="independent-reviews",
                    ),
                )
                for i, deployment in enumerate(state.model_deployments)
            )
            states.append(
                CurrentRoutingState(
                    capabilities=state,
                    ownership=tuple(
                        DeploymentOwnershipRegistration(
                            ownership=row,
                            valid_from=now - timedelta(hours=1),
                            valid_until=now + timedelta(hours=1),
                            revoked_at=None,
                        )
                        for row in rows
                    ),
                )
            )
            return rows

        budget = prior.BUDGET.model_copy(
            update={
                "default": prior.BUDGET.default.model_copy(
                    update={"retryable_error_classes": ("timeout",)}
                ),
            }
        )
        accepted, preview, intake_host, manager, store = await accept_independent(
            app,
            root / "reviewed",
            "reviewed-fallback",
            source=source,
            ownership=ownership,
            budget=budget,
            compile_capabilities=old,
            host_factory=Host,
        )
        context = preview.context_input()
        host = prior.WorkerInputs(
            intake_host,
            WorkerSource(
                media=preview.scope.media,
                device=context.device_ref,
                camera=context.camera_ref,
                fixture_deployment_id=DEPLOYMENT,
            ),
        )
        inventory = HostInventory(
            "independent-fallback",
            2000,
            0,
            (ResourceScope("remote", "remote", 1, pool_or_quota_id="fixture.quota"),),
            (RemoteQuota("fixture.quota", 1, RemoteCapacity.LOCAL_TRANSPORT),),
        )
        ledger = root / "fallback-admission.db"
        ResourceAdmission.provision(ledger, inventory)
        journal = DispatchJournal(app)
        outcomes = []

        async def current(_scope):
            return states[0]

        async def adapter(claimed):
            if claimed.node.node_id is NodeId.N02:
                binding = next(
                    pin.binding_ref
                    for pin in claimed.opened.resolved.pins.model_pins
                    if pin.binding_ref.artifact_id
                    == ModelCapability.PROFILE_DISCOVERY.value
                )
                definition = claimed.opened.scope.resolve(binding, ModelBinding)
                contract = ChatContract(
                    definition.input_schema_ref,
                    definition.output_schema_ref,
                    Response,
                    response_schema=claimed.opened.resolved.catalogue.resolve(
                        definition.output_schema_ref
                    ).canonical_bytes,
                )
                gateway = CapabilityGateway(
                    app,
                    journal,
                    ResourceAdmission(ledger, inventory),
                    HttpModelTransport(
                        tuple(endpoints), (contract,), max_concurrency=1
                    ),
                    current,
                    ModelCallLimits(
                        node=2, operation=2, deployment=1, retryable=("timeout",)
                    ),
                )
                outcome = await gateway.invoke_model(
                    claimed,
                    ModelCall(
                        binding_ref=binding,
                        purposes=(ModelCapability.PROFILE_DISCOVERY,),
                        profile_refs=(),
                        prompt="independent late response",
                        resource_scope_id="remote",
                    ),
                )
                outcomes.append(outcome)
                assert require_model_output(outcome) == b'{"profile_hits":[]}'
            return await asyncio.to_thread(
                invoke,
                claimed.opened,
                claimed.previous,
                claimed.branch,
                claimed.node,
                claimed.attempt,
            )

        async def tick(owner):
            worker = WorkflowWorker(
                app,
                manager,
                store,
                host=host,
                bundle_limits=LIMITS,
                invoke_claimed_node=adapter,
            )
            return await worker.run_once(
                accepted.task.idempotency_scope,
                accepted.execution.execution_id,
                owner=owner,
                lease_ms=9000,
            )

        try:
            assert await tick("independent-context") is WorkerStep.APPLIED
            assert await tick("independent-primary") is WorkerStep.RETRY_SCHEDULED
            await asyncio.sleep(0.07)
            assert await tick("independent-fallback") is WorkerStep.APPLIED
        finally:
            peers[0].late_continue.set()
        assert [len(peer.requests) for peer in peers] == [1, 1]
        original, target = (outcome.invocation for outcome in outcomes)
        assert original is not None and target is not None
        assert original.outcome.error.error_class == "timeout"
        assert target.outcome.status == "succeeded"
        assert isinstance(target.route_trace, FallbackProviderRoute)
        assert (
            target.route_trace.trigger_invocation_ref.record_id
            == original.model_invocation_id
        )
        assert target.route_trace.evaluation_refs == (
            old.model_reviews[0].metadata.report_ref,
        )
        assert target.route_trace.from_deployment_id == endpoints[0].deployment_id
        assert target.route_trace.to_deployment_id == endpoints[1].deployment_id
        assert target.attempt_ref != original.attempt_ref
        async with app.connect() as connection:
            rows = (
                await connection.execute(
                    text("""
                SELECT i.dispatch_id, i.operation_id, i.attempt_id, i.fence, i.lease_owner,
                       r.record->'error'->>'error_class' AS exact_error
                FROM jimu_workflow.dispatch_intents i
                JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
                WHERE i.execution_id=:eid ORDER BY i.fence
            """),
                    {"eid": accepted.execution.execution_id},
                )
            ).all()
        assert len(rows) == 2 and rows[0].operation_id == rows[1].operation_id
        assert rows[0].exact_error == "timeout" and rows[1].exact_error is None
        assert [row.lease_owner for row in rows] == [
            "independent-primary",
            "independent-fallback",
        ]
        prior.emit(
            "current_exact_fallback_after_timeout",
            dispatches=[tuple(row) for row in rows],
            primary=endpoints[0].deployment_id,
            fallback=endpoints[1].deployment_id,
            review=target.route_trace.evaluation_refs[0].report_id,
        )


async def main():
    with (
        isolated_postgres() as database,
        TemporaryDirectory(prefix="wp04-fallback-") as root,
    ):
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        try:
            await apply_migrations(owner, database.target)
            await run(app, Path(root))
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
