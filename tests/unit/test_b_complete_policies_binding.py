"""Complete Policy registration and independent failure/skip subtype checks."""

import json
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.fact_selectors import ToolPlanContentRef
from jimu_runtime.domain.validation.policies import (
    PolicyRegistration,
    validate_step_failure_policies,
)


def test_step_skip_ref_resolves_exact_complete_subtype() -> None:
    root = Path(__file__).parents[1] / "fixtures/wp01b"
    x1 = cast(
        dict[str, dict[str, dict[str, object]]],
        json.loads((root / "x1/contracts.json").read_text()),
    )
    p1 = cast(dict[str, object], json.loads((root / "p1/contracts.json").read_text()))
    m1 = cast(dict[str, object], json.loads((root / "m1/contracts.json").read_text()))
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        p1["registrations"]
    )
    plan = ToolPlanDefinition.model_validate(m1["plan"])
    plan_ref = ToolPlanContentRef.model_validate(
        cast(dict[str, object], m1["context"])["tool_plan_ref"]
    )
    missing = validate_step_failure_policies(plan, catalogue, plan_ref=plan_ref)
    assert (
        next(
            check
            for check in missing.checks
            if check.check_id == "plan.failure_policies"
        ).status
        == "passed"
    )
    assert (
        next(
            check
            for check in missing.checks
            if check.check_id == "plan.skip_policy_subtypes"
        ).status
        == "incomplete"
    )
    ref = plan.steps[0].skip_policy_ref
    skip = PolicyRegistration.model_validate(
        {"ref": ref, "artifact": x1["artifacts"]["tool_skip"]}
    )
    checked = validate_step_failure_policies(
        plan, (*catalogue, skip), plan_ref=plan_ref
    )
    assert (
        next(
            check
            for check in checked.checks
            if check.check_id == "plan.skip_policy_subtypes"
        ).status
        == "passed"
    )
    assert (
        checked.aggregate_status == "deferred"
    )  # no activation truth/type/use-site proof
    wrong = dict(x1["artifacts"]["unknown"])
    wrong["policy_id"] = ref.artifact_id
    unrelated = PolicyRegistration.model_validate({"ref": ref, "artifact": wrong})
    invalid = validate_step_failure_policies(
        plan, (*catalogue, unrelated), plan_ref=plan_ref
    )
    assert invalid.aggregate_status == "invalid"
    assert any(
        issue.code == "plan.skip_policy_subtype"
        for check in invalid.checks
        for issue in check.issues
    )
