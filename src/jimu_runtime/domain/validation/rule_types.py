"""Statically check a finite condition use and its pinned type dependencies."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.conditions import (
    AllExpr,
    AnyExpr,
    ComparisonExpr,
    ConditionDataError,
    ConditionExpr,
    ContextRefExpr,
    FactRefExpr,
    InExpr,
    LiteralExpr,
    LocalToolFactRefExpr,
    NotExpr,
    RecordFieldRefExpr,
    validate_condition_data,
)
from jimu_runtime.domain.definition_enums import (
    ConditionInputDomain,
    FactScope,
    ProducerKind,
)
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    FieldPath,
    VocabularyMemberRef,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.models import DeclaredUnitMetadata
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
from jimu_runtime.domain.policies import ApplicabilityPolicy, ToolSkipPolicy
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    QuestionProducer,
    SystemProducer,
    ToolConditionActivation,
    ToolStepProducer,
)
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.rules import (
    OPERATOR_DESCRIPTORS,
    FailedTypeCheck,
    PassedTypeCheck,
    RuleDefinition,
    TypeCheckResult,
)
from jimu_runtime.domain.tool_mappings import (
    ProfileContextRegistration,
    RegionContextRegistration,
)
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.condition_inputs import (
    ConditionReferenceResult,
    ProducerActivationConditionContext,
    ToolStepGuardConditionContext,
)
from jimu_runtime.domain.validation.conditions import validate_condition_references
from jimu_runtime.domain.validation.definition_inputs import ProfileRegistration
from jimu_runtime.domain.validation.issues import IssuePath, ValidationIssue
from jimu_runtime.domain.validation.policies import PolicyRegistration
from jimu_runtime.domain.validation.producers import requirements_agree
from jimu_runtime.domain.validation.rule_type_inputs import (
    RuleConditionContext,
    RuleTypeBindings,
    RuleTypeUseContext,
)
from jimu_runtime.domain.validation.schema_descriptors import (
    SchemaCheckError,
    describe_field,
    resolve_descriptor,
)
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import (
    BooleanTypeDefinition,
    EnumTypeDefinition,
    IntegerTypeDefinition,
    ListTypeDefinition,
    ListValue,
    NumberTypeDefinition,
    RecordTypeDefinition,
    TypeDefinition,
)

type _Outcome = Literal["invalid", "incomplete"]
type _Pin = tuple[ArtifactKind, str, str, str]


class _Problem(Exception):
    def __init__(
        self,
        code: str,
        path: IssuePath,
        message: str,
        *,
        outcome: _Outcome = "invalid",
        refs: tuple[ContentRef, ...] = (),
    ) -> None:
        self.outcome = outcome
        if len(path) > 32:
            path = path[:15] + ("…",) + path[-16:]
        self.issue = ValidationIssue(
            code=code, path=path, message=message, related_refs=refs
        )


def _same(left: ContentRef, right: ContentRef) -> bool:
    return reference_identity(left) == reference_identity(right)


def _entry[
    T: (
        ProfileRegistration,
        AuthorityQuestionInput,
        AuthorityToolPlanInput,
        ToolRegistration,
        PolicyRegistration,
    )
](ref: ContentRef, registrations: tuple[T, ...], path: IssuePath) -> T:
    identity = reference_identity(ref)
    matches = [
        item
        for item in registrations
        if reference_identity(item.ref)[:3] == identity[:3]
    ]
    if not matches:
        raise _Problem(
            "rule_type.registration_unavailable",
            path,
            "The pinned definition is unavailable in the supplied type context.",
            outcome="incomplete",
            refs=(ref,),
        )
    if len(matches) != 1 or not _same(ref, matches[0].ref):
        raise _Problem(
            "rule_type.registration_conflict",
            path,
            "The definition registration is ambiguous or its digest differs.",
            refs=(ref,),
        )
    return matches[0]


def _args(expr: ConditionExpr) -> tuple[tuple[IssuePath, ConditionExpr], ...]:
    if isinstance(expr, (AllExpr, AnyExpr)):
        return tuple(
            (("arguments", i), value) for i, value in enumerate(expr.arguments)
        )
    if isinstance(expr, ComparisonExpr):
        return ((("left",), expr.left), (("right",), expr.right))
    if isinstance(expr, InExpr):
        return ((("item",), expr.item), (("collection",), expr.collection))
    if isinstance(expr, NotExpr):
        return ((("argument",), expr.argument),)
    return ()


def _occurrences(
    expr: ConditionExpr, path: IssuePath = ()
) -> Iterator[tuple[IssuePath, ConditionExpr]]:
    yield path, expr
    for child_path, child in _args(expr):
        yield from _occurrences(child, path + child_path)


def _structure(raw: object) -> ConditionExpr:
    # Check B's structural contract once. The traversal below applies the narrower
    # input domain at each reference occurrence so errors retain an AST location.
    return validate_condition_data(
        raw, input_domain=ConditionInputDomain.TOOL_CONDITION
    )


@dataclass(frozen=True, slots=True)
class _Type:
    shape: TypeDefinition
    unit: VocabularyMemberRef | None = None
    schema: _Pin | None = None
    field_path: FieldPath = ()
    literal_count: int | None = None

    @property
    def numeric(self) -> bool:
        return isinstance(self.shape, (IntegerTypeDefinition, NumberTypeDefinition))

    @property
    def scalar(self) -> bool:
        return not isinstance(self.shape, (RecordTypeDefinition, ListTypeDefinition))

    def list_item(self) -> "_Type":
        assert isinstance(self.shape, ListTypeDefinition)
        return _Type(self.shape.item_type, self.unit, self.schema, self.field_path)


def _compatible(left: _Type, right: _Type, path: IssuePath) -> None:
    if left.shape.kind != right.shape.kind:
        raise _Problem(
            "rule_type.kind_mismatch", path, "Operands must have the same scalar type."
        )
    if left.unit != right.unit:
        raise _Problem(
            "rule_type.unit_mismatch",
            path,
            "Operands require the same exact declared unit.",
        )
    if isinstance(left.shape, EnumTypeDefinition) and isinstance(
        right.shape, EnumTypeDefinition
    ):
        if left.schema is None and right.schema is None:
            raise _Problem(
                "rule_type.enum_identity_unavailable",
                path,
                "Enum comparison requires a pinned enum Schema operand.",
                outcome="incomplete",
            )
        if (
            left.schema is not None
            and right.schema is not None
            and (left.schema, left.field_path) != (right.schema, right.field_path)
        ):
            raise _Problem(
                "rule_type.enum_identity_mismatch",
                path,
                "Enum operands have different exact Schema or field identities.",
            )
        if set(left.shape.members) != set(right.shape.members):
            raise _Problem(
                "rule_type.enum_members_mismatch",
                path,
                "An enum literal must declare exactly the pinned field's closed member set.",
            )


def _signature(
    expr: ConditionExpr, operands: tuple[_Type, ...], path: IssuePath
) -> _Type:
    key = expr.operator.value if isinstance(expr, ComparisonExpr) else expr.kind
    descriptor = OPERATOR_DESCRIPTORS.get(key)
    if descriptor is None:
        raise _Problem(
            "rule_type.unknown_operator", path, "Operator is not registered."
        )
    if not descriptor.arity.min_args <= len(operands) <= descriptor.arity.max_args:
        raise _Problem(
            "rule_type.arity", path, "Operator argument count is outside its signature."
        )
    constraint = descriptor.input_constraint
    if constraint == "boolean_arguments":
        for (argument_path, _), operand in zip(_args(expr), operands, strict=True):
            if not isinstance(operand.shape, BooleanTypeDefinition):
                raise _Problem(
                    "rule_type.boolean_argument",
                    path + argument_path,
                    "The operator requires a Boolean operand.",
                )
    elif constraint == "scalar_and_nonempty_list":
        left, right = operands
        bounded = isinstance(right.shape, ListTypeDefinition) and (
            right.literal_count > 0
            if right.literal_count is not None
            else right.shape.min_items > 0
        )
        if not left.scalar or not bounded:
            raise _Problem(
                "rule_type.membership_shape",
                path,
                "Membership requires a scalar and a declared nonempty bounded list.",
            )
        _compatible(left, right.list_item(), path + ("collection",))
    else:
        left, right = operands
        if constraint == "same_numeric_type_unit" and not (
            left.numeric and right.numeric
        ):
            raise _Problem(
                "rule_type.numeric_comparison",
                path,
                "Ordering requires two numeric operands.",
            )
        if not (left.scalar and right.scalar):
            raise _Problem(
                "rule_type.scalar_comparison",
                path,
                "Comparison requires two scalar operands.",
            )
        _compatible(left, right, path + ("right",))
    return _Type(descriptor.output_type)


def _schema_problem(
    exc: SchemaCheckError | RegistryValidationError, path: IssuePath
) -> _Problem:
    if isinstance(exc, SchemaCheckError):
        issue = exc.issue
        outcome: _Outcome = "invalid" if issue.outcome == "invalid" else "incomplete"
        return _Problem(issue.code, path, issue.message, outcome=outcome)
    diagnostic = exc.report.checks[0].issues[0]
    outcome = (
        "incomplete" if diagnostic.code == "registry.unknown_identity" else "invalid"
    )
    return _Problem(diagnostic.code, path, diagnostic.message, outcome=outcome)


class _Checker:
    def __init__(
        self,
        domain: ConditionInputDomain,
        context: RuleTypeUseContext,
        bindings: RuleTypeBindings,
        fact_schemas: FactSchemaRegistry,
        allowlist: tuple[ContextFieldSelector, ...] | None,
    ) -> None:
        self.domain = domain
        self.context = context
        self.bindings = bindings
        self.fact_schemas = fact_schemas
        self.allowlist = allowlist
        self.profile: ProfileDefinition | None = None
        self.activation: FactProducerDeclaration | None = None
        catalogue = bindings.producer_catalogue
        self.declarations = (
            () if catalogue is None else catalogue.definition.declarations
        )
        self.literal_units = {
            binding.path: binding.unit for binding in bindings.literal_units
        }
        self.context_units = {
            binding.selector: binding.unit for binding in bindings.context_units
        }
        self.projected: dict[IssuePath, FactSelector] = {}

    def check(self, condition: object) -> BooleanTypeDefinition:
        context = self.context
        required_role = {
            ConditionInputDomain.RULE_APPLICABILITY: ("applicability",),
            ConditionInputDomain.RULE_CONDITION: ("rule_condition",),
            ConditionInputDomain.TOOL_CONDITION: (
                "tool_step_guard",
                "producer_activation",
            ),
        }
        if context.use_role not in required_role[self.domain]:
            raise _Problem(
                "rule_type.use_role",
                ("context", "use_role"),
                "The explicit input domain does not permit this condition use.",
            )
        if context.profile_ref is not None:
            self.profile = _entry(
                context.profile_ref, self.bindings.profiles, ("context", "profile_ref")
            ).definition
        expr: ConditionExpr | Literal[True]
        if isinstance(context, RuleConditionContext):
            expr = self._rule(condition, context)
        else:
            expr = True if condition is True else _structure(condition)
            self._anchor(expr)
        if expr is True:
            self._bind_nonrule(expr)
            if self.literal_units:
                raise _Problem(
                    "rule_type.literal_unit_path",
                    ("literal_units",),
                    "Unit binding requires a numeric literal occurrence.",
                )
            return BooleanTypeDefinition(kind="bool")
        self._check_domain(expr)
        if not isinstance(context, RuleConditionContext):
            self._bind_nonrule(expr)
        inferred = self._infer(expr)
        for path in self.literal_units:
            raise _Problem(
                "rule_type.literal_unit_path",
                path,
                "Unit binding does not point to a numeric literal.",
            )
        if not isinstance(inferred.shape, BooleanTypeDefinition):
            raise _Problem(
                "rule_type.nonboolean_root", (), "Predicate root must be Boolean."
            )
        return inferred.shape

    def _rule(self, condition: object, context: RuleConditionContext) -> ConditionExpr:
        if not isinstance(condition, RuleDefinition):
            raise _Problem(
                "rule_type.rule_required",
                (),
                "A Rule condition requires its RuleDefinition and dependency lists.",
            )
        rule = RuleDefinition.model_validate(condition)
        if (context.rule_ref.artifact_id, context.rule_ref.version) != (
            rule.rule_id,
            rule.version,
        ):
            raise _Problem(
                "rule_type.rule_identity",
                ("context", "rule_ref"),
                "Rule ID/version differs from its pin.",
            )
        assert self.profile is not None
        if not any(
            _same(ref, context.rule_set.ref) for ref in self.profile.rule_set_refs
        ):
            raise _Problem(
                "rule_type.rule_set_membership",
                ("context", "rule_set"),
                "The pinned RuleSet is outside this Profile.",
            )
        if not any(
            _same(ref, context.rule_ref)
            for ref in context.rule_set.definition.rule_refs
        ):
            raise _Problem(
                "rule_type.rule_membership",
                ("context", "rule_ref"),
                "The pinned Rule is outside this RuleSet.",
            )
        return rule.condition_ast

    def _anchor(self, expr: ConditionExpr | Literal[True]) -> None:
        context = self.context
        if context.use_role == "applicability":
            if expr is True:
                raise _Problem(
                    "rule_type.bare_true",
                    (),
                    "Applicability requires a Boolean expression.",
                )
            if self.bindings.policies:
                policy = _entry(
                    context.policy_ref,
                    self.bindings.policies,
                    ("context", "policy_ref"),
                ).artifact.policy
                if (
                    not isinstance(policy, ApplicabilityPolicy)
                    or expr != policy.condition_ast
                ):
                    raise _Problem(
                        "rule_type.policy_binding",
                        ("context", "policy_ref"),
                        "Condition differs from the pinned ApplicabilityPolicy.",
                    )
                if self.allowlist is not None and set(self.allowlist) != set(
                    policy.context_allowlist
                ):
                    raise _Problem(
                        "rule_type.context_allowlist",
                        ("context_allowlist",),
                        "The supplied allowlist differs from the pinned policy.",
                    )
                self.allowlist = policy.context_allowlist
            if self.allowlist is None:
                raise _Problem(
                    "rule_type.context_allowlist_unavailable",
                    ("context_allowlist",),
                    "Applicability requires an explicit declared Context allowlist.",
                    outcome="incomplete",
                )
        elif context.use_role == "tool_step_guard":
            assert self.profile is not None
            if not _same(self.profile.tool_plan_ref, context.tool_plan_ref):
                raise _Problem(
                    "rule_type.foreign_plan",
                    ("context", "tool_plan_ref"),
                    "Tool guard must use the Profile's exact ToolPlan.",
                )
            plan = _entry(
                context.tool_plan_ref, self.bindings.plans, ("context", "tool_plan_ref")
            ).definition
            step = next(
                (value for value in plan.steps if value.step_id == context.step_id),
                None,
            )
            if step is None or not _same(step.skip_policy_ref, context.policy_ref):
                raise _Problem(
                    "rule_type.guard_anchor",
                    ("context", "step_id"),
                    "ToolStep and its exact skip policy must identify the guard use.",
                )
            if self.bindings.policies:
                policy = _entry(
                    context.policy_ref,
                    self.bindings.policies,
                    ("context", "policy_ref"),
                ).artifact.policy
                if (
                    not isinstance(policy, ToolSkipPolicy)
                    or expr != policy.condition_ast
                ):
                    raise _Problem(
                        "rule_type.policy_binding",
                        ("context", "policy_ref"),
                        "Condition differs from the pinned ToolSkipPolicy.",
                    )
        elif context.use_role == "producer_activation":
            self._producer_anchor(context, expr)

    def _producer_anchor(
        self,
        context: ProducerActivationConditionContext,
        expr: ConditionExpr | Literal[True],
    ) -> None:
        catalogue = self.bindings.producer_catalogue
        if catalogue is None:
            raise _Problem(
                "rule_type.catalogue_unavailable",
                ("context", "producer_catalogue_ref"),
                "Producer activation requires its pinned catalogue.",
                outcome="incomplete",
            )
        activation = next(
            (
                item
                for item in self.declarations
                if item.declaration_id == context.declaration_id
            ),
            None,
        )
        if activation is None or not (
            _same(catalogue.ref, context.producer_catalogue_ref)
            and context.producer_kind is activation.producer_kind
            and context.producer_scope is activation.scope
            and context.profile_ref == activation.profile_ref
        ):
            raise _Problem(
                "rule_type.activation_anchor",
                ("context", "declaration_id"),
                "Producer activation differs from the supplied declaration binding.",
            )
        active = activation.activation_condition
        declared = (
            active.condition_ast
            if isinstance(active, ToolConditionActivation)
            else True
        )
        if expr != declared:
            raise _Problem(
                "rule_type.activation_binding",
                (),
                "Condition differs from the pinned producer activation.",
            )
        if context.tool_plan_ref is not None:
            producer = activation.producer
            assert isinstance(producer, ToolStepProducer)
            if not (
                _same(producer.tool_plan_ref, context.tool_plan_ref)
                and producer.step_id == context.step_id
            ):
                raise _Problem(
                    "rule_type.activation_step",
                    ("context", "step_id"),
                    "Activation must use the declared ToolStep.",
                )
            self._tool_output(
                FactSelector(
                    scope=activation.scope,
                    fact_key=activation.fact_key,
                    fact_schema_ref=activation.fact_schema_ref,
                    field_path=(),
                    producer_selector=ToolProducerSelector(
                        producer_kind=producer.producer_kind,
                        tool_plan_ref=producer.tool_plan_ref,
                        step_id=producer.step_id,
                        tool_ref=producer.tool_ref,
                        output_name=activation.output_name,
                    ),
                ),
                ("context", "step_id"),
            )
        if isinstance(activation.producer, QuestionProducer):
            self._question_output(
                FactSelector(
                    scope=activation.scope,
                    fact_key=activation.fact_key,
                    fact_schema_ref=activation.fact_schema_ref,
                    field_path=(),
                    producer_selector=QuestionProducerSelector(
                        producer_kind=ProducerKind.VLM_QUESTION,
                        question_ref=activation.producer.question_ref,
                        output_name="primary",
                    ),
                ),
                ("context", "declaration_id"),
            )
        self.activation = activation

    def _check_domain(self, expr: ConditionExpr) -> None:
        for path, node in _occurrences(expr):
            if isinstance(
                node, (FactRefExpr, RecordFieldRefExpr, LocalToolFactRefExpr)
            ):
                if self.domain is ConditionInputDomain.RULE_APPLICABILITY:
                    raise _Problem(
                        "condition.applicability_fact_forbidden",
                        path,
                        "Applicability reads only declared Context metadata.",
                    )
                if (
                    isinstance(node, LocalToolFactRefExpr)
                    and self.domain is not ConditionInputDomain.TOOL_CONDITION
                ):
                    raise _Problem(
                        "condition.local_tool_domain",
                        path,
                        "Only Tool conditions can bind a local Tool Fact.",
                    )

    def _bind_nonrule(self, expr: ConditionExpr | Literal[True]) -> None:
        context = self.context
        assert not isinstance(context, RuleConditionContext)
        allowlist = self.allowlist if context.use_role == "applicability" else None

        def bind(tree: ConditionExpr | Literal[True]) -> ConditionReferenceResult:
            return validate_condition_references(
                tree,
                context,
                input_domain=self.domain,
                fact_schemas=self.fact_schemas,
                context_allowlist=allowlist,
                profiles=self.bindings.profiles,
                questions=self.bindings.questions,
                tools=self.bindings.tools,
                plans=self.bindings.plans,
            )

        result = bind(expr)
        status = result.report.aggregate_status
        if status in ("invalid", "incomplete"):
            issue = next(
                issue
                for check in result.report.checks
                if check.status == status
                for issue in check.issues
            )
            # The B diagnostics omit occurrence paths. Locate that issue only on
            # failed binding. Global directory identities remain checked by the full call.
            path: IssuePath = ("context",)
            occurrences = () if expr is True else _occurrences(expr)
            for candidate_path, leaf in occurrences:
                if isinstance(
                    leaf,
                    (
                        ContextRefExpr,
                        FactRefExpr,
                        RecordFieldRefExpr,
                        LocalToolFactRefExpr,
                    ),
                ):
                    single = bind(leaf)
                    if any(
                        item.code == issue.code
                        for check in single.report.checks
                        for item in check.issues
                    ):
                        path = candidate_path + ("selector",)
                        break
            outcome: _Outcome = "invalid" if status == "invalid" else "incomplete"
            raise _Problem(issue.code, path, issue.message, outcome=outcome)
        if expr is True:
            return
        exact: list[IssuePath] = []
        local: list[IssuePath] = []
        for path, node in _occurrences(expr):
            if isinstance(node, (FactRefExpr, RecordFieldRefExpr)):
                exact.append(path)
            elif isinstance(node, LocalToolFactRefExpr):
                local.append(path)
        self.projected.update(
            zip(exact + local, result.selectors_for_matching, strict=True)
        )

    def _question_output(self, selector: FactSelector, path: IssuePath) -> None:
        producer = selector.producer_selector
        assert isinstance(producer, QuestionProducerSelector)
        if self.profile is None or not any(
            _same(producer.question_ref, ref) for ref in self.profile.question_refs
        ):
            raise _Problem(
                "rule_type.foreign_question",
                path,
                "Question is outside the current Profile.",
            )
        question = _entry(
            producer.question_ref, self.bindings.questions, path
        ).definition
        if question.primary_fact_key != selector.fact_key or not _same(
            question.primary_fact_schema_ref, selector.fact_schema_ref
        ):
            raise _Problem(
                "rule_type.question_output_identity",
                path,
                "Selector must identify the Question's exact primary Fact.",
            )

    def _tool_output(self, selector: FactSelector, path: IssuePath) -> None:
        producer = selector.producer_selector
        assert isinstance(producer, ToolProducerSelector)
        if self.profile is None or not _same(
            self.profile.tool_plan_ref, producer.tool_plan_ref
        ):
            raise _Problem(
                "rule_type.foreign_plan",
                path,
                "Tool producer must use the current Profile's exact ToolPlan.",
            )
        plan = _entry(producer.tool_plan_ref, self.bindings.plans, path).definition
        step = next(
            (step for step in plan.steps if step.step_id == producer.step_id), None
        )
        if step is None or not _same(step.tool_ref, producer.tool_ref):
            raise _Problem(
                "rule_type.tool_step_identity",
                path,
                "The pinned Tool does not own the selected step.",
            )
        tool = _entry(producer.tool_ref, self.bindings.tools, path).definition
        output = next(
            (
                output
                for output in tool.fact_outputs
                if output.output_name == producer.output_name
            ),
            None,
        )
        if output is None or not (
            _same(output.fact_schema_ref, selector.fact_schema_ref)
            and output.scope is selector.scope
            and output.fact_key == selector.fact_key
            and any(
                mapping.output_name == producer.output_name
                and mapping.target_fact_key == selector.fact_key
                for mapping in step.fact_output_mappings
            )
        ):
            raise _Problem(
                "rule_type.tool_output_identity",
                path,
                "Selector must identify a mapped Tool output with its exact key and Schema.",
            )

    def _fact(self, selector: FactSelector, path: IssuePath) -> _Type:
        selector_path = path + ("selector",)
        producer = selector.producer_selector
        try:
            definition = self.fact_schemas.resolve(selector.fact_schema_ref)
            field = self.fact_schemas.describe_type(
                selector.fact_schema_ref, selector.field_path
            )
        except RegistryValidationError as exc:
            raise _schema_problem(
                exc,
                selector_path + ("field_path",)
                if selector.field_path
                else selector_path,
            ) from exc
        if (
            definition.scope is not selector.scope
            or producer.producer_kind not in definition.allowed_producer_kinds
            or (selector.scope is FactScope.OBJECT and self.context.profile_ref is None)
        ):
            raise _Problem(
                "rule_type.fact_scope",
                selector_path,
                "Fact Schema, producer kind and current scope must agree.",
            )
        if isinstance(producer, QuestionProducerSelector):
            self._question_output(selector, selector_path)
        elif isinstance(producer, ToolProducerSelector):
            self._tool_output(selector, selector_path)
        self._producer(selector, selector_path)
        unit = None
        if isinstance(definition.units, DeclaredUnitMetadata):
            unit = next(
                (
                    binding.unit
                    for binding in definition.units.bindings
                    if binding.field_path == selector.field_path
                ),
                None,
            )
            if (
                unit is None
                and not isinstance(field.type_definition, RecordTypeDefinition)
                and any(
                    selector.field_path[: len(binding.field_path)] == binding.field_path
                    for binding in definition.units.bindings
                )
            ):
                raise _Problem(
                    "rule_type.unit_path_unavailable",
                    selector_path + ("field_path",),
                    "The containing Record's unit does not declare the selected field's unit.",
                    outcome="incomplete",
                )
        return _Type(
            field.type_definition,
            unit,
            reference_identity(selector.fact_schema_ref),
            selector.field_path,
        )

    def _producer(self, selector: FactSelector, path: IssuePath) -> None:
        if self.bindings.producer_catalogue is None:
            raise _Problem(
                "rule_type.catalogue_unavailable",
                path,
                "Fact selector requires a finite exact Producer catalogue.",
                outcome="incomplete",
            )
        profile = (
            self.context.profile_ref if selector.scope is FactScope.OBJECT else None
        )
        matches = [
            item
            for item in self.declarations
            if item.scope is selector.scope
            and item.profile_ref == profile
            and item.fact_key == selector.fact_key
        ]
        if not matches:
            raise _Problem(
                "rule_type.producer_unavailable",
                path,
                "No Producer declares this Fact key in the current scope.",
                outcome="incomplete",
            )
        if len(matches) != 1:
            raise _Problem(
                "rule_type.producer_ambiguous",
                path,
                "Fact key has more than one Producer in the current scope.",
            )
        declaration = matches[0]
        producer = selector.producer_selector
        actual = declaration.producer
        if isinstance(producer, QuestionProducerSelector) and isinstance(
            actual, QuestionProducer
        ):
            owner_matches = _same(producer.question_ref, actual.question_ref)
        elif isinstance(producer, ToolProducerSelector) and isinstance(
            actual, ToolStepProducer
        ):
            owner_matches = (
                _same(producer.tool_plan_ref, actual.tool_plan_ref)
                and producer.step_id == actual.step_id
                and _same(producer.tool_ref, actual.tool_ref)
            )
        elif isinstance(producer, SystemProducerSelector) and isinstance(
            actual, SystemProducer
        ):
            owner_matches = requirements_agree(producer.requirement, actual.requirement)
        else:
            owner_matches = False
        if not (
            owner_matches
            and producer.output_name == declaration.output_name
            and _same(selector.fact_schema_ref, declaration.fact_schema_ref)
        ):
            raise _Problem(
                "rule_type.producer_mismatch",
                path,
                "Selector differs from the pinned Producer output identity or Schema.",
            )
        if self.domain is ConditionInputDomain.TOOL_CONDITION:
            self._prior(declaration, path)

    def _prior(self, declaration: FactProducerDeclaration, path: IssuePath) -> None:
        context = self.context
        if self.activation == declaration:
            raise _Problem(
                "rule_type.self_producer",
                path,
                "Producer activation cannot consume its own output.",
            )
        if (
            self.activation is not None
            and isinstance(self.activation.producer, QuestionProducer)
            and not isinstance(declaration.producer, SystemProducer)
        ):
            raise _Problem(
                "rule_type.question_producer_not_prior",
                path,
                "N04 activation cannot read peer Question outputs or later Tool outputs.",
            )
        if (
            isinstance(
                context,
                (ToolStepGuardConditionContext, ProducerActivationConditionContext),
            )
            and context.tool_plan_ref is not None
            and isinstance(declaration.producer, (QuestionProducer, ToolStepProducer))
        ):
            # N04 precedes N05. B bound the Tool output to a strict local ancestor.
            return
        if (
            declaration.declaration_id
            not in self.bindings.prior_producer_declaration_ids
        ):
            raise _Problem(
                "rule_type.prior_producer_unavailable",
                path,
                "The type context establishes no prior availability for this Producer.",
                outcome="incomplete",
            )

    def _context_field(self, selector: ContextFieldSelector, path: IssuePath) -> _Type:
        path += ("selector",)
        if self.allowlist is not None and selector not in self.allowlist:
            raise _Problem(
                "condition.context_not_allowlisted",
                path,
                "Context selector is outside the declared allowlist.",
            )
        matches = [
            item
            for item in self.context.context_registrations
            if item.source == selector.source.value
        ]
        if not matches:
            raise _Problem(
                "condition.context_unavailable",
                path,
                "Context source is not registered for this use.",
                outcome="incomplete",
            )
        if len(matches) != 1 or matches[0].schema_ref != selector.schema_ref:
            raise _Problem(
                "condition.context_identity",
                path,
                "Context source is ambiguous or the exact Schema differs.",
            )
        registration = matches[0]
        if (
            isinstance(registration, RegionContextRegistration)
            and self.context.region_ref is None
        ):
            raise _Problem(
                "condition.region_unavailable",
                path,
                "Region context requires its pinned Region.",
                outcome="incomplete",
            )
        if (
            isinstance(registration, ProfileContextRegistration)
            and registration.profile_ref != self.context.profile_ref
        ) or (
            isinstance(registration, RegionContextRegistration)
            and registration.region_ref != self.context.region_ref
        ):
            raise _Problem(
                "condition.foreign_context",
                path,
                "Context source belongs to a foreign Profile or Region.",
            )
        try:
            descriptor = resolve_descriptor(
                selector.schema_ref, self.context.descriptors
            )
            field = describe_field(descriptor, selector.field_path)
        except SchemaCheckError as exc:
            raise _schema_problem(exc, path + ("field_path",)) from exc
        unit = self.context_units.get(selector)
        shape = field.type_definition
        target = shape.item_type if isinstance(shape, ListTypeDefinition) else shape
        if unit is not None and not isinstance(
            target, (IntegerTypeDefinition, NumberTypeDefinition)
        ):
            raise _Problem(
                "rule_type.context_unit_kind",
                path,
                "Context unit binding requires a numeric field or numeric list.",
            )
        return _Type(
            shape, unit, reference_identity(selector.schema_ref), selector.field_path
        )

    def _infer(self, expr: ConditionExpr, path: IssuePath = ()) -> _Type:
        if isinstance(expr, LiteralExpr):
            shape = expr.constant.type_definition
            value = expr.constant.value
            count = len(value.value) if isinstance(value, ListValue) else None
            unit = self.literal_units.pop(path, None)
            if unit is not None:
                target = (
                    shape.item_type if isinstance(shape, ListTypeDefinition) else shape
                )
                if not isinstance(
                    target, (IntegerTypeDefinition, NumberTypeDefinition)
                ):
                    raise _Problem(
                        "rule_type.literal_unit_kind",
                        path,
                        "Unit binding requires a numeric literal or numeric list.",
                    )
            return _Type(shape, unit, literal_count=count)
        if isinstance(expr, ContextRefExpr):
            return self._context_field(expr.selector, path)
        if isinstance(expr, (FactRefExpr, RecordFieldRefExpr, LocalToolFactRefExpr)):
            if isinstance(self.context, RuleConditionContext):
                assert not isinstance(expr, LocalToolFactRefExpr)
                return self._fact(expr.selector, path)
            return self._fact(self.projected[path], path)
        return _signature(
            expr,
            tuple(self._infer(child, path + key) for key, child in _args(expr)),
            path,
        )


def check_condition_types(
    condition: object,
    *,
    input_domain: ConditionInputDomain,
    context: RuleTypeUseContext,
    bindings: RuleTypeBindings,
    fact_schemas: FactSchemaRegistry,
    context_allowlist: tuple[ContextFieldSelector, ...] | None = None,
) -> TypeCheckResult:
    """Type-check one explicit use. Rule conditions must supply their RuleDefinition.

    Pins and prior-availability declarations come from the caller. For a supplied
    Policy directory, the expression must also match its registered body. Without
    that directory, the caller supplies the candidate body for the pinned use.

    Equality and ordering operate on supported scalar types. Enum literals use
    the other operand's exact Schema and declared members. Single-use unit
    bindings supplement the shared wire format. A pass certifies finite type
    checking only.
    """
    domain = ConditionInputDomain._decode(input_domain)
    prefix: IssuePath = ("context",)
    try:
        context = TypeAdapter[RuleTypeUseContext](RuleTypeUseContext).validate_python(
            context
        )
        prefix = ("bindings",)
        bindings = RuleTypeBindings.model_validate(bindings)
        if not isinstance(fact_schemas, FactSchemaRegistry):
            raise _Problem(
                "rule_type.registry_unavailable",
                ("fact_schemas",),
                "An explicit immutable Fact Schema Registry is required.",
                outcome="incomplete",
            )
        if context_allowlist is not None:
            if type(context_allowlist) is not tuple or len(context_allowlist) > 1024:
                raise _Problem(
                    "rule_type.allowlist_budget",
                    ("context_allowlist",),
                    "The allowlist requires a bounded immutable sequence.",
                )
            prefix = ("context_allowlist",)
            context_allowlist = tuple(
                ContextFieldSelector.model_validate(item) for item in context_allowlist
            )
        prefix = ()
        inferred = _Checker(
            domain, context, bindings, fact_schemas, context_allowlist
        ).check(condition)
    except _Problem as exc:
        return FailedTypeCheck(
            status=exc.outcome, input_domain=domain, issues=(exc.issue,)
        )
    except ConditionDataError as exc:
        issue = ValidationIssue(
            code=exc.code,
            path=(),
            message="Condition data violates a structural constraint.",
            related_refs=(),
        )
        return FailedTypeCheck(status="invalid", input_domain=domain, issues=(issue,))
    except ValidationError as exc:
        diagnostics = []
        for error in exc.errors(
            include_url=False, include_input=False, include_context=False
        )[:64]:
            location = prefix + tuple(
                (segment[:128] or "field") if isinstance(segment, str) else segment
                for segment in error["loc"]
            )
            if not location and error["type"] in (
                "union_tag_invalid",
                "union_tag_not_found",
            ):
                location = ("kind",)
            diagnostics.append(
                _Problem(
                    "rule_type.invalid_contract",
                    location,
                    "Input violates a strict condition or type context contract.",
                ).issue
            )
        return FailedTypeCheck(
            status="invalid", input_domain=domain, issues=tuple(diagnostics)
        )
    return PassedTypeCheck(status="passed", input_domain=domain, inferred_type=inferred)
