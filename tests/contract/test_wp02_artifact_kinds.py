"""Each inventory kind uses its approved path and authoritative root validator."""

import json

import pytest

from jimu_runtime.bundle.artifacts import ARTIFACT_LAYOUTS, SchemaResource
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from tests.fixtures.wp02.artifacts.support import (
    FIXTURES,
    VERSION,
    Payloads,
    digest,
    frozen,
    schema,
)

RAW_PATHS = {
    "response_schema": "prompts/response_contracts/fixture.schema.json",
    "attribute_schema": "schemas/attributes/fixture.schema.json",
    "context_schema": "schemas/context/fixture.schema.json",
    "tool_input_schema": "schemas/tools/inputs/fixture.schema.json",
    "tool_output_schema": "schemas/tools/outputs/fixture.schema.json",
    "object_schema": "schemas/objects/fixture.schema.json",
    "decision_schema": "schemas/decisions/fixture.schema.json",
}
DEFINITION_VECTORS = [
    ("entity", "ontology/entities.json", "s1", "entity", "entity_id"),
    ("region", "ontology/regions.json", "s1", "region", "region_id"),
    ("profile", "capabilities/profiles.json", "o1", "profile", "profile_id"),
    ("facet", "capabilities/facets.json", "o1", "facet", "facet_id"),
    ("question", "capabilities/questions.json", "o1", "question", "question_id"),
    ("tool", "tools/registry.json", "m1", "tool", "tool_id"),
    ("tool_plan", "tools/plans.json", "m1", "plan", "tool_plan_id"),
    ("model_binding", "models/bindings.json", "m1", "model", "model_binding_id"),
    (
        "fact_schema",
        "schemas/facts/definitions/fixture.json",
        "a2",
        "fact_schema",
        "schema_id",
    ),
    (
        "prompt_template",
        "prompts/templates/fixture.json",
        "o1",
        "prompt",
        "prompt_template_id",
    ),
    ("vocabulary", "vocabularies/fixture.json", "s1", "vocabulary", "vocabulary_id"),
    (
        "fact_producer_catalog",
        "facts/producers.json",
        "x2",
        "catalogue",
        "catalogue_id",
    ),
]


def test_frozen_kind_set_and_inventory_coverage():
    special = {
        "rule",
        "rule_set",
        "policy",
        "geometry_resource",
        "static_asset",
        "value_schema",
    }
    assert {kind.value for kind in ArtifactKind} == (
        set(RAW_PATHS) | {vector[0] for vector in DEFINITION_VECTORS} | special
    )
    assert len(ARTIFACT_LAYOUTS) == 25
    assert set(ARTIFACT_LAYOUTS) == set(ArtifactKind)


@pytest.mark.parametrize("kind,path,batch,key,id_key", DEFINITION_VECTORS)
def test_definition_roots_and_paths(kind, path, batch, key, id_key):
    bundle = Payloads()
    document = frozen(batch, key)
    ref = bundle.add(path, kind, document[id_key], document)
    (resolved,) = bundle.catalogue().artifacts
    assert resolved.ref.digest == ref["digest"]
    assert getattr(resolved.value, id_key) == document[id_key]


@pytest.mark.parametrize("kind,path", RAW_PATHS.items())
def test_raw_resources_use_the_pinned_locator_without_a_new_id_format(kind, path):
    # A project may use an independently chosen versioned HTTPS or URN alias.
    # No URL or fixed URN syntax overrides the pinned inventory identity.
    bundle = Payloads()
    alias = f"https://offline.example/definitions/v{VERSION}/{kind}.json"
    content = schema(alias, type="boolean")
    bundle.add(path, kind, "inventory-id", content)
    (resolved,) = bundle.catalogue().artifacts
    assert isinstance(resolved.value, SchemaResource)
    assert resolved.value.resource_id == alias
    assert resolved.value.document == content


@pytest.mark.parametrize(
    "kind,path,name,id_key",
    [
        ("rule", "rules/registry.json", "rule-valid.json", "rule_id"),
        ("rule_set", "rules/sets.json", "rule-set-valid.json", "rule_set_id"),
        (
            "geometry_resource",
            "resources/geometry/fixture.json",
            "geometry-valid.json",
            "geometry_resource_id",
        ),
    ],
)
def test_additional_frozen_definition_roots(kind, path, name, id_key):
    directory = (
        "geometry_replay" if kind == "geometry_resource" else "rule_declarations"
    )
    document = json.loads((FIXTURES / "wp01c" / directory / name).read_bytes())
    bundle = Payloads()
    bundle.add(path, kind, document[id_key], document)
    (resolved,) = bundle.catalogue().artifacts
    assert getattr(resolved.value, id_key) == document[id_key]


@pytest.mark.parametrize(
    "subtype,filename",
    [
        ("execution", "execution"),
        ("evidence", "evidence"),
        ("coverage", "coverage"),
        ("resource_requirements", "resources"),
        ("tool_skip", "registry"),
    ],
)
def test_typed_policy_locations(subtype, filename):
    document = frozen("x1", "artifacts")[subtype]
    bundle = Payloads()
    bundle.add(f"policies/{filename}.json", "policy", document["policy_id"], document)
    (resolved,) = bundle.catalogue().artifacts
    assert resolved.value.policy.policy_kind.value == subtype


def test_value_schema_id_uses_the_existing_independent_generator_contract():
    definition = FactSchemaDefinition.model_validate(frozen("a2", "fact_schema"))
    generated = generate_value_schema(
        definition.type_definition, definition.value_schema_generation
    )
    document = json.loads(generated.canonical_schema_bytes)
    bundle = Payloads()
    bundle.add(
        "schemas/values/generated.schema.json",
        "value_schema",
        generated.child_ref.artifact_id,
        document,
        version=generated.child_ref.version,
    )
    (resolved,) = bundle.catalogue().artifacts
    assert resolved.ref.digest == generated.child_ref.digest
    assert resolved.canonical_bytes == generated.canonical_schema_bytes


def test_static_asset_descriptor_covers_exact_data_bytes_with_json_extension():
    bundle = Payloads()
    data_path = "resources/assets/data/region/ledger.json"
    data = b'{"rows": []}\r\n'
    descriptor = {
        "schema_version": VERSION,
        "asset_id": "ledger",
        "version": VERSION,
        "data_path": data_path,
        "sha256": digest(data),
        "mime_type": "application/json",
        "byte_size": len(data),
    }
    bundle.add(
        "resources/assets/ledger.descriptor", "static_asset", "ledger", descriptor
    )
    bundle.files[data_path] = data
    (resolved,) = bundle.catalogue().artifacts
    assert resolved.asset_bytes == data
    assert resolved.value.data_path == data_path
    assert resolved.ref.digest == digest(resolved.canonical_bytes)
