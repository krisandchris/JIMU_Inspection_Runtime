"""Wire DTOs for the versioned Runtime API."""

from typing import Annotated, Literal

from pydantic import Field

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.enums import TaskStatus
from jimu_runtime.domain.primitives import Digest, RecordId
from jimu_runtime.domain.runtime.executions import (
    ExecutionPins,
    ExecutionRecord,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.releases import ActivationScope
from jimu_runtime.observability.contracts import EventNumber, TraceEvent


class AcceptedExecution(ContractModel):
    execution_id: RecordId
    pins: ExecutionPins


class TaskAcceptance(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    task: AcceptedTask
    execution: AcceptedExecution


class TaskView(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    task: AcceptedTask
    task_status: TaskStatus
    execution: ExecutionRecord
    cancel_requested_at: UTCDateTime | None


class CancellationReceipt(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    execution_id: RecordId
    cancellation_requested: Literal[True] = True


class TracePage(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    execution_id: RecordId
    trace_id: RecordId
    through_event_number: EventNumber
    events: Annotated[tuple[TraceEvent, ...], Field(max_length=100)]
    next_after_event_number: Annotated[int, Field(ge=1, le=2**63 - 1)] | None


class RegisterRelease(ContractModel):
    schema_version: Literal["1.0.0"]
    operation_key: RecordId
    release_id: RecordId
    admission_receipt_id: RecordId


class ApproveRelease(ContractModel):
    schema_version: Literal["1.0.0"]
    operation_key: RecordId
    scope: ActivationScope


class ActivateRelease(ApproveRelease):
    activation_id: RecordId
    expected_previous_activation_id: RecordId | None


class EvidenceCoordinates(ContractModel):
    execution_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    evidence_id: RecordId
    output_digest: Digest


class ErrorResponse(ContractModel):
    code: str
    request_id: RecordId
