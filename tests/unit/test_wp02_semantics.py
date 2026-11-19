"""Validate semantic source reconstruction across real compiled Bundle bytes."""

from fixtures.wp02.semantics.support import (
    build_inputs,
    compile_fixture,
    context_only_scenario,
    reopen,
    validate_reopened,
)

from jimu_runtime.bundle.semantic_inputs import build_tool_mapping_uses
from jimu_runtime.domain.tool_mappings import (
    RegionContextRegistration,
    SnapshotContextRegistration,
)


def test_compile_rebuilds_context_only_mappings_without_unconsumed_object_or_image(
    tmp_path,
):
    report = compile_fixture(tmp_path, context_only_scenario())
    _, catalogue, closure = reopen(report)
    inputs = build_inputs(catalogue, closure)
    (profile,) = inputs.use_contexts
    assert profile.object_geometry_schema_ref is None
    assert profile.image_reference_schema_ref is None
    roots = {type(item) for item in profile.context_registrations}
    assert roots == {RegionContextRegistration, SnapshotContextRegistration}
    (mapping,) = build_tool_mapping_uses(catalogue, inputs, profile)
    assert mapping.object_schema_ref is mapping.image_schema_ref is None
    semantic_report = validate_reopened(report)
    assert semantic_report.accepted
    assert any(
        check.check_id == "definitions.tool_plans.passed"
        for check in semantic_report.checks
    )


def test_recomputed_semantics_displays_individual_checks_and_exact_input_pins(tmp_path):
    report = compile_fixture(tmp_path)
    manifest, _, _ = reopen(report)
    rebuilt = validate_reopened(report)
    assert rebuilt.accepted
    assert rebuilt.bundle_digest == manifest.integrity.root_digest
    assert rebuilt.capability_snapshot_pin.identity == "fixture.capabilities"
    checks = {check.check_id: check.status for check in rebuilt.checks}
    assert checks["semantics.protocol_io"] == "passed"
    assert checks["semantics.producer_authority_admission"] == "passed"
    assert checks["semantics.mandatory_producer_completeness"] == "passed"
    assert checks["definitions.deferred.condition_evaluation"] == "deferred"
    assert checks["definitions.deferred.external_authenticity"] == "deferred"


def test_profile_contexts_cover_all_configured_regions(tmp_path):
    from fixtures.wp02.semantics.support import two_region_scenario

    report = compile_fixture(
        tmp_path, two_region_scenario(second_uses_same_schema=True)
    )
    _, catalogue, closure = reopen(report)
    inputs = build_inputs(catalogue, closure)
    regions = {
        item.region_ref.artifact_id for item in inputs.use_contexts if item.region_ref
    }
    assert "second" in regions
    assert len(regions) == 2


def test_exact_profile_gate_limits_region_use_without_evaluating_context(tmp_path):
    from fixtures.wp02.semantics.support import two_region_scenario

    report = compile_fixture(tmp_path, two_region_scenario(restrict_to_original=True))
    _, catalogue, closure = reopen(report)
    (use,) = build_inputs(catalogue, closure).use_contexts
    assert use.region_ref is not None and use.region_ref.artifact_id != "second"


def test_question_prompt_reads_exact_declared_region_path(tmp_path):
    from fixtures.wp02.semantics.support import prompt_context_scenario

    report = compile_fixture(tmp_path, prompt_context_scenario())
    assert report.succeeded, report.errors
    assert validate_reopened(report).accepted


def test_geometry_vocabulary_and_pinned_asset_bytes_are_checked(tmp_path):
    from fixtures.wp02.semantics.support import geometry_scenario

    report = compile_fixture(tmp_path, geometry_scenario())
    assert report.succeeded, report.errors
    result = validate_reopened(report)
    assert (
        next(
            check.status
            for check in result.checks
            if check.check_id == "semantics.geometry_body_and_asset_bytes"
        )
        == "passed"
    )
