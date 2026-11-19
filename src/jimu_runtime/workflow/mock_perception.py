"""Scoped fixture responses for Profile discovery and independent grounding."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import (
    ModelGroundingBinding,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding, PolicyContentRef
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    ProfileHit,
    SkippedDiscoveryOutput,
)
from jimu_runtime.domain.nodes.grounding import GroundingInput, GroundingOutput
from jimu_runtime.domain.policies import QuestionSelectionPolicyArtifact
from jimu_runtime.domain.runtime.applicability import ProfileEligibilityEvaluation
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    ModelInvocationRef,
    ObjectRef,
    ProfileContentRef,
    QuestionSelectionRef,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    GroundingRecord,
    GroundingRecordRef,
    ModelGroundingProvenance,
    ObjectRecord,
)
from jimu_runtime.domain.runtime.selection import AllDeclaredSelectionRecord
from jimu_runtime.workflow.mock_context import check_gate_scope, snapshot_artifact
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    SyntheticArtifact,
    SyntheticExecution,
    VerifiedExecutionScope,
    artifact,
    record_digest,
    synthetic_issue,
)


@dataclass(frozen=True, slots=True)
class SyntheticDiscoveryResult:
    output: DiscoveryAttemptOutput
    selections: tuple[AllDeclaredSelectionRecord, ...]
    invocations: tuple[ModelInvocation, ...]
    artifacts: tuple[SyntheticArtifact, ...]


@dataclass(frozen=True, slots=True)
class SyntheticGroundingResult:
    output: GroundingOutput
    records: tuple[GroundingRecord, ...]
    objects: tuple[ObjectRecord, ...]
    invocations: tuple[ModelInvocation, ...]
    artifacts: tuple[SyntheticArtifact, ...]


class SyntheticGroundingCase(ContractModel):
    profile_ref: ProfileContentRef
    object_count: int = Field(ge=0, le=16)
    disposition: Literal["complete", "partial", "failed"] = "complete"

    @model_validator(mode="after")
    def check_count(self) -> "SyntheticGroundingCase":
        if self.disposition == "failed" and self.object_count:
            raise ValueError("failed fixture grounding cannot return Objects")
        return self


def skipped_discovery(
    scope: VerifiedExecutionScope, gate: ProfileEligibilityEvaluation, node_run_id: str
) -> SkippedDiscoveryOutput:
    check_gate_scope(scope, gate)
    record = SkippedDiscoveryOutput(
        schema_version=VERSION,
        outcome="not_evaluated",
        node_id=NodeId.N02,
        status="skipped",
        reason_code="no_eligible_profiles",
        node_run_id=node_run_id,
        execution_id=scope.execution_id,
        profile_eligibility_evaluation_ref=gate.evaluation_ref,
    )
    record.check_gate(gate)
    return record


def discover(
    scope: SyntheticExecution,
    inputs: DiscoveryInput,
    snapshot: ContextSnapshot,
    gate: ProfileEligibilityEvaluation,
    at: datetime,
    *,
    hit_profiles: tuple[ProfileContentRef, ...] | None = None,
    failed_profiles: tuple[ProfileContentRef, ...] = (),
) -> SyntheticDiscoveryResult:
    inputs = DiscoveryInput.model_validate(inputs)
    call = scope.check_call(inputs.envelope, NodeId.N02, at)
    scope.check_snapshot(snapshot, inputs.context_snapshot_ref)
    check_gate_scope(scope, gate)
    inputs.check_gate(gate)
    if (
        inputs.context_snapshot_digest != record_digest(snapshot)
        or inputs.original_media_ref != scope.media.media_ref
    ):
        raise ValueError(
            "discovery requires the synthetic execution's exact image and Snapshot"
        )
    call.check_upstream(
        snapshot_artifact(snapshot),
        artifact(K.PROFILE_ELIGIBILITY_EVALUATION, gate.evaluation_id, gate),
    )
    profiles = tuple(
        scope.resolve(ref, ProfileDefinition) for ref in inputs.eligible_profiles
    )
    inputs.check_profiles(profiles)
    eligible = set(inputs.eligible_profiles)
    failures = set(failed_profiles)
    hits = eligible - failures if hit_profiles is None else set(hit_profiles)
    if (
        len(failures) != len(failed_profiles)
        or (hit_profiles is not None and len(hits) != len(hit_profiles))
        or not hits | failures <= eligible
        or hits & failures
    ):
        raise ValueError("fixture discovery cases must be disjoint and eligible")
    invocations: list[ModelInvocation] = []
    accepted_hits: list[ProfileHit] = []
    selections: list[AllDeclaredSelectionRecord] = []
    artifacts: list[SyntheticArtifact] = []
    for entry, profile in zip(inputs.bindings, profiles, strict=True):
        ref = entry.profile_ref
        model = scope.resolve(entry.discovery_binding_ref, ModelBinding)
        if model.prompt_template_ref != entry.prompt_template_ref:
            raise ValueError(
                "synthetic discovery must pin the configured prompt template"
            )
        invocation = scope.model_invocation(
            call,
            "N02",
            ModelCapability.PROFILE_DISCOVERY,
            entry.discovery_binding_ref,
            (ref,),
            record_digest(inputs),
            scope.response_digest(
                entry.discovery_binding_ref,
                {"profile_hits": [ref.artifact_id] if ref in hits else []},
            ),
            failure="synthetic-discovery-failure" if ref in failures else None,
        )
        invocations.append(invocation)
        inv_ref = ModelInvocationRef(
            record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
        )
        artifacts.append(artifact(K.MODEL_INVOCATION, inv_ref.record_id, invocation))
        if ref not in hits:
            continue
        policy = scope.resolve(
            entry.question_selection_policy_ref, QuestionSelectionPolicyArtifact
        ).policy
        configured = profile.question_refs
        mandatory = {
            reference_identity(item) for item in policy.mandatory_question_refs
        }
        optional = {reference_identity(item) for item in policy.optional_question_refs}
        if (
            policy.mode != "all_declared"
            or {reference_identity(item) for item in configured} != mandatory | optional
        ):
            raise ValueError(
                "synthetic discovery requires exact all-declared Question selection"
            )
        selection = AllDeclaredSelectionRecord(
            schema_version=VERSION,
            selection_id=call.id("questions", ref.stable_json()),
            execution_id=scope.execution_id,
            profile_ref=ref,
            policy_ref=PolicyContentRef.model_validate(
                entry.question_selection_policy_ref.model_dump()
            ),
            configured_question_refs=configured,
            mandatory_question_refs=tuple(
                item for item in configured if reference_identity(item) in mandatory
            ),
            proposed_optional_question_refs=(),
            selected_optional_question_refs=tuple(
                item for item in configured if reference_identity(item) in optional
            ),
            effective_question_refs=configured,
            excluded_question_reasons=(),
            context_snapshot_ref=inputs.context_snapshot_ref,
            context_snapshot_digest=inputs.context_snapshot_digest,
            node_run_id=inputs.envelope.node_run_id,
            attempt_id=inputs.envelope.attempt_id,
            discovery_model_invocation_ref=inv_ref,
            validation_issues=(),
            created_at=at,
            method="all_declared",
            fallback_applied=False,
        )
        selections.append(selection)
        artifacts.append(
            artifact(K.QUESTION_SELECTION_RECORD, selection.selection_id, selection)
        )
        accepted_hits.append(
            ProfileHit(
                profile_ref=ref,
                question_selection_ref=QuestionSelectionRef(
                    record_kind=K.QUESTION_SELECTION_RECORD,
                    record_id=selection.selection_id,
                ),
                effective_question_refs=configured,
                model_invocation_ref=inv_ref,
            )
        )
    failed = failures == eligible
    issue = (
        synthetic_issue(
            "synthetic-discovery-failure", "All synthetic discovery responses failed"
        )
        if failed
        else None
    )
    output = DiscoveryAttemptOutput(
        schema_version=VERSION,
        outcome="failed"
        if failed
        else "partial"
        if failures
        else "hits"
        if hits
        else "no_hits",
        execution_id=scope.execution_id,
        envelope=call.output(tuple(artifacts), issue),
        profile_hits=tuple(accepted_hits),
        diagnostics=(),
        model_invocation_refs=tuple(
            ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=item.model_invocation_id
            )
            for item in invocations
        ),
    )
    output.check_records(inputs, tuple(selections), tuple(invocations))
    return SyntheticDiscoveryResult(
        output, tuple(selections), tuple(invocations), tuple(artifacts)
    )


def original_coordinate_system(scope: VerifiedExecutionScope) -> CoordinateSystem:
    return CoordinateSystem(
        coordinate_system_id=scope.media.media_ref.media_id,
        frame="original_image",
        dimensions=scope.media.dimensions,
    )


def ground(
    scope: SyntheticExecution,
    inputs: GroundingInput,
    discovery: SyntheticDiscoveryResult,
    at: datetime,
    *,
    cases: tuple[SyntheticGroundingCase, ...] = (),
) -> SyntheticGroundingResult:
    inputs = GroundingInput.model_validate(inputs)
    call = scope.check_call(inputs.envelope, NodeId.N03, at)
    inputs.check_discovery(discovery.output)
    if (
        inputs.original_media_ref != scope.media.media_ref
        or inputs.original_coordinate_system != original_coordinate_system(scope)
    ):
        raise ValueError(
            "grounding must use the exact original image and coordinate frame"
        )
    if discovery.output.envelope.output_artifacts != tuple(
        item.pin for item in discovery.artifacts
    ) or discovery.output.model_invocation_refs != tuple(
        ModelInvocationRef(
            record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
        )
        for invocation in discovery.invocations
    ):
        raise ValueError("grounding requires exact discovery output artifacts")
    call.check_upstream(*discovery.artifacts)
    by_profile = {
        item.profile_ref: SyntheticGroundingCase.model_validate(item) for item in cases
    }
    if len(by_profile) != len(cases) or not by_profile.keys() <= {
        item.hit.profile_ref for item in inputs.profiles
    }:
        raise ValueError(
            "fixture grounding cases must identify one actual hit Profile each"
        )
    definitions = tuple(
        scope.resolve(item.hit.profile_ref, ProfileDefinition)
        for item in inputs.profiles
    )
    inputs.check_profiles(definitions)
    records: list[GroundingRecord] = []
    objects: list[ObjectRecord] = []
    invocations: list[ModelInvocation] = []
    for item, profile in zip(inputs.profiles, definitions, strict=True):
        ref = item.hit.profile_ref
        case = by_profile.get(
            ref, SyntheticGroundingCase(profile_ref=ref, object_count=1)
        )
        binding = item.grounding_binding
        if not isinstance(binding, ModelGroundingBinding):
            raise ValueError(
                "parking fixture requires independently bound model grounding"
            )
        frame = inputs.original_coordinate_system
        width, height = frame.dimensions.width, frame.dimensions.height
        boxes = []
        for index in range(case.object_count):
            x1 = width * index // case.object_count
            y1 = height // 4
            x2 = max(x1 + 1, width * (index + 1) // case.object_count)
            y2 = max(y1 + 1, height * 3 // 4)
            boxes.append(BBoxXYXY(xyxy=(float(x1), float(y1), float(x2), float(y2))))
        invocation = scope.model_invocation(
            call,
            "N03",
            ModelCapability.OBJECT_GROUNDING,
            binding.model_binding_ref,
            (ref,),
            record_digest(inputs),
            scope.response_digest(
                binding.model_binding_ref,
                {
                    "boxes": [
                        dict(
                            zip(
                                ("x1", "y1", "x2", "y2"),
                                (int(value) for value in box.xyxy),
                                strict=True,
                            )
                        )
                        for box in boxes
                    ]
                },
            ),
            failure="synthetic-grounding-failure"
            if case.disposition == "failed"
            else None,
        )
        invocations.append(invocation)
        producer = ModelGroundingProvenance(
            kind="model",
            model_binding_ref=binding.model_binding_ref,
            model_invocation_ref=ModelInvocationRef(
                record_kind=K.MODEL_INVOCATION, record_id=invocation.model_invocation_id
            ),
        )
        object_refs = tuple(
            ObjectRef(
                record_kind=K.OBJECT,
                record_id=call.id("object", ref.stable_json(), str(index)),
            )
            for index in range(case.object_count)
        )
        grounding_id = call.id("grounding", ref.stable_json())
        grounding_ref = GroundingRecordRef(
            record_kind=K.GROUNDING_RECORD, record_id=grounding_id
        )
        outcome = (
            GroundingOutcome.FAILED
            if case.disposition == "failed"
            else GroundingOutcome.PARTIAL
            if case.disposition == "partial"
            else GroundingOutcome.OBJECTS
            if boxes
            else GroundingOutcome.NO_TARGET
        )
        selection = {
            "profile_ref": ref,
            "question_selection_ref": item.hit.question_selection_ref,
            "effective_question_refs": item.hit.effective_question_refs,
        }
        records.append(
            GroundingRecord.model_validate(
                {
                    **selection,
                    "schema_version": VERSION,
                    "grounding_record_id": grounding_id,
                    "execution_id": scope.execution_id,
                    "node_run_id": inputs.envelope.node_run_id,
                    "attempt_id": inputs.envelope.attempt_id,
                    "grounding_binding": binding,
                    "source_media_ref": inputs.original_media_ref,
                    "coordinate_system": frame,
                    "transforms": (),
                    "outcome": outcome,
                    "object_refs": object_refs,
                    "evidence_refs": (),
                    "producer": producer,
                    "validation_issues": (
                        (
                            synthetic_issue(
                                "synthetic-grounding-incomplete",
                                "Synthetic grounding failed or returned partial coverage",
                            ),
                        )
                        if case.disposition != "complete"
                        else ()
                    ),
                }
            )
        )
        objects.extend(
            ObjectRecord.model_validate(
                {
                    **selection,
                    "schema_version": VERSION,
                    "object_id": object_ref.record_id,
                    "execution_id": scope.execution_id,
                    "entity_ref": profile.entity_ref,
                    "grounding_record_ref": grounding_ref,
                    "source_media_ref": inputs.original_media_ref,
                    "coordinate_system": frame,
                    "bbox": box,
                    "grounding_producer": producer,
                    "visibility": "partially_visible"
                    if case.disposition == "partial"
                    else "visible",
                    "localization_status": "partial"
                    if case.disposition == "partial"
                    else "localized",
                    "evidence_refs": (),
                }
            )
            for object_ref, box in zip(object_refs, boxes, strict=True)
        )
    artifacts = (
        *(
            artifact(K.GROUNDING_RECORD, item.grounding_record_id, item)
            for item in records
        ),
        *(artifact(K.OBJECT, item.object_id, item) for item in objects),
        *(
            artifact(K.MODEL_INVOCATION, item.model_invocation_id, item)
            for item in invocations
        ),
    )
    failure = (
        synthetic_issue(
            "synthetic-grounding-failure", "All fixture grounding producers failed"
        )
        if all(item.outcome is GroundingOutcome.FAILED for item in records)
        else None
    )
    output = GroundingOutput(
        schema_version=VERSION,
        envelope=call.output(artifacts, failure),
        profile_results=tuple(item.as_profile_result() for item in records),
    )
    output.check_records(
        inputs,
        tuple(records),
        tuple(objects),
        discovery.selections,
        tuple(invocations),
        definitions,
    )
    return SyntheticGroundingResult(
        output, tuple(records), tuple(objects), tuple(invocations), artifacts
    )
