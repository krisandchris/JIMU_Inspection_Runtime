"""HTTP host config, byte-bound media, and strict data helpers."""

import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fixtures.wp04.http_support import ChatPayload, endpoint, transport_fixture
from pydantic import ValidationError

from jimu_runtime.bundle.schema_validation import (
    OfflineSchemaError,
    compile_offline_response_schema,
)
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.observation import N04_REGIONS_MARKER
from jimu_runtime.gateway.http import ChatContract, HttpModelTransport, VerifiedImage
from jimu_runtime.gateway.http_wire import WireError, strict_json
from jimu_runtime.gateway.invocation import DispatchRequest

_LOOPBACK = "http://127.0.0.1:21000/v1/chat/completions"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://localhost/v1/chat/completions", "http_plaintext_denied"),
        ("http://example.invalid/v1/chat/completions", "http_plaintext_denied"),
        ("https://own.invalid/v1/chat/completions?", "http_invalid_endpoint"),
        ("https://own.invalid/v1/chat/completions#fragment", "http_invalid_endpoint"),
        (
            "https://user:synthetic-password@own.invalid/v1/chat/completions",
            "http_invalid_endpoint",
        ),
        ("https://@own.invalid/v1/chat/completions", "http_invalid_endpoint"),
        ("ftp://own.invalid/v1/chat/completions", "http_invalid_endpoint"),
        ("//own.invalid/v1/chat/completions", "http_invalid_endpoint"),
        ("https://own.invalid/v1/other", "http_invalid_endpoint"),
    ],
)
def test_endpoint_host_config_is_exact_and_rejections_hide_raw_url(url, code):
    with pytest.raises(ValidationError, match=code) as error:
        endpoint(url)
    assert "synthetic-password" not in str(error.value)
    assert url not in str(error.value)


def test_plaintext_needs_explicit_loopback_enablement_and_https_egress_defaults_off():
    with pytest.raises(ValidationError, match="http_plaintext_denied"):
        endpoint(_LOOPBACK, allow_plaintext_loopback=False)
    config = endpoint("https://own.invalid/v1/chat/completions")
    transport, route = transport_fixture(config)
    with pytest.raises(WireError, match="http_network_disabled"):
        transport.prepare(route, "Pending explicit egress enablement.")
    assert config.credential_ref is None


def test_descriptor_binds_connection_and_limits_without_exposing_location_in_repr():
    left = endpoint(_LOOPBACK)
    right = endpoint(_LOOPBACK.replace("21000", "21001"))
    more_bytes = endpoint(_LOOPBACK, request_bytes=8192)
    assert left.descriptor_pin.sha256 != right.descriptor_pin.sha256
    assert left.descriptor_pin.sha256 != more_bytes.descriptor_pin.sha256
    assert left.url not in repr(left)
    assert left.url not in repr(left.descriptor_pin)


def test_region_support_is_an_explicit_endpoint_pin_change():
    legacy = endpoint(_LOOPBACK)
    old_document = legacy.model_dump(
        mode="json", exclude={"supports_region_projection"}
    )
    old_bytes = json.dumps(old_document, sort_keys=True, separators=(",", ":")).encode()
    enabled = legacy.model_copy(update={"supports_region_projection": True})
    assert legacy.descriptor_pin.sha256 == sha256(old_bytes).hexdigest()
    assert enabled.descriptor_pin != legacy.descriptor_pin


@pytest.fixture
def signed_question_contract():
    _, route = transport_fixture(endpoint(_LOOPBACK))
    signed_schema = canonical_bytes(
        {
            "$id": "urn:jimu:region-wire",
            "$defs": {
                "code": {"type": "string", "enum": ["clear", "obstructed"]},
                "score": {"type": "number", "minimum": 0.1},
            },
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "finding": {"$ref": "#/$defs/code"},
                "score": {"$ref": "#/$defs/score"},
                "visible": {"type": "boolean"},
            },
            "required": ["finding", "visible", "score"],
        }
    )
    output_ref = route.binding.output_schema_ref.model_copy(
        update={"digest": sha256(signed_schema).hexdigest()}
    )
    return ChatContract(
        input_schema_ref=route.binding.input_schema_ref,
        output_schema_ref=output_ref,
        response_model=ChatPayload,
        accepts_image=True,
        response_schema=signed_schema,
        question_slots=("finding", "visible", "score"),
    )


