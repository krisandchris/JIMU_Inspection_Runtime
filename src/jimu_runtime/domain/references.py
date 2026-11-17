"""Content, identity-only and runtime references have distinct wire shapes."""

from typing import Literal

from pydantic import field_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version


class _ArtifactReference(ContractModel):
    artifact_kind: ArtifactKind

    @field_validator("artifact_kind", mode="before")
    @classmethod
    def decode_kind(cls, value: object) -> ArtifactKind:
        return ArtifactKind._decode(value)


class ContentRef(_ArtifactReference):
    artifact_id: ArtifactId
    version: Version
    digest: Digest


class IdentitySelector(_ArtifactReference):
    """Only approved Profile/Region applicability identity; never content authority."""

    artifact_kind: Literal[ArtifactKind.PROFILE, ArtifactKind.REGION]
    artifact_id: ArtifactId
    version_selector: Version


class RuntimeRecordRef(ContractModel):
    """Record identity, interpreted within its owning root's pinned scope.

    In particular an Object ID is Execution-local: this nested ref must not be
    resolved globally or reused across Executions. Scope/existence checks belong
    to the later owning record contracts, not a hidden resolver in this model.
    """

    record_kind: RuntimeRecordKind
    record_id: RecordId


class SchemaRef(ContentRef):
    """A constrained ContentRef, not a second schema identity system."""

    artifact_kind: Literal[
        ArtifactKind.FACT_SCHEMA,
        ArtifactKind.VALUE_SCHEMA,
        ArtifactKind.RESPONSE_SCHEMA,
        ArtifactKind.ATTRIBUTE_SCHEMA,
        ArtifactKind.CONTEXT_SCHEMA,
        ArtifactKind.TOOL_INPUT_SCHEMA,
        ArtifactKind.TOOL_OUTPUT_SCHEMA,
        ArtifactKind.OBJECT_SCHEMA,
        ArtifactKind.DECISION_SCHEMA,
    ]
    schema_dialect: Literal["https://json-schema.org/draft/2020-12/schema"]
