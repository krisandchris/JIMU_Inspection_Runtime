"""Compile the local synthetic scene with the installed JIMU Runtime."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from pydantic import TypeAdapter

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.artifacts import DRAFT_2020_12, ArtifactIdentity
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.compiler import (
    BundleBuildMetadata,
    BundleCompileInput,
    ReferenceSlot,
    SourceArtifact,
    SourceAssetBytes,
    SourceIdentity,
)
from jimu_runtime.bundle.integrity import (
    JSONLocation,
    canonical_json,
    canonicalize_json,
    sha256_digest,
)
from jimu_runtime.bundle.trust import TrustConfiguration, TrustPin, TrustPolicy
from jimu_runtime.domain.definition_support import ExternalCapabilityRequirement
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.runtime.bundle_manifest import (
    CompatibilityRequirement,
    SourceArtifactRef,
    SourceProvenance,
)
from jimu_runtime.domain.runtime.tool_executions import ToolResult

DIRECTORY = Path(__file__).resolve().parent
VERSION = "1.0.0"
SOURCE = "synthetic.nonmotor.parking.source"
START = datetime(2026, 1, 1, tzinfo=UTC)
AT = datetime(2026, 1, 2, tzinfo=UTC)
END = datetime(2026, 1, 3, tzinfo=UTC)
LIMITS = BundleLimits(
    max_archive_bytes=512 * 1024,
    max_expanded_bytes=2 * 1024 * 1024,
    max_file_bytes=256 * 1024,
    max_files=128,
    max_json_depth=48,
)


class _JSONLimits(TypedDict):
    max_bytes: int
    max_depth: int


JSON_LIMITS: _JSONLimits = {
    "max_bytes": LIMITS.max_file_bytes,
    "max_depth": LIMITS.max_json_depth,
}
QUESTIONS = ("vehicle_type", "parking_state", "parking_position")
STEPS = ("wheel_segmentation", "tactile_paving_segmentation", "wheel_boundary_geometry")
PROFILE = "nonmotor_vehicle"
REGION = "synthetic_nonmotor_parking"
RULE = "nonmotor_vehicle_illegal_parking"


def _extract_slots(
    value: JSONValue, slots: list[ReferenceSlot], path: JSONLocation = ()
) -> JSONValue:
    if isinstance(value, dict):
        if "$pin" in value:
            target = value["$pin"]
            if (
                not isinstance(target, list)
                or len(target) != 3
                or not all(isinstance(item, str) for item in target)
                or set(value) - {"$pin", "schema_ref"}
                or type(value.get("schema_ref", False)) is not bool
            ):
                raise ValueError(f"Invalid scene pin: {path}")
            kind, identifier, version = target
            assert isinstance(kind, str)
            assert isinstance(identifier, str)
            assert isinstance(version, str)
            slots.append(
                ReferenceSlot(
                    location=path,
                    target=(ArtifactKind(kind), identifier, version),
                    schema_ref=value.get("schema_ref") is True,
                )
            )
            return None
        return {
            key: _extract_slots(child, slots, (*path, key))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _extract_slots(child, slots, (*path, index))
            for index, child in enumerate(value)
        ]
    return value


def _output_contract(
    artifact_id: str, payload: JSONValue, evidence_names: list[str]
) -> dict[str, JSONValue]:
    """Use the authoritative ToolResult status envelope with scene-owned slots."""
    schema = TypeAdapter[ToolResult](ToolResult).json_schema()
    for branch in ("SucceededToolResult", "PartialToolResult", "FailedToolResult"):
        properties = schema["$defs"][branch]["properties"]
        if branch != "FailedToolResult":
            properties["payload"] = payload
        if not evidence_names:
            properties["evidence"] = {"type": "array", "maxItems": 0}
            continue
        properties["evidence"] = {
            "type": "array",
            "maxItems": 64,
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"const": name},
                            "evidence_refs": {
                                "$ref": "#/$defs/EvidenceRefs",
                                "minItems": 1,
                                "maxItems": 1,
                            },
                        },
                        "required": ["name", "evidence_refs"],
                    }
                    for name in evidence_names
                ]
            },
            "allOf": [
                {
                    "contains": {"properties": {"name": {"const": name}}},
                    "minContains": int(branch == "SucceededToolResult"),
                    "maxContains": 1,
                }
                for name in evidence_names
            ],
        }
    return {
        "$schema": DRAFT_2020_12,
        "$id": f"urn:jimu:synthetic:parking:{artifact_id}:{VERSION}",
        **schema,
    }


def _scene_bytes(subdirectory: str, location: str) -> bytes:
    fragment = Path(location)
    root = (DIRECTORY / subdirectory).resolve(strict=True)
    if (
        fragment.is_absolute()
        or ".." in fragment.parts
        or root != DIRECTORY / subdirectory
    ):
        raise ValueError(
            f"Scene file is outside its {subdirectory} directory: {location}"
        )
    resolved = (root / fragment).resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(
            f"Scene file is outside its {subdirectory} directory: {location}"
        )
    return resolved.read_bytes()


def _scene_json(raw: bytes, location: str) -> JSONValue:
    # Validate original bytes before expanding ToolResult or replacing pin slots.
    return cast(
        JSONValue, json.loads(canonicalize_json(raw, path=location, **JSON_LIMITS))
    )


def _index() -> dict[str, JSONValue]:
    document = _scene_json(_scene_bytes("source", "index.json"), "source/index.json")
    if not isinstance(document, dict):
        raise ValueError("Scene index requires an object")
    if document["schema_version"] != VERSION:
        raise ValueError("Unsupported scene source version")
    return document


def _rows(value: object) -> list[dict[str, str]]:
    # The index is authored alongside the example and is used only by this loader.
    return TypeAdapter(list[dict[str, str]]).validate_python(value, strict=True)


def _source_artifact(index_row: dict[str, str]) -> SourceArtifact:
    kind = ArtifactKind(index_row["kind"])
    identifier = index_row["id"]
    raw = _scene_bytes("source", index_row["file"])
    slots: list[ReferenceSlot] = []
    value = _scene_json(raw, index_row["file"])
    if not isinstance(value, dict):
        raise ValueError("Scene source artifact requires an object")
    if kind is ArtifactKind.TOOL_OUTPUT_SCHEMA:
        names = TypeAdapter(list[str]).validate_python(
            value["evidence_slots"], strict=True
        )
        compiled_bytes = canonical_json(
            _output_contract(identifier, value["payload"], names),
            path=index_row["path"],
            **JSON_LIMITS,
        )
    else:
        value = _extract_slots(value, slots)
        compiled_bytes = (
            canonical_json(value, path=index_row["path"], **JSON_LIMITS)
            if slots
            else raw
        )
    # The compiler compares SourceArtifactRef.digest to the null-slot input.
    # Content-address the origin separately to bind the authored bytes and slot targets.
    origin_material: JSONValue = {
        "format": "jimu.synthetic-scene-source.v1",
        "artifact": [kind.value, identifier, VERSION],
        "source_path": index_row["file"],
        "bundle_path": index_row["path"],
        "raw_sha256": sha256_digest(raw),
        "slots": [
            {
                "location": list(slot.location),
                "target": [slot.target[0].value, slot.target[1], slot.target[2]],
                "schema_ref": slot.schema_ref,
            }
            for slot in slots
        ],
    }
    digest = sha256_digest(canonical_json(origin_material, **JSON_LIMITS))
    return SourceArtifact(
        kind=kind,
        artifact_id=identifier,
        version=VERSION,
        path=index_row["path"],
        content=compiled_bytes,
        origin=SourceIdentity(artifact_id=f"scene-source.{digest}", version=VERSION),
        reference_slots=tuple(slots),
    )


def with_source_provenance(source: BundleCompileInput) -> BundleCompileInput:
    """Declare exact compiler inputs for this wholly synthetic source."""
    inputs: tuple[SourceArtifact | SourceAssetBytes, ...] = (
        *source.artifacts,
        *source.assets,
    )
    provenance = SourceProvenance(
        source_system=SOURCE,
        artifacts=tuple(
            SourceArtifactRef(
                artifact_id=item.origin.artifact_id,
                version=item.origin.version,
                digest=sha256_digest(item.content),
            )
            for item in inputs
        ),
    )
    return replace(source, metadata=replace(source.metadata, source=provenance))


def load_source() -> BundleCompileInput:
    """Load only the scene's co-located versioned source and its Region asset."""
    index = _index()
    requirements = TypeAdapter(list[ExternalCapabilityRequirement])
    return with_source_provenance(
        BundleCompileInput(
            metadata=BundleBuildMetadata(
                bundle_id="synthetic.nonmotor.parking",
                bundle_version=VERSION,
                compiler_version=VERSION,
                created_at=START,
                source=SourceProvenance(source_system=SOURCE, artifacts=()),
                compatibility=CompatibilityRequirement(
                    minimum_runtime_version_inclusive=VERSION,
                    maximum_runtime_version_exclusive="2.0.0",
                ),
                required_capabilities=tuple(
                    requirements.validate_python(index["capabilities"])
                ),
                producer_catalogue_id="synthetic.nonmotor.parking.producers",
                producer_catalogue_version=VERSION,
            ),
            artifacts=tuple(_source_artifact(row) for row in _rows(index["artifacts"])),
            assets=tuple(
                SourceAssetBytes(
                    path=row["path"],
                    content=_scene_bytes("assets", row["file"]),
                    origin=SourceIdentity(
                        artifact_id=f"asset-bytes:{row['id']}", version=VERSION
                    ),
                )
                for row in _rows(index["raw_assets"])
            ),
        )
    )


