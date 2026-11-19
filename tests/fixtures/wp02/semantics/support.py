"""Generate byte-verified semantic fixtures from the local synthetic scenario."""

import json
from dataclasses import replace

from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.closure import validate_closure
from jimu_runtime.bundle.compiler import (
    ReferenceSlot,
    SourceAssetBytes,
    SourceIdentity,
    compile_bundle,
)
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.bundle.semantic_inputs import build_definition_inputs
from jimu_runtime.bundle.semantics import validate_bundle_semantics
from jimu_runtime.domain.enums import ArtifactKind

from ..compiler.scenario import (
    LIMITS,
    PLAN,
    QUESTION,
    VEHICLE,
    _definition,
    _member,
    _ref,
    approve_source_bytes,
    capability_snapshot,
    compile_environment,
    source_scenario,
)
from ..trust.support import AT, VERSION

BUDGET = {"max_bytes": LIMITS.max_file_bytes, "max_depth": LIMITS.max_json_depth}


def changed_source(source, kind, identity, mutate, *, new_slots=(), remove_at=()):
    artifacts = []
    found = False
    for artifact in source.artifacts:
        if artifact.kind != kind or artifact.artifact_id != identity:
            artifacts.append(artifact)
            continue
        found = True
        data = json.loads(artifact.content)
        mutate(data)
        slots = tuple(
            item
            for item in artifact.reference_slots
            if not remove_at or item.location[: len(remove_at)] != remove_at
        )
        artifacts.append(
            replace(
                artifact,
                content=json.dumps(data).encode(),
                reference_slots=(*slots, *new_slots),
            )
        )
    assert found
    return refresh_source(source, tuple(artifacts))


def refresh_source(source, artifacts):
    return approve_source_bytes(replace(source, artifacts=artifacts))


def _context_mapping(source, kind, identity, prefix):
    def mutate(raw):
        current = raw
        for part in prefix:
            current = current[part]
        current["source"] = {
            "source_kind": "context",
            "selector": {
                "source": "context_snapshot",
                "schema_ref": None,
                "field_path": ["zone"],
            },
        }

    scope = (*prefix, "source")
    return changed_source(
        source,
        kind,
        identity,
        mutate,
        new_slots=(
            ReferenceSlot(
                location=(*scope, "selector", "schema_ref"),
                target=(ArtifactKind.CONTEXT_SCHEMA, "context", VERSION),
                schema_ref=True,
            ),
        ),
        remove_at=scope,
    )


def context_only_scenario():
    source = _context_mapping(
        source_scenario(),
        ArtifactKind.PROFILE,
        VEHICLE,
        ("grounding_binding", "input_bindings", 0),
    )
    return _context_mapping(
        source,
        ArtifactKind.TOOL_PLAN,
        PLAN,
        ("steps", 0, "input_bindings", 0),
    )


def question_context_scenario():
    return changed_source(
        source_scenario(),
        ArtifactKind.QUESTION,
        QUESTION,
        lambda data: data.update(
            context_allowlist=[
                {
                    "source": "region_metadata",
                    "schema_ref": None,
                    "field_path": ["zone"],
                }
            ]
        ),
        new_slots=(
            ReferenceSlot(
                location=("context_allowlist", 0, "schema_ref"),
                target=(ArtifactKind.CONTEXT_SCHEMA, "context", VERSION),
                schema_ref=True,
            ),
        ),
    )


def compile_fixture(tmp_path, source=None, snapshot=None):
    environment = compile_environment(tmp_path)
    if snapshot is not None:
        environment = replace(environment, capabilities=snapshot)
    return compile_bundle(
        source if source is not None else source_scenario(),
        limits=LIMITS,
        environment=environment,
    )


def reopen(report):
    assert report.bundle is not None, report.errors
    archive = read_bundle(report.bundle.archive_bytes, LIMITS)
    catalogue = build_catalogue(archive.manifest, archive.payloads, **BUDGET)
    closure = validate_closure(catalogue)
    return archive.manifest, catalogue, closure


def build_inputs(catalogue, closure):
    return build_definition_inputs(
        catalogue,
        closure,
        build_schema_descriptors(catalogue, schema_index=closure.schemas),
        reviews=(),
        validation_at=AT,
    )


def validate_reopened(report, snapshot=None):
    manifest, catalogue, closure = reopen(report)
    return validate_bundle_semantics(
        catalogue,
        closure,
        manifest,
        snapshot if snapshot is not None else capability_snapshot(),
        validation_at=AT,
        **BUDGET,
    )


