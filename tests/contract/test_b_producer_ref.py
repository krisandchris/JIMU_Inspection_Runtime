"""Catalogue ref extraction preserves the old import and exact four-field wire."""

import ast
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.fact_selectors import ProducerCatalogueContentRef
from jimu_runtime.domain.runtime import facts
from jimu_runtime.domain.runtime.facts import FactRecord


def test_old_import_is_identical_to_shared_class() -> None:
    assert facts.ProducerCatalogueContentRef is ProducerCatalogueContentRef
    assert (
        ProducerCatalogueContentRef.__module__ == "jimu_runtime.domain.fact_selectors"
    )
    assert set(ProducerCatalogueContentRef.model_fields) == {
        "artifact_kind",
        "artifact_id",
        "version",
        "digest",
    }


def test_shared_ref_roundtrip_and_narrow_schema() -> None:
    raw = {
        "artifact_kind": "fact_producer_catalog",
        "artifact_id": "catalogue",
        "version": "1.0.0",
        "digest": "a" * 64,
    }
    value = ProducerCatalogueContentRef.model_validate(raw)
    schema = ProducerCatalogueContentRef.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(raw)
    assert value.model_dump(mode="json") == raw
    assert (
        facts.ProducerCatalogueContentRef.model_validate_json(json.dumps(raw)) == value
    )
    for invalid in (
        {**raw, "artifact_kind": "fact_schema"},
        {**raw, "schema_dialect": "https://json-schema.org/draft/2020-12/schema"},
        {**raw, "version": "latest"},
        {**raw, "digest": None},
    ):
        with pytest.raises(ValidationError):
            ProducerCatalogueContentRef.model_validate(invalid)
        assert not Draft202012Validator(schema).is_valid(invalid)


def test_reference_schema_matches_pre_move_fact_export() -> None:
    exported = (
        Path(__file__).parents[2]
        / "schemas/generated/runtime.facts.FactRecord.schema.json"
    )
    old_reference_schema = json.loads(exported.read_text())["$defs"][
        "ProducerCatalogueContentRef"
    ]
    assert ProducerCatalogueContentRef.model_json_schema() == old_reference_schema


@pytest.mark.parametrize(
    "name",
    [
        "known",
        "unknown",
        "error",
        "not_applicable",
        "not_evaluated",
        "tool_fact",
        "system_fact",
    ],
)
def test_old_facts_keep_catalogue_ref_wire_and_class(name: str) -> None:
    raw = json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/a1/contracts.json").read_text()
    )[name]
    adapter = TypeAdapter(FactRecord)
    value = adapter.validate_json(json.dumps(raw))
    assert type(value.producer_catalogue_ref) is ProducerCatalogueContentRef
    assert value.model_dump(mode="json") == raw
    assert adapter.validate_python(value) == value
    Draft202012Validator(adapter.json_schema()).validate(raw)


def test_only_one_class_definition_and_no_low_level_runtime_import() -> None:
    root = Path(__file__).parents[2] / "src/jimu_runtime/domain"
    classes = []
    for relative in ("fact_selectors.py", "runtime/facts.py"):
        tree = ast.parse((root / relative).read_text())
        classes.extend(
            relative
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and node.name == "ProducerCatalogueContentRef"
        )
    assert classes == ["fact_selectors.py"]
    tree = ast.parse((root / "fact_selectors.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom) and ".runtime" in (node.module or "")
        for node in ast.walk(tree)
    )
