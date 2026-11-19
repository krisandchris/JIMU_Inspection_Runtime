"""Isolated principals, scopes and input metadata for Release acceptance tests."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jimu_runtime.bundle.releases import (
    ReleaseAction,
    ReleaseManager,
    ReleaseTarget,
    ResolvedTaskScope,
    ScopeKind,
)
from jimu_runtime.bundle.store import (
    IdentifiedAdmission,
    LocalBundleStore,
)
from jimu_runtime.domain.definition_support import (
    VocabularyContentRef,
    VocabularyMemberRef,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.intake import TaskSubmission
from jimu_runtime.domain.runtime.releases import (
    ActivationRecord,
    ActivationScope,
    DeviceActivationScope,
    GlobalActivationScope,
    RegionActivationScope,
    ReleaseRecord,
    SceneActivationScope,
)

from ..store.support import ACTOR, PARAMETERS, raw_zip, verdict

NOW = datetime(2026, 12, 1, tzinfo=UTC)
GLOBAL = GlobalActivationScope(scope="global")
REGION = RegionActivationScope(scope="region", region_id="region-a")
DEVICE = DeviceActivationScope(scope="device", device_id="rover-01")
PRECEDENCE: tuple[ScopeKind, ...] = ("device", "region", "scene", "global")


def scene_scope(digest: str = "d" * 64) -> SceneActivationScope:
    return SceneActivationScope(
        scope="scene",
        scene_type=VocabularyMemberRef(
            vocabulary_ref=VocabularyContentRef(
                artifact_kind=ArtifactKind.VOCABULARY,
                artifact_id="scene-types",
                version="1.0.0",
                digest=digest,
            ),
            member="street",
        ),
    )


SCENE = scene_scope()
SUBMISSION = Path(__file__).parents[2] / "wp01c/intake/task-submission-valid.json"


@dataclass
class FixtureHost:
    registrar: object = field(default_factory=object)
    approver: object = field(default_factory=object)
    region_approver: object = field(default_factory=object)
    controller: object = field(default_factory=object)
    task_client: object = field(default_factory=object)
    override_client: object = field(default_factory=object)
    revoked: set[str] = field(default_factory=set)
    trusted_scope: ResolvedTaskScope | None = field(
        default_factory=lambda: ResolvedTaskScope(
            device_id=DEVICE.device_id,
            camera_id="front-camera",
            region_id=REGION.region_id,
            scene_type=SCENE.scene_type,
        )
    )

    def authorize(
        self, principal: object, action: ReleaseAction, target: ReleaseTarget
    ) -> ActorRef | None:
        policies = (
            (self.registrar, "registrar", (ReleaseAction.REGISTER,), GLOBAL),
            (self.approver, "approver", (ReleaseAction.APPROVE,), None),
            (self.region_approver, "region-approver", (ReleaseAction.APPROVE,), REGION),
            (
                self.controller,
                "controller",
                (ReleaseAction.ACTIVATE, ReleaseAction.ROLLBACK),
                None,
            ),
            (self.task_client, "task-client", (ReleaseAction.ACCEPT_TASK,), None),
            (
                self.override_client,
                "override-client",
                (ReleaseAction.ACCEPT_TASK, ReleaseAction.REQUEST_RELEASE),
                None,
            ),
        )
        for token, actor, actions, scope in policies:
            if (
                principal is token
                and action in actions
                and (scope is None or target.scope == scope)
            ):
                return ActorRef(
                    actor_id=actor,
                    actor_kind="human",
                    identity_registry_id="test-roles",
                )
        return None

    def resolve(
        self, principal: object, submission: TaskSubmission
    ) -> ResolvedTaskScope | None:
        if principal is not self.task_client and principal is not self.override_client:
            return None
        if submission.idempotency_scope.project_id != "demo":
            return None
        return self.trusted_scope

    def permit(
        self,
        release: ReleaseRecord,
        activation: ActivationRecord,
        scope: ResolvedTaskScope,
        at: datetime,
    ) -> bool:
        return release.bundle_ref.bundle_digest not in self.revoked

    def manager(
        self, store: LocalBundleStore, precedence: tuple[ScopeKind, ...] = PRECEDENCE
    ) -> ReleaseManager:
        return ReleaseManager(
            store,
            authorize=self.authorize,
            scope_precedence=precedence,
            resolve_task_scope=self.resolve,
            allow_new_task=self.permit,
            clock=lambda: NOW,
        )


def submission(
    request_id: str = "request-1", *, release: str | None = None
) -> TaskSubmission:
    original = TaskSubmission.model_validate_json(SUBMISSION.read_bytes())
    return original.model_copy(
        update={
            "request_id": request_id,
            "idempotency_key": request_id,
            "requested_release_id": release,
        }
    )


def store_receipt(
    store: LocalBundleStore, release: int, *, accepted: bool = True
) -> str:
    receipt_id = f"receipt-{release}"
    result = verdict(receipt_id, logical_root=f"{release:064x}", accepted=accepted)
    assert result.receipt is not None
    identified = IdentifiedAdmission(
        kind="identified",
        receipt=result.receipt.model_copy(update={"bundle_version": f"1.0.{release}"}),
    )
    store._admit(
        f"synthetic-{release}",
        raw_zip(),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: result.model_copy(update={"result": identified}),
    )
    return receipt_id


def register(
    manager: ReleaseManager, host: FixtureHost, number: int, receipt_id: str
) -> ReleaseRecord:
    return manager.register_release(
        operation_key=f"register-{number}",
        release_id=f"release-{number}",
        admission_receipt_id=receipt_id,
        principal=host.registrar,
    )


def approve(
    manager: ReleaseManager, host: FixtureHost, number: int, scope: ActivationScope
) -> ReleaseRecord:
    return manager.approve_release(
        operation_key=f"approve-{number}-{scope.scope}",
        release_id=f"release-{number}",
        scope=scope,
        principal=host.approver,
    )


def activate(
    manager: ReleaseManager,
    host: FixtureHost,
    number: int,
    scope: ActivationScope,
    *,
    expected: str | None = None,
) -> ActivationRecord:
    return manager.activate(
        operation_key=f"activate-{number}-{scope.scope}",
        release_id=f"release-{number}",
        scope=scope,
        activation_id=f"activation-{number}-{scope.scope}",
        expected_previous_activation_id=expected,
        principal=host.controller,
    )
