"""Reject type, byte, aggregate and offline-reference counterexamples."""

import urllib.request

import pytest

from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.bundle.closure import validate_closure
from tests.fixtures.wp02.artifacts.support import (
    VERSION,
    Payloads,
    add_entity,
    add_schema,
    applicability,
    digest,
    frozen,
    schema,
    vocabulary,
    wire,
)


def _reject(call, code):
    with pytest.raises(BundleArtifactError) as captured:
        call()
    assert captured.value.code == code
    return captured.value


@pytest.mark.parametrize("location", ["/00", "/0/identity", "/-"])
def test_aggregate_requires_an_exact_direct_member_pointer(location):
    bundle = Payloads()
    document = vocabulary()
    path = "vocabularies/fixture.json"
    bundle.add(path, "vocabulary", "types", document, pointer=location)
    bundle.files[path] = wire([document])
    _reject(bundle.catalogue, "aggregate_inventory_mismatch")


def test_aggregate_omission_and_uninventoried_payload_reject_independently():
    bundle = Payloads()
    path = "vocabularies/fixture.json"
    document = vocabulary()
    bundle.add(path, "vocabulary", "types", document, pointer="/0")
    bundle.files[path] = wire([document, vocabulary("missing")])
    _reject(bundle.catalogue, "aggregate_inventory_mismatch")

    bundle.files[path] = wire([document])
    bundle.files["resources/assets/data/untracked.bin"] = b"9"
    _reject(bundle.catalogue, "payload_unaccounted")


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"artifact_id": "different"}, "artifact_identity_mismatch"),
        ({"version": "2.0.0"}, "artifact_identity_mismatch"),
        ({"digest": "f" * 64}, "artifact_digest_mismatch"),
    ],
)
def test_inventory_identity_and_digest_must_match_stored_artifact(changes, expected):
    bundle = Payloads()
    item = bundle.add("vocabularies/fixture.json", "vocabulary", "types", vocabulary())
    item.update(changes)
    _reject(bundle.catalogue, expected)


def test_artifact_kind_has_its_own_path_and_schema():
    bundle = Payloads()
    bundle.add("ontology/entities.json", "vocabulary", "types", vocabulary())
    _reject(bundle.catalogue, "artifact_path_mismatch")

    bundle = Payloads()
    bundle.add("ontology/entities.json", "entity", "entity", {"arbitrary": "JSON"})
    _reject(bundle.catalogue, "artifact_schema_invalid")


def test_path_with_inventory_but_no_file_is_rejected():
    bundle = Payloads()
    bundle.add("vocabularies/fixture.json", "vocabulary", "types", vocabulary())
    bundle.files.clear()
    _reject(bundle.catalogue, "artifact_file_missing")


@pytest.mark.parametrize(
    "content, code",
    [
        ({"type": "string"}, "schema_dialect_mismatch"),
        (schema("relative"), "schema_id_invalid"),
        (schema("https://offline.test/schema#anchor"), "schema_id_invalid"),
        (schema("urn:offline:test", type="imaginary"), "schema_draft_invalid"),
    ],
)
def test_raw_schema_uses_draft_validation(content, code):
    bundle = Payloads()
    bundle.add(
        "schemas/context/fixture.schema.json", "context_schema", "fixture", content
    )
    _reject(bundle.catalogue, code)


def test_value_schema_id_cannot_claim_an_independent_inventory_identity():
    bundle = Payloads()
    bundle.add(
        "schemas/values/fixture.schema.json",
        "value_schema",
        "invented",
        schema(f"urn:jimu:value-schema:{VERSION}:{'f' * 64}", type="boolean"),
    )
    _reject(bundle.catalogue, "schema_identity_mismatch")


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"artifact_id": "missing"}, "reference_missing"),
        ({"version": "2.0.0"}, "reference_missing"),
        ({"digest": "f" * 64}, "reference_digest_mismatch"),
        ({"artifact_id": "context"}, "reference_kind_mismatch"),
    ],
)
def test_content_dependency_uses_exact_type_id_version_and_digest(changes, code):
    bundle = Payloads()
    vocab = bundle.add("vocabularies/fixture.json", "vocabulary", "types", vocabulary())
    add_schema(
        bundle,
        "schemas/context/fixture.schema.json",
        "context_schema",
        "context",
        "urn:context:1",
    )
    add_entity(bundle, {**vocab, **changes})
    error = _reject(lambda: validate_closure(bundle.catalogue()), code)
    assert error.path == "ontology/entities.json"


