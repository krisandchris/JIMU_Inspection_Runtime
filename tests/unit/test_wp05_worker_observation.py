"""Only an explicit N04 retry can consume independently classified failures."""

import pytest
from fixtures.wp01c.nodes.cases import ISSUE, cases, parse

from jimu_runtime.domain.nodes.observation import ObservationInput
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.observations import ObservationBatch

_QUALIFICATION = {
    "transport_error": ("unresolved", "unresolved"),
    "parse_error": ("untrusted", "unresolved"),
    "association_error": ("trusted", "untrusted"),
}


@pytest.mark.parametrize(
    ("error_class", "batch_outcome", "accepted"),
    [
        ("validation", "parse_error", True),
        ("association", "association_error", True),
        ("transport", "transport_error", True),
        ("validation", "association_error", False),
        ("association", "parse_error", False),
        ("validation", "transport_error", False),
        ("transport", "parse_error", False),
    ],
)
def test_batch_failure_matches_actual_invocation(
    error_class: str,
    batch_outcome: str,
    accepted: bool,
) -> None:
    wire = cases()
    inputs = parse(ObservationInput, wire["observation_input"])
    envelope, association = _QUALIFICATION[batch_outcome]
    batch = parse(
        ObservationBatch,
        wire["batch"]
        | {
            "outcome": batch_outcome,
            "envelope_validation": envelope,
            "association_validation": association,
            "question_results": [],
            "validation_issues": [ISSUE],
        },
    )
    invoice = parse(
        ModelInvocation,
        wire["observation_invocation"]
        | {
            "outcome": {
                "status": "failed",
                "error": {
                    "code": "fixture.failure",
                    "error_class": error_class,
                    "diagnostic_refs": [],
                },
            },
        },
    )
    if accepted:
        inputs.check_batch(batch, (invoice,), None)
    else:
        with pytest.raises(ValueError, match="actual invocation outcome"):
            inputs.check_batch(batch, (invoice,), None)
