"""Complete sixteen-way tagged Policy data, preserving old specialized artifacts."""

import json
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_enums import PolicyKind
from jimu_runtime.domain.policies import (
    NonExpressionPolicyArtifact,
    PolicyArtifact,
    ToolSkipPolicyArtifact,
)
from jimu_runtime.domain.validation.policies import PolicyRegistration, resolve_policy


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/x1/contracts.json").read_text()
        ),
    )


@pytest.mark.parametrize("kind", [kind.value for kind in PolicyKind])
def test_all_sixteen_tagged_artifacts_schema_and_instance_revalidation(
    kind: str,
) -> None:
    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])[kind]
    adapter = TypeAdapter(PolicyArtifact)
    schema = adapter.json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    artifact = adapter.validate_python(raw)
    assert artifact.policy.policy_kind.value == kind
    assert adapter.validate_python(artifact) == artifact
    assert adapter.validate_json(adapter.dump_json(artifact)) == artifact
    assert set(artifact.model_dump(mode="json")) == {
        "schema_version",
        "policy_id",
        "version",
        "policy",
    }
    if kind not in {"applicability", "tool_skip"}:
        old = TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)
        assert type(artifact) is type(old)
        assert artifact == old
    ref = {
        "artifact_kind": "policy",
        "artifact_id": raw["policy_id"],
        "version": raw["version"],
        "digest": "a" * 64,
    }
    registration = PolicyRegistration.model_validate({"ref": ref, "artifact": artifact})
    assert resolve_policy(registration.ref, (registration,)) == artifact


@pytest.mark.parametrize("value", [False, 0, 1, None, "true"])
def test_tool_skip_has_only_explicit_strict_true_shortcut(value: object) -> None:
    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])["tool_skip"]
    cast(dict[str, object], raw["policy"])["condition_ast"] = value
    with pytest.raises(ValidationError):
        TypeAdapter(PolicyArtifact).validate_python(raw)


def test_typed_false_and_real_condition_preserved_without_folding() -> None:
    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])["tool_skip"]
    cast(dict[str, object], raw["policy"])["condition_ast"] = fixture()["bool_false"]
    artifact = TypeAdapter(PolicyArtifact).validate_python(raw)
    assert isinstance(artifact, ToolSkipPolicyArtifact)
    assert artifact.policy.condition_ast is not True
    assert artifact.policy.condition_ast.kind == "literal"


@pytest.mark.parametrize("where", ["artifact", "policy", "constant"])
def test_complete_tagged_union_rejects_copy_taint(where: str) -> None:
    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])["tool_skip"]
    cast(dict[str, object], raw["policy"])["condition_ast"] = fixture()["bool_false"]
    adapter = TypeAdapter(PolicyArtifact)
    artifact = adapter.validate_python(raw)
    if where == "artifact":
        tainted = artifact.model_copy(update={"extra": True})
    elif where == "policy":
        tainted = artifact.model_copy(
            update={"policy": artifact.policy.model_copy(update={"extra": True})}
        )
    else:
        assert isinstance(artifact, ToolSkipPolicyArtifact)
        node = artifact.policy.condition_ast
        assert node is not True and node.kind == "literal"
        tainted = artifact.model_copy(
            update={
                "policy": artifact.policy.model_copy(
                    update={
                        "condition_ast": node.model_copy(
                            update={
                                "constant": node.constant.model_copy(
                                    update={"extra": True}
                                )
                            }
                        )
                    }
                )
            }
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(tainted)


@pytest.mark.parametrize(
    "variant",
    ["remote_resource", "gpu_resource", "local_provider", "evaluated_fallback"],
)
def test_complete_tag_preserves_inner_discriminators(variant: str) -> None:
    data = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json").read_text()
        ),
    )
    if variant in {"local_provider", "evaluated_fallback"}:
        raw = data[variant]
    else:
        raw = cast(dict[str, dict[str, object]], data["artifacts"])[
            "resource_requirements"
        ]
        policy = cast(dict[str, object], raw["policy"])
        if variant == "remote_resource":
            policy.update(resource_class="remote", remote_quota_id="synthetic-quota")
        else:
            policy.update(
                resource_class="gpu",
                gpu_memory_limit_bytes=1,
                worker_pool_id="synthetic-pool",
            )
    adapter = TypeAdapter(PolicyArtifact)
    result = adapter.validate_json(json.dumps(raw))
    Draft202012Validator(adapter.json_schema()).validate(raw)
    assert adapter.validate_python(result) == result
    assert adapter.validate_json(adapter.dump_json(result)) == result


def test_applicability_identity_constraints_are_not_evaluated_or_membership_grants() -> (
    None
):
    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])["applicability"]
    policy = cast(dict[str, object], raw["policy"])
    policy["identity_constraints"] = [
        {
            "artifact_kind": "profile",
            "artifact_id": "another-profile",
            "version_selector": "1.0.0",
        },
        {
            "artifact_kind": "region",
            "artifact_id": "another-region",
            "version_selector": "1.0.0",
        },
    ]
    artifact = TypeAdapter(PolicyArtifact).validate_python(raw)
    assert artifact.policy.policy_kind is PolicyKind.APPLICABILITY
    assert len(artifact.policy.identity_constraints) == 2
