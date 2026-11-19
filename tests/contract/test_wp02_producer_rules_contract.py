"""System output grants pin their exact Tool consumption phase."""

import pytest
from fixtures.wp02.producer_rules.support import ProducerExample
from pydantic import ValidationError

from jimu_runtime.bundle.producer_rules import SystemProducerGrant


def test_grant_requires_explicit_profile_plan_step_pins():
    example = ProducerExample()
    _, system = example.grant_system()
    with pytest.raises(ValidationError, match="profile_ref"):
        SystemProducerGrant.model_validate(
            {
                "declaration": system,
                "available_before_tool_steps": ({"step_id": "step"},),
            }
        )
    with pytest.raises(ValidationError, match="available_before_tool_steps"):
        SystemProducerGrant.model_validate(
            {
                "declaration": system,
                "available_before_tool_steps": True,
            }
        )
