"""Rebuild finite semantic validation views from exact Bundle use sites."""

from collections.abc import Iterator
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.bundle.artifacts import (
    DRAFT_2020_12,
    BundleArtifactError,
    artifact_identity,
)
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.bundle.closure import BundleClosure
from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definition_enums import ContextSource, ModelCapability
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    ContextSchemaRef,
    UTCDateTime,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import (
    ModelGroundingBinding,
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.prompts import (
    PromptPurpose,
    PromptTemplateDefinition,
    PromptVariable,
)
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    FactSchemaRegistryBuilder,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef
from jimu_runtime.domain.policies import (
    AdditionalSelectionCallPolicy,
    ApplicabilityPolicyArtifact,
    ContextProvenancePolicyArtifact,
    ExecutionPolicyArtifact,
    QuestionSelectionPolicyArtifact,
    ToolSkipPolicyArtifact,
)
from jimu_runtime.domain.references import ContentRef, SchemaRef
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.schema_types import ImageSchemaRef, SchemaDescriptor
from jimu_runtime.domain.tool_mappings import (
    ContextInput,
    ContextTypeRegistration,
    ImageInput,
    ObjectInput,
    ProfileContentRef,
    ProfileContextRegistration,
    RegionContentRef,
    RegionContextRegistration,
    SnapshotContextRegistration,
)
from jimu_runtime.domain.validation.definition_inputs import (
    DefinitionRegistration,
    DefinitionValidationInput,
    ProfileUseContext,
)
from jimu_runtime.domain.validation.policies import PolicyRegistration
from jimu_runtime.domain.validation.review_inputs import ReviewMetadata
from jimu_runtime.domain.validation.tool_plans import ToolMappingUse

_DEFINITION = TypeAdapter[DefinitionRegistration](DefinitionRegistration)
_POLICY = TypeAdapter[PolicyRegistration](PolicyRegistration)
_DEFINITION_KINDS = frozenset(
    {
        ArtifactKind.ENTITY,
        ArtifactKind.REGION,
        ArtifactKind.FACET,
        ArtifactKind.QUESTION,
        ArtifactKind.PROFILE,
        ArtifactKind.PROMPT_TEMPLATE,
        ArtifactKind.MODEL_BINDING,
        ArtifactKind.TOOL,
        ArtifactKind.TOOL_PLAN,
        ArtifactKind.VOCABULARY,
        ArtifactKind.STATIC_ASSET,
    }
)


@dataclass(frozen=True, slots=True)
class ModelUse:
    binding_ref: ModelBindingContentRef
    purpose: PromptPurpose | None
    auxiliary: bool = False


@dataclass(frozen=True, slots=True)
class _ProfileSources:
    profile: ResolvedArtifact
    definition: ProfileDefinition
    models: tuple[ModelUse, ...]
    declared_context: tuple[ContextFieldSelector, ...]
    question_context: tuple[tuple[ContentRef, ContextFieldSelector], ...]
    prompt_context: tuple[PromptVariable, ...]
    profile_attribute_schemas: tuple[SchemaRef, ...]
    question_metadata: tuple[tuple[ContentRef, SchemaRef], ...]
    objects: tuple[SchemaRef, ...]
    images: tuple[SchemaRef, ...]


def _error(
    artifact: ResolvedArtifact, code: str, message: str, *location: str | int
) -> BundleArtifactError:
    return BundleArtifactError(code, message, artifact.inventory.path, location)


def require_definition[T](
    catalogue: ArtifactCatalogue, ref: ContentRef, expected: type[T]
) -> T:
    artifact = catalogue.resolve(ref)
    if not isinstance(artifact.value, expected):
        raise _error(
            artifact, "semantic_definition_type", "unexpected definition subtype"
        )
    return artifact.value


def build_fact_schema_registry(catalogue: ArtifactCatalogue) -> FactSchemaRegistry:
    builder = FactSchemaRegistryBuilder()
    for item in catalogue.of_kind(ArtifactKind.FACT_SCHEMA):
        definition = require_definition(catalogue, item.ref, FactSchemaDefinition)
        try:
            builder.register(item.ref, definition)
        except RegistryValidationError as exc:
            issue = exc.report.checks[0].issues[0]
            raise _error(item, issue.code, issue.message, *issue.path) from exc
    return builder.freeze()


def _coerce_ref[T: ContentRef](ref: ContentRef, expected: type[T]) -> T:
    return expected.model_validate(ref.model_dump())


def _policy_selectors(artifact: object) -> tuple[ContextFieldSelector, ...]:
    if isinstance(artifact, ApplicabilityPolicyArtifact):
        return tuple(
            dict.fromkeys(
                (
                    *artifact.policy.context_allowlist,
                    *condition_references(
                        artifact.policy.condition_ast
                    ).context_selectors,
                )
            )
        )
    if isinstance(artifact, ToolSkipPolicyArtifact):
        guard = artifact.policy.condition_ast
        if guard is not True:
            return condition_references(guard).context_selectors
    return ()


def _reachable_variables(
    catalogue: ArtifactCatalogue, root: ContentRef
) -> Iterator[PromptVariable]:
    stack = [root]
    seen = set()
    while stack:
        ref = stack.pop()
        identity = reference_identity(ref)
        if identity in seen:
            continue
        seen.add(identity)
        prompt = require_definition(catalogue, ref, PromptTemplateDefinition)
        yield from prompt.variables
        stack.extend(reversed(prompt.partial_refs or ()))


def profile_model_uses(
    catalogue: ArtifactCatalogue, definition: ProfileDefinition
) -> tuple[ModelUse, ...]:
    uses: list[ModelUse] = [
        ModelUse(
            _coerce_ref(definition.discovery_binding_ref, ModelBindingContentRef),
            ModelCapability.PROFILE_DISCOVERY,
        ),
        ModelUse(
            _coerce_ref(definition.observation_binding_ref, ModelBindingContentRef),
            ModelCapability.OBJECT_OBSERVATION,
        ),
    ]
    grounding = definition.grounding_binding
    if isinstance(grounding, ModelGroundingBinding):
        uses.append(
            ModelUse(
                _coerce_ref(grounding.model_binding_ref, ModelBindingContentRef),
                ModelCapability.OBJECT_GROUNDING,
            )
        )
    else:
        tool = require_definition(catalogue, grounding.tool_ref, ToolDefinition)
        if tool.model_binding_ref is not None:
            uses.append(
                ModelUse(
                    tool.model_binding_ref,
                    ModelCapability.OBJECT_GROUNDING,
                    auxiliary=True,
                )
            )
    plan = require_definition(catalogue, definition.tool_plan_ref, ToolPlanDefinition)
    for step in plan.steps:
        tool = require_definition(catalogue, step.tool_ref, ToolDefinition)
        if tool.model_binding_ref is not None:
            # N05 has no PromptPurpose in the V1 model contract.
            uses.append(ModelUse(tool.model_binding_ref, None, auxiliary=True))
    selection = require_definition(
        catalogue,
        definition.question_selection_policy_ref,
        QuestionSelectionPolicyArtifact,
    )
    if isinstance(
        selection.policy.selection_call_policy, AdditionalSelectionCallPolicy
    ):
        uses.append(
            ModelUse(
                selection.policy.selection_call_policy.proposal_binding_ref,
                ModelCapability.PROFILE_DISCOVERY,
            )
        )
    for ref in definition.rule_set_refs:
        rule_set = require_definition(catalogue, ref, RuleSetDefinition)
        if rule_set.candidate_binding_ref is not None:
            uses.append(
                ModelUse(
                    rule_set.candidate_binding_ref, ModelCapability.CANDIDATE_PREDICTION
                )
            )
    return tuple(dict.fromkeys(uses))


def _profile_sources(
    catalogue: ArtifactCatalogue,
    artifact: ResolvedArtifact,
    context_policies: tuple[ContextProvenancePolicyArtifact, ...],
) -> _ProfileSources:
    definition = require_definition(catalogue, artifact.ref, ProfileDefinition)
    plan = require_definition(catalogue, definition.tool_plan_ref, ToolPlanDefinition)
    model_uses = profile_model_uses(catalogue, definition)
    selectors: list[ContextFieldSelector] = []
    question_context: list[tuple[ContentRef, ContextFieldSelector]] = []
    prompt_context: list[PromptVariable] = []
    profile_attributes: list[SchemaRef] = []
    question_metadata: list[tuple[ContentRef, SchemaRef]] = []
    objects: list[SchemaRef] = []
    images: list[SchemaRef] = []

    def add_prompt(ref: ContentRef, question_ref: ContentRef | None = None) -> None:
        for variable in _reachable_variables(catalogue, ref):
            if variable.source == "context":
                prompt_context.append(variable)
            elif variable.source == "profile_metadata":
                profile_attributes.append(variable.schema_ref)
            elif variable.source == "question_metadata" and question_ref is not None:
                question_metadata.append((question_ref, variable.schema_ref))
            elif variable.source == "object_geometry":
                objects.append(variable.schema_ref)
            elif variable.source == "image_reference":
                images.append(variable.schema_ref)

    for use in model_uses:
        model = require_definition(catalogue, use.binding_ref, ModelBinding)
        if model.prompt_template_ref is not None:
            if use.purpose is None:
                raise _error(
                    catalogue.resolve(use.binding_ref),
                    "semantic_tool_prompt_role_unavailable",
                    "An N05 nested model Prompt has no declared V1 consuming purpose.",
                    "prompt_template_ref",
                )
            add_prompt(model.prompt_template_ref)
    for question_ref in definition.question_refs:
        question = require_definition(catalogue, question_ref, QuestionDefinition)
        selectors.extend(question.context_allowlist)
        question_context.extend(
            (question_ref, item) for item in question.context_allowlist
        )
        add_prompt(question.prompt_template_ref, question_ref)

    policies: list[ContentRef] = [definition.applicability_policy_ref]
    selection = require_definition(
        catalogue,
        definition.question_selection_policy_ref,
        QuestionSelectionPolicyArtifact,
    )
    policies.extend(
        rule.applicability_policy_ref
        for rule in selection.policy.context_selection_rules or ()
    )
    for set_ref in definition.rule_set_refs:
        rule_set = require_definition(catalogue, set_ref, RuleSetDefinition)
        policies.append(rule_set.applicability_policy_ref)
        for rule_ref in rule_set.rule_refs:
            rule = require_definition(catalogue, rule_ref, RuleDefinition)
            policies.append(rule.applicability_policy_ref)
            selectors.extend(condition_references(rule.condition_ast).context_selectors)
    policies.extend(step.skip_policy_ref for step in plan.steps)
    for policy_ref in policies:
        selectors.extend(_policy_selectors(catalogue.resolve(policy_ref).value))

    for context in context_policies:
        for field in context.policy.fields:
            selectors.extend(field.input_selectors or ())

    for binding in definition.grounding_binding.input_bindings or ():
        if isinstance(binding.source, ContextInput):
            selectors.append(binding.source.selector)
        else:
            images.append(binding.source.selector.schema_ref)
    for step in plan.steps:
        for mapping in step.input_bindings:
            source = mapping.source
            if isinstance(source, ContextInput):
                selectors.append(source.selector)
            elif isinstance(source, ImageInput):
                images.append(source.selector.schema_ref)
            elif isinstance(source, ObjectInput):
                objects.append(source.selector.schema_ref)

    return _ProfileSources(
        profile=artifact,
        definition=definition,
        models=model_uses,
        declared_context=tuple(dict.fromkeys(selectors)),
        question_context=tuple(question_context),
        prompt_context=tuple(dict.fromkeys(prompt_context)),
        profile_attribute_schemas=tuple(profile_attributes),
        question_metadata=tuple(question_metadata),
        objects=tuple(objects),
        images=tuple(images),
    )


def _one_schema(
    schemas: Iterator[SchemaRef], artifact: ResolvedArtifact, field: str
) -> SchemaRef | None:
    entries = {reference_identity(ref): ref for ref in schemas}
    if len(entries) > 1:
        raise _error(
            artifact,
            "semantic_ambiguous_schema_view",
            "Multiple exact Schema identities are declared for one source/owner.",
            field,
        )
    return next(iter(entries.values()), None)


def _region_uses(
    catalogue: ArtifactCatalogue,
    profile: ProfileDefinition,
) -> tuple[ResolvedArtifact | None, ...]:
    policy = require_definition(
        catalogue, profile.applicability_policy_ref, ApplicabilityPolicyArtifact
    ).policy
    region_targets: set[tuple[ArtifactKind, str, str, str]] = set()
    for selector in policy.identity_constraints:
        matched = reference_identity(catalogue.resolve_selector(selector).ref)
        if selector.artifact_kind is ArtifactKind.REGION:
            region_targets.add(matched)
    regions = catalogue.of_kind(ArtifactKind.REGION)
    if not regions:
        return (None,)
    return tuple(
        region
        for region in regions
        if not region_targets or reference_identity(region.ref) in region_targets
    )


def _uses(
    catalogue: ArtifactCatalogue,
    sources: _ProfileSources,
    snapshot_policies: tuple[ContextProvenancePolicyArtifact, ...],
) -> Iterator[ProfileUseContext]:
    ref = _coerce_ref(sources.profile.ref, ProfileContentRef)
    profile_context = _one_schema(
        (
            selector.schema_ref
            for selector in sources.declared_context
            if selector.source is ContextSource.PROFILE_METADATA
        ),
        sources.profile,
        "profile_metadata",
    )
    snapshot_fields = tuple(
        {
            (reference_identity(field.schema_ref), field.field_path): field.schema_ref
            for field in context.policy.fields
        }
        for context in snapshot_policies
    )
    common_snapshot_fields = (
        {
            key: schema
            for key, schema in snapshot_fields[0].items()
            if all(key in fields for fields in snapshot_fields[1:])
        }
        if snapshot_fields
        else {}
    )
    explicit_snapshot_schemas = []
    for selector in sources.declared_context:
        if selector.source is not ContextSource.CONTEXT_SNAPSHOT:
            continue
        key = reference_identity(selector.schema_ref), selector.field_path
        schema = common_snapshot_fields.get(key)
        if schema is None:
            raise _error(
                sources.profile,
                "semantic_snapshot_field_unavailable",
                "The Context Snapshot selector is not declared by every possible provenance policy.",
                "context_registrations",
            )
        explicit_snapshot_schemas.append(schema)
    for variable in sources.prompt_context:
        key = reference_identity(variable.schema_ref), variable.field_path
        if key in common_snapshot_fields:
            explicit_snapshot_schemas.append(common_snapshot_fields[key])
    snapshot_schema = _one_schema(
        iter(explicit_snapshot_schemas), sources.profile, "context_registrations"
    )
    profile_attribute = _one_schema(
        iter(sources.profile_attribute_schemas),
        sources.profile,
        "profile_metadata_schema_ref",
    )
    object_view = _one_schema(
        iter(sources.objects), sources.profile, "object_geometry_schema_ref"
    )
    image_view = _one_schema(
        iter(sources.images), sources.profile, "image_reference_schema_ref"
    )
    hypotheses: list[dict[str, object]] = []
    for question_ref in sources.definition.question_refs:
        schema_ref = _one_schema(
            (
                schema_ref
                for candidate, schema_ref in sources.question_metadata
                if reference_identity(candidate) == reference_identity(question_ref)
            ),
            catalogue.resolve(question_ref),
            "question_metadata",
        )
        if schema_ref is not None:
            hypotheses.append(
                {
                    "question_ref": question_ref.model_dump(),
                    "schema_ref": schema_ref.model_dump(),
                }
            )
    prompt_uses: dict[tuple[ArtifactKind, str, str, str], ModelUse] = {}
    for item in sources.models:
        if item.purpose is None:
            continue
        binding_key = reference_identity(item.binding_ref)
        prior = prompt_uses.setdefault(binding_key, item)
        if prior.purpose != item.purpose:
            raise _error(
                sources.profile,
                "semantic_model_use_conflict",
                "One ModelBinding has conflicting consuming purposes.",
            )
    for region in _region_uses(catalogue, sources.definition):
        registrations: list[ContextTypeRegistration] = []
        if snapshot_schema is not None:
            registrations.append(
                SnapshotContextRegistration(
                    source="context_snapshot",
                    schema_ref=_coerce_ref(snapshot_schema, ContextSchemaRef),
                )
            )
        if profile_context is not None:
            registrations.append(
                ProfileContextRegistration(
                    source="profile_metadata",
                    profile_ref=ref,
                    schema_ref=_coerce_ref(profile_context, ContextSchemaRef),
                )
            )
        fields: dict[str, object] = {}
        if region is not None:
            region_ref = _coerce_ref(region.ref, RegionContentRef)
            definition = require_definition(catalogue, region.ref, RegionDefinition)
            registrations.append(
                RegionContextRegistration(
                    source="region_metadata",
                    region_ref=region_ref,
                    schema_ref=ContextSchemaRef.model_validate(
                        {
                            **definition.context_schema_ref.model_dump(),
                            "schema_dialect": DRAFT_2020_12,
                        }
                    ),
                )
            )
            fields["region_ref"] = region_ref
        for question_owner_ref, selector in sources.question_context:
            if not any(
                registration.source == selector.source.value
                and reference_identity(registration.schema_ref)
                == reference_identity(selector.schema_ref)
                for registration in registrations
            ):
                raise _error(
                    catalogue.resolve(question_owner_ref),
                    "semantic_question_context_owner",
                    "Question allowlist has no matching source/owner in this Profile/Region use.",
                    "context_allowlist",
                )
        for field_name, schema_ref in (
            ("profile_metadata_schema_ref", profile_attribute),
            ("object_geometry_schema_ref", object_view),
            ("image_reference_schema_ref", image_view),
        ):
            if schema_ref is not None:
                fields[field_name] = schema_ref.model_dump()
        yield ProfileUseContext.model_validate(
            dict(
                fields,
                profile_ref=ref,
                context_registrations=tuple(registrations),
                question_metadata_views=tuple(hypotheses),
                mapping_contexts=(),
                model_prompt_uses=tuple(
                    {"binding_ref": item.binding_ref, "consuming_purpose": item.purpose}
                    for item in prompt_uses.values()
                ),
            )
        )


def build_definition_inputs(
    catalogue: ArtifactCatalogue,
    closure: BundleClosure,
    descriptors: tuple[SchemaDescriptor, ...],
    *,
    reviews: tuple[ReviewMetadata, ...],
    validation_at: UTCDateTime,
) -> DefinitionValidationInput:
    """Project the WP-01B view using only pinned definitions and host reviews."""
    if closure.schemas.catalogue is not catalogue or set(closure.dependencies) != {
        artifact_identity(item.ref) for item in catalogue.artifacts
    }:
        raise BundleArtifactError(
            "semantic_closure_mismatch",
            "The closure does not describe this catalogue.",
            "manifest.json",
        )
    try:
        definitions = tuple(
            _DEFINITION.validate_python(
                {
                    "registration_kind": item.ref.artifact_kind.value,
                    "ref": item.ref.model_dump(),
                    "definition": item.value,
                }
            )
            for item in catalogue.artifacts
            if item.ref.artifact_kind in _DEFINITION_KINDS
        )
        policies = tuple(
            _POLICY.validate_python(
                {"ref": item.ref.model_dump(), "artifact": item.value}
            )
            for item in catalogue.of_kind(ArtifactKind.POLICY)
        )
        context_policies = tuple(
            item.artifact
            for item in policies
            if isinstance(item.artifact, ContextProvenancePolicyArtifact)
        )
        snapshot_policies = tuple(
            require_definition(
                catalogue,
                item.artifact.policy.context_provenance_policy_ref,
                ContextProvenancePolicyArtifact,
            )
            for item in policies
            if isinstance(item.artifact, ExecutionPolicyArtifact)
        )
        uses = tuple(
            use
            for item in catalogue.of_kind(ArtifactKind.PROFILE)
            for use in _uses(
                catalogue,
                _profile_sources(catalogue, item, context_policies),
                snapshot_policies,
            )
        )
        return DefinitionValidationInput(
            definitions=definitions,
            descriptors=descriptors,
            policies=policies,
            reviews=reviews,
            validation_at=validation_at,
            fact_schema_refs=tuple(
                _coerce_ref(item.ref, FactSchemaContentRef)
                for item in catalogue.of_kind(ArtifactKind.FACT_SCHEMA)
            ),
            use_contexts=uses,
        )
    except ValidationError as exc:
        raise BundleArtifactError(
            "semantic_definition_inputs",
            "The reconstructed finite Definition view violates its contract or capacity.",
            "manifest.json",
        ) from exc


def build_tool_mapping_uses(
    catalogue: ArtifactCatalogue,
    inputs: DefinitionValidationInput,
    use: ProfileUseContext,
) -> tuple[ToolMappingUse, ...]:
    """Provide one current Profile/Plan view per actual Tool step."""
    profile = require_definition(catalogue, use.profile_ref, ProfileDefinition)
    plan = require_definition(catalogue, profile.tool_plan_ref, ToolPlanDefinition)
    return tuple(
        ToolMappingUse(
            profile_ref=use.profile_ref,
            tool_plan_ref=profile.tool_plan_ref,
            current_step_id=step.step_id,
            context_registrations=use.context_registrations,
            object_schema_ref=use.object_geometry_schema_ref,
            image_schema_ref=(
                _coerce_ref(use.image_reference_schema_ref, ImageSchemaRef)
                if use.image_reference_schema_ref is not None
                else None
            ),
            descriptors=inputs.descriptors,
        )
        for step in plan.steps
    )
