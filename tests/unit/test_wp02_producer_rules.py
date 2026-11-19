"""Positive producer inference and mandatory consumer checks."""

import json
from dataclasses import replace
from hashlib import sha256

import rfc8785
from fixtures.wp01c.rule_types.support import literal
from fixtures.wp02.producer_rules.support import ProducerExample

from jimu_runtime.bundle.producer_rules import SystemProducerGrant
from jimu_runtime.domain.producers import QuestionProducer, ToolStepProducer
from jimu_runtime.domain.validation.producer_inputs import SystemCapabilityRegistration


def test_catalogue_sort_and_question_id_from_independent_jcs():
    example = ProducerExample()
    catalogue = example.catalogue()
    derived = example.derive(catalogue)
    assert derived == example.derive(
        replace(catalogue, artifacts=catalogue.artifacts[::-1])
    )
    declaration = next(
        item
        for item in derived.declarations
        if isinstance(item.producer, QuestionProducer)
    )
    identity = [
        declaration.profile_ref.model_dump(mode="json"),
        {"producer_kind": "vlm_question", "question_ref": example.question["ref"]},
        "primary",
    ]
    assert (
        declaration.declaration_id
        == "vlm_question." + sha256(rfc8785.dumps(identity)).hexdigest()
    )
    example.check()


def test_mandatory_question_and_exact_required_tool_outputs_support_rules():
    example = ProducerExample()
    question = example.configured_question(mandatory=True)
    example.plan_chain()
    tool = example.tool_fact()
    example.rule_use({"kind": "all", "arguments": [question, tool]})
    example.check()


def test_guard_can_read_a_strict_transitive_ancestor_and_catalogue_keeps_the_policy():
    example = ProducerExample()
    example.plan_chain()
    guard = example.local_fact()
    example.guard("third", guard)
    example.tool_fact("third-output", step="third")
    derived = example.derive()
    declaration = next(
        item
        for item in derived.declarations
        if isinstance(item.producer, ToolStepProducer)
        and item.producer.step_id == "third"
    )
    assert declaration.activation_condition.condition_ast.model_dump(
        mode="json"
    ) == json.loads(json.dumps(guard))
    example.check()


def test_literal_false_ancestor_excludes_mandatory_producer_availability_check():
    example = ProducerExample()
    question = example.configured_question(mandatory=False)
    example.rule_use(question, set_applies=literal(value=False))
    example.check()


def test_exact_system_output_grant_accepts_one_available_allowed_version():
    example = ProducerExample()
    fact, system = example.grant_system()
    example.rule_use(fact)
    host = SystemCapabilityRegistration.model_validate(
        {
            "registry_id": "synthetic-runtime",
            "family": "system_fact_producer",
            "capability_id": system.producer_id,
            "contract_version": system.producer_version,
            "available_implementation_versions": ("2.0.0",),
            "supported_features": (),
        }
    )
    example.check(
        capabilities=(host,), grants=(SystemProducerGrant(declaration=system),)
    )


def test_same_object_fact_key_in_distinct_profiles_does_not_collide():
    from copy import deepcopy

    example = ProducerExample()
    second = deepcopy(example.profile)
    second["ref"]["artifact_id"] = "another-profile"
    second["definition"]["profile_id"] = "another-profile"
    example.data["input"]["definitions"].append(second)
    derived = example.derive()
    declarations = derived.declarations
    assert len(declarations) == 2
    assert declarations[0].fact_key == declarations[1].fact_key
    assert declarations[0].declaration_id != declarations[1].declaration_id
    assert declarations[0].profile_ref != declarations[1].profile_ref


def test_optional_rule_selector_still_binds_exactly_without_guaranteed_availability():
    from jimu_runtime.domain.fact_selectors import FactSelector

    example = ProducerExample()
    optional_question = example.configured_question(mandatory=False)
    optional = FactSelector.model_validate(optional_question["selector"])
    example.rule_use(optional_question, optional=(optional,))
    example.check()
