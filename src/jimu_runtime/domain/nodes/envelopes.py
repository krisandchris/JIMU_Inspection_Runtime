"""Common invocation data used by the six node-specific contracts."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    Description,
    NonNegativeBudget,
    UTCDateTime,
    immutable_sequence,
)
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.executions import (
    NodeId,
    PinnedRuntimeInput,
    TraceContext,
)
from jimu_runtime.domain.runtime.facts import require_unique_runtime_refs
from jimu_runtime.domain.validation.issues import ValidationIssue

type NodeIssues = Annotated[
    tuple[ValidationIssue, ...],
    Field(max_length=1024),
    BeforeValidator(immutable_sequence),
]


class NodeError(ContractModel):
    code: ArtifactId
    error_class: Literal[
        "validation", "dependency", "transport", "timeout", "resource", "internal"
    ]
    retryable: bool
    message: Description
    validation_issues: NodeIssues


class ArtifactOutputRef(PinnedRuntimeInput):
    """Immutable reference and digest of an emitted runtime artifact."""


type NodeInputs = Annotated[
    tuple[PinnedRuntimeInput, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]
type NodeOutputs = Annotated[
    tuple[ArtifactOutputRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class NodeInputEnvelope(ContractModel):
    schema_version: Literal["1.0.0"]
    node_id: NodeId
    task_id: RecordId
    execution_id: RecordId
    release_id: RecordId
    bundle_digest: Digest
    node_run_id: RecordId
    attempt_id: RecordId
    trace: TraceContext
    upstream_artifacts: NodeInputs
    node_config_ref: ContentRef

    @model_validator(mode="after")
    def validate_refs(self) -> Self:
        require_unique_runtime_refs(tuple(item.ref for item in self.upstream_artifacts))
        return self


class _FinishedNodeOutput(ContractModel):
    schema_version: Literal["1.0.0"]
    node_id: NodeId
    node_run_id: RecordId
    attempt_id: RecordId
    trace: TraceContext
    output_artifacts: NodeOutputs
    started_at: UTCDateTime
    finished_at: UTCDateTime
    duration_ms: NonNegativeBudget

    @model_validator(mode="before")
    @classmethod
    def decode_status(cls, value: object) -> object:
        return _decode_node_status(value)

    @model_validator(mode="after")
    def verify_output(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("node finish cannot precede start")
        elapsed = self.finished_at - self.started_at
        expected_ms = (
            elapsed.days * 86_400_000
            + elapsed.seconds * 1000
            + elapsed.microseconds // 1000
        )
        if self.duration_ms != expected_ms:
            raise ValueError("node duration must equal the elapsed whole milliseconds")
        require_unique_runtime_refs(tuple(item.ref for item in self.output_artifacts))
        return self


class SucceededNodeOutput(_FinishedNodeOutput):
    status: Literal[NodeRunStatus.SUCCEEDED]


class FailedNodeOutput(_FinishedNodeOutput):
    status: Literal[NodeRunStatus.FAILED]
    error: NodeError


class CancelledNodeOutput(_FinishedNodeOutput):
    status: Literal[NodeRunStatus.CANCELLED]
    error: NodeError


def _decode_node_status(raw: object) -> object:
    if isinstance(raw, dict) and "status" in raw:
        return {**raw, "status": NodeRunStatus._decode(raw["status"])}
    return raw


type NodeOutputEnvelope = Annotated[
    SucceededNodeOutput | FailedNodeOutput | CancelledNodeOutput,
    Field(discriminator="status"),
    BeforeValidator(_decode_node_status),
]


def check_envelope_pair(
    input_record: NodeInputEnvelope, output: NodeOutputEnvelope
) -> None:
    if (
        output.node_id,
        output.node_run_id,
        output.attempt_id,
        output.trace.trace_id,
    ) != (
        input_record.node_id,
        input_record.node_run_id,
        input_record.attempt_id,
        input_record.trace.trace_id,
    ):
        raise ValueError("node output must match the input invocation and trace")
