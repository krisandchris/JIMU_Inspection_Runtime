"""Public worker and gateway entry with current revocation and real HTTP."""

import asyncio
import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from probe_model import LoopbackChat, Response, accept_independent, digest, prior
from sqlalchemy import text

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.gateway.bridge import (
    CapabilityGateway,
    ModelCall,
    ModelCallFailure,
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
from jimu_runtime.workflow.worker import WorkflowWorker
from jimu_runtime.workflow.worker_nodes import invoke
from jimu_runtime.workflow.worker_source import WorkerSource
from tests.fixtures.wp02.compiler.scenario import DEPLOYMENT, LIMITS
from tests.fixtures.wp03.postgres.support import isolated_postgres


async def exercise(app, work: Path, label: str) -> None:
    server_fixture = LoopbackChat()
    server = await asyncio.start_server(server_fixture.handle, host="127.0.0.1", port=0)

    async def historical(_scope, capabilities):
        nonlocal endpoint, initial, active
        selected = capabilities.model_deployments[0].identity
        server_fixture.model_version = selected.model_version
        server_fixture.deployment_version = selected.deployment_version
        endpoint = ChatEndpoint(
            endpoint_id="bridge-chat-endpoint",
            endpoint_version="1.0.0",
            provider_id=selected.provider_id,
            deployment_id=selected.deployment_id,
            provider_kind=selected.provider_kind,
            model_id="independent-vision",
            url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1/chat/completions",
            protocol="openai_chat_completions",
            allow_plaintext_loopback=True,
            limits=HttpLimits(
                timeout_ms=4000,
                io_timeout_ms=3500,
                request_bytes=4096,
                response_bytes=2048,
                image_bytes=1024,
                max_retry_after_seconds=3,
            ),
        )
        own = DeploymentOwnership(
            deployment=selected,
            owner_ref=TrustPin(
                identity="bridge-owner",
                version="1.0.0",
                sha256=digest(b"independent bridge ownership"),
            ),
            endpoint_descriptor_ref=endpoint.descriptor_pin,
            model_id=endpoint.model_id,
            version_evidence=None,
        )
        now = datetime.now(UTC)
        active = DeploymentOwnershipRegistration(
            ownership=own,
            revoked_at=None,
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(hours=1),
        )
        initial = CurrentRoutingState(capabilities=capabilities, ownership=(active,))
        return (own,)

    endpoint = initial = active = None
    async with server:
        accepted, preview, intake_host, manager, store = await accept_independent(
            app,
            work / label,
            label,
            ownership=historical,
        )
        assert endpoint is not None and initial is not None and active is not None
        context = preview.context_input()
        host = prior.WorkerInputs(
            capability_host=intake_host,
            source=WorkerSource(
                media=preview.scope.media,
                device=context.device_ref,
                camera=context.camera_ref,
                fixture_deployment_id=DEPLOYMENT,
            ),
        )
        observations = {}
        current = initial
        registry_calls = 0

        async def registry(scope):
            nonlocal registry_calls
            assert scope == accepted.task.idempotency_scope
            registry_calls += 1
            return current

        def revoke():
            nonlocal current
            current = CurrentRoutingState(
                capabilities=initial.capabilities,
                ownership=(
                    active.model_copy(update={"revoked_at": datetime.now(UTC)}),
                ),
            )

        async def revoke_in_flight():
            await asyncio.wait_for(server_fixture.late_started.wait(), timeout=4)
            revoke()
            server_fixture.late_continue.set()

        resource_scope = "independent-provider"
        inventory = HostInventory(
            "independent-bridge",
            3000,
            0,
            (
                ResourceScope(
                    resource_scope, "remote", 2, pool_or_quota_id="fixture.quota"
                ),
            ),
            (RemoteQuota("fixture.quota", 2, RemoteCapacity.LOCAL_TRANSPORT),),
        )
        admission = ResourceAdmission.provision(
            work / f"{label}-admission.db", inventory
        )
        journal = DispatchJournal(app)

        async def adapter(claimed):
            nonlocal current
            if claimed.node.node_id is not NodeId.N02:
                return await asyncio.to_thread(
                    invoke,
                    claimed.opened,
                    claimed.previous,
                    claimed.branch,
                    claimed.node,
                    claimed.attempt,
                )
            binding = next(
                pin.binding_ref
                for pin in claimed.opened.resolved.pins.model_pins
                if pin.binding_ref.artifact_id
                == ModelCapability.PROFILE_DISCOVERY.value
            )
            definition = claimed.opened.scope.resolve(binding, ModelBinding)
            transport = HttpModelTransport(
                (endpoint,),
                (
                    ChatContract(
                        definition.input_schema_ref,
                        definition.output_schema_ref,
                        Response,
                        response_schema=claimed.opened.resolved.catalogue.resolve(
                            definition.output_schema_ref
                        ).canonical_bytes,
                    ),
                ),
                max_concurrency=2,
            )
            gateway = CapabilityGateway(
                app,
                journal,
                admission,
                transport,
                registry,
                ModelCallLimits(
                    node=1, operation=2, deployment=1, retryable=("transport",)
                ),
            )
            sequence = (1,) if label == "inflight-revocation" else (1, 2)
            for number in sequence:
                if label == "revocation" and number == 2:
                    revoke()
                call = ModelCall(
                    binding_ref=binding,
                    purposes=(ModelCapability.PROFILE_DISCOVERY,),
                    profile_refs=(),
                    prompt="independent late response"
                    if label == "inflight-revocation"
                    else f"independent {label} {number}",
                    resource_scope_id=resource_scope,
                )
                try:
                    outcome = await gateway.invoke_model(claimed, call)
                    observations[number] = (
                        outcome.boundary,
                        outcome.error.code if outcome.error else None,
                    )
                    if label == "inflight-revocation":
                        assert outcome.output is None
                    elif number == 1:
                        assert require_model_output(outcome) == b'{"profile_hits":[]}'
                except ModelCallFailure as error:
                    observations[number] = ("rejected", error.code)
                except Exception:
                    traceback.print_exc()
                    raise
            return await asyncio.to_thread(
                invoke,
                claimed.opened,
                claimed.previous,
                claimed.branch,
                claimed.node,
                claimed.attempt,
            )

        worker = WorkflowWorker(
            app,
            manager,
            store,
            host=host,
            bundle_limits=LIMITS,
            invoke_claimed_node=adapter,
        )
        revoker = (
            asyncio.create_task(revoke_in_flight())
            if label == "inflight-revocation"
            else None
        )
        try:
            for number in (1, 2):
                await worker.run_once(
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                    owner=f"independent-{label}-{number}",
                    lease_ms=10000,
                )
        finally:
            if revoker is not None:
                await revoker
        async with app.connect() as connection:
            records = (
                await connection.execute(
                    text("""
                SELECT i.dispatch_id, i.record->>'operation_id' AS operation,
                       r.record->>'boundary' AS boundary
                FROM jimu_workflow.dispatch_intents i
                LEFT JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
                WHERE i.execution_id=:eid ORDER BY i.recorded_at
            """),
                    {"eid": accepted.execution.execution_id},
                )
            ).all()
        prior.emit(
            label,
            attempts=observations,
            registry_calls=registry_calls,
            actual_posts=len(server_fixture.requests),
            journal_rows=[tuple(row) for row in records],
        )
        if label == "inflight-revocation":
            assert observations == {1: ("acknowledged", "model.ownership_unavailable")}
            assert len(server_fixture.requests) == len(records) == 1
            assert records[0].boundary == "acknowledged" and registry_calls >= 3
            return
        assert observations.get(1) == ("acknowledged", None), observations
        assert len(server_fixture.requests) == 1, (
            "second operation exceeded node or deployment send ceiling"
        )
        assert observations[2][0] in {"rejected", "not_dispatched"}
        assert registry_calls >= 2
        assert len(records) == 2 and records[-1].boundary == "not_dispatched", (
            "pre-send rejection has no durable receipt"
        )


async def main() -> None:
    for label in sys.argv[1:] or ("revocation", "inflight-revocation", "ceilings"):
        with (
            isolated_postgres() as database,
            TemporaryDirectory(prefix=f"wp04-{label}-") as root,
        ):
            owner = create_postgres_engine(database.migration_url)
            app = create_postgres_engine(database.application_url)
            try:
                await apply_migrations(owner, database.target)
                await exercise(app, Path(root), label)
            finally:
                await app.dispose()
                await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
