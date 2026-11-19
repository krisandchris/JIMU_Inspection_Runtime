"""Shared offline semantic checks for compilers and independent admission."""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial

from pydantic import TypeAdapter

from jimu_runtime.bundle.artifacts import DRAFT_2020_12, BundleArtifactError
from jimu_runtime.bundle.capabilities import (
    CapabilityIdentity,
    CapabilitySnapshot,
    CapabilityValidation,
    RuntimeProtocolRole,
    assess_capabilities,
    validate_model_deployments,
)
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.bundle.closure import BundleClosure
from jimu_runtime.bundle.producer_rules import (
    SystemProducerGrant,
    validate_producer_rules,
)
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.bundle.semantic_inputs import (
    build_definition_inputs,
    build_fact_schema_registry,
    build_tool_mapping_uses,
    profile_model_uses,
    require_definition,
)
from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import CapabilityFeature, ModelCapability
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    UTCDateTime,
    VocabularyMemberRef,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
    ToolGroundingBinding,
)
from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.definitions.prompts import PromptTemplateDefinition
from jimu_runtime.domain.definitions.resources import VocabularyDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.policies import (
    ExecutionPolicyArtifact,
    ProviderPolicyArtifact,
    QuestionSelectionPolicyArtifact,
    ToolFailurePolicyArtifact,
)
from jimu_runtime.domain.primitives import Digest
from jimu_runtime.domain.references import ContentRef, SchemaRef
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest
from jimu_runtime.domain.schema_types import TypedValueSchemaDescriptor
from jimu_runtime.domain.validation.definition_inputs import (
    DefinitionValidationInput,
    ProfileUseContext,
    RegisteredPrompt,
)
from jimu_runtime.domain.validation.definitions import validate_definition_relations
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    IssuePath,
    ValidationIssue,
)
from jimu_runtime.domain.validation.policies import PromptRegistration
from jimu_runtime.domain.validation.producers import requirements_agree
from jimu_runtime.domain.validation.schema_descriptors import resolve_descriptor

_UTC = TypeAdapter[UTCDateTime](UTCDateTime)

_REQUIRED_FEATURES: dict[ModelCapability, frozenset[CapabilityFeature]] = {
    ModelCapability.PROFILE_DISCOVERY: frozenset(
        (
            CapabilityFeature.RGB_IMAGE,
            CapabilityFeature.TYPED_CONTEXT,
            CapabilityFeature.STRUCTURED_OUTPUT,
        )
    ),
    ModelCapability.OBJECT_GROUNDING: frozenset(
        (
            CapabilityFeature.RGB_IMAGE,
            CapabilityFeature.STRUCTURED_OUTPUT,
            CapabilityFeature.ORIGINAL_XYXY,
        )
    ),
    ModelCapability.OBJECT_OBSERVATION: frozenset(
        (CapabilityFeature.RGB_IMAGE, CapabilityFeature.STRUCTURED_OUTPUT)
    ),
    ModelCapability.CANDIDATE_PREDICTION: frozenset(
        (CapabilityFeature.STRUCTURED_OUTPUT,)
    ),
    ModelCapability.VISION_AUXILIARY: frozenset(),
}

# Each finite deferral is addressed by the separate check named here. The
# report retains runtime deferrals individually and rejects new unknown ones.
_HANDOFFS = {
    "definitions.deferred.condition_operand_typechecking": "semantics.condition_operand_typechecking",
    "definitions.deferred.applicability_identity_eligibility": "semantics.applicability_identity_eligibility",
    "definitions.deferred.rule_set_body": "semantics.rule_set_body",
    "definitions.deferred.producer_authority_admission": "semantics.producer_authority_admission",
    "definitions.deferred.mandatory_producer_completeness": "semantics.mandatory_producer_completeness",
    "definitions.deferred.bundle_closure": "semantics.bundle_closure",
    "definitions.deferred.semantic_protocol_io": "semantics.protocol_io",
    "definitions.deferred.geometry_body_and_asset_bytes": "semantics.geometry_body_and_asset_bytes",
    "model.external_trust": "semantics.host_model_deployments",
    "tool.external_admission": "semantics.host_capabilities",
    "tool_plan.policy_contents": "semantics.tool_plan_policies",
    "tool_plan.producer_authority": "semantics.producer_authority_admission",
    "selection.required_producer_closure": "semantics.mandatory_producer_completeness",
    "selection.independent_review": "semantics.selection_review",
    "execution.workflow_and_schema_closure": "semantics.workflow_schema_closure",
    "condition.deferred.operand_types_and_truth": "semantics.condition_operand_typechecking",
    "plan.skip_conditions": "semantics.condition_operand_typechecking",
    "context.external_trust": "semantics.context_source_trust",
}

