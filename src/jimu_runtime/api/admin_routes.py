"""Administrative routes with both host ACL and ReleaseManager authorization."""

from asyncio import Semaphore, Task, create_task, shield, to_thread
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from jimu_runtime.api.auth import Access, Action, Resource
from jimu_runtime.api.boundary import ApiError, HttpBoundary, read_body, read_json
from jimu_runtime.api.contracts import ActivateRelease, ApproveRelease, RegisterRelease
from jimu_runtime.api.ports import Administration
from jimu_runtime.api.route_support import request_schema, required_key
from jimu_runtime.bundle.admission import admit_bundle
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ReleaseOperationError
from jimu_runtime.bundle.store import StoredAdmission
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.releases import ActivationRecord, ReleaseRecord

_EXPECTED_RELEASE_ERRORS = frozenset(
    (
        "accepted_admission_required",
        "release_id_conflict",
        "release_scope_already_approved",
        "release_scope_not_approved",
        "stale_activation_predecessor",
        "release_already_active",
        "activation_id_conflict",
        "release_operation_conflict",
        "operation_key_conflict",
    )
)


async def _release_call[T](operation: Callable[..., T], **arguments: object) -> T:
    try:
        return await to_thread(operation, **arguments)
    except ReleaseOperationError as error:
        if error.code == "release_not_found":
            raise ApiError(404, "resource_not_found") from None
        if error.code == "unauthorized_release_action":
            raise ApiError(403, "access_denied") from None
        if error.code in _EXPECTED_RELEASE_ERRORS:
            raise ApiError(409, error.code) from None
        raise


def bundle_admission_operation_id(scope: IdempotencyScope, key: str) -> str:
    """Translate a transport key into the shared ledger's scoped namespace."""
    preimage = scope.stable_json().encode("utf-8") + b"\x00" + key.encode("ascii")
    return "bundle-admit." + sha256_digest(preimage)


def mount_administration(
    router: APIRouter,
    boundary: HttpBoundary,
    admin: Administration,
) -> None:
    authenticated = Depends(boundary.access)
    admission_slots = Semaphore(boundary.limits.admission_concurrency)
    pending: set[Task[StoredAdmission]] = set()

    def release_admission(job: Task[StoredAdmission]) -> None:
        pending.discard(job)
        admission_slots.release()
        if not job.cancelled():
            job.exception()

    @router.post(
        "/bundles",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/zip": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    async def submit_bundle(
        request: Request, access: Access = authenticated
    ) -> StoredAdmission:
        operation = required_key(request, record_id=True)
        await boundary.require(
            access,
            Resource(
                action=Action.BUNDLE_ADMIT, scope=access.scope, resource_id=operation
            ),
        )
        if admission_slots.locked():
            raise ApiError(503, "bundle_admission_busy")
        await admission_slots.acquire()
        try:
            raw = await read_body(
                request,
                media_type="application/zip",
                limit=admin.bundle_limits.max_archive_bytes,
                deadline_seconds=boundary.limits.body_timeout_seconds,
            )
            environment = await admin.admission_environment(
                access.identity, access.scope
            )
            operation_id = bundle_admission_operation_id(access.scope, operation)
            job = create_task(
                to_thread(
                    admit_bundle,
                    raw,
                    operation_id=operation_id,
                    actor=access.identity.actor,
                    limits=admin.bundle_limits,
                    environment=environment,
                    store=admin.bundles,
                )
            )
        except BaseException:
            admission_slots.release()
            raise
        pending.add(job)
        job.add_done_callback(release_admission)
        return await shield(job)

    @router.get("/bundles/{bundle_id}")
    async def get_bundle(
        bundle_id: RecordId,
        receipt_id: Annotated[RecordId, Query()],
        access: Access = authenticated,
    ) -> StoredAdmission:
        await boundary.require(
            access,
            Resource(
                action=Action.BUNDLE_READ,
                scope=access.scope,
                resource_id=bundle_id,
                related_id=receipt_id,
            ),
            conceal=True,
        )
        record = await to_thread(admin.bundles.get_receipt, receipt_id)
        if (
            record is None
            or record.receipt is None
            or record.receipt.bundle_id != bundle_id
        ):
            raise ApiError(404, "resource_not_found")
        return record

    @router.post("/releases", openapi_extra=request_schema(RegisterRelease))
    async def register_release(
        request: Request, access: Access = authenticated
    ) -> ReleaseRecord:
        payload = await read_json(request, RegisterRelease, boundary.limits)
        await boundary.require(
            access,
            Resource(
                action=Action.RELEASE_REGISTER,
                scope=access.scope,
                resource_id=payload.release_id,
                related_id=payload.admission_receipt_id,
            ),
        )
        return await _release_call(
            admin.releases.register_release,
            operation_key=payload.operation_key,
            release_id=payload.release_id,
            admission_receipt_id=payload.admission_receipt_id,
            principal=access.identity.principal,
        )

    @router.get("/releases/{release_id}")
    async def get_release(
        release_id: RecordId, access: Access = authenticated
    ) -> ReleaseRecord:
        await boundary.require(
            access,
            Resource(
                action=Action.RELEASE_READ, scope=access.scope, resource_id=release_id
            ),
            conceal=True,
        )
        release = await to_thread(admin.releases.get_release, release_id)
        if release is None:
            raise ApiError(404, "resource_not_found")
        return release

    @router.post(
        "/releases/{release_id}/approve", openapi_extra=request_schema(ApproveRelease)
    )
    async def approve_release(
        release_id: RecordId,
        request: Request,
        access: Access = authenticated,
    ) -> ReleaseRecord:
        payload = await read_json(request, ApproveRelease, boundary.limits)
        await boundary.require(
            access,
            Resource(
                action=Action.RELEASE_APPROVE,
                scope=access.scope,
                resource_id=release_id,
                activation_scope=payload.scope,
            ),
        )
        return await _release_call(
            admin.releases.approve_release,
            operation_key=payload.operation_key,
            release_id=release_id,
            scope=payload.scope,
            principal=access.identity.principal,
        )

    @router.post(
        "/releases/{release_id}/activate", openapi_extra=request_schema(ActivateRelease)
    )
    async def activate_release(
        release_id: RecordId,
        request: Request,
        access: Access = authenticated,
    ) -> ActivationRecord:
        payload = await read_json(request, ActivateRelease, boundary.limits)
        await boundary.require(
            access,
            Resource(
                action=Action.RELEASE_ACTIVATE,
                scope=access.scope,
                resource_id=release_id,
                activation_scope=payload.scope,
            ),
        )
        return await _release_call(
            admin.releases.activate,
            operation_key=payload.operation_key,
            release_id=release_id,
            scope=payload.scope,
            activation_id=payload.activation_id,
            expected_previous_activation_id=payload.expected_previous_activation_id,
            principal=access.identity.principal,
        )
