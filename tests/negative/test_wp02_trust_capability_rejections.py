"""Fail closed for unavailable implementations, exact reviews and route contracts."""

from dataclasses import replace
from typing import Literal

import pytest
from fixtures.wp02.trust.capability_cases import (
    EVALUATION,
    MODEL_AT,
    registration,
    requirement,
    route_case,
    snapshot,
)
from fixtures.wp02.trust.support import AT, END, manifest
from pydantic import ValidationError

from jimu_runtime.bundle.capabilities import assess_capabilities
from jimu_runtime.domain.definition_enums import CapabilityFeature


@pytest.mark.parametrize(
    ("fault", "expected", "code"),
    [
        ("implementation", "unsupported", "capability.unsupported"),
        ("contract", "unsupported", "capability.unsupported"),
        ("features", "unsupported", "capability.unsupported"),
        ("revoked", "unsupported", "capability.unsupported"),
        ("review_missing", "not_evaluated", "capability.evaluation_unavailable"),
        ("review_revoked", "not_evaluated", "capability.evaluation_unavailable"),
        ("review_features", "not_evaluated", "capability.evaluation_unavailable"),
        ("review_digest", "not_evaluated", "capability.evaluation_unavailable"),
        ("review_registry", "not_evaluated", "capability.evaluation_unavailable"),
    ],
)
def test_required_capability_versions_features_and_evaluation(
    fault: str, expected: str, code: str
) -> None:
    required = requirement()
    host_entry = registration()
    reviewed = host_entry.evaluations[0]
    if fault == "implementation":
        host_entry = host_entry.model_copy(update={"implementation_version": "3.0.0"})
    elif fault == "contract":
        host_entry = host_entry.model_copy(update={"contract_version": "1.0.1"})
    elif fault == "features":
        host_entry = host_entry.model_copy(
            update={
                "supported_features": (CapabilityFeature.RGB_IMAGE,),
                "evaluations": (),
            }
        )
    elif fault == "revoked":
        host_entry = host_entry.model_copy(update={"revoked_at": AT})
    elif fault == "review_missing":
        host_entry = host_entry.model_copy(update={"evaluations": ()})
    elif fault == "review_revoked":
        host_entry = host_entry.model_copy(
            update={"evaluations": (reviewed.model_copy(update={"revoked_at": AT}),)}
        )
    elif fault == "review_features":
        host_entry = host_entry.model_copy(
            update={
                "evaluations": (reviewed.model_copy(update={"reviewed_features": ()}),)
            }
        )
    elif fault == "review_digest":
        required = required.model_copy(
            update={
                "evaluation_ref": EVALUATION.model_copy(update={"sha256": "ff" * 32})
            }
        )
    elif fault == "review_registry":
        required = required.model_copy(
            update={
                "evaluation_ref": EVALUATION.model_copy(
                    update={"review_registry_id": "other.registry"}
                )
            }
        )
    bundle = manifest().model_copy(update={"required_capabilities": (required,)})

    results = assess_capabilities(bundle, snapshot(host_entry), validation_at=AT)

    assert results.report.required[0].status == expected
    assert results.errors[0].code == code
    assert results.errors[0].path == ("required_capabilities", 0)


def test_optional_requirement_cannot_downgrade_a_required_identity() -> None:
    required = requirement()
    bundle = manifest().model_copy(update={"required_capabilities": (required,)})
    with pytest.raises(ValueError, match="disjoint"):
        assess_capabilities(
            bundle, snapshot(registration()), validation_at=AT, optional=(required,)
        )


def test_schema_runtime_range_and_snapshot_window_are_required() -> None:
    host = snapshot(runtime_version="1.12.0").model_copy(
        update={"bundle_schema_versions": ("2.0.0",)}
    )
    results = assess_capabilities(manifest(), host, validation_at=END)
    assert [issue.code for issue in results.errors] == [
        "capability.snapshot_time",
        "capability.bundle_schema",
        "capability.runtime_version",
    ]


def test_ambiguous_host_registration_is_invalid() -> None:
    with pytest.raises(ValidationError, match="identities must be unique"):
        snapshot(registration(), registration())


def test_diagnostics_are_bounded_and_report_remains_complete() -> None:
    declarations = tuple(
        requirement(f"absent-{i}", evaluated=False) for i in range(130)
    )
    bundle = manifest().model_copy(update={"required_capabilities": declarations})
    report = assess_capabilities(bundle, snapshot(), validation_at=AT)
    assert len(report.report.required) == 130
    assert len(report.errors) == 128
    assert report.errors[-1].code == "capability.issue_limit"


@pytest.mark.parametrize(
    ("fault", "issue_code"),
    [
        ("no_approval", "model.fallback_review_missing"),
        ("revoked_approval", "model.fallback_review_missing"),
        ("wrong_review_digest", "review.digest_mismatch"),
        ("wrong_model_version", "model.host_deployment_mismatch"),
        ("wrong_provider", "model.host_deployment_mismatch"),
        ("wrong_features", "model.host_deployment_mismatch"),
        ("wrong_schema", "model.host_deployment_mismatch"),
        ("revoked_deployment", "model.host_deployment_mismatch"),
    ],
)
def test_fallback_requires_active_independent_review_and_exact_host_contract(
    fault: str, issue_code: str
) -> None:
    case = route_case("fallback")
    home, fallback = case.host.model_deployments
    assert len(case.host.model_reviews) == 1
    registered_review = case.host.model_reviews[0]
    approvals = (registered_review,)
    if fault == "no_approval":
        approvals = ()
    elif fault == "revoked_approval":
        approvals = (registered_review.model_copy(update={"revoked_at": MODEL_AT}),)
    elif fault == "wrong_review_digest":
        review = registered_review.metadata
        wrong = review.report_ref.model_copy(update={"sha256": "ff" * 32})
        approvals = (
            registered_review.model_copy(
                update={"metadata": review.model_copy(update={"report_ref": wrong})}
            ),
        )
    elif fault == "wrong_model_version":
        fallback = fallback.model_copy(
            update={
                "identity": fallback.identity.model_copy(
                    update={"model_version": "2.0.0"}
                )
            }
        )
    elif fault == "wrong_provider":
        fallback = fallback.model_copy(
            update={
                "identity": fallback.identity.model_copy(
                    update={"provider_id": "another"}
                )
            }
        )
    elif fault == "wrong_features":
        fallback = fallback.model_copy(update={"supported_features": ()})
    elif fault == "wrong_schema":
        contract = fallback.contracts[0]
        new_output = contract.output_schema_ref.model_copy(update={"digest": "dd" * 32})
        fallback = fallback.model_copy(
            update={
                "contracts": (
                    contract.model_copy(update={"output_schema_ref": new_output}),
                )
            }
        )
    elif fault == "revoked_deployment":
        fallback = fallback.model_copy(update={"revoked_at": MODEL_AT})
    host = case.host.model_copy(
        update={"model_deployments": (home, fallback), "model_reviews": approvals}
    )

    report = replace(case, host=host).validate()

    assert report.aggregate_status in {"invalid", "incomplete"}
    assert issue_code in {
        issue.code for check in report.checks for issue in check.issues
    }


@pytest.mark.parametrize("mode", ["local", "fallback"])
def test_model_policy_cannot_self_approve_local_switch(
    mode: Literal["local", "fallback"],
) -> None:
    case = route_case(mode)
    report = replace(
        case, host=case.host.model_copy(update={"model_reviews": ()})
    ).validate()
    assert report.aggregate_status == "incomplete"
