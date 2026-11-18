"""Owner domain/allowlist constraints remain data validation, not applicability decisions."""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.policies import PolicyArtifact


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/x1/contracts.json").read_text()
        ),
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_domain",
        "unlisted_context",
        "duplicate_context",
        "duplicate_identity",
        "null_description",
        "fact_hidden",
        "local_hidden",
        "unknown_kind",
    ],
)
def test_applicability_strict_owner_contract(mutation: str) -> None:
    data = fixture()
    raw = cast(dict[str, dict[str, object]], data["artifacts"])["applicability"]
    policy = cast(dict[str, object], raw["policy"])
    if mutation == "wrong_domain":
        policy["input_domain"] = "tool_condition"
    elif mutation == "unlisted_context":
        policy["context_allowlist"] = []
    elif mutation == "duplicate_context":
        cast(list[object], policy["context_allowlist"]).append(
            cast(list[object], policy["context_allowlist"])[0]
        )
    elif mutation == "duplicate_identity":
        cast(list[object], policy["identity_constraints"]).append(
            cast(list[object], policy["identity_constraints"])[0]
        )
    elif mutation == "null_description":
        policy["description"] = None
    elif mutation in {"fact_hidden", "local_hidden"}:
        name = "fact_ref" if mutation == "fact_hidden" else "local_tool_fact_ref"
        policy["condition_ast"] = {
            "kind": "all",
            "arguments": [
                data["bool_false"],
                cast(dict[str, object], data["expressions"])[name],
            ],
        }
    else:
        policy["policy_kind"] = "arbitrary"
    with pytest.raises(ValidationError):
        TypeAdapter(PolicyArtifact).validate_python(raw)


@pytest.mark.parametrize(
    "name",
    [
        "input_domain",
        "context_allowlist",
        "identity_constraints",
        "condition_ast",
        "on_unknown",
    ],
)
def test_applicability_required_fields_have_no_default(name: str) -> None:
    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])["applicability"]
    cast(dict[str, object], raw["policy"]).pop(name)
    with pytest.raises(ValidationError):
        TypeAdapter(PolicyArtifact).validate_python(raw)
