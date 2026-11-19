"""Adapt the WP-06 evidence lifecycle to the HTTP material port."""

from collections.abc import Callable
from typing import Literal, Protocol

from jimu_runtime.api.auth import Identity
from jimu_runtime.api.contracts import EvidenceCoordinates
from jimu_runtime.api.ports import EvidenceMaterial
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.intake import IdempotencyScope


class AssetRead(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def asset(self) -> EvidenceAsset | None: ...

    @property
    def content(self) -> bytes | None: ...


class AuditRead(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def content(self) -> bytes | None: ...


class LifecycleReader[Grant, Locator](Protocol):
    async def read_evidence(
        self, grant: Grant, locator: Locator, *, max_bytes: int
    ) -> AssetRead: ...

    async def read_model_audit(
        self,
        grant: Grant,
        execution_id: str,
        dispatch_id: str,
        *,
        late: bool,
        role: Literal["raw", "normalized"],
        expected_digest: str,
        max_bytes: int,
    ) -> AuditRead: ...


class LifecycleEvidence[Grant, Locator]:
    """Build lifecycle grants from authenticated host context, never wire data."""

    def __init__(
        self,
        service: LifecycleReader[Grant, Locator],
        grant: Callable[[IdempotencyScope, ActorRef, str], Grant],
        locator: Callable[[EvidenceCoordinates], Locator],
    ) -> None:
        self._service = service
        self._grant = grant
        self._locator = locator

    async def read_asset(
        self,
        identity: Identity,
        scope: IdempotencyScope,
        task_id: str,
        locator: EvidenceCoordinates,
        *,
        max_bytes: int,
    ) -> EvidenceMaterial:
        read = await self._service.read_evidence(
            self._grant(scope, identity.actor, task_id),
            self._locator(locator),
            max_bytes=max_bytes,
        )
        return EvidenceMaterial(
            status=read.status,
            mime_type=None if read.asset is None else read.asset.mime_type,
            content=read.content,
        )

    async def read_audit(
        self,
        identity: Identity,
        scope: IdempotencyScope,
        task_id: str,
        execution_id: str,
        dispatch_id: str,
        *,
        late: bool,
        role: Literal["raw", "normalized"],
        expected_digest: str,
        max_bytes: int,
    ) -> EvidenceMaterial:
        read = await self._service.read_model_audit(
            self._grant(scope, identity.actor, task_id),
            execution_id,
            dispatch_id,
            late=late,
            role=role,
            expected_digest=expected_digest,
            max_bytes=max_bytes,
        )
        return EvidenceMaterial(
            status=read.status,
            mime_type="application/octet-stream"
            if role == "raw"
            else "application/json",
            content=read.content,
        )
