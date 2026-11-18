"""Finite Policy compatibility checks over caller-pinned data, never live approvals."""

from typing import NoReturn, Self

from pydantic import TypeAdapter, ValidationError, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    ContextProvenanceSource,
    ModelCapability,
    PolicyKind,
)
from jimu_runtime.domain.definition_support import (
    ReviewReportRef,
    UTCDateTime,
    VocabularyContentRef,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import (
    ModelBinding,
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.definitions.prompts import (
    PromptTemplateContentRef,
    PromptTemplateDefinition,
)
from jimu_runtime.domain.definitions.resources import VocabularyDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_selectors import ToolPlanContentRef
from jimu_runtime.domain.policies import (
    AdditionalSelectionCallPolicy,
    ContextProvenancePolicy,
    ContextProvenancePolicyArtifact,
    CoveragePolicyArtifact,
    DeploymentRequirement,
    EvaluatedFallbackPolicy,
    EvidencePolicy,
    ExecutionPolicyArtifact,
    FallbackPolicyArtifact,
    LocalPrimaryProviderPolicy,
    PolicyArtifact,
    ProviderPolicyArtifact,
    QuestionSelectionPolicyArtifact,
    ResourceRequirementsArtifact,
    ToolExecutionPolicyArtifact,
    ToolFailurePolicyArtifact,
    ToolSkipPolicyArtifact,
)
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.schema_types import FieldSelector, SchemaDescriptor
from jimu_runtime.domain.tool_mappings import (
    ContextTypeRegistration,
    ProfileContentRef,
    RegionContentRef,
)
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.review_inputs import (
    FallbackTransitionAssessment,
    LocalExceptionAssessment,
    ProfileRegionReviewScope,
    ProfileReviewScope,
    RegionReviewScope,
    ReviewedDeployment,
    ReviewMetadata,
    ReviewScope,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    SchemaCheckIssue,
    resolve_field,
)


class PolicyRegistration(ContractModel):
    ref: PolicyContentRef
    artifact: PolicyArtifact

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (
            self.ref.artifact_id != self.artifact.policy_id
            or self.ref.version != self.artifact.version
        ):
            raise ValueError("Policy registration identity mismatch")
        return self


class ModelBindingRegistration(ContractModel):
    ref: ModelBindingContentRef
    definition: ModelBinding

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (
            self.ref.artifact_id != self.definition.model_binding_id
            or self.ref.version != self.definition.version
        ):
            raise ValueError("ModelBinding registration identity mismatch")
        return self


class PromptRegistration(ContractModel):
    ref: PromptTemplateContentRef
    definition: PromptTemplateDefinition

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (
            self.ref.artifact_id != self.definition.prompt_template_id
            or self.ref.version != self.definition.version
        ):
            raise ValueError("Prompt registration identity mismatch")
        return self


def _fail(code: str, message: str, outcome: CheckStatus = "invalid") -> NoReturn:
    if outcome == "passed":
        raise ValueError("failure cannot have passed outcome")
    raise SchemaCheckError(
        SchemaCheckIssue(outcome=outcome, code=code, field_path=(), message=message)
    )


def _check(
    check_id: str, status: CheckStatus, code: str = "", message: str = ""
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=status,
        issues=()
        if status == "passed"
        else (ValidationIssue(code=code, path=(), message=message, related_refs=()),),
    )


def _failure(check_id: str, exc: SchemaCheckError) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        status=exc.issue.outcome,
        issues=(
            ValidationIssue(
                code=exc.issue.code,
                path=exc.issue.field_path,
                message=exc.issue.message,
                related_refs=(),
            ),
        ),
    )


def resolve_policy(
    ref: PolicyContentRef, catalogue: tuple[PolicyRegistration, ...]
) -> PolicyArtifact:
    ref = PolicyContentRef.model_validate(ref)
    if len(catalogue) > 1024:
        _fail(
            "policy.catalogue_limit", "At most1024 Policy registrations are supported"
        )
    checked = tuple(PolicyRegistration.model_validate(item) for item in catalogue)
    keys = [(r.ref.artifact_id, r.ref.version) for r in checked]
    if len(set(keys)) != len(keys):
        _fail("policy.catalogue_conflict", "Duplicate or conflicting Policy identity")
    for entry in checked:
        if (entry.ref.artifact_id, entry.ref.version) == (ref.artifact_id, ref.version):
            if reference_identity(ref) != reference_identity(entry.ref):
                _fail(
                    "policy.digest_mismatch",
                    "Policy pinned digest differs from supplied identity",
                )
            return entry.artifact
    _fail(
        "policy.content_unavailable",
        "Exact Policy content is not supplied",
        "incomplete",
    )


def _resolve_content[T](
    requested: ContentRef,
    entries: tuple[tuple[ContentRef, T], ...],
    *,
    missing_code: str,
    digest_code: str,
    missing_message: str,
) -> T:
    """Resolve a preflighted finite directory; asserted digest conflicts are invalid."""
    identity = reference_identity(requested)
    for ref, value in entries:
        existing = reference_identity(ref)
        if existing[:3] == identity[:3]:
            if existing != identity:
                _fail(
                    digest_code,
                    "Known content identity has a different caller-pinned digest",
                )
            return value
    _fail(missing_code, missing_message, "incomplete")


def _resolve_review(
    requested: ReviewReportRef,
    entries: tuple[ReviewMetadata, ...],
    *,
    missing_code: str,
) -> ReviewMetadata:
    key = (requested.review_registry_id, requested.report_id, requested.version)
    for entry in entries:
        ref = entry.report_ref
        if (ref.review_registry_id, ref.report_id, ref.version) == key:
            if ref.sha256 != requested.sha256:
                _fail(
                    "review.digest_mismatch",
                    "Known review registry/id/version has a different pinned SHA256",
                )
            return entry
    _fail(missing_code, "Exact review resolver result is not supplied", "incomplete")


def _scope_covers(
    scope: ReviewScope,
    binding_ref: ModelBindingContentRef,
    profile_ref: ProfileContentRef | None,
    region_ref: RegionContentRef | None,
) -> bool | None:
    """True=applicable, False=excluded, None=owner unresolved; never grants scope."""
    if reference_identity(binding_ref) not in {
        reference_identity(ref) for ref in scope.binding_refs
    }:
        return False
    unresolved = False
    if isinstance(scope, (ProfileReviewScope, ProfileRegionReviewScope)):
        if profile_ref is None:
            unresolved = True
        elif reference_identity(profile_ref) not in {
            reference_identity(ref) for ref in scope.profile_refs
        }:
            return False
    if isinstance(scope, (RegionReviewScope, ProfileRegionReviewScope)):
        if region_ref is None:
            unresolved = True
        elif reference_identity(region_ref) not in {
            reference_identity(ref) for ref in scope.region_refs
        }:
            return False
    return None if unresolved else True


def _validated_prompts(
    prompts: tuple[PromptRegistration, ...],
) -> tuple[PromptRegistration, ...]:
    if len(prompts) > 1024:
        _fail("model.prompt_limit", "At most1024 Prompt registrations are supported")
    try:
        checked = tuple(PromptRegistration.model_validate(entry) for entry in prompts)
    except ValidationError:
        _fail(
            "model.prompt_registration_invalid",
            "Prompt catalogue contains an invalid registration",
        )
    keys = [
        (entry.ref.artifact_kind, entry.ref.artifact_id, entry.ref.version)
        for entry in checked
    ]
    if len(set(keys)) != len(keys):
        _fail(
            "model.prompt_catalogue_conflict",
            "Duplicate or conflicting Prompt kind/id/version",
        )
    return checked


def _deployment_matches(
    identity: ReviewedDeployment,
    requirement: DeploymentRequirement,
    binding: ModelBinding,
) -> bool:
    return (
        identity.provider_id == requirement.provider_id
        and identity.deployment_id == requirement.deployment_id
        and identity.provider_kind == requirement.provider_kind
        and identity.logical_model_id == binding.logical_model_id
        and identity.deployment_version
        in requirement.actual_version_constraints.allowed_deployment_versions
        and identity.model_version
        in requirement.actual_version_constraints.allowed_model_versions
    )


def validate_model_policies(
    binding: ModelBinding,
    catalogue: tuple[PolicyRegistration, ...],
    reviews: tuple[ReviewMetadata, ...],
    validation_at: UTCDateTime,
    *,
    binding_ref: ModelBindingContentRef,
    profile_ref: ProfileContentRef | None = None,
    region_ref: RegionContentRef | None = None,
    prompts: tuple[PromptRegistration, ...] = (),
    consuming_purpose: ModelCapability | None = None,
) -> ValidationReport:
    """Check supplied resolver associations. This does NOT authenticate report bytes."""
    binding = ModelBinding.model_validate(binding)
    binding_ref = ModelBindingContentRef.model_validate(binding_ref)
    at = TypeAdapter[UTCDateTime](UTCDateTime).validate_python(validation_at)
    checks: list[CheckResult] = []
    prompt_entries: tuple[PromptRegistration, ...] | None = None
    try:
        prompt_entries = _validated_prompts(prompts)
    except SchemaCheckError as exc:
        checks.append(_failure("model.prompt_catalogue", exc))
    try:
        if (
            binding_ref.artifact_id != binding.model_binding_id
            or binding_ref.version != binding.version
        ):
            _fail("model.binding_identity", "Binding ref differs from definition")
        provider_artifact = resolve_policy(binding.provider_policy_ref, catalogue)
        fallback_artifact = resolve_policy(binding.fallback_policy_ref, catalogue)
        resource_artifact = resolve_policy(binding.resource_requirements_ref, catalogue)
        if (
            not isinstance(provider_artifact, ProviderPolicyArtifact)
            or not isinstance(fallback_artifact, FallbackPolicyArtifact)
            or not isinstance(resource_artifact, ResourceRequirementsArtifact)
        ):
            _fail(
                "model.policy_subtype",
                "A ModelBinding Policy reference has the wrong subtype",
            )
        provider = provider_artifact.policy
        fallback = fallback_artifact.policy
        if any(
            deployment.logical_model_id != binding.logical_model_id
            for deployment in provider.deployments
        ):
            _fail(
                "model.logical_identity",
                "Deployment logical model ID differs from Binding",
            )
        checked_reviews = tuple(
            ReviewMetadata.model_validate(review) for review in reviews
        )
        if len(checked_reviews) > 1024:
            _fail(
                "review.input_limit",
                "At most1024 review resolver results are supported",
            )
        report_keys = [
            (
                r.report_ref.review_registry_id,
                r.report_ref.report_id,
                r.report_ref.version,
            )
            for r in checked_reviews
        ]
        if len(set(report_keys)) != len(report_keys):
            _fail(
                "review.identity_conflict",
                "Duplicate or conflicting report resolver result",
            )
        checks.append(_check("model.policy_refs", "passed"))
        if isinstance(provider, LocalPrimaryProviderPolicy):
            exception = provider.local_primary_exception
            if (
                binding.capability not in exception.applicable_capabilities
                or not exception.valid_from <= at < exception.valid_until
            ):
                _fail(
                    "model.local_exception_scope",
                    "Local exception is expired or capability does not apply",
                )
            report = _resolve_review(
                exception.approval_ref,
                checked_reviews,
                missing_code="model.local_review_missing",
            )
            if (
                report.verdict != "approved"
                or not report.valid_from <= at < report.valid_until
            ):
                _fail(
                    "model.local_review_invalid",
                    "Local exception review is rejected or out of window",
                )
            primary = next(
                d
                for d in provider.deployments
                if d.deployment_id == provider.primary_deployment_id
            )
            local_covered_versions: set[tuple[str, str]] = set()
            local_unknown_versions: set[tuple[str, str]] = set()
            for assessment in report.assessments:
                if (
                    isinstance(assessment, LocalExceptionAssessment)
                    and assessment.exception_id == exception.exception_id
                    and assessment.exception_version == exception.version
                    and assessment.reason == exception.reason
                    and binding.capability in assessment.applicable_capabilities
                    and _deployment_matches(assessment.primary, primary, binding)
                ):
                    scope = _scope_covers(
                        assessment.scope, binding_ref, profile_ref, region_ref
                    )
                    versions = (
                        assessment.primary.deployment_version,
                        assessment.primary.model_version,
                    )
                    if scope is True:
                        local_covered_versions.add(versions)
                    elif scope is None:
                        local_unknown_versions.add(versions)
            expected_versions = len(
                primary.actual_version_constraints.allowed_deployment_versions
            ) * len(primary.actual_version_constraints.allowed_model_versions)
            if len(local_covered_versions) != expected_versions:
                if (
                    len(local_covered_versions | local_unknown_versions)
                    == expected_versions
                ):
                    _fail(
                        "model.local_review_owner_missing",
                        "Missing owner identities are needed to establish remaining local review coverage",
                        "incomplete",
                    )
                _fail(
                    "model.local_review_coverage",
                    "Local review does not cover exact exception/deployment/capability/scope",
                )
            checks.append(_check("model.local_exception", "passed"))
        if isinstance(fallback, EvaluatedFallbackPolicy):
            reports = []
            for ref in fallback.evaluation_refs:
                report = _resolve_review(
                    ref, checked_reviews, missing_code="model.fallback_review_missing"
                )
                if (
                    report.verdict != "approved"
                    or not report.valid_from <= at < report.valid_until
                ):
                    _fail(
                        "model.fallback_review_invalid",
                        "Fallback report is rejected or out of window",
                    )
                reports.append(report)
            deployments = {d.deployment_id: d for d in provider.deployments}
            for transition in fallback.allowed_transitions:
                if (
                    transition.from_deployment_id not in deployments
                    or transition.to_deployment_id not in deployments
                ):
                    _fail(
                        "model.fallback_endpoint", "Fallback endpoint is not declared"
                    )
                source = deployments[transition.from_deployment_id]
                target = deployments[transition.to_deployment_id]
                required_count = (
                    len(source.actual_version_constraints.allowed_deployment_versions)
                    * len(source.actual_version_constraints.allowed_model_versions)
                    * len(target.actual_version_constraints.allowed_deployment_versions)
                    * len(target.actual_version_constraints.allowed_model_versions)
                )
                for trigger in fallback.trigger_errors:
                    covered_versions: set[tuple[str, str, str, str]] = set()
                    unknown_versions: set[tuple[str, str, str, str]] = set()
                    for report in reports:
                        for assessment in report.assessments:
                            if not isinstance(assessment, FallbackTransitionAssessment):
                                continue
                            if (
                                trigger not in assessment.trigger_errors
                                or assessment.capability != binding.capability
                            ):
                                continue
                            if reference_identity(
                                assessment.input_schema_ref
                            ) != reference_identity(
                                binding.input_schema_ref
                            ) or reference_identity(
                                assessment.output_schema_ref
                            ) != reference_identity(binding.output_schema_ref):
                                continue
                            if _deployment_matches(
                                assessment.source, source, binding
                            ) and _deployment_matches(
                                assessment.target, target, binding
                            ):
                                scope = _scope_covers(
                                    assessment.scope,
                                    binding_ref,
                                    profile_ref,
                                    region_ref,
                                )
                                combination = (
                                    assessment.source.deployment_version,
                                    assessment.source.model_version,
                                    assessment.target.deployment_version,
                                    assessment.target.model_version,
                                )
                                if scope is True:
                                    covered_versions.add(combination)
                                elif scope is None:
                                    unknown_versions.add(combination)
                    if len(covered_versions) != required_count:
                        if len(covered_versions | unknown_versions) == required_count:
                            _fail(
                                "model.fallback_owner_missing",
                                "Missing owner identities are needed to establish remaining fallback coverage",
                                "incomplete",
                            )
                        _fail(
                            "model.fallback_coverage",
                            "Each permitted transition/trigger/exact version combination requires compatible evaluated coverage",
                        )
            checks.append(_check("model.fallback_coverage", "passed"))
        else:
            checks.append(_check("model.fallback_disabled", "passed"))
    except SchemaCheckError as exc:
        checks.append(_failure("model.configuration", exc))
    if binding.prompt_template_ref is not None and prompt_entries is not None:
        try:
            entries = prompt_entries
            prompt = _resolve_content(
                binding.prompt_template_ref,
                tuple((entry.ref, entry.definition) for entry in entries),
                missing_code="model.prompt_missing",
                digest_code="model.prompt_digest_mismatch",
                missing_message="Exact Prompt definition is not supplied",
            )
            purpose = (
                binding.capability
                if binding.capability is not ModelCapability.VISION_AUXILIARY
                else consuming_purpose
            )
            if purpose is None:
                _fail(
                    "model.prompt_consuming_purpose",
                    "Auxiliary prompt requires explicit consuming purpose",
                    "incomplete",
                )
            if prompt.purpose is not purpose:
                _fail(
                    "model.prompt_purpose",
                    "Prompt purpose differs from consuming capability",
                )
            if reference_identity(prompt.response_schema_ref) != reference_identity(
                binding.output_schema_ref
            ):
                _fail(
                    "model.prompt_response_schema",
                    "Prompt response Schema differs from Binding output Schema",
                )
            checks.append(_check("model.prompt_compatibility", "passed"))
        except SchemaCheckError as exc:
            checks.append(_failure("model.prompt_compatibility", exc))
    checks.append(
        _check(
            "model.external_trust",
            "deferred",
            "model.external_trust",
            "Report bytes/authenticity, independent quality, host features and real routing are not executed",
        )
    )
    return ValidationReport(scope="definition_relations", checks=tuple(checks))


def validate_tool_policy_bindings(
    tool: ToolDefinition,
    catalogue: tuple[PolicyRegistration, ...],
    model_bindings: tuple[ModelBindingRegistration, ...] = (),
) -> ValidationReport:
    checks: list[CheckResult] = []
    try:
        tool = ToolDefinition.model_validate(tool)
        resources = resolve_policy(tool.resource_requirements_ref, catalogue)
        execution = resolve_policy(tool.execution_policy_ref, catalogue)
        evidence = resolve_policy(tool.evidence_policy_ref, catalogue)
        if (
            not isinstance(resources, ResourceRequirementsArtifact)
            or not isinstance(execution, ToolExecutionPolicyArtifact)
            or evidence.policy.policy_kind is not PolicyKind.EVIDENCE
        ):
            _fail("tool.policy_subtype", "Tool Policy reference has wrong subtype")
        if (
            execution.policy.timeout_ms > resources.policy.timeout_ms
            or execution.policy.max_concurrency > resources.policy.max_concurrency
            or execution.policy.input_byte_limit > resources.policy.input_byte_limit
        ):
            _fail(
                "tool.policy_budget_widening",
                "ToolExecutionPolicy may only tighten ResourceRequirements",
            )
        checks.append(_check("tool.policy_bindings", "passed"))
        if tool.model_binding_ref is not None:
            if len(model_bindings) > 1024:
                _fail(
                    "tool.model_catalogue_limit",
                    "At most1024 ModelBindings are supported",
                )
            models = tuple(
                ModelBindingRegistration.model_validate(item) for item in model_bindings
            )
            keys = [(item.ref.artifact_id, item.ref.version) for item in models]
            if len(set(keys)) != len(keys):
                _fail(
                    "tool.model_catalogue_conflict",
                    "Duplicate/conflicting ModelBinding identity",
                )
            nested = _resolve_content(
                tool.model_binding_ref,
                tuple((entry.ref, entry.definition) for entry in models),
                missing_code="tool.model_binding_missing",
                digest_code="tool.model_binding_digest_mismatch",
                missing_message="Declared nested ModelBinding is not supplied",
            )
            if nested.capability is not ModelCapability.VISION_AUXILIARY:
                _fail(
                    "tool.model_binding_capability",
                    "Nested Tool ModelBinding must be vision_auxiliary",
                )
            checks.append(_check("tool.model_binding", "passed"))
    except SchemaCheckError as exc:
        checks.append(_failure("tool.configuration", exc))
    return ValidationReport(scope="definition_relations", checks=tuple(checks))


def validate_step_failure_policies(
    plan: ToolPlanDefinition,
    catalogue: tuple[PolicyRegistration, ...],
    *,
    plan_ref: ToolPlanContentRef,
) -> ValidationReport:
    plan = ToolPlanDefinition.model_validate(plan)
    plan_ref = ToolPlanContentRef.model_validate(plan_ref)
    checks: list[CheckResult] = []
    identity_valid = False
    try:
        if (
            plan_ref.artifact_id != plan.tool_plan_id
            or plan_ref.version != plan.version
        ):
            _fail("plan.identity", "Plan identity differs from definition")
        identity_valid = True
        for step in plan.steps:
            artifact = resolve_policy(step.failure_policy_ref, catalogue)
            if (
                not isinstance(artifact, ToolFailurePolicyArtifact)
                or artifact.policy.requirement != step.requirement
            ):
                _fail(
                    "plan.failure_requirement",
                    "Step and FailurePolicy requirement differ",
                )
        checks.append(_check("plan.failure_policies", "passed"))
    except SchemaCheckError as exc:
        checks.append(_failure("plan.failure_policies", exc))
    if identity_valid:
        try:
            for step in plan.steps:
                skip = resolve_policy(step.skip_policy_ref, catalogue)
                if not isinstance(skip, ToolSkipPolicyArtifact):
                    _fail(
                        "plan.skip_policy_subtype",
                        "ToolPlan step skip reference must resolve ToolSkipPolicy",
                    )
            checks.append(_check("plan.skip_policy_subtypes", "passed"))
        except SchemaCheckError as exc:
            checks.append(_failure("plan.skip_policy_subtypes", exc))
    checks.append(
        _check(
            "plan.skip_conditions",
            "deferred",
            "plan.skip_conditions",
            "Condition use-site references, operand types and activation truth require separate checks",
        )
    )
    return ValidationReport(scope="definition_relations", checks=tuple(checks))


def validate_context_derivations(
    policy: ContextProvenancePolicy,
    descriptors: tuple[SchemaDescriptor, ...],
    external_roots: tuple[ContextTypeRegistration, ...] = (),
) -> ValidationReport:
    policy = ContextProvenancePolicy.model_validate(policy)
    checks: list[CheckResult] = []
    try:
        if len(external_roots) > 1024:
            _fail(
                "context.input_limit", "At most1024 external field views are supported"
            )
        roots = tuple(
            TypeAdapter[ContextTypeRegistration](
                ContextTypeRegistration
            ).validate_python(root)
            for root in external_roots
        )
        outputs = {
            (reference_identity(field.schema_ref), field.field_path): field
            for field in policy.fields
        }
        edges: dict[
            tuple[tuple[ArtifactKind, str, str, str], tuple[str, ...]],
            set[tuple[tuple[ArtifactKind, str, str, str], tuple[str, ...]]],
        ] = {key: set() for key in outputs}
        for key, field in outputs.items():
            resolve_field(
                FieldSelector(schema_ref=field.schema_ref, field_path=field.field_path),
                descriptors,
            )
            if field.input_selectors is None:
                continue
            for selector in field.input_selectors:
                resolve_field(
                    FieldSelector(
                        schema_ref=selector.schema_ref, field_path=selector.field_path
                    ),
                    descriptors,
                )
                source_key = (
                    reference_identity(selector.schema_ref),
                    selector.field_path,
                )
                if selector.source.value == "context_snapshot":
                    if source_key not in outputs:
                        _fail(
                            "context.source_missing",
                            "Snapshot input is not a declared trusted field",
                            "incomplete",
                        )
                    if (
                        ContextProvenanceSource.DECLARED_DERIVATION
                        in outputs[source_key].allowed_sources
                    ):
                        edges[key].add(source_key)
                else:
                    matching = [
                        root
                        for root in roots
                        if root.source == selector.source.value
                        and reference_identity(root.schema_ref)
                        == reference_identity(selector.schema_ref)
                    ]
                    if not matching:
                        _fail(
                            "context.external_root_missing",
                            "Region/Profile input requires explicit trusted owner view",
                            "incomplete",
                        )
                    if len(matching) != 1:
                        _fail(
                            "context.external_root_ambiguous",
                            "Source owner view is ambiguous",
                        )
        resolved: set[tuple[tuple[ArtifactKind, str, str, str], tuple[str, ...]]] = (
            set()
        )
        while True:
            ready = {
                key
                for key, deps in edges.items()
                if key not in resolved and deps.issubset(resolved)
            }
            if not ready:
                break
            resolved.update(ready)
        if len(resolved) != len(edges):
            _fail(
                "context.derivation_cycle", "Context derivation graph contains a cycle"
            )
        checks.append(_check("context.field_graph", "passed"))
    except SchemaCheckError as exc:
        checks.append(_failure("context.field_graph", exc))
    checks.append(
        _check(
            "context.external_trust",
            "deferred",
            "context.external_trust",
            "Source authenticity and registered derivation execution are not performed",
        )
    )
    return ValidationReport(scope="definition_relations", checks=tuple(checks))


def validate_question_selection(
    artifact: "QuestionSelectionPolicyArtifact",
    profile: "ProfileDefinition",
    catalogue: tuple[PolicyRegistration, ...],
    *,
    policy_ref: PolicyContentRef,
    model_bindings: tuple[ModelBindingRegistration, ...] = (),
) -> ValidationReport:
    """Check declared partition and optional-call refs; not mandatory Rule closure."""
    artifact = QuestionSelectionPolicyArtifact.model_validate(artifact)
    profile = ProfileDefinition.model_validate(profile)
    policy_ref = PolicyContentRef.model_validate(policy_ref)
    checks: list[CheckResult] = []
    try:
        if (
            policy_ref.artifact_id != artifact.policy_id
            or policy_ref.version != artifact.version
            or reference_identity(profile.question_selection_policy_ref)
            != reference_identity(policy_ref)
        ):
            _fail(
                "selection.policy_identity",
                "Selection Policy identity differs from Profile pin",
            )
        policy = artifact.policy
        selected = {
            reference_identity(ref)
            for ref in (*policy.mandatory_question_refs, *policy.optional_question_refs)
        }
        declared = {reference_identity(ref) for ref in profile.question_refs}
        if selected != declared:
            _fail(
                "selection.profile_partition",
                "Mandatory/optional union differs from Profile Question catalogue",
            )
        if isinstance(policy.selection_call_policy, AdditionalSelectionCallPolicy):
            resource = resolve_policy(
                policy.selection_call_policy.additional_resource_requirements_ref,
                catalogue,
            )
            if not isinstance(resource, ResourceRequirementsArtifact):
                _fail(
                    "selection.additional_resources",
                    "Additional call requires ResourceRequirements",
                )
            if len(model_bindings) > 1024:
                _fail(
                    "selection.model_limit",
                    "At most1024 additional-call model bindings are supported",
                )
            bindings = tuple(
                ModelBindingRegistration.model_validate(item) for item in model_bindings
            )
            keys = [(item.ref.artifact_id, item.ref.version) for item in bindings]
            if len(keys) != len(set(keys)):
                _fail(
                    "selection.binding_conflict",
                    "Duplicate/conflicting proposal ModelBinding identity",
                )
            binding = _resolve_content(
                policy.selection_call_policy.proposal_binding_ref,
                tuple((entry.ref, entry.definition) for entry in bindings),
                missing_code="selection.proposal_binding_missing",
                digest_code="selection.proposal_digest_mismatch",
                missing_message="Exact proposal ModelBinding is missing",
            )
            if binding.capability is not ModelCapability.PROFILE_DISCOVERY:
                _fail(
                    "selection.proposal_capability",
                    "Additional proposal binding must be profile_discovery compatible",
                )
        checks.append(_check("selection.declared_partition", "passed"))
    except SchemaCheckError as exc:
        checks.append(_failure("selection.declared_partition", exc))
    checks.append(
        _check(
            "selection.required_producer_closure",
            "deferred",
            "selection.required_producer_closure",
            "Mandatory Rule/Tool producer closure and Applicability expressions are not established by Policy refs",
        )
    )
    if artifact.policy.optional_source == "model_advisory":
        checks.append(
            _check(
                "selection.independent_review",
                "deferred",
                "selection.independent_review",
                "Selection quality review and actual invocation evidence are not proved by this static check",
            )
        )
    return ValidationReport(scope="definition_relations", checks=tuple(checks))


def validate_execution_policy(
    artifact: "ExecutionPolicyArtifact", catalogue: tuple[PolicyRegistration, ...]
) -> ValidationReport:
    artifact = ExecutionPolicyArtifact.model_validate(artifact)
    checks: list[CheckResult] = []
    try:
        policy = artifact.policy
        coverage = resolve_policy(policy.coverage_policy_ref, catalogue)
        provenance = resolve_policy(policy.context_provenance_policy_ref, catalogue)
        resources = resolve_policy(policy.resource_requirements_ref, catalogue)
        if (
            not isinstance(coverage, CoveragePolicyArtifact)
            or not isinstance(provenance, ContextProvenancePolicyArtifact)
            or not isinstance(resources, ResourceRequirementsArtifact)
        ):
            _fail(
                "execution.policy_subtypes",
                "Execution Policy refers to wrong Policy subtype",
            )
        checks.append(_check("execution.policy_refs", "passed"))
    except SchemaCheckError as exc:
        checks.append(_failure("execution.policy_refs", exc))
    checks.append(
        _check(
            "execution.workflow_and_schema_closure",
            "deferred",
            "execution.workflow_and_schema_closure",
            "No Workflow execution or full artifact Schema/Bundle closure is implemented",
        )
    )
    return ValidationReport(scope="definition_relations", checks=tuple(checks))


class RetentionVocabularyRegistration(ContractModel):
    ref: "VocabularyContentRef"
    definition: "VocabularyDefinition"

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (
            self.ref.artifact_id != self.definition.vocabulary_id
            or self.ref.version != self.definition.version
        ):
            raise ValueError("Retention Vocabulary identity mismatch")
        return self


def validate_evidence_retention(
    policy: "EvidencePolicy", vocabularies: tuple[RetentionVocabularyRegistration, ...]
) -> ValidationReport:
    policy = EvidencePolicy.model_validate(policy)
    try:
        if len(vocabularies) > 1024:
            _fail("evidence.vocabulary_limit", "At most1024 Vocabularies are supported")
        checked = tuple(
            RetentionVocabularyRegistration.model_validate(item)
            for item in vocabularies
        )
        keys = [(item.ref.artifact_id, item.ref.version) for item in checked]
        if len(set(keys)) != len(keys):
            _fail(
                "evidence.vocabulary_conflict",
                "Duplicate or conflicting Vocabulary identity",
            )
        definition = _resolve_content(
            policy.retention_class.vocabulary_ref,
            tuple((entry.ref, entry.definition) for entry in checked),
            missing_code="evidence.retention_vocabulary_missing",
            digest_code="evidence.retention_digest_mismatch",
            missing_message="Exact retention class Vocabulary is not supplied",
        )
        if (
            definition.semantic_domain != "evidence.retention_class"
            or policy.retention_class.member not in definition.members
        ):
            _fail(
                "evidence.retention_member",
                "Retention member/domain does not match the declared Vocabulary",
            )
        result = _check("evidence.retention_vocabulary", "passed")
    except SchemaCheckError as exc:
        result = _failure("evidence.retention_vocabulary", exc)
    return ValidationReport(scope="definition_relations", checks=(result,))
