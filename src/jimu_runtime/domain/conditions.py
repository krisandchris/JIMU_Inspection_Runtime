"""Shared condition data, without inference, evaluation or an operator registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    ComparisonOperator,
    ConditionInputDomain,
    FactScope,
)
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    FieldPath,
    OptionalContract,
    immutable_sequence,
)
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    FactSelector,
    ToolContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.values import (
    BooleanValue,
    ListTypeDefinition,
    ListValue,
    RecordTypeDefinition,
    ValueContract,
)

MAX_CONDITION_DEPTH = 32
MAX_CONDITION_NODES = 512
MAX_CONDITION_ARGUMENTS = 64


class ConditionDataError(ValueError):
    """A bounded structure code; contains no expression dump or truth assertion."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _payload(value: object) -> object:
    if not isinstance(value, BaseModel):
        return value
    data: dict[str, object] = dict(value.__dict__)
    if value.__pydantic_extra__:
        if data.keys() & value.__pydantic_extra__.keys():
            raise ConditionDataError("condition.conflicting_instance_fields")
        data.update(value.__pydantic_extra__)
    if isinstance(value, OptionalContract):
        for name, field in type(value).model_fields.items():
            if (
                field.default is None
                and name not in value.model_fields_set
                and data.get(name) is None
            ):
                data.pop(name, None)
    return data


def _raw_children(value: object) -> tuple[object, ...]:
    data = _payload(value)
    if not isinstance(data, dict):
        return ()
    kind = data.get("kind")
    if kind in ("all", "any"):
        arguments = data.get("arguments")
        if type(arguments) in (tuple, list):
            sequence = cast(Sequence[object], arguments)
            if len(sequence) > MAX_CONDITION_ARGUMENTS:
                raise ConditionDataError("condition.arguments_limit")
            return tuple(sequence)
        return ()
    if kind == "comparison":
        return (data.get("left"), data.get("right"))
    if kind == "in":
        return (data.get("item"), data.get("collection"))
    if kind == "not":
        return (data.get("argument"),)
    return ()


def _prewalk(value: object) -> None:
    # The ancestor set is intentionally not a global visited set: every shared
    # subtree occurrence consumes the expression budget independently.
    active: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(value, 1, False)]
    occurrences = 0
    while stack:
        node, depth, leaving = stack.pop()
        identity = id(node)
        if leaving:
            active.remove(identity)
            continue
        if identity in active:
            raise ConditionDataError("condition.cycle")
        if depth > MAX_CONDITION_DEPTH:
            raise ConditionDataError("condition.depth_limit")
        occurrences += 1
        if occurrences > MAX_CONDITION_NODES:
            raise ConditionDataError("condition.node_limit")
        children = _raw_children(node)
        active.add(identity)
        stack.append((node, depth, True))
        stack.extend((child, depth + 1, False) for child in reversed(children))


def _snapshot(value: object, active: set[int], depth: int = 0) -> object:
    # Every valid expression is <=32 levels, with A containers <=8 levels.
    # This secondary raw-container guard only rejects malformed nesting that
    # cannot fit either approved language, before Python recursion can overflow.
    if depth > 128:
        raise ConditionDataError("condition.invalid_raw_nesting")
    if not isinstance(value, (BaseModel, Mapping, list, tuple)):
        return value
    identity = id(value)
    if identity in active:
        raise ConditionDataError("condition.cycle")
    active.add(identity)
    try:
        data = _payload(value)
        if isinstance(data, Mapping):
            return {
                key: _snapshot(item, active, depth + 1) for key, item in data.items()
            }
        if isinstance(data, (list, tuple)):
            converted = [_snapshot(item, active, depth + 1) for item in data]
            return tuple(converted) if isinstance(data, tuple) else converted
        return data
    finally:
        active.remove(identity)


class _ConditionNode(ContractModel):
    @model_validator(mode="before")
    @classmethod
    def bounded_payload(cls, value: object) -> object:
        _prewalk(value)
        return _snapshot(value, set())


class LiteralExpr(_ConditionNode):
    kind: Literal["literal"]
    constant: ValueContract

    @model_validator(mode="after")
    def scalar_list_language(self) -> Self:
        pending = [self.constant.type_definition]
        while pending:
            definition = pending.pop()
            if isinstance(definition, RecordTypeDefinition):
                raise ConditionDataError("condition.record_literal_forbidden")
            if isinstance(definition, ListTypeDefinition):
                pending.append(definition.item_type)
        return self


class FactRefExpr(_ConditionNode):
    kind: Literal["fact_ref"]
    selector: FactSelector

    @model_validator(mode="after")
    def whole_value(self) -> Self:
        if self.selector.field_path:
            raise ConditionDataError("condition.fact_ref_requires_whole_value")
        return self


class RecordFieldRefExpr(_ConditionNode):
    kind: Literal["record_field_ref"]
    selector: FactSelector

    @model_validator(mode="after")
    def selected_field(self) -> Self:
        if not self.selector.field_path:
            raise ConditionDataError("condition.record_field_requires_path")
        return self


class ContextRefExpr(_ConditionNode):
    kind: Literal["context_ref"]
    selector: ContextFieldSelector


