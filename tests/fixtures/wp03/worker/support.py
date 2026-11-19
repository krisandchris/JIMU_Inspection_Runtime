"""Worker test host and an admitted parking fixture with real scoped intake."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.releases import ReleaseManager, ResolvedTaskScope
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    RegistryRecordRef,
    TaskSubmission,
)
from jimu_runtime.domain.values import RecordValue, StringValue
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeResult,
    IntakeService,
)
from jimu_runtime.workflow.scheduling import JobBudget, SchedulingLimits
from jimu_runtime.workflow.worker import WorkflowWorker
from jimu_runtime.workflow.worker_source import SyntheticScenario, WorkerSource
from tests.fixtures.wp02.compiler.scenario import LIMITS, capability_snapshot
from tests.fixtures.wp02.releases.support import FixtureHost
from tests.fixtures.wp03.intake.support import (
    IDENTITY,
    FixtureIntakeHost,
    manager_for,
    submission,
)
from tests.fixtures.wp03.nodes.scenario import REGION, parking_source
from tests.fixtures.wp03.pins.support import activate_scenario

SCHEDULING = SchedulingLimits(
    schema_version="1.0.0",
    version="1.0.0",
    default=JobBudget(
        max_attempts=2,
        timeout_ms=10_000,
        retry_backoff_ms=(25,),
        retryable_error_classes=("transport",),
        retry_on_lease_loss=True,
    ),
    overrides=(),
)


def worker_submission(
    request: str = "worker-1", *, empty_context: bool = False
) -> TaskSubmission:
    original = submission(request)
    context = original.context.model_copy(
        update={
            "region_id": REGION,
            "metadata": RecordValue(
                kind="record",
                value={}
                if empty_context
                else {"zone": StringValue(kind="string", value="fixture-parking")},
            ),
        }
    )
    return TaskSubmission.model_validate(
        original.model_copy(update={"context": context})
    )


@dataclass
class FixtureWorkerHost(FixtureIntakeHost):
    scenario: SyntheticScenario = SyntheticScenario()

    async def authorize_submission(
        self, principal: object, original: TaskSubmission
    ) -> IntakeIdentity:
        trusted = worker_submission().context
        if (
            principal is not self.release_host.task_client
            or original.idempotency_scope != self.scope
            or (
                original.context.device_id,
                original.context.camera_id,
                original.context.region_id,
            )
            != (trusted.device_id, trusted.camera_id, trusted.region_id)
        ):
            raise IntakeAuthorizationError()
        return IDENTITY

    async def worker_source(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> WorkerSource:
        media = await self.verified_original(scope, task.media_ref)
        deployment = capability_snapshot().model_deployments[0].identity.deployment_id
        return WorkerSource(
            media,
            RegistryRecordRef(
                record_id=task.context.device_id,
                version="1.0.0",
                digest=sha256_digest(b"device"),
            ),
            RegistryRecordRef(
                record_id=task.context.camera_id,
                version="1.0.0",
                digest=sha256_digest(b"camera"),
            ),
            deployment,
            self.scenario,
        )


def open_worker_fixture(
    path: Path, host: FixtureWorkerHost | None = None
) -> tuple[LocalBundleStore, ReleaseManager, FixtureWorkerHost]:
    store = LocalBundleStore(path / "ledger")
    chosen = FixtureWorkerHost(FixtureHost()) if host is None else host
    return store, manager_for(store, chosen.release_host), chosen


async def accepted_worker(
    engine: AsyncEngine,
    path: Path,
    *,
    second_profile: bool = False,
    excluded: bool = False,
    second_question: bool = False,
    empty_context: bool = False,
    scheduling: SchedulingLimits = SCHEDULING,
) -> tuple[WorkflowWorker, IntakeResult, FixtureWorkerHost]:
    store, manager, host = open_worker_fixture(path)
    source = parking_source(
        second_profile=second_profile,
        excluded=excluded,
        second_question=second_question,
    )
    region = next(item for item in source.artifacts if item.kind is ArtifactKind.REGION)
    assert region.artifact_id == REGION
    activate_scenario(
        path / "build",
        store,
        manager,
        host.release_host,
        source,
        capability_snapshot(),
        number=1,
    )
    release = manager.get_release("release-1")
    assert release is not None
    host.release_host.trusted_scope = ResolvedTaskScope(
        device_id="rover-01",
        camera_id="front-camera",
        region_id=REGION,
        scene_type=host.release_host.trusted_scope.scene_type,
    )
    service = IntakeService(
        engine,
        manager,
        store,
        host=host,
        bundle_limits=LIMITS,
        scheduling_limits=scheduling,
        max_submission_bytes=64 * 1024,
    )
    accepted = await service.submit(
        worker_submission(empty_context=empty_context),
        principal=host.release_host.task_client,
    )
    return (
        WorkflowWorker(engine, manager, store, host=host, bundle_limits=LIMITS),
        accepted,
        host,
    )
