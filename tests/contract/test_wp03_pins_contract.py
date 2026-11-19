import json
from pathlib import Path
from uuid import UUID

import pytest

from jimu_runtime.domain.runtime.executions import ExecutionPins
from tests.fixtures.wp03.pins.support import accepted_scenario

_VECTOR = Path(__file__).parents[1] / "fixtures/wp03/pins/input_digest_vector.json"


def test_version_one_fixed_execution_pin_vector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the admitted receipt's ID source is fixed. Signing, independent
    # verification, store reads and authorized acceptance all use real code.
    monkeypatch.setattr("jimu_runtime.bundle.admission.uuid4", lambda: UUID(int=3))
    resolved = accepted_scenario(tmp_path).resolve()
    vector = json.loads(_VECTOR.read_bytes())
    assert resolved.policy_ref.model_dump(mode="json") == vector["policy_ref"]
    assert resolved.pins.model_dump(mode="json") == vector["pins"]
    assert [
        item.model_dump(mode="json") for item in resolved.model_deployments
    ] == vector["model_deployments"]
    assert (
        ExecutionPins.model_validate_json(json.dumps(vector["pins"])) == resolved.pins
    )
