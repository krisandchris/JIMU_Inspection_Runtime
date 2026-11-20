"""Grounding evidence and original-frame Objects."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    PositiveBudget,
    immutable_sequence,
    omit_default_from_schema,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import (
    EntityContentRef,
    GroundingBinding,
    ModelBindingContentRef,
    ModelGroundingBinding,
)
from jimu_runtime.domain.enums import RuntimeRecordKind, WireEnum
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.primitives import RecordId, Version
from jimu_runtime.domain.runtime.facts import (
    EvidenceRefs,
    ModelInvocationRef,
    ObjectRef,
    ProfileContentRef,
    QuestionSelectionRef,
    ToolExecutionRef,
    TypedRuntimeRef,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.runtime.media import (
    BBoxXYXY,
    CoordinateSystem,
    MediaRef,
    TransformRecord,
)
from jimu_runtime.domain.runtime.selection import (
    FallbackSelectionRecord,
    QuestionSelectionRecord,
    QuestionSelectionRefs,
)
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.values import FiniteNumber


class DiscoveryOutcome(WireEnum):
    HITS = "hits"
    NO_HITS = "no_hits"
    FAILED = "failed"
    PARTIAL = "partial"
    NOT_EVALUATED = "not_evaluated"


class GroundingOutcome(WireEnum):
    OBJECTS = "objects"
    NO_TARGET = "no_target"
    FAILED = "failed"
    PARTIAL = "partial"
    NOT_EVALUATED = "not_evaluated"


class GroundingRecordRef(TypedRuntimeRef):
    record_kind: Literal[RuntimeRecordKind.GROUNDING_RECORD]


type ObjectRefs = Annotated[
    tuple[ObjectRef, ...],
    Field(max_length=1024, json_schema_extra={"uniqueItems": True}),
    BeforeValidator(immutable_sequence),
]
type TransformRecords = Annotated[
    tuple[TransformRecord, ...],
    Field(max_length=64),
    BeforeValidator(immutable_sequence),
]
type PerceptionValidationIssues = Annotated[
    tuple[ValidationIssue, ...],
    Field(max_length=1024),
    BeforeValidator(immutable_sequence),
]


class EffectiveQuestionSelection(OptionalContract):
    profile_ref: ProfileContentRef
    question_selection_ref: QuestionSelectionRef
    effective_question_refs: QuestionSelectionRefs

    @model_validator(mode="after")
    def check_exact_questions(self) -> Self:
        require_unique_references(self.effective_question_refs)
        return self

    def check_selection(
        self, execution_id: RecordId, selection: QuestionSelectionRecord
    ) -> None:
        if (
            execution_id,
            self.profile_ref,
            self.question_selection_ref.record_id,
            self.effective_question_refs,
        ) != (
            selection.execution_id,
            selection.profile_ref,
            selection.selection_id,
            selection.effective_question_refs,
        ):
            raise ValueError(
                "Question selection must match the Profile and pinned effective Questions"
            )
        if (
            isinstance(selection, FallbackSelectionRecord)
            and selection.fallback_action == "block_profile"
        ):
            raise ValueError(
                "blocked Question selection cannot enter grounding or observation"
            )


class ModelGroundingProvenance(ContractModel):
    kind: Literal["model"]
    model_binding_ref: ModelBindingContentRef
    model_invocation_ref: ModelInvocationRef


class ToolGroundingProvenance(OptionalContract):
    kind: Literal["tool"]
    tool_ref: ToolContentRef
    tool_execution_ref: ToolExecutionRef
    actual_implementation_version: AbsentOptional[Version] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    nested_model_invocation_refs: Annotated[
        tuple[ModelInvocationRef, ...],
        Field(max_length=64, json_schema_extra={"uniqueItems": True}),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_nested_refs(self) -> Self:
        require_unique_runtime_refs(self.nested_model_invocation_refs)
        return self


type GroundingProvenance = Annotated[
    ModelGroundingProvenance | ToolGroundingProvenance,
    Field(discriminator="kind"),
]


class MarkedGroundingFrame(ContractModel):
    """Full-size RGB PNG annotated by the versioned red outline renderer."""

    media_ref: MediaRef
    byte_size: PositiveBudget
    mime_type: Literal["image/png"]
    renderer_version: Literal["1.0.0"]


type GroundingMemberBoxes = Annotated[
    tuple[BBoxXYXY, ...],
    Field(min_length=1, max_length=1024),
    BeforeValidator(immutable_sequence),
]


class ObjectRecord(EffectiveQuestionSelection):
    schema_version: Literal["1.0.0"]
    object_id: RecordId
    execution_id: RecordId
    entity_ref: EntityContentRef
    grounding_record_ref: GroundingRecordRef
    source_media_ref: MediaRef
    coordinate_system: CoordinateSystem
    bbox: BBoxXYXY
    grounding_member_boxes: AbsentOptional[GroundingMemberBoxes] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    # Preserve serialized records and digests written before geometry-only grounding.
    marked_frame: AbsentOptional[MarkedGroundingFrame] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    grounding_producer: GroundingProvenance
    visibility: Literal["visible", "partially_visible", "unknown"]
    localization_status: Literal["localized", "partial"]
    confidence: AbsentOptional[FiniteNumber] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )
    evidence_refs: EvidenceRefs

    @model_validator(mode="after")
    def validate_original_frame(self) -> Self:
        if self.coordinate_system.frame != "original_image":
            raise ValueError("Object box requires the original image frame")
        self.bbox.validate_bounds(self.coordinate_system.dimensions)
        if self.grounding_member_boxes is None:
            if self.marked_frame is not None:
                raise ValueError("a marked frame requires grouped grounding members")
        elif (
            BBoxXYXY.enclosing(self.grounding_member_boxes) != self.bbox
            or tuple(sorted(self.grounding_member_boxes, key=lambda box: box.xyxy))
            != self.grounding_member_boxes
        ):
            raise ValueError(
                "grouped grounding requires canonical members and their enclosing box"
            )
        if self.marked_frame is not None and self.marked_frame.media_ref.media_id in {
            self.source_media_ref.media_id,
            self.object_id,
        }:
            raise ValueError(
                "a historical marked frame requires a distinct media identity"
            )
        require_unique_runtime_refs(self.evidence_refs)
        return self


class ProfileGroundingResult(EffectiveQuestionSelection):
    outcome: GroundingOutcome
    grounding_record_ref: GroundingRecordRef
    object_refs: ObjectRefs
    validation_issues: PerceptionValidationIssues

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome is GroundingOutcome.NOT_EVALUATED:
            raise ValueError("grounding result requires an actual grounding Attempt")
        require_unique_runtime_refs(self.object_refs)
        if (
            self.outcome is GroundingOutcome.OBJECTS
            and not self.object_refs
            or self.outcome in {GroundingOutcome.FAILED, GroundingOutcome.NO_TARGET}
            and self.object_refs
        ):
            raise ValueError("Object references must agree with the grounding outcome")
        if (
            self.outcome in {GroundingOutcome.FAILED, GroundingOutcome.PARTIAL}
            and not self.validation_issues
        ):
            raise ValueError("failed or partial grounding requires diagnostics")
        return self


class GroundingRecord(EffectiveQuestionSelection):
    schema_version: Literal["1.0.0"]
    grounding_record_id: RecordId
    execution_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    grounding_binding: GroundingBinding
    source_media_ref: MediaRef
    coordinate_system: CoordinateSystem
    transforms: TransformRecords
    outcome: GroundingOutcome
    object_refs: ObjectRefs
    evidence_refs: EvidenceRefs
    producer: GroundingProvenance
    validation_issues: PerceptionValidationIssues

    @property
    def grounding_ref(self) -> GroundingRecordRef:
        return GroundingRecordRef(
            record_kind=RuntimeRecordKind.GROUNDING_RECORD,
            record_id=self.grounding_record_id,
        )

    @model_validator(mode="after")
    def validate_grounding(self) -> Self:
        binding, producer = self.grounding_binding, self.producer
        if isinstance(binding, ModelGroundingBinding):
            if (
                not isinstance(producer, ModelGroundingProvenance)
                or producer.model_binding_ref != binding.model_binding_ref
            ):
                raise ValueError(
                    "model grounding requires its exact ModelBinding provenance"
                )
        elif (
            not isinstance(producer, ToolGroundingProvenance)
            or producer.tool_ref != binding.tool_ref
        ):
            raise ValueError("tool grounding requires its exact Tool provenance")
        if (
            isinstance(producer, ToolGroundingProvenance)
            and self.outcome is not GroundingOutcome.FAILED
            and producer.actual_implementation_version is None
        ):
            raise ValueError(
                "a usable Tool grounding result requires an actual implementation version"
            )
        require_unique_runtime_refs(self.evidence_refs)
        self.as_profile_result()
        check_transforms(self.coordinate_system, self.transforms)
        return self

    def as_profile_result(self) -> ProfileGroundingResult:
        return ProfileGroundingResult(
            profile_ref=self.profile_ref,
            question_selection_ref=self.question_selection_ref,
            effective_question_refs=self.effective_question_refs,
            outcome=self.outcome,
            grounding_record_ref=self.grounding_ref,
            object_refs=self.object_refs,
            validation_issues=self.validation_issues,
        )

    def check_objects(self, objects: tuple[ObjectRecord, ...]) -> None:
        if tuple(item.object_id for item in objects) != tuple(
            ref.record_id for ref in self.object_refs
        ):
            raise ValueError(
                "Objects must exhaust the Profile grounding result in recorded order"
            )
        for item in objects:
            if (
                item.execution_id != self.execution_id
                or item.profile_ref != self.profile_ref
                or item.question_selection_ref != self.question_selection_ref
                or item.effective_question_refs != self.effective_question_refs
                or item.grounding_record_ref != self.grounding_ref
                or item.grounding_producer != self.producer
                or item.source_media_ref != self.source_media_ref
                or item.coordinate_system != self.coordinate_system
            ):
                raise ValueError(
                    "Object must retain its exact Profile grounding provenance"
                )


def check_transforms(original: CoordinateSystem, transforms: TransformRecords) -> None:
    if original.frame != "original_image":
        raise ValueError("image provenance requires an original frame")
    known_frames = {original.coordinate_system_id: original}
    for transform in transforms:
        target_id = transform.target.coordinate_system_id
        if (
            known_frames.get(transform.source.coordinate_system_id) != transform.source
            or target_id in known_frames
        ):
            raise ValueError(
                "transform must start in a known frame and introduce a new frame"
            )
        known_frames[target_id] = transform.target
