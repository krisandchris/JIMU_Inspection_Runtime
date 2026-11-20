"""Check N04 projection identity, original-media selection and historical views."""

import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from jimu_runtime.bundle.schema_validation import compile_offline_response_schema
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import QuestionContentRef
from jimu_runtime.domain.nodes.observation import N04_REGIONS_MARKER, ObjectImageView
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    ImageDimensions,
    MediaRef,
)
from jimu_runtime.domain.runtime.perception import MarkedGroundingFrame
from jimu_runtime.gateway.bridge import ModelCallFailure, _region_prompt_digest
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.inspection.perception import observation
from jimu_runtime.inspection.perception.model import ModelAccess
from jimu_runtime.workflow.worker import AdapterRejected


def test_regional_retry_changes_wire_but_preserves_operation_identity():
    image_digest = "c" * 64
    claimed = SimpleNamespace(
        opened=SimpleNamespace(
            scope=SimpleNamespace(
                execution_id="execution",
                pins=SimpleNamespace(release_id="release", bundle_digest="b" * 64),
                media=SimpleNamespace(media_ref=SimpleNamespace(sha256=image_digest)),
            )
        ),
        previous=SimpleNamespace(
            grounding=SimpleNamespace(
                objects=[
                    SimpleNamespace(
                        object_id="object.a", effective_question_refs=("question",)
                    ),
                    SimpleNamespace(
                        object_id="object.no-questions", effective_question_refs=()
                    ),
                    SimpleNamespace(
                        object_id="object.b", effective_question_refs=("question",)
                    ),
                ]
            )
        ),
        attempt=SimpleNamespace(attempt_number=1),
    )
    request = {
        "execution_id": "execution",
        "attempt_number": 1,
        "release_id": "release",
        "bundle_digest": "b" * 64,
        "image_sha256": image_digest,
        "prompt": "Look at both grounded regions.",
        "response_schema": {},
        "object_ids": ["object.a", "object.b"],
    }

    def call():
        wire = canonical_bytes(
            {"jimu_region_request": request, "jimu_projection_object": "object.a"}
        )
        return SimpleNamespace(
            prompt=f"{N04_REGIONS_MARKER}\n{wire.decode()}",
            object_ref=SimpleNamespace(record_id="object.a"),
        )

    initial = call()
    digest = _region_prompt_digest(claimed, initial)
    claimed.attempt.attempt_number = 2
    with pytest.raises(ModelCallFailure, match="region_request_pin_mismatch"):
        _region_prompt_digest(claimed, initial)
    request["attempt_number"] = 2
    retry = call()
    assert retry.prompt != initial.prompt
    assert _region_prompt_digest(claimed, retry) == digest
    for invalid in (
        ["object.a"],
        ["object.b", "object.a"],
        ["object.a", "object.b", "object.no-questions"],
        ["object.a", "object.a"],
    ):
        request["object_ids"] = invalid
        with pytest.raises(ModelCallFailure, match="region_request_pin_mismatch"):
            _region_prompt_digest(claimed, call())


