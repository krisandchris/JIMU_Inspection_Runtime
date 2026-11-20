"""Compile and project a shared transport request for one original RGB frame."""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from math import ceil, floor
from types import MappingProxyType
from typing import cast

from jimu_runtime.bundle.schema_validation import compile_offline_response_schema
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.gateway.http_wire import strict_json as strict_json_loads

__all__ = [
    "RegionObject",
    "RegionRequest",
    "compile_region_request",
    "extract_region_response",
    "project_region_response",
    "strict_json_loads",
]

type Box = tuple[float, float, float, float]
type QuestionContract = tuple[str, dict[str, JSONValue]]


@dataclass(frozen=True, slots=True)
class RegionObject:
    """A grounded object and its already-admitted question contracts."""

    object_id: str
    entity_id: str
    xyxy: Box
    member_boxes: tuple[Box, ...]
    questions: Mapping[str, QuestionContract]


@dataclass(frozen=True, slots=True)
class RegionRequest:
    prompt: str
    schema: dict[str, JSONValue]
    object_ids: tuple[str, ...]
    questions_by_object: Mapping[str, tuple[str, ...]] = field(repr=False)

    def extract(self, payload: object) -> dict[str, object]:
        return extract_region_response(self, payload)


def _region_id(index: int) -> str:
    return f"r{index}"


def _valid_identifier(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and all(character.isprintable() for character in value)
    )


def _closed_object(properties: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "required": list(properties),
    }


def _json(value: object, *, sort_keys: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
    )


def _normalize_interval(start: float, end: float, dimension: int) -> tuple[int, int]:
    start_normalized = start * 1000 / dimension
    end_normalized = end * 1000 / dimension
    lower = min(999, round(start_normalized))
    upper = min(999, round(end_normalized))
    if lower == upper:
        # Use the smallest enclosing integer interval for a collapsed box.
        # If it meets the frame's upper edge, use the last in-frame unit.
        lower = min(998, floor(start_normalized))
        upper = max(lower + 1, min(999, ceil(end_normalized)))
    return lower, upper


def _normalize_box(box: Box, width: int, height: int) -> list[JSONValue]:
    if len(box) != 4 or any(type(coordinate) not in (int, float) for coordinate in box):
        raise ValueError("region_box_shape")
    left, top, right, bottom = box
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("region_box_bounds")
    left_normalized, right_normalized = _normalize_interval(left, right, width)
    top_normalized, bottom_normalized = _normalize_interval(top, bottom, height)
    return [left_normalized, top_normalized, right_normalized, bottom_normalized]


def compile_region_request(
    objects: Sequence[RegionObject],
    *,
    width: int,
    height: int,
    definitions: dict[str, JSONValue] | None = None,
) -> RegionRequest:
    """Use input order to assign stable short IDs r1..rN within the request."""
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise ValueError("region_image_dimensions")
    if not objects:
        raise ValueError("region_objects_empty")

    boxes: dict[str, JSONValue] = {}
    member_boxes: dict[str, JSONValue] = {}
    region_properties: dict[str, JSONValue] = {}
    questions: dict[str, JSONValue] = {}
    question_schemas: dict[str, QuestionContract] = {}
    schema_texts: dict[str, str] = {}
    assignments: dict[tuple[str, tuple[str, ...]], list[JSONValue]] = {}
    questions_by_object: dict[str, tuple[str, ...]] = {}
    for index, obj in enumerate(objects, 1):
        if not _valid_identifier(obj.object_id) or not _valid_identifier(obj.entity_id):
            raise ValueError("region_invalid_object_identity")
        if obj.object_id in questions_by_object:
            raise ValueError("region_duplicate_object_id")
        if (
            not all(_valid_identifier(key) for key in obj.questions)
            or not obj.questions
        ):
            raise ValueError("region_question_ids")
        object_questions = tuple(sorted(obj.questions))
        questions_by_object[obj.object_id] = object_questions
        region_id = _region_id(index)
        assignments.setdefault((obj.entity_id, object_questions), []).append(region_id)
        boxes[region_id] = _normalize_box(obj.xyxy, width, height)
        if obj.member_boxes:
            member_boxes[region_id] = [
                [member_index, _normalize_box(box, width, height)]
                for member_index, box in enumerate(obj.member_boxes, start=1)
            ]

        for question_id in object_questions:
            prompt, schema = obj.questions[question_id]
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or not isinstance(schema, dict)
            ):
                raise ValueError("region_question_contract")
            schema_text = _json(schema, sort_keys=True)
            if question_id in question_schemas:
                if (
                    question_schemas[question_id][0] != prompt
                    or schema_texts[question_id] != schema_text
                ):
                    raise ValueError("region_conflicting_question_contract")
                continue
            schema_copy = cast(dict[str, JSONValue], strict_json_loads(schema_text))
            question_schemas[question_id] = (prompt, schema_copy)
            schema_texts[question_id] = schema_text
            questions[question_id] = prompt
        region_properties[region_id] = _closed_object(
            {key: question_schemas[key][1] for key in object_questions}
        )

    schema = _closed_object({"regions": _closed_object(region_properties)})
    if definitions is not None:
        # All embedded slots share one admitted response schema's definition scope.
        schema["$defs"] = cast(
            JSONValue, strict_json_loads(_json(definitions, sort_keys=True))
        )
    # Check the embedded slots in their new document scope and enforce the
    # existing offline profile's complexity and reference bounds.
    compile_offline_response_schema(schema)
    instructions = (
        "Observe the original image. Boxes are [left,top,right,bottom], integer "
        "thousandths of image width and height (0..999, top-left origin). "
        "Any highlight or outline in a question means the supplied box. "
        "Answer only assigned questions, using scene context as needed. "
        "Use the question's uncertainty option when evidence is insufficient. "
        "Do not decide violations. Return only JSON: regions keyed by region ID, "
        "then question ID."
    )
    metadata: dict[str, JSONValue] = {
        "image_size_pixels": [width, height],
        "boxes": boxes,
        "assignments": [
            {"subject": subject, "questions": list(question_ids), "regions": regions}
            for (subject, question_ids), regions in assignments.items()
        ],
        "questions": questions,
    }
    if member_boxes:
        instructions += (
            " Member boxes are [member_index, box]; keep the 1-based indices."
        )
        metadata["member_boxes"] = member_boxes
    return RegionRequest(
        f"{instructions}\n{_json(metadata)}",
        schema,
        tuple(questions_by_object),
        MappingProxyType(questions_by_object),
    )


def extract_region_response(
    request: RegionRequest, payload: object
) -> dict[str, object]:
    return project_region_response(
        payload,
        object_ids=request.object_ids,
        questions_by_object=request.questions_by_object,
    )


def project_region_response(
    payload: object,
    *,
    object_ids: Sequence[str],
    questions_by_object: Mapping[str, Collection[str]],
) -> dict[str, object]:
    """Preserve each known region's raw reply for its own signed validator."""
    if len(set(object_ids)) != len(object_ids) or set(object_ids) != set(
        questions_by_object
    ):
        raise ValueError("region_response_invalid_projection")
    if not isinstance(payload, dict) or payload.keys() != {"regions"}:
        raise ValueError("region_response_invalid_envelope")
    regions = payload["regions"]
    if not isinstance(regions, dict):
        raise ValueError("region_response_invalid_envelope")
    object_by_region = {
        _region_id(index): object_id for index, object_id in enumerate(object_ids, 1)
    }
    if regions.keys() - object_by_region.keys():
        raise ValueError("region_response_unknown_target")
    return {
        object_by_region[region_id]: region for region_id, region in regions.items()
    }
