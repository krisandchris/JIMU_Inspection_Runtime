import json
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import (
    SourceArtifact,
    SourceAssetBytes,
    SourceIdentity,
    compile_bundle,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.producers import ProducerCatalogue
from tests.fixtures.wp02.compiler.scenario import (
    LIMITS,
    OUTSIDE,
    PARKED,
    approve_source_bytes,
    compile_environment,
    source_scenario,
)


def test_synthetic_parking_compile_emits_typed_closed_bundle(tmp_path):
    result = compile_bundle(
        source_scenario(), limits=LIMITS, environment=compile_environment(tmp_path)
    )
    assert result.succeeded, result.errors
    package = result.bundle
    assert package is not None
    verified = read_bundle(package.archive_bytes, LIMITS)
    assert verified.manifest.integrity.root_digest == package.bundle_ref.bundle_digest
    assert verified.manifest.integrity.signatures == ()
    assert result.semantic_checks
    assert all(row.status in {"passed", "deferred"} for row in result.semantic_checks)
    catalogue = build_catalogue(
        verified.manifest,
        verified.payloads,
        max_bytes=LIMITS.max_file_bytes,
        max_depth=LIMITS.max_json_depth,
    )
    fact_schemas = catalogue.of_kind(ArtifactKind.FACT_SCHEMA)
    assert len(fact_schemas) == 2
    assert all(isinstance(item.value, FactSchemaDefinition) for item in fact_schemas)
    assert len({row.value.payload_schema_ref for row in fact_schemas}) == 1
    assert len(catalogue.of_kind(ArtifactKind.VALUE_SCHEMA)) == 1
    (producer,) = catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    assert isinstance(producer.value, ProducerCatalogue)
    assert {
        row.fact_key: row.producer_kind.value for row in producer.value.declarations
    } == {
        PARKED: "vlm_question",
        OUTSIDE: "tool",
    }


def test_fixed_metadata_and_artifact_order_control_determinism(tmp_path):
    source = source_scenario()
    options = dict(limits=LIMITS, environment=compile_environment(tmp_path))
    first = compile_bundle(source, **options)
    reordered = replace(source, artifacts=tuple(reversed(source.artifacts)))
    second = compile_bundle(reordered, **options)
    updated = replace(
        source,
        metadata=replace(
            source.metadata,
            created_at=source.metadata.created_at + timedelta(seconds=1),
        ),
    )
    third = compile_bundle(updated, **options)
    assert first.bundle is not None, first.errors
    assert second.bundle == first.bundle
    assert third.bundle is not None, third.errors
    assert (
        third.bundle.bundle_ref.bundle_digest != first.bundle.bundle_ref.bundle_digest
    )


def test_payload_change_propagates_through_transitive_content_refs(tmp_path):
    source = source_scenario()
    shifted = replace(
        source,
        artifacts=tuple(
            replace(
                item,
                content=item.content.replace(
                    b"Inspect synthetic parking state",
                    b"Inspect new synthetic parking state",
                ),
            )
            if item.kind is ArtifactKind.QUESTION
            else item
            for item in source.artifacts
        ),
    )
    options = dict(limits=LIMITS, environment=compile_environment(tmp_path))
    initial = compile_bundle(source, **options)
    revised = compile_bundle(approve_source_bytes(shifted), **options)
    assert initial.bundle is not None, initial.errors
    assert revised.bundle is not None, revised.errors
    assert (
        initial.bundle.bundle_ref.bundle_digest
        != revised.bundle.bundle_ref.bundle_digest
    )

    def pins(result):
        verified = read_bundle(result.bundle.archive_bytes, LIMITS)
        return {
            (
                row.content_ref.artifact_kind,
                row.content_ref.artifact_id,
            ): row.content_ref.digest
            for row in verified.manifest.artifacts
        }

    old_pins, new_pins = pins(initial), pins(revised)
    assert set(old_pins) == set(new_pins)
    changed = {
        kind
        for (kind, identity), digest in old_pins.items()
        if digest != new_pins[kind, identity]
    }
    assert changed == {
        ArtifactKind.QUESTION,
        ArtifactKind.POLICY,
        ArtifactKind.RULE,
        ArtifactKind.RULE_SET,
        ArtifactKind.PROFILE,
        ArtifactKind.FACT_PRODUCER_CATALOG,
    }


def test_static_asset_data_passes_through_as_exact_bytes(tmp_path):
    source = source_scenario()
    raw_asset = b'{ "inventory": [] }\r\n'
    path = "resources/assets/data/ledger.json"
    descriptor = SourceArtifact(
        kind=ArtifactKind.STATIC_ASSET,
        artifact_id="fixture.ledger",
        version="1.0.0",
        path="resources/assets/ledger.json",
        origin=SourceIdentity(artifact_id="ledger-descriptor", version="1.0.0"),
        content=json.dumps(
            {
                "schema_version": "1.0.0",
                "version": "1.0.0",
                "asset_id": "fixture.ledger",
                "data_path": path,
                "sha256": sha256(raw_asset).hexdigest(),
                "mime_type": "application/json",
                "byte_size": len(raw_asset),
            }
        ).encode(),
    )
    extended = replace(
        source,
        artifacts=(*source.artifacts, descriptor),
        assets=(
            SourceAssetBytes(
                path=path,
                content=raw_asset,
                origin=SourceIdentity(artifact_id="ledger-original", version="1.0.0"),
            ),
        ),
    )
    report = compile_bundle(
        approve_source_bytes(extended),
        limits=LIMITS,
        environment=compile_environment(tmp_path),
    )
    assert report.bundle is not None, report.errors
    verified = read_bundle(report.bundle.archive_bytes, LIMITS)
    assert verified.payloads[path] == raw_asset
    assert not any(entry.path == path for entry in verified.manifest.artifacts)
    assert any(entry.path == path for entry in verified.checksums.entries)
