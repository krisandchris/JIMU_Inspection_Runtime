"""Bounded typed payload algebra and local type conformance, without a Registry.

Bare Value models describe payload shape. ValueContract binds that shape to an
explicit TypeDefinition: enum membership, list item contracts and closed Record
fields are never inferred from a value or supplied by an untrusted payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BeforeValidator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

from jimu_runtime.domain.base import ContractModel

MAX_STRING_LENGTH = 4096
MAX_LIST_ITEMS = 256
MAX_RECORD_FIELDS = 64
MAX_CONTAINER_DEPTH = 8

type BoundedString = Annotated[
    str, StringConstraints(strict=True, max_length=MAX_STRING_LENGTH)
]
type FieldName = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=128)
]


def _exact_float(value: object) -> object:
    if type(value) is not float:
        raise ValueError("number requires a float, without integer/boolean coercion")
    return value


def _immutable_sequence(value: object) -> object:
    if type(value) not in (list, tuple):
        raise ValueError("expected an array (list/tuple), not another iterable")
    return tuple(cast(Sequence[object], value))


type FiniteNumber = Annotated[
    float, BeforeValidator(_exact_float), Field(allow_inf_nan=False)
]
type Size = Annotated[StrictInt, Field(ge=0)]


class BooleanValue(ContractModel):
    kind: Literal["bool"]
    value: StrictBool


class IntegerValue(ContractModel):
    kind: Literal["integer"]
    value: StrictInt


class NumberValue(ContractModel):
    kind: Literal["number"]
    value: FiniteNumber


class StringValue(ContractModel):
    kind: Literal["string"]
    value: BoundedString


class EnumValue(ContractModel):
    kind: Literal["enum"]
    value: BoundedString


class ListValue(ContractModel):
    kind: Literal["list"]
    value: Annotated[
        tuple[Value, ...],
        Field(max_length=MAX_LIST_ITEMS),
        BeforeValidator(_immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if len({item.kind for item in self.value}) > 1:
            raise ValueError("list items must have a homogeneous kind")
        _check_value_depth(self)
        return self


class RecordValue(ContractModel):
    kind: Literal["record"]
    value: Annotated[Mapping[FieldName, Value], Field(max_length=MAX_RECORD_FIELDS)]

    @field_validator("value")
    @classmethod
    def freeze_fields(cls, value: Mapping[str, Value]) -> Mapping[str, Value]:
        return MappingProxyType(dict(value))

    @field_serializer("value")
    def serialize_fields(self, value: Mapping[str, Value]) -> dict[str, Value]:
        return dict(value)

    @model_validator(mode="after")
    def check_depth(self) -> Self:
        _check_value_depth(self)
        return self


type Value = Annotated[
    BooleanValue
    | IntegerValue
    | NumberValue
    | StringValue
    | EnumValue
    | ListValue
    | RecordValue,
    Field(discriminator="kind"),
]


def _check_value_depth(value: Value, depth: int = 0) -> None:
    if isinstance(value, (ListValue, RecordValue)):
        depth += 1
        if depth > MAX_CONTAINER_DEPTH:
            raise ValueError("value exceeds maximum container depth")
        children = value.value if isinstance(value, ListValue) else value.value.values()
        for child in children:
            _check_value_depth(child, depth)


class BooleanTypeDefinition(ContractModel):
    kind: Literal["bool"]


class IntegerTypeDefinition(ContractModel):
    kind: Literal["integer"]
    minimum: StrictInt | None = None
    maximum: StrictInt | None = None

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        _check_bounds(self.minimum, self.maximum)
        return self


class NumberTypeDefinition(ContractModel):
    kind: Literal["number"]
    minimum: StrictInt | FiniteNumber | None = None
    maximum: StrictInt | FiniteNumber | None = None

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        _check_bounds(self.minimum, self.maximum)
        return self


class StringTypeDefinition(ContractModel):
    kind: Literal["string"]
    min_length: Annotated[Size, Field(le=MAX_STRING_LENGTH)] = 0
    max_length: Annotated[Size, Field(le=MAX_STRING_LENGTH)] = MAX_STRING_LENGTH

    @model_validator(mode="after")
    def check_bounds(self) -> Self:
        _check_bounds(self.min_length, self.max_length)
        return self


class EnumTypeDefinition(ContractModel):
    kind: Literal["enum"]
    members: Annotated[
        tuple[BoundedString, ...],
        Field(min_length=1, max_length=MAX_LIST_ITEMS),
        BeforeValidator(_immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        if len(self.members) != len(set(self.members)):
            raise ValueError("enum members must be unique")
        return self


class ListTypeDefinition(ContractModel):
    kind: Literal["list"]
    item_type: TypeDefinition
    min_items: Annotated[Size, Field(le=MAX_LIST_ITEMS)] = 0
    max_items: Annotated[Size, Field(le=MAX_LIST_ITEMS)] = MAX_LIST_ITEMS

    @model_validator(mode="after")
    def check_constraints(self) -> Self:
        _check_bounds(self.min_items, self.max_items)
        _check_type_depth(self)
        return self


class RecordFieldDefinition(ContractModel):
    name: FieldName
    required: StrictBool
    type_definition: TypeDefinition


class RecordTypeDefinition(ContractModel):
    kind: Literal["record"]
    fields: Annotated[
        tuple[RecordFieldDefinition, ...],
        Field(max_length=MAX_RECORD_FIELDS),
        BeforeValidator(_immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_fields(self) -> Self:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("record field names must be unique")
        _check_type_depth(self)
        return self


type TypeDefinition = Annotated[
    BooleanTypeDefinition
    | IntegerTypeDefinition
    | NumberTypeDefinition
    | StringTypeDefinition
    | EnumTypeDefinition
    | ListTypeDefinition
    | RecordTypeDefinition,
    Field(discriminator="kind"),
]


def _check_bounds(minimum: int | float | None, maximum: int | float | None) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum must not exceed maximum")


def _check_type_depth(definition: TypeDefinition, depth: int = 0) -> None:
    if isinstance(definition, (ListTypeDefinition, RecordTypeDefinition)):
        depth += 1
        if depth > MAX_CONTAINER_DEPTH:
            raise ValueError("type exceeds maximum container depth")
        if isinstance(definition, ListTypeDefinition):
            _check_type_depth(definition.item_type, depth)
        else:
            for field in definition.fields:
                _check_type_depth(field.type_definition, depth)


def _conform(definition: TypeDefinition, value: Value) -> None:
    if definition.kind != value.kind:
        raise ValueError(f"expected {definition.kind}, received {value.kind}")
    if isinstance(definition, (IntegerTypeDefinition, NumberTypeDefinition)):
        if not isinstance(value, (IntegerValue, NumberValue)):
            raise ValueError("numeric value required")
        if definition.minimum is not None and value.value < definition.minimum:
            raise ValueError("value is below minimum")
        if definition.maximum is not None and value.value > definition.maximum:
            raise ValueError("value exceeds maximum")
    elif isinstance(definition, StringTypeDefinition) and isinstance(
        value, StringValue
    ):
        if not definition.min_length <= len(value.value) <= definition.max_length:
            raise ValueError("string length violates declared bounds")
    elif isinstance(definition, EnumTypeDefinition) and isinstance(value, EnumValue):
        if value.value not in definition.members:
            raise ValueError("value is not a declared enum member")
    elif isinstance(definition, ListTypeDefinition) and isinstance(value, ListValue):
        if not definition.min_items <= len(value.value) <= definition.max_items:
            raise ValueError("list length violates declared bounds")
        for item in value.value:
            _conform(definition.item_type, item)
    elif isinstance(definition, RecordTypeDefinition) and isinstance(
        value, RecordValue
    ):
        fields = {field.name: field for field in definition.fields}
        if set(value.value) - fields.keys():
            raise ValueError("record contains undeclared fields")
        for name, field in fields.items():
            if name not in value.value:
                if field.required:
                    raise ValueError(f"record missing required field: {name}")
            else:
                _conform(field.type_definition, value.value[name])


class ValueContract(ContractModel):
    """Bind payload to explicit type; this local check confers no Fact authority.

    JSON Schema validates the structural algebra. Cross-field conformance needs
    this model validator too, just as schema/key/producer checks later need a
    Registry. A bare EnumValue/RecordValue is not proof of semantic validation.
    """

    type_definition: TypeDefinition
    value: Value

    @model_validator(mode="after")
    def check_conformance(self) -> Self:
        _conform(self.type_definition, self.value)
        return self


def validate_typed_value(definition: TypeDefinition, raw_value: object) -> Value:
    """Validate against a caller-owned type without registration/I/O.

    The definition comes from trusted configuration, never from the payload being
    checked. Only this binding (or ValueContract), not a bare Value shape, checks
    enum choices, homogeneous item types and the closed set of Record fields.
    """
    return ValueContract.model_validate(
        {"type_definition": definition, "value": raw_value}
    ).value


ListValue.model_rebuild()
RecordValue.model_rebuild()
ListTypeDefinition.model_rebuild()
RecordFieldDefinition.model_rebuild()
RecordTypeDefinition.model_rebuild()
ValueContract.model_rebuild()
