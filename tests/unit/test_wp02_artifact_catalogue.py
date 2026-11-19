"""Positive closure and location evidence for Bundle artifacts."""

from jimu_runtime.bundle.artifacts import artifact_identity
from jimu_runtime.bundle.closure import validate_closure
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import SystemProducerRequirement
from jimu_runtime.domain.policies import DeploymentRequirement
from jimu_runtime.domain.references import ContentRef
from tests.fixtures.wp02.artifacts.support import (
    Payloads,
    add_entity,
    add_schema,
    digest,
    frozen,
    profile_closure,
    vocabulary,
    wire,
)


def _ref(value):
    return ContentRef.model_validate(value)


def _identity(value):
    return artifact_identity(_ref(value))


def test_ordered_array_aggregate_supplies_every_member_with_independent_digest():
    path = "ontology/entities.json"
    bundle = Payloads()
    vocab = bundle.add("vocabularies/types.json", "vocabulary", "types", vocabulary())
    first = frozen("s1", "entity")
    first.update(entity_type={"vocabulary_ref": vocab, "member": "synthetic"})
    first.update(lifecycle={"vocabulary_ref": vocab, "member": "synthetic"})
    first["attribute_schema_refs"] = []
    second = {**first, "entity_id": "entity-two"}
    first_ref = bundle.add(path, "entity", first["entity_id"], first, pointer="/0")
    second_ref = bundle.add(path, "entity", second["entity_id"], second, pointer="/1")
    bundle.files[path] = wire([first, second])

    catalogue = bundle.catalogue()
    assert catalogue.resolve(_ref(first_ref)).canonical_bytes == wire(first)
    assert catalogue.resolve(_ref(second_ref)).canonical_bytes == wire(second)
    closure = validate_closure(catalogue)
    assert closure.dependencies[_identity(first_ref)] == (_ref(vocab),)
    assert closure.dependencies[_identity(second_ref)] == (_ref(vocab),)


def test_object_aggregate_escaped_key_and_names_are_independent_of_identity():
    path = "vocabularies/types.json"
    bundle = Payloads()
    data = vocabulary("exact-id")
    ref = bundle.add(path, "vocabulary", "exact-id", data, pointer="/kind~1~0")
    bundle.files[path] = wire({"kind/~": data})
    catalogue = bundle.catalogue()
    assert catalogue.resolve(_ref(ref)).value.vocabulary_id == "exact-id"


def test_schema_offline_relative_resource_pointer_anchor_and_embedded_id():
    bundle = Payloads()
    context = add_schema(
        bundle,
        "schemas/context/metadata.schema.json",
        "context_schema",
        "context",
        "https://offline.test/bundle/context/metadata",
        **{
            "$defs": {
                "slash/field": {"type": "string", "$anchor": "local"},
                "embedded": {
                    "$id": "embedded",
                    "properties": {"bool": True},
                    "default": {"$ref": "https://not-a-resource.example/file"},
                },
            }
        },
    )
    response = add_schema(
        bundle,
        "prompts/response_contracts/response.schema.json",
        "response_schema",
        "response",
        "https://offline.test/bundle/prompts/response",
        allOf=[
            {"$ref": "../context/metadata#/$defs/slash~1field"},
            {"$ref": "../context/metadata#local"},
            {"$ref": "../context/embedded#/properties/bool"},
        ],
    )
    catalogue = bundle.catalogue()
    closure = validate_closure(catalogue)
    item = catalogue.resolve(_ref(response))
    assert closure.dependencies[_identity(response)] == (_ref(context),)
    for index in (0, 1):
        resolved = closure.schemas.resolve_reference(
            item, ("allOf", index), item.value.document["allOf"][index]["$ref"]
        )
        assert resolved.node["type"] == "string"
        assert resolved.location == ("$defs", "slash/field")
        # The returned node is an isolated view.
        resolved.node["type"] = "integer"
    assert (
        closure.schemas.resolve_reference(
            item, ("allOf", 2), "../context/embedded#/properties/bool"
        ).node
        is True
    )


