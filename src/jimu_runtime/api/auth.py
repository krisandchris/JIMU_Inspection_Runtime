"""Authentication and resource authorization supplied by the trusted host."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.releases import ActivationScope


class Action(StrEnum):
    ACCESS_SCOPE = "access_scope"
    TASK_SUBMIT = "task_submit"
    TASK_READ = "task_read"
    TASK_CANCEL = "task_cancel"
    TASK_RESULT = "task_result"
    EXECUTION_READ = "execution_read"
    EXECUTION_TRACE = "execution_trace"
    ARTIFACT_REPLAY = "artifact_replay"
    INFERENCE_REPLAY = "inference_replay"
    EVIDENCE_READ = "evidence_read"
    EVIDENCE_NORMALIZED = "evidence_normalized"
    EVIDENCE_RAW = "evidence_raw"
    EVIDENCE_LATE = "evidence_late"
    BUNDLE_ADMIT = "bundle_admit"
    BUNDLE_READ = "bundle_read"
    RELEASE_REGISTER = "release_register"
    RELEASE_READ = "release_read"
    RELEASE_APPROVE = "release_approve"
    RELEASE_ACTIVATE = "release_activate"


@dataclass(frozen=True, slots=True)
class Identity:
    principal: object = field(repr=False)
    actor: ActorRef


@dataclass(frozen=True, slots=True, kw_only=True)
class Resource:
    action: Action
    scope: IdempotencyScope
    task_id: str | None = None
    execution_id: str | None = None
    resource_id: str | None = None
    related_id: str | None = None
    activation_scope: ActivationScope | None = None


class ApiAuthority(Protocol):
    async def authenticate(self, credential: str) -> Identity | None:
        """Authenticate the bearer against the host's trusted identity registry."""

    async def allows(self, identity: Identity, resource: Resource) -> bool:
        """Authoritatively check this action on this exact scoped resource."""


@dataclass(frozen=True, slots=True)
class Access:
    identity: Identity
    scope: IdempotencyScope
