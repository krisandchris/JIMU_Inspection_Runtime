"""A successful type check cannot authorize a different expression or input."""

from dataclasses import replace

import pytest
from fixtures.wp01c.rule_types.support import Example, compare, literal, ref, unit
from unit.test_wp05_predicates import fact, scenario, value

from jimu_runtime.bundle.artifacts import artifact_identity
from jimu_runtime.domain.definition_enums import ConditionInputDomain
from jimu_runtime.domain.definition_support import ContextFieldSelector
from jimu_runtime.domain.values import BooleanValue, ListValue, RecordValue
from jimu_runtime.inspection.rules import (
    PredicateInputError,
    PredicatePreparationError,
    PreparedPredicate,
)


def test_a_passed_type_check_cannot_supply_an_expression():
    example = Example()
    expr = literal()
    options = scenario(example, expr)
    checked = example.check(example.rule(expr))
    assert checked.status == "passed"
    with pytest.raises(TypeError):
        PreparedPredicate(
            **options, type_check=checked, expression=literal(value=False)
        )


def test_missing_and_different_digest_rules_cannot_be_prepared():
    options = scenario(Example(), literal())
    context = options["context"]
    rule_key = artifact_identity(context.rule_ref)
    catalogue = options["catalogue"]
    without_rule = {
        key: item for key, item in catalogue._by_identity.items() if key != rule_key
    }
    with pytest.raises(
        PredicatePreparationError, match="predicate.artifact_unavailable"
    ):
        PreparedPredicate(
            **{**options, "catalogue": replace(catalogue, _by_identity=without_rule)}
        )
    foreign_ref = context.rule_ref.model_copy(update={"digest": "c" * 64})
    with pytest.raises(
        PredicatePreparationError, match="predicate.artifact_unavailable"
    ):
        PreparedPredicate(
            **{
                **options,
                "context": context.model_copy(update={"rule_ref": foreign_ref}),
            }
        )


def test_rule_set_substitution_cannot_authorize_another_use():
    options = scenario(Example(), literal())
    context = options["context"]
    rule_set = context.rule_set
    substituted = rule_set.definition.model_copy(
        update={"candidate_binding_ref": None, "rule_refs": ()}
    )
    options["context"] = context.model_copy(
        update={"rule_set": rule_set.model_copy(update={"definition": substituted})}
    )
    with pytest.raises(PredicatePreparationError):
        PreparedPredicate(**options)


def test_invalid_domain_is_rejected_before_evaluation():
    options = scenario(Example(), literal())
    options["input_domain"] = ConditionInputDomain.RULE_APPLICABILITY
    with pytest.raises(PredicatePreparationError, match="rule_type.use_role"):
        PreparedPredicate(**options)


@pytest.mark.parametrize(
    ("right", "declared_unit", "code"),
    [
        (literal("number", 0.0), None, "rule_type.kind_mismatch"),
        (literal("integer", 0), "cm", "rule_type.unit_mismatch"),
        (literal("bool", False), None, "rule_type.kind_mismatch"),
    ],
)
def test_no_numeric_widening_unit_conversion_or_boolean_coercion(
    right, declared_unit, code
):
    example = Example()
    left = example.question_fact(shape={"kind": "integer"}, units=[((), unit())])
    if declared_unit:
        example.units = [{"path": ("right",), "unit": unit(declared_unit)}]
    with pytest.raises(PredicatePreparationError, match=code) as exc:
        PreparedPredicate(**scenario(example, compare(left, right)))
    assert exc.value.issues[0].path == ("right",)


def test_enum_identity_is_bound_to_the_actual_schema_not_member_spelling():
    example = Example()
    shape = {"kind": "enum", "members": ["parked", "unparked"]}
    left, right = (example.question_fact(key, shape=shape) for key in ("left", "right"))
    with pytest.raises(
        PredicatePreparationError, match="rule_type.enum_identity_mismatch"
    ):
        PreparedPredicate(**scenario(example, compare(left, right)))


def test_context_rejects_type_projection_substitution_at_same_digest():
    example = Example()
    selector = example.context_field()
    options = scenario(
        example, compare(selector, literal("string", "road")), "applicability"
    )
    actual_ref = ContextFieldSelector.model_validate(selector["selector"]).schema_ref
    context = options["context"]
    descriptors = []
    for descriptor in context.descriptors:
        if artifact_identity(descriptor.schema_ref) == artifact_identity(actual_ref):
            descriptor = descriptor.model_copy(
                update={
                    "type_definition": descriptor.type_definition.model_copy(
                        update={
                            "fields": tuple(
                                field.model_copy(update={"required": False})
                                for field in descriptor.type_definition.fields
                            )
                        }
                    )
                }
            )
        descriptors.append(descriptor)
    options["context"] = context.model_copy(update={"descriptors": tuple(descriptors)})
    with pytest.raises(
        PredicatePreparationError, match="predicate.context_schema_binding"
    ):
        PreparedPredicate(**options)


