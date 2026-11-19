"""Pinned replay request and run data for artifact scoring and inference."""

from enum import unique
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    UTCDateTime,
    immutable_sequence,
)
from jimu_runtime.domain.enums import RuntimeRecordKind, WireEnum
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.executions import (
    ExecutionPins,
    ExecutionRecord,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import EvidenceRef, TypedRuntimeRef


@unique
class ReplayKind(WireEnum):
    ARTIFACT = "artifact"
    INFERENCE = "inference"


class ExecutionRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.EXECUTION]


class ReplayExecutionSource(ContractModel):
    execution_ref: ExecutionRef
    task_id: RecordId
    pins: ExecutionPins

    def validate_execution(self, execution: ExecutionRecord) -> None:
        """Match supplied historical Execution data to the source pin."""
        checked = ExecutionRecord.model_validate(execution)
        if (
            checked.execution_id != self.execution_ref.record_id
            or checked.task_id != self.task_id
            or checked.pins != self.pins
        ):
            raise ValueError(
                "source Execution identity, Task and pins must match the Replay request"
            )


class ScoringProtocolPin(ContractModel):
    protocol_id: ArtifactId
    version: Version
    digest: Digest


def _normalize_kind(value: object) -> object:
    if isinstance(value, dict) and "kind" in value:
        return {**value, "kind": ReplayKind._decode(value["kind"])}
    return value


class _ReplayContract(ContractModel):
    @model_validator(mode="before")
    @classmethod
    def decode_kind(cls, value: object) -> object:
        return _normalize_kind(value)


type ReplayArtifacts = Annotated[
    tuple[PinnedRuntimeInput, ...],
    Field(min_length=1, max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]


class ArtifactReplayRequest(_ReplayContract):
    schema_version: Literal["1.0.0"]
    kind: Literal[ReplayKind.ARTIFACT]
    source: ReplayExecutionSource
    scoring_protocol: ScoringProtocolPin
    source_artifacts: ReplayArtifacts

    @model_validator(mode="after")
    def unique_source_artifacts(self) -> Self:
        refs = {
            (item.ref.record_kind, item.ref.record_id) for item in self.source_artifacts
        }
        if len(refs) != len(self.source_artifacts):
            raise ValueError("source artifact references must be unique by kind and ID")
        return self


class InferenceReplayRequest(_ReplayContract):
    schema_version: Literal["1.0.0"]
    kind: Literal[ReplayKind.INFERENCE]
    source: ReplayExecutionSource
    target_pins: ExecutionPins

    @model_validator(mode="after")
    def preserve_task_input(self) -> Self:
        if self.target_pins.input_digest != self.source.pins.input_digest:
            raise ValueError(
                "Inference Replay must preserve the original Task input digest"
            )
        return self


type ReplayRequest = Annotated[
    ArtifactReplayRequest | InferenceReplayRequest,
    Field(discriminator="kind"),
    BeforeValidator(_normalize_kind),
]


class ReplayScoringEvidence(PinnedRuntimeInput):
    ref: EvidenceRef


class ArtifactReplayRun(_ReplayContract):
    schema_version: Literal["1.0.0"]
    kind: Literal[ReplayKind.ARTIFACT]
    replay_run_id: RecordId
    request: ArtifactReplayRequest
    scoring_evidence: ReplayScoringEvidence
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def distinct_scoring_evidence(self) -> Self:
        evidence = self.scoring_evidence.ref
        if any(
            item.ref.record_kind == evidence.record_kind
            and item.ref.record_id == evidence.record_id
            for item in self.request.source_artifacts
        ):
            raise ValueError("scoring evidence must have a new identity")
        return self


class InferenceReplayRun(_ReplayContract):
    schema_version: Literal["1.0.0"]
    kind: Literal[ReplayKind.INFERENCE]
    replay_run_id: RecordId
    request: InferenceReplayRequest
    target_execution_ref: ExecutionRef
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def distinct_execution(self) -> Self:
        if self.target_execution_ref == self.request.source.execution_ref:
            raise ValueError("Inference Replay requires a distinct target Execution")
        return self

    def validate_target_execution(self, execution: ExecutionRecord) -> None:
        """Match supplied target data to the replay pin and original Task."""
        checked = ExecutionRecord.model_validate(execution)
        if (
            checked.execution_id != self.target_execution_ref.record_id
            or checked.task_id != self.request.source.task_id
            or checked.pins != self.request.target_pins
        ):
            raise ValueError(
                "target Execution identity, Task and pins must match the Replay Run"
            )


type ReplayRun = Annotated[
    ArtifactReplayRun | InferenceReplayRun,
    Field(discriminator="kind"),
    BeforeValidator(_normalize_kind),
]
