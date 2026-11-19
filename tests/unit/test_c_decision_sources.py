import json

import pytest
from fixtures.wp01c.decisions.cases import source_case

from jimu_runtime.domain.runtime.coverage import ExecutionCoverage
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageSources,
    validate_decision_coverage_sources,
)
from jimu_runtime.domain.validation.decision_coverage import DecisionCoverageRecords


@pytest.mark.parametrize("discovery", ["hits", "no_hits", "partial"])
def test_grouped_discovery_with_selected_actual_records(discovery):
    coverage, records, sources = source_case(discovery, two_groups=True)
    validate_decision_coverage_sources(
        ExecutionCoverage.model_validate_json(json.dumps(coverage)),
        DecisionCoverageRecords.model_validate_json(json.dumps(records)),
        CoverageSources.model_validate_json(json.dumps(sources)),
    )


def test_object_scoped_n06_blocker_without_attempt():
    coverage, records, sources = source_case(no_decision=True)
    validate_decision_coverage_sources(
        ExecutionCoverage.model_validate_json(json.dumps(coverage)),
        DecisionCoverageRecords.model_validate_json(json.dumps(records)),
        CoverageSources.model_validate_json(json.dumps(sources)),
    )
