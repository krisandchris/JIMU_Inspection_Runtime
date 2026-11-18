"""Finite immutable association evidence shapes, not producer/authority admission."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    PositiveBudget,
    immutable_sequence,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import QuestionDefinition
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import AttemptStatus, FactStatus, NodeRunStatus
from jimu_runtime.domain.fact_selectors import (
    QuestionContentRef,
    SystemProducerRequirement,
    ToolContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.primitives import ArtifactId, RecordId, Version
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    ExecutionEventRef,
    FactReason,
    FactRecord,
    FactRef,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    ObservationBatchRef,
    ProfileContentRef,
    QuestionSelectionRef,
    ScopedRecord,
    ToolExecutionRef,
)


class ObjectDescriptor(ContractModel):
    object_ref: ObjectRef
    execution_id: RecordId
    profile_ref: ProfileContentRef


class NodeRunDescriptor(ContractModel):
    node_run_ref: NodeRunRef
    execution_id: RecordId
    node_id: Literal["N01", "N02", "N03", "N04", "N05", "N06"]
    status: NodeRunStatus


class NodeAttemptOwner(ContractModel):
    owner_kind: Literal["node"]
    node_run_ref: NodeRunRef


class SystemAttemptOwner(ContractModel):
    owner_kind: Literal["system"]
    requirement: SystemProducerRequirement


type AttemptOwner = Annotated[
    NodeAttemptOwner | SystemAttemptOwner, Field(discriminator="owner_kind")
]


class AttemptDescriptor(ContractModel):
    attempt_ref: AttemptRef
    execution_id: RecordId
    owner: AttemptOwner
    attempt_number: PositiveBudget
    status: AttemptStatus


type QuestionRefs = Annotated[
    tuple[QuestionContentRef, ...],
    Field(min_length=1, max_length=1024),
    BeforeValidator(immutable_sequence),
]


class QuestionSelectionDescriptor(ContractModel):
    question_selection_ref: QuestionSelectionRef
    execution_id: RecordId
    profile_ref: ProfileContentRef
    eligibility_qualification: Literal["validated", "unresolved", "rejected"]
    effective_question_refs: QuestionRefs

    @model_validator(mode="after")
    def unique_questions(self) -> Self:
        require_unique_references(self.effective_question_refs)
        return self


class QuestionPrimaryCandidate(ContractModel):
    question_ref: QuestionContentRef
    fact_ref: FactRef


class EmittedOutput(ContractModel):
    disposition: Literal["emitted"]
    output_name: ArtifactId
    fact_status: FactStatus
    fact_ref: FactRef


class OptionalAbsentOutput(ContractModel):
    disposition: Literal["optional_absence"]
    output_name: ArtifactId
    reason: FactReason


type MappedOutputResult = Annotated[
    EmittedOutput | OptionalAbsentOutput, Field(discriminator="disposition")
]


class _ResultBase(ScopedRecord):
    attempt_ref: AttemptRef
    arrival_qualification: Literal["on_time", "late", "unresolved"]


class ObservationResultDescriptor(_ResultBase):
    scope: Literal[FactScope.OBJECT]
    result_ref: ObservationBatchRef
    question_selection_ref: QuestionSelectionRef
    effective_question_refs: QuestionRefs
    model_invocation_ref: ModelInvocationRef
    envelope_validation: Literal["trusted", "untrusted", "unresolved"]
    association_validation: Literal["trusted", "untrusted", "unresolved"]
    question_results: Annotated[
        tuple[QuestionPrimaryCandidate, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_questions_and_candidates(self) -> Self:
        require_unique_references(self.effective_question_refs)
        questions = [
            (
                item.question_ref.artifact_kind,
                item.question_ref.artifact_id,
                item.question_ref.version,
            )
            for item in self.question_results
        ]
        facts = [
            (item.fact_ref.record_kind, item.fact_ref.record_id)
            for item in self.question_results
        ]
        if len(set(questions)) != len(questions) or len(set(facts)) != len(facts):
            raise ValueError(
                "question results require unique question identities and fact refs independently"
            )
        return self


class ToolStepResultDescriptor(_ResultBase):
    scope: Literal[FactScope.OBJECT]
    result_ref: ToolExecutionRef
    invocation_origin: Literal["tool_plan_step"]
    tool_plan_ref: ToolPlanContentRef
    step_id: ArtifactId
    tool_ref: ToolContentRef
    actual_implementation_version: Version
    envelope_validation: Literal["trusted", "untrusted", "unresolved"]
    association_validation: Literal["trusted", "untrusted", "unresolved"]
    mapped_outputs: Annotated[
        tuple[MappedOutputResult, ...],
        Field(max_length=64),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def unique_output_names(self) -> Self:
        names = [output.output_name for output in self.mapped_outputs]
        if len(set(names)) != len(names):
            raise ValueError("mapped output names must be unique")
        return self


class SystemOutputResultDescriptor(_ResultBase):
    result_ref: ExecutionEventRef
    requirement: SystemProducerRequirement
    output_name: ArtifactId
    actual_implementation_version: Version
    output: MappedOutputResult

    @model_validator(mode="after")
    def output_and_implementation_identity(self) -> Self:
        if self.output.output_name != self.output_name:
            raise ValueError("system result and output names must agree")
        if (
            self.actual_implementation_version
            not in self.requirement.allowed_implementation_versions
        ):
            raise ValueError(
                "actual system implementation version must be explicitly allowed"
            )
        return self


class FactCandidateSubmission(ContractModel):
    """Data only; raw byte/content equality requires the separately authorized parser."""

    fact: FactRecord
    raw_bytes: bytes


class AuthorityQuestionInput(ContractModel):
    ref: QuestionContentRef
    definition: QuestionDefinition

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (self.ref.artifact_id, self.ref.version) != (
            self.definition.question_id,
            self.definition.version,
        ):
            raise ValueError("Question registration identity mismatch")
        return self


class AuthorityToolPlanInput(ContractModel):
    ref: ToolPlanContentRef
    definition: ToolPlanDefinition

    @model_validator(mode="after")
    def identity_matches(self) -> Self:
        if (self.ref.artifact_id, self.ref.version) != (
            self.definition.tool_plan_id,
            self.definition.version,
        ):
            raise ValueError("ToolPlan registration identity mismatch")
        return self
