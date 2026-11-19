"""WP-02 reuses published detached signature references and capability reports."""

import json
from pathlib import Path

from fixtures.wp02.trust.support import ROOT, manifest, public_vector
from jsonschema import Draft202012Validator

from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest

ROOT_DIR = Path(__file__).resolve().parents[2]


def test_detached_signature_fixture_fits_existing_bundle_v1_schema() -> None:
    schema_path = (
        ROOT_DIR
        / "schemas/generated/runtime.bundle_manifest.BundleManifest.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    _, _, evidence, _ = public_vector()
    bundle = manifest((evidence.ref,))
    wire = bundle.model_dump(mode="json")
    Draft202012Validator(schema).validate(wire)
    assert BundleManifest.model_validate_json(bundle.model_dump_json()) == bundle
    assert set(wire["integrity"]["signatures"][0]) == {
        "signature_id",
        "version",
        "sha256",
        "signature_registry_id",
    }
    assert wire["integrity"]["root_digest"] == ROOT


def test_capability_assessment_uses_existing_admission_report_schema() -> None:
    from fixtures.wp02.trust.capability_cases import registration, requirement, snapshot
    from fixtures.wp02.trust.support import AT

    from jimu_runtime.bundle.capabilities import assess_capabilities
    from jimu_runtime.domain.runtime.releases import CapabilityReport

    schema_path = (
        ROOT_DIR / "schemas/generated/runtime.releases.CapabilityReport.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    bundle = manifest().model_copy(update={"required_capabilities": (requirement(),)})
    result = assess_capabilities(
        bundle,
        snapshot(registration()),
        validation_at=AT,
        optional=(requirement("absent", evaluated=False),),
    )
    wire = result.report.model_dump(mode="json")
    Draft202012Validator(schema).validate(wire)
    assert (
        CapabilityReport.model_validate_json(result.report.model_dump_json())
        == result.report
    )
    assert set(wire) == {"required", "optional"}
