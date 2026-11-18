"""Intrinsic Catalogue identity and output-slot semantics, without evaluation."""

import json
from pathlib import Path

from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.producers import (
    FactProducerDeclaration,
    ProducerCatalogue,
    ProfileContentRef,
    ToolConditionActivation,
)
from jimu_runtime.domain.runtime.facts import ProfileContentRef as RuntimeProfileRef
from jimu_runtime.domain.tool_mappings import ProfileContentRef as MappingProfileRef


def test_profile_reference_reuses_low_level_class_and_compares_cross_wrapper_identity() -> (
    None
):
    assert ProfileContentRef is MappingProfileRef
    raw = fixture("question")
    runtime = RuntimeProfileRef.model_validate(raw["profile_ref"])
    declaration = FactProducerDeclaration.model_validate(raw)
    assert type(runtime) is not type(declaration.profile_ref)
    assert reference_identity(runtime) == reference_identity(declaration.profile_ref)
    checked = RuntimeProfileRef.model_validate(runtime)
    raw["profile_ref"] = ProfileContentRef.model_validate(
        checked.model_dump(mode="json")
    )
    assert FactProducerDeclaration.model_validate(raw) == declaration


def fixture(name: str) -> dict:
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/wp01b/x2/contracts.json").read_text()
    )[name]


def test_question_reuse_is_one_primary_per_exact_profile_not_global_question() -> None:
    first = fixture("question")
    second = fixture("question")
    second["declaration_id"] = "question.other_profile"
    second["profile_ref"]["artifact_id"] = "other-profile"
    root = fixture("catalogue")
    root["declarations"] = [first, second]
    assert len(ProducerCatalogue.model_validate(root).declarations) == 2


def test_one_tool_step_can_have_multiple_distinct_declared_outputs() -> None:
    first = fixture("tool")
    second = fixture("tool")
    second.update(
        declaration_id="tool.extra", output_name="extra", fact_key="extra.fact"
    )
    root = fixture("catalogue")
    root["declarations"] = [first, second]
    value = ProducerCatalogue.model_validate(root)
    assert value.declarations[0].producer == value.declarations[1].producer
    assert len(value.declarations) == 2


def test_shape_valid_operand_mismatch_is_not_certified_type_correct() -> None:
    raw = fixture("question")
    raw["activation_condition"] = {
        "mode": "tool_condition",
        "condition_ast": {
            "kind": "comparison",
            "operator": "eq",
            "left": {
                "kind": "literal",
                "constant": {
                    "type_definition": {"kind": "integer"},
                    "value": {"kind": "integer", "value": 1},
                },
            },
            "right": {
                "kind": "literal",
                "constant": {
                    "type_definition": {"kind": "string"},
                    "value": {"kind": "string", "value": "one"},
                },
            },
        },
    }
    root = fixture("catalogue")
    root["declarations"] = [raw]
    value = ProducerCatalogue.model_validate(root)
    assert isinstance(
        value.declarations[0].activation_condition, ToolConditionActivation
    )
    assert not hasattr(value, "activation_truth")
