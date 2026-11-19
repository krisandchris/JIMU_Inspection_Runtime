"""Required and optional capability availability and configured model review checks."""

from typing import Literal

import pytest
from fixtures.wp02.trust.capability_cases import (
    registration,
    requirement,
    route_case,
    snapshot,
)
from fixtures.wp02.trust.support import AT, END, manifest

from jimu_runtime.bundle.capabilities import assess_capabilities
from jimu_runtime.domain.definition_enums import CapabilityFamily


def test_optional_warning_does_not_block_supported_required_capability() -> None:
    required = requirement()
    optional = requirement("unavailable", evaluated=False)
    bundle = manifest().model_copy(update={"required_capabilities": (required,)})

    results = assess_capabilities(
        bundle, snapshot(registration()), validation_at=AT, optional=(optional,)
    )

    assert results.errors == ()
    assert [issue.code for issue in results.warnings] == ["capability.unsupported"]
    assert results.report.required[0].status == "supported"
    assert results.report.optional[0].status == "unsupported"


def test_active_system_producer_registrations_reuse_domain_view() -> None:
    current = registration("producer", family=CapabilityFamily.SYSTEM_FACT_PRODUCER)
    revoked = registration(
        "revoked", family=CapabilityFamily.SYSTEM_FACT_PRODUCER
    ).model_copy(update={"revoked_at": AT})
    host = snapshot(current, revoked, registration())
    (view,) = host.system_capabilities_at(AT)

    assert view.registry_id == host.pin.identity
    assert view.capability_id == current.capability_id
    assert view.available_implementation_versions == ("2.0.0",)
    assert view.supported_features == current.supported_features
    assert host.system_capabilities_at(END) == ()


@pytest.mark.parametrize(
    ("mode", "review_check"),
    [
        ("primary", "model.fallback_disabled"),
        ("fallback", "model.fallback_coverage"),
        ("local", "model.local_exception"),
    ],
)
def test_model_registration_matches_remote_primary_or_explicit_review(
    mode: Literal["primary", "fallback", "local"], review_check: str
) -> None:
    report = route_case(mode).validate()

    assert report.aggregate_status == "passed"
    assert review_check in {entry.check_id for entry in report.checks}
    assert report.checks[-1].check_id == "model.host_deployments"


def test_runtime_protocol_and_workflow_are_scoped_to_current_snapshot() -> None:
    from fixtures.wp02.trust.capability_cases import MODEL_AT

    from jimu_runtime.bundle.capabilities import (
        CapabilitySnapshot,
        RuntimeProtocolRegistration,
    )

    case = route_case()
    binding = case.binding
    protocol = RuntimeProtocolRegistration(
        role=binding.capability,
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        protocol_version="2.0.0",
        valid_from=case.host.valid_from,
        valid_until=case.host.valid_until,
        revoked_at=None,
    )
    revoked = protocol.model_copy(
        update={"role": "tool_execution", "revoked_at": MODEL_AT}
    )
    host = CapabilitySnapshot.model_validate(
        case.host.model_copy(
            update={
                "runtime_protocols": (protocol, revoked),
                "supported_workflow_versions": ("1.0.0", "1.1.0"),
            }
        )
    )

    assert host.protocols_at(MODEL_AT) == (protocol,)
    assert host.protocols_at(host.valid_until) == ()
    assert host.supports_workflow_at("1.1.0", MODEL_AT)
    assert not host.supports_workflow_at("1.1.1", MODEL_AT)
    assert not host.supports_workflow_at("1.1.0", host.valid_until)


def test_runtime_protocol_cannot_select_multiple_versions_for_an_exact_role_pair() -> (
    None
):
    from pydantic import ValidationError

    from jimu_runtime.bundle.capabilities import (
        CapabilitySnapshot,
        RuntimeProtocolRegistration,
    )

    case = route_case()
    binding = case.binding
    current = RuntimeProtocolRegistration(
        role=binding.capability,
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        protocol_version="2.0.0",
        valid_from=case.host.valid_from,
        valid_until=case.host.valid_until,
        revoked_at=None,
    )
    duplicate = current.model_copy(update={"protocol_version": "2.1.0"})
    with pytest.raises(ValidationError, match="registry identities must be unique"):
        CapabilitySnapshot.model_validate(
            case.host.model_copy(update={"runtime_protocols": (current, duplicate)})
        )
