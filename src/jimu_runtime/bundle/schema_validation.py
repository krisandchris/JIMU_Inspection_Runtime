"""Bounded offline evaluation for host-registered provider response schemas."""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue

_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_REF = re.compile(r"#/\$defs/([A-Za-z0-9_.-]{1,128})")
_ANNOTATIONS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "default",
        "examples",
        "$comment",
        "deprecated",
    }
)
_SCALARS = frozenset(
    {
        "type",
        "required",
        "const",
        "enum",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    }
)
_SINGLES = frozenset({"items", "additionalProperties", "not"})
_COMBINERS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_ALLOWED = (
    _ANNOTATIONS | _SCALARS | _SINGLES | _COMBINERS | {"properties", "$defs", "$ref"}
)
_MAX_EXPANDED = 4096
_MAX_DEPTH = 32
_MAX_WORK = 2_000_000
INSUFFICIENT_VISIBILITY = "insufficient_visibility"


class OfflineSchemaError(ValueError):
    """The schema or value is outside the bounded offline profile."""


@dataclass(slots=True)
class _CompileBudget:
    used: int = 0

    def claim(self, count: int = 1) -> None:
        self.used += count
        if self.used > _MAX_EXPANDED:
            raise OfflineSchemaError("offline_schema_complexity")


def _scalar(value: JSONValue) -> bool:
    return not isinstance(value, (list, dict)) and (
        not isinstance(value, str) or len(value) <= 4096
    )


def _normalize(
    node: JSONValue,
    definitions: dict[str, JSONValue],
    budget: _CompileBudget,
    ref_path: tuple[str, ...] = (),
    depth: int = 0,
) -> dict[str, JSONValue] | bool:
    budget.claim()
    if depth > _MAX_DEPTH:
        raise OfflineSchemaError("offline_schema_complexity")
    if isinstance(node, bool):
        return node
    if not isinstance(node, dict) or node.keys() - _ALLOWED:
        raise OfflineSchemaError("offline_schema_keyword")
    if node.get("$schema", _DIALECT) != _DIALECT or (
        depth and ("$defs" in node or "$id" in node)
    ):
        raise OfflineSchemaError("offline_schema_dialect")

    def child(value: JSONValue) -> JSONValue:
        return _normalize(value, definitions, budget, ref_path, depth + 1)

    normalized: dict[str, JSONValue] = {}
    for name, value in node.items():
        if name in _SCALARS:
            if name == "const" and not _scalar(value):
                raise OfflineSchemaError("offline_schema_complex_constant")
            if name == "enum" and (
                not isinstance(value, list) or not all(_scalar(item) for item in value)
            ):
                raise OfflineSchemaError("offline_schema_complex_constant")
            if isinstance(value, list):
                budget.claim(len(value))
            normalized[name] = value
        elif name in _SINGLES:
            normalized[name] = child(value)
        elif name in _COMBINERS and isinstance(value, list):
            normalized[name] = [child(item) for item in value]
        elif name == "properties" and isinstance(value, dict):
            normalized[name] = {key: child(item) for key, item in value.items()}
        elif name in _COMBINERS or name == "properties":
            raise OfflineSchemaError("offline_schema_structure")

    if "$ref" in node:
        ref = node["$ref"]
        match = _REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None or match[1] in ref_path or match[1] not in definitions:
            raise OfflineSchemaError("offline_schema_reference")
        target = _normalize(
            definitions[match[1]], definitions, budget, (*ref_path, match[1]), depth + 1
        )
        return {"allOf": [target, normalized]} if normalized else target
    return normalized


def _decimal_numbers(value: JSONValue) -> object:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _decimal_numbers(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_decimal_numbers(child) for child in value]
    return value


def _validator(schema: JSONValue) -> Callable[[object], bool]:
    exact = cast(dict[str, object] | bool, _decimal_numbers(schema))
    return Draft202012Validator(exact).is_valid


def _require_bounded_value(value: object, max_nodes: int) -> None:
    pending = [value]
    used = 0
    while pending:
        node = pending.pop()
        used += 1
        children = (
            node.values()
            if isinstance(node, dict)
            else node
            if isinstance(node, list)
            else ()
        )
        if used + len(pending) + len(children) > max_nodes:
            raise OfflineSchemaError("offline_schema_value_complexity")
        pending.extend(children)


@dataclass(frozen=True, slots=True)
class OfflineResponseSchema:
    """A checked Draft 2020-12 subset without active references or regular expressions."""

    _validate: Callable[[object], bool] = field(repr=False)
    _slots: tuple[tuple[str, Callable[[object], bool]], ...] = field(repr=False)
    _max_nodes: int

    def validate(self, value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
        _require_bounded_value(value, self._max_nodes)
        if not self._slots:
            if not self._validate(value):
                raise OfflineSchemaError("offline_schema_value_mismatch")
            return (), ()
        if not isinstance(value, dict) or not self._validate(value):
            raise OfflineSchemaError("offline_schema_question_envelope")
        invalid = tuple(
            name
            for name, validate in self._slots
            if name in value and not validate(value[name])
        )
        missing = tuple(name for name, _ in self._slots if name not in value)
        return invalid, missing


def compile_offline_response_schema(
    document: dict[str, JSONValue],
    question_slots: tuple[str, ...] = (),
) -> OfflineResponseSchema:
    definitions = document.get("$defs", {})
    if not isinstance(definitions, dict):
        raise OfflineSchemaError("offline_schema_structure")
    budget = _CompileBudget()
    normalized = _normalize(document, definitions, budget)
    # Unused definitions are checked too, so the document sent to the provider
    # never carries unbounded or externally resolved keywords.
    for name, node in definitions.items():
        _normalize(node, definitions, budget, (name,), 1)
    assert isinstance(normalized, (dict, bool))
    slots: tuple[tuple[str, Callable[[object], bool]], ...]
    try:
        Draft202012Validator.check_schema(document)
        if not question_slots:
            validate = _validator(normalized)
            slots = ()
        else:
            if not isinstance(normalized, dict):
                raise OfflineSchemaError("offline_schema_question_envelope")
            properties = normalized.get("properties")
            required = normalized.get("required")
            if (
                normalized.keys()
                != {"type", "properties", "required", "additionalProperties"}
                or normalized["type"] != "object"
                or normalized["additionalProperties"] is not False
                or not isinstance(properties, dict)
                or not isinstance(required, list)
                or set(properties) != set(question_slots)
                or set(cast(list[str], required)) != set(question_slots)
                or len(required) != len(question_slots)
            ):
                raise OfflineSchemaError("offline_schema_question_envelope")
            envelope = dict(normalized)
            del envelope["required"]
            # Keep the closed root shape; individual item validators own values.
            envelope["properties"] = dict.fromkeys(question_slots, True)
            validate = _validator(envelope)
            slots = tuple(
                (
                    name,
                    _validator(properties[name]),
                )
                for name in question_slots
            )
    except SchemaError:
        raise OfflineSchemaError("offline_schema_invalid") from None
    return OfflineResponseSchema(validate, slots, _MAX_WORK // max(1, budget.used))
