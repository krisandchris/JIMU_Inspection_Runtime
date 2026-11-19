"""Independent source coverage and structural/semantic Schema boundaries."""

import ast
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.domain.definition_support import UTCDateTime
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.values import IntegerValue, NumberValue
from scripts import generate_schemas as exporter

ROOT = Path(__file__).parents[2]


def declared_contracts(source_root: Path) -> tuple[set[str], set[str]]:
    """Scan source independently; never derive expected coverage from export mapping."""
    classes: list[tuple[str, str, set[str], bool]] = []
    unions: set[str] = set()
    for source in source_root.rglob("*.py"):
        module = "jimu_runtime.domain." + ".".join(
            source.relative_to(source_root).with_suffix("").parts
        )
        tree = ast.parse(source.read_text())
        aliases = {
            name.asname or name.name: name.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for name in node.names
        }
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases = {
                    aliases.get(base.id, base.id)
                    for base in node.bases
                    if isinstance(base, ast.Name)
                }
                bases.update(
                    base.attr for base in node.bases if isinstance(base, ast.Attribute)
                )
                versioned = any(
                    isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Name)
                    and item.target.id == "schema_version"
                    for item in node.body
                )
                classes.append((module, node.name, bases, versioned))
            elif (
                isinstance(node, ast.TypeAlias)
                and not node.type_params
                and not node.name.id.startswith("_")
            ):
                value = node.value
                if (
                    isinstance(value, ast.Subscript)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "Annotated"
                    and isinstance(value.slice, ast.Tuple)
                ):
                    value = value.slice.elts[0]
                if isinstance(value, ast.BinOp) and isinstance(value.op, ast.BitOr):
                    unions.add(module + "." + node.name.id)
    versioned_names = {name for _, name, _, versioned in classes if versioned}
    while True:
        expanded = versioned_names | {
            name for _, name, bases, _ in classes if bases & versioned_names
        }
        if expanded == versioned_names:
            break
        versioned_names = expanded
    roots = {
        module + "." + name
        for module, name, _, _ in classes
        if name in versioned_names and not name.startswith("_")
    }
    # Public shared base is an abstract scaffold, not a persisted product root.
    roots.discard("jimu_runtime.domain.definition_support.DefinitionContract")
    # This private-to-generation serialization algebra is not a domain contract.
    unions.discard("jimu_runtime.domain.fact_schemas.canonicalization.JSONValue")
    return roots, unions


def test_independent_ast_coverage_for_public_roots_and_named_unions() -> None:
    roots, unions = declared_contracts(ROOT / "src/jimu_runtime/domain")
    sources = {entry.source_symbol for entry in exporter.INVENTORY}
    assert roots <= sources, roots - sources
    assert unions <= sources, unions - sources
    assert len(roots) >= 34
    assert len(unions) >= 28


def test_coverage_probe_detects_a_new_root_without_changing_export_mapping(
    tmp_path: Path,
) -> None:
    (tmp_path / "probe.py").write_text(
        "class Base:\n    schema_version: str\nclass FutureContract(Base):\n    value: str\n"
    )
    roots, _ = declared_contracts(tmp_path)
    assert "jimu_runtime.domain.probe.FutureContract" in roots
    assert "jimu_runtime.domain.probe.FutureContract" not in {
        entry.source_symbol for entry in exporter.INVENTORY
    }


@pytest.fixture(scope="module")
def rendered_b() -> dict[str, bytes]:
    return exporter.render_exports(exporter.WP01B_INVENTORY)


