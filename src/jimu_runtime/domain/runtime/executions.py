"""Versioned runtime records for execution and lifecycle audit."""

from enum import unique
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    PositiveBudget,
    ShortText,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.definitions.models import (
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    TaskStatus,
    WireEnum,
)
from jimu_runtime.domain.fact_selectors import (
    SystemProducerRequirement,
    ToolContentRef,
)
from jimu_runtime.domain.policies import DeploymentRequirement
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    ModelInvocationRef,
    NodeRunRef,
    ScopedRecord,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptOwner,
    MappedOutputResult,
    NodeAttemptOwner,
)


@unique
class NodeId(WireEnum):
    N01 = "N01"
    N02 = "N02"
    N03 = "N03"
    N04 = "N04"
    N05 = "N05"
    N06 = "N06"


class TraceContext(ContractModel):
    trace_id: RecordId
    span_id: RecordId | None = None
    parent_span_id: RecordId | None = None

    @model_validator(mode="after")
    def parent_requires_distinct_span(self) -> Self:
        if self.parent_span_id is not None and (
            self.span_id is None or self.span_id == self.parent_span_id
        ):
            raise ValueError("a parent span requires a distinct local span")
        return self


class ConfiguredModelPin(ContractModel):
    binding_ref: ModelBindingContentRef
    provider_policy_ref: PolicyContentRef
    fallback_policy_ref: PolicyContentRef
    primary_deployment_id: ArtifactId
    deployments: Annotated[
        tuple[DeploymentRequirement, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def primary_is_registered_once(self) -> Self:
        ids = {item.deployment_id for item in self.deployments}
        if len(ids) != len(self.deployments):
            raise ValueError("configured deployment IDs must be unique")
        if self.primary_deployment_id not in ids:
            raise ValueError("primary deployment requires a registered deployment pin")
        return self


class ConfiguredToolPin(ContractModel):
    tool_ref: ToolContentRef
    implementation_version: Version


class ExecutionPins(ContractModel):
    release_id: RecordId
    bundle_digest: Digest
    runtime_version: Version
    workflow_version: Version
    model_pins: Annotated[
        tuple[ConfiguredModelPin, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    tool_pins: Annotated[
        tuple[ConfiguredToolPin, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    input_digest: Digest

    @model_validator(mode="after")
    def one_pin_per_definition(self) -> Self:
        require_unique_references(tuple(pin.binding_ref for pin in self.model_pins))
        require_unique_references(tuple(pin.tool_ref for pin in self.tool_pins))
        return self


class ContextSnapshotPin(ContractModel):
    snapshot_ref: ContextSnapshotRef
    snapshot_digest: Digest


OBSERVATION_DEPENDENCY_DEFERRED = "observation_dependency_deferred"


class LifecycleReason(ContractModel):
    code: ArtifactId
    detail: ShortText | None = None


def _check_times(
    created_at: UTCDateTime,
    started_at: UTCDateTime | None,
    ended_at: UTCDateTime | None,
) -> None:
    timestamps = tuple(t for t in (created_at, started_at, ended_at) if t is not None)
    if timestamps != tuple(sorted(timestamps)):
        raise ValueError("lifecycle timestamps must be chronological")


class ExecutionRecord(ContractModel):
    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    task_id: RecordId
    pins: ExecutionPins
    trace: TraceContext
    status: ExecutionStatus
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    ended_at: UTCDateTime | None = None
    context_snapshot: ContextSnapshotPin | None = None
    model_invocation_refs: Annotated[
        tuple[ModelInvocationRef, ...],
        Field(max_length=16384, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def lifecycle_shape(self) -> Self:
        started = self.status is not ExecutionStatus.INITIALIZED
        ended = self.status not in (
            ExecutionStatus.INITIALIZED,
            ExecutionStatus.RUNNING,
        )
        if (self.started_at is not None, self.ended_at is not None) != (started, ended):
            raise ValueError("Execution timestamps must agree with status")
        if not started and (
            self.context_snapshot is not None or self.model_invocation_refs
        ):
            raise ValueError(
                "initialized Execution has no resolved context or invocation"
            )
        _check_times(self.created_at, self.started_at, self.ended_at)
        require_unique_runtime_refs(self.model_invocation_refs)
        return self


class PinnedRuntimeInput(ContractModel):
    ref: RuntimeRecordRef
    digest: Digest


class AttemptLink(ContractModel):
    attempt_ref: AttemptRef
    attempt_number: PositiveBudget


class NodeRun(ContractModel):
    model_config = ConfigDict(
        json_schema_extra={
            "if": {"properties": {"node_id": {"not": {"const": "N05"}}}},
            "then": {"properties": {"attempt_history": {"maxItems": 1024}}},
        }
    )

    schema_version: Literal["1.0.0"]
    node_run_id: RecordId
    execution_id: RecordId
    node_id: NodeId
    trace: TraceContext
    config_ref: PolicyContentRef
    upstream_inputs: Annotated[
        tuple[PinnedRuntimeInput, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    input_digest: Digest
    status: NodeRunStatus
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    ended_at: UTCDateTime | None = None
    reason: LifecycleReason | None = None
    predecessor_ref: NodeRunRef | None = None
    attempt_history: Annotated[
        tuple[AttemptLink, ...],
        Field(max_length=1025),
        BeforeValidator(immutable_sequence),
    ]
    output_refs: Annotated[
        tuple[RuntimeRecordRef, ...],
        Field(max_length=16384, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def lifecycle_shape(self) -> Self:
        if (
            self.predecessor_ref is not None
            and self.predecessor_ref.record_id == self.node_run_id
        ):
            raise ValueError("predecessor must identify another NodeRun")
        started = self.started_at is not None
        if self.status is not NodeRunStatus.CANCELLED:
            expected_start = self.status in (
                NodeRunStatus.RUNNING,
                NodeRunStatus.SUCCEEDED,
                NodeRunStatus.FAILED,
            )
            if started != expected_start:
                raise ValueError("NodeRun started_at must agree with status")
        terminal = self.status not in (
            NodeRunStatus.PENDING,
            NodeRunStatus.READY,
            NodeRunStatus.RUNNING,
        )
        if (self.ended_at is not None) != terminal:
            raise ValueError("NodeRun ended_at must agree with status")
        if bool(self.attempt_history) != started or (self.output_refs and not started):
            raise ValueError("Attempt history and output require NodeRun start")
        if (
            self.status in (NodeRunStatus.BLOCKED, NodeRunStatus.SKIPPED)
            and self.reason is None
        ):
            raise ValueError("blocked or skipped NodeRun requires a structured reason")
        if self.status is NodeRunStatus.SUCCEEDED and not self.output_refs:
            raise ValueError("succeeded NodeRun requires output references")
        _check_times(self.created_at, self.started_at, self.ended_at)
        require_unique_runtime_refs(tuple(item.ref for item in self.upstream_inputs))
        require_unique_runtime_refs(
            tuple(item.attempt_ref for item in self.attempt_history)
        )
        require_unique_runtime_refs(self.output_refs)
        if self.node_id is not NodeId.N05 and len(self.attempt_history) > 1024:
            raise ValueError("only N05 can record an observation deferral Attempt")
        numbers = [item.attempt_number for item in self.attempt_history]
        if any(current >= following for current, following in pairwise(numbers)):
            raise ValueError("Attempt numbers must increase strictly within NodeRun")
        return self


class Attempt(ContractModel):
    schema_version: Literal["1.0.0"]
    attempt_id: RecordId
    execution_id: RecordId
    owner: AttemptOwner
    attempt_number: PositiveBudget
    status: AttemptStatus
    created_at: UTCDateTime
    started_at: UTCDateTime | None = None
    ended_at: UTCDateTime | None = None
    reason: LifecycleReason | None = None

    @model_validator(mode="after")
    def lifecycle_shape(self) -> Self:
        started = self.started_at is not None
        if self.status not in (AttemptStatus.CANCELLED, AttemptStatus.SUPERSEDED):
            if started != (self.status is not AttemptStatus.CREATED):
                raise ValueError("Attempt started_at must agree with status")
        terminal = self.status not in (AttemptStatus.CREATED, AttemptStatus.RUNNING)
        if (self.ended_at is not None) != terminal:
            raise ValueError("Attempt ended_at must agree with status")
        if (
            self.status in (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT)
            and self.reason is None
        ):
            raise ValueError("failed or timed-out Attempt requires a structured reason")
        _check_times(self.created_at, self.started_at, self.ended_at)
        return self

    @property
    def observation_dependency_deferred(self) -> bool:
        return (
            self.status is AttemptStatus.SUPERSEDED
            and self.reason is not None
            and self.reason.code == OBSERVATION_DEPENDENCY_DEFERRED
        )

    def belongs_to_node(self, node: NodeRun) -> bool:
        return (
            self.execution_id == node.execution_id
            and isinstance(self.owner, NodeAttemptOwner)
            and self.owner.node_run_ref.record_id == node.node_run_id
        )


class TaskTransitionEvent(ContractModel):
    event_kind: Literal["task_transition"]
    task_id: RecordId
    previous_status: TaskStatus
    next_status: TaskStatus


class ExecutionTransitionEvent(ContractModel):
    event_kind: Literal["execution_transition"]
    previous_status: ExecutionStatus
    next_status: ExecutionStatus


class NodeTransitionEvent(ContractModel):
    event_kind: Literal["node_transition"]
    node_run_ref: NodeRunRef
    previous_status: NodeRunStatus
    next_status: NodeRunStatus


class AttemptTransitionEvent(ContractModel):
    event_kind: Literal["attempt_transition"]
    attempt_ref: AttemptRef
    previous_status: AttemptStatus
    next_status: AttemptStatus


class LateResponseEvent(ContractModel):
    event_kind: Literal["late_response"]
    attempt_ref: AttemptRef
    response_ref: RuntimeRecordRef
    reason: LifecycleReason


class SystemOutputAuthorityWitness(ScopedRecord):
    arrival_qualification: Literal["on_time", "late", "unresolved"]
    output: MappedOutputResult


class SystemOutputEvent(OptionalContract):
    event_kind: Literal["system_output"]
    attempt_ref: AttemptRef
    requirement: SystemProducerRequirement
    output_name: ArtifactId
    actual_implementation_version: Version
    authority_output: AbsentOptional[SystemOutputAuthorityWitness] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def implementation_is_allowed(self) -> Self:
        if (
            self.actual_implementation_version
            not in self.requirement.allowed_implementation_versions
        ):
            raise ValueError("system output requires an allowed implementation version")
        if (
            self.authority_output is not None
            and self.authority_output.output.output_name != self.output_name
        ):
            raise ValueError(
                "system output witness must identify the emitted or absent output"
            )
        return self


type ExecutionEventPayload = Annotated[
    TaskTransitionEvent
    | ExecutionTransitionEvent
    | NodeTransitionEvent
    | AttemptTransitionEvent
    | LateResponseEvent
    | SystemOutputEvent,
    Field(discriminator="event_kind"),
]


class ExecutionEvent(ContractModel):
    schema_version: Literal["1.0.0"]
    execution_event_id: RecordId
    execution_id: RecordId
    event_number: PositiveBudget
    trace: TraceContext
    recorded_at: UTCDateTime
    payload: ExecutionEventPayload

    @model_validator(mode="after")
    def system_output_witness_scope(self) -> Self:
        if isinstance(self.payload, SystemOutputEvent):
            witness = self.payload.authority_output
            if witness is not None and witness.execution_id != self.execution_id:
                raise ValueError("system output witness must belong to this Execution")
        return self
