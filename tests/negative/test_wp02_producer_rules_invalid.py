"""Reject invalid authority, incomplete availability, and semantic catalogue drift."""

from copy import deepcopy

import pytest
from fixtures.wp01c.rule_types.support import compare, literal
from fixtures.wp02.producer_rules.support import ProducerExample

from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.bundle.producer_rules import PriorToolUse, SystemProducerGrant
from jimu_runtime.domain.tool_mappings import ProfileContentRef
from jimu_runtime.domain.validation.producer_inputs import SystemCapabilityRegistration


def test_admission_recomputes_a_structurally_valid_derived_declaration_id():
    example = ProducerExample()
    derived = example.derive()
    declaration = derived.declarations[0].model_copy(
        update={"declaration_id": "forged-owner"}
    )
    forged = derived.model_copy(update={"declarations": (declaration,)})
    with pytest.raises(BundleArtifactError, match="producer.catalogue_mismatch"):
        example.check(definition=forged)


def test_admission_rejects_same_profile_same_key_even_with_different_sources():
    example = ProducerExample()
    example.configured_question(mandatory=True)
    example.question["definition"]["primary_fact_key"] = "q.test"
    with pytest.raises(BundleArtifactError, match="Fact key collision"):
        example.derive()


def test_potential_mandatory_rule_cannot_read_an_optional_question():
    example = ProducerExample()
    question = example.configured_question(mandatory=False)
    example.rule_use(question)
    with pytest.raises(BundleArtifactError, match="producer.required_unavailable"):
        example.check()


@pytest.mark.parametrize(
    "unavailable", ["optional_step", "optional_output", "guard", "ancestor"]
)
def test_mandatory_rule_cannot_claim_a_tool_fact_has_guaranteed_production(unavailable):
    example = ProducerExample()
    example.plan_chain()
    output = example.tool_fact(step="second")
    example.rule_use(output)
    if unavailable == "optional_output":
        example.tool["definition"]["fact_outputs"][-1]["required"] = False
    elif unavailable == "guard":
        example.guard("second", literal(value=False))
    else:
        step = example.plan["definition"]["steps"][
            0 if unavailable == "ancestor" else 1
        ]
        step["requirement"] = "optional"
    with pytest.raises(BundleArtifactError, match="producer.required_unavailable"):
        example.check()


@pytest.mark.parametrize("upstream", ["sibling", "third"])
def test_guard_requires_a_strict_local_ancestor(upstream):
    example = ProducerExample()
    example.plan_chain()
    guard = example.local_fact(step=upstream)
    example.guard("third", guard)
    with pytest.raises(BundleArtifactError, match="condition.local_not_ancestor"):
        example.check()


def test_required_tool_input_rejects_optional_question():
    example = ProducerExample()
    question = example.configured_question(mandatory=False)
    step = example.plan["definition"]["steps"][0]
    step["input_bindings"][0]["source"] = {
        "source_kind": "fact",
        "selector": question["selector"],
    }
    with pytest.raises(BundleArtifactError, match="producer.required_unavailable"):
        example.check()


def test_required_step_output_cannot_depend_on_optional_step():
    example = ProducerExample()
    example.plan_chain()
    first, second, *_ = example.plan["definition"]["steps"]
    first["requirement"] = "optional"
    second["input_bindings"][0]["source"] = {
        "source_kind": "step_output",
        "step_id": "first",
        "selector": {
            "schema_ref": example.tool["definition"]["output_schema_ref"],
            "field_path": (),
        },
    }
    with pytest.raises(BundleArtifactError, match="producer.required_step_unavailable"):
        example.check()


def test_exact_tool_input_rejects_a_sibling_at_semantic_boundary():
    example = ProducerExample()
    example.plan_chain()
    sibling = example.tool_fact(step="sibling")
    third = example.plan["definition"]["steps"][2]
    third["input_bindings"][0]["source"] = {
        "source_kind": "fact",
        "selector": sibling["selector"],
    }
    with pytest.raises(BundleArtifactError, match="producer.tool_input_ancestor"):
        example.check()


@pytest.mark.parametrize("site", ["rule", "applicability", "tool"])
def test_all_three_condition_domains_check_registered_operator_types(site):
    example = ProducerExample()
    wrong = compare(literal(), literal("integer", 2))
    if site == "rule":
        example.rule_use(wrong)
    elif site == "applicability":
        name = example.original_profile_applicability["artifact_id"]
        example.policy_by_name(name)["artifact"]["policy"]["condition_ast"] = wrong
    else:
        example.guard("step", wrong)
    with pytest.raises(BundleArtifactError, match=r"rule_type\..*"):
        example.check()


