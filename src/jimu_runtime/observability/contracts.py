"""Bounded public projections of runtime audit records."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.enums import AttemptStatus, ExecutionStatus, TaskStatus
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.runtime.executions import NodeId

PageSize = Annotated[int, Field(ge=1, le=100)]
EventNumber = Annotated[int, Field(ge=0, le=2**63 - 1)]

type EventKind = Literal[
    "task_transition",
    "execution_transition",
    "node_transition",
    "attempt_transition",
    "late_response",
    "system_output",
]
type Boundary = Literal["not_dispatched", "sent_unknown", "acknowledged"]
type CallKind = Literal["model", "tool", "unknown"]
type CallStatus = Literal[
    "succeeded",
    "partial",
    "failed",
    "not_dispatched",
    "unqualified",
]
type CallErrorClass = Literal[
    "transport",
    "timeout",
    "rate_limit",
    "unavailable",
    "validation",
    "association",
    "resource_exhausted",
    "cancelled",
    "internal",
    "other",
]
type Availability = Literal["ready", "unavailable", "unknown"]


class EventCursor(ContractModel):
    after: EventNumber = 0
    through: EventNumber | None = None

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.through is not None and self.after > self.through:
            raise ValueError("event cursor is outside its high-water mark")
        return self


class TimeCursor(ContractModel):
    recorded_at: UTCDateTime
    record_id: RecordId
    revision: Digest


class ToolStepCursor(ContractModel):
    child_attempt_id: RecordId
    revision: Digest


class TraceExecution(ContractModel):
    task_id: RecordId
    execution_id: RecordId
    trace_id: RecordId
    task_status: TaskStatus
    execution_status: ExecutionStatus
    accepted_at: UTCDateTime
    initialized_at: UTCDateTime
    started_at: UTCDateTime | None
    ended_at: UTCDateTime | None


class TraceEvent(ContractModel):
    number: Annotated[int, Field(ge=1, le=2**63 - 1)]
    recorded_at: UTCDateTime
    kind: EventKind
    span_id: RecordId | None
    parent_span_id: RecordId | None
    node_run_id: RecordId | None
    node_id: NodeId | None
    attempt_id: RecordId | None
    attempt_number: Annotated[int, Field(ge=1, le=1024)] | None
    dispatch_id: RecordId | None
    status: (
        Literal[
            "accepted",
            "queued",
            "initialized",
            "pending",
            "ready",
            "running",
            "created",
            "completed",
            "partial_failed",
            "succeeded",
            "failed",
            "blocked",
            "skipped",
            "cancelled",
            "timed_out",
            "superseded",
        ]
        | None
    )


class EventPage(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    execution: TraceExecution
    execution_state_through: EventNumber
    through: EventNumber
    items: Annotated[tuple[TraceEvent, ...], Field(max_length=100)]
    next_cursor: EventCursor | None


class CallReceipt(ContractModel):
    recorded_at: UTCDateTime
    boundary: Boundary
    late: bool
    fence_qualified: bool | None
    status: CallStatus
    error_class: CallErrorClass | None
    producer_id: RecordId | None
    started_at: UTCDateTime | None
    completed_at: UTCDateTime | None
    response_evidence_failed: bool
    invalid_slots: Annotated[int, Field(ge=0, le=1024)]
    missing_slots: Annotated[int, Field(ge=0, le=1024)]


class CallSpan(ContractModel):
    dispatch_id: RecordId
    recorded_at: UTCDateTime
    kind: CallKind
    node_id: NodeId
    node_run_id: RecordId
    operation_node_run_id: RecordId
    attempt_id: RecordId
    parent_node_run_id: RecordId | None
    parent_attempt_id: RecordId | None
    send_admitted: bool
    receipts: Annotated[tuple[CallReceipt, ...], Field(max_length=2)]


class CallPage(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    task_id: RecordId
    execution_id: RecordId
    trace_id: RecordId
    items: Annotated[tuple[CallSpan, ...], Field(max_length=100)]
    next_cursor: TimeCursor | None


class ToolStepSpan(ContractModel):
    step_id: RecordId
    parent_node_run_id: RecordId
    parent_attempt_id: RecordId
    child_node_run_id: RecordId
    child_attempt_id: RecordId
    started_at: UTCDateTime | None
    ended_at: UTCDateTime | None
    tool_execution_id: RecordId | None
    outcome: CallStatus | None
    attempt_status: AttemptStatus | None


class ToolStepPage(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    task_id: RecordId
    execution_id: RecordId
    trace_id: RecordId
    items: Annotated[tuple[ToolStepSpan, ...], Field(max_length=100)]
    next_cursor: ToolStepCursor | None


class Readiness(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    database: Availability
    workflow_schema: Availability
    worker_claim: Availability

    @property
    def ready(self) -> bool:
        return (
            self.database == self.workflow_schema == "ready"
            and self.worker_claim != "unavailable"
        )