_RUNTIME_BOUNDARIES = {
    "definitions.deferred.condition_evaluation": "Condition truth and actual eligibility require a Task Context Snapshot and its authoritative Facts.",
    "definitions.deferred.external_authenticity": "Bundle signature and source authority belong to the independent trust stage.",
    "semantics.context_source_trust": "Actual metadata provenance and derivation execution require a Task. Declared capabilities were checked against the host snapshot.",
    "semantics.selection_review": "No trusted selection-specific review is available under V1. Use each affected Policy's explicit fallback.",
}


@dataclass(frozen=True, slots=True)
class BundleSemanticReport:
    bundle_digest: Digest
    capability_snapshot_pin: TrustPin
    validation_at: UTCDateTime
    checks: tuple[CheckResult, ...]
    capabilities: CapabilityValidation

    @property
    def accepted(self) -> bool:
        return all(check.status in ("passed", "deferred") for check in self.checks)


def _issue(
    code: str,
    message: str,
    artifact: ResolvedArtifact | None = None,
    path: IssuePath = (),
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        message=message,
        path=path,
        related_refs=() if artifact is None else (artifact.ref,),
    )


def _check(
    check_id: str,
    issues: Iterable[ValidationIssue] = (),
    *,
    failure_status: CheckStatus = "invalid",
) -> CheckResult:
    bounded = tuple(issues)
    if len(bounded) > 64:
        bounded = (
            *bounded[:63],
            _issue(
                "semantics.diagnostic_limit",
                "Additional failures exceeded the diagnostic limit.",
            ),
        )
    return CheckResult(
        check_id=check_id,
        status=failure_status if bounded else "passed",
        issues=bounded,
    )


def _resources(catalogue: ArtifactCatalogue) -> None:
    for item in catalogue.of_kind(ArtifactKind.GEOMETRY_RESOURCE):
        geometry = require_definition(catalogue, item.ref, GeometryResourceDefinition)
        for field, member, domain in (
            ("coordinate_system", geometry.coordinate_system, "coordinate_system"),
            ("units", geometry.units, "unit"),
        ):
            _vocabulary_domain(catalogue, item, field, member, domain)
        if catalogue.resolve(geometry.provenance.source_asset_ref).asset_bytes is None:
            raise BundleArtifactError(
                "semantic_geometry_asset_missing",
                "Geometry provenance requires verified source asset bytes.",
                item.inventory.path,
                ("provenance", "source_asset_ref"),
            )


def _vocabulary_domain(
    catalogue: ArtifactCatalogue,
    item: ResolvedArtifact,
    field: str,
    member: VocabularyMemberRef,
    domain: str,
) -> None:
    vocabulary = require_definition(
        catalogue, member.vocabulary_ref, VocabularyDefinition
    )
    if vocabulary.semantic_domain != domain or member.member not in vocabulary.members:
        raise BundleArtifactError(
            "semantic_vocabulary_domain",
            "The geometry field does not resolve to the required vocabulary domain.",
            item.inventory.path,
            (field,),
        )


def _validate_capability_manifest(
    catalogue: ArtifactCatalogue,
    closure: BundleClosure,
    manifest: BundleManifest,
) -> tuple[ExternalCapabilityRequirement, ...]:
    # Include exact declared capabilities for every configuration use. A Tool
    # implementation may be optional only at sites with explicit continuation.
    uses: dict[tuple[ArtifactKind, str, str, str], bool] = {}
    for item in catalogue.of_kind(ArtifactKind.PROFILE):
        profile = require_definition(catalogue, item.ref, ProfileDefinition)
        if isinstance(profile.grounding_binding, ToolGroundingBinding):
            uses[reference_identity(profile.grounding_binding.tool_ref)] = True
        plan = require_definition(catalogue, profile.tool_plan_ref, ToolPlanDefinition)
        for step in plan.steps:
            failure = require_definition(
                catalogue, step.failure_policy_ref, ToolFailurePolicyArtifact
            )
            tool_key = reference_identity(step.tool_ref)
            uses[tool_key] = (
                uses.get(tool_key, False)
                or step.requirement == "required"
                or failure.policy.optional_continuation != "allow_if_consumer_policy"
            )
    primary = {
        (item.family, item.capability_id, item.contract_version): item
        for item in manifest.required_capabilities
    }
    optional: dict[CapabilityIdentity, ExternalCapabilityRequirement] = {}
    for ref in closure.external_references:
        requirement = ref.requirement
        if not isinstance(requirement, ExternalCapabilityRequirement):
            continue
        key = (
            requirement.family,
            requirement.capability_id,
            requirement.contract_version,
        )
        registered = primary.get(key)
        # Model-shaped specializations may differ in Python class while
        # representing the exact same closed requirement wire.
        if registered is not None and requirements_agree(registered, requirement):
            continue
        required = ref.origin.ref.artifact_kind is not ArtifactKind.TOOL or uses.get(
            reference_identity(ref.origin.ref), True
        )
        prior = optional.get(key)
        if (
            required
            or registered is not None
            or (prior is not None and not requirements_agree(prior, requirement))
        ):
            raise BundleArtifactError(
                "semantic_manifest_capability_mismatch",
                "The inferred capability obligation has no compatible manifest declaration.",
                ref.origin.inventory.path,
                ref.location,
            )
        optional[key] = ExternalCapabilityRequirement.model_validate(
            requirement.model_dump()
        )
    return tuple(optional.values())