@pytest.mark.parametrize(
    ("reject", "diagnostic"),
    [
        ("missing_grant", "producer.system_unauthorized"),
        ("different_declaration", "producer.system_unauthorized"),
        ("different_manifest_requirement", "producer.system_requirement"),
        ("no_version_overlap", "producer.host_requirement"),
        ("missing_feature", "producer.host_requirement"),
    ],
)
def test_system_authority_requires_exact_declaration_manifest_membership_and_host_agreement(
    reject, diagnostic
):
    example = ProducerExample()
    _, system = example.grant_system(features=("typed_context",))
    grants = (SystemProducerGrant(declaration=system),)
    if reject == "missing_grant":
        grants = ()
    elif reject == "different_declaration":
        grants = (
            SystemProducerGrant(
                declaration=system.model_copy(update={"fact_key": "changed"})
            ),
        )
    elif reject == "different_manifest_requirement":
        example.requirements.clear()
    host = SystemCapabilityRegistration.model_validate(
        {
            "registry_id": "synthetic-runtime",
            "family": "system_fact_producer",
            "capability_id": system.producer_id,
            "contract_version": system.producer_version,
            "available_implementation_versions": (
                "9.0.0" if reject == "no_version_overlap" else "2.0.0",
            ),
            "supported_features": ()
            if reject == "missing_feature"
            else ("typed_context",),
        }
    )
    with pytest.raises(BundleArtifactError, match=diagnostic):
        example.check(capabilities=(host,), grants=grants)


def test_system_tool_consumer_rejects_grant_for_a_different_step():
    example = ProducerExample()
    system_fact, system = example.grant_system()
    example.plan_chain()
    example.guard("third", system_fact)
    host = SystemCapabilityRegistration.model_validate(
        {
            "registry_id": "synthetic-runtime",
            "family": "system_fact_producer",
            "capability_id": system.producer_id,
            "contract_version": system.producer_version,
            "available_implementation_versions": ("1.0.0",),
            "supported_features": (),
        }
    )
    grant = SystemProducerGrant(
        declaration=system,
        available_before_tool_steps=(
            PriorToolUse.model_validate(
                {
                    "profile_ref": example.profile["ref"],
                    "tool_plan_ref": example.plan["ref"],
                    "step_id": "second",
                }
            ),
        ),
    )
    with pytest.raises(
        BundleArtifactError, match="rule_type.prior_producer_unavailable"
    ):
        example.check(capabilities=(host,), grants=(grant,))
    applicable = grant.available_before_tool_steps[0].model_copy(
        update={"step_id": "third"}
    )
    example.check(
        capabilities=(host,),
        grants=(
            grant.model_copy(update={"available_before_tool_steps": (applicable,)}),
        ),
    )


def test_declared_fact_scope_is_not_inferred_from_matching_name():
    example = ProducerExample()
    fact = example.configured_question(mandatory=True)
    forged_profile = deepcopy(example.profile["ref"])
    forged_profile["artifact_id"] = "foreign-profile"
    effect = example.derive()
    changed = effect.declarations[0].model_copy(
        update={"profile_ref": ProfileContentRef.model_validate(forged_profile)}
    )
    compiled = effect.model_copy(
        update={"declarations": (changed, *effect.declarations[1:])}
    )
    example.rule_use(fact)
    with pytest.raises(BundleArtifactError, match="producer.catalogue_mismatch"):
        example.check(definition=compiled)


def test_unreachable_rule_still_rejects_a_missing_fact_producer_configuration():
    example = ProducerExample()
    fact = example.system_fact()
    example.rule_use(fact, set_applies=literal(value=False))
    with pytest.raises(BundleArtifactError, match="rule_type.producer_unavailable"):
        example.check()


@pytest.mark.parametrize("kind", ["rule", "rule_set"])
def test_inventoried_rule_artifact_requires_a_typed_profile_use(kind):
    example = ProducerExample()
    if kind == "rule":
        example.extra.append((example.rule_ref, example.rule(literal())))
    else:
        example.rule_use(literal())
        example.profile["definition"]["rule_set_refs"] = []
    with pytest.raises(BundleArtifactError, match=f"producer.{kind}_unbound_use"):
        example.check()
