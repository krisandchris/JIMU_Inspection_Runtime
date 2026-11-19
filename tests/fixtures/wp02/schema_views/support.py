"""Deterministic signed-content inputs, without a compiler descriptor sidecar."""

import json
from datetime import UTC, datetime
from pathlib import Path

from jimu_runtime.bundle.artifacts import DRAFT_2020_12
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.integrity import canonical_json, sha256_digest
from jimu_runtime.domain.fact_schemas.models import ValueSchemaGenerationSpec
from jimu_runtime.domain.fact_schemas.projection import generate_value_schema
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest

ROOT = Path(__file__).resolve().parents[4]
DIALECT = {"$schema": DRAFT_2020_12}
TOOL_OUTPUT = "schemas/tools/outputs/tool-result.schema.json"
TOOL_INPUT = "schemas/tools/inputs/tool-input.schema.json"
CONTEXT = "schemas/context/context.schema.json"
ATTRIBUTE = "schemas/attributes/attributes.schema.json"
FACT_OUTPUT = "schemas/facts/definitions/parked.json"
CHILD = "schemas/values/parked.schema.json"
TOOL = "tools/registry.json"


def record(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def wrapped(kind, value):
    return record({"kind": {"const": kind}, "value": value}, ["kind", "value"])


def payload(schema_id, **value):
    return {**DIALECT, "$id": schema_id, **value}


class Fixture:
    def __init__(self):
        self.sources = {}

    def add(self, path, kind, artifact_id, value, *, pointer=None, dialect=False):
        self.sources[path] = (kind, artifact_id, value, pointer)
        return {
            "artifact_kind": kind,
            "artifact_id": artifact_id,
            "version": "1.0.0",
            "digest": sha256_digest(
                canonical_json(value, max_bytes=500_000, max_depth=90)
            ),
            **({"schema_dialect": DRAFT_2020_12} if dialect else {}),
        }

    def catalogue(self):
        entries, files = [], {}
        for path, (kind, artifact_id, value, pointer) in self.sources.items():
            member_ref = self.add(path, kind, artifact_id, value, pointer=pointer)
            entry = {"content_ref": member_ref, "path": path}
            if pointer is not None:
                entry["json_pointer"] = pointer
                serialized = [value]
            else:
                serialized = value
            entries.append(entry)
            files[path] = canonical_json(serialized, max_bytes=500_000, max_depth=90)
        manifest = BundleManifest.model_validate(
            {
                "format": "jimu.runtime-bundle",
                "schema_version": "1.0.0",
                "bundle_id": "typed-schema-fixtures",
                "bundle_version": "1.0.0",
                "compiler_version": "1.0.0",
                "created_at": datetime(2026, 11, 18, tzinfo=UTC),
                "source": {"source_system": "test-fixtures", "artifacts": []},
                "artifacts": entries,
                "compatibility": {
                    "minimum_runtime_version_inclusive": "1.0.0",
                    "maximum_runtime_version_exclusive": "2.0.0",
                },
                "required_capabilities": [],
                "integrity": {
                    "hash_algorithm": "sha256",
                    "canonicalization_id": "jcs-sha256-v1",
                    "root_digest": "0" * 64,
                    "signatures": [],
                },
            }
        )
        return build_catalogue(manifest, files, max_bytes=500_000, max_depth=90)


def fact_child():
    source = json.loads((ROOT / "tests/fixtures/wp01b/jcs_vectors.json").read_text())
    specification = ValueSchemaGenerationSpec.model_validate(source["spec"])
    result = generate_value_schema(
        source["fact_schema"]["type_definition"], specification
    )
    parent = {
        **source["fact_schema"],
        "allowed_producer_kinds": ["tool"],
        "value_schema_generation": source["spec"],
        "payload_schema_ref": result.child_ref.model_dump(mode="json"),
    }
    return parent, json.loads(result.canonical_schema_bytes)


def evidence_contract(*, succeeded):
    item_branches = []
    occurrences = []
    for name, single, required in (
        ("primary", True, True),
        ("supporting", False, False),
    ):
        refs = {"$ref": "#/$defs/EvidenceRefs", "minItems": 1}
        if single:
            refs.update(maxItems=1)
        item_branches.append(
            record(
                {"name": {"const": name}, "evidence_refs": refs},
                ["name", "evidence_refs"],
            )
        )
        occurrences.append(
            {
                "contains": {"properties": {"name": {"const": name}}},
                "minContains": 1 if succeeded and required else 0,
                "maxContains": 1,
            }
        )
    return {
        "type": "array",
        "maxItems": 64,
        "items": {"oneOf": item_branches},
        "allOf": occurrences,
    }


def tool_result():
    raw = json.loads(
        (
            ROOT / "schemas/generated/runtime.tool_executions.ToolResult.schema.json"
        ).read_text()
    )
    raw["$id"] = "urn:test:schema:result"
    raw["$defs"]["SpecificPayload"] = wrapped(
        "record", record({"parked": wrapped("bool", {"type": "boolean"})}, [])
    )
    for name in ("SucceededToolResult", "PartialToolResult", "FailedToolResult"):
        fields = raw["$defs"][name]["properties"]
        fields["evidence"] = evidence_contract(succeeded=name == "SucceededToolResult")
        if name != "FailedToolResult":
            fields["payload"] = {"$ref": "#/$defs/SpecificPayload"}
    return raw


def tool_fixture(result=None, *, evidence_name="primary"):
    fixture = Fixture()
    output = tool_result() if result is None else result
    output_ref = fixture.add(
        TOOL_OUTPUT, "tool_output_schema", "tool-result", output, dialect=True
    )
    input_ref = fixture.add(
        TOOL_INPUT,
        "tool_input_schema",
        "tool-input",
        payload("urn:test:schema:tool-input", **record({}, [])),
        dialect=True,
    )
    fact, child = fact_child()
    fixture.add(CHILD, "value_schema", fact["payload_schema_ref"]["artifact_id"], child)
    fact_ref = fixture.add(FACT_OUTPUT, "fact_schema", "parked", fact)
    tool = json.loads((ROOT / "tests/fixtures/wp01b/m1/contracts.json").read_text())[
        "tool"
    ]
    tool["input_schema_ref"] = input_ref
    tool["output_schema_ref"] = output_ref
    tool["fact_outputs"] = [
        {
            "output_name": "parked",
            "fact_key": "parking.state",
            "fact_schema_ref": fact_ref,
            "scope": "object",
            "required": True,
            "value_path": ["parked"],
            "evidence_paths": [[evidence_name], ["supporting"]],
        }
    ]
    fixture.add(TOOL, "tool", "tool", tool, pointer="/0")
    return fixture