def _role_requirements(
    artifact: ResolvedArtifact,
    catalogue: ArtifactCatalogue,
    binding: ModelBinding,
    role: ModelCapability,
) -> tuple[ValidationIssue, ...]:
    expected = _REQUIRED_FEATURES[role]
    policy = require_definition(
        catalogue, binding.provider_policy_ref, ProviderPolicyArtifact
    )
    return tuple(
        _issue(
            "semantic_model_role_features",
            "A deployment's explicit features do not cover the consuming protocol role.",
            artifact,
            ("deployments", index, "required_features"),
        )
        for index, item in enumerate(policy.policy.deployments)
        if not expected.issubset(item.required_features)
    )


def _protocol_checks(
    catalogue: ArtifactCatalogue,
    inputs: DefinitionValidationInput,
    fact_schemas: FactSchemaRegistry,
    snapshot: CapabilitySnapshot,
    at: UTCDateTime,
) -> tuple[ValidationIssue, ...]:
    trusted = {
        (
            item.role,
            reference_identity(item.input_schema_ref),
            reference_identity(item.output_schema_ref),
        )
        for item in snapshot.protocols_at(at)
    }
    examined: set[tuple[tuple[ArtifactKind, str, str, str], RuntimeProtocolRole]] = (
        set()
    )
    problems: list[ValidationIssue] = []

    def require(
        ref: ContentRef,
        role: RuntimeProtocolRole,
        input_ref: SchemaRef,
        output_ref: SchemaRef,
    ) -> bool:
        key = reference_identity(ref), role
        if key in examined:
            return False
        examined.add(key)
        if (
            role,
            reference_identity(input_ref),
            reference_identity(output_ref),
        ) not in trusted:
            problems.append(
                _issue(
                    "semantic_runtime_protocol_mismatch",
                    f"The active Runtime protocol roster lacks this exact {role} input/output pair.",
                    catalogue.resolve(ref),
                    ("input_schema_ref",),
                )
            )
        return True

    for item in catalogue.of_kind(ArtifactKind.PROFILE):
        profile = require_definition(catalogue, item.ref, ProfileDefinition)
        for use in profile_model_uses(catalogue, profile):
            binding = require_definition(catalogue, use.binding_ref, ModelBinding)
            role = use.purpose or ModelCapability.VISION_AUXILIARY
            model_artifact = catalogue.resolve(use.binding_ref)
            expected = ModelCapability.VISION_AUXILIARY if use.auxiliary else role
            if binding.capability is not expected:
                problems.append(
                    _issue(
                        "semantic_model_role",
                        "The configured ModelBinding capability differs from its consuming role.",
                        model_artifact,
                        ("capability",),
                    )
                )
            if require(
                use.binding_ref,
                role,
                binding.input_schema_ref,
                binding.output_schema_ref,
            ):
                problems.extend(
                    _role_requirements(model_artifact, catalogue, binding, role)
                )
        if isinstance(profile.grounding_binding, ToolGroundingBinding):
            tool_ref = profile.grounding_binding.tool_ref
            tool = require_definition(catalogue, tool_ref, ToolDefinition)
            if require(
                tool_ref,
                "tool_grounding",
                tool.input_schema_ref,
                tool.output_schema_ref,
            ) and not {
                CapabilityFeature.RGB_IMAGE,
                CapabilityFeature.ORIGINAL_XYXY,
            }.issubset(tool.implementation_requirement.required_features):
                problems.append(
                    _issue(
                        "semantic_tool_grounding_features",
                        "Tool grounding requires explicit RGB and original-coordinate XYXY features.",
                        catalogue.resolve(tool_ref),
                        ("implementation_requirement", "required_features"),
                    )
                )
        plan = require_definition(catalogue, profile.tool_plan_ref, ToolPlanDefinition)
        for step in plan.steps:
            tool = require_definition(catalogue, step.tool_ref, ToolDefinition)
            require(
                step.tool_ref,
                "tool_execution",
                tool.input_schema_ref,
                tool.output_schema_ref,
            )

    for artifact in catalogue.of_kind(ArtifactKind.QUESTION):
        question = require_definition(catalogue, artifact.ref, QuestionDefinition)
        prompt = require_definition(
            catalogue, question.prompt_template_ref, PromptTemplateDefinition
        )
        response_ref = SchemaRef.model_validate(
            {
                **prompt.response_schema_ref.model_dump(),
                "schema_dialect": DRAFT_2020_12,
            },
        )
        response = resolve_descriptor(response_ref, inputs.descriptors)
        primary = fact_schemas.resolve(question.primary_fact_schema_ref)
        if (
            not isinstance(response, TypedValueSchemaDescriptor)
            or response.type_definition != primary.type_definition
        ):
            problems.append(
                _issue(
                    "semantic_question_response_incompatible",
                    "The Prompt response component must match the primary Fact's typed value projection.",
                    artifact,
                    ("prompt_template_ref",),
                )
            )
    return tuple(problems)


