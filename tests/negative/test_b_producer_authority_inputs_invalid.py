"""Known directory/pin contradictions fail before selective interpretation."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x3.support import fixture, parse
from pydantic import ValidationError


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_catalogues_never_win_by_order(changed):
    data = fixture()
    duplicate = deepcopy(data["producer_catalogues"][0])
    if changed:
        duplicate["registration"]["ref"]["digest"] = "b" * 64
    data["producer_catalogues"].append(duplicate)
    with pytest.raises(ValidationError, match="Catalogue contexts require unique"):
        parse(data)


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_execution_pins_never_win_by_order(changed):
    data = fixture()
    duplicate = deepcopy(data["execution_pins"][0])
    if changed:
        duplicate["release_id"] = "foreign-release"
    data["execution_pins"].append(duplicate)
    with pytest.raises(ValidationError, match="unique execution_id"):
        parse(data)


@pytest.mark.parametrize(
    "change",
    [
        "known_catalogue_digest",
        "missing_profile",
        "extra_profile",
        "profile_digest",
        "duplicate_profile",
        "conflicting_profile",
    ],
)
def test_known_catalogue_and_profile_set_contradictions(change):
    data = fixture()
    pin = data["execution_pins"][0]
    if change == "known_catalogue_digest":
        pin["producer_catalogue_ref"]["digest"] = "b" * 64
    elif change == "missing_profile":
        pin["configured_profile_refs"] = []
    elif change == "profile_digest":
        pin["configured_profile_refs"][0]["digest"] = "b" * 64
    else:
        extra = deepcopy(pin["configured_profile_refs"][0])
        if change == "extra_profile":
            extra["artifact_id"] = "foreign-profile"
        if change == "conflicting_profile":
            extra["digest"] = "b" * 64
        pin["configured_profile_refs"].append(extra)
    with pytest.raises(ValidationError):
        parse(data)


@pytest.mark.parametrize("directory", ["questions", "tools", "plans"])
@pytest.mark.parametrize("change", ["digest", "body"])
def test_history_and_context_conflicts_even_for_unused_definition(directory, change):
    data = fixture()
    supplied = deepcopy(data["producer_catalogues"][0]["context"][directory][0])
    data["history"][directory] = [supplied]
    if change == "digest":
        supplied["ref"]["digest"] = "b" * 64
    elif directory == "questions":
        supplied["definition"]["objective"] = "Contradictory actual objective."
    elif directory == "tools":
        supplied["definition"]["capability_tags"] = []
    else:
        supplied["definition"]["steps"] = []
    with pytest.raises(ValidationError, match="sources contradict"):
        parse(data)


@pytest.mark.parametrize("field", ["producer_catalogues", "execution_pins"])
def test_input_directory_bound(field):
    data = fixture()
    data[field] *= 1025
    with pytest.raises(ValidationError):
        parse(data)


def test_pin_catalogue_reference_is_exact_four_field_kind():
    for changed in (
        {"artifact_kind": "policy"},
        {"schema_dialect": "https://json-schema.org/draft/2020-12/schema"},
        {"digest": "invalid"},
    ):
        data = fixture()
        data["execution_pins"][0]["producer_catalogue_ref"].update(changed)
        with pytest.raises(ValidationError):
            parse(data)
