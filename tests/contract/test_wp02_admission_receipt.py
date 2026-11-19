"""Admission uses the existing C receipt contract for verified Bundle identities."""

import json
from pathlib import Path

from fixtures.wp02.admission.support import build_scenario, sign_bundle, submit
from jsonschema import Draft202012Validator, FormatChecker

from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.runtime.releases import AdmissionReceipt

_RECEIPT_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "schemas/generated/runtime.releases.AdmissionReceipt.schema.json"
)


def test_public_admission_receipt_satisfies_frozen_c_wire_contract(
    tmp_path: Path,
) -> None:
    compiled = build_scenario(tmp_path)
    signed = sign_bundle(
        tmp_path, compiled.archive_bytes, compiled.bundle_ref.bundle_digest
    )
    store = LocalBundleStore(tmp_path / "store")
    accepted = submit(store, signed.archive, "accepted", signed.environment)
    rejected = submit(store, signed.unsigned, "rejected", signed.environment)
    assert accepted.accepted and not rejected.accepted
    validator = Draft202012Validator(
        json.loads(_RECEIPT_SCHEMA.read_text()), format_checker=FormatChecker()
    )
    for record in (accepted, rejected):
        assert record.receipt is not None
        wire = record.receipt.model_dump_json()
        validator.validate(json.loads(wire))
        assert AdmissionReceipt.model_validate_json(wire) == record.receipt
