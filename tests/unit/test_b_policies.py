"""Finite review coverage, budget and context-graph tests with synthetic resolver data."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import TypeAdapter

from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.policies import (
    ContextProvenancePolicyArtifact,
    NonExpressionPolicyArtifact,
)
from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.tool_mappings import ProfileContentRef, RegionContentRef
from jimu_runtime.domain.validation.issues import ValidationReport
from jimu_runtime.domain.validation.policies import (
    PolicyRegistration,
    PromptRegistration,
    validate_context_derivations,
    validate_model_policies,
    validate_tool_policy_bindings,
)
from jimu_runtime.domain.validation.review_inputs import ReviewMetadata


def fixture() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/p1/contracts.json").read_text()
        ),
    )


def evaluate(
    data: dict[str, object],
    *,
    reviews: bool = True,
    prompts: tuple[PromptRegistration, ...] | None = None,
    profile_ref: ProfileContentRef | None = None,
    region_ref: RegionContentRef | None = None,
) -> ValidationReport:
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_json(
        json.dumps(data["registrations"])
    )
    model = ModelBinding.model_validate(data["model"])
    metadata = (
        (ReviewMetadata.model_validate_json(json.dumps(data["review"])),)
        if reviews
        else ()
    )
    if prompts is None:
        prompts = (PromptRegistration.model_validate(data["prompt"]),)
    return validate_model_policies(
        model,
        catalogue,
        metadata,
        datetime(2026, 6, 1, tzinfo=UTC),
        binding_ref=ModelBindingContentRef.model_validate(data["binding_ref"]),
        prompts=prompts,
        profile_ref=profile_ref,
        region_ref=region_ref,
    )


def with_fallback(data: dict[str, object]) -> None:
    for entry in cast(list[dict[str, object]], data["registrations"]):
        if cast(dict[str, object], entry["ref"])["artifact_id"] == "fallback":
            entry["artifact"] = data["evaluated_fallback"]


def test_remote_primary_disabled_fallback_and_same_provider_multiple_deployments() -> (
    None
):
    report = evaluate(fixture(), reviews=False)
    assert (
        next(c for c in report.checks if c.check_id == "model.fallback_disabled").status
        == "passed"
    )
    assert (
        next(
            c for c in report.checks if c.check_id == "model.prompt_compatibility"
        ).status
        == "passed"
    )
    assert (
        report.aggregate_status == "deferred"
    )  # actual trust/quality is not established


def test_exact_transition_review_coverage() -> None:
    data = fixture()
    with_fallback(data)
    report = evaluate(data)
    assert (
        next(c for c in report.checks if c.check_id == "model.fallback_coverage").status
        == "passed"
    )
    assert evaluate(data, reviews=False).aggregate_status == "incomplete"


@pytest.mark.parametrize(
    "mutation",
    [
        "trigger",
        "source",
        "target_version",
        "scope",
        "io",
        "expired",
        "rejected",
        "logical_model",
        "missing_endpoint",
        "permitted_version_unreviewed",
    ],
)
def test_fallback_cannot_borrow_unrelated_review(mutation: str) -> None:
    data = fixture()
    with_fallback(data)
    review = cast(dict[str, object], data["review"])
    assessment = cast(list[dict[str, object]], review["assessments"])[0]
    if mutation == "trigger":
        assessment["trigger_errors"] = ["rate_limit"]
    elif mutation == "source":
        cast(dict[str, object], assessment["source"])["deployment_id"] = "different"
    elif mutation == "target_version":
        cast(dict[str, object], assessment["target"])["model_version"] = "2.0.0"
    elif mutation == "scope":
        cast(
            list[dict[str, object]],
            cast(dict[str, object], assessment["scope"])["binding_refs"],
        )[0]["digest"] = "b" * 64
    elif mutation == "io":
        cast(dict[str, object], assessment["output_schema_ref"])["digest"] = "b" * 64
    elif mutation == "expired":
        review["valid_until"] = "2026-02-01T00:00:00Z"
    elif mutation == "rejected":
        review["verdict"] = "rejected"
    else:
        registrations = cast(list[dict[str, object]], data["registrations"])
        provider = next(
            cast(dict[str, object], e["artifact"])
            for e in registrations
            if cast(dict[str, object], e["ref"])["artifact_id"] == "provider"
        )
        deployments = cast(
            list[dict[str, object]],
            cast(dict[str, object], provider["policy"])["deployments"],
        )
        if mutation == "logical_model":
            deployments[0]["logical_model_id"] = "wrong"
        elif mutation == "missing_endpoint":
            deployments[1]["deployment_id"] = "different"
        else:
            cast(dict[str, object], deployments[1]["actual_version_constraints"])[
                "allowed_model_versions"
            ] = ["1.0.0", "2.0.0"]
    assert evaluate(data).aggregate_status == "invalid"


def test_local_primary_exception_needs_exact_report_and_capability() -> None:
    data = fixture()
    for entry in cast(list[dict[str, object]], data["registrations"]):
        if cast(dict[str, object], entry["ref"])["artifact_id"] == "provider":
            entry["artifact"] = data["local_provider"]
    report = evaluate(data)
    assert (
        next(c for c in report.checks if c.check_id == "model.local_exception").status
        == "passed"
    )
    assert evaluate(data, reviews=False).aggregate_status == "incomplete"
    assessment = cast(
        list[dict[str, object]], cast(dict[str, object], data["review"])["assessments"]
    )[1]
    assessment["reason"] = "explicit_local_capability"
    assert evaluate(data).aggregate_status == "invalid"


def test_tool_execution_budgets_only_tighten_resources() -> None:
    data = fixture()
    m1 = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/m1/contracts.json").read_text()
        ),
    )
    tool = ToolDefinition.model_validate(m1["tool"])
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        data["registrations"]
    )
    assert validate_tool_policy_bindings(tool, catalogue).aggregate_status == "passed"
    for entry in cast(list[dict[str, object]], data["registrations"]):
        if cast(dict[str, object], entry["ref"])["artifact_id"] == "execution":
            cast(
                dict[str, object], cast(dict[str, object], entry["artifact"])["policy"]
            )["timeout_ms"] = 101
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        data["registrations"]
    )
    assert validate_tool_policy_bindings(tool, catalogue).aggregate_status == "invalid"


def test_declared_context_roots_and_cycle_are_separate() -> None:
    data = fixture()
    raw = cast(dict[str, dict[str, object]], data["artifacts"])["context_provenance"]
    model = ContextProvenancePolicyArtifact.model_validate(raw)
    descriptor = TypeAdapter(SchemaDescriptor).validate_python(
        data["context_descriptor"]
    )
    assert (
        validate_context_derivations(model.policy, (descriptor,)).aggregate_status
        == "deferred"
    )
    field = cast(
        list[dict[str, object]], cast(dict[str, object], raw["policy"])["fields"]
    )[0]
    field["allowed_sources"] = ["declared_derivation"]
    field["derivation_requirement"] = {
        "family": "context_derivation",
        "capability_id": "derive",
        "contract_version": "1.0.0",
        "allowed_implementation_versions": ["1.0.0"],
        "required_features": [],
    }
    field["input_selectors"] = [
        {
            "source": "context_snapshot",
            "schema_ref": field["schema_ref"],
            "field_path": field["field_path"],
        }
    ]
    model = ContextProvenancePolicyArtifact.model_validate(raw)
    report = validate_context_derivations(model.policy, (descriptor,))
    assert report.aggregate_status == "invalid"
    assert report.checks[0].issues[0].code == "context.derivation_cycle"
    field["input_selectors"] = [
        {
            "source": "region_metadata",
            "schema_ref": field["schema_ref"],
            "field_path": field["field_path"],
        }
    ]
    model = ContextProvenancePolicyArtifact.model_validate(raw)
    assert (
        validate_context_derivations(model.policy, (descriptor,)).aggregate_status
        == "incomplete"
    )


def test_all_resource_placements_and_full_retry_schedule_support() -> None:
    data = fixture()
    artifacts = cast(dict[str, dict[str, object]], data["artifacts"])
    raw = deepcopy(artifacts["resource_requirements"])
    policy = cast(dict[str, object], raw["policy"])
    policy.update(resource_class="remote", remote_quota_id="quota")
    assert (
        TypeAdapter(NonExpressionPolicyArtifact)
        .validate_python(raw)
        .policy.policy_kind.value
        == "resource_requirements"
    )
    del policy["remote_quota_id"]
    policy.update(resource_class="gpu", gpu_memory_limit_bytes=1, worker_pool_id="pool")
    assert (
        TypeAdapter(NonExpressionPolicyArtifact)
        .validate_python(raw)
        .policy.policy_kind.value
        == "resource_requirements"
    )
    retry = deepcopy(artifacts["tool_execution"])
    cast(dict[str, object], retry["policy"]).update(
        max_attempts=1025, retry_backoff_ms=[1] * 1024
    )
    assert (
        TypeAdapter(NonExpressionPolicyArtifact)
        .validate_python(retry)
        .policy.policy_kind.value
        == "tool_execution"
    )


def test_optional_selection_context_and_both_model_call_branches() -> None:
    data = fixture()
    artifacts = cast(dict[str, dict[str, object]], data["artifacts"])
    raw = deepcopy(artifacts["question_selection"])
    policy = cast(dict[str, object], raw["policy"])
    question = cast(list[dict[str, object]], policy["mandatory_question_refs"])[0]
    policy.update(
        mode="mandatory_plus_optional",
        mandatory_question_refs=[],
        optional_question_refs=[question],
        optional_source="context_rules",
        context_selection_rules=[
            {
                "question_ref": question,
                "applicability_policy_ref": {
                    "artifact_kind": "policy",
                    "artifact_id": "applicability",
                    "version": "1.0.0",
                    "digest": "a" * 64,
                },
            }
        ],
    )
    TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)
    del policy["context_selection_rules"]
    policy.update(
        optional_source="model_advisory",
        proposal_schema_ref=cast(dict[str, object], data["model"])["output_schema_ref"],
        selection_review_ref=cast(dict[str, object], data["review"])["report_ref"],
        selection_call_policy={"mode": "shared_n02"},
    )
    # proposal_schema_ref is ContentRef, not SchemaRef; retain its authoritative wire.
    cast(dict[str, object], policy["proposal_schema_ref"]).pop("schema_dialect")
    TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)
    policy["selection_call_policy"] = {
        "mode": "explicit_additional",
        "proposal_binding_ref": data["binding_ref"],
        "additional_resource_requirements_ref": cast(dict[str, object], data["model"])[
            "resource_requirements_ref"
        ],
        "max_additional_invocations_per_execution": 1,
    }
    TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


def test_spatial_guard_is_not_forced_on_ordinary_tool_output() -> None:
    data = fixture()
    raw = deepcopy(cast(dict[str, dict[str, object]], data["artifacts"])["evidence"])
    policy = cast(dict[str, object], raw["policy"])
    policy.update(original_media_required=False, allowed_evidence_types=["tool_output"])
    TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)
    policy.update(allowed_evidence_types=["mask"], transform_required_for_derived=True)
    TypeAdapter(NonExpressionPolicyArtifact).validate_python(raw)


def test_context_external_owner_root_does_not_require_snapshot_output_declaration() -> (
    None
):
    from jimu_runtime.domain.tool_mappings import ContextTypeRegistration

    data = fixture()
    raw = deepcopy(
        cast(dict[str, dict[str, object]], data["artifacts"])["context_provenance"]
    )
    field = cast(
        list[dict[str, object]], cast(dict[str, object], raw["policy"])["fields"]
    )[0]
    field.update(
        allowed_sources=["declared_derivation"],
        derivation_requirement={
            "family": "context_derivation",
            "capability_id": "derive",
            "contract_version": "1.0.0",
            "allowed_implementation_versions": ["1.0.0"],
            "required_features": [],
        },
        input_selectors=[
            {
                "source": "region_metadata",
                "schema_ref": field["schema_ref"],
                "field_path": field["field_path"],
            }
        ],
    )
    model = ContextProvenancePolicyArtifact.model_validate(raw)
    descriptor = TypeAdapter(SchemaDescriptor).validate_python(
        data["context_descriptor"]
    )
    root = TypeAdapter(ContextTypeRegistration).validate_python(
        {
            "source": "region_metadata",
            "schema_ref": field["schema_ref"],
            "region_ref": {
                "artifact_kind": "region",
                "artifact_id": "region",
                "version": "1.0.0",
                "digest": "a" * 64,
            },
        }
    )
    report = validate_context_derivations(model.policy, (descriptor,), (root,))
    assert report.checks[0].status == "passed"
    assert report.aggregate_status == "deferred"


def test_retention_vocabulary_requires_exact_identity_member_and_domain() -> None:
    from jimu_runtime.domain.policies import EvidencePolicyArtifact
    from jimu_runtime.domain.validation.policies import (
        RetentionVocabularyRegistration,
        validate_evidence_retention,
    )

    raw = cast(dict[str, dict[str, object]], fixture()["artifacts"])["evidence"]
    policy = EvidencePolicyArtifact.model_validate(raw).policy
    assert validate_evidence_retention(policy, ()).aggregate_status == "incomplete"
    registration = RetentionVocabularyRegistration.model_validate(
        {
            "ref": policy.retention_class.vocabulary_ref,
            "definition": {
                "schema_version": "1.0.0",
                "vocabulary_id": "retention",
                "version": "1.0.0",
                "semantic_domain": "evidence.retention_class",
                "members": ["synthetic"],
            },
        }
    )
    assert (
        validate_evidence_retention(policy, (registration,)).aggregate_status
        == "passed"
    )
    wrong = registration.model_copy(
        update={
            "definition": registration.definition.model_copy(
                update={"semantic_domain": "other"}
            )
        }
    )
    assert validate_evidence_retention(policy, (wrong,)).aggregate_status == "invalid"


def test_question_partition_and_execution_refs_have_finite_checks() -> None:
    from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
    from jimu_runtime.domain.definitions.models import PolicyContentRef
    from jimu_runtime.domain.policies import (
        ExecutionPolicyArtifact,
        QuestionSelectionPolicyArtifact,
    )
    from jimu_runtime.domain.validation.policies import (
        validate_execution_policy,
        validate_question_selection,
    )

    data = fixture()
    artifacts = cast(dict[str, dict[str, object]], data["artifacts"])
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        data["registrations"]
    )
    selection = QuestionSelectionPolicyArtifact.model_validate(
        artifacts["question_selection"]
    )
    profile = ProfileDefinition.model_validate(data["selection_profile"])
    policy_ref = PolicyContentRef.model_validate(
        profile.question_selection_policy_ref.model_dump(mode="json")
    )
    report = validate_question_selection(
        selection, profile, catalogue, policy_ref=policy_ref
    )
    assert report.checks[0].status == "passed"
    assert report.aggregate_status == "deferred"
    wrong = profile.model_copy(
        update={
            "question_refs": (
                profile.question_refs[0].model_copy(update={"digest": "b" * 64}),
            )
        }
    )
    assert (
        validate_question_selection(
            selection, wrong, catalogue, policy_ref=policy_ref
        ).aggregate_status
        == "invalid"
    )
    execution = ExecutionPolicyArtifact.model_validate(artifacts["execution"])
    assert validate_execution_policy(execution, catalogue).checks[0].status == "passed"
    assert validate_execution_policy(execution, ()).aggregate_status == "incomplete"


def test_step_failure_requirement_and_nested_model_capability() -> None:
    from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
    from jimu_runtime.domain.fact_selectors import ToolPlanContentRef
    from jimu_runtime.domain.validation.policies import (
        ModelBindingRegistration,
        validate_step_failure_policies,
    )

    data = fixture()
    m1 = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/m1/contracts.json").read_text()
        ),
    )
    plan = ToolPlanDefinition.model_validate(m1["plan"])
    context = cast(dict[str, object], m1["context"])
    plan_ref = ToolPlanContentRef.model_validate(context["tool_plan_ref"])
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        data["registrations"]
    )
    assert (
        validate_step_failure_policies(plan, catalogue, plan_ref=plan_ref)
        .checks[0]
        .status
        == "passed"
    )
    toolraw = cast(dict[str, object], m1["tool"])
    toolraw["model_binding_ref"] = data["binding_ref"]
    tool = ToolDefinition.model_validate(toolraw)
    assert (
        validate_tool_policy_bindings(tool, catalogue).aggregate_status == "incomplete"
    )
    binding = ModelBindingRegistration.model_validate(
        {"ref": data["binding_ref"], "definition": data["model"]}
    )
    assert (
        validate_tool_policy_bindings(tool, catalogue, (binding,)).aggregate_status
        == "invalid"
    )
    auxiliary = binding.model_copy(
        update={
            "definition": binding.definition.model_copy(
                update={"capability": ModelCapability.VISION_AUXILIARY}
            )
        }
    )
    assert (
        validate_tool_policy_bindings(tool, catalogue, (auxiliary,)).aggregate_status
        == "passed"
    )


@pytest.mark.parametrize(
    "mutation", ["same_ref", "other_purpose", "other_digest", "tainted", "oversized"]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_prompt_catalogue_conflicts_and_taint_precede_first_match(
    mutation: str, reverse: bool
) -> None:
    data = fixture()
    correct = PromptRegistration.model_validate(data["prompt"])
    if mutation == "other_purpose":
        other = correct.model_copy(
            update={
                "definition": correct.definition.model_copy(
                    update={"purpose": ModelCapability.PROFILE_DISCOVERY}
                )
            }
        )
    elif mutation == "other_digest":
        other = correct.model_copy(
            update={"ref": correct.ref.model_copy(update={"digest": "b" * 64})}
        )
    elif mutation == "tainted":
        other = correct.model_copy(update={"unregistered": True})
    else:
        other = correct
    entries = (correct, other)
    if mutation == "oversized":
        entries = (correct,) * 1025
    if reverse:
        entries = entries[::-1]
    report = evaluate(data, prompts=entries)
    assert report.aggregate_status == "invalid"
    expected = (
        "model.prompt_limit"
        if mutation == "oversized"
        else "model.prompt_registration_invalid"
        if mutation == "tainted"
        else "model.prompt_catalogue_conflict"
    )
    assert any(
        issue.code == expected for check in report.checks for issue in check.issues
    )


@pytest.mark.parametrize("family", ["fallback", "local"])
@pytest.mark.parametrize("owner", ["profile", "region"])
@pytest.mark.parametrize("narrow_first", [False, True])
def test_complete_binding_scope_not_blocked_by_unknown_narrow_owner(
    family: str, owner: str, narrow_first: bool
) -> None:
    data = fixture()
    if family == "fallback":
        with_fallback(data)
        index = 0
        expected = "model.fallback_coverage"
    else:
        for entry in cast(list[dict[str, object]], data["registrations"]):
            if cast(dict[str, object], entry["ref"])["artifact_id"] == "provider":
                entry["artifact"] = data["local_provider"]
        index = 1
        expected = "model.local_exception"
    review = cast(dict[str, object], data["review"])
    broad = deepcopy(cast(list[dict[str, object]], review["assessments"])[index])
    narrow = deepcopy(broad)
    scope = cast(dict[str, object], narrow["scope"])
    scope["scope_kind"] = owner
    scope[owner + "_refs"] = [
        {
            "artifact_kind": owner,
            "artifact_id": owner,
            "version": "1.0.0",
            "digest": "a" * 64,
        }
    ]
    review["assessments"] = [narrow, broad] if narrow_first else [broad, narrow]
    report = evaluate(data)
    assert (
        next(check for check in report.checks if check.check_id == expected).status
        == "passed"
    )
    assert report.aggregate_status == "deferred"
    review["assessments"] = [narrow]
    report = evaluate(data)
    assert report.aggregate_status == "incomplete"


def test_narrow_scope_unknown_never_grants_or_overrides_known_exclusion() -> None:
    data = fixture()
    with_fallback(data)
    review = cast(dict[str, object], data["review"])
    assessment = deepcopy(cast(list[dict[str, object]], review["assessments"])[0])
    profile = ProfileContentRef.model_validate(
        {
            "artifact_kind": "profile",
            "artifact_id": "profile",
            "version": "1.0.0",
            "digest": "a" * 64,
        }
    )
    region = RegionContentRef.model_validate(
        {
            "artifact_kind": "region",
            "artifact_id": "region",
            "version": "1.0.0",
            "digest": "a" * 64,
        }
    )
    scope = cast(dict[str, object], assessment["scope"])
    scope.update(
        scope_kind="profile_region",
        profile_refs=[profile.model_dump(mode="json")],
        region_refs=[region.model_dump(mode="json")],
    )
    review["assessments"] = [assessment]
    assert evaluate(data, profile_ref=profile).aggregate_status == "incomplete"
    assert (
        evaluate(data, profile_ref=profile, region_ref=region).aggregate_status
        == "deferred"
    )
    wrong = profile.model_copy(update={"digest": "b" * 64})
    assert evaluate(data, profile_ref=wrong).aggregate_status == "invalid"
    wrong_region = region.model_copy(update={"digest": "b" * 64})
    assert (
        evaluate(data, profile_ref=profile, region_ref=wrong_region).aggregate_status
        == "invalid"
    )


@pytest.mark.parametrize("available", [False, True])
def test_single_prompt_wrong_digest_is_not_absent(available: bool) -> None:
    data = fixture()
    correct = PromptRegistration.model_validate(data["prompt"])
    wrong = correct.model_copy(
        update={"ref": correct.ref.model_copy(update={"digest": "b" * 64})}
    )
    report = evaluate(data, prompts=(wrong,) if available else ())
    assert report.aggregate_status == ("invalid" if available else "incomplete")
    expected = "model.prompt_digest_mismatch" if available else "model.prompt_missing"
    assert any(
        issue.code == expected for check in report.checks for issue in check.issues
    )


@pytest.mark.parametrize("family", ["local", "fallback"])
@pytest.mark.parametrize(
    "mutation", ["absent", "sha", "other_registry", "other_version"]
)
def test_review_wrong_sha_is_known_identity_conflict(
    family: str, mutation: str
) -> None:
    data = fixture()
    if family == "fallback":
        with_fallback(data)
    else:
        for entry in cast(list[dict[str, object]], data["registrations"]):
            if cast(dict[str, object], entry["ref"])["artifact_id"] == "provider":
                entry["artifact"] = data["local_provider"]
    report_ref = cast(
        dict[str, object], cast(dict[str, object], data["review"])["report_ref"]
    )
    if mutation == "sha":
        report_ref["sha256"] = "c" * 64
    elif mutation == "other_registry":
        report_ref["review_registry_id"] = "other-registry"
    elif mutation == "other_version":
        report_ref["version"] = "2.0.0"
    report = evaluate(data, reviews=mutation != "absent")
    assert report.aggregate_status == ("invalid" if mutation == "sha" else "incomplete")
    if mutation == "sha":
        assert any(
            issue.code == "review.digest_mismatch"
            for check in report.checks
            for issue in check.issues
        )


@pytest.mark.parametrize("available", [False, True])
def test_nested_model_wrong_digest_is_not_absent(available: bool) -> None:
    from jimu_runtime.domain.validation.policies import ModelBindingRegistration

    data = fixture()
    m1 = cast(
        dict[str, object],
        json.loads(
            (Path(__file__).parents[1] / "fixtures/wp01b/m1/contracts.json").read_text()
        ),
    )
    rawtool = cast(dict[str, object], m1["tool"])
    rawtool["model_binding_ref"] = data["binding_ref"]
    tool = ToolDefinition.model_validate(rawtool)
    rawmodel = cast(dict[str, object], data["model"])
    rawmodel["capability"] = "vision_auxiliary"
    ref = dict(cast(dict[str, object], data["binding_ref"]))
    ref["digest"] = "b" * 64
    model = ModelBindingRegistration.model_validate(
        {"ref": ref, "definition": rawmodel}
    )
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        data["registrations"]
    )
    report = validate_tool_policy_bindings(
        tool, catalogue, (model,) if available else ()
    )
    assert report.aggregate_status == ("invalid" if available else "incomplete")
    expected = (
        "tool.model_binding_digest_mismatch"
        if available
        else "tool.model_binding_missing"
    )
    assert any(
        issue.code == expected for check in report.checks for issue in check.issues
    )


@pytest.mark.parametrize("available", [False, True])
def test_additional_proposal_binding_wrong_digest_is_not_absent(
    available: bool,
) -> None:
    from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
    from jimu_runtime.domain.definitions.models import PolicyContentRef
    from jimu_runtime.domain.policies import QuestionSelectionPolicyArtifact
    from jimu_runtime.domain.validation.policies import (
        ModelBindingRegistration,
        validate_question_selection,
    )

    data = fixture()
    raw = deepcopy(
        cast(dict[str, dict[str, object]], data["artifacts"])["question_selection"]
    )
    policy = cast(dict[str, object], raw["policy"])
    policy.update(
        mode="mandatory_plus_optional",
        optional_source="model_advisory",
        proposal_schema_ref={
            "artifact_kind": "response_schema",
            "artifact_id": "proposal",
            "version": "1.0.0",
            "digest": "a" * 64,
        },
        selection_review_ref=cast(dict[str, object], data["review"])["report_ref"],
        selection_call_policy={
            "mode": "explicit_additional",
            "proposal_binding_ref": data["binding_ref"],
            "additional_resource_requirements_ref": cast(
                dict[str, object], data["model"]
            )["resource_requirements_ref"],
            "max_additional_invocations_per_execution": 1,
        },
    )
    artifact = QuestionSelectionPolicyArtifact.model_validate(raw)
    profile = ProfileDefinition.model_validate(data["selection_profile"])
    rawmodel = cast(dict[str, object], data["model"])
    rawmodel["capability"] = "profile_discovery"
    ref = dict(cast(dict[str, object], data["binding_ref"]))
    ref["digest"] = "b" * 64
    model = ModelBindingRegistration.model_validate(
        {"ref": ref, "definition": rawmodel}
    )
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_python(
        data["registrations"]
    )
    report = validate_question_selection(
        artifact,
        profile,
        catalogue,
        policy_ref=PolicyContentRef.model_validate(
            profile.question_selection_policy_ref.model_dump(mode="json")
        ),
        model_bindings=(model,) if available else (),
    )
    assert report.aggregate_status == ("invalid" if available else "incomplete")
    expected = (
        "selection.proposal_digest_mismatch"
        if available
        else "selection.proposal_binding_missing"
    )
    assert any(
        issue.code == expected for check in report.checks for issue in check.issues
    )


@pytest.mark.parametrize("available", [False, True])
def test_retention_vocabulary_wrong_digest_is_not_absent(available: bool) -> None:
    from jimu_runtime.domain.policies import EvidencePolicyArtifact
    from jimu_runtime.domain.validation.policies import (
        RetentionVocabularyRegistration,
        validate_evidence_retention,
    )

    data = fixture()
    artifact = EvidencePolicyArtifact.model_validate(
        cast(dict[str, dict[str, object]], data["artifacts"])["evidence"]
    )
    ref = artifact.policy.retention_class.vocabulary_ref.model_copy(
        update={"digest": "b" * 64}
    )
    registration = RetentionVocabularyRegistration.model_validate(
        {
            "ref": ref,
            "definition": {
                "schema_version": "1.0.0",
                "vocabulary_id": "retention",
                "version": "1.0.0",
                "semantic_domain": "evidence.retention_class",
                "members": ["synthetic"],
            },
        }
    )
    report = validate_evidence_retention(
        artifact.policy, (registration,) if available else ()
    )
    assert report.aggregate_status == ("invalid" if available else "incomplete")
    expected = (
        "evidence.retention_digest_mismatch"
        if available
        else "evidence.retention_vocabulary_missing"
    )
    assert any(
        issue.code == expected for check in report.checks for issue in check.issues
    )
