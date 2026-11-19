from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from jimu_runtime.domain.enums import AttemptStatus, RuntimeRecordKind
from jimu_runtime.domain.runtime.executions import AttemptLink, LifecycleReason, NodeId
from jimu_runtime.domain.runtime.facts import AttemptRef
from tests.fixtures.wp03.coverage.scenario import scenario


@pytest.mark.parametrize(
    "change,error",
    [
        ("old", "must pin the final actual Attempt"),
        ("foreign", "must belong to the actual NodeRun history"),
        ("wrong_reason", "matching NodeRun interruption"),
        ("wrong_node_reason", "matching NodeRun interruption"),
        ("missing_prior", "selected actual Attempt"),
        ("no_start", "complete ordered history"),
        ("disordered", "complete ordered history"),
    ],
)
def test_cannot_inject_superseded_attempt_as_decision_blocker(
    tmp_path: Path,
    change: str,
    error: str,
) -> None:
    case = scenario(tmp_path, mode="decision-failed")
    (blocker,) = case.sources.decision_blockers
    assert blocker.attempt_ref is not None and case.runtime is not None
    (n06,) = tuple(
        item.run for item in case.state.runs if item.key.node_id is NodeId.N06
    )
    old = next(
        item
        for item in case.sources.attempts
        if item.attempt_id == blocker.attempt_ref.record_id
    )
    start = case.scope.task.accepted_at
    next_start = start + timedelta(seconds=2)
    next_end = start + timedelta(seconds=3)
    lost = LifecycleReason(code="lease_expired")
    old = old.model_copy(
        update={
            "status": AttemptStatus.SUPERSEDED,
            "reason": lost,
            "ended_at": start + timedelta(seconds=1),
        }
    )
    recent_ref = AttemptRef(
        record_kind=RuntimeRecordKind.ATTEMPT, record_id="recent-n06-attempt"
    )
    recent = old.model_copy(
        update={
            "attempt_id": recent_ref.record_id,
            "attempt_number": 2,
            "created_at": next_start,
            "started_at": next_start,
            "ended_at": next_end,
        }
    )
    node_reason = lost
    blocker_ref = recent_ref
    if change == "old":
        blocker_ref = blocker.attempt_ref
    elif change == "foreign":
        foreign = case.sources.attempts[0]
        blocker_ref = AttemptRef(
            record_kind=RuntimeRecordKind.ATTEMPT, record_id=foreign.attempt_id
        )
    elif change == "wrong_reason":
        recent = recent.model_copy(
            update={"reason": LifecycleReason(code="operator_action")}
        )
    elif change == "wrong_node_reason":
        node_reason = LifecycleReason(code="operator_action")
    elif change == "no_start":
        recent = recent.model_copy(update={"started_at": None})
    elif change == "disordered":
        old = old.model_copy(update={"ended_at": next_end})
    interrupted = n06.model_copy(
        update={
            "ended_at": next_end,
            "reason": node_reason,
            "attempt_history": (
                *n06.attempt_history,
                AttemptLink(attempt_ref=recent_ref, attempt_number=2),
            ),
        }
    )
    attempts = (
        *(item for item in case.sources.attempts if item.attempt_id != old.attempt_id),
        *((old,) if change != "missing_prior" else ()),
        recent,
    )
    runs = tuple(
        replace(item, run=interrupted) if item.key.node_id is NodeId.N06 else item
        for item in case.state.runs
    )
    nodes = tuple(item.run for item in runs)
    state = replace(case.state, runs=runs)
    sources = case.sources.model_copy(
        update={
            "node_runs": nodes,
            "attempts": attempts,
            "decision_blockers": (
                blocker.model_copy(update={"attempt_ref": blocker_ref}),
            ),
        }
    )
    runtime = case.runtime.model_copy(update={"node_runs": nodes, "attempts": attempts})
    with pytest.raises(ValueError, match=error):
        replace(case, state=state, sources=sources, runtime=runtime).finalize()


@pytest.mark.parametrize(
    "reason,error",
    [
        ("ordinary_failure", "requires a recorded scheduler interruption"),
        ("lease_expired", "matching NodeRun interruption"),
        ("attempt_deadline_elapsed", "matching NodeRun interruption"),
    ],
)
def test_n02_without_an_output_must_show_the_actual_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    error: str,
) -> None:
    from jimu_runtime.domain.runtime.coverage import CoverageReason, ExecutionCoverage
    from jimu_runtime.domain.runtime.perception import DiscoveryOutcome
    from jimu_runtime.domain.validation.coverage_sources import (
        CoverageAuthorityInput,
        CoverageSources,
        validate_decision_coverage_sources,
    )
    from jimu_runtime.domain.validation.decision_coverage import DecisionCoverageRecords
    from jimu_runtime.workflow import worker_coverage

    case = scenario(tmp_path, mode="discovery-failed")
    snapshots: list[DecisionCoverageRecords] = []

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
        snapshots.append(records)

    monkeypatch.setattr(worker_coverage, "validate_decision_coverage_sources", capture)
    finalization = case.finalize()
    (records,) = snapshots
    coverage = finalization.coverage.model_copy(
        update={
            "discovery_outcome": DiscoveryOutcome.NOT_EVALUATED,
            "discovery_model_invocation_refs": (),
            "reasons": (
                CoverageReason.DISCOVERY_NOT_EVALUATED,
                *(
                    item
                    for item in finalization.coverage.reasons
                    if item is not CoverageReason.DISCOVERY_FAILED
                ),
            ),
        }
    )
    sources = case.sources.model_copy(
        update={
            "discovery_input": None,
            "discovery_output": None,
            "node_runs": tuple(
                item.model_copy(update={"reason": LifecycleReason(code=reason)})
                if item.node_id is NodeId.N02
                else item
                for item in case.sources.node_runs
            ),
        }
    )
    with pytest.raises(ValueError, match=error):
        validate_decision_coverage_sources(coverage, records, sources)
