import json

import pytest
from jsonschema import Draft202012Validator

from jimu_runtime.inspection.perception.region_request import (
    RegionObject,
    compile_region_request,
    project_region_response,
    strict_json_loads,
)

_STATE = (
    "Inspect the highlighted subject and report what is visible.",
    {"type": "string", "enum": ["present", "absent", "insufficient_visibility"]},
)
_VISIBLE = ("Is the target assessable?", {"type": "boolean"})


def _region(object_id="object-1", *, box=(0, 0, 500, 1000), members=(), questions=None):
    return RegionObject(
        object_id,
        "entity",
        box,
        members,
        {"state": _STATE, "visible": _VISIBLE} if questions is None else questions,
    )


def _request(*objects):
    return compile_region_request(objects, width=1000, height=1000)


def test_compile_one_original_frame_with_stable_ids_and_deduplicated_assignments():
    first = _region(members=((300, 400, 500, 500), (100, 300, 200, 400)))
    second = _region("object-2", questions={"visible": _VISIBLE, "state": _STATE})
    third = _region(
        "object-3", box=(101, 22, 608, 999), questions={"visible": _VISIBLE}
    )
    request = _request(first, second, third)

    assert request.object_ids == ("object-1", "object-2", "object-3")
    assert _request(first, second, third) == request
    metadata = json.loads(request.prompt.splitlines()[-1])
    assert metadata["image_size_pixels"] == [1000, 1000]
    assert metadata["boxes"] == {
        "r1": [0, 0, 500, 999],
        "r2": [0, 0, 500, 999],
        "r3": [101, 22, 608, 999],
    }
    assert metadata["questions"] == {"state": _STATE[0], "visible": _VISIBLE[0]}
    assert metadata["assignments"] == [
        {
            "subject": "entity",
            "questions": ["state", "visible"],
            "regions": ["r1", "r2"],
        },
        {"subject": "entity", "questions": ["visible"], "regions": ["r3"]},
    ]
    assert metadata["member_boxes"] == {
        "r1": [[1, [300, 400, 500, 500]], [2, [100, 300, 200, 400]]],
    }
    assert "keep the 1-based indices" in request.prompt
    assert all(object_id not in request.prompt for object_id in request.object_ids)
    assert request.prompt.count(_STATE[0]) == 1


def test_normalization_uses_each_original_image_dimension_and_clamps_edges():
    request = compile_region_request(
        (_region(box=(445, 410, 1536, 1024)),), width=1536, height=1024
    )
    metadata = json.loads(request.prompt.splitlines()[-1])
    assert metadata["boxes"]["r1"] == [290, 400, 999, 999]
    assert "member_boxes" not in metadata


def test_normalization_expands_collapsed_axes_inside_image_for_objects_and_members():
    full = _region(
        box=(0, 0, 10_000, 8_000),
        members=((0, 0, 1, 1), (9_999, 7_999, 10_000, 8_000)),
    )
    narrow = _region("object-2", box=(1_228, 200, 1_232, 600))
    request = compile_region_request((full, narrow), width=10_000, height=8_000)
    metadata = json.loads(request.prompt.splitlines()[-1])
    assert metadata["boxes"] == {"r1": [0, 0, 999, 999], "r2": [122, 25, 124, 75]}
    assert metadata["member_boxes"] == {
        "r1": [[1, [0, 0, 1, 1]], [2, [998, 998, 999, 999]]]
    }


def test_nine_regions_share_one_assignment_and_question_definition():
    regions = tuple(
        _region(f"object-{index}", questions={"state": _STATE})
        for index in range(1, 10)
    )
    request = _request(*regions)
    metadata = json.loads(request.prompt.splitlines()[-1])
    aliases = [f"r{index}" for index in range(1, 10)]
    assert list(metadata["boxes"]) == aliases
    assert metadata["assignments"] == [
        {"subject": "entity", "questions": ["state"], "regions": aliases}
    ]
    assert metadata["questions"] == {"state": _STATE[0]}
    assert "insufficient_visibility" not in request.prompt
    assert request.schema["properties"]["regions"]["properties"]["r9"][
        "properties"
    ] == {"state": _STATE[1]}


