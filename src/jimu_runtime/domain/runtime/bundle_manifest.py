"""Bundle package declarations and exact pins, without file or trust resolution."""

import re
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StringConstraints, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    ExternalCapabilityRequirement,
    NonNegativeBudget,
    OptionalContract,
    UTCDateTime,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.references import ContentRef

# A package-wide inventory spans multiple artifact kinds and aggregates. Use the
# same cap for source rows and payload checksum rows so neither gains a lower
# structural limit just because the bundle stores one artifact per file.
_MAX_BUNDLE_INVENTORY_ROWS = 16_384
MAX_BUNDLE_CAPABILITIES = 1_024
# Signature envelopes describe a small set of signing identities per package.
_MAX_BUNDLE_SIGNATURES = 64

# Relative normalized POSIX payload names with the same unsafe character
# exclusions as StaticAssetDescriptor.data_path. The two root files are metadata.
_PAYLOAD_PATH_PATTERN = (
    r"^(?!(?:manifest|checksums)\.json$(?![\s\S]))(?!.*(?:^|/)[.]{1,2}(?:/|$))"
    r"[^\\\x00-\x1f\x7f-\x9f:?#/]+(?:/[^\\\x00-\x1f\x7f-\x9f:?#/]+)*$(?![\s\S])"
)


type BundlePayloadPath = Annotated[
    str,
    StringConstraints(
        strict=True, max_length=512, pattern=re.compile(_PAYLOAD_PATH_PATTERN)
    ),
]
type JsonPointer = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=1024,
        pattern=re.compile(r"^(?:/(?:[^~/]|~[01])*)*$(?![\s\S])"),
    ),
]
type CapabilityRequirements = Annotated[
    tuple[ExternalCapabilityRequirement, ...],
    Field(max_length=MAX_BUNDLE_CAPABILITIES),
    BeforeValidator(immutable_sequence),
]


class BundleRef(ContractModel):
    """Identity of one immutable logical Bundle, separate from artifact references."""

    bundle_id: ArtifactId
    bundle_version: Version
    bundle_digest: Digest


class SourceArtifactRef(ContractModel):
    """External source provenance, scoped by SourceProvenance.source_system."""

    artifact_id: RecordId
    version: Version
    digest: Digest


class SourceProvenance(ContractModel):
    source_system: ArtifactId
    artifacts: Annotated[
        tuple[SourceArtifactRef, ...],
        Field(max_length=_MAX_BUNDLE_INVENTORY_ROWS),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_artifacts(self) -> Self:
        identities = {(ref.artifact_id, ref.version) for ref in self.artifacts}
        if len(identities) != len(self.artifacts):
            raise ValueError("source artifact IDs and versions must be unique")
        return self


class CompatibilityRequirement(ContractModel):
    # Runtime range only. BundleManifest.required_capabilities is the sole list
    # of external capability requirements, independent of the Runtime version.
    minimum_runtime_version_inclusive: Version
    maximum_runtime_version_exclusive: Version

    @model_validator(mode="after")
    def nonempty_version_range(self) -> Self:
        minimum = tuple(map(int, self.minimum_runtime_version_inclusive.split(".")))
        maximum = tuple(map(int, self.maximum_runtime_version_exclusive.split(".")))
        if minimum >= maximum:
            raise ValueError("Runtime version range must be nonempty and ordered")
        return self


class DetachedSignatureRef(ContractModel):
    """Pin opaque signature evidence without declaring cryptographic trust."""

    signature_id: RecordId
    version: Version
    sha256: Digest
    signature_registry_id: ArtifactId


class IntegrityEnvelope(ContractModel):
    hash_algorithm: Literal["sha256"]
    canonicalization_id: Literal["jcs-sha256-v1"]
    root_digest: Digest
    signatures: Annotated[
        tuple[DetachedSignatureRef, ...],
        Field(max_length=_MAX_BUNDLE_SIGNATURES),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_signatures(self) -> Self:
        identities = {
            (ref.signature_registry_id, ref.signature_id, ref.version)
            for ref in self.signatures
        }
        if len(identities) != len(self.signatures):
            raise ValueError("signature registry, ID and version must be unique")
        return self


class ArtifactInventoryEntry(OptionalContract):
    content_ref: ContentRef
    path: BundlePayloadPath
    json_pointer: AbsentOptional[JsonPointer] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def pointer_addresses_json(self) -> Self:
        if self.json_pointer is not None and not self.path.endswith(".json"):
            raise ValueError("JSON Pointer requires a JSON payload path")
        return self


class BundleManifest(ContractModel):
    format: Literal["jimu.runtime-bundle"]
    schema_version: Literal["1.0.0"]
    bundle_id: ArtifactId
    bundle_version: Version
    compiler_version: Version
    created_at: UTCDateTime
    source: SourceProvenance
    artifacts: Annotated[
        tuple[ArtifactInventoryEntry, ...],
        Field(max_length=_MAX_BUNDLE_INVENTORY_ROWS),
        BeforeValidator(immutable_sequence),
    ]
    compatibility: CompatibilityRequirement
    required_capabilities: CapabilityRequirements
    integrity: IntegrityEnvelope

    @model_validator(mode="after")
    def unique_inventory(self) -> Self:
        require_unique_references(tuple(entry.content_ref for entry in self.artifacts))
        pointers_by_path: dict[str, set[str]] = {}
        for entry in self.artifacts:
            pointers = pointers_by_path.setdefault(entry.path, set())
            # The empty JSON Pointer also designates the entire document.
            pointer = entry.json_pointer or ""
            if pointer in pointers or (pointers and (not pointer or "" in pointers)):
                raise ValueError("artifact locators overlap or are duplicated")
            pointers.add(pointer)
        capabilities = {
            (item.family, item.capability_id, item.contract_version)
            for item in self.required_capabilities
        }
        if len(capabilities) != len(self.required_capabilities):
            raise ValueError("required capabilities must use unique identities")
        return self


class ChecksumEntry(ContractModel):
    path: BundlePayloadPath
    size: NonNegativeBudget
    sha256: Digest


class ChecksumManifest(ContractModel):
    """Versioned checksums.json data. File/hash comparison belongs to admission."""

    schema_version: Literal["1.0.0"]
    entries: Annotated[
        tuple[ChecksumEntry, ...],
        Field(max_length=_MAX_BUNDLE_INVENTORY_ROWS),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def sorted_unique_paths(self) -> Self:
        paths = [entry.path for entry in self.entries]
        if paths != sorted(set(paths)):
            raise ValueError("checksum entries must have unique paths in sorted order")
        return self
