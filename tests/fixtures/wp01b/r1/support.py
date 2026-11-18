"""Synthetic schema setup, with asserted parent identity separate from child hash."""

import json
from pathlib import Path

from jimu_runtime.domain.fact_schemas.models import (
    FactSchemaDefinition,
    ValueSchemaGenerationSpec,
)
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.references import ContentRef


def fixtures():
    return json.loads(Path(__file__).with_name("contracts.json").read_text())


def registration(
    type_definition=None, schema_id="registry.example", version="1.0.0", digest="a" * 64
):
    data = fixtures()
    tree = {"kind": "bool"} if type_definition is None else type_definition
    spec = ValueSchemaGenerationSpec.model_validate(data["spec"])
    generated = generate_value_schema(tree, spec)
    definition = FactSchemaDefinition.model_validate(
        {
            **data["fact_schema"],
            "schema_id": schema_id,
            "version": version,
            "type_definition": tree,
            "value_schema_generation": spec,
            "payload_schema_ref": generated.child_ref.model_dump(mode="json"),
        }
    )
    ref = ContentRef.model_validate(
        {
            "artifact_kind": "fact_schema",
            "artifact_id": schema_id,
            "version": version,
            "digest": digest,
        }
    )
    return ref, definition, generated
