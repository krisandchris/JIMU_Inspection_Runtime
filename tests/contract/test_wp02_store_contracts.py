from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from jimu_runtime.bundle.store import LocalBundleStore, StoredAdmission
from tests.fixtures.wp02.store.support import (
    ACTOR,
    PARAMETERS,
    raw_zip,
    rejection,
    verdict,
)


@pytest.mark.parametrize("is_identified", [True, False])
def test_persisted_operation_schema_roundtrip(
    tmp_path: Path,
    is_identified: bool,
) -> None:
    stored = LocalBundleStore(tmp_path)._admit(
        "operation",
        raw_zip(),
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: verdict("receipt") if is_identified else rejection(),
    )
    schema = StoredAdmission.model_json_schema(mode="serialization")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(stored.model_dump(mode="json"))
    assert StoredAdmission.model_validate_json(stored.model_dump_json()) == stored


def test_persisted_operation_requires_discriminant_and_version(tmp_path: Path) -> None:
    stored = LocalBundleStore(tmp_path)._admit(
        "operation",
        b"invalid",
        actor=ACTOR,
        parameters_digest=PARAMETERS,
        verify=lambda _: rejection(),
    )
    payload = stored.model_dump(mode="json")
    payload["schema_version"] = "2.0.0"
    payload["verification"]["result"]["kind"] = "accepted"
    payload["verification"]["result"]["bundle_id"] = "invented"
    with pytest.raises(ValidationError):
        StoredAdmission.model_validate(payload)
