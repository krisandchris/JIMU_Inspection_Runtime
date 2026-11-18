"""Incomplete references remain explicit data for X3 checks, not default authority."""

from copy import deepcopy

import pytest
from fixtures.wp01b.x3.support import fixture, parse
from pydantic import ValidationError

from jimu_runtime.domain.validation.authority_inputs import AuthorityQuestionInput
from jimu_runtime.domain.validation.producer_authority_inputs import (
    ProducerAuthorityValidationInput,
)


def test_zero_fact_system_absence_preserves_execution_for_later_pin_checks():
    value = parse(fixture())
    assert value.history.candidates.entries == ()
    result = value.history.system_results[0]
    assert result.output.disposition == "optional_absence"
    assert value.execution_pins[0].execution_id == result.execution_id
    assert value.history.proposed_selections[0].selected_fact_refs == ()
    assert not hasattr(value, "authority_verified")


@pytest.mark.parametrize("missing", ["execution_pins", "producer_catalogues"])
def test_missing_binding_is_not_malformed_shape_or_implicitly_verified(missing):
    data = fixture()
    data[missing] = []
    parsed = parse(data)
    assert parsed.history.system_results
    assert getattr(parsed, missing) == ()
    # The forthcoming X3 validator must return incomplete; shape parsing alone
    # does not execute any admission checks or infer default pins/catalogues.
    assert not hasattr(parsed, "report")


def test_equal_cross_source_content_is_accepted_without_wrapper_class_comparison():
    data = fixture()
    data["history"]["questions"] = deepcopy(
        data["producer_catalogues"][0]["context"]["questions"]
    )
    value = parse(data)
    assert isinstance(value.history.questions[0], AuthorityQuestionInput)
    assert (
        value.history.questions[0].definition
        == value.producer_catalogues[0].context.questions[0].definition
    )
    assert value.history.questions is not value.producer_catalogues[0].context.questions


def test_copied_containers_are_immutable_and_tainted_models_revalidated():
    data = fixture()
    value = parse(data)
    data["execution_pins"].clear()
    assert len(value.execution_pins) == 1
    with pytest.raises(ValidationError):
        value.execution_pins = ()
    tainted = value.model_copy(update={"unexpected": True})
    with pytest.raises(ValidationError):
        ProducerAuthorityValidationInput.model_validate(tainted)
    bad_pin = value.execution_pins[0].model_copy(update={"bundle_digest": "bad"})
    with pytest.raises(ValidationError):
        ProducerAuthorityValidationInput.model_validate(
            value.model_copy(update={"execution_pins": (bad_pin,)})
        )
    bad_context = value.producer_catalogues[0].model_copy(update={"unexpected": True})
    with pytest.raises(ValidationError):
        ProducerAuthorityValidationInput.model_validate(
            value.model_copy(update={"producer_catalogues": (bad_context,)})
        )
