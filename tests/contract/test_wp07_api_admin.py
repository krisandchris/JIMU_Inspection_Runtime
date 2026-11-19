"""Authorize signed ZIP admission and separate Release operations against SQLite."""

import asyncio
import secrets
import threading
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.api import admin_routes
from jimu_runtime.api.admin_routes import bundle_admission_operation_id
from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Action, Identity, Resource
from jimu_runtime.api.boundary import ApiLimits
from jimu_runtime.api.ports import Administration
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.workflow.intake import IntakeService
from tests.fixtures.wp02.admission.support import LIMITS, build_scenario, sign_bundle
from tests.fixtures.wp02.releases.support import GLOBAL, FixtureHost, submission


def test_bundle_release_permissions_and_exact_activation_scope(tmp_path: Path) -> None:
    fixture = FixtureHost()
    store = LocalBundleStore(tmp_path / "releases")
    manager = fixture.manager(store)
    bundle = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, bundle.archive_bytes, bundle.bundle_ref.bundle_digest
    )
    scope = submission().idempotency_scope
    other = scope.model_copy(update={"client_id": "other-admission-client"})

    def credential(name, principal):
        return secrets.token_urlsafe(32), Identity(
            principal,
            ActorRef(
                actor_kind="human", actor_id=name, identity_registry_id="test-roles"
            ),
        )

    registrar = credential("registrar", fixture.registrar)
    approver = credential("approver", fixture.approver)
    controller = credential("controller", fixture.controller)
    edge = credential("task-client", fixture.task_client)
    identities = dict((registrar, approver, controller, edge))
    seen: list[Resource] = []

    class Authority:
        async def authenticate(self, token):
            return identities.get(token)

        async def allows(self, identity, target):
            seen.append(target)
            if target.scope == other:
                return identity == registrar[1] and target.action in {
                    Action.ACCESS_SCOPE,
                    Action.BUNDLE_ADMIT,
                }
            if target.scope != scope:
                return False
            if target.action is Action.ACCESS_SCOPE:
                return True
            if identity == registrar[1]:
                # Approve is intentionally passed through the API ACL to verify the
                # existing ReleaseManager's independent rejection at the ledger.
                return target.action in {
                    Action.BUNDLE_ADMIT,
                    Action.BUNDLE_READ,
                    Action.RELEASE_REGISTER,
                    Action.RELEASE_READ,
                    Action.RELEASE_APPROVE,
                }
            if target.action is Action.RELEASE_APPROVE and identity == approver[1]:
                return target.activation_scope == GLOBAL
            if target.action is Action.RELEASE_ACTIVATE and identity == controller[1]:
                return target.activation_scope == GLOBAL
            return False

    async def environment(identity, requested_scope):
        assert identity == registrar[1] and requested_scope in (scope, other)
        return signed.environment

    async def run() -> None:
        app = create_app(
            Mock(spec=AsyncEngine),
            Mock(spec=IntakeService),
            authority=Authority(),
            administration=Administration(store, manager, LIMITS, environment),
        )
        schema = app.openapi()
        assert "/api/v1/releases/{release_id}/approve" in schema["paths"]
        assert "/api/v1/releases/{release_id}/accept" not in schema["paths"]
        assert "/api/v1/releases/{release_id}/deprecate" not in schema["paths"]
        assert "/api/v1/bundles/{bundle_id}/validate" not in schema["paths"]

        def headers(token: str) -> dict[str, str]:
            return {
                "Authorization": "Bearer " + token,
                "X-Jimu-Project": scope.project_id,
                "X-Jimu-Client": scope.client_id,
            }

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1/api/v1/",
            transport=httpx.ASGITransport(app=app),
        ) as client:
            operation = {
                "Content-Type": "application/zip",
                "Idempotency-Key": "api-zip-1",
            }
            denied = await client.post(
                "bundles",
                headers={**headers(edge[0]), **operation},
                content=signed.archive,
            )
            assert denied.status_code == 403
            assert (
                store.get_admission(bundle_admission_operation_id(scope, "api-zip-1"))
                is None
            )
            admitted = await client.post(
                "bundles",
                headers={**headers(registrar[0]), **operation},
                content=signed.archive,
            )
            assert admitted.status_code == 200, admitted.text
            persisted = store.get_admission(
                bundle_admission_operation_id(scope, "api-zip-1")
            )
            assert persisted is not None and persisted.accepted
            assert persisted.receipt is not None
            other_admission = await client.post(
                "bundles",
                headers={
                    **headers(registrar[0]),
                    **operation,
                    "X-Jimu-Client": other.client_id,
                },
                content=signed.archive,
            )
            assert other_admission.status_code == 200, other_admission.text
            other_persisted = store.get_admission(
                bundle_admission_operation_id(other, "api-zip-1")
            )
            assert other_persisted is not None and other_persisted.accepted
            assert other_persisted.operation_id != persisted.operation_id
            receipt, bundle_id = (
                persisted.receipt.receipt_id,
                persisted.receipt.bundle_id,
            )
            lookup = await client.get(
                f"bundles/{bundle_id}",
                headers=headers(registrar[0]),
                params={"receipt_id": receipt},
            )
            assert lookup.json() == admitted.json()
            wrong_bundle = await client.get(
                "bundles/other",
                headers=headers(registrar[0]),
                params={"receipt_id": receipt},
            )
            assert wrong_bundle.status_code == 404
            revoked = await client.get(
                f"bundles/{bundle_id}",
                headers=headers(edge[0]),
                params={"receipt_id": receipt},
            )
            assert revoked.status_code == 404

            register = await client.post(
                "releases",
                headers=headers(registrar[0]),
                json={
                    "schema_version": "1.0.0",
                    "operation_key": "api-register",
                    "release_id": "api-release",
                    "admission_receipt_id": receipt,
                },
            )
            assert register.status_code == 200, register.text
            result = await client.get(
                "releases/api-release", headers=headers(registrar[0])
            )
            assert result.json() == register.json()

            body = {
                "schema_version": "1.0.0",
                "operation_key": "api-approval",
                "scope": {"scope": "global"},
            }
            path = "releases/api-release/approve"
            assert (
                await client.post(path, headers=headers(registrar[0]), json=body)
            ).status_code == 403
            assert (
                await client.post(
                    path,
                    headers=headers(approver[0]),
                    json={
                        **body,
                        "scope": {"scope": "device", "device_id": "other"},
                    },
                )
            ).status_code == 403
            approved = await client.post(path, headers=headers(approver[0]), json=body)
            assert approved.status_code == 200, approved.text
            assert any(target.activation_scope == GLOBAL for target in seen)

            body = {
                **body,
                "operation_key": "api-activate",
                "activation_id": "api-active",
                "expected_previous_activation_id": None,
            }
            activated = await client.post(
                "releases/api-release/activate",
                headers=headers(controller[0]),
                json=body,
            )
            assert activated.status_code == 200, activated.text
            active = manager.active_activation(GLOBAL)
            assert active is not None and active.activation_id == "api-active"
            stale = await client.post(
                "releases/api-release/activate",
                headers=headers(controller[0]),
                json={
                    **body,
                    "operation_key": "api-stale",
                    "activation_id": "api-other",
                },
            )
            assert stale.status_code == 409, stale.text
            assert manager.active_activation(GLOBAL) == active

    asyncio.run(run())