def test_false_tool_branch_cannot_bypass_forbidden_local_dependency():
    example = Example()
    example.plan_chain()
    local = example.local_fact(step="sibling")
    condition = {"kind": "all", "arguments": [literal(value=False), local]}
    with pytest.raises(PredicatePreparationError, match="condition.local_not_ancestor"):
        PreparedPredicate(**scenario(example, condition, "tool_step_guard"))


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"execution_id": "foreign-execution"}, "predicate.foreign_fact_scope"),
        ({"object_id": "foreign-object"}, "predicate.foreign_fact_scope"),
        ({"profile_ref": ref("profile", "foreign")}, "predicate.foreign_fact_scope"),
        (
            {"fact_schema_ref": ref("fact_schema", "q.test", digest="c")},
            "predicate.fact_identity_mismatch",
        ),
        (
            {
                "producer_catalogue_ref": ref(
                    "fact_producer_catalog", "catalogue-test", digest="c"
                )
            },
            "predicate.fact_identity_mismatch",
        ),
        ({"producer_declaration_id": "other"}, "predicate.fact_identity_mismatch"),
        ({"fact_key": "other"}, "predicate.unexpected_fact"),
    ],
)
def test_fact_exact_scope_and_identity(overrides, code):
    example = Example()
    node = example.question_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    item = fact(example, node, value=value("bool", False), **overrides)
    with pytest.raises(PredicateInputError, match=code):
        predicate.evaluate(execution_id="execution", object_id="object", facts=(item,))


def test_unexpected_or_missing_selected_authority_is_not_inferred():
    example = Example()
    node = example.question_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    selected = fact(example, node, value=value("bool", False))
    additional = selected.model_copy(update={"attempt_id": "attempt-two"})
    for items in ((selected, additional), (additional, selected)):
        with pytest.raises(PredicateInputError, match="predicate.duplicate_fact"):
            predicate.evaluate(
                execution_id="execution", object_id="object", facts=items
            )
    with pytest.raises(PredicateInputError, match="predicate.object_scope_required"):
        predicate.evaluate(execution_id="execution", facts=(selected,))


def test_foreign_context_digest_cannot_supply_a_same_named_field():
    example = Example()
    node = example.context_field()
    predicate = PreparedPredicate(
        **scenario(example, compare(node, literal("string", "road")), "applicability")
    )
    selector = ContextFieldSelector.model_validate(node["selector"])
    selector = selector.model_copy(
        update={
            "schema_ref": selector.schema_ref.model_copy(update={"digest": "b" * 64})
        }
    )
    with pytest.raises(
        PredicateInputError, match="predicate.unexpected_context_selector"
    ):
        predicate.evaluate(
            execution_id="execution", context_fields={selector: value("string", "road")}
        )


def test_value_budget_counts_expanded_occurrences_even_for_shared_subtrees():
    example = Example()
    node = example.question_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    item = fact(example, node, value=value("bool", True))
    branch = ListValue.model_construct(kind="list", value=(item.value,) * 256)
    expanded = ListValue.model_construct(kind="list", value=(branch,) * 129)
    item = item.model_copy(update={"value": expanded})
    with pytest.raises(PredicateInputError, match="predicate.value_budget"):
        predicate.evaluate(execution_id="execution", object_id="object", facts=(item,))


def test_same_source_identity_with_a_wrong_digest_cannot_supply_the_fact():
    example = Example()
    node = example.question_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    item = fact(example, node, value=value("bool", False))
    wrong_pin = item.source.question_ref.model_copy(update={"digest": "b" * 64})
    foreign_source = item.source.model_copy(update={"question_ref": wrong_pin})
    item = item.model_copy(update={"source": foreign_source})
    with pytest.raises(PredicateInputError, match="predicate.fact_identity_mismatch"):
        predicate.evaluate(execution_id="execution", object_id="object", facts=(item,))


def test_fact_registry_cannot_supply_a_different_schema_at_the_same_pin():
    example = Example()
    node = example.question_fact(shape={"kind": "integer"})
    options = scenario(example, compare(node, literal("integer", 0)))
    schema = example.data["fact_schemas"][-1]["definition"]
    schema["allowed_producer_kinds"] = ("vlm_question", "tool")
    options["fact_schemas"] = example.registry()
    with pytest.raises(
        PredicatePreparationError, match="predicate.fact_schema_binding"
    ):
        PreparedPredicate(**options)


def test_malformed_context_container_is_rejected_even_when_empty():
    predicate = PreparedPredicate(**scenario(Example(), literal()))
    with pytest.raises(PredicateInputError) as exc:
        predicate.evaluate(execution_id="execution", context_fields=[])
    assert exc.value.code == "predicate.context_inputs"


def test_known_fact_missing_value_is_rejected_before_budget_walk():
    example = Example()
    node = example.question_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    valid = fact(example, node, value=value("bool", True))
    incomplete = type(valid).model_construct(**valid.model_dump(exclude={"value"}))
    with pytest.raises(PredicateInputError) as exc:
        predicate.evaluate(
            execution_id="execution", object_id="object", facts=(incomplete,)
        )
    assert exc.value.code == "predicate.invalid_fact_record"


@pytest.mark.parametrize(
    ("model", "kind", "code"),
    [
        (ListValue, "list", "predicate.invalid_value_shape"),
        (RecordValue, "record", "predicate.invalid_value_shape"),
        (BooleanValue, "bool", "predicate.invalid_fact_record"),
    ],
)
def test_nested_typed_value_missing_value_field_is_rejected(model, kind, code):
    example = Example()
    node = example.question_fact()
    predicate = PreparedPredicate(**scenario(example, node))
    incomplete = model.model_construct(kind=kind)
    outer = ListValue.model_construct(kind="list", value=(incomplete,))
    invalid = fact(example, node, value=value("bool", True)).model_copy(
        update={"value": outer}
    )
    with pytest.raises(PredicateInputError) as exc:
        predicate.evaluate(
            execution_id="execution", object_id="object", facts=(invalid,)
        )
    assert exc.value.code == code
