"""Compile two host-qualified trial scenes through JIMU's authoritative Bundle compiler."""

import json
from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from examples.live_inspection.scenes.trusted_scope import (
    CAPTURE_IMAGE_SIZE,
    SCOPE_EVIDENCE_SHA256,
    SCOPE_IMAGE_SHA256,
    SIDEWALK_ZONE,
    scope_pins,
)
from examples.nonmotor_vehicle_illegal_parking.compilable import scene as base
from jimu_runtime.bundle.artifacts import ARTIFACT_LAYOUTS, DRAFT_2020_12
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    ReferenceSlot,
    SourceArtifact,
    SourceIdentity,
)
from jimu_runtime.bundle.integrity import (
    canonical_json,
    canonicalize_json,
    sha256_digest,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.nodes.observation import N04_REGIONS_MARKER

_DIRECTORY = Path(__file__).resolve().parent
_VERSION = base.VERSION
REGION_PROJECTION_BUNDLE_VERSION = "1.1.0"
K = ArtifactKind


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str
    members: list[str]
    matching_value: str
    prompt: str
    region_prompt: str


class _Scene(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scene_id: str
    region_id: str
    profile_id: str
    entity_name: str
    violation_category: str
    discovery_prompt: str
    grounding_prompt: str
    questions: list[_Question]
    trusted_scope_field: str = Field(min_length=1)


def _pin(kind: ArtifactKind, identifier: str, *, schema: bool = False) -> dict:
    pin = {"$pin": [kind.value, identifier, _VERSION]}
    return {**pin, "schema_ref": True} if schema else pin


def _template(kind: ArtifactKind, identifier: str) -> dict:
    path = base.DIRECTORY / "source" / kind.value / f"{identifier}.json"
    return json.loads(path.read_bytes())


def _source(
    scene: _Scene, kind: ArtifactKind, identifier: str, body: dict
) -> SourceArtifact:
    layout = ARTIFACT_LAYOUTS[kind]
    path = layout.location.replace("*", identifier)
    document: JSONValue = body
    if layout.identity_field:
        document = {**body, layout.identity_field: identifier}
    if kind in (K.CONTEXT_SCHEMA, K.RESPONSE_SCHEMA):
        document = {
            **body,
            "$schema": DRAFT_2020_12,
            "$id": f"urn:jimu:isolated:{scene.scene_id}:{kind.value}:{identifier}:{_VERSION}",
        }
    slots: list[ReferenceSlot] = []
    with_slots = canonical_json(document, path=path, **base.JSON_LIMITS)
    contents = canonical_json(
        base._extract_slots(document, slots), path=path, **base.JSON_LIMITS
    )
    return SourceArtifact(
        kind=kind,
        artifact_id=identifier,
        version=_VERSION,
        path=path,
        content=contents,
        origin=SourceIdentity(
            artifact_id=f"trial-source.{sha256_digest(with_slots)}", version=_VERSION
        ),
        reference_slots=tuple(slots),
    )


def _definition(**fields: JSONValue) -> dict:
    return {"schema_version": _VERSION, "version": _VERSION, **fields}


def _rule_selector(question: _Question) -> dict:
    return {
        "scope": "object",
        "fact_key": question.id,
        "fact_schema_ref": _pin(K.FACT_SCHEMA, question.id),
        "field_path": [],
        "producer_selector": {
            "producer_kind": "vlm_question",
            "question_ref": _pin(K.QUESTION, question.id),
            "output_name": "primary",
        },
    }


def _comparison(question: _Question) -> dict:
    return {
        "kind": "comparison",
        "operator": "eq",
        "left": {"kind": "fact_ref", "selector": _rule_selector(question)},
        "right": {
            "kind": "literal",
            "constant": {
                "type_definition": {"kind": "enum", "members": question.members},
                "value": {"kind": "enum", "value": question.matching_value},
            },
        },
    }


def _build(filename: str, *, region_projection: bool) -> BundleCompileInput:
    scene = _Scene.model_validate_json((_DIRECTORY / filename).read_bytes())
    original = base.load_source()
    facet_id = f"{scene.scene_id}.observations"
    rule_set_id = f"{scene.scene_id}.rules"
    context_id = f"{scene.scene_id}.context"
    question_refs = [_pin(K.QUESTION, question.id) for question in scene.questions]
    context_schema = _pin(K.CONTEXT_SCHEMA, context_id, schema=True)
    rule_applicability = "verified-scope-applicability"
    image_digest, evidence_digest = scope_pins(
        scene.region_id, scene.trusted_scope_field
    )
    excluded = {
        K.CONTEXT_SCHEMA,
        K.ENTITY,
        K.FACET,
        K.FACT_SCHEMA,
        K.GEOMETRY_RESOURCE,
        K.PROFILE,
        K.QUESTION,
        K.REGION,
        K.RULE,
        K.RULE_SET,
        K.STATIC_ASSET,
        K.TOOL,
        K.TOOL_INPUT_SCHEMA,
        K.TOOL_OUTPUT_SCHEMA,
        K.TOOL_PLAN,
    }
    edited_policy_ids = {
        "context-provenance",
        "geometry-evidence",
        "mask-evidence",
        "selection",
        "skip",
        "tool-execution",
        "tool-failure",
    }
    parking_ids = {
        "candidate_prediction-result",
        "entity-type",
        "geometry-unit",
        "object_grounding",
        "object_observation-result",
        "profile_discovery",
        "region-type",
        "scene-type",
        *(
            name
            for question_id in base.QUESTIONS
            for name in (question_id, f"{question_id}-result")
        ),
    }
    if region_projection:
        parking_ids.add("object_observation")
    artifacts = [
        item
        for item in original.artifacts
        if item.kind not in excluded
        and not (item.kind == K.POLICY and item.artifact_id in edited_policy_ids)
        and not (
            item.kind in (K.PROMPT_TEMPLATE, K.RESPONSE_SCHEMA, K.VOCABULARY)
            and item.artifact_id in parking_ids
        )
    ]

    def add(kind: ArtifactKind, identifier: str, document: dict) -> None:
        artifacts.append(_source(scene, kind, identifier, document))

    for identifier, member in (
        ("entity-type", "physical_object"),
        ("region-type", "camera_view"),
        ("scene-type", scene.scene_id),
    ):
        add(
            K.VOCABULARY,
            identifier,
            {**_template(K.VOCABULARY, identifier), "members": [member]},
        )

    entity = _template(K.ENTITY, base.PROFILE)
    add(
        K.ENTITY,
        scene.profile_id,
        {
            **entity,
            "name": scene.entity_name,
            "description": "Definition for an isolated runtime trial.",
        },
    )
    region = _template(K.REGION, base.REGION)
    region.pop("geometry_ref")
    region["region_type"]["member"] = "camera_view"
    region["scene_type"]["member"] = scene.scene_id
    add(
        K.REGION,
        scene.region_id,
        {
            **region,
            "name": f"Isolated camera view: {scene.scene_id}",
            "asset_refs": [],
            "context_schema_ref": _pin(K.CONTEXT_SCHEMA, context_id),
        },
    )

    context_policy = _template(K.POLICY, "context-provenance")
    expected_digests = {
        SCOPE_EVIDENCE_SHA256: evidence_digest,
        SCOPE_IMAGE_SHA256: image_digest,
    }
    properties = {
        scene.trusted_scope_field: {"type": "boolean"},
        **{
            field: {"type": "string", "enum": [digest]}
            for field, digest in expected_digests.items()
        },
    }
    context_policy["policy"]["fields"] = [
        {
            "field_path": [field],
            "schema_ref": context_schema,
            "required": False,
            "time_sensitive": False,
            "allowed_sources": ["trusted_task_metadata"],
        }
        for field in properties
    ]
    selectors = {
        field: {
            "source": "context_snapshot",
            "schema_ref": context_schema,
            "field_path": [field],
        }
        for field in properties
    }
    applicability = _template(K.POLICY, "applicability")
    applicability["policy"].update(
        context_allowlist=list(selectors.values()),
        condition_ast={
            "kind": "all",
            "arguments": [
                {
                    "kind": "context_ref",
                    "selector": selectors[scene.trusted_scope_field],
                },
                *(
                    {
                        "kind": "comparison",
                        "operator": "eq",
                        "left": {"kind": "context_ref", "selector": selectors[field]},
                        "right": {
                            "kind": "literal",
                            "constant": {
                                "type_definition": {
                                    "kind": "enum",
                                    "members": [digest],
                                },
                                "value": {"kind": "enum", "value": digest},
                            },
                        },
                    }
                    for field, digest in expected_digests.items()
                ),
            ],
        },
        description="Require the trusted scope assertion, pinned capture digest, and pinned scope evidence revision.",
    )
    add(K.POLICY, rule_applicability, applicability)
    add(
        K.CONTEXT_SCHEMA,
        context_id,
        {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
    )
    add(K.POLICY, "context-provenance", context_policy)

    profile = _template(K.PROFILE, base.PROFILE)
    add(
        K.PROFILE,
        scene.profile_id,
        {
            **profile,
            "entity_ref": _pin(K.ENTITY, scene.profile_id),
            "facet_refs": [_pin(K.FACET, facet_id)],
            "question_refs": question_refs,
            "rule_set_refs": [_pin(K.RULE_SET, rule_set_id)],
            "tool_plan_ref": _pin(K.TOOL_PLAN, "no-image-tools"),
            "grounding_grouping": {"mode": "per_box"},
        },
    )
    add(
        K.FACET,
        facet_id,
        _definition(
            name="Independent object observations",
            description="The selected observations concern only the current object.",
            fact_schema_refs=[
                _pin(K.FACT_SCHEMA, question.id) for question in scene.questions
            ],
        ),
    )
    add(K.TOOL_PLAN, "no-image-tools", _definition(steps=[]))

    selection = _template(K.POLICY, "selection")
    selection["policy"]["mandatory_question_refs"] = question_refs
    add(K.POLICY, "selection", selection)
    for question in scene.questions:
        response_id = f"{question.id}-result"
        question_doc = _template(K.QUESTION, "parking_state")
        add(
            K.QUESTION,
            question.id,
            {
                **question_doc,
                "objective": question.id.replace("_", " ").capitalize(),
                "prompt_template_ref": _pin(K.PROMPT_TEMPLATE, question.id),
                "facet_refs": [_pin(K.FACET, facet_id)],
                "primary_fact_key": question.id,
                "primary_fact_schema_ref": _pin(K.FACT_SCHEMA, question.id),
            },
        )
        fact = _template(K.FACT_SCHEMA, "parking_state")
        add(
            K.FACT_SCHEMA,
            question.id,
            {
                **fact,
                "semantic_key": question.id,
                "type_definition": {"kind": "enum", "members": question.members},
            },
        )
        prompt = _template(K.PROMPT_TEMPLATE, "parking_state")
        left, top, right, bottom = SIDEWALK_ZONE
        if region_projection:
            text = question.region_prompt
            if "$SIDEWALK_ZONE" in text:
                normalized = [
                    min(999, round(coordinate * 1000 / dimension))
                    for coordinate, dimension in zip(
                        SIDEWALK_ZONE, CAPTURE_IMAGE_SIZE * 2, strict=True
                    )
                ]
                text = text.replace("$SIDEWALK_ZONE", json.dumps(normalized))
        else:
            left, top, right, bottom = SIDEWALK_ZONE
            text = question.prompt.replace(
                "$SIDEWALK_ZONE", f"x={left}..{right} and y={top}..{bottom}"
            )
        add(
            K.PROMPT_TEMPLATE,
            question.id,
            {
                **prompt,
                "template_text": text,
                "response_schema_ref": _pin(K.RESPONSE_SCHEMA, response_id),
            },
        )
        response = _template(K.RESPONSE_SCHEMA, "parking_state-result")
        response["properties"]["value"]["enum"] = sorted(question.members)
        add(K.RESPONSE_SCHEMA, response_id, response)

    for identifier, text in (
        ("profile_discovery", scene.discovery_prompt),
        ("object_grounding", scene.grounding_prompt),
    ):
        add(
            K.PROMPT_TEMPLATE,
            identifier,
            {
                **_template(K.PROMPT_TEMPLATE, identifier),
                "template_text": text,
            },
        )

    if region_projection:
        add(
            K.PROMPT_TEMPLATE,
            "object_observation",
            {
                **_template(K.PROMPT_TEMPLATE, "object_observation"),
                "template_text": (
                    f"{N04_REGIONS_MARKER}\nAssess each object from its coordinates "
                    "and answer its requested questions independently."
                ),
            },
        )

    response = _template(K.RESPONSE_SCHEMA, "object_observation-result")
    response["required"] = [question.id for question in scene.questions]
    response["properties"] = {
        question.id: {
            "type": "string",
            "enum": [*question.members, "insufficient_visibility"],
        }
        for question in scene.questions
    }
    add(K.RESPONSE_SCHEMA, "object_observation-result", response)
    candidate = _template(K.RESPONSE_SCHEMA, "candidate_prediction-result")
    candidate["properties"]["categories"]["items"]["properties"]["category_id"][
        "enum"
    ] = [scene.violation_category]
    add(K.RESPONSE_SCHEMA, "candidate_prediction-result", candidate)

    add(
        K.RULE,
        scene.violation_category,
        _definition(
            violation_category=scene.violation_category,
            applicability_policy_ref=_pin(K.POLICY, rule_applicability),
            required_fact_selectors=[
                _rule_selector(question) for question in scene.questions
            ],
            optional_fact_selectors=[],
            condition_ast={
                "kind": "all",
                "arguments": [_comparison(question) for question in scene.questions],
            },
            rule_policy_ref=_pin(K.POLICY, "rule"),
            evidence_policy_ref=_pin(K.POLICY, "question-evidence"),
        ),
    )
    add(
        K.RULE_SET,
        rule_set_id,
        {
            **_template(K.RULE_SET, "parking-rules"),
            "rule_refs": [_pin(K.RULE, scene.violation_category)],
            "applicability_policy_ref": _pin(K.POLICY, rule_applicability),
        },
    )

    bundle_id = f"isolated.live-trial.scope-bound.{scene.scene_id}"
    metadata = replace(
        original.metadata,
        bundle_id=bundle_id,
        bundle_version=(
            REGION_PROJECTION_BUNDLE_VERSION
            if region_projection
            else original.metadata.bundle_version
        ),
        producer_catalogue_id=f"{bundle_id}.producers",
        required_capabilities=tuple(
            requirement
            for requirement in original.metadata.required_capabilities
            if requirement.family.value == "model_deployment"
        ),
    )
    return base.with_source_provenance(
        BundleCompileInput(metadata=metadata, artifacts=tuple(artifacts))
    )


def build_mask_compile_input(*, region_projection: bool = False) -> BundleCompileInput:
    return _build("food-service-mask.json", region_projection=region_projection)


def build_sidewalk_goods_compile_input(
    *, region_projection: bool = False
) -> BundleCompileInput:
    return _build("sidewalk-goods.json", region_projection=region_projection)


def capability_snapshot(source: BundleCompileInput) -> CapabilitySnapshot:
    """Refresh the four held model contracts against exactly this scene's schema pins."""
    original = base.capability_snapshot(base.load_source())
    schemas = {
        artifact.artifact_id: artifact
        for artifact in source.artifacts
        if artifact.kind == K.RESPONSE_SCHEMA
    }

    def ref_for(ref: dict) -> dict:
        artifact = schemas[ref["artifact_id"]]
        digest = sha256_digest(
            canonicalize_json(artifact.content, path=artifact.path, **base.JSON_LIMITS)
        )
        return {**ref, "digest": digest}

    def contract(row: dict) -> dict:
        return {
            **row,
            "input_schema_ref": ref_for(row["input_schema_ref"]),
            "output_schema_ref": ref_for(row["output_schema_ref"]),
        }

    data = original.model_dump(mode="python")
    data["capabilities"] = tuple(
        row for row in data["capabilities"] if row["family"].value == "model_deployment"
    )
    data["runtime_protocols"] = tuple(
        contract(row)
        for row in data["runtime_protocols"]
        if row["role"] != "tool_execution"
    )
    data["model_deployments"] = tuple(
        {
            **row,
            "contracts": tuple(contract(item) for item in row["contracts"]),
        }
        for row in data["model_deployments"]
    )
    data["pin"]["identity"] = f"{source.metadata.bundle_id}.capabilities"
    provisional = CapabilitySnapshot.model_validate(data)
    return provisional.model_copy(update={"pin": base.configuration_pin(provisional)})