def _region_route(config, base, selected):
    _, route = transport_fixture(config)
    route.binding.input_schema_ref = base.input_schema_ref
    route.binding.output_schema_ref = base.output_schema_ref
    route.operation.purposes = (ModelCapability.OBJECT_OBSERVATION,)
    route.operation.response_contract_digest = selected.response_schema_digest
    route.operation.contract_snapshot = SimpleNamespace(
        configured_output_schema_ref=base.output_schema_ref,
        response_schema_sha256=selected.response_schema_digest,
        effective_question_refs=tuple(
            SimpleNamespace(artifact_id=name) for name in selected.question_slots
        ),
    )
    return route


def test_region_projection_retains_signed_constraints_for_each_exact_subset(
    signed_question_contract,
):
    base = signed_question_contract
    finding = base.for_region_questions(("finding",))
    score_and_visibility = base.for_region_questions(("score", "visible"))
    assert (
        score_and_visibility.response_schema_bytes
        == base.for_region_questions(("visible", "score")).response_schema_bytes
    )
    finding_document = finding.response_schema_document()
    original_document = base.response_schema_document()
    assert finding_document == {
        **original_document,
        "properties": {"finding": original_document["properties"]["finding"]},
        "required": ["finding"],
    }
    assert finding.validate_response_content(
        '{"finding":"obstructed"}', {"finding": "obstructed"}
    ) == ((), ())
    assert score_and_visibility.validate_response_content(
        '{"score":0}', {"score": 0}
    ) == (
        ("score",),
        ("visible",),
    )
    with pytest.raises(WireError, match="offline_schema_question_envelope"):
        finding.validate_response_content('{"score":5}', {"score": 5})


def test_prepare_and_dispatch_reject_regions_on_legacy_endpoint(
    signed_question_contract,
):
    base = signed_question_contract
    selected = base.for_region_questions(("score",))
    wire_prompt = f"{N04_REGIONS_MARKER}\n{{}}"
    image = b"\xff\xd8\xffsynthetic-image"
    verified = VerifiedImage(image, "image/jpeg", sha256(image).hexdigest())
    legacy = endpoint(_LOOPBACK)
    enabled = legacy.model_copy(update={"supports_region_projection": True})
    approved_transport = HttpModelTransport((enabled,), (base,), max_concurrency=1)
    allowed_route = _region_route(enabled, base, selected)
    payload = approved_transport.prepare(allowed_route, wire_prompt, verified)
    assert json.loads(payload)["response_format"]["json_schema"]["schema"] == (
        selected.response_schema_document()
    )
    with pytest.raises(WireError, match="http_schema_pin_unregistered"):
        approved_transport.prepare(allowed_route, "Legacy observation", verified)
    for untrusted_route, expected in (
        (allowed_route, "http_endpoint_pin_mismatch"),
        (_region_route(legacy, base, selected), "http_region_projection_unsupported"),
    ):
        legacy_transport = HttpModelTransport((legacy,), (base,), max_concurrency=1)
        with pytest.raises(WireError, match=expected):
            legacy_transport.prepare(untrusted_route, wire_prompt, verified)
        result = asyncio.run(
            legacy_transport.dispatch(
                DispatchRequest("projection-rejected", untrusted_route, payload)
            )
        )
        assert not result.dispatched
        assert result.error is not None and result.error.code == expected