def test_zip_quota_survives_request_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FixtureHost()
    store = LocalBundleStore(tmp_path / "releases")
    manager = fixture.manager(store)
    bundle = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, bundle.archive_bytes, bundle.bundle_ref.bundle_digest
    )
    scope = submission().idempotency_scope
    token = secrets.token_urlsafe(32)
    actor = ActorRef(
        actor_kind="human", actor_id="registrar", identity_registry_id="test-roles"
    )

    class Authority:
        async def authenticate(self, credential):
            return Identity(fixture.registrar, actor) if credential == token else None

        async def allows(self, identity, target):
            return target.scope == scope and target.action in {
                Action.BUNDLE_ADMIT,
                Action.ACCESS_SCOPE,
            }

    async def environment(identity, requested_scope):
        return signed.environment

    async def run() -> None:
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        allow_finish = threading.Event()
        admit = admin_routes.admit_bundle

        def slow_admission(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert allow_finish.wait(10)
            return admit(*args, **kwargs)

        monkeypatch.setattr(admin_routes, "admit_bundle", slow_admission)
        app = create_app(
            Mock(spec=AsyncEngine),
            Mock(spec=IntakeService),
            authority=Authority(),
            administration=Administration(store, manager, LIMITS, environment),
            limits=ApiLimits(admission_concurrency=1, body_timeout_seconds=0.05),
        )
        headers = {
            "Authorization": "Bearer " + token,
            "X-Jimu-Project": scope.project_id,
            "X-Jimu-Client": scope.client_id,
            "Content-Type": "application/zip",
        }
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1/api/v1/",
            transport=httpx.ASGITransport(app=app),
            headers=headers,
        ) as client:

            async def stalled():
                yield b"P"
                await asyncio.Event().wait()

            timed_out = await client.post(
                "bundles", headers={"Idempotency-Key": "stalled"}, content=stalled()
            )
            assert timed_out.status_code == 408
            assert (
                store.get_admission(bundle_admission_operation_id(scope, "stalled"))
                is None
            )

            first = asyncio.create_task(
                client.post(
                    "bundles",
                    headers={"Idempotency-Key": "first"},
                    content=signed.archive,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first

                async def should_not_read():
                    pytest.fail("ZIP was buffered before acquiring admission capacity")
                    yield signed.archive

                denied = await client.post(
                    "bundles",
                    headers={"Idempotency-Key": "second"},
                    content=should_not_read(),
                )
                assert denied.status_code == 503
                assert denied.json()["code"] == "bundle_admission_busy"
            finally:
                allow_finish.set()

            async with asyncio.timeout(10):
                while True:
                    retry = await client.post(
                        "bundles",
                        headers={"Idempotency-Key": "second"},
                        content=signed.archive,
                    )
                    if retry.status_code == 200:
                        break
                    assert retry.status_code == 503
                    await asyncio.sleep(0.02)
            assert store.get_admission(
                bundle_admission_operation_id(scope, "first")
            ).accepted
            assert store.get_admission(
                bundle_admission_operation_id(scope, "second")
            ).accepted

    asyncio.run(run())
