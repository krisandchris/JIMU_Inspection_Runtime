"""Bundle source for the live member-aware non-motor vehicle parking scene."""

import json
from dataclasses import replace
from hashlib import sha256

from examples.nonmotor_vehicle_illegal_parking.compilable import scene as base
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.compiler import BundleCompileInput, ReferenceSlot
from jimu_runtime.domain.enums import ArtifactKind as K
from jimu_runtime.domain.fact_schemas.models import ValueSchemaGenerationSpec
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.nodes.observation import N04_REGIONS_MARKER
from jimu_runtime.inspection.geometry.parking_members_evidence import (
    GROUP_CAPABILITY,
    GROUP_OUTPUT,
)

from .wevis_masks import PARKING_OUTER

VERSION = base.VERSION
REGION_PROJECTION_BUNDLE_VERSION = "1.1.2"
TOOL = "wheel_boundary_geometry"
CAPABILITY = GROUP_CAPABILITY
OUTPUT = GROUP_OUTPUT
OBSERVATION = "parking_state"
MAX_OBSERVED_MEMBERS = 256
IMAGE_SIZE = (1536, 1024)


def _pin(kind: K, name: str) -> dict:
    return {"$pin": [kind.value, name, VERSION]}


def _field(name: str, definition: dict, *, required: bool = True) -> dict:
    return {"name": name, "required": required, "type_definition": definition}


def _typed_schema(definition: dict, generation: dict) -> dict:
    compiled = generate_value_schema(
        definition, ValueSchemaGenerationSpec.model_validate(generation)
    )
    return json.loads(compiled.canonical_schema_bytes)


def _raw_schema(schema: dict) -> dict:
    raw = schema["properties"]["value"].copy()
    if raw["type"] == "object":
        raw["properties"] = {
            key: _raw_schema(value) for key, value in raw["properties"].items()
        }
    elif raw["type"] == "array":
        raw["items"] = _raw_schema(raw["items"])
    return raw