def test_shared_schema_keeps_admitted_local_reference_scope():
    slot = {"$ref": "#/$defs/state"}
    definitions = {"state": _STATE[1]}
    request = compile_region_request(
        (_region(questions={"state": (_STATE[0], slot)}),),
        width=1000,
        height=1000,
        definitions=definitions,
    )
    validator = Draft202012Validator(request.schema)
    validator.validate({"regions": {"r1": {"state": "present"}}})
    assert not validator.is_valid({"regions": {"r1": {"state": "invalid"}}})
    assert request.schema["$defs"] == definitions
    with pytest.raises(ValueError, match="offline_schema_reference"):
        _request(_region(questions={"state": (_STATE[0], slot)}))


def test_wire_schema_is_closed_and_projection_keeps_object_and_question_validation_local():
    request = _request(_region(), _region("object-2", questions={"visible": _VISIBLE}))
    validator = Draft202012Validator(request.schema)
    complete = {
        "regions": {
            "r1": {"state": "present", "visible": False},
            "r2": {"visible": True},
        }
    }
    validator.validate(complete)

    partial = {"regions": {"r1": {"state": "insufficient_visibility"}}}
    assert not validator.is_valid(partial)
    assert request.extract(partial) == {
        "object-1": {"state": "insufficient_visibility"}
    }
    assert request.extract({"regions": {}}) == {}
    invalid_value = {
        "regions": {"r1": {"visible": "invalid"}, "r2": {"visible": False}}
    }
    assert not validator.is_valid(invalid_value)
    # The existing per-object validator owns the malformed slot and valid sibling.
    assert request.extract(invalid_value) == {
        "object-1": {"visible": "invalid"},
        "object-2": {"visible": False},
    }


@pytest.mark.parametrize("bad_region", [None, [], "scalar", {"foreign_question": True}])
def test_projection_preserves_malformed_known_region_without_losing_sibling(bad_region):
    request = _request(_region(), _region("object-2", questions={"visible": _VISIBLE}))
    complete_sibling = {"state": "insufficient_visibility", "visible": False}
    payload = {"regions": {"r1": complete_sibling, "r2": bad_region}}
    assert not Draft202012Validator(request.schema).is_valid(payload)
    assert request.extract(payload) == {
        "object-1": complete_sibling,
        "object-2": bad_region,
    }


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"regions": {}, "other": 1}, "invalid_envelope"),
        ({"regions": []}, "invalid_envelope"),
        ({"regions": {"r2": {"state": "present"}}}, "unknown_target"),
    ],
)
def test_projection_rejects_untrusted_reply_associations(payload, error):
    request = _request(_region())
    assert not Draft202012Validator(request.schema).is_valid(payload)
    with pytest.raises(ValueError, match=error):
        request.extract(payload)


def test_projection_rejects_inconsistent_or_duplicate_expected_object_ids():
    with pytest.raises(ValueError, match="invalid_projection"):
        project_region_response(
            {"regions": {}},
            object_ids=["object-1", "object-1"],
            questions_by_object={"object-1": ["state"]},
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"regions":{"r1":{},"r1":{}}}',
        '{"regions":{"r1":{"visible":true,"visible":false}}}',
        '{"regions":{"r1":{"visible":NaN}}}',
        '{"regions":{"r1":{"visible":1e999}}}',
    ],
)
def test_strict_parse_rejects_duplicate_or_nonfinite_json_before_projection(raw):
    with pytest.raises(ValueError):
        strict_json_loads(raw)


@pytest.mark.parametrize(
    ("objects", "error"),
    [
        ((), "objects_empty"),
        ((_region(), _region()), "duplicate_object_id"),
        ((_region(box=(0, 0, 1001, 100)),), "box_bounds"),
        ((_region(box=(0, 0, float("nan"), 100)),), "box_bounds"),
        ((_region(box=(False, 0, 100, 100)),), "box_shape"),
        ((_region(questions={}),), "question_ids"),
        (
            (
                _region(),
                _region("object-2", questions={"state": ("Different text", _STATE[1])}),
            ),
            "conflicting_question_contract",
        ),
        (
            (
                _region(),
                _region("object-2", questions={"state": (_STATE[0], _VISIBLE[1])}),
            ),
            "conflicting_question_contract",
        ),
    ],
)
def test_compile_fails_explicitly_for_unadmitted_regions_or_inconsistent_contracts(
    objects, error
):
    with pytest.raises(ValueError, match=error):
        _request(*objects)
