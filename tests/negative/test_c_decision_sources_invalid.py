import json

import pytest
from fixtures.wp01c.decisions.cases import source_case

from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageSources,
    validate_decision_coverage_sources,
)
from jimu_runtime.domain.validation.decision_coverage import DecisionCoverageRecords


def validate(coverage, records, sources):
    validate_decision_coverage_sources(
        ExecutionCoverage.model_validate_json(json.dumps(coverage)),
        DecisionCoverageRecords.model_validate_json(json.dumps(records)),
        CoverageSources.model_validate_json(json.dumps(sources)),
    )


def test_successful_invocations_do_not_turn_hits_into_no_hits():
    coverage, records, sources = source_case("no_hits", two_groups=True)
    actual_hits = source_case("hits", two_groups=True)[2]["discovery_output"]
    sources["discovery_output"] = actual_hits
    with pytest.raises(ValueError, match="outcome must match its selected N02 output"):
        validate(coverage, records, sources)


def test_grouped_discovery_does_not_accept_a_foreign_binding():
    coverage, records, sources = source_case("no_hits", two_groups=True)
    sources["discovery_input"]["bindings"][1]["discovery_binding_ref"][
        "artifact_id"
    ] = "other-binding"
    with pytest.raises(ValueError, match="bindings must be eligible and pinned"):
        validate(coverage, records, sources)


@pytest.mark.parametrize("mutation", ["result", "object"])
def test_grounding_outcome_and_objects_cannot_be_swapped(mutation):
    coverage, records, sources = source_case()
    grounded = sources["grounding_records"][0]
    if mutation == "result":
        grounded.update(outcome="no_target", object_refs=[])
    else:
        grounded["object_refs"][0]["record_id"] = "other-object"
    with pytest.raises(ValueError, match="must match its actual GroundingRecord"):
        validate(coverage, records, sources)


@pytest.mark.parametrize("no_decision", [True, False])
def test_foreign_object_n06_node_cannot_be_used_for_blocker_or_decision(no_decision):
    coverage, records, sources = source_case(no_decision=no_decision)
    node = next(item for item in sources["node_runs"] if item["node_id"] == "N06")
    node["upstream_inputs"][0]["ref"]["record_id"] = "other-object"
    with pytest.raises(ValueError, match="N06 must retain its exact Object"):
        validate(coverage, records, sources)


def test_rebinding_the_actual_execution_snapshot_digest_is_rejected():
    coverage, records, sources = source_case()
    sources["execution"]["context_snapshot"]["snapshot_digest"] = "f" * 64
    with pytest.raises(ValueError, match="ContextSnapshot ref and digest"):
        validate(coverage, records, sources)
