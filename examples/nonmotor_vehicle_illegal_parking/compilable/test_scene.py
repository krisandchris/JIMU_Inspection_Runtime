"""Offline checks for source provenance, semantic compilation and local reopening."""

import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from shutil import copytree

import pytest

from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    CompileEnvironment,
    CompileReport,
    attach_signature_refs,
    compile_bundle,
)
from jimu_runtime.bundle.integrity import (
    BundleIntegrityError,
    canonical_json,
    sha256_digest,
)
from jimu_runtime.bundle.schema_validation import (
    OfflineSchemaError,
    compile_offline_response_schema,
)
from jimu_runtime.bundle.trust import TrustError, verify_bundle_trust
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.policies import ConflictPolicyArtifact, RulePolicyArtifact
from jimu_runtime.domain.producers import ProducerCatalogue
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.decisions import CandidateCategory
from jimu_runtime.domain.schema_types import ToolResultSchemaDescriptor
from jimu_runtime.domain.tool_mappings import ContextInput, StepOutputInput
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    tool_output_omission_path,
    validate_independent_tool_outputs,
)
from jimu_runtime.gateway.http_wire import ChatContract, WireError, strict_json

from . import scene as scene_loader
from .lifecycle import ReopenedScene, exercise_lifecycle
from .local_trust import LocalEd25519, sign_bundle
from .scene import (
    AT,
    DIRECTORY,
    JSON_LIMITS,
    LIMITS,
    PROFILE,
    QUESTIONS,
    REGION,
    RULE,
    STEPS,
    capability_snapshot,
    configuration_pin,
    load_source,
    with_source_provenance,
)


@pytest.fixture(scope="module")
def scene(tmp_path_factory: pytest.TempPathFactory) -> ReopenedScene:
    return exercise_lifecycle(tmp_path_factory.mktemp("isolated_scene"))


def _compile(
    source: BundleCompileInput,
    tmp_path: Path,
    *,
    host_source: BundleCompileInput | None = None,
) -> CompileReport:
    return compile_bundle(
        source,
        limits=LIMITS,
        environment=CompileEnvironment(
            capabilities=capability_snapshot(host_source or source),
            trust=LocalEd25519(tmp_path).as_trust_source(),
            validation_at=AT,
        ),
    )


def _edited(
    source: BundleCompileInput,
    kind: ArtifactKind,
    identifier: str,
    edit: Callable[[dict[str, JSONValue]], None],
) -> BundleCompileInput:
    artifacts = list(source.artifacts)
    (position,) = [
        i
        for i, item in enumerate(artifacts)
        if item.kind == kind and item.artifact_id == identifier
    ]
    item = artifacts[position]
    body = json.loads(item.content)
    edit(body)
    artifacts[position] = replace(item, content=canonical_json(body, **JSON_LIMITS))
    return with_source_provenance(replace(source, artifacts=tuple(artifacts)))


