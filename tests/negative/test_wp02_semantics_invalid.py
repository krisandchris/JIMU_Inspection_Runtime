"""Reject byte-consistent configurations and independently changed host pins."""

import pytest
from fixtures.wp02.compiler.scenario import (
    QUESTION,
    VEHICLE,
    capability_snapshot,
    source_scenario,
)
from fixtures.wp02.semantics.support import (
    changed_source,
    compile_fixture,
    context_only_scenario,
    question_context_scenario,
    refresh_source,
    reopen,
    validate_reopened,
)
from fixtures.wp02.trust.support import AT

from jimu_runtime.bundle.artifacts import BundleArtifactError
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.bundle.semantic_inputs import build_definition_inputs
from jimu_runtime.domain.enums import ArtifactKind


def _codes(report):
    return {
        issue.code
        for check in report.checks
        for issue in check.issues
        if check.status in ("invalid", "incomplete")
    }


def test_reopen_rejects_revoked_protocol_despite_available_deployment(tmp_path):
    report = compile_fixture(tmp_path)
    snapshot = capability_snapshot()
    first, *rest = snapshot.runtime_protocols
    revoked = first.model_copy(update={"revoked_at": AT})
    altered = snapshot.model_copy(update={"runtime_protocols": (revoked, *rest)})
    result = validate_reopened(report, altered)
    assert not result.accepted
    assert "semantic_runtime_protocol_mismatch" in _codes(result)
    assert (
        next(
            check.status
            for check in result.checks
            if check.check_id == "semantics.host_model_deployments"
        )
        == "passed"
    )


def test_reopen_rejects_unsupported_workflow_with_same_runtime_version(tmp_path):
    report = compile_fixture(tmp_path)
    snapshot = capability_snapshot().model_copy(
        update={"supported_workflow_versions": ("1.1.0",)}
    )
    output = validate_reopened(report, snapshot)
    assert not output.accepted
    assert "semantic_workflow_version" in _codes(output)


def test_compile_rejects_wrong_question_component_with_valid_schema_bytes(tmp_path):
    source = changed_source(
        source_scenario(),
        ArtifactKind.RESPONSE_SCHEMA,
        f"{QUESTION}-result",
        lambda data: data["properties"].update(
            {"kind": {"const": "number"}, "value": {"type": "number"}}
        ),
    )
    report = compile_fixture(tmp_path, source=source)
    assert not report.succeeded
    assert "semantic_question_response_incompatible" in {
        issue.code for issue in report.errors
    }


def test_compile_rejects_snapshot_field_absent_from_provenance(tmp_path):
    source = changed_source(
        context_only_scenario(),
        ArtifactKind.POLICY,
        "context_provenance",
        lambda raw: raw["policy"].update(fields=[]),
        remove_at=("policy", "fields"),
    )
    report = compile_fixture(tmp_path, source=source)
    assert not report.succeeded
    assert "semantic_snapshot_field_unavailable" in {
        issue.code for issue in report.errors
    }


def test_compile_rejects_question_context_with_no_region_owner(tmp_path):
    source = question_context_scenario()
    source = refresh_source(
        source,
        tuple(
            item for item in source.artifacts if item.kind is not ArtifactKind.REGION
        ),
    )
    report = compile_fixture(tmp_path, source=source)
    assert not report.succeeded
    assert "semantic_question_context_owner" in {issue.code for issue in report.errors}


def test_compile_rejects_same_shape_region_schema_with_different_pin(tmp_path):
    from fixtures.wp02.semantics.support import two_region_scenario

    source = two_region_scenario()
    report = compile_fixture(tmp_path, source=source)
    assert not report.succeeded
    assert "semantic_question_context_owner" in {issue.code for issue in report.errors}


def test_compile_rejects_prompt_context_field_outside_question_allowlist(tmp_path):
    from fixtures.wp02.semantics.support import prompt_context_scenario

    report = compile_fixture(tmp_path, prompt_context_scenario(declared=False))
    assert not report.succeeded
    assert "definitions.question_context_allowlist" in {
        issue.code for issue in report.errors
    }


def test_compile_rejects_geometry_unit_in_wrong_vocabulary_domain(tmp_path):
    from fixtures.wp02.semantics.support import geometry_scenario

    report = compile_fixture(tmp_path, geometry_scenario(unit_domain="fixture.unknown"))
    assert not report.succeeded
    assert "semantic_vocabulary_domain" in {issue.code for issue in report.errors}


def test_definition_inputs_reject_closure_from_foreign_catalogue_with_same_keys(
    tmp_path,
):
    _, catalogue, closure = reopen(compile_fixture(tmp_path))
    updated = changed_source(
        source_scenario(),
        ArtifactKind.ENTITY,
        VEHICLE,
        lambda raw: raw.update(name="Revised synthetic vehicle"),
    )
    _, _, foreign = reopen(compile_fixture(tmp_path, source=updated))
    assert set(foreign.dependencies) == set(closure.dependencies)
    assert foreign.dependencies != closure.dependencies
    descriptors = build_schema_descriptors(catalogue, schema_index=closure.schemas)

    with pytest.raises(BundleArtifactError) as error:
        build_definition_inputs(
            catalogue, foreign, descriptors, reviews=(), validation_at=AT
        )

    assert error.value.code == "semantic_closure_mismatch"
    assert error.value.path == "manifest.json"