def _deployment_checks(
    catalogue: ArtifactCatalogue,
    inputs: DefinitionValidationInput,
    snapshot: CapabilitySnapshot,
    at: UTCDateTime,
) -> tuple[ValidationIssue, ...]:
    prompts = tuple(
        PromptRegistration(ref=entry.ref, definition=entry.definition)
        for entry in inputs.definitions
        if isinstance(entry, RegisteredPrompt)
    )
    checked: set[tuple[ArtifactKind, str, str, str]] = set()
    problems: list[ValidationIssue] = []

    def verify(
        ref: ModelBindingContentRef,
        site: ProfileUseContext | None = None,
        purpose: ModelCapability | None = None,
    ) -> None:
        checked.add(reference_identity(ref))
        report = validate_model_deployments(
            require_definition(catalogue, ref, ModelBinding),
            inputs.policies,
            snapshot,
            at,
            binding_ref=ref,
            profile_ref=None if site is None else site.profile_ref,
            region_ref=None if site is None else site.region_ref,
            prompts=prompts,
            consuming_purpose=purpose,
        )
        for check in report.checks:
            for issue in check.issues:
                related = tuple(
                    dict.fromkeys(
                        (
                            ref,
                            *((site.profile_ref,) if site is not None else ()),
                            *(
                                (site.region_ref,)
                                if site is not None and site.region_ref is not None
                                else ()
                            ),
                            *issue.related_refs,
                        )
                    )
                )
                problems.append(
                    ValidationIssue(
                        code=issue.code,
                        path=issue.path,
                        message=issue.message,
                        related_refs=related[:16],
                    )
                )

    for site in inputs.use_contexts:
        definition = require_definition(catalogue, site.profile_ref, ProfileDefinition)
        for use in profile_model_uses(catalogue, definition):
            verify(use.binding_ref, site, use.purpose)
    for artifact in catalogue.of_kind(ArtifactKind.MODEL_BINDING):
        if reference_identity(artifact.ref) not in checked:
            verify(ModelBindingContentRef.model_validate(artifact.ref.model_dump()))
    return tuple(problems)


def _workflow_checks(
    inputs: DefinitionValidationInput,
    snapshot: CapabilitySnapshot,
    at: UTCDateTime,
) -> tuple[ValidationIssue, ...]:
    return tuple(
        ValidationIssue(
            code="semantic_workflow_version",
            message="The host does not support the pinned ExecutionPolicy workflow version.",
            path=("policy", "workflow_version"),
            related_refs=(entry.ref,),
        )
        for entry in inputs.policies
        if isinstance(entry.artifact, ExecutionPolicyArtifact)
        and entry.artifact.policy.workflow_version
        not in snapshot.supported_workflow_versions
    )


def _finite_checks(checks: tuple[CheckResult, ...]) -> tuple[CheckResult, ...]:
    results: list[CheckResult] = []
    for check in checks:
        if check.status != "deferred":
            results.append(check)
            continue
        for item in dict.fromkeys(check.issues):
            if item.code in _HANDOFFS:
                continue
            message = _RUNTIME_BOUNDARIES.get(item.code)
            results.append(
                _check(
                    item.code,
                    (_issue(item.code, message) if message is not None else item,),
                    failure_status="deferred" if message is not None else "incomplete",
                )
            )
    return tuple(results)


