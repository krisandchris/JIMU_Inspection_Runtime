"""JSON round trips and generated schema for the frozen evaluation inputs and report."""

import json

import pytest
from fixtures.wp06.evaluation_sample import score, synthetic_inputs
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from jimu_runtime.evaluation.scoring import Ratio


def test_generated_schemas_and_roundtrips():
    dataset, truth, predictions, protocol = synthetic_inputs()
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    for model in (dataset, truth, predictions, protocol, report):
        schema = type(model).model_json_schema()
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        payload = json.loads(model.model_dump_json())
        validator.validate(payload)
        restored = type(model).model_validate_json(model.model_dump_json())
        assert restored == model and restored.pin() == model.pin()
        payload["undocumented"] = True
        assert not validator.is_valid(payload)
        with pytest.raises(ValidationError, match="extra"):
            type(model).model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "bad",
    [
        {"numerator": 1, "denominator": 0, "value": None},
        {"numerator": 0, "denominator": 0, "value": 0.0},
        {"numerator": 1, "denominator": 2, "value": 0.25},
        {"numerator": True, "denominator": 2, "value": 0.5},
    ],
)
def test_invalid_ratios_rejected(bad):
    with pytest.raises(ValidationError):
        Ratio.model_validate(bad)


@pytest.mark.parametrize("mutation", ["duplicate-sample", "summary", "basis"])
def test_inconsistent_report_cannot_be_read_or_repinned(mutation):
    dataset, truth, predictions, protocol = synthetic_inputs()
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    wire = json.loads(report.model_dump_json())
    if mutation == "duplicate-sample":
        wire["samples"].append(wire["samples"][0])
    elif mutation == "summary":
        wire["summary"]["samples"] += 1
    else:
        wire["trial_source"]["digest"] = "c" * 64
    with pytest.raises(ValidationError, match="report"):
        type(report).model_validate_json(json.dumps(wire))
