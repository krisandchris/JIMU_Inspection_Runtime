"""Resolve prepared operand slots from one execution's selected inputs."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.conditions import MAX_CONDITION_NODES
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    ContextFieldSelector,
    FieldPath,
    reference_identity,
)
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    ProducerCatalogueContentRef,
    QuestionProducerSelector,
    SystemProducerSelector,
    ToolProducerSelector,
)
from jimu_runtime.domain.primitives import ArtifactId, RecordId
from jimu_runtime.domain.runtime.facts import (
    ErrorFact,
    FactRecord,
    KnownFact,
    NotApplicableFact,
    NotEvaluatedFact,
    QuestionFactSource,
    SystemFactSource,
    ToolFactSource,
    UnknownFact,
)
from jimu_runtime.domain.tool_mappings import ProfileContentRef
from jimu_runtime.domain.validation.producers import requirements_agree
from jimu_runtime.domain.values import (
    MAX_CONTAINER_DEPTH,
    MAX_LIST_ITEMS,
    MAX_RECORD_FIELDS,
    BooleanValue,
    EnumValue,
    IntegerValue,
    ListValue,
    NumberValue,
    RecordValue,
    StringValue,
    TypeDefinition,
    Value,
    validate_typed_value,
)

_FACT = TypeAdapter[FactRecord](FactRecord)
_ID = TypeAdapter[RecordId](RecordId)
_MAX_VALUE_NODES = 32_768
_MISSING_VALUE = object()
_SCALARS = (BooleanValue, EnumValue, IntegerValue, NumberValue, StringValue)
_FACT_TYPES = (KnownFact, UnknownFact, ErrorFact, NotApplicableFact, NotEvaluatedFact)

type FactKey = tuple[FactScope, ArtifactId]


class PredicateInputError(ValueError):
    """An invalid use or inconsistent input batch, with a bounded diagnostic code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FactSlot:
    producer: QuestionProducerSelector | ToolProducerSelector | SystemProducerSelector
    catalogue_ref: ProducerCatalogueContentRef
    declaration_id: ArtifactId
    schema_ref: FactSchemaContentRef
    shape: TypeDefinition

    def accepts_source(self, fact: FactRecord) -> bool:
        source, producer = fact.source, self.producer
        if isinstance(source, QuestionFactSource) and isinstance(
            producer, QuestionProducerSelector
        ):
            return source.question_ref == producer.question_ref
        if isinstance(source, ToolFactSource) and isinstance(
            producer, ToolProducerSelector
        ):
            return (
                source.tool_plan_ref == producer.tool_plan_ref
                and source.step_id == producer.step_id
                and source.tool_ref == producer.tool_ref
                and source.output_name == producer.output_name
            )
        if isinstance(source, SystemFactSource) and isinstance(
            producer, SystemProducerSelector
        ):
            return source.output_name == producer.output_name and requirements_agree(
                source.requirement, producer.requirement
            )
        return False


@dataclass(frozen=True, slots=True)
class Operand:
    value: Value | None
    diagnostics: tuple[ArtifactId, ...] = ()


class _ValueBudget:
    """Count expanded occurrences before recursive contract revalidation."""

    def __init__(self) -> None:
        self.remaining = _MAX_VALUE_NODES

    def check(self, value: object) -> None:
        pending = [(value, 0)]
        while pending:
            node, depth = pending.pop()
            self.remaining -= 1
            if self.remaining < 0 or depth > MAX_CONTAINER_DEPTH:
                raise PredicateInputError("predicate.value_budget")
            if isinstance(node, (ListValue, RecordValue)):
                payload: object = getattr(node, "value", _MISSING_VALUE)
                if payload is _MISSING_VALUE:
                    raise PredicateInputError("predicate.invalid_value_shape")
                if isinstance(node, ListValue):
                    if type(payload) is not tuple or len(payload) > MAX_LIST_ITEMS:
                        raise PredicateInputError("predicate.value_budget")
                    pending.extend((child, depth + 1) for child in payload)
                else:
                    if (
                        not isinstance(payload, Mapping)
                        or len(payload) > MAX_RECORD_FIELDS
                    ):
                        raise PredicateInputError("predicate.value_budget")
                    pending.extend((child, depth + 1) for child in payload.values())
            elif not isinstance(node, _SCALARS):
                raise PredicateInputError("predicate.invalid_value_shape")


