"""Deterministic local child schema generation, without parent/Bundle hashing."""

from hashlib import sha256
from typing import cast

from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    GeneratedValueSchema,
    ProjectionValidationError,
    ValueSchemaContentRef,
    ValueSchemaGenerationInput,
    ValueSchemaGenerationSpec,
    normalize_type,
)
from jimu_runtime.domain.values import (
    BooleanTypeDefinition,
    EnumTypeDefinition,
    IntegerTypeDefinition,
    ListTypeDefinition,
    NumberTypeDefinition,
    RecordTypeDefinition,
    StringTypeDefinition,
    TypeDefinition,
)

__all__ = ["generate_value_schema", "normalize_type", "validate_child_reference"]


def _typed_schema(definition: TypeDefinition) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue]
    if isinstance(definition, BooleanTypeDefinition):
        payload = {"type": "boolean"}
    elif isinstance(definition, (IntegerTypeDefinition, NumberTypeDefinition)):
        payload = {"type": definition.kind}
        if definition.minimum is not None:
            payload["minimum"] = definition.minimum
        if definition.maximum is not None:
            payload["maximum"] = definition.maximum
    elif isinstance(definition, StringTypeDefinition):
        payload = {
            "type": "string",
            "minLength": definition.min_length,
            "maxLength": definition.max_length,
        }
    elif isinstance(definition, EnumTypeDefinition):
        payload = {"type": "string", "enum": list(definition.members)}
    elif isinstance(definition, ListTypeDefinition):
        payload = {
            "type": "array",
            "minItems": definition.min_items,
            "maxItems": definition.max_items,
            "items": _typed_schema(definition.item_type),
        }
    elif isinstance(definition, RecordTypeDefinition):
        properties: dict[str, JSONValue] = {
            field.name: _typed_schema(field.type_definition)
            for field in definition.fields
        }
        payload = {
            "type": "object",
            "properties": properties,
            "required": [field.name for field in definition.fields if field.required],
            "additionalProperties": False,
        }
    else:
        raise ProjectionValidationError("invalid_type", (), "unsupported type")
    return {
        "type": "object",
        "properties": {"kind": {"const": definition.kind}, "value": payload},
        "required": ["kind", "value"],
        "additionalProperties": False,
    }


def generate_value_schema(
    type_input: object, generation_spec: ValueSchemaGenerationSpec
) -> GeneratedValueSchema:
    """Revalidate every entry, normalize before defaults, hash only local products."""
    checked_spec = ValueSchemaGenerationSpec.model_validate(generation_spec)
    normalized = normalize_type(type_input, checked_spec.global_limits)
    generation_input = ValueSchemaGenerationInput.model_validate(
        {"projection": normalized, "generation_spec": checked_spec}
    )
    input_bytes = canonical_bytes(
        cast(JSONValue, generation_input.model_dump(mode="json", exclude_none=True))
    )
    input_digest = sha256(input_bytes).hexdigest()
    spec = generation_input.generation_spec
    schema = _typed_schema(generation_input.projection)
    schema["$schema"] = spec.schema_dialect
    schema["$id"] = (
        f"urn:jimu:value-schema:{spec.projection_profile_version}:{input_digest}"
    )
    schema_bytes = canonical_bytes(schema)
    child_digest = sha256(schema_bytes).hexdigest()
    child_ref = ValueSchemaContentRef.model_validate(
        {
            "artifact_kind": "value_schema",
            "artifact_id": f"value-schema.{input_digest}",
            "version": spec.projection_profile_version,
            "digest": child_digest,
        }
    )
    return GeneratedValueSchema(
        generation_input=generation_input,
        canonical_input_bytes=input_bytes,
        input_sha256=input_digest,
        canonical_schema_bytes=schema_bytes,
        child_digest=child_digest,
        child_ref=child_ref,
    )


def validate_child_reference(definition: FactSchemaDefinition) -> GeneratedValueSchema:
    """Regenerate child; never claim injected parent digest/content is authentic."""
    checked = FactSchemaDefinition.model_validate(definition)
    result = generate_value_schema(
        checked.type_definition, checked.value_schema_generation
    )
    if reference_identity(checked.payload_schema_ref) != reference_identity(
        result.child_ref
    ):
        raise ProjectionValidationError(
            "child_reference_mismatch",
            ("payload_schema_ref",),
            "reference differs from deterministic child",
        )
    return result
