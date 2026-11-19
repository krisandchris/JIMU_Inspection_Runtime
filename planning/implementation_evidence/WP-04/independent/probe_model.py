"""Exercise strict routes, real HTTP framing and durable physical call history."""

import asyncio
import importlib.util
import json
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import text

from jimu_runtime.bundle.compiler import BundleCompileInput
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput, NodeError
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.invocations import InvocationError
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    AttemptRef,
    NodeRunDescriptor,
    NodeRunRef,
)
from jimu_runtime.gateway.http import (
    ChatContract,
    ChatEndpoint,
    HttpLimits,
    HttpModelTransport,
)
from jimu_runtime.gateway.invocation import DispatchRequest, project_dispatch
from jimu_runtime.gateway.journal import (
    DispatchConflict,
    DispatchIntent,
    DispatchJournal,
    DispatchLease,
    ModelDispatchResult,
    ModelDispatchTarget,
    ProviderDispatchObservation,
)
from jimu_runtime.gateway.routing import (
    CurrentRoutingState,
    DeploymentBudget,
    DeploymentOwnership,
    DeploymentOwnershipRegistration,
    RecordedInvocation,
    RouteBudget,
    RouteOperation,
    RoutePolicy,
    RouteRejected,
    plan_primary,
    plan_retry,
)
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.intake import IntakeService
from jimu_runtime.workflow.jobs import DurableJobs
from jimu_runtime.workflow.pins import resolve_execution_pins
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from planning.implementation_evidence import __path__ as _evidence_path
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    approve_source_bytes,
    capability_snapshot,
)
from tests.fixtures.wp02.releases.support import FixtureHost
from tests.fixtures.wp03.intake.support import FixtureIntakeHost, manager_for
from tests.fixtures.wp03.nodes.scenario import parking_fixture, parking_source
from tests.fixtures.wp03.pins.support import activate_scenario
from tests.fixtures.wp03.postgres.support import POSTGRES_IMAGE, isolated_postgres

_prior_path = Path(_evidence_path[0]) / "WP-03/independent/probe_wp03.py"
_prior_spec = importlib.util.spec_from_file_location(
    "independent_wp03_fixture", _prior_path
)
assert _prior_spec is not None and _prior_spec.loader is not None
prior = importlib.util.module_from_spec(_prior_spec)
_prior_spec.loader.exec_module(prior)


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


class Response(ContractModel):
    profile_hits: list[str]


class LoopbackChat:
    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.late_started = asyncio.Event()
        self.late_continue = asyncio.Event()
        self.receipts: dict[str, bytes] = {}

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            method, *_ = header.split(b"\r\n")
            assert method == b"POST /v1/chat/completions HTTP/1.1"
            size = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            body = await reader.readexactly(size)
            self.requests.append(body)
            ordinal = len(self.requests)
            data = json.loads(body)
            prompt = data["messages"][0]["content"][0]["text"]
            assert data["model"] == "independent-vision"
            assert data["response_format"]["json_schema"]["strict"] is True
            if prompt == "independent late response":
                self.late_started.set()
                await self.late_continue.wait()
            status = 429 if prompt == "independent throttled response" else 200
            response = json.dumps(
                {
                    "model": data["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": '{"profile_hits":[]}',
                            },
                        }
                    ],
                },
                separators=(",", ":"),
            ).encode()
            if prompt == "independent oversized response":
                response = b"x" * 8192
            if prompt == "independent duplicate response":
                response = response.replace(
                    b'{"model":', b'{"model":"first","model":', 1
                )
            if prompt == "independent nonfinite response":
                response = response.replace(
                    b'{"model":', b'{"created":1e999,"model":', 1
                )
            reported = prompt != "independent missing version response"
            request_id = f"wire-{ordinal}-{len(self.receipts)}"
            self.receipts[prompt] = response
            headers = (
                f"HTTP/1.1 {status} Reply\r\n"
                f"X-Request-Id: {request_id}\r\n"
                "Content-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Connection: close\r\n"
            )
            if reported:
                headers += f"X-Jimu-Deployment-Version: {self.deployment_version}\r\n"
                headers += f"X-Jimu-Model-Version: {self.model_version}\r\n"
            if status == 429:
                headers += "Retry-After: 99999\r\n"
            writer.write(headers.encode() + b"\r\n")
            chunks = (
                (response[:9], response[9:])
                if prompt != "independent oversized response"
                else (response[:17], response[17:])
            )
            for chunk in chunks:
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except ConnectionError:
            # A timed-out client may close after it has submitted the complete POST.
            return
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


