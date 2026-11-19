"""Narrow interfaces bound to separately composed bundle, replay and evidence services."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from jimu_runtime.api.auth import Identity
from jimu_runtime.api.contracts import EvidenceCoordinates
from jimu_runtime.bundle.admission import AdmissionEnvironment
from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.replay import (
    ArtifactReplayRequest,
    ArtifactReplayRun,
    InferenceReplayRequest,
    InferenceReplayRun,
)


@dataclass(frozen=True, slots=True)
class Administration:
    bundles: LocalBundleStore
    releases: ReleaseManager
    bundle_limits: BundleLimits
    admission_environment: Callable[
        [Identity, IdempotencyScope], Awaitable[AdmissionEnvironment]
    ]


class InferenceReplayPort(Protocol):
    async def submit(
        self,
        scope: IdempotencyScope,
        request: InferenceReplayRequest,
        *,
        idempotency_key: str,
        principal: object,
    ) -> InferenceReplayRun: ...


class ArtifactReplayPort(Protocol):
    async def run(
        self,
        scope: IdempotencyScope,
        idempotency_key: str,
        request: ArtifactReplayRequest,
    ) -> ArtifactReplayRun: ...


@dataclass(frozen=True, slots=True)
class EvidenceMaterial:
    status: str
    mime_type: str | None = None
    content: bytes | None = field(default=None, repr=False)


class EvidencePort(Protocol):
    async def read_asset(
        self,
        identity: Identity,
        scope: IdempotencyScope,
        task_id: str,
        locator: EvidenceCoordinates,
        *,
        max_bytes: int,
    ) -> EvidenceMaterial: ...

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
    ) -> EvidenceMaterial: ...
