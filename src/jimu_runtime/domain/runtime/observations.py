"""One Object observation batch with independently checked primary Facts."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    immutable_sequence,
    omit_default_from_schema,
)
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef, QuestionContentRef
from jimu_runtime.domain.primitives import ArtifactId, RecordId
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    ErrorFact,
    FactRecord,
    FactRef,
    ModelInvocationRef,
    NodeRunRef,
    ObjectRef,
    ObservationBatchRef,
    QuestionFactSource,
)
from jimu_runtime.domain.runtime.perception import (
    EffectiveQuestionSelection,
    PerceptionValidationIssues,
)
from jimu_runtime.domain.validation.authority_inputs import (
    ObservationResultDescriptor,
    QuestionPrimaryCandidate,
)


class ValidQuestionResult(ContractModel):
    status: Literal["validated"]


class InvalidQuestionResult(ContractModel):
    status: Literal["invalid"]
    issues: Annotated[PerceptionValidationIssues, Field(min_length=1)]


type QuestionValidationResult = Annotated[
    ValidQuestionResult | InvalidQuestionResult, Field(discriminator="status")
]


class QuestionResult(ContractModel):
    question_ref: QuestionContentRef
    primary_fact_key: ArtifactId
    primary_fact_schema_ref: FactSchemaContentRef
    primary_fact: FactRecord
    validation: QuestionValidationResult

    @model_validator(mode="after")
    def check_primary_fact(self) -> Self:
        fact = self.primary_fact
        if not isinstance(fact.source, QuestionFactSource) or (
            fact.source.question_ref,
            fact.fact_key,
            fact.fact_schema_ref,
        ) != (self.question_ref, self.primary_fact_key, self.primary_fact_schema_ref):
            raise ValueError("Question result must contain its exact primary Fact")
        if isinstance(self.validation, InvalidQuestionResult) != isinstance(
            fact, ErrorFact
        ):
            raise ValueError("invalid Question output must retain an error Fact")
        return self


class ObservationBatch(EffectiveQuestionSelection):
    schema_version: Literal["1.0.0"]
    observation_batch_id: RecordId
    execution_id: RecordId
    object_ref: ObjectRef
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef
    model_invocation_ref: ModelInvocationRef
    outcome: Literal[
        "complete", "partial", "transport_error", "parse_error", "association_error"
    ]
    envelope_validation: Literal["trusted", "untrusted", "unresolved"]
    association_validation: Literal["trusted", "untrusted", "unresolved"]
    arrival_qualification: Literal["on_time", "late", "unresolved"]
    question_results: Annotated[
        tuple[QuestionResult, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    validation_issues: PerceptionValidationIssues
    retry_of_batch_ref: AbsentOptional[ObservationBatchRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @property
    def batch_ref(self) -> ObservationBatchRef:
        return ObservationBatchRef(
            record_kind=RuntimeRecordKind.OBSERVATION_BATCH,
            record_id=self.observation_batch_id,
        )

    @model_validator(mode="after")
    def check_results(self) -> Self:
        if not self.effective_question_refs:
            raise ValueError(
                "an attempted observation batch requires effective Questions"
            )
        if self.retry_of_batch_ref == self.batch_ref:
            raise ValueError("a batch retry must reference a distinct batch")
        trustworthy = self.outcome in {"complete", "partial"}
        qualifications = (self.envelope_validation, self.association_validation)
        expected = {
            "complete": ("trusted", "trusted"),
            "partial": ("trusted", "trusted"),
            "transport_error": ("unresolved", "unresolved"),
            "parse_error": ("untrusted", "unresolved"),
            "association_error": ("trusted", "untrusted"),
        }[self.outcome]
        if qualifications != expected:
            raise ValueError(
                "batch outcome must match envelope and association validation"
            )
        if not trustworthy:
            if self.question_results or not self.validation_issues:
                raise ValueError(
                    "failed batches require diagnostics and cannot contain fabricated Facts"
                )
            return self
        if (
            tuple(item.question_ref for item in self.question_results)
            != self.effective_question_refs
        ):
            raise ValueError(
                "trusted batches require exactly one result per effective Question in order"
            )
        if (self.outcome == "partial") != any(
            isinstance(item.validation, InvalidQuestionResult)
            for item in self.question_results
        ):
            raise ValueError(
                "partial batch must identify independently invalid Question results"
            )
        seen_facts = set()
        for item in self.question_results:
            fact = item.primary_fact
            source = fact.source
            if not isinstance(source, QuestionFactSource) or (
                fact.execution_id,
                fact.profile_ref,
                fact.object_id,
                fact.attempt_id,
                source.observation_batch_ref,
                source.model_invocation_ref,
            ) != (
                self.execution_id,
                self.profile_ref,
                self.object_ref.record_id,
                self.attempt_ref.record_id,
                self.batch_ref,
                self.model_invocation_ref,
            ):
                raise ValueError(
                    "primary Fact must belong to this Object, invocation and batch Attempt"
                )
            if fact.fact_id in seen_facts:
                raise ValueError("primary Fact cannot be shared between Questions")
            seen_facts.add(fact.fact_id)
        return self

    def to_authority_descriptor(self) -> ObservationResultDescriptor:
        """Project existing batch and Fact identities without choosing authority."""
        return ObservationResultDescriptor(
            scope=FactScope.OBJECT,
            execution_id=self.execution_id,
            profile_ref=self.profile_ref,
            object_id=self.object_ref.record_id,
            attempt_ref=self.attempt_ref,
            arrival_qualification=self.arrival_qualification,
            result_ref=self.batch_ref,
            question_selection_ref=self.question_selection_ref,
            effective_question_refs=self.effective_question_refs,
            model_invocation_ref=self.model_invocation_ref,
            envelope_validation=self.envelope_validation,
            association_validation=self.association_validation,
            question_results=tuple(
                QuestionPrimaryCandidate(
                    question_ref=item.question_ref,
                    fact_ref=FactRef(
                        record_kind=RuntimeRecordKind.FACT,
                        record_id=item.primary_fact.fact_id,
                    ),
                )
                for item in self.question_results
            ),
        )
