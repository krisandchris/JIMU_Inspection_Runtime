"""Synthetic admitted views exercising one pure predicate at a time."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from itertools import product

import pytest
from fixtures.wp01c.rule_types.support import (
    Example,
    compare,
    literal,
    membership,
    ref,
    unit,
)
from pydantic import TypeAdapter

from jimu_runtime.bundle.artifacts import SchemaResource, artifact_identity
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, ResolvedArtifact
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.definition_support import ContextFieldSelector
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.runtime.bundle_manifest import ArtifactInventoryEntry
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.values import Value
from jimu_runtime.inspection.rules import PreparedPredicate

_DOMAINS = {
    "rule_condition": ConditionInputDomain.RULE_CONDITION,
    "applicability": ConditionInputDomain.RULE_APPLICABILITY,
    "tool_step_guard": ConditionInputDomain.TOOL_CONDITION,
    "producer_activation": ConditionInputDomain.TOOL_CONDITION,
}


def _pin(item):
    return ContentRef.model_validate(
        {
            key: item[key]
            for key in ("artifact_kind", "artifact_id", "version", "digest")
        }
    )


def _context_schema(shape):
    kind = shape["kind"]
    if kind == "record":
        return {
            "type": "object",
            "properties": {
                field["name"]: _context_schema(field["type_definition"])
                for field in shape["fields"]
            },
            "required": sorted(
                field["name"] for field in shape["fields"] if field["required"]
            ),
            "additionalProperties": False,
        }
    if kind == "string":
        return {
            "type": "string",
            "minLength": shape.get("min_length", 0),
            "maxLength": shape.get("max_length", 4096),
        }
    if kind == "bool":
        return {"type": "boolean"}
    raise AssertionError(kind)


def scenario(example, expression, role="rule_condition", **context_options):
    context = example.context(role, **context_options)
    if role == "applicability":
        policy = deepcopy(
            next(
                item
                for item in example.data["input"]["policies"]
                if item["ref"] == context.policy_ref.model_dump(mode="json")
            )
        )
        policy["artifact"]["policy"].update(
            condition_ast=expression,
            context_allowlist=(expression["left"]["selector"],),
        )
        example.policies = [policy]
    if role == "tool_step_guard":
        policy_ref = context.policy_ref.model_dump(mode="json")
        example.policies = [
            {
                "ref": policy_ref,
                "artifact": {
                    "schema_version": "1.0.0",
                    "policy_id": policy_ref["artifact_id"],
                    "version": policy_ref["version"],
                    "policy": {
                        "policy_kind": "tool_skip",
                        "condition_ast": expression,
                        "on_false": "skip_with_reason",
                        "on_unknown": "block",
                        "on_missing_input": "block",
                    },
                },
            }
        ]
    bindings = example.bindings()
    # The context-only cases need no Tool or Fact resources. Give them actual
    # closed Context Schema documents for the WP-02 view reconstruction.
    context_only = role == "applicability"
    if context_only:
        bindings = bindings.model_copy(update={"tools": ()})
    entries = [
        (item.ref, item.definition)
        for directory in (
            bindings.profiles,
            bindings.questions,
            bindings.plans,
            bindings.tools,
        )
        for item in directory
    ]
    entries.extend((item.ref, item.artifact) for item in bindings.policies)
    catalogue_entry = bindings.producer_catalogue
    entries.append((catalogue_entry.ref, catalogue_entry.definition))
    if context_only:
        for item in example.descriptors:
            pin = _pin(item["schema_ref"])
            if pin.artifact_kind.value != "context_schema":
                continue
            shape = item["type_definition"]
            uri = f"https://jimu.invalid/{pin.artifact_id}"
            document = {
                "$schema": item["schema_ref"]["schema_dialect"],
                "$id": uri,
                **_context_schema(shape),
            }
            entries.append((pin, SchemaResource(uri, json.dumps(document).encode())))
    else:
        entries.extend(
            (_pin(item["ref"]), FactSchemaDefinition.model_validate(item["definition"]))
            for item in example.data["fact_schemas"]
        )
    if role == "rule_condition":
        entries.extend(
            [
                (context.rule_ref, example.rule(expression)),
                (context.rule_set.ref, context.rule_set.definition),
            ]
        )
    artifacts = tuple(
        ResolvedArtifact(
            inventory=ArtifactInventoryEntry(content_ref=pin, path="fixture.json"),
            value=value,
            canonical_bytes=b"",
        )
        for pin, value in entries
    )
    catalogue = ArtifactCatalogue(
        artifacts=artifacts,
        _by_identity={artifact_identity(item.ref): item for item in artifacts},
        _by_id_version={},
        required_capabilities=(),
    )
    return {
        "catalogue": catalogue,
        "input_domain": _DOMAINS[role],
        "context": context,
        "bindings": bindings,
        "fact_schemas": example.registry(),
    }


def value(kind, value):
    return TypeAdapter(Value).validate_python({"kind": kind, "value": value})


def fact(example, selector, *, value=None, status="known", **overrides):
    selector = selector["selector"]
    declaration = next(
        item
        for item in example.declarations
        if item["fact_key"] == selector["fact_key"]
    )
    producer = declaration["producer"]
    source, details = {
        "vlm_question": (
            {"question_ref": producer.get("question_ref")},
            {
                "observation_batch_ref": {
                    "record_kind": "observation_batch",
                    "record_id": "batch",
                },
                "model_invocation_ref": {
                    "record_kind": "model_invocation",
                    "record_id": "call",
                },
            },
        ),
        "tool": (
            {
                key: producer.get(key)
                for key in ("tool_plan_ref", "step_id", "tool_ref")
            },
            {
                "tool_execution_ref": {
                    "record_kind": "tool_execution",
                    "record_id": "tool",
                },
                "output_name": declaration["output_name"],
            },
        ),
        "system": (
            {"requirement": producer.get("requirement")},
            {
                "system_output_ref": {
                    "record_kind": "execution_event",
                    "record_id": "event",
                },
                "output_name": declaration["output_name"],
            },
        ),
    }[declaration["producer_kind"]]
    raw = {
        "schema_version": "1.0.0",
        "fact_id": "fact-" + declaration["declaration_id"],
        "fact_key": declaration["fact_key"],
        "fact_schema_ref": declaration["fact_schema_ref"],
        "producer_catalogue_ref": example.catalogue_ref,
        "producer_declaration_id": declaration["declaration_id"],
        "producer_kind": declaration["producer_kind"],
        "producer_id": declaration["producer_id"],
        "producer_version": declaration["producer_version"],
        "attempt_id": "attempt-one",
        "source": {"source_kind": declaration["producer_kind"], **source, **details},
        "evidence_refs": [],
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "trace_id": "trace",
        "execution_id": "execution",
        "scope": declaration["scope"],
        "status": status,
    }
    if raw["scope"] == "object":
        raw.update(object_id="object", profile_ref=example.profile["ref"])
    if status == "known":
        raw["value"] = value
    else:
        reason = {
            "reason_code": "source." + status,
            "message": "Synthetic reason",
            "diagnostic_refs": [],
        }
        if status == "error":
            reason["diagnostics"] = [
                {"code": "source.detail", "path": [], "message": "Detail"}
            ]
        if status == "not_applicable":
            reason["basis_refs"] = [ref("policy", "synthetic")]
        raw["reason"] = reason
    return TypeAdapter(FactRecord).validate_python({**raw, **overrides})


@pytest.mark.parametrize(
    ("operator", "left", "right", "expected"),
    [
        ("eq", 0, 0, True),
        ("eq", 0, 1, False),
        ("ne", 1, 0, True),
        ("ne", 0, 0, False),
        ("lt", 0, 1, True),
        ("lt", 0, 0, False),
        ("le", 0, 0, True),
        ("le", 1, 0, False),
        ("gt", 1, 0, True),
        ("gt", 0, 0, False),
        ("ge", 0, 0, True),
        ("ge", 0, 1, False),
    ],
)
def test_all_comparison_operators(operator, left, right, expected):
    predicate = PreparedPredicate(
        **scenario(
            Example(),
            compare(literal("integer", left), literal("integer", right), operator),
        )
    )
    assert predicate.evaluate(execution_id="execution").truth is (
        RuleTruth.TRUE if expected else RuleTruth.FALSE
    )


_T, _F, _U = RuleTruth.TRUE, RuleTruth.FALSE, RuleTruth.UNKNOWN
_KLEENE_PAIRS = tuple(
    zip(
        product((_T, _F, _U), repeat=2),
        (_T, _F, _U, _F, _F, _F, _U, _F, _U),
        (_T, _T, _T, _T, _F, _U, _T, _U, _U),
        strict=True,
    )
)


@pytest.mark.parametrize("arguments,conjunction,disjunction", _KLEENE_PAIRS)
def test_strong_kleene_and_or_keep_all_diagnostics(arguments, conjunction, disjunction):
    example = Example()
    missing = example.question_fact()
    nodes = [missing if arg is _U else literal(value=arg is _T) for arg in arguments]
    for operator, expected in (("all", conjunction), ("any", disjunction)):
        predicate = PreparedPredicate(
            **scenario(example, {"kind": operator, "arguments": nodes})
        )
        record = predicate.evaluate(execution_id="execution", object_id="object")
        assert record.truth is expected
        assert record.diagnostic_codes == (
            ("predicate.fact.missing",) if _U in arguments else ()
        )


@pytest.mark.parametrize("item,expected", [(_T, _F), (_F, _T), (_U, _U)])
def test_not(item, expected):
    example = Example()
    node = example.question_fact() if item is _U else literal(value=item is _T)
    predicate = PreparedPredicate(
        **scenario(example, {"kind": "not", "argument": node})
    )
    assert (
        predicate.evaluate(execution_id="execution", object_id="object").truth
        is expected
    )


@pytest.mark.parametrize(
    "status", ["unknown", "error", "not_applicable", "not_evaluated"]
)
def test_non_known_fact_preserves_status_and_reason_even_if_truth_is_decisive(status):
    example = Example()
    node = example.question_fact()
    expr = {"kind": "any", "arguments": [literal(), node, node]}
    predicate = PreparedPredicate(**scenario(example, expr))
    operand = fact(example, node, status=status)
    result = predicate.evaluate(
        execution_id="execution", object_id="object", facts=(operand,)
    )
    expected = (f"predicate.fact.{status}", f"source.{status}")
    assert result.truth is _T
    assert result.diagnostic_codes == (
        expected + ("source.detail",) if status == "error" else expected
    )


def test_optional_nested_record_field_and_zero_unit_preserve_meaning():
    example = Example()
    node = example.question_fact(
        shape={
            "kind": "record",
            "fields": [
                {
                    "name": "distance",
                    "required": False,
                    "type_definition": {"kind": "number"},
                }
            ],
        },
        path=("distance",),
        units=[(("distance",), unit())],
    )
    example.units = [{"path": ("right",), "unit": unit()}]
    predicate = PreparedPredicate(
        **scenario(example, compare(node, literal("number", 0.0)))
    )
    for payload, expected, diagnostics in (
        ({"distance": {"kind": "number", "value": 0.0}}, _T, ()),
        ({}, _U, ("predicate.fact.missing_field",)),
        (
            {"distance": {"kind": "integer", "value": 0}},
            _U,
            ("predicate.fact.invalid_value",),
        ),
    ):
        item = fact(example, node, value=value("record", payload))
        result = predicate.evaluate(
            execution_id="execution", object_id="object", facts=(item,)
        )
        assert (result.truth, result.diagnostic_codes) == (expected, diagnostics)


def test_enum_membership_with_pinned_schema_and_prepared_reuse_across_objects():
    example = Example()
    members = ["parked", "moving"]
    node = example.question_fact(shape={"kind": "enum", "members": members})
    collection = literal(
        "list",
        [{"kind": "enum", "value": "parked"}],
        item_type={"kind": "enum", "members": members},
    )
    predicate = PreparedPredicate(**scenario(example, membership(node, collection)))
    for object_id, member, expected in (
        ("object", "parked", _T),
        ("other", "moving", _F),
    ):
        item = fact(example, node, value=value("enum", member), object_id=object_id)
        result = predicate.evaluate(
            execution_id="execution", object_id=object_id, facts=(item,)
        )
        assert result.truth is expected
        assert result.diagnostic_codes == ()


def test_applicability_context_bound_to_exact_selector_and_value_type():
    example = Example()
    node = example.context_field()
    condition = compare(node, literal("string", "road"))
    predicate = PreparedPredicate(**scenario(example, condition, "applicability"))
    selector = ContextFieldSelector.model_validate(node["selector"])
    for inputs, expected, diagnostics in (
        ({selector: value("string", "road")}, _T, ()),
        ({selector: value("string", "")}, _F, ()),
        ({selector: value("bool", False)}, _U, ("predicate.context.invalid_value",)),
        ({selector: None}, _U, ("predicate.context.invalid_value",)),
        ({}, _U, ("predicate.context.missing",)),
    ):
        result = predicate.evaluate(execution_id="execution", context_fields=inputs)
        assert (result.truth, result.diagnostic_codes) == (expected, diagnostics)


@pytest.mark.parametrize("role", ["tool_step_guard", "producer_activation"])
def test_tool_condition_reads_pinned_local_ancestor(role):
    example = Example()
    example.plan_chain()
    node = example.local_fact()
    options = {}
    if role == "producer_activation":
        example.tool_fact("activation", step="third", activation=node)
        options["declaration"] = "activation"
    predicate = PreparedPredicate(**scenario(example, node, role, **options))
    item = fact(example, node, value=value("bool", False))
    assert (
        predicate.evaluate(
            execution_id="execution", object_id="object", facts=(item,)
        ).truth
        is _F
    )


def test_tool_guard_bare_true_is_explicit():
    example = Example()
    example.plan_chain()
    predicate = PreparedPredicate(**scenario(example, True, "tool_step_guard"))
    assert predicate.evaluate(execution_id="execution").truth is _T


def test_execution_system_fact_is_used_without_an_object():
    example = Example()
    node = example.system_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    result = predicate.evaluate(
        execution_id="execution",
        facts=(fact(example, node, value=value("bool", True)),),
    )
    assert result.truth is _T
    assert result.diagnostic_codes == ()