def validate_bundle_semantics(
    catalogue: ArtifactCatalogue,
    closure: BundleClosure,
    manifest: BundleManifest,
    snapshot: CapabilitySnapshot,
    *,
    validation_at: UTCDateTime,
    max_bytes: int,
    max_depth: int,
    system_grants: tuple[SystemProducerGrant, ...] = (),
) -> BundleSemanticReport:
    """Require verified archive/catalogue/closure inputs and a host snapshot."""
    if tuple(item.inventory for item in catalogue.artifacts) != manifest.artifacts or (
        catalogue.required_capabilities != manifest.required_capabilities
    ):
        raise BundleArtifactError(
            "semantic_manifest_mismatch",
            "Manifest pins do not match the reconstructed catalogue.",
            "manifest.json",
        )
    at = _UTC.validate_python(validation_at)
    snapshot = CapabilitySnapshot.model_validate(snapshot)
    descriptors = build_schema_descriptors(
        catalogue, schema_index=closure.schemas, require_independent_tool_outputs=True
    )
    registry = build_fact_schema_registry(catalogue)
    inputs = build_definition_inputs(
        catalogue,
        closure,
        descriptors,
        reviews=snapshot.reviews_at(at),
        validation_at=at,
    )
    finite = validate_definition_relations(
        inputs,
        registry,
        tool_mapping_uses=partial(build_tool_mapping_uses, catalogue, inputs),
    )
    checks = list(_finite_checks(finite.checks))
    checks.extend(
        (
            _check("semantics.bundle_closure"),
            _check("semantics.schema_projection"),
            _check(
                "semantics.workflow_schema_closure",
                _workflow_checks(inputs, snapshot, at),
            ),
            _check(
                "semantics.protocol_io",
                _protocol_checks(catalogue, inputs, registry, snapshot, at),
            ),
            _check(
                "semantics.host_model_deployments",
                _deployment_checks(catalogue, inputs, snapshot, at),
            ),
        )
    )
    _resources(catalogue)
    checks.append(_check("semantics.geometry_body_and_asset_bytes"))
    optional = _validate_capability_manifest(catalogue, closure, manifest)
    capabilities = assess_capabilities(
        manifest, snapshot, validation_at=at, optional=optional
    )
    checks.append(_check("semantics.host_capabilities", capabilities.errors))
    if capabilities.warnings:
        checks.append(
            _check(
                "semantics.optional_capabilities",
                capabilities.warnings,
                failure_status="deferred",
            )
        )
    checks.append(
        _check(
            "semantics.context_source_trust",
            (
                _issue(
                    "semantics.context_source_trust",
                    _RUNTIME_BOUNDARIES["semantics.context_source_trust"],
                ),
            ),
            failure_status="deferred",
        )
    )
    advisory = tuple(
        ValidationIssue(
            code="semantics.selection_review",
            path=("policy", "selection_review_ref"),
            message=f"No trusted selection review is available. Configured fallback is {entry.artifact.policy.fallback}.",
            related_refs=(entry.ref,),
        )
        for entry in inputs.policies
        if isinstance(entry.artifact, QuestionSelectionPolicyArtifact)
        and entry.artifact.policy.optional_source == "model_advisory"
    )
    if advisory:
        checks.append(
            _check("semantics.selection_review", advisory, failure_status="deferred")
        )
    producer_checks = (
        "semantics.condition_operand_typechecking",
        "semantics.applicability_identity_eligibility",
        "semantics.rule_set_body",
        "semantics.producer_authority_admission",
        "semantics.mandatory_producer_completeness",
        "semantics.tool_plan_policies",
    )
    if any(check.status in ("invalid", "incomplete") for check in checks):
        checks.extend(
            _check(
                check_id,
                (
                    _issue(
                        "semantics.prerequisite_failed",
                        "An earlier semantic prerequisite failed.",
                    ),
                ),
                failure_status="incomplete",
            )
            for check_id in producer_checks
        )
    else:
        validate_producer_rules(
            catalogue,
            inputs,
            fact_schemas=registry,
            system_capabilities=snapshot.system_capabilities_at(at),
            system_grants=system_grants,
            max_bytes=max_bytes,
            max_depth=max_depth,
        )
        checks.extend(_check(check_id) for check_id in producer_checks)
    return BundleSemanticReport(
        bundle_digest=manifest.integrity.root_digest,
        capability_snapshot_pin=snapshot.pin,
        validation_at=at,
        checks=tuple(checks),
        capabilities=capabilities,
    )
