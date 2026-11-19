"""Prepare and evaluate one admitted V1 predicate with Strong Kleene truth."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from operator import ge, gt, le, lt
from types import MappingProxyType
from typing import Literal, assert_never

from pydantic import TypeAdapter

from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.bundle.catalogue import ArtifactCatalogue
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.domain.conditions import (
    AllExpr,
    AnyExpr,
    ComparisonExpr,
    ConditionExpr,
    ContextRefExpr,
    FactRefExpr,
    InExpr,
    LiteralExpr,
    LocalToolFactRefExpr,
    NotExpr,
    RecordFieldRefExpr,
    condition_references,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import (
    ComparisonOperator,
    ConditionInputDomain,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    reference_identity,
)
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.registry import (
    FactSchemaRegistry,
    RegistryValidationError,
)
from jimu_runtime.domain.fact_selectors import FactSelector, ToolProducerSelector
from jimu_runtime.domain.policies import (
    ApplicabilityPolicyArtifact,
    ToolSkipPolicyArtifact,
)
from jimu_runtime.domain.primitives import ArtifactId, RecordId
from jimu_runtime.domain.producers import ProducerCatalogue, ToolConditionActivation
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.rules.definitions import RuleDefinition
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.runtime.decisions import PredicateRecord
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.tool_mappings import ProfileContentRef
from jimu_runtime.domain.validation.condition_inputs import (
    ApplicabilityConditionContext,
    ProducerActivationConditionContext,
    ToolStepGuardConditionContext,
)
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleConditionContext,
    RuleTypeBindings,
    RuleTypeUseContext,
)
from jimu_runtime.domain.validation.rule_types import check_condition_types
from jimu_runtime.domain.validation.schema_descriptors import (
    describe_field,
    resolve_descriptor,
)
from jimu_runtime.domain.values import (
    BooleanValue,
    IntegerValue,
    ListValue,
    NumberValue,
    TypeDefinition,
    Value,
)
from jimu_runtime.inspection.rules._operands import (
    FactKey,
    FactSlot,
    Operand,
    PredicateInputError,
    project_fact,
    read_operands,
)

_USE = TypeAdapter[RuleTypeUseContext](RuleTypeUseContext)
# Capacity of the existing PredicateRecord contract.
_MAX_DIAGNOSTICS = 1024
_ORDERING: Mapping[ComparisonOperator, Callable[[int | float, int | float], bool]] = (
    MappingProxyType(
        {
            ComparisonOperator.LT: lt,
            ComparisonOperator.LE: le,
            ComparisonOperator.GT: gt,
            ComparisonOperator.GE: ge,
        }
    )
)


class PredicatePreparationError(PredicateInputError):
    """An unbound or ill-typed predicate. Type-check failures retain their issues."""

    def __init__(self, code: str, issues: tuple[ValidationIssue, ...] = ()) -> None:
        super().__init__(code)
        self.issues = issues


def _resolve[T](catalogue: ArtifactCatalogue, ref: ContentRef, expected: type[T]) -> T:
    try:
        value = catalogue.resolve(ref).value
    except BundleArtifactError:
        raise PredicatePreparationError("predicate.artifact_unavailable") from None
    if not isinstance(value, expected):
        raise PredicatePreparationError("predicate.artifact_kind")
    return value


def _check_bindings(
    catalogue: ArtifactCatalogue,
    context: RuleTypeUseContext,
    bindings: RuleTypeBindings,
) -> None:
    for directory in (
        bindings.profiles,
        bindings.questions,
        bindings.plans,
        bindings.tools,
    ):
        for item in directory:
            if _resolve(catalogue, item.ref, type(item.definition)) != item.definition:
                raise PredicatePreparationError("predicate.definition_binding")
    for policy in bindings.policies:
        if _resolve(catalogue, policy.ref, type(policy.artifact)) != policy.artifact:
            raise PredicatePreparationError("predicate.policy_binding")
    if isinstance(context, RuleConditionContext):
        rule_set = context.rule_set
        if _resolve(catalogue, rule_set.ref, type(rule_set.definition)) != (
            rule_set.definition
        ):
            raise PredicatePreparationError("predicate.rule_set_binding")
    producer_catalogue = bindings.producer_catalogue
    if (
        producer_catalogue is not None
        and _resolve(catalogue, producer_catalogue.ref, ProducerCatalogue)
        != producer_catalogue.definition
    ):
        raise PredicatePreparationError("predicate.producer_catalogue_binding")


def _condition(
    catalogue: ArtifactCatalogue, context: RuleTypeUseContext
) -> RuleDefinition | ConditionExpr | Literal[True]:
    match context:
        case RuleConditionContext():
            return _resolve(catalogue, context.rule_ref, RuleDefinition)
        case ApplicabilityConditionContext():
            return _resolve(
                catalogue, context.policy_ref, ApplicabilityPolicyArtifact
            ).policy.condition_ast
        case ToolStepGuardConditionContext():
            return _resolve(
                catalogue, context.policy_ref, ToolSkipPolicyArtifact
            ).policy.condition_ast
        case _:
            producers = _resolve(
                catalogue, context.producer_catalogue_ref, ProducerCatalogue
            )
            for item in producers.declarations:
                if item.declaration_id == context.declaration_id:
                    active = item.activation_condition
                    return (
                        active.condition_ast
                        if isinstance(active, ToolConditionActivation)
                        else True
                    )
            raise PredicatePreparationError("predicate.producer_unavailable")


def _slots(
    catalogue: ArtifactCatalogue,
    context: RuleTypeUseContext,
    bindings: RuleTypeBindings,
    fact_schemas: FactSchemaRegistry,
    expression: ConditionExpr | Literal[True],
) -> tuple[dict[ContextFieldSelector, TypeDefinition], dict[FactKey, FactSlot]]:
    if expression is True:
        return {}, {}
    refs = condition_references(expression)
    context_shapes = {}
    if refs.context_selectors:
        try:
            admitted = {
                reference_identity(descriptor.schema_ref): descriptor
                for descriptor in build_schema_descriptors(catalogue)
            }
        except BundleArtifactError:
            raise PredicatePreparationError(
                "predicate.context_schema_binding"
            ) from None
        for field in dict.fromkeys(refs.context_selectors):
            supplied = resolve_descriptor(field.schema_ref, context.descriptors)
            actual = admitted.get(reference_identity(field.schema_ref))
            if actual != supplied:
                raise PredicatePreparationError("predicate.context_schema_binding")
            context_shapes[field] = describe_field(
                supplied, field.field_path
            ).type_definition

    selectors = list(refs.fact_selectors)
    if refs.local_tool_selectors:
        if not isinstance(
            context, (ToolStepGuardConditionContext, ProducerActivationConditionContext)
        ):
            raise PredicatePreparationError("predicate.local_tool_domain")
        plan = context.tool_plan_ref
        if plan is None:
            raise PredicatePreparationError("predicate.local_tool_plan_unavailable")
        for local in refs.local_tool_selectors:
            selectors.append(
                FactSelector(
                    scope=local.scope,
                    fact_key=local.fact_key,
                    fact_schema_ref=local.fact_schema_ref,
                    field_path=local.field_path,
                    producer_selector=ToolProducerSelector(
                        producer_kind=ProducerKind.TOOL,
                        tool_plan_ref=plan,
                        step_id=local.step_id,
                        tool_ref=local.tool_ref,
                        output_name=local.output_name,
                    ),
                )
            )

    fact_slots: dict[FactKey, FactSlot] = {}
    if selectors:
        catalogue_entry = bindings.producer_catalogue
        assert catalogue_entry is not None
        producers = {
            (item.scope, item.fact_key): item
            for item in catalogue_entry.definition.declarations
            if item.profile_ref in (None, context.profile_ref)
        }
        for selector in selectors:
            key = (selector.scope, selector.fact_key)
            if key not in fact_slots:
                definition = _resolve(
                    catalogue, selector.fact_schema_ref, FactSchemaDefinition
                )
                if definition != fact_schemas.resolve(selector.fact_schema_ref):
                    raise PredicatePreparationError("predicate.fact_schema_binding")
                fact_slots[key] = FactSlot(
                    producer=selector.producer_selector,
                    catalogue_ref=catalogue_entry.ref,
                    declaration_id=producers[key].declaration_id,
                    schema_ref=selector.fact_schema_ref,
                    shape=definition.type_definition,
                )
    return context_shapes, fact_slots


def _comparison(operator: ComparisonOperator, left: Value, right: Value) -> bool:
    if operator is ComparisonOperator.EQ:
        return left == right
    if operator is ComparisonOperator.NE:
        return left != right
    assert isinstance(left, (IntegerValue, NumberValue))
    assert isinstance(right, (IntegerValue, NumberValue))
    return _ORDERING[operator](left.value, right.value)


def _boolean(value: bool | None) -> Operand:
    return Operand(None if value is None else BooleanValue(kind="bool", value=value))


def _evaluate(
    node: ConditionExpr,
    contexts: Mapping[ContextFieldSelector, Operand],
    facts: Mapping[FactKey, Operand],
    diagnostics: dict[ArtifactId, None],
) -> Operand:
    def child(expr: ConditionExpr) -> Operand:
        return _evaluate(expr, contexts, facts, diagnostics)

    match node:
        case LiteralExpr():
            return Operand(node.constant.value)
        case ContextRefExpr():
            operand = contexts.get(
                node.selector, Operand(None, ("predicate.context.missing",))
            )
        case FactRefExpr() | RecordFieldRefExpr() | LocalToolFactRefExpr():
            selector = node.selector
            operand = project_fact(
                facts.get(
                    (selector.scope, selector.fact_key),
                    Operand(None, ("predicate.fact.missing",)),
                ),
                selector.field_path,
            )
        case ComparisonExpr():
            left, right = child(node.left).value, child(node.right).value
            return _boolean(
                None
                if left is None or right is None
                else _comparison(node.operator, left, right)
            )
        case InExpr():
            item, collection = child(node.item).value, child(node.collection).value
            if item is None or collection is None:
                return _boolean(None)
            assert isinstance(collection, ListValue)
            return _boolean(item in collection.value)
        case AllExpr() | AnyExpr():
            # Visit every branch, including branches that cannot affect the truth.
            values = tuple(
                _nullable_bool(child(argument)) for argument in node.arguments
            )
            decisive = isinstance(node, AnyExpr)
            if decisive in values:
                return _boolean(decisive)
            if None in values:
                return _boolean(None)
            return _boolean(not decisive)
        case NotExpr():
            value = _nullable_bool(child(node.argument))
            return _boolean(None if value is None else not value)
        case _:
            assert_never(node)

    for code in operand.diagnostics:
        diagnostics[code] = None
        if len(diagnostics) > _MAX_DIAGNOSTICS:
            raise PredicateInputError("predicate.diagnostic_limit")
    return operand


def _nullable_bool(operand: Operand) -> bool | None:
    if operand.value is None:
        return None
    assert isinstance(operand.value, BooleanValue)
    return operand.value.value


@dataclass(frozen=True, slots=True, init=False)
class PreparedPredicate:
    """Pure, reusable predicate from the current Release's admitted catalogue.

    Supply admission-derived type views and only operand inputs already selected
    for the current Execution/Profile/Object. Callers own admission, Context
    projection, Fact authority and Required Evidence Guard evaluation.
    """

    _expression: ConditionExpr | Literal[True]
    _profile_ref: ProfileContentRef | None
    _context_shapes: Mapping[ContextFieldSelector, TypeDefinition]
    _fact_slots: Mapping[FactKey, FactSlot]

    def __init__(
        self,
        *,
        catalogue: ArtifactCatalogue,
        input_domain: ConditionInputDomain,
        context: RuleTypeUseContext,
        bindings: RuleTypeBindings,
        fact_schemas: FactSchemaRegistry,
    ) -> None:
        try:
            input_domain = ConditionInputDomain._decode(input_domain)
            context = _USE.validate_python(context)
            bindings = RuleTypeBindings.model_validate(bindings)
        except ValueError:
            raise PredicatePreparationError("predicate.invalid_use") from None

        _check_bindings(catalogue, context, bindings)
        condition = _condition(catalogue, context)
        # Applicability needs the actual policy allowlist even if the caller's
        # otherwise sufficient type bindings omit the policy directory.
        allowlist = (
            _resolve(
                catalogue, context.policy_ref, ApplicabilityPolicyArtifact
            ).policy.context_allowlist
            if isinstance(context, ApplicabilityConditionContext)
            else None
        )
        checked = check_condition_types(
            condition,
            input_domain=input_domain,
            context=context,
            bindings=bindings,
            fact_schemas=fact_schemas,
            context_allowlist=allowlist,
        )
        if checked.status != "passed":
            raise PredicatePreparationError(checked.issues[0].code, checked.issues)
        expression = (
            condition.condition_ast
            if isinstance(condition, RuleDefinition)
            else condition
        )
        if expression is not True:
            expression = validate_condition_data(expression, input_domain=input_domain)
        try:
            contexts, slots = _slots(
                catalogue, context, bindings, fact_schemas, expression
            )
        except RegistryValidationError:
            raise PredicatePreparationError("predicate.fact_schema_binding") from None

        object.__setattr__(self, "_expression", expression)
        object.__setattr__(self, "_profile_ref", context.profile_ref)
        object.__setattr__(self, "_context_shapes", MappingProxyType(contexts))
        object.__setattr__(self, "_fact_slots", MappingProxyType(slots))

    def evaluate(
        self,
        *,
        execution_id: RecordId,
        object_id: RecordId | None = None,
        context_fields: Mapping[ContextFieldSelector, Value] | None = None,
        facts: tuple[FactRecord, ...] = (),
    ) -> PredicateRecord:
        """Return predicate truth and first-seen distinct codes for every operand."""
        contexts, operands = read_operands(
            execution_id=execution_id,
            object_id=object_id,
            profile_ref=self._profile_ref,
            context_shapes=self._context_shapes,
            context_fields=context_fields,
            fact_slots=self._fact_slots,
            facts=facts,
        )
        diagnostics: dict[ArtifactId, None] = {}
        value = (
            True
            if self._expression is True
            else _nullable_bool(
                _evaluate(self._expression, contexts, operands, diagnostics)
            )
        )
        if value is None:
            truth = RuleTruth.UNKNOWN
        else:
            truth = RuleTruth.TRUE if value else RuleTruth.FALSE
        return PredicateRecord(truth=truth, diagnostic_codes=tuple(diagnostics))