def test_vocabulary_ref_does_not_authorize_unlisted_member():
    bundle = Payloads()
    vocab = bundle.add(
        "vocabularies/fixture.json",
        "vocabulary",
        "types",
        vocabulary(members=("other",)),
    )
    add_entity(bundle, vocab)
    error = _reject(
        lambda: validate_closure(bundle.catalogue()), "vocabulary_member_missing"
    )
    assert error.json_location == ("entity_type", "member")


def test_unresolved_identity_selector_rejects_without_searching_other_versions():
    bundle = Payloads()
    doc = applicability("absent")
    bundle.add("policies/registry.json", "policy", doc["policy_id"], doc)
    error = _reject(lambda: validate_closure(bundle.catalogue()), "reference_missing")
    assert error.json_location == ("policy", "identity_constraints", 0)


def test_policy_kind_checked_by_path_and_by_reference_owner():
    policy = frozen("p1", "artifacts")["unknown"]
    bundle = Payloads()
    bundle.add("policies/coverage.json", "policy", policy["policy_id"], policy)
    _reject(bundle.catalogue, "policy_path_mismatch")

    bundle = Payloads()
    wrong = bundle.add("policies/registry.json", "policy", policy["policy_id"], policy)
    model = frozen("m1", "model")
    model["provider_policy_ref"] = wrong
    bundle.add(
        "models/bindings.json", "model_binding", model["model_binding_id"], model
    )
    error = _reject(
        lambda: validate_closure(bundle.catalogue()), "policy_kind_mismatch"
    )
    assert error.json_location == ("provider_policy_ref",)


@pytest.mark.parametrize(
    "stored,expected_code",
    [
        (None, "asset_data_missing"),
        (b"B", "asset_data_mismatch"),
        (b"AA", "asset_data_mismatch"),
    ],
)
def test_static_asset_descriptor_declares_the_exact_data_member(stored, expected_code):
    bundle = Payloads()
    descriptor = frozen("s1", "asset")
    descriptor.update(byte_size=1, sha256=digest(b"A"))
    bundle.add(
        "resources/assets/fixture.json",
        "static_asset",
        descriptor["asset_id"],
        descriptor,
    )
    if stored is not None:
        bundle.files[descriptor["data_path"]] = stored
    error = _reject(bundle.catalogue, expected_code)
    assert error.path == descriptor["data_path"]


@pytest.mark.parametrize(
    "ref, keywords, code",
    [
        ("https://offline.test/unavailable", {}, "schema_resource_missing"),
        ("#absent", {}, "schema_target_missing"),
        ("#/$defs/absent", {}, "schema_pointer_missing"),
        ("#/examples/0", {"examples": [{"type": "string"}]}, "schema_target_missing"),
        ("#/examples/0", {"examples": [True]}, "schema_target_missing"),
    ],
)
def test_schema_never_retrieves_or_treats_instance_data_as_subschema(
    monkeypatch, ref, keywords, code
):
    def attempted_fetch(*args, **kwargs):
        pytest.fail("Schema resolution attempted online access")

    monkeypatch.setattr(urllib.request, "urlopen", attempted_fetch)
    bundle = Payloads()
    add_schema(
        bundle,
        "schemas/context/fixture.schema.json",
        "context_schema",
        "context",
        "https://offline.test/contexts/1",
        **{"$ref": ref},
        **keywords,
    )
    error = _reject(lambda: validate_closure(bundle.catalogue()), code)
    assert error.json_location == ("$ref",)


