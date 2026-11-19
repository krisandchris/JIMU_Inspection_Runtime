"""Derive producer identities and prove availability at static Bundle use sites."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, ValidationError, model_validator

from jimu_runtime.bundle.artifacts import DRAFT_2020_12, BundleArtifactError
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.bundle.integrity import canonical_json, sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import LiteralExpr
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ProducerCardinality,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    ContextSchemaRef,
    immutable_sequence,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.tools import (
    ToolDefinition,
    ToolPlanDefinition,
    plan_ancestors,
)
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    ProducerCatalogueContentRef,
    QuestionContentRef,
    QuestionProducerSelector,
    SystemProducerSelector,
    ToolPlanContentRef,
    ToolProducerSelector,
)
from jimu_runtime.domain.policies import (
    ApplicabilityPolicy,
    QuestionSelectionPolicy,
    RulePolicy,
    ToolSkipPolicy,
)
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.producers import (
    AlwaysActivation,
    FactProducerDeclaration,
    ProducerCatalogue,
    QuestionProducer,
    SystemProducer,
    ToolConditionActivation,
    ToolStepProducer,
    declaration_collision_key,
)
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.rules import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.tool_mappings import (
    ContextTypeRegistration,
    FactInput,
    ProfileContentRef,
    RegionContentRef,
    RegionContextRegistration,
    SnapshotContextRegistration,
    StepOutputInput,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ApplicabilityConditionContext,
    ProducerActivationConditionContext,
    ToolStepGuardConditionContext,
)
from jimu_runtime.domain.validation.definition_inputs import (
    DefinitionValidationInput,
    ProfileRegistration,
    ProfileUseContext,
)
from jimu_runtime.domain.validation.issues import IssuePath
from jimu_runtime.domain.validation.policies import PolicyRegistration
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueRegistration,
    ProducerValidationContext,
    SystemCapabilityRegistration,
)
from jimu_runtime.domain.validation.producers import (
    requirements_agree,
    validate_producer_catalogue,
)
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleConditionContext,
    RuleSetTypeRegistration,
    RuleTypeBindings,
    RuleTypeUseContext,
)
from jimu_runtime.domain.validation.rule_types import check_condition_types
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import BooleanValue

_CATALOGUE_PATH = "facts/producers.json"
_MAX_DIRECTORY = 1024


def _check_declaration_budget(count: int) -> None:
    if count > _MAX_DIRECTORY:
        raise BundleArtifactError(
            "producer.catalogue_budget",
            "Producer catalogue exceeds its declaration bound.",
            _CATALOGUE_PATH,
        )


class PriorToolUse(ContractModel):
    """An exact N05 location where the trusted system output is already available."""

    profile_ref: ProfileContentRef
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId


class SystemProducerGrant(ContractModel):
    """Deployment-owned grant for one exact system Fact declaration."""

    declaration: FactProducerDeclaration
    available_before_tool_steps: Annotated[
        tuple[PriorToolUse, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ] = ()

    @model_validator(mode="after")
    def require_exact_system_grant(self) -> Self:
        if not isinstance(self.declaration.producer, SystemProducer):
            raise ValueError("grant must authorize a system producer")
        if len(set(self.available_before_tool_steps)) != len(
            self.available_before_tool_steps
        ):
            raise ValueError("grant must use distinct Tool use locations")
        return self


def _typed[T](catalogue: ArtifactCatalogue, ref: ContentRef, model: type[T]) -> T:
    entry = catalogue.resolve(ref)
    if not isinstance(entry.value, model):
        raise _error("producer.definition_type", "Unexpected artifact contract.", entry)
    return entry.value


def _error(
    code: str,
    message: str,
    entry: ResolvedArtifact,
    location: IssuePath = (),
) -> BundleArtifactError:
    prefix = (entry.inventory.json_pointer,) if entry.inventory.json_pointer else ()
    return BundleArtifactError(code, message, entry.inventory.path, prefix + location)


def _policy[T](
    policies: dict[tuple[object, ...], PolicyRegistration],
    ref: ContentRef,
    subtype: type[T],
    owner: ResolvedArtifact,
) -> T:
    registered = policies.get(reference_identity(ref))
    if registered is None or not isinstance(registered.artifact.policy, subtype):
        raise _error(
            "producer.policy_subtype",
            "Exact policy of the required subtype is unavailable.",
            owner,
        )
    return registered.artifact.policy


def _registration[T: ContractModel](
    kind: type[T],
    entry: ResolvedArtifact,
    *,
    value_field: Literal["artifact", "definition"] = "definition",
    registration_kind: str | None = None,
) -> T:
    fields: dict[str, object] = {
        "ref": entry.ref.model_dump(),
        value_field: entry.value,
    }
    if registration_kind is not None:
        fields["registration_kind"] = registration_kind
    return kind.model_validate(fields)


def _policies(
    registrations: tuple[PolicyRegistration, ...],
) -> dict[tuple[object, ...], PolicyRegistration]:
    return {reference_identity(item.ref): item for item in registrations}


def _literal_bool(condition: object) -> bool | None:
    if condition is True:
        return True
    if isinstance(condition, LiteralExpr) and isinstance(
        condition.constant.value, BooleanValue
    ):
        return condition.constant.value.value
    return None


def _declaration_id(
    profile: ProfileContentRef,
    producer: QuestionProducer | ToolStepProducer,
    output_name: str,
) -> str:
    data: list[JSONValue] = [
        profile.model_dump(mode="json"),
        producer.model_dump(mode="json"),
        output_name,
    ]
    slot_digest = sha256_digest(canonical_json(data, max_bytes=8192, max_depth=8))
    return f"{producer.producer_kind.value}.{slot_digest}"


def derive_producer_catalogue(
    catalogue: ArtifactCatalogue,
    *,
    catalogue_id: ArtifactId,
    version: Version,
    schema_version: Literal["1.0.0"],
    system_declarations: tuple[FactProducerDeclaration, ...],
) -> ProducerCatalogue:
    """Build the canonical declaration sequence from exact artefact bytes and explicit systems."""
    policy_registrations = tuple(
        _registration(PolicyRegistration, item, value_field="artifact")
        for item in catalogue.of_kind(ArtifactKind.POLICY)
    )
    policies = _policies(policy_registrations)
    _check_declaration_budget(len(system_declarations))
    declarations = list(system_declarations)
    if any(not isinstance(item.producer, SystemProducer) for item in declarations):
        raise BundleArtifactError(
            "producer.non_system_source",
            "Only system declarations can be supplied.",
            _CATALOGUE_PATH,
        )
    for entry in catalogue.of_kind(ArtifactKind.PROFILE):
        profile_ref = ProfileContentRef.model_validate(entry.ref.model_dump())
        profile = _typed(catalogue, profile_ref, ProfileDefinition)
        _check_declaration_budget(len(declarations) + len(profile.question_refs))
        for question_ref in profile.question_refs:
            question = _typed(catalogue, question_ref, QuestionDefinition)
            question_producer = QuestionProducer(
                producer_kind=ProducerKind.VLM_QUESTION, question_ref=question_ref
            )
            declarations.append(
                FactProducerDeclaration(
                    declaration_id=_declaration_id(
                        profile_ref, question_producer, "primary"
                    ),
                    scope=FactScope.OBJECT,
                    producer_kind=ProducerKind.VLM_QUESTION,
                    producer_id=question_ref.artifact_id,
                    producer_version=question_ref.version,
                    producer=question_producer,
                    output_name="primary",
                    fact_key=question.primary_fact_key,
                    fact_schema_ref=question.primary_fact_schema_ref,
                    cardinality=ProducerCardinality.EXACTLY_ONE,
                    activation_condition=AlwaysActivation(mode="always"),
                    profile_ref=profile_ref,
                )
            )
        plan = _typed(catalogue, profile.tool_plan_ref, ToolPlanDefinition)
        plan_entry = catalogue.resolve(profile.tool_plan_ref)
        for step in plan.steps:
            skip = _policy(policies, step.skip_policy_ref, ToolSkipPolicy, plan_entry)
            activation = (
                AlwaysActivation(mode="always")
                if skip.condition_ast is True
                or _literal_bool(skip.condition_ast) is True
                else ToolConditionActivation(
                    mode="tool_condition", condition_ast=skip.condition_ast
                )
            )
            tool = _typed(catalogue, step.tool_ref, ToolDefinition)
            outputs = {item.output_name: item for item in tool.fact_outputs}
            _check_declaration_budget(
                len(declarations) + len(step.fact_output_mappings)
            )
            for mapping in step.fact_output_mappings:
                output = outputs.get(mapping.output_name)
                if (
                    output is None
                    or output.fact_key != mapping.target_fact_key
                    or output.scope is not FactScope.OBJECT
                ):
                    raise _error(
                        "producer.output_mapping",
                        "A step output must map to the declared object-scoped Tool output and key.",
                        plan_entry,
                        (
                            "steps",
                            step.step_id,
                            "fact_output_mappings",
                            mapping.output_name,
                        ),
                    )
                tool_producer = ToolStepProducer(
                    producer_kind=ProducerKind.TOOL,
                    tool_plan_ref=profile.tool_plan_ref,
                    step_id=step.step_id,
                    tool_ref=step.tool_ref,
                )
                declarations.append(
                    FactProducerDeclaration(
                        declaration_id=_declaration_id(
                            profile_ref, tool_producer, output.output_name
                        ),
                        scope=output.scope,
                        producer_kind=ProducerKind.TOOL,
                        producer_id=step.tool_ref.artifact_id,
                        producer_version=step.tool_ref.version,
                        producer=tool_producer,
                        output_name=output.output_name,
                        fact_key=mapping.target_fact_key,
                        fact_schema_ref=output.fact_schema_ref,
                        cardinality=(
                            ProducerCardinality.EXACTLY_ONE
                            if output.required
                            else ProducerCardinality.ZERO_OR_ONE
                        ),
                        activation_condition=activation,
                        profile_ref=profile_ref,
                    )
                )
    try:
        return ProducerCatalogue(
            schema_version=schema_version,
            catalogue_id=catalogue_id,
            version=version,
            declarations=tuple(
                sorted(declarations, key=lambda item: item.declaration_id)
            ),
        )
    except ValidationError as exc:
        problem = exc.errors(
            include_input=False, include_context=False, include_url=False
        )[0]
        raise BundleArtifactError(
            "producer.catalogue_contract",
            problem["msg"],
            _CATALOGUE_PATH,
            tuple(problem["loc"]),
        ) from None


@dataclass(frozen=True, slots=True)
class _ProducerInputs:
    bindings: RuleTypeBindings
    capabilities: tuple[SystemCapabilityRegistration, ...]
    prior_system_outputs: dict[PriorToolUse, tuple[ArtifactId, ...]]


def _system_authority(
    catalogue: ArtifactCatalogue,
    entry: ResolvedArtifact,
    declarations: tuple[FactProducerDeclaration, ...],
    grants: tuple[SystemProducerGrant, ...],
) -> dict[PriorToolUse, tuple[ArtifactId, ...]]:
    by_id: dict[ArtifactId, SystemProducerGrant] = {}
    for original in grants:
        grant = SystemProducerGrant.model_validate(original)
        declaration = grant.declaration
        if declaration.declaration_id in by_id:
            raise _error(
                "producer.system_grant",
                "Ambiguous or invalid system producer authorization.",
                entry,
            )
        by_id[declaration.declaration_id] = grant

    prior: dict[PriorToolUse, list[ArtifactId]] = {}
    for declaration in declarations:
        producer = declaration.producer
        assert isinstance(producer, SystemProducer)
        matched = by_id.get(declaration.declaration_id)
        if (
            matched is None
            or matched.declaration.stable_json() != declaration.stable_json()
        ):
            raise _error(
                "producer.system_unauthorized",
                "The system output declaration has no exact deployment grant.",
                entry,
                ("declarations", declaration.declaration_id),
            )
        if not any(
            requirements_agree(producer.requirement, requirement)
            for requirement in catalogue.required_capabilities
        ):
            raise _error(
                "producer.system_requirement",
                "The system output capability has no equivalent manifest requirement.",
                entry,
                ("declarations", declaration.declaration_id),
            )
        for use in matched.available_before_tool_steps:
            profile = _typed(catalogue, use.profile_ref, ProfileDefinition)
            if (
                profile.tool_plan_ref != use.tool_plan_ref
                or (
                    declaration.profile_ref is not None
                    and declaration.profile_ref != use.profile_ref
                )
                or not any(
                    step.step_id == use.step_id
                    for step in _typed(
                        catalogue, use.tool_plan_ref, ToolPlanDefinition
                    ).steps
                )
            ):
                raise _error(
                    "producer.system_prior_scope",
                    "System prior availability must target its exact allowed Profile/Plan/step.",
                    entry,
                    ("declarations", declaration.declaration_id),
                )
            prior.setdefault(use, []).append(declaration.declaration_id)
    return {use: tuple(sorted(ids)) for use, ids in prior.items()}


def _check_type(
    condition: object,
    domain: ConditionInputDomain,
    context: RuleTypeUseContext,
    bindings: RuleTypeBindings,
    fact_schemas: FactSchemaRegistry,
    owner: ResolvedArtifact,
) -> None:
    result = check_condition_types(
        condition,
        input_domain=domain,
        context=context,
        bindings=bindings,
        fact_schemas=fact_schemas,
    )
    if result.status != "passed":
        issue = result.issues[0]
        raise _error(issue.code, issue.message, owner, issue.path)


def _type_bindings(
    catalogue: ArtifactCatalogue,
    registration: ProducerCatalogueRegistration,
) -> RuleTypeBindings:
    profiles = tuple(
        _registration(ProfileRegistration, entry, registration_kind="profile")
        for entry in catalogue.of_kind(ArtifactKind.PROFILE)
    )
    questions = tuple(
        _registration(AuthorityQuestionInput, entry)
        for entry in catalogue.of_kind(ArtifactKind.QUESTION)
    )
    plans = tuple(
        _registration(AuthorityToolPlanInput, entry)
        for entry in catalogue.of_kind(ArtifactKind.TOOL_PLAN)
    )
    tools = tuple(
        _registration(ToolRegistration, entry)
        for entry in catalogue.of_kind(ArtifactKind.TOOL)
    )
    return RuleTypeBindings(
        profiles=profiles,
        questions=questions,
        plans=plans,
        tools=tools,
        policies=tuple(
            _registration(PolicyRegistration, entry, value_field="artifact")
            for entry in catalogue.of_kind(ArtifactKind.POLICY)
        ),
        producer_catalogue=registration,
    )


def _execution_contexts(
    catalogue: ArtifactCatalogue,
    uses: tuple[ProfileUseContext, ...],
) -> Iterator[tuple[RegionContentRef | None, tuple[ContextTypeRegistration, ...]]]:
    # The Snapshot schema is common to the current execution policy. Region uses
    # here cover the inventory, including Regions outside all Profile gates.
    snapshots = {
        value
        for use in uses
        for value in use.context_registrations
        if isinstance(value, SnapshotContextRegistration)
    }
    if len(snapshots) > 1:
        raise BundleArtifactError(
            "producer.context_snapshot_scope",
            "Execution Producer activation requires one exact Snapshot context contract.",
            _CATALOGUE_PATH,
        )
    common: tuple[ContextTypeRegistration, ...] = tuple(snapshots)
    regions = catalogue.of_kind(ArtifactKind.REGION)
    if not regions:
        yield None, common
    for entry in regions:
        region_ref = RegionContentRef.model_validate(entry.ref.model_dump())
        region = _typed(catalogue, entry.ref, RegionDefinition)
        schema_ref = ContextSchemaRef.model_validate(
            {**region.context_schema_ref.model_dump(), "schema_dialect": DRAFT_2020_12}
        )
        yield (
            region_ref,
            (
                *common,
                RegionContextRegistration(
                    source="region_metadata",
                    region_ref=region_ref,
                    schema_ref=schema_ref,
                ),
            ),
        )


def _activation_contexts(
    catalogue: ArtifactCatalogue,
    registration: ProducerCatalogueRegistration,
    definitions: DefinitionValidationInput,
) -> tuple[ProducerActivationConditionContext, ...]:
    uses = definitions.use_contexts
    contexts: list[ProducerActivationConditionContext] = []
    execution_uses = None
    for item in registration.definition.declarations:
        if isinstance(item.activation_condition, AlwaysActivation):
            continue
        producer = item.producer
        if item.profile_ref is None:
            if execution_uses is None:
                execution_uses = tuple(_execution_contexts(catalogue, uses))
            positions = execution_uses
        else:
            positions = tuple(
                (use.region_ref, use.context_registrations)
                for use in uses
                if item.profile_ref == use.profile_ref
            )
        if len(contexts) + len(positions) > _MAX_DIRECTORY:
            raise BundleArtifactError(
                "producer.activation_use_budget",
                "Producer condition uses exceed the bounded validation directory.",
                _CATALOGUE_PATH,
            )
        for region, registrations in positions:
            fields: dict[str, object] = {}
            if item.profile_ref is not None:
                fields["profile_ref"] = item.profile_ref
            if region is not None:
                fields["region_ref"] = region
            if isinstance(producer, ToolStepProducer):
                fields["tool_plan_ref"] = producer.tool_plan_ref
                fields["step_id"] = producer.step_id
            contexts.append(
                ProducerActivationConditionContext.model_validate(
                    {
                        "use_role": "producer_activation",
                        "producer_catalogue_ref": registration.ref,
                        "declaration_id": item.declaration_id,
                        "producer_kind": item.producer_kind,
                        "producer_scope": item.scope,
                        "context_registrations": registrations,
                        "descriptors": definitions.descriptors,
                        **fields,
                    }
                )
            )
    return tuple(contexts)


def _at_step(
    bindings: RuleTypeBindings, prior: tuple[ArtifactId, ...]
) -> RuleTypeBindings:
    if not prior:
        return bindings
    return bindings.model_copy(update={"prior_producer_declaration_ids": prior})


def _condition_location(
    use: ProfileUseContext,
    definitions: DefinitionValidationInput,
) -> dict[str, object]:
    fields: dict[str, object] = {
        "profile_ref": use.profile_ref,
        "context_registrations": use.context_registrations,
        "descriptors": definitions.descriptors,
    }
    if use.region_ref is not None:
        fields["region_ref"] = use.region_ref
    return fields


def _check_producer_catalogue(
    catalogue: ArtifactCatalogue,
    entry: ResolvedArtifact,
    registration: ProducerCatalogueRegistration,
    definitions: DefinitionValidationInput,
    inputs: _ProducerInputs,
    fact_schemas: FactSchemaRegistry,
) -> None:
    bindings = inputs.bindings
    contexts = _activation_contexts(catalogue, registration, definitions)
    report = validate_producer_catalogue(
        registration,
        ProducerValidationContext(
            profiles=bindings.profiles,
            questions=bindings.questions,
            plans=bindings.plans,
            tools=bindings.tools,
            policies=bindings.policies,
            system_capabilities=inputs.capabilities,
            condition_contexts=contexts,
        ),
        fact_schemas=fact_schemas,
        require_all_system_versions=False,
    )
    for check in report.checks:
        if check.status in ("invalid", "incomplete"):
            issue = check.issues[0]
            raise _error(issue.code, issue.message, entry, issue.path)

    declarations = {
        item.declaration_id: item for item in registration.definition.declarations
    }
    for context in contexts:
        activation = declarations[context.declaration_id].activation_condition
        assert isinstance(activation, ToolConditionActivation)
        prior: tuple[ArtifactId, ...] = ()
        if context.profile_ref and context.tool_plan_ref and context.step_id:
            prior = inputs.prior_system_outputs.get(
                PriorToolUse(
                    profile_ref=context.profile_ref,
                    tool_plan_ref=context.tool_plan_ref,
                    step_id=context.step_id,
                ),
                (),
            )
        _check_type(
            activation.condition_ast,
            ConditionInputDomain.TOOL_CONDITION,
            context,
            _at_step(bindings, prior),
            fact_schemas,
            entry,
        )


def _resolve_input_producer(
    selector: FactSelector,
    profile: ProfileContentRef,
    slots: dict[tuple[object, ...], FactProducerDeclaration],
    owner: ResolvedArtifact,
    location: IssuePath,
) -> FactProducerDeclaration:
    scope = reference_identity(profile) if selector.scope is FactScope.OBJECT else None
    declaration = slots.get((selector.scope, scope, selector.fact_key))
    if declaration is None:
        raise _error(
            "producer.selector_unavailable",
            "No producer owns this Fact key in its exact scope.",
            owner,
            location,
        )
    producer, selected = declaration.producer, selector.producer_selector
    match = False
    if isinstance(producer, QuestionProducer) and isinstance(
        selected, QuestionProducerSelector
    ):
        match = producer.question_ref == selected.question_ref
    elif isinstance(producer, ToolStepProducer) and isinstance(
        selected, ToolProducerSelector
    ):
        match = (
            producer.tool_plan_ref == selected.tool_plan_ref
            and producer.step_id == selected.step_id
            and producer.tool_ref == selected.tool_ref
        )
    elif isinstance(producer, SystemProducer) and isinstance(
        selected, SystemProducerSelector
    ):
        match = requirements_agree(producer.requirement, selected.requirement)
    if (
        not match
        or declaration.output_name != selected.output_name
        or declaration.fact_schema_ref != selector.fact_schema_ref
    ):
        raise _error(
            "producer.selector_mismatch",
            "The selector conflicts with the exact producer slot or schema.",
            owner,
            location,
        )
    return declaration


@dataclass(frozen=True, slots=True)
class _Availability:
    profile_ref: ProfileContentRef
    mandatory_questions: frozenset[tuple[object, ...]]
    guaranteed_steps: frozenset[str]
    slots: dict[tuple[object, ...], FactProducerDeclaration]

    def require(
        self,
        selector: FactSelector,
        owner: ResolvedArtifact,
        location: IssuePath,
        *,
        prior_system_outputs: tuple[ArtifactId, ...] | None = None,
    ) -> None:
        declaration = _resolve_input_producer(
            selector, self.profile_ref, self.slots, owner, location
        )
        producer = declaration.producer
        available = (
            isinstance(declaration.activation_condition, AlwaysActivation)
            and declaration.cardinality is ProducerCardinality.EXACTLY_ONE
        )
        if isinstance(producer, QuestionProducer):
            available &= (
                reference_identity(producer.question_ref) in self.mandatory_questions
            )
        elif isinstance(producer, ToolStepProducer):
            available &= producer.step_id in self.guaranteed_steps
        elif prior_system_outputs is not None:
            available &= declaration.declaration_id in prior_system_outputs
        if not available:
            raise _error(
                "producer.required_unavailable",
                "Required dependency has no guaranteed producer in this use. Check selection, cardinality, activation and scheduling.",
                owner,
                location,
            )


def _validate_tool_uses(
    catalogue: ArtifactCatalogue,
    definitions: DefinitionValidationInput,
    use: ProfileUseContext,
    profile: ProfileDefinition,
    selection: QuestionSelectionPolicy,
    inputs: _ProducerInputs,
    fact_schemas: FactSchemaRegistry,
) -> _Availability:
    plan_entry = catalogue.resolve(profile.tool_plan_ref)
    plan = _typed(catalogue, profile.tool_plan_ref, ToolPlanDefinition)
    policies = _policies(inputs.bindings.policies)
    registration = inputs.bindings.producer_catalogue
    assert registration is not None
    slots = {
        declaration_collision_key(item): item
        for item in registration.definition.declarations
    }
    ancestors = plan_ancestors(plan.steps)
    guards = {
        step.step_id: _policy(
            policies, step.skip_policy_ref, ToolSkipPolicy, plan_entry
        )
        for step in plan.steps
    }
    unconditional = {
        step.step_id
        for step in plan.steps
        if step.requirement == "required"
        and _literal_bool(guards[step.step_id].condition_ast) is True
    }
    availability = _Availability(
        use.profile_ref,
        frozenset(reference_identity(ref) for ref in selection.mandatory_question_refs),
        frozenset(step for step in unconditional if ancestors[step] <= unconditional),
        slots,
    )
    for step in plan.steps:
        prior = inputs.prior_system_outputs.get(
            PriorToolUse(
                profile_ref=use.profile_ref,
                tool_plan_ref=profile.tool_plan_ref,
                step_id=step.step_id,
            ),
            (),
        )
        fields = _condition_location(use, definitions)
        context = ToolStepGuardConditionContext.model_validate(
            {
                "use_role": "tool_step_guard",
                "policy_ref": step.skip_policy_ref,
                "tool_plan_ref": profile.tool_plan_ref,
                "step_id": step.step_id,
                **fields,
            }
        )
        _check_type(
            guards[step.step_id].condition_ast,
            ConditionInputDomain.TOOL_CONDITION,
            context,
            _at_step(inputs.bindings, prior),
            fact_schemas,
            catalogue.resolve(step.skip_policy_ref),
        )
        for index, binding in enumerate(step.input_bindings):
            location = ("steps", step.step_id, "input_bindings", index)
            source = binding.source
            if isinstance(source, FactInput):
                declaration = _resolve_input_producer(
                    source.selector, use.profile_ref, slots, plan_entry, location
                )
                producer = declaration.producer
                if isinstance(producer, ToolStepProducer) and (
                    producer.tool_plan_ref != profile.tool_plan_ref
                    or producer.step_id not in ancestors[step.step_id]
                ):
                    raise _error(
                        "producer.tool_input_ancestor",
                        "Tool Fact input must use a strict ancestor of this exact ToolPlan.",
                        plan_entry,
                        location,
                    )
                if (
                    isinstance(producer, SystemProducer)
                    and declaration.declaration_id not in prior
                ):
                    raise _error(
                        "producer.tool_input_prior",
                        "System Fact input requires an exact prior-availability grant for this step.",
                        plan_entry,
                        location,
                    )
                if binding.required:
                    availability.require(
                        source.selector,
                        plan_entry,
                        location,
                        prior_system_outputs=prior,
                    )
            elif (
                isinstance(source, StepOutputInput)
                and binding.required
                and source.step_id not in availability.guaranteed_steps
            ):
                raise _error(
                    "producer.required_step_unavailable",
                    "Required step output depends on a conditional or optional step.",
                    plan_entry,
                    location,
                )
    return availability


def _applicability(
    catalogue: ArtifactCatalogue,
    policy_ref: ContentRef,
    definitions: DefinitionValidationInput,
    use: ProfileUseContext,
    inputs: _ProducerInputs,
    fact_schemas: FactSchemaRegistry,
    *,
    question_ref: QuestionContentRef | None = None,
) -> bool | None:
    owner = catalogue.resolve(policy_ref)
    policy = _policy(
        _policies(inputs.bindings.policies), policy_ref, ApplicabilityPolicy, owner
    )
    for ref, kind in (
        (use.profile_ref, ArtifactKind.PROFILE),
        (use.region_ref, ArtifactKind.REGION),
    ):
        selectors = [
            item for item in policy.identity_constraints if item.artifact_kind is kind
        ]
        if selectors and (
            ref is None
            or not any(
                reference_identity(catalogue.resolve_selector(item).ref)
                == reference_identity(ref)
                for item in selectors
            )
        ):
            return None
    fields = _condition_location(use, definitions)
    if question_ref is not None:
        fields["question_ref"] = question_ref
    context = ApplicabilityConditionContext.model_validate(
        {
            "use_role": "applicability",
            "policy_ref": policy_ref.model_dump(),
            **fields,
        }
    )
    _check_type(
        policy.condition_ast,
        ConditionInputDomain.RULE_APPLICABILITY,
        context,
        inputs.bindings,
        fact_schemas,
        owner,
    )
    # No runtime context value is asserted. A literal FALSE is a proof of exclusion.
    return _literal_bool(policy.condition_ast) is not False


def _validate_rule_uses(
    catalogue: ArtifactCatalogue,
    definitions: DefinitionValidationInput,
    use: ProfileUseContext,
    profile: ProfileDefinition,
    inputs: _ProducerInputs,
    fact_schemas: FactSchemaRegistry,
    availability: _Availability,
    checked_uses: set[tuple[ArtifactKind, str, str, str]],
) -> None:
    profile_applies = _applicability(
        catalogue,
        profile.applicability_policy_ref,
        definitions,
        use,
        inputs,
        fact_schemas,
    )
    if profile_applies is None:
        return
    policies = _policies(inputs.bindings.policies)
    for rule_set_ref in profile.rule_set_refs:
        rule_set = _typed(catalogue, rule_set_ref, RuleSetDefinition)
        rule_set_applies = _applicability(
            catalogue,
            rule_set.applicability_policy_ref,
            definitions,
            use,
            inputs,
            fact_schemas,
        )
        if rule_set_applies is None:
            continue
        checked_uses.add(reference_identity(rule_set_ref))
        registration = RuleSetTypeRegistration(ref=rule_set_ref, definition=rule_set)
        for rule_ref in rule_set.rule_refs:
            rule = _typed(catalogue, rule_ref, RuleDefinition)
            owner = catalogue.resolve(rule_ref)
            rule_applies = _applicability(
                catalogue,
                rule.applicability_policy_ref,
                definitions,
                use,
                inputs,
                fact_schemas,
            )
            if rule_applies is None:
                continue
            context = RuleConditionContext.model_validate(
                {
                    "use_role": "rule_condition",
                    "rule_ref": rule_ref,
                    "rule_set": registration,
                    **_condition_location(use, definitions),
                }
            )
            _check_type(
                rule,
                ConditionInputDomain.RULE_CONDITION,
                context,
                inputs.bindings,
                fact_schemas,
                owner,
            )
            checked_uses.add(reference_identity(rule_ref))
            policy = _policy(policies, rule.rule_policy_ref, RulePolicy, owner)
            if (
                policy.mandatory
                and profile_applies
                and rule_set_applies
                and rule_applies
            ):
                for index, selector in enumerate(rule.required_fact_selectors):
                    availability.require(
                        selector, owner, ("required_fact_selectors", index)
                    )


def validate_producer_rules(
    catalogue: ArtifactCatalogue,
    definitions: DefinitionValidationInput,
    *,
    fact_schemas: FactSchemaRegistry,
    system_capabilities: tuple[SystemCapabilityRegistration, ...],
    system_grants: tuple[SystemProducerGrant, ...],
    max_bytes: int,
    max_depth: int,
) -> None:
    """Recompute the final catalogue and verify producer and predicate uses.

    Definition input describes contexts already derived by the Bundle semantic
    resolver from pinned artifact bytes. System grants and registered versions
    come from the trusted deployment, independently of the Bundle.
    """
    entries = catalogue.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    if len(entries) != 1:
        raise BundleArtifactError(
            "producer.catalogue_count",
            "Bundle must contain exactly one ProducerCatalogue.",
            _CATALOGUE_PATH,
        )
    entry = entries[0]
    actual = _typed(catalogue, entry.ref, ProducerCatalogue)
    systems = tuple(
        item
        for item in actual.declarations
        if isinstance(item.producer, SystemProducer)
    )
    if len(system_grants) > _MAX_DIRECTORY:
        raise _error(
            "producer.system_grant_budget",
            "System grant input exceeds its bounded directory.",
            entry,
        )
    try:
        prior_outputs = _system_authority(catalogue, entry, systems, system_grants)
        expected = derive_producer_catalogue(
            catalogue,
            catalogue_id=actual.catalogue_id,
            version=actual.version,
            schema_version=actual.schema_version,
            system_declarations=systems,
        )
        if (
            canonical_json(
                expected.model_dump(mode="json"),
                max_bytes=max_bytes,
                max_depth=max_depth,
                path=entry.inventory.path,
            )
            != entry.canonical_bytes
        ):
            raise _error(
                "producer.catalogue_mismatch",
                "Catalogue bytes differ from independent derivation.",
                entry,
            )
        registration = ProducerCatalogueRegistration(
            ref=ProducerCatalogueContentRef.model_validate(entry.ref.model_dump()),
            definition=expected,
        )
        inputs = _ProducerInputs(
            _type_bindings(catalogue, registration),
            system_capabilities,
            prior_outputs,
        )
        if {use.profile_ref for use in definitions.use_contexts} != {
            item.ref for item in inputs.bindings.profiles
        }:
            raise _error(
                "producer.profile_use_contexts",
                "Every exact Profile needs a derived semantic use context.",
                entry,
            )
        _check_producer_catalogue(
            catalogue,
            entry,
            registration,
            definitions,
            inputs,
            fact_schemas,
        )
        policies = _policies(inputs.bindings.policies)
        checked_uses: set[tuple[ArtifactKind, str, str, str]] = set()
        for use in definitions.use_contexts:
            profile = _typed(catalogue, use.profile_ref, ProfileDefinition)
            selection = _policy(
                policies,
                profile.question_selection_policy_ref,
                QuestionSelectionPolicy,
                catalogue.resolve(use.profile_ref),
            )
            for selector in selection.context_selection_rules or ():
                _applicability(
                    catalogue,
                    selector.applicability_policy_ref,
                    definitions,
                    use,
                    inputs,
                    fact_schemas,
                    question_ref=selector.question_ref,
                )
            availability = _validate_tool_uses(
                catalogue,
                definitions,
                use,
                profile,
                selection,
                inputs,
                fact_schemas,
            )
            _validate_rule_uses(
                catalogue,
                definitions,
                use,
                profile,
                inputs,
                fact_schemas,
                availability,
                checked_uses,
            )
        for kind, name in (
            (ArtifactKind.RULE_SET, "rule_set"),
            (ArtifactKind.RULE, "rule"),
        ):
            for item in catalogue.of_kind(kind):
                if reference_identity(item.ref) not in checked_uses:
                    raise _error(
                        f"producer.{name}_unbound_use",
                        "No exact Profile and Region use establishes this definition's condition types.",
                        item,
                    )
    except ValidationError as exc:
        problem = exc.errors(
            include_input=False, include_context=False, include_url=False
        )[0]
        raise _error(
            "producer.validation_contract",
            "Producer or Rule validation inputs violate their bounded contracts.",
            entry,
            tuple(problem["loc"]),
        ) from None
