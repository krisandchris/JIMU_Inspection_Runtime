from pathlib import Path

import pytest

from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    ExecutionCoverage,
)
from jimu_runtime.domain.runtime.perception import GroundingOutcome
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageAuthorityInput,
    CoverageSources,
    validate_decision_coverage_sources,
)
from jimu_runtime.domain.validation.decision_coverage import (
    DecisionCoverageRecords,
    validate_decision_coverage,
)
from jimu_runtime.workflow import worker_coverage
from jimu_runtime.workflow.engine import ExecutionFinalization
from tests.fixtures.wp03.coverage.scenario import scenario


def test_ancestor_projection_is_durable_and_has_no_n06_witness(tmp_path: Path) -> None:
    case = scenario(tmp_path, mode="excluded")
    result = case.finalize()
    assert result == ExecutionFinalization.model_validate_json(result.model_dump_json())
    root, *descendants = result.coverage.mandatory_rule_coverage.obligations
    assert len(descendants) == len(result.projected_traces) == 2
    for obligation in descendants:
        assert isinstance(obligation.outcome, AncestorNotEvaluatedObligation)
        assert obligation.outcome.ancestor_obligation_id == root.obligation_id
    for evaluation in (
        *result.projected_rule_set_evaluations,
        *result.projected_rule_evaluations,
    ):
        assert evaluation.attempt_ref is None and evaluation.node_run_ref is None


def test_mixed_grounding_acceptance_requires_verified_actual_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[tuple[DecisionCoverageRecords, CoverageAuthorityInput | None]] = []

    def capture(
        coverage: ExecutionCoverage,
        records: DecisionCoverageRecords,
        sources: CoverageSources,
        *,
        authority: CoverageAuthorityInput | None,
    ) -> None:
        validate_decision_coverage_sources(
            coverage, records, sources, authority=authority
        )
        validated.append((records, authority))

    monkeypatch.setattr(worker_coverage, "validate_decision_coverage_sources", capture)
    case = scenario(tmp_path, mode="mixed")
    finalization = case.finalize()
    ((records, authority),) = validated
    with pytest.raises(ValueError, match="corresponding resolved dependency"):
        validate_decision_coverage(finalization.coverage, records)

    usable, failed = case.sources.grounding_records
    for grounding_records, error in (
        ((usable,), "resolve every evaluated Profile grounding"),
        (
            (usable, failed.model_copy(update={"profile_ref": usable.profile_ref})),
            "duplicate identities",
        ),
        (
            (
                usable,
                failed.model_copy(
                    update={"attempt_id": case.sources.attempts[0].attempt_id}
                ),
            ),
            "actual NodeRun history",
        ),
        (
            (
                usable.model_copy(
                    update={
                        "outcome": GroundingOutcome.FAILED,
                        "object_refs": (),
                        "validation_issues": failed.validation_issues,
                    }
                ),
                failed,
            ),
            "must match its actual GroundingRecord",
        ),
    ):
        with pytest.raises(ValueError, match=error):
            validate_decision_coverage_sources(
                finalization.coverage,
                records,
                case.sources.model_copy(
                    update={"grounding_records": grounding_records}
                ),
                authority=authority,
            )
