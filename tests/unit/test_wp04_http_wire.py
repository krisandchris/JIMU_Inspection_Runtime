"""HTTP host config, byte-bound media, and strict data helpers."""

import json
from hashlib import sha256

import pytest
from fixtures.wp04.http_support import endpoint, transport_fixture
from pydantic import ValidationError

from jimu_runtime.bundle.schema_validation import (
    OfflineSchemaError,
    compile_offline_response_schema,
)
from jimu_runtime.gateway.http import VerifiedImage
from jimu_runtime.gateway.http_wire import WireError, strict_json

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