def test_new_observation_uses_original_even_when_legacy_frame_exists():
    original = b"original"
    marked = b"marked"
    media_ref = MediaRef(media_id="media.original", sha256=sha256(original).hexdigest())
    frame = MarkedGroundingFrame(
        media_ref=MediaRef(media_id="media.marked", sha256=sha256(marked).hexdigest()),
        byte_size=len(marked),
        mime_type="image/png",
        renderer_version="1.0.0",
    )
    media = SimpleNamespace(
        media_ref=media_ref, mime_type="image/png", byte_size=len(original)
    )
    scope = SimpleNamespace(execution_id="execution", media=media)
    obj = SimpleNamespace(
        execution_id=scope.execution_id,
        source_media_ref=media_ref,
        marked_frame=frame,
        grounding_member_boxes=(
            BBoxXYXY(xyxy=(1.0, 2.0, 2.0, 3.0)),
            BBoxXYXY(xyxy=(2.0, 3.0, 3.0, 4.0)),
        ),
        bbox=BBoxXYXY(xyxy=(1.0, 2.0, 3.0, 4.0)),
        coordinate_system=CoordinateSystem(
            coordinate_system_id=media_ref.media_id,
            frame="original_image",
            dimensions=ImageDimensions(width=10, height=10),
        ),
    )

    async def load_original(_scope):
        return VerifiedImage(original, "image/png", media_ref.sha256)

    access = ModelAccess(load_original, "model")
    assert asyncio.run(access.observation_image(scope, obj)).content == original
    foreign = SimpleNamespace(
        execution_id=scope.execution_id, source_media_ref=frame.media_ref
    )
    with pytest.raises(
        AdapterRejected, match="perception.observation_image_scope_mismatch"
    ):
        asyncio.run(access.observation_image(scope, foreign))

    async def load_archived(_scope):
        return VerifiedImage(marked, "image/png", frame.media_ref.sha256)

    with pytest.raises(AdapterRejected, match="perception.original_media_mismatch"):
        asyncio.run(ModelAccess(load_archived, "model").observation_image(scope, obj))

    view = ObjectImageView.for_object(obj)
    assert view.model_dump() == {"roi": {"xyxy": obj.bbox.xyxy}, "transforms": ()}
    view.check_object(obj)
    view.check_object(obj, original_regions=True)
    archived = ObjectImageView.model_validate(
        {**view.model_dump(), "marked_media_ref": frame.media_ref}
    )
    archived.check_object(obj)
    with pytest.raises(ValueError, match="exact Object frame"):
        archived.check_object(obj, original_regions=True)

    assert observation._observation_model_input(scope, obj, include_members=True) == {
        "image": media_ref.model_dump(mode="json"),
        "bbox": {"xyxy": [1.0, 2.0, 3.0, 4.0]},
        "member_boxes": [
            {"xyxy": [1.0, 2.0, 2.0, 3.0]},
            {"xyxy": [2.0, 3.0, 3.0, 4.0]},
        ],
    }
    assert observation._observation_model_input(scope, obj, include_members=False) == {
        "image": media_ref.model_dump(mode="json"),
        "bbox": {"xyxy": [1.0, 2.0, 3.0, 4.0]},
    }