def test_reproducible_bundle_and_reopened_capabilities(
    scene: ReopenedScene, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    second = _compile(load_source(), tmp_path).bundle
    assert second == scene.bundle
    assert scene.execution.pins.bundle_digest == scene.bundle.bundle_ref.bundle_digest
    assert scene.task.release_id == scene.release.release_id
    assert scene.activation.bundle_ref == scene.release.bundle_ref
    assert scene.current_activation.release_ref.record_id != scene.task.release_id
    assert scene.current_activation.bundle_ref.bundle_digest != scene.task.bundle_digest
    assert scene.execution.pins.release_id == scene.task.release_id
    assert (
        scene.execution.files.manifest.integrity.root_digest == scene.task.bundle_digest
    )
    assert (
        scene.execution.files.manifest.bundle_version
        == scene.bundle.bundle_ref.bundle_version
    )
    assert scene.execution.policy.node_order == (
        "N01",
        "N02",
        "N03",
        "N04",
        "N05",
        "N06",
    )
    assert {
        item.binding_ref.artifact_id for item in scene.execution.pins.model_pins
    } == {
        "profile_discovery",
        "object_grounding",
        "object_observation",
        "candidate_prediction",
    }
    assert {
        item.tool_ref.artifact_id for item in scene.execution.pins.tool_pins
    } == set(STEPS)


def test_explicit_primary_facts_and_spatial_ancestry(scene: ReopenedScene) -> None:
    catalogue = scene.execution.catalogue
    (profile,) = (item.value for item in catalogue.of_kind(ArtifactKind.PROFILE))
    assert isinstance(profile, ProfileDefinition)
    assert profile.profile_id == PROFILE
    assert tuple(ref.artifact_id for ref in profile.question_refs) == QUESTIONS
    assert profile.grounding_binding.kind == "model"
    assert profile.grounding_binding.model_binding_ref.artifact_id == "object_grounding"
    (producer_catalogue,) = (
        item.value for item in catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    )
    assert isinstance(producer_catalogue, ProducerCatalogue)
    assert {
        (item.producer_id, item.output_name, item.fact_key)
        for item in producer_catalogue.declarations
    } == {
        *((q, "primary", q) for q in QUESTIONS),
        (STEPS[0], "wheel_mask", "wheel_mask"),
        (STEPS[1], "tactile_paving_mask", "tactile_paving_mask"),
        (STEPS[2], "wheel_boundary_relation", "wheel_boundary_relation"),
    }
    plan = catalogue.resolve(profile.tool_plan_ref).value
    assert tuple(step.step_id for step in plan.steps) == STEPS
    assert plan.steps[-1].depends_on == STEPS[:2]
    (region,) = (item.value for item in catalogue.of_kind(ArtifactKind.REGION))
    assert isinstance(region, RegionDefinition) and region.region_id == REGION
    assert region.geometry_ref is not None
    geometry = catalogue.resolve(region.geometry_ref).value
    assert isinstance(geometry, GeometryResourceDefinition)
    assert geometry.units.member == "pixel"
    assert geometry.coordinate_system.member == "original_image"
    asset = catalogue.resolve(geometry.provenance.source_asset_ref)
    assert asset.ref.model_dump() == region.asset_refs[0].model_dump()
    assert asset.asset_bytes is not None
    asset_value = json.loads(asset.asset_bytes)
    assert geometry.geometry.model_dump(mode="json") == asset_value["geometry"]
    target_fields = {
        item.target.field_path: item.source for item in plan.steps[-1].input_bindings
    }
    region_source = target_fields[("parking_area",)]
    assert isinstance(region_source, ContextInput)
    assert region_source.selector.field_path == ("parking_area",)
    assert region_source.selector.schema_ref.digest == region.context_schema_ref.digest
    for step, name in zip(
        plan.steps[:2], ("wheel_mask", "tactile_paving_mask"), strict=True
    ):
        source = target_fields[(name,)]
        assert isinstance(source, StepOutputInput)
        tool = catalogue.resolve(step.tool_ref).value
        assert source.selector.schema_ref == tool.output_schema_ref
        assert source.step_id == step.step_id and source.selector.field_path == (name,)


def test_exact_rule_sources_and_mandatory_collect_all(scene: ReopenedScene) -> None:
    catalogue = scene.execution.catalogue
    (rule,) = (item.value for item in catalogue.of_kind(ArtifactKind.RULE))
    (ruleset,) = (item.value for item in catalogue.of_kind(ArtifactKind.RULE_SET))
    assert isinstance(rule, RuleDefinition) and rule.rule_id == RULE
    assert isinstance(ruleset, RuleSetDefinition)
    assert ruleset.candidate_binding_ref is not None
    assert ruleset.candidate_binding_ref.artifact_id == "candidate_prediction"
    policy = catalogue.resolve(rule.rule_policy_ref).value
    conflict = catalogue.resolve(ruleset.conflict_policy_ref).value
    assert isinstance(policy, RulePolicyArtifact)
    assert policy.policy.mandatory and policy.policy.scan == "all_mandatory"
    assert isinstance(conflict, ConflictPolicyArtifact)
    assert conflict.policy.mode == "collect_all" and not conflict.policy.early_exit
    question, geometry = rule.required_fact_selectors
    assert question.fact_key == "parking_state"
    assert question.producer_selector.output_name == "primary"
    assert geometry.fact_key == "wheel_boundary_relation"
    assert geometry.producer_selector.step_id == STEPS[-1]
    assert geometry.producer_selector.output_name == "wheel_boundary_relation"


def test_joint_wire_retains_valid_siblings_and_explicit_visibility(
    scene: ReopenedScene,
) -> None:
    model = next(
        pin
        for pin in scene.execution.pins.model_pins
        if pin.binding_ref.artifact_id == "object_observation"
    )
    binding = scene.execution.catalogue.resolve(model.binding_ref).value
    output = scene.execution.catalogue.resolve(binding.output_schema_ref)
    document = json.loads(output.canonical_bytes)
    offline = compile_offline_response_schema(document, QUESTIONS)
    wire = ChatContract(
        input_schema_ref=SchemaRef.model_validate(
            binding.input_schema_ref.model_dump()
        ),
        output_schema_ref=SchemaRef.model_validate(
            binding.output_schema_ref.model_dump()
        ),
        response_model=ContractModel,
        response_schema=output.canonical_bytes,
        question_slots=QUESTIONS,
    )
    examples = json.loads((DIRECTORY / "assets" / "model-wire.v1.json").read_bytes())
    known = examples["object_observation"]
    visibility = examples["object_observation_insufficient_visibility"]
    for result in (known, visibility):
        assert (
            offline.validate(result)
            == wire.validate_response_content(json.dumps(result), result)
            == ((), ())
        )
    missing = {"vehicle_type": known["vehicle_type"], "parking_state": "unknown"}
    assert (
        offline.validate(missing)
        == wire.validate_response_content(json.dumps(missing), missing)
        == (("parking_state",), ("parking_position",))
    )
    with pytest.raises(OfflineSchemaError, match="offline_schema_question_envelope"):
        offline.validate({**known, "object_id": "forged"})
    with pytest.raises(WireError, match="offline_schema_question_envelope"):
        wire.validate_response_content("", {**known, "object_id": "forged"})
    for fact in scene.execution.catalogue.of_kind(ArtifactKind.FACT_SCHEMA):
        if fact.ref.artifact_id in QUESTIONS:
            assert "insufficient_visibility" not in fact.value.type_definition.members
            assert "unknown" not in fact.value.type_definition.members


class _CandidateResponse(ContractModel):
    categories: list[CandidateCategory]


def test_candidate_responses_keep_actual_scores_and_rule_policy(
    scene: ReopenedScene,
) -> None:
    catalogue = scene.execution.catalogue
    (ruleset,) = (item.value for item in catalogue.of_kind(ArtifactKind.RULE_SET))
    assert isinstance(ruleset, RuleSetDefinition) and ruleset.candidate_binding_ref
    binding = catalogue.resolve(ruleset.candidate_binding_ref).value
    response = catalogue.resolve(binding.output_schema_ref)
    examples = json.loads((DIRECTORY / "assets" / "model-wire.v1.json").read_bytes())
    wire = ChatContract(
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        response_model=_CandidateResponse,
        response_schema=response.canonical_bytes,
    )
    empty = examples["candidate_prediction"]
    assert wire.validate_response_content(json.dumps(empty), empty) == ((), ())
    scored = examples["candidate_prediction_scored"]
    scored_content = json.dumps(scored)
    assert wire.validate_response_content(
        scored_content, strict_json(scored_content, exact_numbers=True)
    ) == ((), ())
    parsed = _CandidateResponse.model_validate_json(scored_content)
    assert parsed.categories[0].score == 1.25
    integral_float = '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":1.0}]}'
    assert wire.validate_response_content(
        integral_float, strict_json(integral_float, exact_numbers=True)
    ) == ((), ())
    assert (
        type(_CandidateResponse.model_validate_json(integral_float).categories[0].score)
        is float
    )
    negative = {"categories": [{"category_id": RULE, "score": -2.5}]}
    assert wire.validate_response_content(json.dumps(negative), negative) == ((), ())
    for bad in (
        {"category_id": RULE},
        {"category_id": RULE, "score": "1.25"},
        {"category_id": RULE, "score": True},
        {"category_id": RULE, "score": 1},
        {"category_id": RULE, "score": 1.25, "extra": 1},
        {"category_id": "unregistered", "score": 1.25},
    ):
        document = {"categories": [bad]}
        with pytest.raises(WireError):
            wire.validate_response_content(json.dumps(document), document)
    with pytest.raises(WireError, match="http_json_nonfinite"):
        strict_json(b'{"categories":[{"score":NaN}]}')

    (rule_ref,) = ruleset.rule_refs
    rule = catalogue.resolve(rule_ref).value
    policy = catalogue.resolve(rule.rule_policy_ref).value
    assert isinstance(policy, RulePolicyArtifact)
    assert policy.policy.mandatory and policy.policy.scan == "all_mandatory"


def test_independent_named_omission_and_evidence_slots(scene: ReopenedScene) -> None:
    descriptors = {
        item.schema_ref.artifact_id: item
        for item in scene.execution.schema_descriptors
        if isinstance(item, ToolResultSchemaDescriptor)
    }
    geometry = descriptors["wheel_boundary_geometry-result"]
    assert {field.name for field in geometry.evidence_fields} == {
        "wheel_mask",
        "tactile_paving_mask",
        "geometry",
    }
    assert all(
        field.required and not field.multiple for field in geometry.evidence_fields
    )
    for descriptor in descriptors.values():
        validate_independent_tool_outputs(descriptor)
        (output,) = descriptor.output_declarations
        assert tool_output_omission_path(descriptor, output.value_path, ()) == (
            output.output_name,
        )


def test_named_outputs_sharing_only_one_optional_parent_are_rejected(
    scene: ReopenedScene,
) -> None:
    example = next(
        item
        for item in scene.execution.schema_descriptors
        if isinstance(item, ToolResultSchemaDescriptor)
    )

    def declaration(name: str) -> dict[str, object]:
        return dict(output_name=name, value_path=("combined", name), required=True)

    def boolean_field(name: str) -> dict[str, object]:
        return dict(name=name, required=True, type_definition={"kind": "bool"})

    payload = {
        "kind": "record",
        "fields": [
            {
                "name": "combined",
                "required": False,
                "type_definition": {
                    "kind": "record",
                    "fields": [boolean_field("first"), boolean_field("second")],
                },
            }
        ],
    }
    mutant = ToolResultSchemaDescriptor.model_validate(
        {
            **example.model_dump(mode="json"),
            "payload_type": payload,
            "output_declarations": (declaration("first"), declaration("second")),
        }
    )
    with pytest.raises(SchemaCheckError, match="tool_output_omission_shared"):
        validate_independent_tool_outputs(mutant)


def test_region_asset_changes_require_a_new_matching_descriptor(tmp_path: Path) -> None:
    source = load_source()
    (asset,) = source.assets
    changed = replace(source, assets=(replace(asset, content=asset.content + b" "),))
    assert (
        _compile(changed, tmp_path).errors[0].code
        == "source_provenance_digest_mismatch"
    )
    assert _compile(with_source_provenance(changed), tmp_path).errors[0].code == (
        "asset_data_mismatch"
    )


def test_wrong_upstream_step_and_wrong_enum_are_rejected(tmp_path: Path) -> None:
    source = load_source()

    def swap_step(body: dict[str, JSONValue]) -> None:
        body["steps"][-1]["input_bindings"][0]["source"]["step_id"] = STEPS[1]

    def wrong_member(body: dict[str, JSONValue]) -> None:
        body["type_definition"]["members"].append("unknown")

    wrong_ancestor = _edited(
        source, ArtifactKind.TOOL_PLAN, "nonmotor_vehicle_geometry", swap_step
    )
    ancestry = _compile(wrong_ancestor, tmp_path)
    assert not ancestry.succeeded
    assert any("output Schema mismatch" in issue.message for issue in ancestry.errors)
    wrong_enum = _edited(
        source, ArtifactKind.FACT_SCHEMA, "parking_state", wrong_member
    )
    enum = _compile(wrong_enum, tmp_path)
    assert not enum.succeeded
    assert "semantic_question_response_incompatible" in {
        issue.code for issue in enum.errors
    }


def test_signature_over_wrong_root_is_rejected(
    scene: ReopenedScene, tmp_path: Path
) -> None:
    wrong_root = sha256_digest(b"another bundle root")
    verifier, ref = sign_bundle(tmp_path, wrong_root)
    signed = attach_signature_refs(scene.bundle.archive_bytes, (ref,), limits=LIMITS)
    files = read_bundle(signed, LIMITS)
    with pytest.raises(TrustError) as rejected:
        verify_bundle_trust(
            files.manifest,
            scene.bundle.bundle_ref.bundle_digest,
            verifier.as_trust_source(),
            validation_at=AT,
        )
    assert rejected.value.issue.code == "trust.scene_signature_invalid"
    assert not list(tmp_path.rglob("private.pem"))


@pytest.fixture
def local_scene_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    copytree(DIRECTORY / "source", tmp_path / "source")
    copytree(DIRECTORY / "assets", tmp_path / "assets")
    monkeypatch.setattr(scene_loader, "DIRECTORY", tmp_path)
    return tmp_path


def test_source_provenance_distinguishes_reference_slot_targets(
    local_scene_dir: Path,
) -> None:
    before = load_source()
    authored = local_scene_dir / "source" / "profile" / f"{PROFILE}.json"
    original = authored.read_bytes()
    altered = original.replace(b'"profile_discovery"', b'"object_grounding"', 1)
    assert altered != original
    authored.write_bytes(altered)
    after = load_source()
    old = next(item for item in before.artifacts if item.kind is ArtifactKind.PROFILE)
    new = next(item for item in after.artifacts if item.kind is ArtifactKind.PROFILE)
    assert new.content == old.content
    assert new.reference_slots != old.reference_slots
    assert new.origin.artifact_id != old.origin.artifact_id
    proof = next(
        row
        for row in after.metadata.source.artifacts
        if row.artifact_id == new.origin.artifact_id
    )
    assert proof.digest == sha256_digest(new.content)


@pytest.mark.parametrize(
    "location",
    ["source/index.json", "source/tool_output_schema/wheel_segmentation-result.json"],
)
def test_duplicate_source_json_is_rejected_before_normalizing(
    local_scene_dir: Path, location: str
) -> None:
    target = local_scene_dir / location
    original = target.read_text()
    target.write_text(original.replace("{", '{"duplicated":0,"duplicated":1,', 1))
    with pytest.raises(BundleIntegrityError) as error:
        load_source()
    assert error.value.code == "duplicate_json_key"


def test_reference_schema_type_requires_a_boolean(local_scene_dir: Path) -> None:
    target = local_scene_dir / "source" / "model_binding" / "object_observation.json"
    body = json.loads(target.read_bytes())
    body["input_schema_ref"]["schema_ref"] = 1
    target.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="Invalid scene pin"):
        load_source()