def test_vocabulary_member_and_ordered_reverse_profile_relationships():
    bundle, profile, question, plan, app, prompt, partial = profile_closure()
    closure = validate_closure(bundle.catalogue())
    assert closure.question_profiles[_identity(question)] == (_ref(profile),)
    assert closure.tool_plan_profiles[_identity(plan)] == (_ref(profile),)
    assert _ref(app) in closure.dependencies[_identity(profile)]
    assert not closure.dependencies[_identity(app)]
    assert _ref(partial) in closure.dependencies[_identity(prompt)]
    external = [entry.requirement for entry in closure.external_references]
    assert (
        len([entry for entry in external if isinstance(entry, DeploymentRequirement)])
        == 2
    )
    assert len([entry for entry in external if isinstance(entry, ReviewReportRef)]) == 1


def test_same_digest_payload_does_not_require_a_runtime_record_lookup():
    bundle = Payloads()
    vocab = bundle.add("vocabularies/types.json", "vocabulary", "types", vocabulary())
    ref = add_entity(bundle, vocab)
    closure = validate_closure(bundle.catalogue())
    assert closure.dependencies[_identity(ref)] == (_ref(vocab),)
    assert not closure.external_references


def test_nested_aggregate_and_schema_member_use_the_full_locator():
    bundle = Payloads()
    path = "schemas/context/resources.schema.json"
    leaf = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:local:leaf",
        "type": "boolean",
    }
    head = {
        "$schema": leaf["$schema"],
        "$id": "urn:local:head",
        "$ref": "urn:local:leaf",
    }
    leaf_ref = bundle.add(
        path, "context_schema", "leaf", leaf, pointer="/groups/0/leaf"
    )
    head_ref = bundle.add(path, "context_schema", "head", head, pointer="/groups/1")
    bundle.files[path] = wire({"groups": [{"leaf": leaf}, head]})

    closure = validate_closure(bundle.catalogue())
    assert closure.dependencies[_identity(head_ref)] == (_ref(leaf_ref),)


def test_zip_integrity_and_typed_closure_jointly_account_for_raw_bytes():
    from jimu_runtime.bundle.archive import BundleLimits, read_bundle, write_archive
    from jimu_runtime.bundle.catalogue import build_catalogue
    from jimu_runtime.bundle.integrity import build_checksums, compute_root_digest

    bundle = Payloads()
    data = b'{"raw": 123}\r\n'
    descriptor = frozen("s1", "asset")
    descriptor.update(
        data_path="resources/assets/data/plain.json",
        byte_size=len(data),
        sha256=digest(data),
    )
    asset = bundle.add(
        "resources/assets/demo.json", "static_asset", descriptor["asset_id"], descriptor
    )
    bundle.files[descriptor["data_path"]] = data

    limits = BundleLimits(
        max_archive_bytes=100_000,
        max_expanded_bytes=100_000,
        max_file_bytes=100_000,
        max_files=10,
        max_json_depth=32,
    )
    manifest = bundle.manifest().model_dump(mode="json")
    checksums = build_checksums(bundle.files)
    manifest["integrity"]["root_digest"] = compute_root_digest(
        manifest, checksums, max_bytes=100_000, max_depth=32
    )
    packed = write_archive(
        {
            **bundle.files,
            "manifest.json": wire(manifest),
            "checksums.json": wire(checksums.model_dump(mode="json")),
        },
        limits,
    )
    verified = read_bundle(packed, limits)
    catalogue = build_catalogue(
        verified.manifest, verified.payloads, max_bytes=100_000, max_depth=32
    )
    assert catalogue.resolve(_ref(asset)).asset_bytes == data
    assert not validate_closure(catalogue).external_references


def test_system_producer_requirement_has_no_bundle_or_runtime_record_target():
    bundle, profile, *_ = profile_closure()
    (source_fact,) = [
        entry["content_ref"]
        for entry in bundle.entries
        if entry["content_ref"]["artifact_kind"] == ArtifactKind.FACT_SCHEMA.value
    ]
    declaration = frozen("x2", "system_object")
    declaration.update(profile_ref=profile, fact_schema_ref=source_fact)
    document = frozen("x2", "catalogue")
    document["declarations"] = [declaration]
    ref = bundle.add(
        "facts/producers.json",
        "fact_producer_catalog",
        document["catalogue_id"],
        document,
    )

    closure = validate_closure(bundle.catalogue())
    assert set(closure.dependencies[_identity(ref)]) == {
        _ref(profile),
        _ref(source_fact),
    }
    (external,) = [
        entry
        for entry in closure.external_references
        if isinstance(entry.requirement, SystemProducerRequirement)
    ]
    assert external.origin.ref == _ref(ref)
    assert external.location == ("declarations", 0, "producer", "requirement")
    assert external.requirement.capability_id == "system-context"
