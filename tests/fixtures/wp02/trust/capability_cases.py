"""Finite host capability and gateway review fixtures."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fixtures.wp02.trust.support import END, START, VERSION, pin
from pydantic import TypeAdapter

from jimu_runtime.bundle.capabilities import (
    CapabilityEvaluation,
    CapabilityRegistration,
    CapabilitySnapshot,
    DeploymentContract,
    ModelDeploymentRegistration,
    ModelReviewRegistration,
    validate_model_deployments,
)
from jimu_runtime.domain.definition_enums import CapabilityFamily, CapabilityFeature
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    ReviewReportRef,
)
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.schema_types import SchemaRef
from jimu_runtime.domain.validation.issues import ValidationReport
from jimu_runtime.domain.validation.policies import (
    PolicyRegistration,
    PromptRegistration,
)
from jimu_runtime.domain.validation.review_inputs import (
    ReviewedDeployment,
    ReviewMetadata,
)

FEATURE = CapabilityFeature.STRUCTURED_OUTPUT
MODEL_AT = datetime(2026, 6, 1, tzinfo=UTC)
EVALUATION = ReviewReportRef(
    review_registry_id="fixture.review",
    report_id="tool-quality",
    version=VERSION,
    sha256="b3" * 32,
)


def requirement(
    name: str = "supported-tool",
    *,
    family: CapabilityFamily = CapabilityFamily.TOOL_IMPLEMENTATION,
    evaluated: bool = True,
) -> ExternalCapabilityRequirement:
    args = {
        "family": family,
        "capability_id": name,
        "contract_version": VERSION,
        "allowed_implementation_versions": ("2.0.0",),
        "required_features": (FEATURE,),
    }
    if evaluated:
        args["evaluation_ref"] = EVALUATION
    return ExternalCapabilityRequirement.model_validate(args)


def registration(
    name: str = "supported-tool",
    *,
    family: CapabilityFamily = CapabilityFamily.TOOL_IMPLEMENTATION,
    before: datetime = END,
) -> CapabilityRegistration:
    evaluation = CapabilityEvaluation(
        ref=EVALUATION,
        reviewed_features=(FEATURE,),
        valid_from=START,
        valid_until=END,
        revoked_at=None,
    )
    return CapabilityRegistration(
        family=family,
        capability_id=name,
        contract_version=VERSION,
        implementation_version="2.0.0",
        supported_features=(FEATURE,),
        evaluations=(evaluation,),
        valid_from=START,
        valid_until=before,
        revoked_at=None,
    )


def snapshot(
    *entries: CapabilityRegistration, runtime_version: str = "1.10.0"
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        pin=pin("fixture.capability.snapshot"),
        runtime_version=runtime_version,
        bundle_schema_versions=(VERSION,),
        valid_from=START,
        valid_until=END,
        capabilities=entries,
        model_deployments=(),
        model_reviews=(),
    )


@dataclass(frozen=True)
class RouteCase:
    binding: ModelBinding
    binding_ref: ModelBindingContentRef
    catalogue: tuple[PolicyRegistration, ...]
    prompt: PromptRegistration
    host: CapabilitySnapshot

    def validate(self) -> ValidationReport:
        return validate_model_deployments(
            self.binding,
            self.catalogue,
            self.host,
            MODEL_AT,
            binding_ref=self.binding_ref,
            prompts=(self.prompt,),
        )


def route_case(
    mode: Literal["primary", "fallback", "local"] = "primary",
) -> RouteCase:
    path = Path(__file__).resolve().parents[2] / "wp01b/p1/contracts.json"
    data = json.loads(path.read_text())
    for entry in data["registrations"]:
        policy_id = entry["ref"]["artifact_id"]
        if mode == "fallback" and policy_id == "fallback":
            entry["artifact"] = data["evaluated_fallback"]
        if mode == "local" and policy_id == "provider":
            entry["artifact"] = data["local_provider"]
    catalogue = TypeAdapter(tuple[PolicyRegistration, ...]).validate_json(
        json.dumps(data["registrations"])
    )
    model = ModelBinding.model_validate(data["model"])
    review = ReviewMetadata.model_validate_json(json.dumps(data["review"]))
    features = (FEATURE, CapabilityFeature.RGB_IMAGE)
    contract = DeploymentContract(
        capability="object_observation",
        input_schema_ref=SchemaRef.model_validate(
            data["review"]["assessments"][0]["input_schema_ref"]
        ),
        output_schema_ref=SchemaRef.model_validate(
            data["review"]["assessments"][0]["output_schema_ref"]
        ),
    )
    deployments = tuple(
        ModelDeploymentRegistration(
            identity=identity,
            supported_features=features,
            contracts=(contract,),
            valid_from=review.valid_from,
            valid_until=review.valid_until,
            revoked_at=None,
        )
        for identity in (
            ReviewedDeployment.model_validate(
                data["review"]["assessments"][0]["source"]
            ),
            ReviewedDeployment.model_validate(
                data["review"]["assessments"][0]["target"]
            ),
        )
    )
    host = snapshot().model_copy(
        update={
            "valid_from": review.valid_from,
            "valid_until": review.valid_until,
            "model_deployments": deployments,
            "model_reviews": (
                ModelReviewRegistration(metadata=review, revoked_at=None),
            )
            if mode != "primary"
            else (),
        }
    )
    return RouteCase(
        model,
        ModelBindingContentRef.model_validate(data["binding_ref"]),
        catalogue,
        PromptRegistration.model_validate(data["prompt"]),
        host,
    )
