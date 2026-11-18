"""Finite Producer conformance; no predicate typing, evaluation or authority view."""

import struct
from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.conditions import (
    ConditionExpr,
    LiteralExpr,
    condition_references,
)
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ProducerCardinality,
)
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    reference_identity,
)
from jimu_runtime.domain.fact_schemas.models import (
    ForbiddenConfidenceMetadataPolicy,
    RequiredConfidenceMetadataPolicy,
)
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    QuestionProducerSelector,
    SystemProducerSelector,
    ToolProducerSelector,
)
from jimu_runtime.domain.policies import ToolSkipPolicy
from jimu_runtime.domain.producers import (
    AlwaysActivation,
    FactProducerDeclaration,
    QuestionProducer,
    SystemProducer,
    ToolConditionActivation,
    ToolStepProducer,
    producer_output_slot,
)
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.facts import (
    FactRecord,
    KnownFact,
    QuestionFactSource,
    SystemFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.validation.conditions import validate_condition_references
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    CheckStatus,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.producer_inputs import (
    ProducerCatalogueRegistration,
    ProducerValidationContext,
    SystemCapabilityRegistration,
)
from jimu_runtime.domain.values import BooleanValue


def _same_data(left: object, right: object) -> bool:
    if isinstance(left, BaseModel) and isinstance(right, BaseModel):
        return _same_data(
            left.model_dump(mode="python"), right.model_dump(mode="python")
        )
    if type(left) is not type(right):
        return False
    if isinstance(left, float) and isinstance(right, float):
        return struct.pack("!d", left) == struct.pack("!d", right)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _same_data(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _same_data(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def _requirement_identity(value: ExternalCapabilityRequirement) -> tuple[object, ...]:
    return value.family, value.capability_id, value.contract_version


def requirements_agree(
    left: ExternalCapabilityRequirement, right: ExternalCapabilityRequirement
) -> bool:
    """Constraint set equivalence, without altering configuration or candidate bytes."""
    return (
        _requirement_identity(left) == _requirement_identity(right)
        and set(left.allowed_implementation_versions)
        == set(right.allowed_implementation_versions)
        and set(left.required_features) == set(right.required_features)
        and _same_data(left.evaluation_ref, right.evaluation_ref)
    )


class _Checks:
    names = (
        "producer.input",
        "producer.identities",
        "producer.bindings",
        "producer.completeness",
        "producer.schema",
        "producer.conditions",
        "producer.selector_binding",
    )

    def __init__(self) -> None:
        self.performed: set[str] = set()
        self.problems: dict[str, list[tuple[CheckStatus, ValidationIssue]]] = {}

    def add(
        self,
        check: str,
        status: CheckStatus,
        code: str,
        message: str,
        path: tuple[str | int, ...] = (),
    ) -> None:
        if len(path) > 32 or any(
            isinstance(part, str) and not 1 <= len(part) <= 128 for part in path
        ):
            path = ()
            message = (
                "Root location: diagnostic exceeds supported path domain. " + message
            )
        entries = self.problems.setdefault(check, [])
        problem = ValidationIssue(
            code=code, path=path, message=message, related_refs=()
        )
        if len(entries) < 64:
            entries.append((status, problem))
        elif entries[-1][1].code != "producer.diagnostic_overflow":
            entries[-1] = (
                "invalid",
                ValidationIssue(
                    code="producer.diagnostic_overflow",
                    path=(),
                    message="More than64 diagnostics; retained prefix is not exhaustive.",
                    related_refs=(),
                ),
            )

    def report(self) -> ValidationReport:
        checks: list[CheckResult] = []
        for name in self.names:
            entries = self.problems.get(name, [])
            if not entries and name not in self.performed:
                entries = [
                    (
                        "incomplete",
                        ValidationIssue(
                            code="producer.not_checked",
                            path=(),
                            message="Prerequisites prevented this finite check; it was not executed.",
                            related_refs=(),
                        ),
                    )
                ]
            statuses = {status for status, _ in entries}
            status: CheckStatus = (
                "invalid"
                if "invalid" in statuses
                else "incomplete"
                if "incomplete" in statuses
                else "deferred"
                if "deferred" in statuses
                else "passed"
            )
            checks.append(
                CheckResult(
                    check_id=name,
                    status=status,
                    issues=tuple(issue for _, issue in entries),
                )
            )
        for name, message in (
            (
                "producer.expression_semantics",
                "Operand types, nominal enum/units and Boolean result typing require the later TypeChecker; no evaluator ran.",
            ),
            (
                "producer.conditional_availability",
                "Dynamic availability, activation truth and mandatory consumer completeness are not proven by data bindings.",
            ),
            (
                "producer.runtime_provenance",
                "Fact-only input has no actual implementation version; actual invocation/version/evidence trust remains deferred to integrated records/runtime.",
            ),
            (
                "producer.bundle_host_authenticity",
                "Parent catalogue/Bundle digest authenticity, trusted host/report resolution and release admission are not established by injected metadata.",
            ),
        ):
            checks.append(
                CheckResult(
                    check_id=name,
                    status="deferred",
                    issues=(
                        ValidationIssue(
                            code=name, path=(), message=message, related_refs=()
                        ),
                    ),
                )
            )
        return ValidationReport(scope="producer_admission", checks=tuple(checks))


def _index[T: ContractModel](
    entries: tuple[T, ...],
    ref: Callable[[T], ContentRef],
    body: Callable[[T], ContractModel],
    checks: _Checks,
) -> dict[tuple[object, ...], T]:
    result: dict[tuple[object, ...], T] = {}
    for entry in entries:
        identity = reference_identity(ref(entry))
        old = result.get(identity[:3])
        if old is not None:
            if reference_identity(ref(old)) != identity or not _same_data(
                body(old), body(entry)
            ):
                checks.add(
                    "producer.identities",
                    "invalid",
                    "producer.definition_rebinding",
                    "One definition identity has conflicting digest or content.",
                )
        else:
            result[identity[:3]] = entry
    return result


def _resolve[T: ContractModel](
    ref: ContentRef,
    indexed: dict[tuple[object, ...], T],
    actual: Callable[[T], ContentRef],
    checks: _Checks,
) -> T | None:
    entry = indexed.get(reference_identity(ref)[:3])
    if entry is None:
        checks.add(
            "producer.bindings",
            "incomplete",
            "producer.missing_definition",
            "An exact referenced definition is absent from the explicit context.",
        )
        return None
    if reference_identity(actual(entry)) != reference_identity(ref):
        checks.add(
            "producer.bindings",
            "invalid",
            "producer.definition_digest",
            "Known definition identity has the wrong pinned digest.",
        )
        return None
    return entry


def _registry_failure(exc: RegistryValidationError, checks: _Checks) -> None:
    unknown = any(
        issue.code == "registry.unknown_identity"
        for check in exc.report.checks
        for issue in check.issues
    )
    checks.add(
        "producer.schema",
        "incomplete" if unknown else "invalid",
        "producer.missing_schema" if unknown else "producer.invalid_schema",
        "Exact registered Schema, payload or path could not be validated.",
    )


def _skip_matches(declaration: FactProducerDeclaration, skip: ToolSkipPolicy) -> bool:
    activation = declaration.activation_condition
    condition = skip.condition_ast
    literal_true = (
        isinstance(condition, LiteralExpr)
        and isinstance(condition.constant.value, BooleanValue)
        and condition.constant.value.value is True
    )
    if isinstance(activation, AlwaysActivation):
        return condition is True or literal_true
    return condition is not True and _same_data(activation.condition_ast, condition)


def _selector_slot(
    selector: FactSelector, profile: ContentRef | None
) -> tuple[object, ...]:
    producer = selector.producer_selector
    prefix: tuple[object, ...] = (
        selector.scope,
        reference_identity(profile)
        if selector.scope is FactScope.OBJECT and profile is not None
        else None,
        producer.producer_kind,
    )
    if isinstance(producer, QuestionProducerSelector):
        return (
            *prefix,
            reference_identity(producer.question_ref),
            producer.output_name,
        )
    if isinstance(producer, ToolProducerSelector):
        return (
            *prefix,
            reference_identity(producer.tool_plan_ref),
            producer.step_id,
            reference_identity(producer.tool_ref),
            producer.output_name,
        )
    return (*prefix, *_requirement_identity(producer.requirement), producer.output_name)


def validate_producer_catalogue(
    registration: ProducerCatalogueRegistration,
    context: ProducerValidationContext,
    *,
    fact_schemas: FactSchemaRegistry,
) -> ValidationReport:
    checks = _Checks()
    try:
        registration = ProducerCatalogueRegistration.model_validate(registration)
        context = ProducerValidationContext.model_validate(context)
    except (ValidationError, ValueError):
        checks.add(
            "producer.input",
            "invalid",
            "producer.invalid_input",
            "Catalogue or context violates a strict immutable data contract.",
        )
        return checks.report()
    checks.performed.update(("producer.input", "producer.identities"))
    profiles = _index(
        context.profiles, lambda item: item.ref, lambda item: item.definition, checks
    )
    questions = _index(
        context.questions, lambda item: item.ref, lambda item: item.definition, checks
    )
    plans = _index(
        context.plans, lambda item: item.ref, lambda item: item.definition, checks
    )
    tools = _index(
        context.tools, lambda item: item.ref, lambda item: item.definition, checks
    )
    policies = _index(
        context.policies, lambda item: item.ref, lambda item: item.artifact, checks
    )
    hosts: dict[tuple[object, ...], SystemCapabilityRegistration] = {}
    for host in context.system_capabilities:
        key = (host.family, host.capability_id, host.contract_version)
        old = hosts.get(key)
        if old is not None:
            if (
                old.registry_id != host.registry_id
                or set(old.available_implementation_versions)
                != set(host.available_implementation_versions)
                or set(old.supported_features) != set(host.supported_features)
            ):
                checks.add(
                    "producer.identities",
                    "invalid",
                    "producer.host_rebinding",
                    "Registered capability identity has contradictory host data.",
                )
        else:
            hosts[key] = host
    declarations = registration.definition.declarations
    declared_ids = {item.declaration_id: item for item in declarations}
    for use in context.condition_contexts:
        declaration = declared_ids.get(use.declaration_id)
        valid = reference_identity(use.producer_catalogue_ref) == reference_identity(
            registration.ref
        )
        if declaration is None:
            valid = False
        else:
            valid = (
                valid
                and use.producer_kind is declaration.producer_kind
                and use.producer_scope is declaration.scope
            )
            valid = valid and (
                reference_identity(use.profile_ref) if use.profile_ref else None
            ) == (
                reference_identity(declaration.profile_ref)
                if declaration.profile_ref
                else None
            )
            if isinstance(declaration.producer, ToolStepProducer):
                valid = (
                    valid
                    and use.tool_plan_ref is not None
                    and reference_identity(use.tool_plan_ref)
                    == reference_identity(declaration.producer.tool_plan_ref)
                    and use.step_id == declaration.producer.step_id
                )
            else:
                valid = valid and use.tool_plan_ref is None and use.step_id is None
        if not valid:
            checks.add(
                "producer.identities",
                "invalid",
                "producer.condition_anchor",
                "A supplied condition use contradicts its exact catalogue/declaration anchor.",
            )
    if checks.problems.get("producer.identities"):
        return checks.report()
    checks.performed.update(
        (
            "producer.bindings",
            "producer.completeness",
            "producer.schema",
            "producer.conditions",
            "producer.selector_binding",
        )
    )
    schema_registry = isinstance(fact_schemas, FactSchemaRegistry)
    if not schema_registry:
        checks.add(
            "producer.schema",
            "incomplete",
            "producer.registry_required",
            "Explicit immutable Fact Schema registry is required.",
        )
    slots = {
        producer_output_slot(item): item for item in declarations
    }  # Catalogue model rejected collisions before this index.
    for declaration in declarations:
        producer = declaration.producer
        profile = (
            _resolve(declaration.profile_ref, profiles, lambda item: item.ref, checks)
            if declaration.profile_ref is not None
            else None
        )
        if schema_registry:
            try:
                schema = fact_schemas.resolve(declaration.fact_schema_ref)
                if (
                    schema.scope is not declaration.scope
                    or declaration.producer_kind not in schema.allowed_producer_kinds
                ):
                    checks.add(
                        "producer.schema",
                        "invalid",
                        "producer.schema_permission",
                        "Schema scope/producer-kind allowlist contradicts declaration.",
                    )
            except RegistryValidationError as exc:
                _registry_failure(exc, checks)
        if isinstance(producer, QuestionProducer):
            question = _resolve(
                producer.question_ref, questions, lambda item: item.ref, checks
            )
            if profile is not None and reference_identity(
                producer.question_ref
            ) not in {
                reference_identity(ref) for ref in profile.definition.question_refs
            }:
                checks.add(
                    "producer.bindings",
                    "invalid",
                    "producer.question_membership",
                    "Question is not in its exact Profile catalogue.",
                )
            if question is not None and (
                declaration.fact_key != question.definition.primary_fact_key
                or reference_identity(declaration.fact_schema_ref)
                != reference_identity(question.definition.primary_fact_schema_ref)
            ):
                checks.add(
                    "producer.bindings",
                    "invalid",
                    "producer.question_primary",
                    "Question key/Schema differs from its primary declaration.",
                )
        elif isinstance(producer, ToolStepProducer):
            plan = _resolve(
                producer.tool_plan_ref, plans, lambda item: item.ref, checks
            )
            tool = _resolve(producer.tool_ref, tools, lambda item: item.ref, checks)
            if profile is not None and reference_identity(
                profile.definition.tool_plan_ref
            ) != reference_identity(producer.tool_plan_ref):
                checks.add(
                    "producer.bindings",
                    "invalid",
                    "producer.plan_membership",
                    "ToolPlan is not the Profile's exact configured plan.",
                )
            step = (
                next(
                    (
                        step
                        for step in plan.definition.steps
                        if step.step_id == producer.step_id
                    ),
                    None,
                )
                if plan
                else None
            )
            if plan is not None and step is None:
                checks.add(
                    "producer.bindings",
                    "invalid",
                    "producer.missing_step",
                    "Known ToolPlan has no such step.",
                )
            if step is not None:
                if reference_identity(step.tool_ref) != reference_identity(
                    producer.tool_ref
                ):
                    checks.add(
                        "producer.bindings",
                        "invalid",
                        "producer.step_tool",
                        "Tool identity differs from actual step binding.",
                    )
                mapping = next(
                    (
                        item
                        for item in step.fact_output_mappings
                        if item.output_name == declaration.output_name
                    ),
                    None,
                )
                if mapping is None:
                    checks.add(
                        "producer.bindings",
                        "invalid",
                        "producer.unmapped_output",
                        "Producer output is not mapped by this step.",
                    )
                elif mapping.target_fact_key != declaration.fact_key:
                    checks.add(
                        "producer.bindings",
                        "invalid",
                        "producer.output_alias",
                        "Mapped target key differs from Producer output key.",
                    )
                skip_registration = _resolve(
                    step.skip_policy_ref, policies, lambda item: item.ref, checks
                )
                if skip_registration is not None:
                    skip = skip_registration.artifact.policy
                    if not isinstance(skip, ToolSkipPolicy):
                        checks.add(
                            "producer.bindings",
                            "invalid",
                            "producer.skip_subtype",
                            "Step skip reference must resolve to the full ToolSkipPolicy subtype.",
                        )
                    elif not _skip_matches(declaration, skip):
                        checks.add(
                            "producer.bindings",
                            "invalid",
                            "producer.activation_mismatch",
                            "Activation data does not structurally match actual pinned skip policy.",
                        )
            if tool is not None:
                output = next(
                    (
                        item
                        for item in tool.definition.fact_outputs
                        if item.output_name == declaration.output_name
                    ),
                    None,
                )
                if output is None:
                    checks.add(
                        "producer.bindings",
                        "invalid",
                        "producer.unknown_tool_output",
                        "Actual Tool has no such named Fact output.",
                    )
                elif (
                    output.fact_key != declaration.fact_key
                    or reference_identity(output.fact_schema_ref)
                    != reference_identity(declaration.fact_schema_ref)
                    or output.scope is not declaration.scope
                    or declaration.cardinality
                    is not (
                        ProducerCardinality.EXACTLY_ONE
                        if output.required
                        else ProducerCardinality.ZERO_OR_ONE
                    )
                ):
                    checks.add(
                        "producer.bindings",
                        "invalid",
                        "producer.tool_output_contract",
                        "Producer key/Schema/scope/cardinality contradicts actual Tool output.",
                    )
        elif isinstance(producer, SystemProducer):
            matching_host = hosts.get(_requirement_identity(producer.requirement))
            if matching_host is None:
                checks.add(
                    "producer.bindings",
                    "incomplete",
                    "producer.missing_host",
                    "System capability requirement has no matching supplied registration.",
                )
            elif not set(producer.requirement.allowed_implementation_versions) <= set(
                matching_host.available_implementation_versions
            ) or not set(producer.requirement.required_features) <= set(
                matching_host.supported_features
            ):
                checks.add(
                    "producer.bindings",
                    "invalid",
                    "producer.host_requirement",
                    "System versions/features are unsupported by injected host registration.",
                )
        uses = [
            use
            for use in context.condition_contexts
            if use.declaration_id == declaration.declaration_id
        ]
        activation = declaration.activation_condition
        condition: ConditionExpr | Literal[True] = (
            activation.condition_ast
            if isinstance(activation, ToolConditionActivation)
            else True
        )
        references = (
            condition_references(activation.condition_ast)
            if isinstance(activation, ToolConditionActivation)
            else None
        )
        has_references = references is not None and bool(
            references.context_selectors
            or references.fact_selectors
            or references.local_tool_selectors
        )
        if has_references and not uses:
            checks.add(
                "producer.conditions",
                "incomplete",
                "producer.missing_condition_context",
                "Referenced conditional data needs an explicitly anchored use environment.",
            )
            checks.add(
                "producer.selector_binding",
                "incomplete",
                "producer.selector_prerequisite",
                "Selectors cannot be uniquely matched before reference binding.",
            )
        for use in uses:
            bound = validate_condition_references(
                condition,
                use,
                input_domain=ConditionInputDomain.TOOL_CONDITION,
                profiles=context.profiles,
                questions=context.questions,
                tools=context.tools,
                plans=context.plans,
                fact_schemas=fact_schemas,
                context_allowlist=None,
            )
            for check in bound.report.checks:
                if check.status in ("invalid", "incomplete"):
                    for problem in check.issues:
                        checks.add(
                            "producer.conditions",
                            check.status,
                            problem.code,
                            problem.message,
                            problem.path,
                        )
            if bound.report.aggregate_status in ("invalid", "incomplete"):
                checks.add(
                    "producer.selector_binding",
                    "incomplete",
                    "producer.selector_prerequisite",
                    "Failed binding exposes no usable selector projection.",
                )
                continue
            for selector in bound.selectors_for_matching:
                target = slots.get(_selector_slot(selector, use.profile_ref))
                if target is None:
                    checks.add(
                        "producer.selector_binding",
                        "invalid",
                        "producer.no_matching_declaration",
                        "Bound selector does not uniquely identify a declaration in this Catalogue scope.",
                    )
                    continue
                if target.fact_key != selector.fact_key or reference_identity(
                    target.fact_schema_ref
                ) != reference_identity(selector.fact_schema_ref):
                    checks.add(
                        "producer.selector_binding",
                        "invalid",
                        "producer.selector_key_schema",
                        "Selector key/Schema differs from its declared output slot.",
                    )
                selected_producer = selector.producer_selector
                if (
                    isinstance(selected_producer, SystemProducerSelector)
                    and isinstance(target.producer, SystemProducer)
                    and not requirements_agree(
                        selected_producer.requirement, target.producer.requirement
                    )
                ):
                    checks.add(
                        "producer.selector_binding",
                        "invalid",
                        "producer.selector_requirement",
                        "System selector constraints differ from the declared requirement.",
                    )
                if schema_registry:
                    try:
                        fact_schemas.describe_type(
                            selector.fact_schema_ref, selector.field_path
                        )
                    except RegistryValidationError as exc:
                        _registry_failure(exc, checks)
    for profile in context.profiles:
        profile_key = reference_identity(profile.ref)
        owned = [
            item
            for item in declarations
            if item.profile_ref is not None
            and reference_identity(item.profile_ref) == profile_key
        ]
        for question_ref in profile.definition.question_refs:
            count = sum(
                isinstance(item.producer, QuestionProducer)
                and reference_identity(item.producer.question_ref)
                == reference_identity(question_ref)
                for item in owned
            )
            if count != 1:
                checks.add(
                    "producer.completeness",
                    "invalid",
                    "producer.primary_completeness",
                    "Every configured Profile+Question needs exactly one primary declaration.",
                )
        plan = _resolve(
            profile.definition.tool_plan_ref, plans, lambda item: item.ref, checks
        )
        if plan is None:
            checks.add(
                "producer.completeness",
                "incomplete",
                "producer.plan_prerequisite",
                "Mapped-output completeness needs the actual pinned plan.",
            )
            continue
        for step in plan.definition.steps:
            for mapping in step.fact_output_mappings:
                count = sum(
                    isinstance(item.producer, ToolStepProducer)
                    and reference_identity(item.producer.tool_plan_ref)
                    == reference_identity(plan.ref)
                    and item.producer.step_id == step.step_id
                    and reference_identity(item.producer.tool_ref)
                    == reference_identity(step.tool_ref)
                    and item.output_name == mapping.output_name
                    for item in owned
                )
                if count != 1:
                    checks.add(
                        "producer.completeness",
                        "invalid",
                        "producer.mapped_completeness",
                        "Every mapped step output needs exactly one declaration, regardless of activation.",
                    )
    return checks.report()


def validate_fact_producer(
    fact: FactRecord,
    registration: ProducerCatalogueRegistration,
    context: ProducerValidationContext,
    *,
    fact_schemas: FactSchemaRegistry,
) -> ValidationReport:
    report = validate_producer_catalogue(
        registration, context, fact_schemas=fact_schemas
    )
    problems: list[ValidationIssue] = []
    status: CheckStatus = "passed"

    def reject(
        code: str, message: str, state: Literal["invalid", "incomplete"] = "invalid"
    ) -> None:
        nonlocal status
        if state == "invalid" or status == "passed":
            status = state
        problems.append(
            ValidationIssue(code=code, path=(), message=message, related_refs=())
        )

    try:
        checked: FactRecord = TypeAdapter(FactRecord).validate_python(fact)
        registration = ProducerCatalogueRegistration.model_validate(registration)
        context = ProducerValidationContext.model_validate(context)
    except (ValidationError, ValueError):
        reject(
            "producer.invalid_fact",
            "Fact or Producer input violates its strict data contract.",
        )
    else:
        if report.aggregate_status in ("invalid", "incomplete"):
            reject(
                "producer.fact_prerequisite",
                "Catalogue/context conformance is not established.",
                "incomplete",
            )
        elif reference_identity(checked.producer_catalogue_ref) != reference_identity(
            registration.ref
        ):
            reject(
                "producer.fact_catalogue",
                "Fact pins a different catalogue identity/digest.",
            )
        else:
            declaration = next(
                (
                    item
                    for item in registration.definition.declarations
                    if item.declaration_id == checked.producer_declaration_id
                ),
                None,
            )
            if declaration is None:
                reject(
                    "producer.fact_declaration",
                    "Pinned Catalogue does not contain the Fact's declaration ID.",
                )
            else:
                profile = (
                    reference_identity(checked.profile_ref)
                    if checked.profile_ref is not None
                    else None
                )
                expected_profile = (
                    reference_identity(declaration.profile_ref)
                    if declaration.profile_ref is not None
                    else None
                )
                if (
                    checked.scope is not declaration.scope
                    or profile != expected_profile
                    or checked.producer_kind is not declaration.producer_kind
                    or (checked.producer_id, checked.producer_version)
                    != (declaration.producer_id, declaration.producer_version)
                    or checked.fact_key != declaration.fact_key
                    or reference_identity(checked.fact_schema_ref)
                    != reference_identity(declaration.fact_schema_ref)
                ):
                    reject(
                        "producer.fact_binding",
                        "Fact scope/Profile/producer/key/Schema contradicts its exact declaration.",
                    )
                producer, source = declaration.producer, checked.source
                if isinstance(producer, QuestionProducer):
                    valid = (
                        isinstance(source, QuestionFactSource)
                        and reference_identity(source.question_ref)
                        == reference_identity(producer.question_ref)
                        and declaration.output_name == "primary"
                    )
                elif isinstance(producer, ToolStepProducer):
                    valid = (
                        isinstance(source, ToolFactSource)
                        and reference_identity(source.tool_plan_ref)
                        == reference_identity(producer.tool_plan_ref)
                        and source.step_id == producer.step_id
                        and reference_identity(source.tool_ref)
                        == reference_identity(producer.tool_ref)
                        and source.output_name == declaration.output_name
                    )
                else:
                    valid = (
                        isinstance(source, SystemFactSource)
                        and source.output_name == declaration.output_name
                        and requirements_agree(source.requirement, producer.requirement)
                    )
                if not valid:
                    reject(
                        "producer.fact_source",
                        "Fact source identity/output/requirements differ from declared Producer.",
                    )
                try:
                    schema = fact_schemas.resolve(checked.fact_schema_ref)
                    if (
                        schema.scope is not checked.scope
                        or checked.producer_kind not in schema.allowed_producer_kinds
                    ):
                        reject(
                            "producer.fact_schema_permission",
                            "Registered Schema rejects scope or producer kind.",
                        )
                    if isinstance(checked, KnownFact):
                        fact_schemas.validate_payload(
                            checked.fact_schema_ref, checked.value
                        )
                    policy = schema.confidence_metadata_policy
                    present = checked.confidence is not None
                    if isinstance(policy, ForbiddenConfidenceMetadataPolicy):
                        valid_confidence = not present
                    else:
                        allowed = checked.status in policy.allowed_fact_statuses
                        valid_confidence = (allowed or not present) and (
                            not isinstance(policy, RequiredConfidenceMetadataPolicy)
                            or not allowed
                            or present
                        )
                    if not valid_confidence:
                        reject(
                            "producer.fact_confidence",
                            "Confidence presence/status contradicts Schema metadata policy.",
                        )
                except RegistryValidationError as exc:
                    unknown = any(
                        issue.code == "registry.unknown_identity"
                        for check in exc.report.checks
                        for issue in check.issues
                    )
                    reject(
                        "producer.fact_schema",
                        "Fact value/Schema could not be validated.",
                        "incomplete" if unknown else "invalid",
                    )
    return ValidationReport(
        scope="producer_admission",
        checks=(
            *report.checks,
            CheckResult(
                check_id="producer.fact", status=status, issues=tuple(problems)
            ),
        ),
    )
