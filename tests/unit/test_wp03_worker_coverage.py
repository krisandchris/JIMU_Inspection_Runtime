from pathlib import Path

import pytest

from jimu_runtime.domain.enums import DecisionResult, NodeRunStatus
from jimu_runtime.domain.runtime.coverage import CoverageReason, CoverageStatus
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome, GroundingOutcome
from jimu_runtime.workflow.worker_coverage import _summary
from tests.fixtures.wp03.coverage.scenario import scenario


@pytest.mark.parametrize(
    "mode,status,result,discovery,grounding",
    [
        (
            "violation",
            CoverageStatus.COMPLETE,
            DecisionResult.VIOLATION,
            DiscoveryOutcome.HITS,
            GroundingOutcome.OBJECTS,
        ),
        (
            "negative",
            CoverageStatus.COMPLETE,
            DecisionResult.NO_VIOLATION,
            DiscoveryOutcome.HITS,
            GroundingOutcome.OBJECTS,
        ),
        (
            "no-hits",
            CoverageStatus.NONE,
            DecisionResult.UNCERTAIN,
            DiscoveryOutcome.NO_HITS,
            GroundingOutcome.NOT_EVALUATED,
        ),
        (
            "no-target",
            CoverageStatus.NONE,
            DecisionResult.UNCERTAIN,
            DiscoveryOutcome.HITS,
            GroundingOutcome.NO_TARGET,
        ),
        (
            "discovery-failed",
            CoverageStatus.NONE,
            DecisionResult.UNCERTAIN,
            DiscoveryOutcome.FAILED,
            GroundingOutcome.NOT_EVALUATED,
        ),
        (
            "grounding-failed",
            CoverageStatus.NONE,
            DecisionResult.UNCERTAIN,
            DiscoveryOutcome.HITS,
            GroundingOutcome.FAILED,
        ),
        (
            "grounding-unreached",
            CoverageStatus.NONE,
            DecisionResult.UNCERTAIN,
            DiscoveryOutcome.HITS,
            GroundingOutcome.NOT_EVALUATED,
        ),
        (
            "excluded",
            CoverageStatus.NOT_APPLICABLE,
            DecisionResult.NOT_APPLICABLE,
            DiscoveryOutcome.NOT_EVALUATED,
            GroundingOutcome.NOT_EVALUATED,
        ),
    ],
)
def test_final_coverage_retains_outcomes(
    tmp_path: Path,
    mode: str,
    status: CoverageStatus,
    result: DecisionResult,
    discovery: DiscoveryOutcome,
    grounding: GroundingOutcome,
) -> None:
    fixture = scenario(tmp_path, mode=mode)
    finalization = fixture.finalize()
    item = finalization.coverage
    assert (item.coverage_status, item.summary_result, item.discovery_outcome) == (
        status,
        result,
        discovery,
    )
    assert item.profiles[0].grounding_outcome is grounding
    assert bool(item.matches) is (result is DecisionResult.VIOLATION)
    assert len(item.mandatory_rule_coverage.obligations) == 3
    assert bool(finalization.decisions) is (status is CoverageStatus.COMPLETE)


@pytest.mark.parametrize("mode", ["mixed", "partial-discovery"])
def test_incomplete_scope_preserves_proven_violation(tmp_path: Path, mode: str) -> None:
    case = scenario(tmp_path, mode=mode)
    finalization = case.finalize()
    coverage = finalization.coverage
    assert coverage.coverage_status is CoverageStatus.PARTIAL
    assert coverage.summary_result is DecisionResult.VIOLATION
    assert len(finalization.decisions) == len(coverage.matches) == 1
    assert len(coverage.mandatory_rule_coverage.obligations) == 6
    if mode == "mixed":
        n03 = next(
            item for item in case.sources.node_runs if item.node_id is NodeId.N03
        )
        assert n03.status is NodeRunStatus.SUCCEEDED
        assert coverage.profiles[1].grounding_outcome is GroundingOutcome.FAILED
        assert CoverageReason.GROUNDING_FAILED in coverage.profiles[1].reasons
    else:
        assert coverage.discovery_outcome is DiscoveryOutcome.PARTIAL
        assert coverage.profiles[1].grounding_outcome is GroundingOutcome.NOT_EVALUATED


@pytest.mark.parametrize(
    "mode,reason,decision_count",
    [
        ("tool-failed", CoverageReason.REQUIRED_EVIDENCE_BLOCKED, 1),
        ("decision-failed", CoverageReason.DECISION_NOT_EVALUATED, 0),
    ],
)
def test_required_work_gap_does_not_become_negative(
    tmp_path: Path,
    mode: str,
    reason: CoverageReason,
    decision_count: int,
) -> None:
    case = scenario(tmp_path, mode=mode)
    finalization = case.finalize()
    coverage = finalization.coverage
    assert coverage.coverage_status is CoverageStatus.PARTIAL
    assert coverage.summary_result is DecisionResult.UNCERTAIN
    assert len(finalization.decisions) == decision_count
    assert not coverage.matches
    assert reason in coverage.profiles[0].reasons


def test_context_failure_preserves_attempt_free_descendant_blockers(
    tmp_path: Path,
) -> None:
    case = scenario(tmp_path, mode="context-failed")
    finalization = case.finalize()
    coverage = finalization.coverage
    assert coverage.coverage_status is CoverageStatus.NONE
    assert coverage.summary_result is DecisionResult.UNCERTAIN
    assert finalization.decisions == ()
    assert coverage.discovery_outcome is DiscoveryOutcome.NOT_EVALUATED
    assert CoverageReason.CONTEXT_BUILD_BLOCKED in coverage.reasons
    assert len(case.sources.attempts) == 1
    assert len(coverage.mandatory_rule_coverage.obligations) == 3
    assert all(
        item.outcome.disposition == "dependency_not_evaluated"
        for item in coverage.mandatory_rule_coverage.obligations
    )


def test_empty_configured_scope_does_not_establish_a_negative() -> None:
    assert _summary((), (), DiscoveryOutcome.NOT_EVALUATED, ()) == (
        CoverageStatus.NONE,
        DecisionResult.UNCERTAIN,
    )
