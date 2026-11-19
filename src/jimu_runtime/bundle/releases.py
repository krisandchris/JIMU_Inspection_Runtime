"""Local Release approval, activation and immutable Task pinning."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.store import LocalBundleStore, ReleaseLedger
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import VocabularyMemberRef
from jimu_runtime.domain.enums import RuntimeRecordKind, TaskStatus
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.bundle_manifest import BundleRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    TaskSubmission,
)
from jimu_runtime.domain.runtime.releases import (
    ActivationRecord,
    ActivationRef,
    ActivationScope,
    ApprovalReceipt,
    DeviceActivationScope,
    GlobalActivationScope,
    RegionActivationScope,
    ReleaseRecord,
    ReleaseRef,
    ReleaseStatus,
    SceneActivationScope,
)

type ScopeKind = Literal["device", "region", "scene", "global"]
_SCOPE_KINDS = frozenset(("device", "region", "scene", "global"))
_RECORD_ID = TypeAdapter[RecordId](RecordId)
_ACTIVATION_SCOPE = TypeAdapter[ActivationScope](ActivationScope)


class ReleaseOperationError(ValueError):
    """A local lifecycle operation failed with a stable reason."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@unique
class ReleaseAction(StrEnum):
    REGISTER = "register_release"
    APPROVE = "approve_release"
    ACTIVATE = "activate_release"
    ROLLBACK = "rollback_release"
    ACCEPT_TASK = "accept_task"
    REQUEST_RELEASE = "request_release"


@dataclass(frozen=True)
class ReleaseTarget:
    release_id: str
    bundle_ref: BundleRef
    scope: ActivationScope


class ResolvedTaskScope(ContractModel):
    """Trusted device, camera, region and exact scene resolved by the host."""

    device_id: RecordId
    camera_id: RecordId
    region_id: ArtifactId
    scene_type: VocabularyMemberRef

    def activation_scopes(self) -> dict[ScopeKind, ActivationScope]:
        return {
            "device": DeviceActivationScope(scope="device", device_id=self.device_id),
            "region": RegionActivationScope(scope="region", region_id=self.region_id),
            "scene": SceneActivationScope(scope="scene", scene_type=self.scene_type),
            "global": GlobalActivationScope(scope="global"),
        }


# Host callbacks perform local checks inside a short transaction. The principal
# comes from the authenticated host boundary. ActorRef records the result.
type ReleaseAuthorizer = Callable[
    [object, ReleaseAction, ReleaseTarget], ActorRef | None
]
type TaskScopeResolver = Callable[[object, TaskSubmission], ResolvedTaskScope | None]
type TaskReleasePolicy = Callable[
    [ReleaseRecord, ActivationRecord, ResolvedTaskScope, datetime], bool
]


class _CompletedOperation(ContractModel):
    schema_version: Literal["1.0.0"]
    parameters_sha256: Digest
    action: ReleaseAction
    actor: ActorRef
    scope: ActivationScope
    rollback_target_activation_id: RecordId | None = None
    result_json: str


class _ScopedApproval(ContractModel):
    schema_version: Literal["1.0.0"]
    scope: ActivationScope
    release: ReleaseRecord


class _TaskRequest(ContractModel):
    schema_version: Literal["1.0.0"]
    submission_sha256: Digest
    accepted_by: ActorRef
    activation_id: RecordId
    task_id: RecordId


def _key(*values: str | None) -> str:
    return sha256_digest(
        json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )


def _read[RecordT: ContractModel](
    raw: bytes | None, model: type[RecordT]
) -> RecordT | None:
    return None if raw is None else model.model_validate_json(raw)


def _wire(record: ContractModel) -> bytes:
    return record.model_dump_json().encode("utf-8")


def _task_keys(
    scope: IdempotencyScope, request_id: str, idempotency_key: str
) -> tuple[str, str]:
    identities = (scope.project_id, scope.client_id)
    return (
        f"request.{_key(*identities, request_id)}",
        f"idempotency.{_key(*identities, idempotency_key)}",
    )


def _scope_key(scope: ActivationScope) -> str:
    return _key(scope.stable_json())


def _approval_key(release_id: str, scope: ActivationScope) -> str:
    return _key(release_id, scope.stable_json())


def _release_ref(release_id: str) -> ReleaseRef:
    return ReleaseRef(record_kind=RuntimeRecordKind.RELEASE, record_id=release_id)