def two_region_scenario(*, restrict_to_original=False, second_uses_same_schema=False):
    source = question_context_scenario()
    original = next(
        item for item in source.artifacts if item.kind is ArtifactKind.REGION
    )
    data = json.loads(original.content)
    data["region_id"] = "second"
    second = replace(
        original,
        artifact_id="second",
        origin=SourceIdentity(artifact_id="region:second", version=VERSION),
        content=json.dumps(data).encode(),
        reference_slots=tuple(
            replace(
                item, target=(ArtifactKind.CONTEXT_SCHEMA, "second-schema", VERSION)
            )
            if item.location == ("context_schema_ref",) and not second_uses_same_schema
            else item
            for item in original.reference_slots
        ),
    )
    context = next(
        item
        for item in source.artifacts
        if item.kind is ArtifactKind.CONTEXT_SCHEMA and item.artifact_id == "context"
    )
    schema = json.loads(context.content)
    schema["$id"] = "urn:jimu:fixture:second-schema"
    second_schema = replace(
        context,
        artifact_id="second-schema",
        origin=SourceIdentity(
            artifact_id="context_schema:second-schema", version=VERSION
        ),
        path="schemas/context/second-schema.schema.json",
        content=json.dumps(schema).encode(),
    )
    if restrict_to_original:
        source = changed_source(
            source,
            ArtifactKind.POLICY,
            "applicability",
            lambda raw: raw["policy"].update(
                identity_constraints=[
                    {
                        "artifact_kind": "region",
                        "artifact_id": original.artifact_id,
                        "version_selector": VERSION,
                    }
                ]
            ),
        )
    artifacts = (*source.artifacts, second, second_schema)
    return refresh_source(source, artifacts)


def prompt_context_scenario(*, declared=True):
    source = question_context_scenario() if declared else source_scenario()
    source = changed_source(
        source,
        ArtifactKind.POLICY,
        "context_provenance",
        lambda raw: raw["policy"].update(fields=[]),
        remove_at=("policy", "fields"),
    )
    return changed_source(
        source,
        ArtifactKind.PROMPT_TEMPLATE,
        QUESTION,
        lambda raw: raw.update(
            template_text="Read zone {zone}.",
            variables=[
                {
                    "name": "zone",
                    "source": "context",
                    "field_path": ["zone"],
                    "schema_ref": None,
                    "required": True,
                    "on_missing": "block",
                },
            ],
        ),
        new_slots=(
            ReferenceSlot(
                location=("variables", 0, "schema_ref"),
                target=(ArtifactKind.CONTEXT_SCHEMA, "context", VERSION),
                schema_ref=True,
            ),
        ),
    )


def geometry_scenario(*, unit_domain="unit"):
    source = source_scenario()
    path = "resources/assets/data/survey.bin"
    raw_asset = b"synthetic measured source"
    artifacts = (
        *source.artifacts,
        _definition(
            "vocabulary",
            "coords",
            "vocabularies/coords.json",
            "vocabulary_id",
            semantic_domain="coordinate_system",
            members=["xy"],
        ),
        _definition(
            "vocabulary",
            "units",
            "vocabularies/units.json",
            "vocabulary_id",
            semantic_domain=unit_domain,
            members=["metres"],
        ),
        _definition(
            "static_asset",
            "survey",
            "resources/assets/survey.json",
            "asset_id",
            data_path=path,
            sha256=sha256_digest(raw_asset),
            mime_type="application/octet-stream",
            byte_size=len(raw_asset),
        ),
        _definition(
            "geometry_resource",
            "parking-shape",
            "resources/geometry/parking-shape.json",
            "geometry_resource_id",
            coordinate_system=_member("coords", "xy"),
            units=_member("units", "metres"),
            geometry={
                "kind": "polygon",
                "vertices": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}],
            },
            provenance={"source_asset_ref": _ref("static_asset", "survey")},
        ),
    )
    source = approve_source_bytes(
        replace(
            source,
            artifacts=artifacts,
            assets=(
                SourceAssetBytes(
                    path=path,
                    content=raw_asset,
                    origin=SourceIdentity(
                        artifact_id="fixture-survey-bytes", version=VERSION
                    ),
                ),
            ),
        )
    )
    return changed_source(
        source,
        ArtifactKind.REGION,
        "test_parking",
        lambda raw: raw.update(geometry_ref=None, asset_refs=[None]),
        new_slots=(
            ReferenceSlot(
                location=("geometry_ref",),
                target=(ArtifactKind.GEOMETRY_RESOURCE, "parking-shape", VERSION),
            ),
            ReferenceSlot(
                location=("asset_refs", 0),
                target=(ArtifactKind.STATIC_ASSET, "survey", VERSION),
            ),
        ),
    )
