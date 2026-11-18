"""Shared RFC3339 UTC semantics across JSON mode, nested policies and runtime data."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_support import OptionalContract, UTCDateTime
from jimu_runtime.domain.policies import (
    LocalPrimaryProviderPolicy,
    ProviderPolicyArtifact,
)
from jimu_runtime.domain.runtime.facts import RuntimeUTCDateTime


class Window(OptionalContract):
    valid_from: UTCDateTime
    valid_until: UTCDateTime


class NestedWindow(OptionalContract):
    window: Window


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01t00:00:00z",
        "2026-01-01T00:00:00.123456Z",
    ],
)
def test_json_utc_lexical_forms_agree_with_schema(value: str) -> None:
    adapter = TypeAdapter(UTCDateTime)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    timestamp = adapter.validate_json(json.dumps(value))
    assert timestamp.utcoffset() == timedelta(0)
    assert TypeAdapter(RuntimeUTCDateTime).validate_json(json.dumps(value)) == timestamp


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "1234567890",
        "0.0",
        "2026-01-01 00:00:00Z",
        "2026-01-01_00:00:00Z",
        "20260101T000000Z",
        "2026-01-01",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00+08:00",
        "2026-01-01T00:00:00-00:00",
        "2026-01-01T00:00:00+0000",
        "2026-01-01T24:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-01-01T00:60:00Z",
        "2016-12-31T23:59:60Z",
        "2026-01-01T00:00:00Z\n",
        "2026-01-01T00:00:00Z ",
        " 2026-01-01T00:00:00Z",
        0,
        1.0,
        True,
        None,
    ],
)
def test_invalid_json_spelling_is_not_an_epoch_or_coerced_datetime(
    value: object,
) -> None:
    adapter = TypeAdapter(UTCDateTime)
    for annotation in (UTCDateTime, RuntimeUTCDateTime):
        with pytest.raises(ValidationError):
            TypeAdapter(annotation).validate_json(json.dumps(value))
    assert not Draft202012Validator(
        adapter.json_schema(), format_checker=FormatChecker()
    ).is_valid(value)


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30T00:00:00Z",
        "0000-01-01T00:00:00Z",
    ],
)
def test_calendar_validity_is_checked_as_well_as_lexical_shape(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(UTCDateTime).validate_json(json.dumps(value))
    schema = TypeAdapter(UTCDateTime).json_schema()
    assert schema["format"] == "date-time"
    # Calendar validation is a second phase, not a claim that this lexical
    # regex implements the Gregorian calendar. The locked CPU environment does
    # not install the optional jsonschema date-time format checker dependency.
    assert Draft202012Validator(schema).is_valid(value)


def test_python_mode_stays_strict_aware_utc_only() -> None:
    adapter = TypeAdapter(UTCDateTime)
    valid = datetime(2026, 1, 1, tzinfo=UTC)
    assert adapter.validate_python(valid) is valid
    for invalid in (
        "2026-01-01T00:00:00Z",
        0,
        0.0,
        True,
        None,
        valid.replace(tzinfo=None),
        valid.astimezone(timezone(timedelta(hours=8))),
    ):
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid)


def test_optional_wrap_nested_json_keeps_datetime_mode_and_revalidation() -> None:
    raw = {
        "window": {
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": "2027-01-01T00:00:00Z",
        }
    }
    value = NestedWindow.model_validate_json(json.dumps(raw))
    assert NestedWindow.model_validate_json(value.model_dump_json()) == value
    assert NestedWindow.model_validate(value) == value
    forged = value.window.model_copy(update={"valid_from": "2026-01-01T00:00:00Z"})
    with pytest.raises(ValidationError):
        NestedWindow.model_validate(value.model_copy(update={"window": forged}))


@pytest.mark.parametrize("artifact", [False, True])
def test_actual_local_primary_exception_nested_policy_json(artifact: bool) -> None:
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json").read_text()
    )["local_provider"]
    model = ProviderPolicyArtifact if artifact else LocalPrimaryProviderPolicy
    raw = data if artifact else data["policy"]
    value = model.model_validate_json(json.dumps(raw))
    assert model.model_validate_json(value.model_dump_json()) == value
    assert model.model_validate(value) == value
    Draft202012Validator(
        model.model_json_schema(), format_checker=FormatChecker()
    ).validate(raw)
