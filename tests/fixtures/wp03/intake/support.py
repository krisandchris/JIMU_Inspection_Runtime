"""Trusted synthetic intake authority and an owned PostgreSQL test session."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from multiprocessing.synchronize import Event
from pathlib import Path

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.runtime.intake import IdempotencyScope, TaskSubmission
from jimu_runtime.domain.runtime.media import (
    FinalizedAsset,
    ImageDimensions,
    MediaAsset,
    MediaRef,
)
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeHostUnavailable,
    IntakeIdentity,
    IntakeService,
)
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.scheduling import JobBudget, SchedulingLimits
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    capability_snapshot,
    source_scenario,
)
from tests.fixtures.wp02.releases.support import (
    GLOBAL,
    PRECEDENCE,
    FixtureHost,
)
from tests.fixtures.wp02.releases.support import (
    submission as release_submission,
)
from tests.fixtures.wp03.pins.support import activate_scenario
from tests.fixtures.wp03.postgres.support import isolated_postgres

SCHEDULING = SchedulingLimits(
    schema_version="1.0.0",
    version="1.0.0",
    default=JobBudget(
        max_attempts=2,
        timeout_ms=500,
        retry_backoff_ms=(200,),
        retryable_error_classes=("transport",),
        retry_on_lease_loss=True,
    ),
    overrides=(),
)
ORIGINAL_METADATA = Path(__file__).parents[2] / "wp01c/intake/media-asset-valid.json"
ORIGINAL_IMAGE = b"P6\n2 1\n255\n\x20\x40\x60\x80\xa0\xc0"


def submission(request_id: str = "request-1") -> TaskSubmission:
    original = release_submission(request_id)
    media_ref = MediaRef(media_id="frame-1", sha256=sha256_digest(ORIGINAL_IMAGE))
    return original.model_copy(update={"media_ref": media_ref})


IDENTITY = IntakeIdentity(
    issuer="https://fixture.example/registry", subject="task-client"
)


@dataclass
class FixtureIntakeHost:
    release_host: FixtureHost
    scope: IdempotencyScope = submission().idempotency_scope
    restored_identity: IntakeIdentity = IDENTITY
    restored_unavailable: bool = False
    denied: bool = False
    media_calls: int = 0
    capabilities_calls: int = 0

    async def authorize_submission(
        self, principal: object, original: TaskSubmission
    ) -> IntakeIdentity:
        want = submission()
        context = original.context
        if (
            principal is not self.release_host.task_client
            or self.denied
            or original.idempotency_scope != self.scope
            or (context.device_id, context.camera_id, context.region_id)
            != (want.context.device_id, want.context.camera_id, want.context.region_id)
        ):
            raise IntakeAuthorizationError()
        return self.restored_identity

    async def restore_principal(
        self, identity: IntakeIdentity, original: TaskSubmission
    ) -> object:
        if self.restored_unavailable:
            raise IntakeHostUnavailable()
        if identity != IDENTITY or original.idempotency_scope != self.scope:
            raise IntakeAuthorizationError()
        return self.release_host.task_client

    async def verified_original(
        self, scope: IdempotencyScope, media_ref: MediaRef
    ) -> MediaAsset:
        self.media_calls += 1
        digest = sha256_digest(ORIGINAL_IMAGE)
        if (
            scope != self.scope
            or media_ref.media_id != "frame-1"
            or media_ref.sha256 != digest
        ):
            raise IntakeAuthorizationError()
        fixture = MediaAsset.model_validate_json(ORIGINAL_METADATA.read_bytes())
        assert isinstance(fixture.stage, FinalizedAsset)
        asset = fixture.model_copy(
            update={
                "media_ref": media_ref,
                "storage": fixture.storage.model_copy(
                    update={"object_key": "media/frame-1.ppm"}
                ),
                "stage": fixture.stage.model_copy(update={"verified_sha256": digest}),
                "mime_type": "image/x-portable-pixmap",
                "dimensions": ImageDimensions(width=2, height=1),
                "byte_size": len(ORIGINAL_IMAGE),
            }
        )
        return MediaAsset.model_validate(asset)

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
        self.capabilities_calls += 1
        if scope != self.scope:
            raise IntakeAuthorizationError()
        original = capability_snapshot()
        # The published compiler/approval fixture is historical. Only the new
        # trusted host's registration validity window is extended at intake.
        validity = {
            "valid_from": datetime(2025, 1, 1, tzinfo=UTC),
            "valid_until": datetime(2100, 1, 1, tzinfo=UTC),
        }
        return original.model_copy(
            update={
                **validity,
                "capabilities": tuple(
                    row.model_copy(update=validity) for row in original.capabilities
                ),
                "model_deployments": tuple(
                    row.model_copy(update=validity)
                    for row in original.model_deployments
                ),
                "runtime_protocols": tuple(
                    row.model_copy(update=validity)
                    for row in original.runtime_protocols
                ),
            }
        )


def manager_for(
    store: LocalBundleStore,
    host: FixtureHost,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ReleaseManager:
    return ReleaseManager(
        store,
        authorize=host.authorize,
        scope_precedence=PRECEDENCE,
        resolve_task_scope=host.resolve,
        allow_new_task=host.permit,
        clock=clock or (lambda: datetime(2026, 11, 17, 4, tzinfo=UTC)),
    )


def publish(
    work_dir: Path,
    store: LocalBundleStore,
    manager: ReleaseManager,
    host: FixtureHost,
    number: int,
) -> None:
    source = source_scenario()
    previous = manager.active_activation(GLOBAL)
    activate_scenario(
        work_dir,
        store,
        manager,
        host,
        replace(
            source, metadata=replace(source.metadata, bundle_version=f"1.0.{number}")
        ),
        capability_snapshot(),
        number=number,
        expected=previous.activation_id if previous is not None else None,
    )


@asynccontextmanager
async def migrated_application() -> AsyncIterator[AsyncEngine]:
    with isolated_postgres() as database:
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        try:
            assert database.server_version.startswith("16.")
            await apply_migrations(owner, database.target)
            yield app
        finally:
            await app.dispose()
            await owner.dispose()


def make_service(
    engine: AsyncEngine,
    store: LocalBundleStore,
    manager: ReleaseManager,
    host: FixtureIntakeHost,
) -> IntakeService:
    return IntakeService(
        engine,
        manager,
        store,
        host=host,
        bundle_limits=LIMITS,
        scheduling_limits=SCHEDULING,
        max_submission_bytes=64 * 1024,
    )


def submit_until_release_pin(
    db: URL, ledger: Path, request_id: str, pinned: Event
) -> None:
    """Spawn target that exposes the post-pin, pre-finalization kill point."""
    store = LocalBundleStore(ledger)
    release_host = FixtureHost()
    manager = manager_for(store, release_host)
    original = submission(request_id)

    class InterruptedHost(FixtureIntakeHost):
        async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot:
            # Intake asks for capabilities only after the ledger accepted the
            # task and the independent PostgreSQL read confirmed it is pending.
            pinned.set()
            await asyncio.Event().wait()
            raise AssertionError("the fixture must terminate this child process")

    async def run() -> None:
        engine = create_postgres_engine(db)
        try:
            service = make_service(
                engine, store, manager, InterruptedHost(release_host)
            )
            await service.submit(original, principal=release_host.task_client)
        finally:
            await engine.dispose()

    asyncio.run(run())
