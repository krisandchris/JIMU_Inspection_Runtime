"""Pinned HTTP contract registration accepts exact admitted bytes only."""

import asyncio

import pytest

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    ReopenedScene,
    exercise_lifecycle,
)
from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.gateway.http_wire import VerifiedImage, WireError, strict_json
from jimu_runtime.inspection.perception import build_chat_contract
from jimu_runtime.inspection.perception.model import CandidateReply, ModelAccess
from jimu_runtime.workflow.worker import AdapterRejected
from tests.fixtures.wp05.perception import IMAGE, release_scope


@pytest.fixture(scope="module")
def admitted_scene(tmp_path_factory: pytest.TempPathFactory) -> ReopenedScene:
    return exercise_lifecycle(tmp_path_factory.mktemp("signed-perception-contracts"))


def test_exact_model_and_question_schema_pins(admitted_scene: ReopenedScene) -> None:
    scope = release_scope(admitted_scene)
    (profile_member,) = scope.catalogue.of_kind(ArtifactKind.PROFILE)
    profile = scope.resolve(profile_member.ref, ProfileDefinition)
    contract = build_chat_contract(
        scope, profile.observation_binding_ref, profile.question_refs
    )
    assert contract.response_schema_digest == contract.output_schema_ref.digest
    assert contract.question_slots == tuple(
        ref.artifact_id for ref in profile.question_refs
    )
    tampered_model = profile.observation_binding_ref.model_copy(
        update={"digest": "0" * 64}
    )
    with pytest.raises(BundleArtifactError, match="incorrect digest"):
        build_chat_contract(scope, tampered_model, profile.question_refs)
    tampered_question = profile.question_refs[0].model_copy(update={"digest": "f" * 64})
    with pytest.raises(BundleArtifactError, match="incorrect digest"):
        build_chat_contract(
            scope,
            profile.observation_binding_ref,
            (tampered_question, *profile.question_refs[1:]),
        )
    with pytest.raises(WireError, match="question_envelope"):
        build_chat_contract(
            scope, profile.observation_binding_ref, profile.question_refs[:1]
        )
    with pytest.raises(AdapterRejected, match="unsupported_model_contract"):
        build_chat_contract(scope, profile.observation_binding_ref)


def test_admitted_candidate_preserves_observed_numeric_scores(
    admitted_scene: ReopenedScene,
) -> None:
    scope = release_scope(admitted_scene)
    (candidate,) = (
        member
        for member in scope.catalogue.of_kind(ArtifactKind.MODEL_BINDING)
        if member.ref.artifact_id == "candidate_prediction"
    )
    contract = build_chat_contract(
        scope, ModelBindingContentRef.model_validate(candidate.ref.model_dump())
    )

    for raw in (
        '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":1.25}]}',
        '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":-2}]}',
        '{"categories":[]}',
    ):
        assert contract.validate_response_content(
            raw, strict_json(raw, exact_numbers=True)
        ) == ((), ())
    assert CandidateReply.model_validate_json(raw).categories == []
    assert (
        CandidateReply.model_validate_json(
            '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":1.25}]}'
        )
        .categories[0]
        .score
        == 1.25
    )
    for raw in (
        '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking"}]}',
        '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":"1.25"}]}',
        '{"categories":[{"category_id":"nonmotor_vehicle_illegal_parking","score":NaN}]}',
    ):
        with pytest.raises(WireError):
            contract.validate_response_content(
                raw, strict_json(raw, exact_numbers=True)
            )


def test_raw_original_bytes_must_match_admitted_task(
    admitted_scene: ReopenedScene,
) -> None:
    scope = release_scope(admitted_scene)
    wrong = IMAGE[:-1] + bytes([IMAGE[-1] ^ 1])

    async def loader(requested):
        assert requested.task.idempotency_scope == scope.task.idempotency_scope
        return VerifiedImage(wrong, "image/png", requested.media.media_ref.sha256)

    with pytest.raises(AdapterRejected, match="original_media_mismatch"):
        asyncio.run(ModelAccess(loader, "model").image(scope))


