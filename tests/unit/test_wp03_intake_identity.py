import pytest
from pydantic import ValidationError

from jimu_runtime.workflow.intake import IntakeIdentity


def test_identity_preserves_authenticated_issuer_and_subject_literals() -> None:
    identity = IntakeIdentity(
        issuer="https://fixture.example/issuer", subject="摄像机-1"
    )
    assert IntakeIdentity.model_validate_json(identity.stable_json()) == identity
    assert identity != identity.model_copy(update={"issuer": identity.issuer.upper()})
    assert identity != identity.model_copy(update={"subject": identity.subject + " "})


@pytest.mark.parametrize(
    "payload",
    (
        {"issuer": "", "subject": "user"},
        {"issuer": "abc\x00def", "subject": "user"},
        {"issuer": "host", "subject": "abc\x00def"},
        {"issuer": "host", "subject": 7},
        {"issuer": "host", "subject": "user", "credential": "forbidden"},
    ),
)
def test_identity_rejects_invalid_coordinates_and_extra_data(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        IntakeIdentity.model_validate(payload)
