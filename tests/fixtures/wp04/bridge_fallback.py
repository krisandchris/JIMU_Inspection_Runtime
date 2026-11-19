"""Admitted two-deployment fallback configuration for the loopback Worker probe."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from jimu_runtime.bundle.capabilities import CapabilitySnapshot, ModelReviewRegistration
from jimu_runtime.bundle.compiler import (
    BundleCompileInput,
    _resolve_members,
    _source_documents,
)
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.validation.review_inputs import (
    BindingReviewScope,
    FallbackTransitionAssessment,
    ReviewBindingRef,
    ReviewMetadata,
)
from tests.fixtures.wp02.compiler.scenario import (
    DEPLOYMENT,
    LIMITS,
    VERSION,
    approve_source_bytes,
    capability_snapshot,
)

FALLBACK = "fixture.remote.alternate"
_PRIMARY = capability_snapshot().model_deployments[0]
_ALTERNATE = _PRIMARY.model_copy(
    update={
        "identity": _PRIMARY.identity.model_copy(update={"deployment_id": FALLBACK})
    }
)
PROOF = ReviewReportRef(
    review_registry_id="fixture-review",
    report_id="fixture-transition",
    version=VERSION,
    sha256=sha256_digest(b"fixture transition report"),
)


def fallback_admission(
    source: BundleCompileInput,
) -> tuple[BundleCompileInput, CapabilitySnapshot]:
    rows = []
    for entry in source.artifacts:
        if entry.kind is ArtifactKind.POLICY and entry.artifact_id in {
            "provider",
            "fallback",
        }:
            body = json.loads(entry.content)
            policy = body["policy"]
            if entry.artifact_id == "provider":
                alternate = deepcopy(policy["deployments"][0])
                alternate.update(deployment_id=FALLBACK)
                policy["deployments"].append(alternate)
            else:
                policy.update(
                    mode="explicit_evaluated",
                    trigger_errors=["unavailable"],
                    allowed_transitions=[
                        {"from_deployment_id": DEPLOYMENT, "to_deployment_id": FALLBACK}
                    ],
                    evaluation_refs=[PROOF.model_dump(mode="json")],
                )
            entry = replace(entry, content=json.dumps(body).encode())
        rows.append(entry)
    approved = approve_source_bytes(replace(source, artifacts=tuple(rows)))
    documents, _ = _source_documents(approved, LIMITS)
    assessments = []
    for member in _resolve_members(documents, LIMITS):
        if member.ref.artifact_kind is not ArtifactKind.MODEL_BINDING:
            continue
        binding = ModelBinding.model_validate(member.value)
        assessments.append(
            FallbackTransitionAssessment(
                assessment_kind="fallback_transition",
                source=_PRIMARY.identity,
                target=_ALTERNATE.identity,
                capability=binding.capability,
                scope=BindingReviewScope(
                    scope_kind="binding",
                    binding_refs=(
                        ReviewBindingRef.model_validate(member.ref.model_dump()),
                    ),
                ),
                trigger_errors=("unavailable",),
                input_schema_ref=binding.input_schema_ref,
                output_schema_ref=binding.output_schema_ref,
                contract_compatibility="exact",
                quality_evaluated=True,
            )
        )
    snapshot = capability_snapshot().model_copy(
        update={
            "model_deployments": (_PRIMARY, _ALTERNATE),
            "model_reviews": (
                ModelReviewRegistration(
                    metadata=ReviewMetadata(
                        report_ref=PROOF,
                        verdict="approved",
                        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
                        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
                        assessments=tuple(assessments),
                    ),
                    revoked_at=None,
                ),
            ),
        }
    )
    return approved, snapshot