@pytest.mark.parametrize(
    ("policy_changes", "input_constraint", "error"),
    (
        ({}, None, None),
        (
            {
                "allowed_evidence_types": (
                    EvidenceType.MODEL_REQUEST,
                    EvidenceType.MODEL_RESPONSE,
                )
            },
            None,
            "region_evidence_not_permitted",
        ),
        (
            {
                "allowed_evidence_types": (
                    EvidenceType.ORIGINAL_IMAGE,
                    EvidenceType.MODEL_RESPONSE,
                )
            },
            None,
            "region_evidence_not_permitted",
        ),
        (
            {
                "allowed_evidence_types": (
                    EvidenceType.ORIGINAL_IMAGE,
                    EvidenceType.MODEL_REQUEST,
                )
            },
            None,
            "region_evidence_not_permitted",
        ),
        ({"access_scope": "task"}, None, "incompatible_region_evidence_policies"),
        (
            {"retention_class": "restricted"},
            None,
            "incompatible_region_evidence_policies",
        ),
        (
            {"retention_duration_seconds": 7200},
            None,
            "incompatible_region_evidence_policies",
        ),
        ({}, "max_coordinate", "invalid_region_model_input"),
        ({}, "unknown_required_field", "invalid_region_model_input"),
        ({}, "member_fallback", None),
        ({}, "undeclared_members", None),
    ),
)
def test_regional_prompt_validates_every_sibling(
    monkeypatch, policy_changes, input_constraint, error
):
    common = {
        "allowed_evidence_types": (
            EvidenceType.ORIGINAL_IMAGE,
            EvidenceType.MODEL_REQUEST,
            EvidenceType.MODEL_RESPONSE,
        ),
        "access_scope": "project",
        "retention_class": "standard",
        "retention_duration_seconds": 3600,
    }
    policy_refs = {
        name: PolicyContentRef(
            artifact_kind=ArtifactKind.POLICY,
            artifact_id=f"evidence.{name}",
            version="1.0.0",
            digest="a" * 64,
        )
        for name in ("first", "sibling")
    }
    profile = SimpleNamespace(observation_binding_ref="binding")
    definitions = {
        "profile": profile,
        **{
            policy_ref: SimpleNamespace(policy=SimpleNamespace(**policy))
            for policy_ref, policy in zip(
                policy_refs.values(),
                (common, {**common, **policy_changes}),
                strict=True,
            )
        },
    }
    question_refs = [
        QuestionContentRef(
            artifact_kind=ArtifactKind.QUESTION,
            artifact_id=name,
            version="1.0.0",
            digest="b" * 64,
        )
        for name in policy_refs
    ]
    objects = []
    selections = []
    for ref in question_refs:
        name = ref.artifact_id
        definitions[ref] = SimpleNamespace(evidence_policy_ref=policy_refs[name])
        objects.append(
            SimpleNamespace(
                profile_ref="profile",
                object_id=f"object.{name}",
                entity_ref=SimpleNamespace(artifact_id="target"),
                bbox=BBoxXYXY(xyxy=(1.0, 2.0, 10.0, 20.0 if name == "first" else 30.0)),
                marked_frame=SimpleNamespace(
                    media_ref=MediaRef(media_id="media.marked", sha256="e" * 64)
                ),
                grounding_member_boxes=(
                    (BBoxXYXY(xyxy=(1.0, 2.0, 10.0, 20.0)),)
                    if input_constraint == "undeclared_members" and name == "first"
                    else None
                ),
                question_selection_ref=SimpleNamespace(record_id=name),
                effective_question_refs=(ref,),
            )
        )
        selections.append(SimpleNamespace(selection_id=name))
    scope = SimpleNamespace(
        resolve=lambda ref, _model: definitions[ref],
        media=SimpleNamespace(
            media_ref=MediaRef(media_id="media.original", sha256="c" * 64),
            dimensions=ImageDimensions(width=64, height=64),
        ),
        pins=SimpleNamespace(release_id="release", bundle_digest="d" * 64),
        execution_id="execution",
    )
    records = SimpleNamespace(
        grounding=SimpleNamespace(objects=objects),
        discovery=SimpleNamespace(selections=selections),
    )
    coordinates = {"type": "number"}
    if input_constraint == "max_coordinate":
        coordinates["maximum"] = 20
    model_properties = {
        "image": {
            "type": "object",
            "properties": {
                "media_id": {"const": "media.original"},
                "sha256": {"type": "string"},
            },
            "required": ["media_id", "sha256"],
            "additionalProperties": False,
        },
        "bbox": {
            "type": "object",
            "properties": {"xyxy": {"type": "array", "items": coordinates}},
            "required": ["xyxy"],
            "additionalProperties": False,
        },
    }
    if input_constraint == "unknown_required_field":
        model_properties["camera_profile"] = {"type": "string"}
    include_members = input_constraint == "member_fallback"
    if include_members:
        model_properties["member_boxes"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": model_properties["bbox"],
        }
    validator_builds = []

    def input_validator():
        validator_builds.append(True)
        return compile_offline_response_schema(
            {
                "type": "object",
                "properties": model_properties,
                "required": list(model_properties),
                "additionalProperties": False,
            }
        )

    binding = SimpleNamespace(
        template=N04_REGIONS_MARKER,
        input_validator=input_validator,
        definition=SimpleNamespace(
            output_schema_ref=SimpleNamespace(schema_dialect="draft-2020-12"),
        ),
    )
    monkeypatch.setattr(observation, "_check_object_configuration", lambda *args: None)
    monkeypatch.setattr(observation, "_question_prompt", lambda *args: "Describe.")
    monkeypatch.setattr(
        observation, "_declares_member_boxes", lambda *args: include_members
    )
    monkeypatch.setattr(
        observation,
        "schema_resource",
        lambda *args: SimpleNamespace(
            document={"properties": {name: {"type": "boolean"} for name in policy_refs}}
        ),
    )

    if error is None:
        prompt = observation._regions_prompt(
            scope, records, objects[0], None, "snapshot", profile, binding
        )
        assert '"object_ids":["object.first","object.sibling"]' in prompt
        physical_prompt = json.loads(prompt.split("\n", maxsplit=1)[1])["prompt"]
        physical_input = json.loads(physical_prompt.rsplit("\n", maxsplit=1)[1])
        if input_constraint == "undeclared_members":
            assert physical_input["member_boxes"] == {
                "r1": [[1, physical_input["boxes"]["r1"]]]
            }
        else:
            assert "member_boxes" not in physical_input
    else:
        with pytest.raises(AdapterRejected, match=error):
            observation._regions_prompt(
                scope, records, objects[0], None, "snapshot", profile, binding
            )
    assert len(validator_builds) == 1