def test_optional_context_conflict_only_reports_optional_issue(
    admitted_scene, monkeypatch
):
    from dataclasses import replace
    from types import SimpleNamespace

    from jimu_runtime.domain.definition_enums import ContextProvenanceSource
    from jimu_runtime.domain.definitions.ontology import RegionDefinition
    from jimu_runtime.domain.policies import ContextProvenancePolicyArtifact
    from jimu_runtime.domain.runtime.facts import PolicyContentRef
    from jimu_runtime.domain.schema_types import FieldSelector
    from jimu_runtime.domain.tool_mappings import RegionContentRef
    from jimu_runtime.domain.validation.schema_descriptors import resolve_field
    from jimu_runtime.domain.values import RecordValue, validate_typed_value
    from jimu_runtime.inspection.perception import context as builder

    scope = release_scope(admitted_scene)
    policy_ref = PolicyContentRef.model_validate(
        admitted_scene.execution.policy.context_provenance_policy_ref.model_dump()
    )
    original = scope.resolve(policy_ref, ContextProvenancePolicyArtifact)
    (entry,) = original.policy.fields
    (member,) = scope.catalogue.of_kind(ArtifactKind.REGION)
    region_ref = RegionContentRef.model_validate(member.ref.model_dump())
    region = scope.resolve(region_ref, RegionDefinition)
    opened = SimpleNamespace(scope=scope, region_ref=region_ref, region=region)
    inputs = SimpleNamespace(
        context_schema_ref=entry.schema_ref,
        context_provenance_policy_ref=policy_ref,
        region_ref=region_ref,
    )

    raw_area = builder._region_inputs(opened)["parking_area"]
    assert isinstance(raw_area, dict)
    conflicting = {**raw_area, "region_version": "1.0.2"}
    definition = resolve_field(
        FieldSelector(schema_ref=entry.schema_ref, field_path=entry.field_path),
        scope.schema_descriptors,
    ).type_definition
    metadata = RecordValue(
        kind="record",
        value={
            "parking_area": validate_typed_value(
                definition, builder._typed_raw(definition, conflicting)
            )
        },
    )
    task = scope.task.model_copy(
        update={"context": scope.task.context.model_copy(update={"metadata": metadata})}
    )
    opened.scope = replace(scope, task=task)
    sources = (
        ContextProvenanceSource.TRUSTED_TASK_METADATA,
        ContextProvenanceSource.REGION_DEFINITION,
    )
    alternate = entry.model_copy(update={"allowed_sources": sources})
    registration = original.model_copy(
        update={"policy": original.policy.model_copy(update={"fields": (alternate,)})}
    )
    before = type(scope).resolve

    def resolve(bound, ref, model):
        return (
            registration
            if ref == policy_ref and model is ContextProvenancePolicyArtifact
            else before(bound, ref, model)
        )

    monkeypatch.setattr(type(scope), "resolve", resolve)
    projected = builder._fields(opened, inputs)
    assert not projected.values and not projected.required
    assert [item.code for item in projected.optional] == [
        "perception.context_source_conflict"
    ]


@pytest.mark.parametrize("failure", ("timeout", "missing", "corrupt"))
def test_candidate_image_read_preserves_advisory_boundary(
    admitted_scene, monkeypatch, failure
):
    from types import SimpleNamespace

    from jimu_runtime.inspection.media.types import MediaError
    from jimu_runtime.inspection.perception import candidates

    scope = release_scope(admitted_scene)
    (binding,) = (
        member
        for member in scope.catalogue.of_kind(ArtifactKind.MODEL_BINDING)
        if member.ref.artifact_id == "candidate_prediction"
    )
    bound = ModelBindingContentRef.model_validate(binding.ref.model_dump())
    monkeypatch.setattr(candidates, "_target", lambda _: (SimpleNamespace(), set()))
    claimed = SimpleNamespace(opened=SimpleNamespace(scope=scope))

    async def load_image(_):
        if failure == "timeout":
            raise TimeoutError
        if failure == "missing":
            raise MediaError("media_file_unavailable")
        return VerifiedImage(
            b"x" * len(IMAGE), "image/png", scope.media.media_ref.sha256
        )

    access = ModelAccess(load_image, "model")
    if failure == "corrupt":
        with pytest.raises(AdapterRejected, match="original_media_mismatch"):
            asyncio.run(candidates.predict_candidates(claimed, None, access, bound))
    else:
        result = asyncio.run(
            candidates.predict_candidates(claimed, None, access, bound)
        )
        assert result == candidates.CandidateModelResult(
            (), None, "perception.candidate_image_unavailable"
        )