def descriptor(attempt):
    return AttemptDescriptor(
        attempt_ref=AttemptRef(
            record_kind=RuntimeRecordKind.ATTEMPT, record_id=attempt.attempt_id
        ),
        execution_id=attempt.execution_id,
        owner=attempt.owner,
        attempt_number=attempt.attempt_number,
        status=attempt.status,
    )


def dispatch_intent(route, request, admit: bool = True):
    selected = route.selected
    operation = route.operation
    return DispatchIntent(
        dispatch_id=request.dispatch_id,
        operation_id=operation.operation_id,
        trace_id=operation.trace_id,
        request_digest=request.request_digest,
        input_digest=operation.input_digest,
        response_contract_digest=operation.response_contract_digest,
        resource_lease=DispatchLease(
            lease_id="local-model-reservation",
            owner_id="independent",
            scope_id="loopback",
        )
        if admit
        else None,
        target=ModelDispatchTarget(
            node_id=operation.node_run.node_id,
            configured_provider=route.configured,
            route_trace=route.trace,
            purposes=operation.purposes,
            profile_refs=operation.profile_refs,
            object_ref=operation.object_ref,
            selected_provider_id=selected.provider_id,
            selected_provider_kind=selected.provider_kind,
            selected_deployment_id=selected.deployment_id,
            selected_deployment_version=selected.deployment_version,
            selected_model_id=route.ownership.model_id,
            selected_model_version=selected.model_version,
            endpoint_descriptor_ref=route.ownership.endpoint_descriptor_ref,
            owner_ref=route.ownership.owner_ref,
        ),
    )


def project_http(request, wire, invited_at):
    outcome = project_dispatch(
        request,
        wire,
        started_at=max(invited_at, request.route.approved_at),
        completed_at=datetime.now(UTC),
    )
    observation = wire.observation
    return ModelDispatchResult(
        boundary=outcome.boundary,
        response_digest=outcome.response_digest,
        observed_response_bytes=wire.observed_response_bytes or 0,
        response_status=wire.response_status,
        acknowledgement="http_headers" if wire.response_status is not None else None,
        error=outcome.error,
        actual_provider=outcome.invocation.actual_provider
        if outcome.invocation
        else None,
        provider_request_id=observation.provider_request_id if observation else None,
        provider_observation=ProviderDispatchObservation(
            model_id=observation.model_id,
            deployment_version=observation.deployment_version,
            model_version=observation.model_version,
        )
        if observation
        else None,
        invocation=outcome.invocation,
    )


def capacity_source(source: BundleCompileInput) -> BundleCompileInput:
    artifacts = []
    for item in source.artifacts:
        if item.kind is ArtifactKind.POLICY and item.artifact_id in {
            "resources",
            "remote_resources",
        }:
            data = json.loads(item.content)
            data["policy"].update(
                input_byte_limit=8192, output_byte_limit=8192, timeout_ms=6500
            )
            item = replace(item, content=json.dumps(data).encode())
        artifacts.append(item)
    return approve_source_bytes(replace(source, artifacts=tuple(artifacts)))


async def accept_independent(
    app,
    work,
    label,
    *,
    source=None,
    ownership=None,
    tool_ownership=None,
    budget=prior.BUDGET,
    compile_capabilities=None,
    host_factory=None,
):
    work.mkdir()
    (work / "fixture").mkdir()
    preview = parking_fixture(work / "fixture")
    authority = preview.host
    store = LocalBundleStore(work / "ledger")
    manager = manager_for(store, authority)
    activate_scenario(
        work / "admission",
        store,
        manager,
        authority,
        capacity_source(parking_source() if source is None else source),
        capability_snapshot() if compile_capabilities is None else compile_capabilities,
        number=1,
    )
    task = type(preview.submission).model_validate(
        {
            **preview.submission.model_dump(),
            "request_id": "independent-" + label,
            "idempotency_key": "independent-" + label,
        }
    )
    scope = task.idempotency_scope
    host = (prior.ParkingIntake if host_factory is None else host_factory)(
        authority, scope=scope, asset=preview.scope.media
    )
    intake = IntakeService(
        app,
        manager,
        store,
        host=host,
        bundle_limits=LIMITS,
        scheduling_limits=budget,
        max_submission_bytes=65536,
        historical_ownership=ownership,
        historical_tool_ownership=tool_ownership,
    )
    accepted = await intake.submit(task, principal=authority.task_client)
    assert await intake.submit(task, principal=authority.task_client) == accepted
    return accepted, preview, host, manager, store


