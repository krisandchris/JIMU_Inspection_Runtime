"""Verify the regional and legacy signed scene Bundles can coexist."""

from collections.abc import Callable
from pathlib import Path

import pytest

from examples.live_inspection.parking import scene as parking
from examples.live_inspection.scenes import bundles
from examples.live_inspection.scenes.trusted_scope import MASK_REGION, SIDEWALK_REGION
from examples.nonmotor_vehicle_illegal_parking.compilable import scene as base
from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    local_submission,
    prepare_scene,
)
from jimu_runtime.bundle.archive import read_bundle
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, build_catalogue
from jimu_runtime.bundle.compiler import BundleCompileInput
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.nodes.observation import is_region_observation_template


def _observation_prompt(catalogue: ArtifactCatalogue) -> str:
    (binding,) = (
        row.value
        for row in catalogue.of_kind(ArtifactKind.MODEL_BINDING)
        if row.ref.artifact_id == "object_observation"
    )
    return catalogue.resolve(binding.prompt_template_ref).value.template_text


@pytest.mark.parametrize(
    ("build", "capabilities", "region_id"),
    (
        (parking.build_source, parking.capability_snapshot, base.REGION),
        (bundles.build_mask_compile_input, bundles.capability_snapshot, MASK_REGION),
        (
            bundles.build_sidewalk_goods_compile_input,
            bundles.capability_snapshot,
            SIDEWALK_REGION,
        ),
    ),
)
def test_signed_regional_bundle_admits_alongside_legacy(
    tmp_path: Path,
    build: Callable[..., BundleCompileInput],
    capabilities: Callable[[BundleCompileInput], CapabilitySnapshot],
    region_id: str,
) -> None:
    legacy = build()
    regional = build(region_projection=True)
    submission = local_submission(b"isolated bundle test")
    submission = submission.model_copy(
        update={
            "context": submission.context.model_copy(update={"region_id": region_id})
        }
    )
    prepared = prepare_scene(tmp_path, legacy, capabilities(legacy), submission)
    assert not is_region_observation_template(_observation_prompt(prepared.catalogue))
    bundle, signed, receipt = prepared.admit(regional, "regional")
    assert prepared.bundle.bundle_ref.bundle_digest != bundle.bundle_ref.bundle_digest
    assert receipt.bundle_version == (
        parking.REGION_PROJECTION_BUNDLE_VERSION
        if region_id == base.REGION
        else bundles.REGION_PROJECTION_BUNDLE_VERSION
    )
    assert receipt.bundle_id == prepared.bundle.bundle_ref.bundle_id
    assert prepared.store.get_admission("admit.synthetic.scene.initial") is not None

    files = read_bundle(signed, base.LIMITS)
    catalogue = build_catalogue(files.manifest, files.payloads, **base.JSON_LIMITS)
    assert is_region_observation_template(_observation_prompt(catalogue))
    question_prompts = {
        row.ref.artifact_id: row.value.template_text
        for row in catalogue.of_kind(ArtifactKind.PROMPT_TEMPLATE)
        if row.value.purpose.value == "object_observation"
    }
    assert all(
        legacy_term not in text
        for text in question_prompts.values()
        for legacy_term in ("red-box", "red outline", "highlighted", "marked image")
    )
    if region_id == SIDEWALK_REGION:
        assert "[290, 400, 859, 957]" in question_prompts["sidewalk_encroachment"]