def test_region_projection_requires_registered_base_and_exact_derived_pin(
    signed_question_contract,
):
    base = signed_question_contract
    selected = base.for_region_questions(("score",))
    config = endpoint(_LOOPBACK).model_copy(update={"supports_region_projection": True})
    transport = HttpModelTransport((config,), (base,), max_concurrency=1)
    unpinned = HttpModelTransport((config,), (selected,), max_concurrency=1)
    wire_prompt = f"{N04_REGIONS_MARKER}\n{{}}"
    image = b"\xff\xd8\xffsynthetic-image"
    verified = VerifiedImage(image, "image/jpeg", sha256(image).hexdigest())
    route = _region_route(config, base, selected)
    with pytest.raises(WireError, match="http_schema_pin_unregistered"):
        unpinned.prepare(route, wire_prompt, verified)
    route.operation.contract_snapshot.configured_output_schema_ref = (
        base.output_schema_ref.model_copy(update={"version": "2.0.0"})
    )
    with pytest.raises(WireError, match="http_region_projection_contract_mismatch"):
        transport.prepare(route, wire_prompt, verified)
    route = _region_route(config, base, selected)
    route.operation.response_contract_digest = base.response_schema_digest
    route.operation.contract_snapshot.response_schema_sha256 = (
        base.response_schema_digest
    )
    with pytest.raises(WireError, match="http_schema_pin_unregistered"):
        transport.prepare(route, wire_prompt, verified)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (b'{"k":1,"k":2}', "http_json_duplicate_key"),
        (b'{"k":{"a":1,"a":2}}', "http_json_duplicate_key"),
        (b'{"k":NaN}', "http_json_nonfinite"),
        (b'{"k":1e999}', "http_json_nonfinite"),
        (b'{"k":-Infinity}', "http_json_nonfinite"),
        (b'{"k":"\\ud800"}', "http_invalid_json"),
        (b'{"k":"\xff"}', "http_invalid_json"),
        (b"[" * 66 + b"0" + b"]" * 66, "http_json_excess_depth"),
    ],
)
def test_ambiguous_or_unbounded_json_is_rejected(value, code):
    with pytest.raises(WireError, match=code):
        strict_json(value)


def test_prepare_checks_media_integrity_content_and_profile_permissions():
    image = b"\xff\xd8\xff" + b"synthetic-image"
    valid = VerifiedImage(image, "image/jpeg", sha256(image).hexdigest())
    transport, route = transport_fixture(endpoint(_LOOPBACK))
    assert (
        json.loads(transport.prepare(route, "Inspect", valid))["model"]
        == route.ownership.model_id
    )

    bad_digest = VerifiedImage(image, "image/jpeg", "0" * 64)
    bad_type = VerifiedImage(image, "image/png", sha256(image).hexdigest())
    with pytest.raises(WireError, match="http_image_digest_mismatch"):
        transport.prepare(route, "Inspect", bad_digest)
    with pytest.raises(WireError, match="http_invalid_image"):
        transport.prepare(route, "Inspect", bad_type)

    text_only, route = transport_fixture(endpoint(_LOOPBACK), accepts_image=False)
    with pytest.raises(WireError, match="http_image_not_allowed"):
        text_only.prepare(route, "Inspect", valid)


@pytest.mark.parametrize(
    ("schema", "code"),
    [
        (b'{"minimum":1e-400}', "http_json_number_precision"),
        (b'{"minimum":1.00000000000000000001}', "http_json_number_precision"),
        (b'{"$ref":"https://invalid.example/schema.json"}', "offline_schema_reference"),
        (
            b'{"$defs":{"self":{"$ref":"#/$defs/self"}},"$ref":"#/$defs/self"}',
            "offline_schema_reference",
        ),
        (b'{"type":"string","pattern":"(a+)+$"}', "offline_schema_keyword"),
        (
            b'{"$defs":{"nested":{"$id":"urn:other","$ref":"#/$defs/value"},"value":{"type":"string"}},"$ref":"#/$defs/nested"}',
            "offline_schema_dialect",
        ),
        (
            b'{"$defs":{"unused":{"$id":"urn:other","type":"string"}},"type":"object"}',
            "offline_schema_dialect",
        ),
    ],
)
def test_host_registration_rejects_lossy_or_unbounded_schema(schema, code):
    with pytest.raises(WireError, match=code):
        transport_fixture(endpoint(_LOOPBACK), response_schema=schema)


def test_local_definition_and_numeric_minimum_remain_exact():
    contract = compile_offline_response_schema(
        {
            "$id": "urn:jimu:offline",
            "$defs": {"score": {"type": "number", "minimum": 0.1}},
            "type": "object",
            "properties": {"score": {"$ref": "#/$defs/score"}},
            "required": ["score"],
            "additionalProperties": False,
        }
    )
    assert contract.validate(strict_json('{"score":0.1}', exact_numbers=True)) == (
        (),
        (),
    )
    with pytest.raises(OfflineSchemaError, match="offline_schema_value_mismatch"):
        contract.validate(
            strict_json('{"score":0.09999999999999999999}', exact_numbers=True)
        )