def _fact_operand(fact: FactRecord, slot: FactSlot) -> Operand:
    if not isinstance(fact, KnownFact):
        reason = fact.reason
        diagnostics: tuple[ArtifactId, ...] = (
            f"predicate.fact.{fact.status.value}",
            reason.reason_code,
        )
        if isinstance(fact, ErrorFact):
            diagnostics += tuple(item.code for item in fact.reason.diagnostics)
        return Operand(None, diagnostics)
    try:
        return Operand(validate_typed_value(slot.shape, fact.value))
    except ValueError:
        return Operand(None, ("predicate.fact.invalid_value",))


def project_fact(operand: Operand, path: FieldPath) -> Operand:
    value = operand.value
    if value is None:
        return operand
    for name in path:
        assert isinstance(value, RecordValue)
        value = value.value.get(name)
        if value is None:
            return Operand(None, ("predicate.fact.missing_field",))
    return Operand(value)


def read_operands(
    *,
    execution_id: RecordId,
    object_id: RecordId | None,
    profile_ref: ProfileContentRef | None,
    context_shapes: Mapping[ContextFieldSelector, TypeDefinition],
    context_fields: Mapping[ContextFieldSelector, Value] | None,
    fact_slots: Mapping[FactKey, FactSlot],
    facts: tuple[FactRecord, ...],
) -> tuple[dict[ContextFieldSelector, Operand], dict[FactKey, Operand]]:
    try:
        execution_id = _ID.validate_python(execution_id)
        if object_id is not None:
            object_id = _ID.validate_python(object_id)
    except ValidationError:
        raise PredicateInputError("predicate.invalid_scope") from None
    if object_id is None and any(scope is FactScope.OBJECT for scope, _ in fact_slots):
        raise PredicateInputError("predicate.object_scope_required")
    if type(facts) is not tuple or len(facts) > MAX_CONDITION_NODES:
        raise PredicateInputError("predicate.fact_inputs")

    budget = _ValueBudget()
    context_operands: dict[ContextFieldSelector, Operand] = {}
    if context_fields is not None:
        if not isinstance(context_fields, Mapping) or len(context_fields) > len(
            context_shapes
        ):
            raise PredicateInputError("predicate.context_inputs")
        for selector, value in context_fields.items():
            shape = context_shapes.get(selector)
            if shape is None:
                raise PredicateInputError("predicate.unexpected_context_selector")
            if not isinstance(value, (*_SCALARS, ListValue, RecordValue)):
                context_operands[selector] = Operand(
                    None, ("predicate.context.invalid_value",)
                )
                continue
            budget.check(value)
            try:
                context_operands[selector] = Operand(validate_typed_value(shape, value))
            except ValueError:
                context_operands[selector] = Operand(
                    None, ("predicate.context.invalid_value",)
                )

    fact_operands: dict[FactKey, Operand] = {}
    for raw in facts:
        if not isinstance(raw, _FACT_TYPES):
            raise PredicateInputError("predicate.invalid_fact_record")
        if isinstance(raw, KnownFact):
            payload: object = getattr(raw, "value", _MISSING_VALUE)
            if payload is _MISSING_VALUE:
                raise PredicateInputError("predicate.invalid_fact_record")
            budget.check(payload)
        try:
            fact = _FACT.validate_python(raw)
        except ValidationError:
            raise PredicateInputError("predicate.invalid_fact_record") from None
        key = (fact.scope, fact.fact_key)
        slot = fact_slots.get(key)
        if slot is None:
            raise PredicateInputError("predicate.unexpected_fact")
        if key in fact_operands:
            raise PredicateInputError("predicate.duplicate_fact")
        if fact.execution_id != execution_id or (
            fact.scope is FactScope.OBJECT
            and (
                fact.object_id != object_id
                or fact.profile_ref is None
                or profile_ref is None
                or reference_identity(fact.profile_ref)
                != reference_identity(profile_ref)
            )
        ):
            raise PredicateInputError("predicate.foreign_fact_scope")
        if (
            fact.fact_schema_ref != slot.schema_ref
            or fact.producer_catalogue_ref != slot.catalogue_ref
            or fact.producer_declaration_id != slot.declaration_id
            or not slot.accepts_source(fact)
        ):
            raise PredicateInputError("predicate.fact_identity_mismatch")
        fact_operands[key] = _fact_operand(fact, slot)
    return context_operands, fact_operands