@pytest.mark.parametrize("row_kind", ["artifacts", "raw_assets"])
@pytest.mark.parametrize("escape_kind", ["absolute", "parent", "symlink"])
def test_source_paths_stay_inside_their_root(
    local_scene_dir: Path, row_kind: str, escape_kind: str
) -> None:
    root = local_scene_dir / ("source" if row_kind == "artifacts" else "assets")
    exterior = local_scene_dir / "outside.json"
    exterior.write_text("{}")
    if escape_kind == "absolute":
        escaped = str(exterior)
    elif escape_kind == "parent":
        escaped = "../outside.json"
    else:
        escaped = "outside-link.json"
        (root / escaped).symlink_to(exterior)
    source_index = local_scene_dir / "source" / "index.json"
    entries = json.loads(source_index.read_bytes())
    entries[row_kind][0]["file"] = escaped
    source_index.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match="outside its"):
        load_source()


def test_capability_pin_covers_schema_and_uses_exact_schema_identity() -> None:
    source = load_source()
    original = capability_snapshot(source)

    def change_response(body: dict[str, JSONValue]) -> None:
        body["description"] = "Changed response description"

    changed = _edited(
        source,
        ArtifactKind.RESPONSE_SCHEMA,
        "candidate_prediction-result",
        change_response,
    )
    revised = capability_snapshot(changed)
    assert revised.pin != original.pin
    assert revised.pin == configuration_pin(revised)
    tool_artifact = next(
        item for item in source.artifacts if item.kind == ArtifactKind.TOOL_INPUT_SCHEMA
    )
    model_artifact = next(
        item for item in source.artifacts if item.artifact_id == "model-input"
    )
    same_id_other_kind = replace(tool_artifact, artifact_id=model_artifact.artifact_id)
    extra = replace(source, artifacts=(*source.artifacts, same_id_other_kind))
    assert capability_snapshot(extra) == original
    duplicated = replace(source, artifacts=(*source.artifacts, model_artifact))
    with pytest.raises(ValueError, match="Duplicate capability schema identity"):
        capability_snapshot(duplicated)