def test_generated_inventory_is_complete_exact_and_self_consistent(
    rendered_b: dict[str, bytes],
) -> None:
    rendered = rendered_b
    index = json.loads(rendered["index.json"])
    assert index["generation"] == {
        "mode": "validation",
        "by_alias": True,
        "union_format": "any_of",
        "dialect": exporter.DIALECT,
    }
    assert set(index["limitations"]) == {
        "numeric",
        "utc",
        "bytes",
        "taint",
        "relations",
        "digest",
    }
    assert len(index["schemas"]) == len(exporter.WP01B_INVENTORY)
    assert {item["filename"] for item in index["schemas"]} | {"index.json"} == set(
        rendered
    )
    assert any(
        item["source_symbol"].endswith(".NonExpressionPolicyArtifact")
        for item in index["schemas"]
    )
    assert any(
        item["source_symbol"].endswith(".PolicyArtifact") for item in index["schemas"]
    )
    assert not {
        "ApplicabilityPolicy expression contract",
        "ToolSkipPolicy expression contract",
        "PolicyArtifact complete union",
        "FactProducerDeclaration",
        "ProducerCatalogue",
        "full_authority_admission",
    } & set(index["deferred_groups"])
    for item in index["schemas"]:
        encoded = rendered[item["filename"]]
        assert hashlib.sha256(encoded).hexdigest() == item["sha256"]
        assert encoded.endswith(b"\n")
        assert str(ROOT).encode() not in encoded
        document = json.loads(encoded)
        assert document["$schema"] == exporter.DIALECT
        exporter.validate_document(document)


@pytest.mark.parametrize(
    "name,source",
    [
        ("EntityDefinition", "s1/contracts.json"),
        ("QuestionDefinition", "o1/contracts.json"),
    ],
)
def test_real_definition_fixture_and_missing_version(
    name: str, source: str, rendered_b: dict[str, bytes]
) -> None:
    data = json.loads((ROOT / "tests/fixtures/wp01b" / source).read_text())
    raw = data["entity" if name == "EntityDefinition" else "question"]
    filename = next(
        item.filename for item in exporter.WP01B_INVENTORY if item.symbol == name
    )
    validator = Draft202012Validator(json.loads(rendered_b[filename]))
    assert validator.is_valid(raw)
    del raw["schema_version"]
    assert not validator.is_valid(raw)


def test_fact_source_scope_status_constraints_are_structural(
    rendered_b: dict[str, bytes],
) -> None:
    data = json.loads((ROOT / "tests/fixtures/wp01b/a1/contracts.json").read_text())
    document = json.loads(rendered_b["runtime.facts.FactRecord.schema.json"])
    validator = Draft202012Validator(document)
    assert validator.is_valid(data["known"])
    raw = data["known"]
    raw["producer_kind"] = "system"
    assert not validator.is_valid(raw)
    raw = data["unknown"]
    raw["value"] = None
    assert not validator.is_valid(raw)


def test_numeric_and_relation_strictness_do_not_claim_false_schema_equivalence() -> (
    None
):
    for model, value in (
        (IntegerValue, {"kind": "integer", "value": 1.0}),
        (NumberValue, {"kind": "number", "value": 1}),
    ):
        assert Draft202012Validator(model.model_json_schema()).is_valid(value)
        with pytest.raises(ValidationError):
            model.model_validate(value)
    raw = json.loads((ROOT / "tests/fixtures/wp01b/a1/contracts.json").read_text())[
        "known"
    ]
    raw["producer_id"] = "different"
    assert Draft202012Validator(TypeAdapter(FactRecord).json_schema()).is_valid(raw)
    with pytest.raises(ValidationError):
        TypeAdapter(FactRecord).validate_json(json.dumps(raw))


def test_utc_schema_lexical_and_pydantic_calendar_boundaries() -> None:
    schema = TypeAdapter(UTCDateTime).json_schema()
    assert not Draft202012Validator(schema).is_valid("0")
    assert Draft202012Validator(schema).is_valid("2026-02-30T00:00:00Z")
    with pytest.raises(ValidationError):
        TypeAdapter(UTCDateTime).validate_json('"2026-02-30T00:00:00Z"')