def build_source(*, region_projection: bool = False) -> BundleCompileInput:
    source = base.load_source()
    excluded = {
        "wheel_segmentation",
        "tactile_paving_segmentation",
        "wheel_mask",
        "tactile_paving_mask",
    }
    originals = {(item.kind, item.artifact_id): item for item in source.artifacts}
    changes = {}

    def body(kind: K, name: str) -> dict:
        document = json.loads(
            (base.DIRECTORY / "source" / kind.value / f"{name}.json").read_bytes()
        )
        changes[kind, name] = document
        return document

    x0, y0, x1, y1 = PARKING_OUTER
    polygon = {
        "kind": "polygon",
        "vertices": [
            {"x": x, "y": y} for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        ],
    }
    area = json.dumps(
        {
            "schema_version": VERSION,
            "coordinate_system": "original_image",
            "unit": "pixel",
            "width": IMAGE_SIZE[0],
            "height": IMAGE_SIZE[1],
            "geometry": polygon,
        }
    ).encode()
    body(K.GEOMETRY_RESOURCE, "synthetic-parking-area")["geometry"] = polygon
    body(K.STATIC_ASSET, "synthetic-parking-area-source").update(
        sha256=sha256(area).hexdigest(), byte_size=len(area)
    )
    body(K.PROFILE, base.PROFILE)["grounding_grouping"] = {
        "mode": "connected_boxes",
        "max_gap_pixels": 8,
    }

    observation_type = {
        "kind": "record",
        "fields": [
            _field(
                "members",
                {
                    "kind": "list",
                    "min_items": 0,
                    "max_items": MAX_OBSERVED_MEMBERS,
                    "item_type": {
                        "kind": "record",
                        "fields": [
                            _field(
                                "member_index",
                                {
                                    "kind": "integer",
                                },
                                required=False,
                            ),
                            _field(
                                "state",
                                {
                                    "kind": "enum",
                                    "members": ["parked", "unparked", "unknown"],
                                },
                                required=False,
                            ),
                            _field(
                                "visual_evidence",
                                {"kind": "string", "min_length": 0, "max_length": 160},
                                required=False,
                            ),
                        ],
                    },
                },
            ),
        ],
    }
    observation_fact = body(K.FACT_SCHEMA, OBSERVATION)
    observation_fact["type_definition"] = observation_type
    typed_observation = _typed_schema(
        observation_type, observation_fact["value_schema_generation"]
    )
    observation_result = body(K.RESPONSE_SCHEMA, "parking_state-result")
    typed_observation["$id"] = observation_result["$id"]
    observation_result.clear()
    observation_result.update(typed_observation)
    observation_input = body(K.RESPONSE_SCHEMA, "object_observation-input")
    observation_input["properties"]["member_boxes"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_OBSERVED_MEMBERS,
        "items": observation_input["properties"]["bbox"],
    }
    observation_input["required"].append("member_boxes")
    body(K.RESPONSE_SCHEMA, "object_observation-result")["properties"][OBSERVATION] = (
        _raw_schema(typed_observation)
    )
    body(K.PROMPT_TEMPLATE, "object_observation")["template_text"] = (
        f"{N04_REGIONS_MARKER}\nInspect each group at its supplied coordinates. "
        "Use the member boxes for the parking_state record."
        if region_projection
        else (
            "Inspect the group at the supplied coordinates in the original image. "
            "Answer each requested slot according to its own instructions. "
            "The parking_state slot is a record, and the other slots are scalars."
        )
    )
    body(K.PROMPT_TEMPLATE, OBSERVATION)["template_text"] = (
        "Return a members list. Every entry must contain member_index, state and visual_evidence. "
        "Include all three fields for every supplied member in its one-based order. "
        "Write a short visible clue for each state, or explain what is obscured for unknown. "
        "Judge physical activity only. An unattended vehicle resting on its stand is parked "
        "wherever it stands, including outside a bay. Use unparked only for visible operation "
        "or motion and unknown when state or identity is unclear. In visual_evidence cite a "
        "visible clue to activity for that member. Answer bay position separately."
        if region_projection
        else (
            "For each member_boxes entry (one-based member_index in provided order), identify "
            "that vehicle only. Return a members list with member_index, state, and visual_evidence. "
            "Use parked for a clearly unattended stationary parked vehicle, unparked for a "
            "clearly operated or moving vehicle, unknown when identity or state is ambiguous. "
            "Describe a distinguishing visible detail and the reason for that state in "
            "visual_evidence. One entry per supplied member, including unknown members. "
            "Ignore vehicles outside the supplied boxes. Judge each member's activity independently "
            "of the parking area and other members. Do not decide violations."
        )
    )

    relation = body(K.FACT_SCHEMA, "wheel_boundary_relation")
    relation.update(semantic_key=OUTPUT, type_definition={"kind": "bool"})
    observation_selector = {
        "scope": "object",
        "fact_key": OBSERVATION,
        "field_path": [],
        "fact_schema_ref": _pin(K.FACT_SCHEMA, OBSERVATION),
        "producer_selector": {
            "producer_kind": "vlm_question",
            "question_ref": _pin(K.QUESTION, OBSERVATION),
            "output_name": "primary",
        },
    }
    tool = body(K.TOOL, TOOL)
    tool["implementation_requirement"]["capability_id"] = CAPABILITY
    tool["fact_outputs"][0].update(
        output_name=OUTPUT,
        fact_key=OUTPUT,
        value_path=[OUTPUT],
        evidence_paths=[["geometry"]],
    )
    tool_input = body(K.TOOL_INPUT_SCHEMA, f"{TOOL}-input")
    wheel_input = json.loads(
        (
            base.DIRECTORY
            / "source"
            / "tool_input_schema"
            / "wheel_segmentation-input.json"
        ).read_bytes()
    )
    tool_input["properties"] = {
        "image": wheel_input["properties"]["image"],
        "parking_area": tool_input["properties"]["parking_area"],
        OBSERVATION: {
            key: val
            for key, val in typed_observation.items()
            if not key.startswith("$")
        },
    }
    tool_input["required"] = ["image", "parking_area"]
    tool_result = body(K.TOOL_OUTPUT_SCHEMA, f"{TOOL}-result")
    tool_result["payload"]["properties"]["value"]["properties"] = {
        OUTPUT: {
            "type": "object",
            "required": ["kind", "value"],
            "additionalProperties": False,
            "properties": {"kind": {"const": "bool"}, "value": {"type": "boolean"}},
        },
    }
    tool_result["evidence_slots"] = ["geometry"]
    plan = body(K.TOOL_PLAN, "nonmotor_vehicle_geometry")
    wheel, _, geometry = plan["steps"]
    image_binding = next(
        binding
        for binding in wheel["input_bindings"]
        if binding["source"]["source_kind"] == "image"
    )
    geometry_binding = next(
        binding
        for binding in geometry["input_bindings"]
        if binding["source"]["source_kind"] == "context"
    )
    target_schema = geometry_binding["target"]["schema_ref"]
    image_binding["target"]["schema_ref"] = target_schema
    geometry.update(
        depends_on=[],
        input_bindings=[
            image_binding,
            geometry_binding,
            {
                "source": {"source_kind": "fact", "selector": observation_selector},
                "target": {"schema_ref": target_schema, "field_path": [OBSERVATION]},
                "required": False,
                "on_missing": "omit",
            },
        ],
        fact_output_mappings=[{"output_name": OUTPUT, "target_fact_key": OUTPUT}],
    )
    plan["steps"] = [geometry]

    rule = body(K.RULE, "nonmotor_vehicle_illegal_parking")
    selector = rule["required_fact_selectors"][1]
    selector["fact_key"] = OUTPUT
    selector["producer_selector"]["output_name"] = OUTPUT
    rule["required_fact_selectors"] = [selector]
    rule["condition_ast"] = {
        "kind": "comparison",
        "operator": "eq",
        "left": {"kind": "fact_ref", "selector": selector},
        "right": {
            "kind": "literal",
            "constant": {
                "type_definition": {"kind": "bool"},
                "value": {"kind": "bool", "value": True},
            },
        },
    }
    body(K.POLICY, "geometry-evidence")["policy"]["required_evidence_types"] = [
        "original_image",
        "geometry",
    ]

    for key, document in changes.items():
        slots: list[ReferenceSlot] = []
        original = originals[key]
        if key[0] is K.TOOL_OUTPUT_SCHEMA:
            document = base._output_contract(
                key[1], document["payload"], document["evidence_slots"]
            )
        content = json.dumps(
            base._extract_slots(document, slots), ensure_ascii=False
        ).encode()
        originals[key] = replace(
            original, content=content, reference_slots=tuple(slots)
        )
    artifacts = tuple(
        row
        for (_, name), row in originals.items()
        if name.removesuffix("-input").removesuffix("-result") not in excluded
    )
    requirements = tuple(
        requirement.model_copy(update={"capability_id": CAPABILITY})
        if requirement.capability_id == f"synthetic.cpu.{TOOL}"
        else requirement
        for requirement in source.metadata.required_capabilities
        if requirement.capability_id.rsplit(".", 1)[-1] not in excluded
    )
    (asset,) = source.assets
    return base.with_source_provenance(
        replace(
            source,
            artifacts=artifacts,
            assets=(replace(asset, content=area),),
            metadata=replace(
                source.metadata,
                bundle_version=(
                    REGION_PROJECTION_BUNDLE_VERSION
                    if region_projection
                    else source.metadata.bundle_version
                ),
                required_capabilities=requirements,
            ),
        )
    )


def capability_snapshot(source: BundleCompileInput) -> CapabilitySnapshot:
    # The baseline index also describes the unselected synthetic segmentation
    # protocols. Reuse its constructors and expose only this scene's protocols.
    existing = {row.identity for row in source.artifacts}
    schema_kinds = {K.TOOL_INPUT_SCHEMA, K.TOOL_OUTPUT_SCHEMA}
    unused = tuple(
        row
        for row in base.load_source().artifacts
        if row.kind in schema_kinds and row.identity not in existing
    )
    baseline = base.capability_snapshot(
        replace(source, artifacts=source.artifacts + unused)
    )
    protocols = tuple(
        protocol
        for protocol in baseline.runtime_protocols
        if protocol.input_schema_ref.artifact_kind not in schema_kinds
        or protocol.input_schema_ref.artifact_id == f"{TOOL}-input"
    )
    snapshot = baseline.model_copy(update={"runtime_protocols": protocols})
    return snapshot.model_copy(update={"pin": base.configuration_pin(snapshot)})
