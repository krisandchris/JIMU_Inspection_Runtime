"""Repository projections cannot merge arbitrary fields into immutable domain pins."""

import pytest

from jimu_runtime.domain.enums import ExecutionStatus
from jimu_runtime.domain.runtime.executions import ExecutionRecord
from jimu_runtime.workflow import repository as repo
from tests.fixtures.wp02.releases.support import submission
from tests.fixtures.wp03.repository.support import records


def test_mutable_execution_projection_cannot_override_immutable_pin() -> None:
    _, original, _, _ = records(submission())
    clean_state = repo._state(original, repo._EXECUTION_STATE)
    contaminated = clean_state.replace('{"', '{"pins":null,"', 1)
    with pytest.raises(repo.RepositoryError, match="invalid_mutable_projection"):
        repo._restore_record(
            ExecutionRecord,
            original.stable_json(),
            contaminated,
            ExecutionStatus.INITIALIZED.value,
            repo._EXECUTION_STATE,
        )
    assert (
        repo._restore_record(
            ExecutionRecord,
            original.stable_json(),
            clean_state,
            ExecutionStatus.INITIALIZED.value,
            repo._EXECUTION_STATE,
        )
        == original
    )
