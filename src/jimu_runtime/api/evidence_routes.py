"""Read exact evidence with proof and independent material permissions."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import TypeAdapter

from jimu_runtime.api.auth import Access, Action, Resource
from jimu_runtime.api.boundary import ApiError
from jimu_runtime.api.contracts import EvidenceCoordinates
from jimu_runtime.api.ports import EvidencePort
from jimu_runtime.api.route_support import Routing
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.runtime.evidence import EvidenceMimeType

_MIME = TypeAdapter[str](EvidenceMimeType)


def mount_evidence(router: APIRouter, routes: Routing, reader: EvidencePort) -> None:
    authenticated = Depends(routes.boundary.access)

    @router.get(
        "/evidence/{evidence_id}",
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                }
            }
        },
    )
    async def read_evidence(
        evidence_id: RecordId,
        execution_id: Annotated[RecordId, Query()],
        digest: Annotated[Digest, Query()],
        representation: Literal["evidence", "normalized", "raw"] = "evidence",
        node_run_id: Annotated[RecordId | None, Query()] = None,
        attempt_id: Annotated[RecordId | None, Query()] = None,
        late: bool = False,
        access: Access = authenticated,
    ) -> Response:
        action = {
            "evidence": Action.EVIDENCE_READ,
            "normalized": Action.EVIDENCE_NORMALIZED,
            "raw": Action.EVIDENCE_RAW,
        }[representation]
        owner = await routes.execution(
            access, execution_id, action, resource_id=evidence_id
        )
        max_bytes = routes.boundary.limits.evidence_bytes
        if representation == "evidence":
            if node_run_id is None or attempt_id is None or late:
                raise ApiError(422, "invalid_evidence_locator")
            material = await reader.read_asset(
                access.identity,
                access.scope,
                owner.task.task_id,
                EvidenceCoordinates(
                    execution_id=execution_id,
                    node_run_id=node_run_id,
                    attempt_id=attempt_id,
                    evidence_id=evidence_id,
                    output_digest=digest,
                ),
                max_bytes=max_bytes,
            )
        else:
            if node_run_id is not None or attempt_id is not None:
                raise ApiError(422, "invalid_evidence_locator")
            if late:
                await routes.boundary.require(
                    access,
                    Resource(
                        action=Action.EVIDENCE_LATE,
                        scope=access.scope,
                        task_id=owner.task.task_id,
                        execution_id=execution_id,
                        resource_id=evidence_id,
                    ),
                    conceal=True,
                )
            material = await reader.read_audit(
                access.identity,
                access.scope,
                owner.task.task_id,
                execution_id,
                evidence_id,
                late=late,
                role=representation,
                expected_digest=digest,
                max_bytes=max_bytes,
            )
        if material.status == "denied":
            raise ApiError(404, "resource_not_found")
        if material.status == "too_large":
            raise ApiError(413, "evidence_response_too_large")
        if material.status in {"expired", "disposition_pending"}:
            raise ApiError(410, "evidence_expired")
        if material.status in {"missing", "not_retained", "unmanaged"}:
            raise ApiError(404, "evidence_unavailable")
        if (
            material.status != ("late" if late else "available")
            or material.content is None
            or material.mime_type is None
        ):
            raise ApiError(500, "evidence_verification_failed")
        if len(material.content) > max_bytes:
            raise ApiError(500, "evidence_verification_failed")
        if representation != "evidence" and sha256_digest(material.content) != digest:
            raise ApiError(404, "resource_not_found")
        media_type = _MIME.validate_python(material.mime_type)
        return Response(
            material.content,
            media_type=media_type,
            headers={"Content-Disposition": "attachment"},
        )