async def run(app, work):
    accepted, preview, _host, _manager, store = await accept_independent(
        app, work / "model-parking", "model"
    )
    scope = accepted.task.idempotency_scope
    eid = accepted.execution.execution_id
    context = preview.context_input()
    config = {
        "url": app.url.render_as_string(hide_password=False),
        "scope": scope.model_dump(mode="json"),
        "trace": accepted.execution.trace.model_dump(mode="json"),
        "release_id": accepted.task.release_id,
        "bundle_digest": accepted.task.bundle_digest,
        "execution_id": eid,
        "ledger": str(work / "model-parking" / "ledger"),
        "mode": "standard",
        "profiles": [],
        "source": {
            "media": preview.scope.media.model_dump(mode="json"),
            "device": context.device_ref.model_dump(mode="json"),
            "camera": context.camera_ref.model_dump(mode="json"),
        },
    }
    worker = await prior.launch(config, "wp04-context")
    try:
        assert (await worker.ask("worker"))["result"] == "WorkerStep.APPLIED"
    finally:
        await worker.kill()

    jobs = DurableJobs(app)
    first = await jobs.claim(scope, "independent-model-first", 10_000, execution_id=eid)
    assert first is not None
    attempt = await jobs.start(first)
    assert attempt is not None and attempt.started_at is not None
    async with app.connect() as connection:
        locked = await repo.get_execution(connection, scope, eid)
        node = await repo.get_node(connection, scope, eid, first.node_run_id)
    assert locked is not None and node is not None and node.node_id is NodeId.N02

    authority = FixtureHost()
    capabilities = await FixtureIntakeHost(authority, scope=scope).capabilities(scope)
    now = datetime.now(UTC)
    resolved = resolve_execution_pins(
        manager_for(store, authority),
        store,
        locked.task,
        scope=scope,
        host=capabilities,
        validation_at=now,
        limits=LIMITS,
    )
    selected = capabilities.model_deployments[0].identity
    service = LoopbackChat()
    service.deployment_version, service.model_version = (
        selected.deployment_version,
        selected.model_version,
    )
    server = await asyncio.start_server(service.handle, host="127.0.0.1", port=0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        endpoint = ChatEndpoint(
            endpoint_id="independent-chat-endpoint",
            endpoint_version="1.0.0",
            provider_id=selected.provider_id,
            provider_kind=selected.provider_kind,
            deployment_id=selected.deployment_id,
            model_id="independent-vision",
            url=f"http://127.0.0.1:{port}/v1/chat/completions",
            protocol="openai_chat_completions",
            allow_plaintext_loopback=True,
            limits=HttpLimits(
                timeout_ms=6000,
                io_timeout_ms=5500,
                request_bytes=4096,
                response_bytes=2048,
                image_bytes=1024,
                max_retry_after_seconds=3,
            ),
        )
        ownership = DeploymentOwnership(
            deployment=selected,
            owner_ref=TrustPin(
                identity="independent-owner",
                version="1.0.0",
                sha256=digest(b"independent provider authority"),
            ),
            endpoint_descriptor_ref=endpoint.descriptor_pin,
            model_id=endpoint.model_id,
            version_evidence=None,
        )
        active = DeploymentOwnershipRegistration(
            ownership=ownership,
            revoked_at=None,
            valid_from=now - timedelta(hours=1),
            valid_until=now + timedelta(hours=1),
        )
        state = CurrentRoutingState(capabilities=capabilities, ownership=(active,))
        binding = next(
            item.binding_ref
            for item in resolved.model_deployments
            if item.binding_ref.artifact_id == ModelCapability.PROFILE_DISCOVERY.value
        )
        policy = RoutePolicy.from_execution(resolved, binding, (ownership,))
        operation = RouteOperation(
            operation_id="independent-physical-group",
            execution_input_digest=resolved.pins.input_digest,
            node_run=NodeRunDescriptor(
                node_run_ref=NodeRunRef(
                    record_kind=RuntimeRecordKind.NODE_RUN, record_id=node.node_run_id
                ),
                execution_id=eid,
                node_id=node.node_id.value,
                status=node.status,
            ),
            binding_ref=binding,
            input_digest=node.input_digest,
            response_contract_digest=policy.binding.definition.output_schema_ref.digest,
            trace_id=node.trace.trace_id,
            purposes=(ModelCapability.PROFILE_DISCOVERY,),
            profile_refs=(),
        )
        budget = RouteBudget(
            node_remaining=12,
            operation_remaining=12,
            deployments=(
                DeploymentBudget(deployment_id=selected.deployment_id, remaining=12),
            ),
        )
        route = plan_primary(policy, operation, descriptor(attempt), state, budget, now)
        transport = HttpModelTransport(
            (endpoint,),
            (
                ChatContract(
                    route.binding.input_schema_ref,
                    route.binding.output_schema_ref,
                    Response,
                    response_schema=resolved.catalogue.resolve(
                        route.binding.output_schema_ref
                    ).canonical_bytes,
                ),
            ),
            max_concurrency=3,
        )
        journal = DispatchJournal(app)

        async def prepare_and_persist(key, prompt):
            per_input = operation.model_copy(
                update={
                    "operation_id": "operation." + digest(prompt.encode()),
                    "input_digest": digest(prompt.encode()),
                }
            )
            route = plan_primary(
                policy, per_input, descriptor(attempt), state, budget, datetime.now(UTC)
            )
            request = DispatchRequest(key, route, transport.prepare(route, prompt))
            assert await journal.begin(
                first, attempt.attempt_id, dispatch_intent(route, request)
            )
            assert not await journal.begin(
                first, attempt.attempt_id, dispatch_intent(route, request)
            )
            return request

        prompts = (
            ("independent throttled response", "rate_limit"),
            ("independent oversized response", "resource_exhausted"),
            ("independent duplicate response", "validation"),
            ("independent nonfinite response", "validation"),
            ("independent missing version response", "association"),
        )
        requests = []
        for i, (prompt, reason) in enumerate(prompts):
            request = await prepare_and_persist(f"wire-check-{i}", prompt)
            requests.append(request)
            wire = await transport.dispatch(request)
            result = project_http(request, wire, now)
            assert result.error is not None, (prompt, result)
            assert result.error.error_class == reason, (prompt, result.error)
            assert result.boundary == "acknowledged" and result.provider_request_id
            if i == 0:
                assert wire.retry_after_seconds == 3
            if i == 1:
                assert result.response_digest is None
            else:
                assert result.response_digest == digest(service.receipts[prompt])
            assert await journal.append_result(
                first, attempt.attempt_id, request.dispatch_id, result
            )
            prior.emit(
                "loopback_failure",
                prompt=prompt,
                reason=reason,
                request_id=result.provider_request_id,
                full_digest=result.response_digest,
            )

        failed_wire = await prepare_and_persist(
            "wire-retry-trigger", "independent retry trigger"
        )
        requests.append(failed_wire)
        unknown = ModelDispatchResult(
            boundary="sent_unknown",
            error=InvocationError(
                error_class="transport", code="probe_interrupted", diagnostic_refs=()
            ),
        )
        late = await prepare_and_persist("wire-late", "independent late response")
        requests.append(late)
        pending = asyncio.create_task(transport.dispatch(late))
        try:
            await asyncio.wait_for(service.late_started.wait(), 3)
            assert await journal.append_result(
                first, attempt.attempt_id, late.dispatch_id, unknown
            )
            response = await transport.dispatch(failed_wire)
            failed_observation = project_http(
                failed_wire,
                replace(
                    response,
                    error=InvocationError(
                        error_class="transport",
                        code="probe_response_rejected",
                        diagnostic_refs=(),
                    ),
                    parsed_content=None,
                ),
                now,
            )
            assert failed_observation.invocation is not None
            assert await journal.append_result(
                first, attempt.attempt_id, failed_wire.dispatch_id, failed_observation
            )
            terminal = FailedNodeOutput(
                schema_version="1.0.0",
                node_id=node.node_id,
                node_run_id=node.node_run_id,
                attempt_id=attempt.attempt_id,
                trace=node.trace,
                output_artifacts=(),
                started_at=attempt.started_at,
                finished_at=attempt.started_at,
                duration_ms=0,
                status="failed",
                error=NodeError(
                    code="probe_interrupted",
                    error_class="transport",
                    retryable=True,
                    message="Local probe interruption",
                    validation_issues=(),
                ),
            )
            assert (await jobs.complete(first, terminal)).value == "retry_scheduled"
            await asyncio.sleep(0.08)
            second = await jobs.claim(
                scope, "independent-model-second", 10_000, execution_id=eid
            )
            assert second is not None and second.fence > first.fence
            next_attempt = await jobs.start(second)
            assert next_attempt is not None
            async with app.connect() as connection:
                original = await repo.get_attempt(
                    connection, scope, eid, first.node_run_id, attempt.attempt_id
                )
            assert original is not None and original.status.value == "failed"
            newer = datetime.now(UTC)
            retry = plan_retry(
                policy,
                failed_wire.route.operation,
                descriptor(next_attempt),
                state,
                budget,
                newer,
                history=(
                    RecordedInvocation(
                        failed_wire.route.operation,
                        descriptor(original),
                        failed_observation.invocation,
                    ),
                ),
                retryable_errors=("transport",),
            )
            retry_request = DispatchRequest(
                "wire-retry-success",
                retry,
                transport.prepare(retry, "independent retry trigger"),
            )
            assert await journal.begin(
                second, next_attempt.attempt_id, dispatch_intent(retry, retry_request)
            )
            retry_result = project_http(
                retry_request, await transport.dispatch(retry_request), newer
            )
            assert (
                retry_result.invocation is not None
                and retry_result.invocation.outcome.status == "succeeded"
            )
            assert await journal.append_result(
                second, next_attempt.attempt_id, retry_request.dispatch_id, retry_result
            )
            service.late_continue.set()
            late_result = project_http(late, await pending, now)
            assert (
                late_result.invocation is not None
                and late_result.invocation.outcome.status == "succeeded"
            )
            try:
                await journal.append_result(
                    second,
                    next_attempt.attempt_id,
                    late.dispatch_id,
                    late_result,
                    late=True,
                )
            except DispatchConflict:
                pass
            else:
                raise AssertionError(
                    "a new Attempt appended to the original physical dispatch"
                )
            assert await journal.append_result(
                first, attempt.attempt_id, late.dispatch_id, late_result
            )
            assert not await journal.append_result(
                first, attempt.attempt_id, late.dispatch_id, late_result
            )
        finally:
            service.late_continue.set()
            if not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
        groups = {
            request.route.operation.operation_id: await journal.get_operation(
                scope, eid, first.node_run_id, request.route.operation.operation_id
            )
            for request in requests
        }
        assert len(groups[failed_wire.route.operation.operation_id]) == 2
        rows = tuple(row for group in groups.values() for row in group)
        assert len(rows) == 8
        assert sum(len(row.results) for row in rows) == 9
        late_stored = next(
            row for row in rows if row.intent.dispatch_id == late.dispatch_id
        )
        assert (
            late_stored.attempt_id == attempt.attempt_id
            and late_stored.results[-1].late
        )
        assert late_stored.results[-1].result.response_digest == digest(
            service.receipts["independent late response"]
        )
        assert (
            len(
                {
                    row.result.provider_request_id
                    for dispatch in rows
                    for row in dispatch.results
                    if isinstance(row.result, ModelDispatchResult)
                    and row.result.provider_request_id
                }
            )
            == 8
        )
        prior.emit(
            "partial_multi_dispatch_retry_and_late_ownership",
            intents=len(rows),
            receipts=9,
            actual_posts=len(service.requests),
            old_fence=first.fence,
            retry_fence=second.fence,
        )
        revoked = CurrentRoutingState(
            capabilities=capabilities,
            ownership=(active.model_copy(update={"revoked_at": newer}),),
        )
        try:
            plan_primary(
                policy, operation, descriptor(next_attempt), revoked, budget, newer
            )
        except RouteRejected as error:
            prior.emit(
                "pinned_current_ownership_revoked",
                code=error.code,
                sends=len(service.requests),
            )
        else:
            raise AssertionError(
                "dispatch authorized against a revoked current ownership record"
            )


async def main() -> None:
    with (
        isolated_postgres() as database,
        TemporaryDirectory(prefix="jimu-wp04-model-") as temp,
    ):
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        try:
            applied = await apply_migrations(owner, database.target)
            assert applied == tuple(range(1, len(applied) + 1)) and len(applied) >= 5
            assert await apply_migrations(owner, database.target) == ()
            async with app.connect() as connection:
                assert await connection.scalar(
                    text("SELECT NOT rolsuper FROM pg_roles WHERE rolname=current_user")
                )
            prior.emit(
                "isolated_postgres",
                image=POSTGRES_IMAGE,
                version=database.server_version,
                migrations=applied,
            )
            await run(app, Path(temp))
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
