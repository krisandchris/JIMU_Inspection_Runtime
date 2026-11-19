import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.domain.runtime.coverage import ExecutionCoverage, ProfileCoverage
from jimu_runtime.domain.runtime.decisions import CandidatePrediction, DecisionRecord

FIXTURES = Path(__file__).parents[1] / "fixtures/wp01c/decisions"


@pytest.mark.parametrize(
    "model, filename",
    [
        (CandidatePrediction, "candidate_prediction"),
        (DecisionRecord, "decision_record"),
        (ExecutionCoverage, "execution_coverage"),
        (ProfileCoverage, "profile_coverage"),
    ],
)
def test_persisted_decision_and_coverage_roots(model, filename):
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    good = (FIXTURES / f"{filename}.json").read_text()
    validator.validate(json.loads(good))
    instance = model.model_validate_json(good)
    assert model.model_validate_json(instance.model_dump_json()) == instance
    bad = (FIXTURES / f"invalid_{filename}.json").read_text()
    assert not validator.is_valid(json.loads(bad))
    with pytest.raises(ValidationError):
        model.model_validate_json(bad)
