"""Immutable request acceptance and N01 context data contracts."""

import re
from datetime import datetime
from enum import unique
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ContextProvenanceSource
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    ContextSchemaRef,
    NonNegativeBudget,
    ShortText,
    UTCDateTime,
    immutable_sequence,
)
from jimu_runtime.domain.enums import TaskStatus, WireEnum
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.executions import TraceContext
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.media import FinalizedAsset, MediaAsset, MediaRef
from jimu_runtime.domain.tool_mappings import RegionContentRef
from jimu_runtime.domain.values import FieldName, RecordValue, Value


@unique
class CaptureClockSource(WireEnum):
    DEVICE_CLOCK = "device_clock"
    SERVER_CLOCK = "server_clock"
    EXTERNAL_REFERENCE = "external_reference"


def _valid_capture_timestamp(value: str) -> str:
    # Validate calendar components before comparing it with internal UTC time.
    datetime.fromisoformat(value)
    return value


type OriginalCaptureTimestamp = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=re.compile(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:"
            r"[0-5][0-9](?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$(?![\s\S])"
        ),
    ),
    AfterValidator(_valid_capture_timestamp),
    Field(json_schema_extra={"format": "date-time"}),
]


type CaptureTimezone = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=128,
        pattern=re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*$(?![\s\S])"),
    ),
]


class ClockProvenance(ContractModel):
    captured_at: UTCDateTime
    original_timestamp: OriginalCaptureTimestamp
    original_timezone: CaptureTimezone | None = None
    source: CaptureClockSource
    clock_id: RecordId
    uncertainty_ms: NonNegativeBudget | None = None

    @model_validator(mode="after")
    def matches_original_time(self) -> Self:
        if datetime.fromisoformat(self.original_timestamp) != self.captured_at:
            raise ValueError("UTC capture time must represent the original timestamp")
        return self


class TaskContext(ContractModel):
    """Submitted identifiers and typed metadata carried into trusted intake."""

    device_id: RecordId
    camera_id: RecordId
    region_id: ArtifactId
    clock: ClockProvenance
    metadata: RecordValue


class IdempotencyScope(ContractModel):
    project_id: RecordId
    client_id: RecordId


class TaskSubmission(ContractModel):
    schema_version: Literal["1.0.0"]
    request_id: RecordId
    idempotency_scope: IdempotencyScope
    idempotency_key: ShortText
    media_ref: MediaRef
    context: TaskContext
    requested_release_id: RecordId | None = None
    trace: TraceContext


class AcceptedTask(ContractModel):
    """Immutable accepted intake, recording the resolved Release and Bundle."""

    schema_version: Literal["1.0.0"]
    task_id: RecordId
    request_id: RecordId
    idempotency_scope: IdempotencyScope
    idempotency_key: ShortText
    media_ref: MediaRef
    context: TaskContext
    release_id: RecordId
    bundle_digest: Digest
    accepted_at: UTCDateTime
    status: Literal[TaskStatus.ACCEPTED]
    trace: TraceContext

    @field_validator("status", mode="before")
    @classmethod
    def decode_status(cls, value: object) -> TaskStatus:
        return TaskStatus._decode(value)

    def validate_media(self, asset: MediaAsset) -> None:
        """Validate available media metadata before N01 builds a context."""
        asset = MediaAsset.model_validate(asset)
        if asset.media_ref != self.media_ref or not isinstance(
            asset.stage, FinalizedAsset
        ):
            raise ValueError("accepted task requires the exact finalized media asset")


class RegistryRecordRef(ContractModel):
    """Reference to exact externally supplied device or camera registry metadata."""

    record_id: RecordId
    version: Version
    digest: Digest


def _discriminate_context_source(value: object) -> object:
    if isinstance(value, dict) and "source" in value:
        return {**value, "source": ContextProvenanceSource._decode(value["source"])}
    return value


class _ContextProvenance(ContractModel):
    source: ContextProvenanceSource

    @model_validator(mode="before")
    @classmethod
    def decode_source(cls, value: object) -> object:
        return _discriminate_context_source(value)


class TaskMetadataProvenance(_ContextProvenance):
    source: Literal[ContextProvenanceSource.TRUSTED_TASK_METADATA]
    task_id: RecordId


class DeviceRegistryProvenance(_ContextProvenance):
    source: Literal[ContextProvenanceSource.DEVICE_REGISTRY]
    device_ref: RegistryRecordRef


class CameraRegistryProvenance(_ContextProvenance):
    source: Literal[ContextProvenanceSource.CAMERA_REGISTRY]
    camera_ref: RegistryRecordRef


class RegionDefinitionProvenance(_ContextProvenance):
    source: Literal[ContextProvenanceSource.REGION_DEFINITION]
    region_ref: RegionContentRef


class DeclaredDerivationProvenance(_ContextProvenance):
    source: Literal[ContextProvenanceSource.DECLARED_DERIVATION]
    derivation_id: ArtifactId
    implementation_version: Version
    implementation_digest: Digest
    input_selectors: Annotated[
        tuple[ContextFieldSelector, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_derivation_inputs(self) -> Self:
        if len({item.stable_json() for item in self.input_selectors}) != len(
            self.input_selectors
        ):
            raise ValueError("derivation input selectors must be unique")
        return self


type ContextValueProvenance = Annotated[
    TaskMetadataProvenance
    | DeviceRegistryProvenance
    | CameraRegistryProvenance
    | RegionDefinitionProvenance
    | DeclaredDerivationProvenance,
    Field(discriminator="source"),
    BeforeValidator(_discriminate_context_source),
]


type ContextValuePath = Annotated[
    tuple[FieldName, ...],
    Field(min_length=1, max_length=8),
    BeforeValidator(immutable_sequence),
]


class ContextValue(ContractModel):
    field_path: ContextValuePath
    value: Value
    provenance: ContextValueProvenance


class ContextSnapshot(ContractModel):
    """A successful N01 snapshot pinned to the exact resolved input identities."""

    schema_version: Literal["1.0.0"]
    context_snapshot_id: RecordId
    execution_id: RecordId
    task_id: RecordId
    release_id: RecordId
    bundle_digest: Digest
    media_ref: MediaRef
    device_ref: RegistryRecordRef
    camera_ref: RegistryRecordRef
    region_ref: RegionContentRef
    context_schema_ref: ContextSchemaRef
    context_provenance_policy_ref: PolicyContentRef
    clock: ClockProvenance
    values: Annotated[
        tuple[ContextValue, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]
    created_at: UTCDateTime

    @model_validator(mode="after")
    def provenance_owned_by_snapshot(self) -> Self:
        paths = sorted(item.field_path for item in self.values)
        for left, right in pairwise(paths):
            if right[: len(left)] == left:
                raise ValueError("context value paths cannot duplicate or overlap")
        for item in self.values:
            match item.provenance:
                case TaskMetadataProvenance(task_id=task_id):
                    if task_id != self.task_id:
                        raise ValueError("context value refers to a different Task")
                case DeviceRegistryProvenance(device_ref=device_ref):
                    if device_ref != self.device_ref:
                        raise ValueError(
                            "context value uses a different device registry revision"
                        )
                case CameraRegistryProvenance(camera_ref=camera_ref):
                    if camera_ref != self.camera_ref:
                        raise ValueError(
                            "context value uses a different camera registry revision"
                        )
                case RegionDefinitionProvenance(region_ref=region_ref):
                    if region_ref != self.region_ref:
                        raise ValueError(
                            "context value uses a different Region version"
                        )
        return self