def test_capability_schema_role_cannot_swap_reference_kind(
    local_scene_dir: Path,
) -> None:
    index = local_scene_dir / "source" / "index.json"
    body = json.loads(index.read_bytes())
    body["tool_protocols"][0]["input_schema"]["kind"] = "response_schema"
    index.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="wrongly typed capability schema"):
        capability_snapshot(load_source())


def test_trust_pins_bind_grants_and_the_ephemeral_domain_is_fixed(
    scene: ReopenedScene, tmp_path: Path
) -> None:
    verifier, _ = sign_bundle(tmp_path, scene.task.bundle_digest)
    with_grant = verifier.snapshot_at(AT)
    without_grant = replace(verifier, grant=None).snapshot_at(AT)
    assert with_grant is not None and without_grant is not None
    assert with_grant.configuration.pin != without_grant.configuration.pin
    assert with_grant.configuration.pin == configuration_pin(with_grant.configuration)
    policy = with_grant.configuration.policy
    assert policy.pin == configuration_pin(policy)
    assert policy.pin != configuration_pin(
        policy.model_copy(update={"minimum_distinct_signers": 2})
    )
    with pytest.raises(TypeError):
        LocalEd25519(tmp_path, environment="production")
    with pytest.raises(FrozenInstanceError):
        verifier.profile_id = "different-profile"


def test_verifier_rejects_mismatched_detached_digest_directly(
    scene: ReopenedScene, tmp_path: Path
) -> None:
    verifier, reference = sign_bundle(tmp_path, scene.task.bundle_digest)
    evidence = verifier.resolve(reference, max_bytes=64)
    assert evidence is not None and verifier.grant is not None
    bad = evidence.model_copy(
        update={"ref": reference.model_copy(update={"sha256": "0" * 64})}
    )
    with pytest.raises(TrustError) as rejected:
        verifier.verify(bad, verifier.grant, scene.task.bundle_digest)
    assert rejected.value.issue.code == "trust.scene_signature_digest"
