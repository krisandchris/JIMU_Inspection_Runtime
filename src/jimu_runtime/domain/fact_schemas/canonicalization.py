"""Bounded generated JSON to RFC8785 bytes; no external schema parser."""

import math

import rfc8785

from jimu_runtime.domain.fact_schemas.models import (
    ErrorPath,
    ProjectionValidationError,
)

type JSONValue = (
    None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
)


def _check(value: JSONValue, path: ErrorPath) -> None:
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ProjectionValidationError(
                "invalid_unicode", path, "invalid Unicode scalar"
            ) from exc
    elif type(value) is int and not -(2**53 - 1) <= value <= 2**53 - 1:
        raise ProjectionValidationError(
            "canonicalizer_integer_domain", path, "integer exceeds library domain"
        )
    elif type(value) is float and not math.isfinite(value):
        raise ProjectionValidationError("invalid_number", path, "number must be finite")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check(item, path + (index,))
    elif isinstance(value, dict):
        for key, item in value.items():
            _check(key, path + (key,))
            _check(item, path + (key,))


def canonical_bytes(value: JSONValue) -> bytes:
    """Internal generated trees only; caller owns bounded shape validation."""
    _check(value, ())
    try:
        return rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, UnicodeEncodeError, OverflowError) as exc:
        raise ProjectionValidationError(
            "canonicalization_failed", (), "canonicalizer rejected generated tree"
        ) from exc