class LocalToolFactSelector(ContractModel):
    scope: Literal[FactScope.OBJECT]
    step_id: ArtifactId
    tool_ref: ToolContentRef
    output_name: ArtifactId
    fact_key: ArtifactId
    fact_schema_ref: FactSchemaContentRef
    field_path: FieldPath

    @model_validator(mode="before")
    @classmethod
    def decode_scope(cls, value: object) -> object:
        data = _payload(value)
        if isinstance(data, dict) and "scope" in data:
            return {**data, "scope": FactScope._decode(data["scope"])}
        return data


class LocalToolFactRefExpr(_ConditionNode):
    kind: Literal["local_tool_fact_ref"]
    selector: LocalToolFactSelector


class ComparisonExpr(_ConditionNode):
    kind: Literal["comparison"]
    operator: ComparisonOperator
    left: ConditionExpr
    right: ConditionExpr


class InExpr(_ConditionNode):
    kind: Literal["in"]
    item: ConditionExpr
    collection: ConditionExpr

    @model_validator(mode="after")
    def literal_collection_budget(self) -> Self:
        if isinstance(self.collection, LiteralExpr) and isinstance(
            self.collection.constant.value, ListValue
        ):
            if (
                not 1
                <= len(self.collection.constant.value.value)
                <= MAX_CONDITION_ARGUMENTS
            ):
                raise ConditionDataError("condition.in_literal_size")
        return self


class AllExpr(_ConditionNode):
    kind: Literal["all"]
    arguments: Annotated[
        tuple[ConditionExpr, ...],
        Field(min_length=1, max_length=MAX_CONDITION_ARGUMENTS),
        BeforeValidator(immutable_sequence),
    ]


class AnyExpr(_ConditionNode):
    kind: Literal["any"]
    arguments: Annotated[
        tuple[ConditionExpr, ...],
        Field(min_length=1, max_length=MAX_CONDITION_ARGUMENTS),
        BeforeValidator(immutable_sequence),
    ]


class NotExpr(_ConditionNode):
    kind: Literal["not"]
    argument: ConditionExpr


type ConditionExpr = Annotated[
    LiteralExpr
    | FactRefExpr
    | RecordFieldRefExpr
    | ContextRefExpr
    | LocalToolFactRefExpr
    | ComparisonExpr
    | InExpr
    | AllExpr
    | AnyExpr
    | NotExpr,
    Field(discriminator="kind"),
]


class ConditionReferences(ContractModel):
    context_selectors: Annotated[
        tuple[ContextFieldSelector, ...],
        Field(max_length=512),
        BeforeValidator(immutable_sequence),
    ]
    fact_selectors: Annotated[
        tuple[FactSelector, ...],
        Field(max_length=512),
        BeforeValidator(immutable_sequence),
    ]
    local_tool_selectors: Annotated[
        tuple[LocalToolFactSelector, ...],
        Field(max_length=512),
        BeforeValidator(immutable_sequence),
    ]


def _validated_expr(raw: object) -> ConditionExpr:
    _prewalk(raw)
    return TypeAdapter[ConditionExpr](ConditionExpr).validate_python(
        _snapshot(raw, set())
    )


def _children(expr: ConditionExpr) -> tuple[ConditionExpr, ...]:
    if isinstance(expr, (AllExpr, AnyExpr)):
        return expr.arguments
    if isinstance(expr, ComparisonExpr):
        return (expr.left, expr.right)
    if isinstance(expr, InExpr):
        return (expr.item, expr.collection)
    if isinstance(expr, NotExpr):
        return (expr.argument,)
    return ()


def validate_condition_data(
    raw: object, *, input_domain: ConditionInputDomain
) -> ConditionExpr:
    """Validate data/domain only. A returned tree is NOT expression-type certification."""
    domain = ConditionInputDomain._decode(input_domain)
    expr = _validated_expr(raw)
    if isinstance(expr, LiteralExpr) and not isinstance(
        expr.constant.value, BooleanValue
    ):
        raise ConditionDataError("condition.nonboolean_literal_root")
    pending = [expr]
    while pending:
        node = pending.pop()
        if domain is ConditionInputDomain.RULE_APPLICABILITY and isinstance(
            node, (FactRefExpr, RecordFieldRefExpr, LocalToolFactRefExpr)
        ):
            raise ConditionDataError("condition.applicability_fact_forbidden")
        if domain is not ConditionInputDomain.TOOL_CONDITION and isinstance(
            node, LocalToolFactRefExpr
        ):
            raise ConditionDataError("condition.local_tool_domain")
        pending.extend(reversed(_children(node)))
    return expr


def condition_references(expr: ConditionExpr) -> ConditionReferences:
    """Preorder occurrences, without resolving selectors or evaluating any predicate."""
    tree = _validated_expr(expr)
    contexts: list[ContextFieldSelector] = []
    facts: list[FactSelector] = []
    local: list[LocalToolFactSelector] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ContextRefExpr):
            contexts.append(node.selector)
        elif isinstance(node, (FactRefExpr, RecordFieldRefExpr)):
            facts.append(node.selector)
        elif isinstance(node, LocalToolFactRefExpr):
            local.append(node.selector)
        pending.extend(reversed(_children(node)))
    return ConditionReferences(
        context_selectors=tuple(contexts),
        fact_selectors=tuple(facts),
        local_tool_selectors=tuple(local),
    )


for _node in (ComparisonExpr, InExpr, AllExpr, AnyExpr, NotExpr):
    _node.model_rebuild()
