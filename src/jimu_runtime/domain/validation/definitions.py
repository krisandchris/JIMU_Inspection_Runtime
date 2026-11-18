"""Caller-pinned finite relations; never full Bundle, Producer or runtime admission."""

from collections.abc import Callable
from functools import partial
from typing import NoReturn

from pydantic import ValidationError

from jimu_runtime.domain.conditions import condition_references
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ModelCapability,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    DefinitionContract,
    VocabularyMemberRef,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import (
    FacetDefinition,
    ModelGroundingBinding,
    ProfileDefinition,
    QuestionDefinition,
    ToolGroundingBinding,
)
from jimu_runtime.domain.definitions.models import (
    ModelBinding,
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.definitions.ontology import EntityDefinition, RegionDefinition
from jimu_runtime.domain.definitions.prompts import (
    PromptTemplateDefinition,
    PromptVariable,
    placeholder_names,
)
from jimu_runtime.domain.definitions.resources import VocabularyDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import (
    DeclaredCoordinateMetadata,
    DeclaredUnitMetadata,
    FactSchemaDefinition,
)
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import ToolPlanContentRef
from jimu_runtime.domain.policies import (
    ApplicabilityPolicyArtifact,
    ContextProvenancePolicyArtifact,
    EvidencePolicyArtifact,
    ExecutionPolicyArtifact,
    PolicyArtifact,
    QuestionSelectionPolicyArtifact,
    QuestionValidationPolicyArtifact,
    ToolSkipPolicyArtifact,
    UnknownPolicyArtifact,
)
from jimu_runtime.domain.references import ContentRef, SchemaRef
from jimu_runtime.domain.schema_types import (
    TypedValueSchemaDescriptor,
)
from jimu_runtime.domain.tool_mappings import (
    ContextInput,
    ImageInput,
    MappingTypeContext,
    ObjectInput,
    ProfileContextRegistration,
    RegionContextRegistration,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ApplicabilityConditionContext,
    ConditionReferenceContext,
    ToolStepGuardConditionContext,
)
from jimu_runtime.domain.validation.conditions import validate_condition_references
from jimu_runtime.domain.validation.definition_inputs import (
    DefinitionRegistration,
    DefinitionValidationInput,
    ProfileRegistration,
    ProfileUseContext,
    RegisteredModel,
    RegisteredPrompt,
    RegisteredTool,
)
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.policies import (
    ModelBindingRegistration,
    PromptRegistration,
    resolve_policy,
    validate_context_derivations,
    validate_execution_policy,
    validate_model_policies,
    validate_question_selection,
    validate_step_failure_policies,
    validate_tool_policy_bindings,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    describe_field,
    require_mapping_compatibility,
    resolve_descriptor,
    validate_target_coverage,
)
from jimu_runtime.domain.validation.tool_plans import (
    ToolRegistration,
    validate_tool_definition,
    validate_tool_plan,
)
from jimu_runtime.domain.values import RecordTypeDefinition, validate_typed_value


class _RelationError(ValueError):
    def __init__(
        self, code: str, message: str, status: CheckStatus = "invalid"
    ) -> None:
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _fail(code: str, message: str, status: CheckStatus = "invalid") -> NoReturn:
    raise _RelationError(code, message, status)


class _DiagnosticOverflow(ValueError):
    pass


class _Results:
    def __init__(self) -> None:
        self.groups: dict[tuple[str, CheckStatus], list[ValidationIssue]] = {}

    def add(
        self, category: str, status: CheckStatus, issue: ValidationIssue | None = None
    ) -> None:
        group = self.groups.setdefault((category, status), [])
        if issue is not None:
            if len(group) >= 64:
                raise _DiagnosticOverflow(
                    "Diagnostic enumeration exceeded a category budget"
                )
            group.append(issue)
        if len(self.groups) > 128:
            raise _DiagnosticOverflow(
                "Diagnostic enumeration exceeded its check budget"
            )

    def error(
        self, category: str, code: str, message: str, status: CheckStatus = "invalid"
    ) -> None:
        self.add(
            category,
            status,
            ValidationIssue(code=code, path=(), message=message, related_refs=()),
        )

    def run(self, category: str, action: Callable[[], object]) -> bool:
        failures_before = sum(
            len(items)
            for (_, status), items in self.groups.items()
            if status != "passed"
        )
        try:
            outcome = action()
            if isinstance(outcome, ValidationReport):
                self.merge(category, outcome)
                return outcome.aggregate_status == "passed"
        except _RelationError as exc:
            self.error(category, exc.code, exc.message, exc.status)
        except SchemaCheckError as exc:
            self.add(
                category,
                exc.issue.outcome,
                ValidationIssue(
                    code=exc.issue.code,
                    path=exc.issue.field_path,
                    message=exc.issue.message,
                    related_refs=(),
                ),
            )
        except RegistryValidationError as exc:
            for check in exc.report.checks:
                for issue in check.issues:
                    status: CheckStatus = (
                        "incomplete"
                        if issue.code == "registry.unknown_identity"
                        else check.status
                    )
                    self.add(category, status, issue)
        except _DiagnosticOverflow:
            raise
        except (ValidationError, ValueError, TypeError):
            self.error(
                category,
                "definitions.contract_invalid",
                "Input or dependent contract failed strict validation.",
            )
        else:
            failures_after = sum(
                len(items)
                for (_, status), items in self.groups.items()
                if status != "passed"
            )
            if failures_after != failures_before:
                return False
            self.add(category, "passed")
            return True
        return False

    def merge(self, category: str, report: ValidationReport) -> None:
        for check in report.checks:
            if check.status == "passed":
                self.add(category, "passed")
            else:
                for issue in check.issues:
                    self.add(category, check.status, issue)

    def report(self) -> ValidationReport:
        return ValidationReport(
            scope="definition_relations",
            checks=tuple(
                CheckResult(
                    check_id=f"definitions.{category}.{status}",
                    status=status,
                    issues=tuple(issues),
                )
                for (category, status), issues in self.groups.items()
            ),
        )

    def overflow_report(self) -> ValidationReport:
        """Preserve a bounded diagnostic prefix and never downgrade known invalidity."""
        known_invalid = any(status == "invalid" for _, status in self.groups)
        prefix = _Results()
        for key, issues in list(self.groups.items())[:125]:
            if key[0] != "boundaries":
                prefix.groups[key] = list(issues)
        prefix.error(
            "diagnostics",
            "definitions.diagnostic_capacity",
            "Diagnostic capacity exceeded; the preserved prefix is not a complete enumeration.",
            "invalid" if known_invalid else "incomplete",
        )
        _deferred(prefix)
        return prefix.report()


def _same(left: ContentRef, right: ContentRef) -> bool:
    return reference_identity(left) == reference_identity(right)


def _as_schema(ref: ContentRef) -> SchemaRef:
    return SchemaRef.model_validate(
        {
            **ref.model_dump(mode="json"),
            "schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        }
    )


def _unique(refs: tuple[ContentRef, ...]) -> None:
    seen = set()
    for ref in refs:
        key = reference_identity(ref)[:3]
        if key in seen:
            _fail(
                "definitions.identity_conflict",
                "Duplicate or conflicting versioned catalogue identity.",
            )
        seen.add(key)


class _Validation:
    def __init__(
        self,
        inputs: DefinitionValidationInput,
        registry: FactSchemaRegistry,
        results: _Results,
    ) -> None:
        self.inputs, self.registry, self.results = inputs, registry, results
        self.reachable_schemas: dict[
            tuple[ArtifactKind, str, str, str], ContentRef
        ] = {}
        self.entries = {
            reference_identity(entry.ref)[:3]: entry for entry in inputs.definitions
        }

    def definition[T: DefinitionContract](
        self, ref: ContentRef, expected: type[T]
    ) -> T:
        entry = self.entries.get(reference_identity(ref)[:3])
        if entry is None:
            _fail(
                "definitions.target_missing",
                "Exact definition is not supplied.",
                "incomplete",
            )
        if not _same(entry.ref, ref):
            _fail(
                "definitions.digest_mismatch",
                "Known definition identity has a different digest.",
            )
        if not isinstance(entry.definition, expected):
            _fail(
                "definitions.target_kind", "Referenced definition has the wrong kind."
            )
        return entry.definition

    def schema(self, ref: ContentRef) -> FactSchemaDefinition:
        self.reachable_schemas[reference_identity(ref)] = ref
        return self.registry.resolve(ref)

    def vocabulary(self, member: VocabularyMemberRef, domain: str) -> None:
        definition = self.definition(member.vocabulary_ref, VocabularyDefinition)
        if (
            definition.semantic_domain != domain
            or member.member not in definition.members
        ):
            _fail(
                "definitions.vocabulary_member",
                "Vocabulary member or semantic domain does not match.",
            )

    def policy(self, ref: ContentRef) -> PolicyArtifact:
        return resolve_policy(
            PolicyContentRef.model_validate(ref.model_dump(mode="json")),
            self.inputs.policies,
        )

    def descriptor(self, ref: ContentRef) -> None:
        resolve_descriptor(_as_schema(ref), self.inputs.descriptors)

    def ontology(self, definition: EntityDefinition | RegionDefinition) -> None:
        if isinstance(definition, EntityDefinition):
            for ref in definition.attribute_schema_refs:
                self.descriptor(ref)
        else:
            self.descriptor(definition.context_schema_ref)
            for asset_ref in definition.asset_refs:
                self.definition(asset_ref, DefinitionContract)

    def ontology_vocabulary(
        self, definition: EntityDefinition | RegionDefinition
    ) -> None:
        if isinstance(definition, EntityDefinition):
            self.vocabulary(definition.entity_type, "entity.type")
            self.vocabulary(definition.lifecycle, "entity.lifecycle")
        else:
            self.vocabulary(definition.region_type, "region.type")
            self.vocabulary(definition.scene_type, "region.scene")

    def facet(self, facet: FacetDefinition) -> None:
        for ref in facet.fact_schema_refs:
            self.schema(ref)
        for metadata in facet.metadata or ():
            descriptor = resolve_descriptor(
                _as_schema(metadata.attribute_schema_ref), self.inputs.descriptors
            )
            if not isinstance(descriptor, TypedValueSchemaDescriptor) or not isinstance(
                descriptor.type_definition, RecordTypeDefinition
            ):
                _fail(
                    "definitions.metadata_encoding",
                    "Facet metadata requires a trusted typed Record descriptor.",
                )
            validate_typed_value(descriptor.type_definition, metadata.value)

    def question(self, question: QuestionDefinition) -> None:
        for selector in question.context_allowlist:
            describe_field(
                resolve_descriptor(selector.schema_ref, self.inputs.descriptors),
                selector.field_path,
            )
        schema = self.schema(question.primary_fact_schema_ref)
        if (
            schema.scope is not FactScope.OBJECT
            or ProducerKind.VLM_QUESTION not in schema.allowed_producer_kinds
        ):
            _fail(
                "definitions.question_schema",
                "Question primary Schema must be object scoped and allow vlm_question.",
            )
        for ref in question.facet_refs:
            facet = self.definition(ref, FacetDefinition)
            if not any(
                _same(question.primary_fact_schema_ref, item)
                for item in facet.fact_schema_refs
            ):
                _fail(
                    "definitions.facet_intersection",
                    "Primary Schema must be an exact member of every Question Facet.",
                )
        prompt = self.definition(question.prompt_template_ref, PromptTemplateDefinition)
        if prompt.purpose is not ModelCapability.OBJECT_OBSERVATION:
            _fail(
                "definitions.question_prompt",
                "Question Prompt must have object_observation purpose.",
            )

    def profile(self, profile: ProfileDefinition) -> None:
        self.definition(profile.entity_ref, EntityDefinition)
        self.definition(profile.tool_plan_ref, ToolPlanDefinition)
        self.results.run(
            "policies",
            partial(
                self.condition_policy,
                profile.applicability_policy_ref,
                applicability=True,
            ),
        )
        for ref in profile.facet_refs:
            self.definition(ref, FacetDefinition)
        for question_ref in profile.question_refs:
            question = self.definition(question_ref, QuestionDefinition)
            if any(
                not any(_same(facet, declared) for declared in profile.facet_refs)
                for facet in question.facet_refs
            ):
                _fail(
                    "definitions.profile_facets",
                    "Profile must declare all Facets of each Question.",
                )

    def condition_policy(
        self, ref: ContentRef, *, applicability: bool
    ) -> ApplicabilityPolicyArtifact | ToolSkipPolicyArtifact:
        artifact = self.policy(ref)
        expected = (
            ApplicabilityPolicyArtifact if applicability else ToolSkipPolicyArtifact
        )
        if not isinstance(artifact, expected):
            _fail(
                "definitions.policy_subtype",
                "Condition Policy has the wrong actual subtype.",
            )
        assert isinstance(
            artifact, (ApplicabilityPolicyArtifact, ToolSkipPolicyArtifact)
        )
        return artifact

    def capabilities(self, profile: ProfileDefinition) -> None:
        for ref, capability in (
            (profile.discovery_binding_ref, ModelCapability.PROFILE_DISCOVERY),
            (profile.observation_binding_ref, ModelCapability.OBJECT_OBSERVATION),
        ):
            if self.definition(ref, ModelBinding).capability is not capability:
                _fail(
                    "definitions.model_capability",
                    "Profile ModelBinding capability does not match its role.",
                )
        binding = profile.grounding_binding
        if isinstance(binding, ModelGroundingBinding):
            if (
                self.definition(binding.model_binding_ref, ModelBinding).capability
                is not ModelCapability.OBJECT_GROUNDING
            ):
                _fail(
                    "definitions.grounding_capability",
                    "Model grounding requires object_grounding capability.",
                )
        else:
            tool = self.definition(binding.tool_ref, ToolDefinition)
            if not any(tag.value == "object_grounding" for tag in tool.capability_tags):
                _fail(
                    "definitions.grounding_capability",
                    "Tool grounding requires object_grounding tag.",
                )

    def closure(self, ref: ContentRef) -> tuple[PromptVariable, ...]:
        colors: dict[tuple[ArtifactKind, str, str, str], int] = {}
        variables: dict[str, PromptVariable] = {}
        uses: set[str] = set()
        stack = [(ref, False)]
        while stack:
            current, leaving = stack.pop()
            key = reference_identity(current)
            if leaving:
                colors[key] = 2
                continue
            if colors.get(key) == 1:
                _fail("definitions.prompt_cycle", "Prompt partial graph is cyclic.")
            if colors.get(key) == 2:
                continue
            prompt = self.definition(current, PromptTemplateDefinition)
            self.descriptor(prompt.response_schema_ref)
            colors[key] = 1
            stack.append((current, True))
            for variable in prompt.variables:
                if variable.name in variables and variables[variable.name] != variable:
                    _fail(
                        "definitions.prompt_declaration_conflict",
                        "Partial graph has conflicting declarations for one variable name.",
                    )
                variables[variable.name] = variable
            uses.update(placeholder_names(prompt.template_text))
            stack.extend(
                (partial, False) for partial in reversed(prompt.partial_refs or ())
            )
        if set(variables) != uses:
            _fail(
                "definitions.prompt_registration",
                "Complete Prompt graph must have equal declared and used name sets.",
            )
        return tuple(variables.values())

    def _context(
        self, use: ProfileUseContext, schema_ref: SchemaRef, source: str | None
    ) -> None:
        # A known schema identity with wrong digest is invalid, not a missing
        # owner. Resolve content identity before matching the owner views.
        resolve_descriptor(schema_ref, self.inputs.descriptors)
        matches = [
            registration
            for registration in use.context_registrations
            if _same(registration.schema_ref, schema_ref)
            and (source is None or registration.source == source)
        ]
        if len(matches) != 1:
            _fail(
                "definitions.context_owner_ambiguous",
                "Context requires exactly one declared source/owner view.",
                "incomplete",
            )
        registration = matches[0]
        if isinstance(registration, ProfileContextRegistration) and not _same(
            registration.profile_ref, use.profile_ref
        ):
            _fail(
                "definitions.context_owner",
                "Context Profile owner differs from current use.",
            )
        if isinstance(registration, RegionContextRegistration):
            if use.region_ref is None:
                _fail(
                    "definitions.region_unavailable",
                    "Current Region owner is not supplied.",
                    "incomplete",
                )
            if not _same(registration.region_ref, use.region_ref):
                _fail(
                    "definitions.context_owner",
                    "Used Region metadata differs from current Region.",
                )

    def prompt_context(
        self,
        ref: ContentRef,
        use: ProfileUseContext,
        question: QuestionDefinition | None,
        purpose: ModelCapability,
    ) -> None:
        for variable in self.closure(ref):
            if variable.source == "context":
                self._context(use, variable.schema_ref, None)
                registration = next(
                    item
                    for item in use.context_registrations
                    if _same(item.schema_ref, variable.schema_ref)
                )
                if question is not None and not any(
                    selector.source.value == registration.source
                    and _same(selector.schema_ref, variable.schema_ref)
                    and selector.field_path == variable.field_path
                    for selector in question.context_allowlist
                ):
                    _fail(
                        "definitions.question_context_allowlist",
                        "Question must allow this exact inherited context source/schema/path.",
                    )
            else:
                expected: SchemaRef | None
                if variable.source == "question_metadata":
                    if question is None:
                        _fail(
                            "definitions.question_owner_unavailable",
                            "Model-level joint Prompt has no singular Question metadata owner.",
                            "incomplete",
                        )
                    view = next(
                        (
                            item
                            for item in use.question_metadata_views
                            if item.question_ref.artifact_id == question.question_id
                            and item.question_ref.version == question.version
                        ),
                        None,
                    )
                    expected = None if view is None else view.schema_ref
                    if view is not None:
                        self.definition(view.question_ref, QuestionDefinition)
                elif variable.source == "profile_metadata":
                    expected = use.profile_metadata_schema_ref
                elif variable.source == "object_geometry":
                    if purpose in (
                        ModelCapability.PROFILE_DISCOVERY,
                        ModelCapability.OBJECT_GROUNDING,
                    ):
                        _fail(
                            "definitions.object_owner_unavailable",
                            "No created Object owner exists at discovery/grounding stage.",
                            "incomplete",
                        )
                    expected = use.object_geometry_schema_ref
                else:
                    expected = use.image_reference_schema_ref
                if expected is None:
                    _fail(
                        "definitions.prompt_view_missing",
                        "Prompt source owner/view is not supplied.",
                        "incomplete",
                    )
                if not _same(variable.schema_ref, expected):
                    _fail(
                        "definitions.prompt_view_mismatch",
                        "Prompt Schema differs from its declared owner view.",
                    )
            describe_field(
                resolve_descriptor(variable.schema_ref, self.inputs.descriptors),
                variable.field_path,
            )

    def grounding(self, profile: ProfileDefinition, use: ProfileUseContext) -> None:
        binding = profile.grounding_binding
        consumer: ModelBinding | ToolDefinition
        if isinstance(binding, ModelGroundingBinding):
            consumer = self.definition(binding.model_binding_ref, ModelBinding)
        else:
            consumer = self.definition(binding.tool_ref, ToolDefinition)
        target = resolve_descriptor(consumer.input_schema_ref, self.inputs.descriptors)
        bindings = binding.input_bindings or ()
        validate_target_coverage(target, bindings)
        for mapping in bindings:
            if not _same(mapping.target.schema_ref, consumer.input_schema_ref):
                _fail(
                    "definitions.grounding_target",
                    "Grounding target must match exact consuming input Schema.",
                )
            source = mapping.source
            if isinstance(source, ContextInput):
                self._context(
                    use, source.selector.schema_ref, source.selector.source.value
                )
            elif isinstance(source, ImageInput):
                if use.image_reference_schema_ref is None:
                    _fail(
                        "definitions.image_view_missing",
                        "Original image-reference view is not supplied.",
                        "incomplete",
                    )
                if not _same(
                    source.selector.schema_ref, use.image_reference_schema_ref
                ):
                    _fail(
                        "definitions.image_view_mismatch",
                        "Grounding image source is not the declared original media view.",
                    )
            source_type = describe_field(
                resolve_descriptor(source.selector.schema_ref, self.inputs.descriptors),
                source.selector.field_path,
            )
            require_mapping_compatibility(
                source_type, describe_field(target, mapping.target.field_path), mapping
            )

    def mapping_view(
        self, profile: ProfileDefinition, use: ProfileUseContext, context_index: int
    ) -> None:
        context = use.mapping_contexts[context_index]
        if not _same(context.profile_ref, use.profile_ref) or not _same(
            context.tool_plan_ref, profile.tool_plan_ref
        ):
            _fail(
                "definitions.mapping_owner",
                "Nested mapping context must pin the current Profile and configured ToolPlan.",
            )
        plan = self.definition(profile.tool_plan_ref, ToolPlanDefinition)
        step = next(
            (item for item in plan.steps if item.step_id == context.current_step_id),
            None,
        )
        if step is None:
            _fail(
                "definitions.mapping_step",
                "Mapping context must refer to an actual configured step.",
            )
        for descriptor in context.descriptors:
            outer = resolve_descriptor(descriptor.schema_ref, self.inputs.descriptors)
            if outer != descriptor:
                _fail(
                    "definitions.nested_descriptor_conflict",
                    "Nested descriptor differs from the authoritative outer view.",
                )
        for registration in context.context_registrations:
            if registration not in use.context_registrations:
                _fail(
                    "definitions.nested_context_conflict",
                    "Nested Context registration is not a declared outer source/owner/schema view.",
                )
        for mapping in step.input_bindings:
            source = mapping.source
            if isinstance(source, ContextInput):
                self._context(
                    use, source.selector.schema_ref, source.selector.source.value
                )
            elif isinstance(source, (ObjectInput, ImageInput)):
                outer_ref = (
                    use.object_geometry_schema_ref
                    if isinstance(source, ObjectInput)
                    else use.image_reference_schema_ref
                )
                nested = (
                    context.object_schema_ref
                    if isinstance(source, ObjectInput)
                    else context.image_schema_ref
                )
                if outer_ref is None:
                    _fail(
                        "definitions.outer_view_missing",
                        "Used nested Object/image view is absent from outer use.",
                        "incomplete",
                    )
                if not _same(outer_ref, nested):
                    _fail(
                        "definitions.outer_view_mismatch",
                        "Used nested Object/image view differs from outer use.",
                    )

    def schema_metadata(self, ref: ContentRef) -> None:
        schema = self.schema(ref)
        if isinstance(schema.units, DeclaredUnitMetadata):
            for binding in schema.units.bindings:
                self.vocabulary(binding.unit, "unit")
        if isinstance(schema.coordinate_system, DeclaredCoordinateMetadata):
            for coordinate_binding in schema.coordinate_system.bindings:
                self.vocabulary(
                    coordinate_binding.coordinate_system, "coordinate_system"
                )
        if not isinstance(
            self.policy(schema.evidence_policy_ref), EvidencePolicyArtifact
        ):
            _fail(
                "definitions.policy_subtype",
                "Fact Schema evidence policy must resolve to actual EvidencePolicy.",
            )


def _preflight(inputs: DefinitionValidationInput) -> None:
    _unique(tuple(entry.ref for entry in inputs.definitions))
    _unique(tuple(entry.schema_ref for entry in inputs.descriptors))
    _unique(tuple(entry.ref for entry in inputs.policies))
    _unique(tuple(inputs.fact_schema_refs))
    # Repeated references are legitimate, but a finite input cannot claim two
    # digests for one versioned owner/schema identity, including unused views.
    claims: list[ContentRef] = [entry.ref for entry in inputs.definitions]
    claims.extend(entry.schema_ref for entry in inputs.descriptors)
    claims.extend(entry.ref for entry in inputs.policies)
    claims.extend(inputs.fact_schema_refs)
    for use in inputs.use_contexts:
        claims.append(use.profile_ref)
        if use.region_ref is not None:
            claims.append(use.region_ref)
        for view in (
            use.profile_metadata_schema_ref,
            use.object_geometry_schema_ref,
            use.image_reference_schema_ref,
        ):
            if view is not None:
                claims.append(view)
        for question_view in use.question_metadata_views:
            claims.extend((question_view.question_ref, question_view.schema_ref))
        claims.extend(model_use.binding_ref for model_use in use.model_prompt_uses)
        registrations = list(use.context_registrations)
        for context in use.mapping_contexts:
            claims.extend(
                (
                    context.profile_ref,
                    context.tool_plan_ref,
                    context.object_schema_ref,
                    context.image_schema_ref,
                )
            )
            claims.extend(descriptor.schema_ref for descriptor in context.descriptors)
            registrations.extend(context.context_registrations)
        for registration in registrations:
            claims.append(registration.schema_ref)
            if isinstance(registration, ProfileContextRegistration):
                claims.append(registration.profile_ref)
            elif isinstance(registration, RegionContextRegistration):
                claims.append(registration.region_ref)
    seen_claims: dict[tuple[ArtifactKind, str, str], str] = {}
    for claim in claims:
        identity = reference_identity(claim)
        if identity[:3] in seen_claims and seen_claims[identity[:3]] != identity[3]:
            _fail(
                "definitions.view_identity_conflict",
                "Finite view contains conflicting digests for one versioned owner/schema identity.",
            )
        seen_claims[identity[:3]] = identity[3]
    seen_reports = set()
    for report in inputs.reviews:
        key = (
            report.report_ref.review_registry_id,
            report.report_ref.report_id,
            report.report_ref.version,
        )
        if key in seen_reports:
            _fail(
                "definitions.review_conflict",
                "Duplicate or conflicting trusted report identity.",
            )
        seen_reports.add(key)
    uses = set()
    for use in inputs.use_contexts:
        use_key = (
            reference_identity(use.profile_ref),
            None if use.region_ref is None else reference_identity(use.region_ref),
        )
        if use_key in uses:
            _fail("definitions.use_conflict", "Duplicate Profile/Region use context.")
        uses.add(use_key)
        owners = set()
        for registration in use.context_registrations:
            owner = (
                registration.profile_ref
                if isinstance(registration, ProfileContextRegistration)
                else registration.region_ref
                if isinstance(registration, RegionContextRegistration)
                else None
            )
            owner_key = None if owner is None else reference_identity(owner)[:3]
            k = (registration.source, owner_key)
            if k in owners:
                _fail(
                    "definitions.context_conflict",
                    "Duplicate or conflicting Context source/owner identity.",
                )
            owners.add(k)
            if isinstance(registration, ProfileContextRegistration) and not _same(
                registration.profile_ref, use.profile_ref
            ):
                _fail(
                    "definitions.context_owner",
                    "Profile Context registration has a foreign owner.",
                )


def _nested_preflight(
    inputs: DefinitionValidationInput,
    use: ProfileUseContext,
    context: MappingTypeContext,
) -> None:
    if not _same(context.profile_ref, use.profile_ref):
        _fail(
            "definitions.mapping_owner",
            "Nested mapping context has a foreign current Profile.",
        )
    for descriptor in context.descriptors:
        outer = resolve_descriptor(descriptor.schema_ref, inputs.descriptors)
        if descriptor != outer:
            _fail(
                "definitions.nested_descriptor_conflict",
                "Nested descriptor body differs from its exact authoritative outer descriptor.",
            )
    if any(
        registration not in use.context_registrations
        for registration in context.context_registrations
    ):
        _fail(
            "definitions.nested_context_conflict",
            "Nested source/owner/schema view is not an outer declared view.",
        )


def _deferred(results: _Results) -> None:
    for name in (
        "condition_operand_typechecking",
        "condition_evaluation",
        "applicability_identity_eligibility",
        "rule_set_body",
        "producer_authority_admission",
        "mandatory_producer_completeness",
        "bundle_closure",
        "external_authenticity",
        "semantic_protocol_io",
        "geometry_body_and_asset_bytes",
    ):
        results.error(
            "boundaries",
            f"definitions.deferred.{name}",
            f"Not established by finite Definition relations: {name}.",
            "deferred",
        )


def _question_policies(state: _Validation, question: QuestionDefinition) -> None:
    for ref, expected in (
        (question.evidence_policy_ref, EvidencePolicyArtifact),
        (question.unknown_policy_ref, UnknownPolicyArtifact),
        (question.validation_policy_ref, QuestionValidationPolicyArtifact),
    ):
        if not isinstance(state.policy(ref), expected):
            _fail(
                "definitions.policy_subtype",
                "Question Policy has the wrong actual subtype.",
            )


def _selection(
    state: _Validation,
    profile: ProfileDefinition,
    models: tuple[ModelBindingRegistration, ...],
) -> ValidationReport:
    artifact = state.policy(profile.question_selection_policy_ref)
    if not isinstance(artifact, QuestionSelectionPolicyArtifact):
        _fail(
            "definitions.policy_subtype",
            "Question selection ref has wrong actual subtype.",
        )
    for rule in artifact.policy.context_selection_rules or ():
        state.condition_policy(rule.applicability_policy_ref, applicability=True)
    return validate_question_selection(
        artifact,
        profile,
        state.inputs.policies,
        policy_ref=PolicyContentRef.model_validate(
            profile.question_selection_policy_ref.model_dump(mode="json")
        ),
        model_bindings=models,
    )


def _nested_model(state: _Validation, tool: ToolDefinition) -> None:
    if (
        tool.model_binding_ref is not None
        and state.definition(tool.model_binding_ref, ModelBinding).capability
        is not ModelCapability.VISION_AUXILIARY
    ):
        _fail(
            "definitions.nested_model_capability",
            "Tool nested ModelBinding requires vision_auxiliary.",
        )


def _validate_entry(
    state: _Validation,
    entry: DefinitionRegistration,
    models: tuple[ModelBindingRegistration, ...],
) -> None:
    results, inputs, definition = state.results, state.inputs, entry.definition
    if isinstance(definition, (EntityDefinition, RegionDefinition)):
        results.run("ontology", partial(state.ontology, definition))
        results.run("vocabulary", partial(state.ontology_vocabulary, definition))
    elif isinstance(definition, FacetDefinition):
        results.run("facets", partial(state.facet, definition))
        for tag in definition.tags or ():
            results.run("vocabulary", partial(state.vocabulary, tag, "facet.tag"))
    elif isinstance(definition, QuestionDefinition):
        results.run("questions", partial(state.question, definition))
        results.run("policies", partial(_question_policies, state, definition))
    elif isinstance(definition, ProfileDefinition):
        results.run("profiles", partial(state.profile, definition))
        results.run("capabilities", partial(state.capabilities, definition))
        results.run("policies", partial(_selection, state, definition, models))
    if isinstance(definition, PromptTemplateDefinition):
        results.run("prompt_closure", partial(state.closure, entry.ref))
    if isinstance(definition, ToolDefinition):
        results.run(
            "tool_plans",
            partial(
                validate_tool_definition, definition, inputs.descriptors, state.registry
            ),
        )
        results.run(
            "policies",
            partial(validate_tool_policy_bindings, definition, inputs.policies, models),
        )
        results.run("capabilities", partial(_nested_model, state, definition))


def _question_prompt(
    state: _Validation, ref: ContentRef, use: ProfileUseContext
) -> None:
    question = state.definition(ref, QuestionDefinition)
    state.prompt_context(
        question.prompt_template_ref, use, question, ModelCapability.OBJECT_OBSERVATION
    )


def _validate_use(
    state: _Validation,
    use: ProfileUseContext,
    tools: tuple[ToolRegistration, ...],
    models: tuple[ModelBindingRegistration, ...],
    prompts: tuple[PromptRegistration, ...],
    used_models: set[tuple[ArtifactKind, str, str, str]],
) -> None:
    results = state.results
    profile = state.definition(use.profile_ref, ProfileDefinition)
    if use.region_ref is not None:
        state.definition(use.region_ref, RegionDefinition)
    for view in use.question_metadata_views:
        state.definition(view.question_ref, QuestionDefinition)
    _condition_uses(state, use, profile, tools)
    results.run("grounding_io", partial(state.grounding, profile, use))
    for question_ref in profile.question_refs:
        results.run(
            "prompt_context", partial(_question_prompt, state, question_ref, use)
        )
    valid_contexts = []
    for index, context in enumerate(use.mapping_contexts):
        if results.run("catalogue", partial(state.mapping_view, profile, use, index)):
            valid_contexts.append(context)

    def plan_checks() -> None:
        plan = state.definition(profile.tool_plan_ref, ToolPlanDefinition)
        results.run(
            "tool_plans",
            partial(
                validate_tool_plan,
                plan,
                tools,
                tuple(valid_contexts),
                state.registry,
                plan_ref=profile.tool_plan_ref,
            ),
        )
        results.run(
            "policies",
            partial(
                validate_step_failure_policies,
                plan,
                state.inputs.policies,
                plan_ref=profile.tool_plan_ref,
            ),
        )

    results.run("tool_plans", plan_checks)
    refs: list[ContentRef] = [
        profile.discovery_binding_ref,
        profile.observation_binding_ref,
    ]
    if isinstance(profile.grounding_binding, ModelGroundingBinding):
        refs.append(profile.grounding_binding.model_binding_ref)
    used_tools = set()
    if isinstance(profile.grounding_binding, ToolGroundingBinding):
        used_tools.add(reference_identity(profile.grounding_binding.tool_ref))

    def plan_tool_refs() -> None:
        plan = state.definition(profile.tool_plan_ref, ToolPlanDefinition)
        used_tools.update(reference_identity(step.tool_ref) for step in plan.steps)

    results.run("profiles", plan_tool_refs)
    refs.extend(
        tool.definition.model_binding_ref
        for tool in tools
        if tool.definition.model_binding_ref is not None
        and reference_identity(tool.ref) in used_tools
    )
    refs.extend(item.binding_ref for item in use.model_prompt_uses)
    seen = set()
    for model_ref in refs:
        key = reference_identity(model_ref)
        if key not in seen:
            seen.add(key)
            used_models.add(key)
            _model_use(state, model_ref, use, models, prompts)


def validate_definition_relations(
    inputs: DefinitionValidationInput, fact_schemas: FactSchemaRegistry
) -> ValidationReport:
    """Validate supplied finite relations, preserving every unperformed boundary."""
    results = _Results()
    try:
        try:
            checked = DefinitionValidationInput.model_validate(inputs)
        except (ValidationError, ValueError, TypeError):
            results.error(
                "catalogue",
                "definitions.input_invalid",
                "Finite input failed strict contract validation.",
            )
            _deferred(results)
            return results.report()
        if not results.run("catalogue", partial(_preflight, checked)):
            _deferred(results)
            return results.report()
        for use in checked.use_contexts:
            for context in use.mapping_contexts:
                results.run(
                    "catalogue", partial(_nested_preflight, checked, use, context)
                )
        state = _Validation(checked, fact_schemas, results)
        tools = tuple(
            ToolRegistration(ref=entry.ref, definition=entry.definition)
            for entry in checked.definitions
            if isinstance(entry, RegisteredTool)
        )
        prompts = tuple(
            PromptRegistration(ref=entry.ref, definition=entry.definition)
            for entry in checked.definitions
            if isinstance(entry, RegisteredPrompt)
        )
        models = tuple(
            ModelBindingRegistration(ref=entry.ref, definition=entry.definition)
            for entry in checked.definitions
            if isinstance(entry, RegisteredModel)
        )
        for entry in checked.definitions:
            _validate_entry(state, entry, models)
        for policy in checked.policies:
            if isinstance(policy.artifact, ApplicabilityPolicyArtifact):
                for selector in policy.artifact.policy.context_allowlist:
                    results.run(
                        "condition_references",
                        partial(_condition_descriptor, state, selector),
                    )
            if isinstance(policy.artifact, ExecutionPolicyArtifact):
                results.run(
                    "policies",
                    partial(
                        validate_execution_policy, policy.artifact, checked.policies
                    ),
                )
            if isinstance(policy.artifact, EvidencePolicyArtifact):
                results.run(
                    "vocabulary",
                    partial(
                        state.vocabulary,
                        policy.artifact.policy.retention_class,
                        "evidence.retention_class",
                    ),
                )
            if isinstance(policy.artifact, ContextProvenancePolicyArtifact):
                if not checked.use_contexts:
                    results.run(
                        "policies",
                        partial(
                            validate_context_derivations,
                            policy.artifact.policy,
                            checked.descriptors,
                            (),
                        ),
                    )
                for use in checked.use_contexts:
                    results.run(
                        "policies",
                        partial(
                            validate_context_derivations,
                            policy.artifact.policy,
                            checked.descriptors,
                            use.context_registrations,
                        ),
                    )
        used_models: set[tuple[ArtifactKind, str, str, str]] = set()
        for use in checked.use_contexts:
            results.run(
                "profiles",
                partial(_validate_use, state, use, tools, models, prompts, used_models),
            )
        active_plans = {
            reference_identity(registration.definition.tool_plan_ref)
            for registration in checked.definitions
            if isinstance(registration.definition, ProfileDefinition)
            and any(
                _same(registration.ref, use.profile_ref) for use in checked.use_contexts
            )
        }
        for registration in checked.definitions:
            if isinstance(registration.definition, ToolPlanDefinition):
                for step in registration.definition.steps:
                    results.run(
                        "policies",
                        partial(
                            state.condition_policy,
                            step.skip_policy_ref,
                            applicability=False,
                        ),
                    )
                if reference_identity(registration.ref) not in active_plans:
                    for step in registration.definition.steps:
                        results.run(
                            "condition_references",
                            partial(_unowned_condition, state, step.skip_policy_ref),
                        )
                    plan_ref = ToolPlanContentRef.model_validate(
                        registration.ref.model_dump(mode="json")
                    )
                    results.run(
                        "tool_plans",
                        partial(
                            validate_tool_plan,
                            registration.definition,
                            tools,
                            (),
                            fact_schemas,
                            plan_ref=plan_ref,
                        ),
                    )
                    results.run(
                        "policies",
                        partial(
                            validate_step_failure_policies,
                            registration.definition,
                            checked.policies,
                            plan_ref=plan_ref,
                        ),
                    )
        for registration in checked.definitions:
            if isinstance(registration.definition, ProfileDefinition) and not any(
                _same(registration.ref, use.profile_ref) for use in checked.use_contexts
            ):
                results.error(
                    "grounding_io",
                    "definitions.profile_use_missing",
                    "Profile grounding and owner-dependent Prompt checks need an explicit use context.",
                    "incomplete",
                )
        for model_entry in models:
            if reference_identity(model_entry.ref) not in used_models:
                _model_use(state, model_entry.ref, None, models, prompts)
        for entry in checked.definitions:
            if isinstance(entry.definition, ToolDefinition):
                for output in entry.definition.fact_outputs:
                    state.reachable_schemas[
                        reference_identity(output.fact_schema_ref)
                    ] = output.fact_schema_ref
        for schema_ref in checked.fact_schema_refs:
            state.reachable_schemas[reference_identity(schema_ref)] = schema_ref
        for reachable_ref in tuple(state.reachable_schemas.values()):
            results.run(
                "fact_schema_metadata", partial(state.schema_metadata, reachable_ref)
            )
        _deferred(results)
        return results.report()
    except _DiagnosticOverflow:
        return results.overflow_report()


def _model_use(
    state: _Validation,
    ref: ContentRef,
    use: ProfileUseContext | None,
    models: tuple[ModelBindingRegistration, ...],
    prompts: tuple[PromptRegistration, ...],
) -> None:
    results, inputs = state.results, state.inputs

    def model_checks() -> None:
        binding = state.definition(ref, ModelBinding)
        for schema in (binding.input_schema_ref, binding.output_schema_ref):
            state.descriptor(schema)
        if binding.prompt_template_ref is not None:
            prompt = state.definition(
                binding.prompt_template_ref, PromptTemplateDefinition
            )
            if not _same(prompt.response_schema_ref, binding.output_schema_ref):
                _fail(
                    "definitions.model_response_schema",
                    "Model Prompt response Schema must match the exact configured output Schema.",
                )
            purpose = binding.capability
            explicit = (
                None
                if use is None
                else next(
                    (
                        item
                        for item in use.model_prompt_uses
                        if _same(item.binding_ref, ref)
                    ),
                    None,
                )
            )
            if purpose is ModelCapability.VISION_AUXILIARY:
                if explicit is None:
                    _fail(
                        "definitions.prompt_purpose_missing",
                        "vision_auxiliary Prompt requires explicit consuming purpose.",
                        "incomplete",
                    )
                purpose = explicit.consuming_purpose
            elif explicit is not None and explicit.consuming_purpose is not purpose:
                _fail(
                    "definitions.prompt_purpose_mismatch",
                    "Declared Model Prompt use conflicts with its capability.",
                )
            if prompt.purpose is not purpose:
                _fail(
                    "definitions.model_prompt_purpose",
                    "Model Prompt purpose differs from the consuming capability.",
                )
            if use is None:
                if state.closure(binding.prompt_template_ref):
                    _fail(
                        "definitions.prompt_owner_unavailable",
                        "Model Prompt variables need explicit owning use context.",
                        "incomplete",
                    )
            else:
                state.prompt_context(binding.prompt_template_ref, use, None, purpose)

    results.run("prompt_context", model_checks)

    def policies() -> None:
        binding = state.definition(ref, ModelBinding)
        if inputs.validation_at is None:
            _fail(
                "definitions.validation_at_missing",
                "Model Policy compatibility requires an explicit validation_at instant.",
                "incomplete",
            )
        explicit = (
            None
            if use is None
            else next(
                (
                    item
                    for item in use.model_prompt_uses
                    if _same(item.binding_ref, ref)
                ),
                None,
            )
        )
        results.merge(
            "policies",
            validate_model_policies(
                binding,
                inputs.policies,
                inputs.reviews,
                inputs.validation_at,
                binding_ref=ModelBindingContentRef.model_validate(
                    ref.model_dump(mode="json")
                ),
                profile_ref=None if use is None else use.profile_ref,
                region_ref=None if use is None else use.region_ref,
                prompts=prompts,
                consuming_purpose=None
                if explicit is None
                else explicit.consuming_purpose,
            ),
        )

    results.run("policies", policies)


def _condition_descriptor(state: _Validation, selector: object) -> None:
    from jimu_runtime.domain.definition_support import ContextFieldSelector

    checked = ContextFieldSelector.model_validate(selector)
    describe_field(
        resolve_descriptor(checked.schema_ref, state.inputs.descriptors),
        checked.field_path,
    )


def _condition_uses(
    state: _Validation,
    use: ProfileUseContext,
    profile: ProfileDefinition,
    tools: tuple[ToolRegistration, ...],
) -> None:
    """Bind actual Policy/owner associations before invoking the finite helper."""
    profiles = tuple(
        entry
        for entry in state.inputs.definitions
        if isinstance(entry, ProfileRegistration)
    )
    questions = tuple(
        AuthorityQuestionInput(ref=entry.ref, definition=entry.definition)
        for entry in state.inputs.definitions
        if isinstance(entry.definition, QuestionDefinition)
    )
    plans = tuple(
        AuthorityToolPlanInput(ref=entry.ref, definition=entry.definition)
        for entry in state.inputs.definitions
        if isinstance(entry.definition, ToolPlanDefinition)
    )
    common: dict[str, object] = dict(
        context_registrations=use.context_registrations,
        descriptors=state.inputs.descriptors,
    )
    if use.region_ref is not None:
        common["region_ref"] = use.region_ref

    def bind(
        ref: PolicyContentRef, context: ConditionReferenceContext, applicability: bool
    ) -> ValidationReport:
        artifact = state.condition_policy(ref, applicability=applicability)
        return validate_condition_references(
            artifact.policy.condition_ast,
            context,
            input_domain=ConditionInputDomain.RULE_APPLICABILITY
            if applicability
            else ConditionInputDomain.TOOL_CONDITION,
            profiles=profiles,
            questions=questions,
            tools=tools,
            plans=plans,
            fact_schemas=state.registry,
            context_allowlist=artifact.policy.context_allowlist
            if isinstance(artifact, ApplicabilityPolicyArtifact)
            else None,
        ).report

    def applicability(ref: ContentRef, question_ref: ContentRef | None = None) -> None:
        ref = PolicyContentRef.model_validate(ref.model_dump())
        raw = dict(
            common,
            use_role="applicability",
            profile_ref=use.profile_ref,
            policy_ref=ref,
        )
        if question_ref is not None:
            raw["question_ref"] = question_ref
        context = ApplicabilityConditionContext.model_validate(raw)
        state.results.run("condition_references", partial(bind, ref, context, True))

    state.results.run(
        "condition_references", partial(applicability, profile.applicability_policy_ref)
    )

    def selection_uses() -> None:
        selection = state.policy(profile.question_selection_policy_ref)
        if not isinstance(selection, QuestionSelectionPolicyArtifact):
            _fail(
                "definitions.policy_subtype",
                "Question selection requires its exact subtype.",
            )
        for rule in selection.policy.context_selection_rules or ():
            state.results.run(
                "condition_references",
                partial(
                    applicability, rule.applicability_policy_ref, rule.question_ref
                ),
            )

    state.results.run("condition_references", selection_uses)

    def tool_uses() -> None:
        plan = state.definition(profile.tool_plan_ref, ToolPlanDefinition)
        for step in plan.steps:
            context = ToolStepGuardConditionContext.model_validate(
                dict(
                    common,
                    use_role="tool_step_guard",
                    profile_ref=use.profile_ref,
                    policy_ref=step.skip_policy_ref,
                    tool_plan_ref=profile.tool_plan_ref,
                    step_id=step.step_id,
                )
            )
            state.results.run(
                "condition_references",
                partial(bind, step.skip_policy_ref, context, False),
            )

    state.results.run("condition_references", tool_uses)


def _unowned_condition(state: _Validation, ref: ContentRef) -> None:
    artifact = state.condition_policy(ref, applicability=False)
    if artifact.policy.condition_ast is True:
        return
    refs = condition_references(artifact.policy.condition_ast)
    if refs.context_selectors or refs.fact_selectors or refs.local_tool_selectors:
        _fail(
            "definitions.condition_use_missing",
            "Condition references require an actual Profile/Plan use environment.",
            "incomplete",
        )