@pytest.mark.parametrize(
    "right,code",
    [
        ("urn:test:a", "schema_id_ambiguous"),
        ("urn:test:b", "artifact_dependency_cycle"),
    ],
)
def test_schema_id_ambiguity_and_cross_resource_cycle_reject(right, code):
    bundle = Payloads()
    add_schema(
        bundle,
        "schemas/context/a.schema.json",
        "context_schema",
        "a",
        "urn:test:a",
        **{"$ref": "urn:test:b"},
    )
    add_schema(
        bundle,
        "schemas/context/b.schema.json",
        "context_schema",
        "b",
        right,
        **{"$ref": "urn:test:a"},
    )
    _reject(lambda: validate_closure(bundle.catalogue()), code)


def test_prompt_partials_cannot_be_an_unpinned_external_lookup():
    bundle = Payloads()
    response = add_schema(
        bundle,
        "prompts/response_contracts/fixture.schema.json",
        "response_schema",
        "synthetic",
        "urn:test:response",
    )
    prompt = frozen("o1", "prompt")
    prompt.update(variables=[], template_text="Text", response_schema_ref=response)
    prompt["partial_refs"] = [
        {
            "artifact_kind": "prompt_template",
            "artifact_id": "part-missing",
            "version": VERSION,
            "digest": "d" * 64,
        }
    ]
    bundle.add(
        "prompts/templates/fixture.json",
        "prompt_template",
        prompt["prompt_template_id"],
        prompt,
    )
    error = _reject(lambda: validate_closure(bundle.catalogue()), "reference_missing")
    assert error.json_location == ("partial_refs", 0)


@pytest.mark.parametrize("extra", [{}, [], "untracked"])
def test_aggregate_wrapper_cannot_hide_an_unclaimed_branch(extra):
    bundle = Payloads()
    path = "vocabularies/fixture.json"
    document = vocabulary()
    bundle.add(path, "vocabulary", "types", document, pointer="/known/0")
    bundle.files[path] = wire({"known": [document], "unknown": extra})
    _reject(bundle.catalogue, "aggregate_inventory_mismatch")


def test_parent_and_child_locators_cannot_both_name_a_member():
    bundle = Payloads()
    path = "vocabularies/fixture.json"
    parent, child = vocabulary("parent"), vocabulary("child")
    bundle.add(path, "vocabulary", "parent", parent, pointer="/group")
    bundle.add(path, "vocabulary", "child", child, pointer="/group/child")
    bundle.files[path] = wire({"group": {"child": child}})
    _reject(bundle.catalogue, "aggregate_inventory_mismatch")


def test_duplicate_anchor_inside_one_resource_is_ambiguous():
    bundle = Payloads()
    add_schema(
        bundle,
        "schemas/context/fixture.schema.json",
        "context_schema",
        "context",
        "urn:test:anchor",
        **{
            "$defs": {
                "first": {"$anchor": "duplicate", "type": "boolean"},
                "second": {"$dynamicAnchor": "duplicate", "type": "object"},
            }
        },
    )
    _reject(lambda: validate_closure(bundle.catalogue()), "schema_anchor_ambiguous")


def test_aggregate_diagnostic_names_the_specific_member_and_field():
    bundle = Payloads()
    path = "schemas/context/fixture.schema.json"
    good = schema("urn:bundle:working", type="boolean")
    bad = schema("urn:bundle:missing", **{"$ref": "urn:bundle:absent"})
    bundle.add(path, "context_schema", "working", good, pointer="/members/0")
    bundle.add(path, "context_schema", "missing", bad, pointer="/members/1")
    bundle.files[path] = wire({"members": [good, bad]})
    error = _reject(
        lambda: validate_closure(bundle.catalogue()), "schema_resource_missing"
    )
    assert error.path == path
    assert error.json_location == ("members", 1, "$ref")
