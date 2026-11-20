"""N04 describes one joint request over the Object's frozen Questions."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    ContextFieldSelector,
    Description,
    OptionalContract,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.prompts import PromptTemplateContentRef
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef, QuestionContentRef
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    ObservationBatchRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.intake import ContextValue
from jimu_runtime.domain.runtime.invocations import (
    FailedInvocation,
    InvocationContractSnapshot,
    ModelInvocation,
)
from jimu_runtime.domain.runtime.media import BBoxXYXY, MediaRef
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import (
    ObjectRecord,
    TransformRecords,
    check_transforms,
)
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord

N04_REGIONS_MARKER = "JIMU_N04_REGIONS_V1"


def is_region_observation_template(text: str) -> bool:
    return text.partition("\n")[0] == N04_REGIONS_MARKER


class ObjectImageView(OptionalContract):
    roi: BBoxXYXY
    transforms: TransformRecords
    crop_evidence_ref: AbsentOptional[EvidenceRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    marked_media_ref: AbsentOptional[MediaRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @classmethod
    def for_object(cls, obj: ObjectRecord) -> Self:
        return cls(roi=obj.bbox, transforms=())

    def check_object(
        self, object_record: ObjectRecord, *, original_regions: bool = False
    ) -> None:
        frame = object_record.marked_frame
        # Historical inputs can still pin the archived marked frame.
        if self.marked_media_ref is not None and (
            original_regions
            or frame is None
            or self.marked_media_ref != frame.media_ref
        ):
            raise ValueError("observation image must match the exact Object frame")
        if (frame is not None or original_regions) and (
            self.roi != object_record.bbox or self.transforms
        ):
            raise ValueError(
                "full-frame observation uses the original grid and Object ROI"
            )
        self.roi.validate_bounds(object_record.coordinate_system.dimensions)
        check_transforms(object_record.coordinate_system, self.transforms)
        if (self.crop_evidence_ref is not None) != bool(self.transforms):
            raise ValueError(
                "derived crop evidence and transform provenance must travel together"
            )


class AllowedContextValue(ContractModel):
    selector: ContextFieldSelector
    value: ContextValue

    @model_validator(mode="after")
    def check_path(self) -> Self:
        if self.selector.field_path != self.value.field_path:
            raise ValueError("context value must match its exact selected path")
        return self


class JointQuestionInput(ContractModel):
    question_ref: QuestionContentRef
    prompt_template_ref: PromptTemplateContentRef
    primary_fact_key: ArtifactId
    primary_fact_schema_ref: FactSchemaContentRef
    context_view: Annotated[
        tuple[AllowedContextValue, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_context_view(self) -> Self:
        selectors = [entry.selector.stable_json() for entry in self.context_view]
        if len(set(selectors)) != len(selectors):
            raise ValueError("a Question context view cannot duplicate selectors")
        return self

    def check_definition(
        self, question_ref: QuestionContentRef, question: QuestionDefinition
    ) -> None:
        if self.question_ref != question_ref or (
            question.question_id,
            question.version,
            question.prompt_template_ref,
            question.primary_fact_key,
            question.primary_fact_schema_ref,
        ) != (
            self.question_ref.artifact_id,
            self.question_ref.version,
            self.prompt_template_ref,
            self.primary_fact_key,
            self.primary_fact_schema_ref,
        ):
            raise ValueError(
                "joint request must retain the exact Question prompt and primary Fact contract"
            )
        if not all(
            entry.selector in question.context_allowlist for entry in self.context_view
        ):
            raise ValueError(
                "Question context view may only include its declared allowlist"
            )


class ObservationRetry(ContractModel):
    previous_batch_ref: ObservationBatchRef
    reason: Description


class ObservationInput(OptionalContract):
    schema_version: Literal["1.0.0"]
    envelope: NodeInputEnvelope
    object_record: ObjectRecord
    context_snapshot_ref: ContextSnapshotRef
    image_view: ObjectImageView
    image_mode: AbsentOptional[Literal["original_regions_v1"]] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    observation_binding_ref: ModelBindingContentRef
    compiled_prompt_evidence_ref: EvidenceRef
    contract_snapshot: InvocationContractSnapshot
    joint_questions: Annotated[
        tuple[JointQuestionInput, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    retry: AbsentOptional[ObservationRetry] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def check_joint_request(self) -> Self:
        if (
            self.envelope.node_id is not NodeId.N04
            or self.object_record.execution_id != self.envelope.execution_id
        ):
            raise ValueError(
                "joint observation must run for an Object in this Execution"
            )
        if (
            tuple(entry.question_ref for entry in self.joint_questions)
            != self.object_record.effective_question_refs
            or self.contract_snapshot.effective_question_refs
            != self.object_record.effective_question_refs
        ):
            raise ValueError(
                "joint request must exactly inherit the Object's effective Question set"
            )
        self.image_view.check_object(
            self.object_record,
            original_regions=self.image_mode == "original_regions_v1",
        )
        return self

    def check_configuration(
        self,
        profile_ref: ProfileContentRef,
        profile: ProfileDefinition,
        selection: QuestionSelectionRecord,
        questions: tuple[tuple[QuestionContentRef, QuestionDefinition], ...],
    ) -> None:
        obj = self.object_record
        obj.check_selection(self.envelope.execution_id, selection)
        if profile_ref != obj.profile_ref or (
            profile.profile_id,
            profile.version,
            profile.observation_binding_ref,
        ) != (
            obj.profile_ref.artifact_id,
            obj.profile_ref.version,
            self.observation_binding_ref,
        ):
            raise ValueError("observation must use the pinned Profile binding")
        if (
            selection.context_snapshot_ref.record_id
            != self.context_snapshot_ref.record_id
        ):
            raise ValueError(
                "observation context must use the pinned Question-selection Snapshot"
            )
        if len(questions) != len(self.joint_questions):
            raise ValueError("resolve one exact definition per effective Question")
        for entry, (question_ref, question) in zip(
            self.joint_questions, questions, strict=True
        ):
            entry.check_definition(question_ref, question)

    def check_batch(
        self,
        batch: ObservationBatch,
        invocations: tuple[ModelInvocation, ...],
        previous_batch: ObservationBatch | None,
    ) -> None:
        if len(invocations) != 1:
            raise ValueError(
                "one joint model invocation is required per Object observation Attempt"
            )
        (invocation,) = invocations
        env, obj = self.envelope, self.object_record
        expected = (env.execution_id, env.node_run_id, env.attempt_id, obj.object_id)
        if (
            batch.execution_id,
            batch.node_run_ref.record_id,
            batch.attempt_ref.record_id,
            batch.object_ref.record_id,
        ) != expected or (
            invocation.execution_id,
            invocation.node_run_ref.record_id,
            invocation.attempt_ref.record_id,
            invocation.object_ref.record_id
            if invocation.object_ref is not None
            else None,
        ) != expected:
            raise ValueError(
                "batch and invocation must belong to this N04 Object Attempt"
            )
        if (
            invocation.node_id != NodeId.N04.value
            or invocation.purposes != (ModelCapability.OBJECT_OBSERVATION,)
            or invocation.profile_refs != (obj.profile_ref,)
            or invocation.model_invocation_id != batch.model_invocation_ref.record_id
            or reference_identity(invocation.configured_provider.binding_ref)
            != reference_identity(self.observation_binding_ref)
            or invocation.contract_snapshot != self.contract_snapshot
            or self.compiled_prompt_evidence_ref not in invocation.evidence_refs
            or invocation.trace_id != env.trace.trace_id
        ):
            raise ValueError(
                "observation must retain the actual joint invocation and its exact input contract"
            )
        invocation_status = invocation.outcome
        validation_class = (
            invocation_status.error.error_class
            if isinstance(invocation_status, FailedInvocation)
            and invocation_status.error.error_class in {"validation", "association"}
            else None
        )
        if invocation_status.status == "succeeded":
            outcomes = {"complete", "partial", "parse_error", "association_error"}
        elif validation_class == "validation":
            outcomes = {"parse_error"}
        elif validation_class == "association":
            outcomes = {"association_error"}
        else:
            outcomes = {"transport_error"}
        if batch.outcome not in outcomes:
            raise ValueError(
                "observation batch disposition must match the actual invocation outcome"
            )
        if (
            batch.profile_ref,
            batch.question_selection_ref,
            batch.effective_question_refs,
        ) != (
            obj.profile_ref,
            obj.question_selection_ref,
            obj.effective_question_refs,
        ):
            raise ValueError(
                "observation batch must inherit the Object's exact Question selection"
            )
        definitions = {
            entry.question_ref.stable_json(): entry for entry in self.joint_questions
        }
        for result in batch.question_results:
            entry = definitions[result.question_ref.stable_json()]
            if (result.primary_fact_key, result.primary_fact_schema_ref) != (
                entry.primary_fact_key,
                entry.primary_fact_schema_ref,
            ):
                raise ValueError(
                    "observation Fact must retain its Question's declared key and Schema"
                )
        self._check_retry(batch, previous_batch)

    def _check_retry(
        self, batch: ObservationBatch, previous_batch: ObservationBatch | None
    ) -> None:
        if self.retry is None:
            if previous_batch is not None or batch.retry_of_batch_ref is not None:
                raise ValueError("a retry batch requires an explicit retry input")
            return
        if (
            previous_batch is None
            or self.retry.previous_batch_ref != previous_batch.batch_ref
            or previous_batch.outcome in {"complete", "partial"}
            or (
                batch.retry_of_batch_ref != previous_batch.batch_ref
                or batch.attempt_ref == previous_batch.attempt_ref
                or batch.model_invocation_ref == previous_batch.model_invocation_ref
                or batch.batch_ref == previous_batch.batch_ref
                or (
                    batch.execution_id,
                    batch.object_ref,
                    batch.profile_ref,
                    batch.question_selection_ref,
                    batch.effective_question_refs,
                )
                != (
                    previous_batch.execution_id,
                    previous_batch.object_ref,
                    previous_batch.profile_ref,
                    previous_batch.question_selection_ref,
                    previous_batch.effective_question_refs,
                )
            )
        ):
            raise ValueError(
                "retry evidence requires a distinct whole-batch Attempt for identical Question scope"
            )
