"""Real record projections preserve finite B evidence and selected revisions."""

import json

import pytest
from fixtures.wp01b.x3.integration import parse
from fixtures.wp01c.nodes.cases import ref
from fixtures.wp01c.runtime_authority.cases import check, claim_records, documents
from pydantic import TypeAdapter

from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords


@pytest.mark.parametrize("case", ["question", "tool", "system"])
def test_actual_records_project_into_existing_authority_reports(case):
    claims, records = claim_records(case)
    projected = check(claims, records)
    assert projected.history == claims.history
    assert projected.selected_revisions == records.authority_selections
    assert projected.report.aggregate_status == "deferred"
    assert any(
        check.check_id == "x3.selected_outputs.passed"
        for check in projected.report.checks
    )


def test_verified_projection_retains_only_latest_selected_revision():
    claims, records = documents()
    history = claims["history"]
    first = history["proposed_selections"][0]
    latest = first | {
        "selection_id": "revision-two",
        "reason": "manual_review",
        "authority_revision": 2,
        "expected_previous_selection_ref": ref(
            "authoritative_result_selection", first["selection_id"]
        ),
    }
    history["prior_selections"] = [first]
    history["proposed_selections"] = [latest]
    records["authority_selections"] = [first, latest]
    result = check(
        parse(claims), RuntimeAuthorityRecords.model_validate_json(json.dumps(records))
    )
    assert result.selected_revisions == (
        TypeAdapter(AuthoritativeResultSelection).validate_json(json.dumps(latest)),
    )
    assert len(result.history.prior_selections) == 1