def test_explicit_x1_to_x4_inventory_and_component_roles() -> None:
    # Independent expected public API names: do not derive this set from INVENTORY.
    expected = {
        "conditions.ConditionExpr",
        "conditions.LocalToolFactSelector",
        "conditions.ConditionReferences",
        "definition_enums.ConditionInputDomain",
        "definition_enums.ComparisonOperator",
        "definition_enums.ProducerCardinality",
        "fact_selectors.ProducerCatalogueContentRef",
        "policies.ApplicabilityPolicyArtifact",
        "policies.ToolSkipPolicyArtifact",
        "policies.PolicyArtifact",
        "policies.NonExpressionPolicyArtifact",
        "producers.FactProducerDeclaration",
        "producers.ProducerCatalogue",
        "producers.Producer",
        "producers.ActivationCondition",
        "validation.producer_inputs.ProducerCatalogueRegistration",
        "validation.producer_inputs.SystemCapabilityRegistration",
        "validation.producer_inputs.ProducerValidationContext",
        "validation.producer_inputs.ProducerCatalogueContext",
        "validation.producer_authority_inputs.ExecutionDefinitionPin",
        "validation.producer_authority_inputs.ProducerAuthorityValidationInput",
        "validation.condition_inputs.ApplicabilityConditionContext",
        "validation.condition_inputs.ToolStepGuardConditionContext",
        "validation.condition_inputs.ProducerActivationConditionContext",
        "validation.condition_inputs.ConditionReferenceContext",
        "validation.condition_inputs.ConditionReferenceResult",
        "validation.definition_inputs.DefinitionRegistration",
        "validation.definition_inputs.DefinitionValidationInput",
    }
    entries = {
        entry.source_symbol.removeprefix("jimu_runtime.domain."): entry
        for entry in exporter.WP01B_INVENTORY
    }
    assert expected <= entries.keys(), expected - entries.keys()
    assert entries["producers.FactProducerDeclaration"].role == "definition_component"
    assert entries["producers.ProducerCatalogue"].role == "definition_root"
    for name in ("ApplicabilityPolicyArtifact", "ToolSkipPolicyArtifact"):
        assert entries["policies." + name].role == "definition_root"


def test_complete_exported_unions_preserve_their_actual_branch_counts(
    rendered_b: dict[str, bytes],
) -> None:
    for filename, count in (
        ("conditions.ConditionExpr.schema.json", 10),
        ("policies.PolicyArtifact.schema.json", 16),
        ("policies.NonExpressionPolicyArtifact.schema.json", 14),
        ("producers.Producer.schema.json", 3),
        ("producers.ActivationCondition.schema.json", 2),
        ("validation.condition_inputs.ConditionReferenceContext.schema.json", 3),
    ):
        document = json.loads(rendered_b[filename])
        union = document
        if "$ref" in union:
            # Recursive PEP 695 aliases may retain a root-local definition ref.
            reference = union["$ref"]
            assert reference.startswith("#/$defs/")
            union = document["$defs"][reference.removeprefix("#/$defs/")]
        keyword = "anyOf" if "NonExpressionPolicyArtifact" in filename else "oneOf"
        assert len(union[keyword]) == count


@pytest.mark.parametrize("kind", ["applicability", "tool_skip"])
def test_exported_expression_policy_fixtures_and_invalid_tag(
    kind: str, rendered_b: dict[str, bytes]
) -> None:
    data = json.loads((ROOT / "tests/fixtures/wp01b/x1/contracts.json").read_text())
    raw = data["artifacts"][kind]
    document = json.loads(rendered_b["policies.PolicyArtifact.schema.json"])
    validator = Draft202012Validator(document)
    assert validator.is_valid(raw)
    del raw["schema_version"]
    assert not validator.is_valid(raw)
    raw["schema_version"] = "1.0.0"
    raw["policy"]["policy_kind"] = "unregistered"
    assert not validator.is_valid(raw)


def test_exported_producer_catalogue_fixture_and_missing_version(
    rendered_b: dict[str, bytes],
) -> None:
    raw = json.loads((ROOT / "tests/fixtures/wp01b/x2/contracts.json").read_text())[
        "catalogue"
    ]
    document = json.loads(rendered_b["producers.ProducerCatalogue.schema.json"])
    validator = Draft202012Validator(document)
    assert validator.is_valid(raw)
    del raw["schema_version"]
    assert not validator.is_valid(raw)