class _SchemaIdentity(TypedDict):
    kind: str
    id: str
    version: str


class _ProtocolRow(TypedDict):
    input_schema: _SchemaIdentity
    output_schema: _SchemaIdentity
    role: NotRequired[str]


def capability_snapshot(source: BundleCompileInput) -> CapabilitySnapshot:
    """Host capability declarations for the local compile/admission demonstration."""
    index = _index()
    schema_sources: dict[ArtifactIdentity, SourceArtifact] = {}
    for item in source.artifacts:
        if item.kind not in (
            ArtifactKind.RESPONSE_SCHEMA,
            ArtifactKind.TOOL_INPUT_SCHEMA,
            ArtifactKind.TOOL_OUTPUT_SCHEMA,
        ):
            continue
        if item.identity in schema_sources:
            raise ValueError(f"Duplicate capability schema identity: {item.identity}")
        schema_sources[item.identity] = item
    protocols = TypeAdapter(list[_ProtocolRow])
    roles = protocols.validate_python(index["model_roles"], strict=True)
    tool_protocols = protocols.validate_python(index["tool_protocols"], strict=True)
    requirements = source.metadata.required_capabilities
    current = {"valid_from": START, "valid_until": END, "revoked_at": None}

    def schema_ref(declaration: _SchemaIdentity, kind: ArtifactKind) -> dict[str, str]:
        identity = (
            ArtifactKind(declaration["kind"]),
            declaration["id"],
            declaration["version"],
        )
        if identity[0] is not kind or identity not in schema_sources:
            raise ValueError(f"Unknown or wrongly typed capability schema: {identity}")
        item = schema_sources[identity]
        return {
            "artifact_kind": item.kind.value,
            "artifact_id": item.artifact_id,
            "version": item.version,
            "digest": sha256_digest(
                canonicalize_json(item.content, path=item.path, **JSON_LIMITS)
            ),
            "schema_dialect": DRAFT_2020_12,
        }

    def model_contract(row: _ProtocolRow) -> dict[str, object]:
        return {
            "input_schema_ref": schema_ref(
                row["input_schema"], ArtifactKind.RESPONSE_SCHEMA
            ),
            "output_schema_ref": schema_ref(
                row["output_schema"], ArtifactKind.RESPONSE_SCHEMA
            ),
        }

    def tool_contract(row: _ProtocolRow) -> dict[str, object]:
        return {
            "input_schema_ref": schema_ref(
                row["input_schema"], ArtifactKind.TOOL_INPUT_SCHEMA
            ),
            "output_schema_ref": schema_ref(
                row["output_schema"], ArtifactKind.TOOL_OUTPUT_SCHEMA
            ),
        }

    model_requirement = next(
        row for row in requirements if row.family.value == "model_deployment"
    )
    provisional = CapabilitySnapshot.model_validate(
        {
            "pin": {
                "identity": "synthetic.scene.capabilities",
                "version": VERSION,
                "sha256": "0" * 64,
            },
            "runtime_version": VERSION,
            "bundle_schema_versions": (VERSION,),
            "valid_from": START,
            "valid_until": END,
            "capabilities": tuple(
                {
                    "family": row.family,
                    "capability_id": row.capability_id,
                    "contract_version": row.contract_version,
                    "implementation_version": VERSION,
                    "supported_features": row.required_features,
                    "evaluations": (),
                    **current,
                }
                for row in requirements
            ),
            "model_deployments": (
                {
                    "identity": index["deployment"],
                    "supported_features": model_requirement.required_features,
                    "contracts": tuple(
                        {"capability": row["role"], **model_contract(row)}
                        for row in roles
                    ),
                    **current,
                },
            ),
            "model_reviews": (),
            "supported_workflow_versions": (VERSION,),
            "runtime_protocols": (
                *(
                    {
                        "role": row["role"],
                        **model_contract(row),
                        "protocol_version": VERSION,
                        **current,
                    }
                    for row in roles
                ),
                *(
                    {
                        "role": "tool_execution",
                        **tool_contract(row),
                        "protocol_version": VERSION,
                        **current,
                    }
                    for row in tool_protocols
                ),
            ),
        }
    )
    return provisional.model_copy(update={"pin": configuration_pin(provisional)})


def configuration_pin(
    configuration: CapabilitySnapshot | TrustConfiguration | TrustPolicy,
) -> TrustPin:
    """Address the normalized configuration body without its self-referential pin."""
    content = canonical_json(
        configuration.model_dump(mode="json", exclude={"pin"}), **JSON_LIMITS
    )
    return TrustPin(
        identity=configuration.pin.identity,
        version=configuration.pin.version,
        sha256=sha256_digest(content),
    )