def _activation_ref(activation_id: str) -> ActivationRef:
    return ActivationRef(
        record_kind=RuntimeRecordKind.ACTIVATION, record_id=activation_id
    )


def _target(release: ReleaseRecord, scope: ActivationScope) -> ReleaseTarget:
    return ReleaseTarget(release.release_id, release.bundle_ref, scope)


class ReleaseManager:
    """Persist local lifecycle operations through the isolated Bundle ledger."""

    def __init__(
        self,
        store: LocalBundleStore,
        *,
        authorize: ReleaseAuthorizer,
        scope_precedence: tuple[ScopeKind, ...],
        resolve_task_scope: TaskScopeResolver,
        allow_new_task: TaskReleasePolicy,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            len(scope_precedence) != len(_SCOPE_KINDS)
            or frozenset(scope_precedence) != _SCOPE_KINDS
        ):
            raise ValueError("scope precedence must specify every scope exactly once")
        self._store = store
        self._authorize = authorize
        self._scope_precedence = tuple(scope_precedence)
        self._resolve_task_scope = resolve_task_scope
        self._allow_new_task = allow_new_task
        self._clock = clock

    def _actor(
        self, principal: object, action: ReleaseAction, target: ReleaseTarget
    ) -> ActorRef:
        actor = self._authorize(principal, action, target)
        if actor is None:
            raise ReleaseOperationError("unauthorized_release_action")
        return ActorRef.model_validate(actor)

    @staticmethod
    def _previous_operation[RecordT: ContractModel](
        txn: ReleaseLedger, operation_key: str, digest: str, model: type[RecordT]
    ) -> RecordT | None:
        saved = _read(txn.get("operation", operation_key), _CompletedOperation)
        if saved is None:
            return None
        if saved.parameters_sha256 != digest:
            raise ReleaseOperationError("operation_key_conflict")
        return model.model_validate_json(saved.result_json)

    @staticmethod
    def _complete_operation(
        txn: ReleaseLedger,
        operation_key: str,
        digest: str,
        result: ContractModel,
        *,
        action: ReleaseAction,
        actor: ActorRef,
        scope: ActivationScope,
        rollback_target_activation_id: str | None = None,
    ) -> None:
        saved = _CompletedOperation(
            schema_version="1.0.0",
            parameters_sha256=digest,
            action=action,
            actor=actor,
            scope=scope,
            rollback_target_activation_id=rollback_target_activation_id,
            result_json=result.model_dump_json(),
        )
        txn.compare_and_set(
            "operation", operation_key, expected=None, value=_wire(saved)
        )

    @staticmethod
    def _accepted(txn: ReleaseLedger, release_id: str) -> ReleaseRecord:
        release = _read(txn.get("release", release_id), ReleaseRecord)
        if release is None:
            raise ReleaseOperationError("release_not_found")
        return release

    @staticmethod
    def _scoped_approval(
        txn: ReleaseLedger, release_id: str, scope: ActivationScope
    ) -> ReleaseRecord | None:
        stored = _read(
            txn.get("approval", _approval_key(release_id, scope)), _ScopedApproval
        )
        if stored is None:
            return None
        if stored.scope != scope or stored.release.release_id != release_id:
            raise ReleaseOperationError("invalid_scope_approval")
        return stored.release

    @staticmethod
    def _approved(
        txn: ReleaseLedger, release: ReleaseRecord, scope: ActivationScope
    ) -> ReleaseRecord:
        approved = ReleaseManager._scoped_approval(txn, release.release_id, scope)
        if approved is None or approved.status is not ReleaseStatus.APPROVED:
            raise ReleaseOperationError("release_scope_not_approved")
        if approved.admission_receipt != release.admission_receipt:
            raise ReleaseOperationError("approval_admission_mismatch")
        return approved

    @staticmethod
    def _require_admission(txn: ReleaseLedger, release: ReleaseRecord) -> None:
        pin = release.admission_receipt
        if pin is None:
            raise ReleaseOperationError("accepted_admission_required")
        stored = txn.get_receipt(pin.receipt_id)
        if stored is None or not stored.accepted or stored.receipt != pin:
            raise ReleaseOperationError("accepted_admission_required")

    def register_release(
        self,
        *,
        operation_key: str,
        release_id: str,
        admission_receipt_id: str,
        principal: object,
    ) -> ReleaseRecord:
        """Bind one immutable Release identity to a genuine admitted Bundle."""
        operation_key = _RECORD_ID.validate_python(operation_key)
        release_id = _RECORD_ID.validate_python(release_id)
        admission_receipt_id = _RECORD_ID.validate_python(admission_receipt_id)
        with self._store._release_transaction() as txn:
            stored = txn.get_receipt(admission_receipt_id)
            if stored is None or not stored.accepted or stored.receipt is None:
                raise ReleaseOperationError("accepted_admission_required")
            admission = stored.receipt
            bundle_ref = BundleRef(
                bundle_id=admission.bundle_id,
                bundle_version=admission.bundle_version,
                bundle_digest=admission.bundle_digest,
            )
            scope = GlobalActivationScope(scope="global")
            actor = self._actor(
                principal,
                ReleaseAction.REGISTER,
                ReleaseTarget(release_id, bundle_ref, scope),
            )
            digest = _key(
                ReleaseAction.REGISTER,
                actor.stable_json(),
                release_id,
                admission_receipt_id,
            )
            existing = self._previous_operation(
                txn, operation_key, digest, ReleaseRecord
            )
            if existing is not None:
                return existing
            if txn.get("release", release_id) is not None:
                raise ReleaseOperationError("release_id_conflict")
            record = ReleaseRecord(
                schema_version="1.0.0",
                release_id=release_id,
                bundle_ref=bundle_ref,
                status=ReleaseStatus.ACCEPTED,
                admission_receipt=admission,
                recorded_at=self._clock(),
            )
            txn.compare_and_set(
                "release", release_id, expected=None, value=_wire(record)
            )
            self._complete_operation(
                txn,
                operation_key,
                digest,
                record,
                action=ReleaseAction.REGISTER,
                actor=actor,
                scope=scope,
            )
            return record

    def approve_release(
        self,
        *,
        operation_key: str,
        release_id: str,
        scope: ActivationScope,
        principal: object,
    ) -> ReleaseRecord:
        """Append the business approval snapshot for one exact activation scope."""
        operation_key = _RECORD_ID.validate_python(operation_key)
        release_id = _RECORD_ID.validate_python(release_id)
        scope = _ACTIVATION_SCOPE.validate_python(scope)
        with self._store._release_transaction() as txn:
            release = self._accepted(txn, release_id)
            actor = self._actor(
                principal, ReleaseAction.APPROVE, _target(release, scope)
            )
            digest = _key(
                ReleaseAction.APPROVE,
                actor.stable_json(),
                release_id,
                scope.stable_json(),
            )
            existing = self._previous_operation(
                txn, operation_key, digest, ReleaseRecord
            )
            if existing is not None:
                return existing
            self._require_admission(txn, release)
            key = _approval_key(release_id, scope)
            if txn.get("approval", key) is not None:
                raise ReleaseOperationError("release_scope_already_approved")
            admission = release.admission_receipt
            assert admission is not None  # Checked against the durable receipt above.
            now = self._clock()
            if now < release.recorded_at:
                raise ReleaseOperationError("approval_time_precedes_registration")
            receipt = ApprovalReceipt(
                schema_version="1.0.0",
                receipt_id=f"approval.{_key(operation_key, digest)}",
                release_ref=_release_ref(release_id),
                bundle_ref=release.bundle_ref,
                admission_receipt_id=admission.receipt_id,
                approved_by=actor,
                approved_at=now,
            )
            approved = ReleaseRecord(
                schema_version="1.0.0",
                release_id=release_id,
                bundle_ref=release.bundle_ref,
                status=ReleaseStatus.APPROVED,
                admission_receipt=admission,
                approval_receipt=receipt,
                recorded_at=now,
            )
            scoped = _ScopedApproval(
                schema_version="1.0.0", scope=scope, release=approved
            )
            txn.compare_and_set("approval", key, expected=None, value=_wire(scoped))
            self._complete_operation(
                txn,
                operation_key,
                digest,
                approved,
                action=ReleaseAction.APPROVE,
                actor=actor,
                scope=scope,
            )
            return approved

    @staticmethod
    def _active(txn: ReleaseLedger, scope: ActivationScope) -> ActivationRecord | None:
        pointer = txn.get("active_scope", _scope_key(scope))
        if pointer is None:
            return None
        active = _read(txn.get("activation", pointer.decode("utf-8")), ActivationRecord)
        if active is None or active.scope != scope:
            raise ReleaseOperationError("invalid_active_pointer")
        return active

    def _change_activation(
        self,
        *,
        txn: ReleaseLedger,
        operation_key: str,
        release: ReleaseRecord,
        scope: ActivationScope,
        activation_id: str,
        expected_previous_activation_id: str | None,
        principal: object,
        rollback_target: ActivationRecord | None,
    ) -> ActivationRecord:
        action = (
            ReleaseAction.ACTIVATE
            if rollback_target is None
            else ReleaseAction.ROLLBACK
        )
        actor = self._actor(principal, action, _target(release, scope))
        digest = _key(
            action,
            actor.stable_json(),
            release.release_id,
            scope.stable_json(),
            activation_id,
            expected_previous_activation_id,
            None if rollback_target is None else rollback_target.activation_id,
        )
        existing = self._previous_operation(
            txn, operation_key, digest, ActivationRecord
        )
        if existing is not None:
            return existing

        approved = self._approved(txn, release, scope)
        self._require_admission(txn, approved)
        previous = self._active(txn, scope)
        actual_previous = None if previous is None else previous.activation_id
        if actual_previous != expected_previous_activation_id:
            raise ReleaseOperationError("stale_activation_predecessor")
        if rollback_target is not None and previous is None:
            raise ReleaseOperationError("rollback_requires_active_scope")
        if (
            previous is not None
            and previous.release_ref.record_id == approved.release_id
        ):
            raise ReleaseOperationError("release_already_active")
        if txn.get("activation", activation_id) is not None:
            raise ReleaseOperationError("activation_id_conflict")
        now = self._clock()
        if now < approved.recorded_at or (
            previous is not None and now < previous.activated_at
        ):
            raise ReleaseOperationError("activation_time_precedes_evidence")
        previous_field: dict[str, ActivationRef] = {}
        if expected_previous_activation_id is not None:
            previous_field["previous_activation_ref"] = _activation_ref(
                expected_previous_activation_id
            )
        record = ActivationRecord(
            schema_version="1.0.0",
            activation_id=activation_id,
            release_ref=_release_ref(approved.release_id),
            bundle_ref=approved.bundle_ref,
            scope=scope,
            activated_by=actor,
            activated_at=now,
            **previous_field,
        )
        txn.compare_and_set(
            "activation", activation_id, expected=None, value=_wire(record)
        )
        txn.compare_and_set(
            "active_scope",
            _scope_key(scope),
            expected=None
            if actual_previous is None
            else actual_previous.encode("utf-8"),
            value=activation_id.encode("utf-8"),
        )
        self._complete_operation(
            txn,
            operation_key,
            digest,
            record,
            action=action,
            actor=actor,
            scope=scope,
            rollback_target_activation_id=None
            if rollback_target is None
            else rollback_target.activation_id,
        )
        return record

    def activate(
        self,
        *,
        operation_key: str,
        release_id: str,
        scope: ActivationScope,
        activation_id: str,
        expected_previous_activation_id: str | None,
        principal: object,
    ) -> ActivationRecord:
        """Append an activation with a compare-and-set predecessor."""
        operation_key = _RECORD_ID.validate_python(operation_key)
        release_id = _RECORD_ID.validate_python(release_id)
        scope = _ACTIVATION_SCOPE.validate_python(scope)
        activation_id = _RECORD_ID.validate_python(activation_id)
        if expected_previous_activation_id is not None:
            expected_previous_activation_id = _RECORD_ID.validate_python(
                expected_previous_activation_id
            )
        with self._store._release_transaction() as txn:
            return self._change_activation(
                txn=txn,
                operation_key=operation_key,
                release=self._accepted(txn, release_id),
                scope=scope,
                activation_id=activation_id,
                expected_previous_activation_id=expected_previous_activation_id,
                principal=principal,
                rollback_target=None,
            )

    def rollback(
        self,
        *,
        operation_key: str,
        scope: ActivationScope,
        target_activation_id: str,
        activation_id: str,
        expected_previous_activation_id: str,
        principal: object,
    ) -> ActivationRecord:
        """Append a new pointer to a historical Release in the exact same scope."""
        operation_key = _RECORD_ID.validate_python(operation_key)
        scope = _ACTIVATION_SCOPE.validate_python(scope)
        target_activation_id = _RECORD_ID.validate_python(target_activation_id)
        activation_id = _RECORD_ID.validate_python(activation_id)
        expected_previous_activation_id = _RECORD_ID.validate_python(
            expected_previous_activation_id
        )
        with self._store._release_transaction() as txn:
            target = _read(
                txn.get("activation", target_activation_id), ActivationRecord
            )
            if target is None or target.scope != scope:
                raise ReleaseOperationError("rollback_target_not_in_scope")
            release = self._accepted(txn, target.release_ref.record_id)
            if target.bundle_ref != release.bundle_ref:
                raise ReleaseOperationError("rollback_release_pin_mismatch")
            return self._change_activation(
                txn=txn,
                operation_key=operation_key,
                release=release,
                scope=scope,
                activation_id=activation_id,
                expected_previous_activation_id=expected_previous_activation_id,
                principal=principal,
                rollback_target=target,
            )

    def _authorize_acceptance(
        self,
        principal: object,
        submission: TaskSubmission,
        release: ReleaseRecord,
        activation: ActivationRecord,
    ) -> ActorRef:
        target = _target(release, activation.scope)
        actor = self._actor(principal, ReleaseAction.ACCEPT_TASK, target)
        requested = submission.requested_release_id
        if requested is not None:
            if requested != release.release_id:
                raise ReleaseOperationError("requested_release_not_selected")
            requesting_actor = self._actor(
                principal, ReleaseAction.REQUEST_RELEASE, target
            )
            if requesting_actor != actor:
                raise ReleaseOperationError("inconsistent_principal_identity")
        return actor

    def _replay_task(
        self,
        txn: ReleaseLedger,
        previous: _TaskRequest,
        submission: TaskSubmission,
        submission_digest: str,
        principal: object,
    ) -> AcceptedTask:
        if previous.submission_sha256 != submission_digest:
            raise ReleaseOperationError("task_idempotency_conflict")
        activation = _read(
            txn.get("activation", previous.activation_id), ActivationRecord
        )
        accepted = _read(txn.get("task_pin", previous.task_id), AcceptedTask)
        if activation is None or accepted is None:
            raise ReleaseOperationError("task_pin_missing")
        release = self._accepted(txn, accepted.release_id)
        if (
            activation.release_ref.record_id != release.release_id
            or activation.bundle_ref != release.bundle_ref
            or accepted.bundle_digest != release.bundle_ref.bundle_digest
        ):
            raise ReleaseOperationError("task_pin_release_mismatch")
        actor = self._authorize_acceptance(principal, submission, release, activation)
        if actor != previous.accepted_by:
            raise ReleaseOperationError("task_principal_mismatch")
        return accepted

    def accept_task(
        self, submission: TaskSubmission, *, principal: object
    ) -> AcceptedTask:
        """Resolve a trusted scope once and persist the immutable selected Release."""
        submission = TaskSubmission.model_validate(submission)
        identity = submission.idempotency_scope
        request_key, idempotency_key = _task_keys(
            identity, submission.request_id, submission.idempotency_key
        )
        submission_digest = sha256_digest(submission.stable_json().encode("utf-8"))

        with self._store._release_transaction() as txn:
            for key in (request_key, idempotency_key):
                previous = _read(txn.get("task_pin", key), _TaskRequest)
                if previous is not None:
                    return self._replay_task(
                        txn, previous, submission, submission_digest, principal
                    )

            resolved = self._resolve_task_scope(principal, submission)
            if resolved is None:
                raise ReleaseOperationError("task_scope_untrusted")
            resolved = ResolvedTaskScope.model_validate(resolved)
            context = submission.context
            if (context.device_id, context.camera_id, context.region_id) != (
                resolved.device_id,
                resolved.camera_id,
                resolved.region_id,
            ):
                raise ReleaseOperationError("task_scope_mismatch")

            scopes = resolved.activation_scopes()
            active = None
            for kind in self._scope_precedence:
                active = self._active(txn, scopes[kind])
                if active is not None:
                    break
            if active is None:
                raise ReleaseOperationError("no_active_release")
            release = self._approved(
                txn, self._accepted(txn, active.release_ref.record_id), active.scope
            )
            if active.bundle_ref != release.bundle_ref:
                raise ReleaseOperationError("activation_release_pin_mismatch")
            actor = self._authorize_acceptance(principal, submission, release, active)
            self._require_admission(txn, release)

            now = self._clock()
            if now < active.activated_at:
                raise ReleaseOperationError("task_time_precedes_activation")
            if self._allow_new_task(release, active, resolved, now) is not True:
                raise ReleaseOperationError("new_task_disallowed")
            task_id = f"task.{_key(request_key, idempotency_key)}"
            accepted = AcceptedTask(
                schema_version="1.0.0",
                task_id=task_id,
                request_id=submission.request_id,
                idempotency_scope=identity,
                idempotency_key=submission.idempotency_key,
                media_ref=submission.media_ref,
                context=context,
                release_id=release.release_id,
                bundle_digest=release.bundle_ref.bundle_digest,
                accepted_at=now,
                status=TaskStatus.ACCEPTED,
                trace=submission.trace,
            )
            request = _TaskRequest(
                schema_version="1.0.0",
                submission_sha256=submission_digest,
                accepted_by=actor,
                activation_id=active.activation_id,
                task_id=task_id,
            )
            txn.compare_and_set(
                "task_pin", task_id, expected=None, value=_wire(accepted)
            )
            for key in (request_key, idempotency_key):
                txn.compare_and_set(
                    "task_pin", key, expected=None, value=_wire(request)
                )
            return accepted

    def get_release(
        self, release_id: str, *, scope: ActivationScope | None = None
    ) -> ReleaseRecord | None:
        """Read the unscoped admission snapshot or one exact scoped approval."""
        with self._store._release_transaction() as txn:
            if scope is None:
                return _read(txn.get("release", release_id), ReleaseRecord)
            return self._scoped_approval(txn, release_id, scope)

    def get_activation(self, activation_id: str) -> ActivationRecord | None:
        with self._store._release_transaction() as txn:
            return _read(txn.get("activation", activation_id), ActivationRecord)

    def active_activation(self, scope: ActivationScope) -> ActivationRecord | None:
        with self._store._release_transaction() as txn:
            return self._active(txn, scope)

    def get_task(self, task_id: str) -> AcceptedTask | None:
        if not task_id.startswith("task."):
            return None
        with self._store._release_transaction() as txn:
            return _read(txn.get("task_pin", task_id), AcceptedTask)

    def get_task_activation(self, task: AcceptedTask) -> ActivationRecord | None:
        """Resolve the exact stored activation for an already authorized Task."""
        if not isinstance(task, AcceptedTask):
            return None
        try:
            task = AcceptedTask.model_validate(task)
            request_key, idempotency_key = _task_keys(
                task.idempotency_scope, task.request_id, task.idempotency_key
            )
            if task.task_id != f"task.{_key(request_key, idempotency_key)}":
                return None
            with self._store._release_transaction() as txn:
                if _read(txn.get("task_pin", task.task_id), AcceptedTask) != task:
                    return None
                request = _read(txn.get("task_pin", request_key), _TaskRequest)
                if (
                    request is None
                    or request.task_id != task.task_id
                    or _read(txn.get("task_pin", idempotency_key), _TaskRequest)
                    != request
                ):
                    return None
                activation = _read(
                    txn.get("activation", request.activation_id), ActivationRecord
                )
                if (
                    activation is None
                    or activation.activation_id != request.activation_id
                    or activation.release_ref.record_id != task.release_id
                    or activation.bundle_ref.bundle_digest != task.bundle_digest
                    or activation.activated_at > task.accepted_at
                ):
                    return None
                scope = activation.scope
                if (
                    isinstance(scope, DeviceActivationScope)
                    and scope.device_id != task.context.device_id
                ) or (
                    isinstance(scope, RegionActivationScope)
                    and scope.region_id != task.context.region_id
                ):
                    return None
                release = self._accepted(txn, task.release_id)
                if (
                    release.release_id != task.release_id
                    or release.status is not ReleaseStatus.ACCEPTED
                    or release.bundle_ref != activation.bundle_ref
                ):
                    return None
                approval = self._approved(txn, release, scope)
                if approval.recorded_at > activation.activated_at:
                    return None
                return activation
        except (ReleaseOperationError, ValidationError):
            return None
