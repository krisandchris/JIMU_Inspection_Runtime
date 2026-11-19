import json

import pytest
from fixtures.wp01b.x3.integration import data, registry
from fixtures.wp01c.runtime_authority.cases import claim_records
from pydantic import TypeAdapter

from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageAuthorityInput,
    validate_decision_guard_authority,
)


@pytest.fixture
def actual_authority():
    claims, records = claim_records()
    bounds = data()
    return CoverageAuthorityInput(
        claims=claims,
        records=records,
        fact_schemas=registry(),
        parse_limits=CandidateParseLimits.model_validate(bounds["parse_limits"]),
        snapshot_limits=CandidateSnapshotLimits.model_validate(
            bounds["snapshot_limits"]
        ),
    )


def test_exact_actual_fact_and_selection_are_accepted(actual_authority):
    records = actual_authority.records
    validate_decision_guard_authority(
        records.facts, records.authority_selections, authority=actual_authority
    )


@pytest.mark.parametrize("mutation", ["value", "status"])
def test_same_id_fact_cannot_change_verified_payload(actual_authority, mutation):
    records = actual_authority.records
    raw = records.facts[0].model_dump(mode="json")
    if mutation == "value":
        raw["value"]["value"] = not raw["value"]["value"]
    else:
        raw.pop("value")
        raw["status"] = "unknown"
        raw["reason"] = {
            "reason_code": "condition-unresolved",
            "message": "The supplied producer did not resolve the value.",
            "diagnostic_refs": [],
        }
    supplied = TypeAdapter(FactRecord).validate_json(json.dumps(raw))
    assert supplied.fact_id == records.facts[0].fact_id
    with pytest.raises(
        ValueError, match="Fact must be from the verified runtime authority input"
    ):
        validate_decision_guard_authority(
            (supplied,), records.authority_selections, authority=actual_authority
        )
