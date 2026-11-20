"""Verify compiled and admitted scene structure in local disposable ledgers."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from trial_scenes import (
    build_mask_compile_input,
    build_sidewalk_goods_compile_input,
    capability_snapshot,
)

from examples.nonmotor_vehicle_illegal_parking.compilable import lifecycle
from examples.nonmotor_vehicle_illegal_parking.compilable.local_trust import (
    LocalEd25519,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    AT,
    JSON_LIMITS,
    LIMITS,
)
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.catalogue import build_catalogue
from jimu_runtime.bundle.compiler import CompileEnvironment, compile_bundle
from jimu_runtime.domain.enums import ArtifactKind


def check(builder, region, category, question_ids, directory):
    source = builder()
    case_dir = directory / region
    case_dir.mkdir()
    result = compile_bundle(
        source,
        limits=LIMITS,
        environment=CompileEnvironment(
            capabilities=capability_snapshot(source),
            trust=LocalEd25519(case_dir).as_trust_source(),
            validation_at=AT,
        ),
    )
    if result.bundle is None:
        raise RuntimeError(f"{source.metadata.bundle_id}: {result.errors}")
    files = read_bundle(result.bundle.archive_bytes, LIMITS)
    catalogue = build_catalogue(files.manifest, files.payloads, **JSON_LIMITS)
    (plan,) = catalogue.of_kind(ArtifactKind.TOOL_PLAN)
    (profile,) = catalogue.of_kind(ArtifactKind.PROFILE)
    (rule,) = catalogue.of_kind(ArtifactKind.RULE)
    (scoped_region,) = catalogue.of_kind(ArtifactKind.REGION)
    assert plan.value.steps == ()
    assert not catalogue.of_kind(ArtifactKind.TOOL)
    assert not catalogue.of_kind(ArtifactKind.GEOMETRY_RESOURCE)
    assert catalogue.resolve(profile.value.tool_plan_ref) == plan
    assert tuple(ref.artifact_id for ref in profile.value.question_refs) == question_ids
    assert (
        tuple(selector.fact_key for selector in rule.value.required_fact_selectors)
        == question_ids
    )
    assert rule.value.violation_category == category
    assert (
        catalogue.resolve(rule.value.applicability_policy_ref).value.policy.on_unknown
        == "block"
    )
    assert (
        catalogue.resolve(rule.value.rule_policy_ref).value.policy.required_guard
        == "block_unavailable"
    )
    assert scoped_region.ref.artifact_id == region

    default_submission = lifecycle.local_submission

    def submission(media):
        row = default_submission(media)
        region_context = row.context.model_copy(update={"region_id": region})
        return row.model_copy(update={"context": region_context})

    with (
        patch.object(lifecycle, "load_source", return_value=source),
        patch.object(lifecycle, "capability_snapshot", new=capability_snapshot),
        patch.object(
            lifecycle, "SCOPE", lifecycle.SCOPE.model_copy(update={"region_id": region})
        ),
        patch.object(lifecycle, "REGION", region),
        patch.object(lifecycle, "local_submission", new=submission),
    ):
        reopened = lifecycle.exercise_lifecycle(case_dir)
    assert result.bundle == reopened.bundle
    assert reopened.task.bundle_digest == reopened.execution.pins.bundle_digest
    assert (
        reopened.task.bundle_digest
        != reopened.current_activation.bundle_ref.bundle_digest
    )
    print(
        f"{source.metadata.bundle_id} {len(source.artifacts)} artifacts {result.bundle.bundle_ref.bundle_digest}"
    )
    print(
        "PASS: compiled, signed, admitted, activated, accepted, reopened; exact pins and empty ToolPlan"
    )


if __name__ == "__main__":
    with TemporaryDirectory() as tmp:
        check(
            build_mask_compile_input,
            "isolated_food_service",
            "food_service_worker_no_mask",
            ("food_service_role", "kitchen_position", "face_visibility", "mask_status"),
            Path(tmp),
        )
        check(
            build_sidewalk_goods_compile_input,
            "isolated_sidewalk",
            "sidewalk_goods_encroachment",
            ("sidewalk_encroachment", "placement_permission"),
            Path(tmp),
        )
