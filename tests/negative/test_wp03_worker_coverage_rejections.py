from dataclasses import replace
from pathlib import Path

import pytest

from jimu_runtime.domain.runtime.perception import GroundingOutcome
from jimu_runtime.domain.validation.issues import ValidationIssue
from tests.fixtures.wp03.coverage.scenario import scenario


def test_cannot_drop_selected_n06_work(tmp_path: Path) -> None:
    case = scenario(tmp_path)
    with pytest.raises(ValueError, match="exact selected successful N06 drafts"):
        replace(case, stages=()).finalize()


def test_cannot_duplicate_selected_n06_work(tmp_path: Path) -> None:
    case = scenario(tmp_path)
    with pytest.raises(ValueError, match="exact selected successful N06 drafts"):
        replace(case, stages=case.stages * 2).finalize()


@pytest.mark.parametrize("change", ["missing", "profile", "attempt", "forged"])
def test_mixed_grounding_rejects_mismatched_source(
    tmp_path: Path,
    change: str,
) -> None:
    case = scenario(tmp_path, mode="mixed")
    valid, failed = case.sources.grounding_records
    if change == "missing":
        records = (valid,)
    elif change == "profile":
        records = (valid, failed.model_copy(update={"profile_ref": valid.profile_ref}))
    elif change == "attempt":
        records = (
            valid,
            failed.model_copy(update={"attempt_id": "other-grounding-attempt"}),
        )
    else:
        assert failed.outcome is GroundingOutcome.FAILED
        issue = ValidationIssue(
            code="forged", message="Forged failure", path=(), related_refs=()
        )
        records = (valid, failed.model_copy(update={"validation_issues": (issue,)}))
    with pytest.raises(ValueError, match="selected N03 result digest and Attempt"):
        replace(
            case, sources=case.sources.model_copy(update={"grounding_records": records})
        ).finalize()


def test_guard_ref_cannot_use_unknown_runtime_revision(tmp_path: Path) -> None:
    case = scenario(tmp_path)
    assert case.runtime is not None
    first, *remaining = case.runtime.authority_selections
    forged = first.model_copy(update={"selection_id": "forged-revision"})
    runtime = case.runtime.model_copy(
        update={"authority_selections": (forged, *remaining)}
    )
    with pytest.raises(
        ValueError, match="exact persisted Facts and authority revisions"
    ):
        replace(case, runtime=runtime).finalize()


def test_cannot_change_context_behind_a_persisted_digest(tmp_path: Path) -> None:
    case = scenario(tmp_path, mode="no-hits")
    snapshot = case.sources.context_snapshot
    assert snapshot is not None and snapshot.values
    tampered = snapshot.model_copy(update={"values": ()})
    sources = case.sources.model_copy(update={"context_snapshot": tampered})
    with pytest.raises(ValueError, match="exact selected N01 ContextSnapshot digest"):
        replace(case, sources=sources).finalize()
