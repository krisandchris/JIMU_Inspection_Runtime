"""Typed PostgreSQL payloads for the offline workflow's validated node artifacts."""

from base64 import b64decode, b64encode
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.nodes.context import ContextBuildInput, ContextBuildOutput
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    _DiscoveryGroupFailure,
)
from jimu_runtime.domain.nodes.envelopes import (
    FailedNodeOutput,
    NodeInputEnvelope,
    NodeOutputEnvelope,
    SucceededNodeOutput,
    check_envelope_pair,
)
from jimu_runtime.domain.nodes.grounding import (
    GroundingInput,
    GroundingOutput,
    _GroundingInvocationGap,
)
from jimu_runtime.domain.nodes.observation import ObservationInput
from jimu_runtime.domain.nodes.tool_execution import ToolNodeInput, ToolNodeOutput
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityTrace,
    ProfileEligibilityEvaluation,
)
from jimu_runtime.domain.runtime.authority import (
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.coverage import MandatoryRuleObligation
from jimu_runtime.domain.runtime.decisions import RecordList
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import NodeId, PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import FactRecord
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import (
    GroundingRecord,
    ModelGroundingProvenance,
    ObjectRecord,
)
from jimu_runtime.domain.runtime.selection import AllDeclaredSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.domain.validation.applicability import ApplicabilityScope
from jimu_runtime.workflow.engine import DecisionDraft
from jimu_runtime.workflow.mock_context import SyntheticGateResult
from jimu_runtime.workflow.mock_decision import SyntheticDecisionResult
from jimu_runtime.workflow.mock_facts import SyntheticEvidence
from jimu_runtime.workflow.mock_perception import SyntheticDiscoveryResult
from jimu_runtime.workflow.mock_support import K, SyntheticArtifact, artifact

MESSAGE_KEY = "worker.message"
GATE_KEY = "worker.eligibility"
SKIP_KEY = "worker.discovery.skip"


class _Message(ContractModel):
    schema_version: Literal["1.0.0"]
    worker_format: Literal["configured-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def input_envelope(self) -> NodeInputEnvelope:
        raise NotImplementedError

    def output_envelope(self) -> NodeOutputEnvelope:
        raise NotImplementedError

    @model_validator(mode="after")
    def compare_envelopes(self) -> Self:
        check_envelope_pair(self.input_envelope(), self.output_envelope())
        if self.input_envelope().trace != self.output_envelope().trace:
            raise ValueError("node messages must preserve the full invocation trace")
        return self


class ContextMessage(_Message):
    kind: Literal["context"] = "context"
    inputs: ContextBuildInput
    output: ContextBuildOutput
    snapshot: ContextSnapshot | None

    def input_envelope(self) -> NodeInputEnvelope:
        return self.inputs.envelope

    def output_envelope(self) -> NodeOutputEnvelope:
        return self.output.envelope


class EligibilityMessage(ContractModel):
    schema_version: Literal["1.0.0"]
    evaluation: ProfileEligibilityEvaluation
    traces: RecordList[ApplicabilityTrace]
    scope: ApplicabilityScope

    def result(self) -> SyntheticGateResult:
        return SyntheticGateResult(self.evaluation, self.traces, self.scope)


class DiscoveryMessage(_Message):
    kind: Literal["discovery"] = "discovery"
    inputs: DiscoveryInput
    output: DiscoveryAttemptOutput
    selections: RecordList[AllDeclaredSelectionRecord]
    invocations: RecordList[ModelInvocation]
    group_failures: RecordList[_DiscoveryGroupFailure] = Field(
        default=(), exclude_if=lambda value: not value
    )

    def input_envelope(self) -> NodeInputEnvelope:
        return self.inputs.envelope

    def output_envelope(self) -> NodeOutputEnvelope:
        return self.output.envelope

    def result(self) -> SyntheticDiscoveryResult:
        artifacts = {
            item.ref: item
            for item in (
                *(
                    artifact(K.QUESTION_SELECTION_RECORD, item.selection_id, item)
                    for item in self.selections
                ),
                *(
                    artifact(K.MODEL_INVOCATION, item.model_invocation_id, item)
                    for item in self.invocations
                ),
            )
        }
        selected = tuple(
            artifacts[pin.ref] for pin in self.output.envelope.output_artifacts
        )
        if (
            tuple(item.pin for item in selected)
            != self.output.envelope.output_artifacts
        ):
            raise ValueError(
                "discovery message must reproduce its exact durable output manifest"
            )
        return SyntheticDiscoveryResult(
            self.output, self.selections, self.invocations, selected
        )


class GroundingMessage(_Message):
    kind: Literal["grounding"] = "grounding"
    inputs: GroundingInput
    output: GroundingOutput
    records: RecordList[GroundingRecord]
    objects: RecordList[ObjectRecord]
    invocations: RecordList[ModelInvocation]
    tool_executions: RecordList[ToolExecution] = ()
    nested_invocations: RecordList[ModelInvocation] = ()
    evidence: RecordList[EvidenceAsset] = ()
    undispatched_profiles: RecordList[_GroundingInvocationGap] = Field(
        default=(), exclude_if=lambda value: not value
    )

    def input_envelope(self) -> NodeInputEnvelope:
        return self.inputs.envelope

    def output_envelope(self) -> NodeOutputEnvelope:
        return self.output.envelope

    def producers(self) -> tuple[ModelInvocation | ToolExecution, ...]:
        models = {entry.model_invocation_id: entry for entry in self.invocations}
        tools = {entry.tool_execution_id: entry for entry in self.tool_executions}
        nested = {entry.model_invocation_id: entry for entry in self.nested_invocations}
        required_nested = {
            ref.record_id
            for record in self.records
            if not isinstance(record.producer, ModelGroundingProvenance)
            for ref in record.producer.nested_model_invocation_refs
        }
        if (
            len(models) != len(self.invocations)
            or len(tools) != len(self.tool_executions)
            or len(nested) != len(self.nested_invocations)
            or set(nested) != required_nested
            or set(nested) & set(models)
        ):
            raise ValueError("grounding requires its exact direct and nested producers")
        resolved: list[ModelInvocation | ToolExecution] = []
        for record in self.records:
            producer = record.producer
            if isinstance(producer, ModelGroundingProvenance):
                actual: ModelInvocation | ToolExecution | None = models.pop(
                    producer.model_invocation_ref.record_id, None
                )
            else:
                actual = tools.pop(producer.tool_execution_ref.record_id, None)
            if actual is None:
                raise ValueError("missing exact direct grounding producer")
            resolved.append(actual)
        if models or tools:
            raise ValueError("unreferenced direct grounding producer")
        return tuple(resolved)


class FixtureEvidenceContent(ContractModel):
    """Small synthetic request bytes stored beside their exact evidence record."""

    schema_version: Literal["1.0.0"]
    evidence: EvidenceAsset
    base64_content: Annotated[str, Field(max_length=1_048_576)]

    @classmethod
    def from_evidence(cls, item: SyntheticEvidence) -> Self:
        return cls(
            schema_version="1.0.0",
            evidence=item.record,
            base64_content=b64encode(item.content).decode("ascii"),
        )

    @model_validator(mode="after")
    def verify_content(self) -> Self:
        content = b64decode(self.base64_content, validate=True)
        if (
            b64encode(content).decode("ascii") != self.base64_content
            or len(content) != self.evidence.byte_size
            or sha256_digest(content) != self.evidence.sha256
        ):
            raise ValueError("synthetic request evidence must preserve its exact bytes")
        return self


class ObservationMessage(_Message):
    kind: Literal["observation"] = "observation"
    inputs: ObservationInput
    output: NodeOutputEnvelope
    batch: ObservationBatch
    invocation: ModelInvocation
    request_evidence: RecordList[FixtureEvidenceContent] = ()
    durable_request_evidence: RecordList[EvidenceAsset] = Field(
        default=(), exclude_if=lambda value: not value
    )
    authority: ObservationBatchSelection | None

    def input_envelope(self) -> NodeInputEnvelope:
        return self.inputs.envelope

    def output_envelope(self) -> NodeOutputEnvelope:
        return self.output

    @property
    def evidence(self) -> tuple[EvidenceAsset, ...]:
        return (
            *tuple(item.evidence for item in self.request_evidence),
            *self.durable_request_evidence,
        )

    @property
    def facts(self) -> tuple[FactRecord, ...]:
        return tuple(result.primary_fact for result in self.batch.question_results)

    @model_validator(mode="after")
    def check_batch_authority(self) -> Self:
        trusted = self.batch.outcome in {"complete", "partial"}
        if self.request_evidence and self.durable_request_evidence:
            raise ValueError("observation requires a single request evidence source")
        if self.batch.arrival_qualification == "late" and self.authority is not None:
            raise ValueError("late observation cannot select batch authority")
        if trusted:
            if not isinstance(self.output, SucceededNodeOutput) or (
                (self.batch.arrival_qualification == "on_time")
                != (self.authority is not None)
                or self.batch.arrival_qualification == "unresolved"
            ):
                raise ValueError(
                    "trusted observation requires selected on-time authority or late archival"
                )
        elif (
            not isinstance(self.output, FailedNodeOutput) or self.authority is not None
        ):
            raise ValueError("failed whole batch cannot select observation authority")
        return self

    @property
    def authority_artifact(self) -> SyntheticArtifact:
        if self.authority is None:
            raise ValueError("failed observation batch has no authority artifact")
        return artifact(
            K.AUTHORITATIVE_RESULT_SELECTION,
            self.authority.selection_id,
            self.authority,
        )


class ToolMessage(_Message):
    kind: Literal["tool"] = "tool"
    inputs: ToolNodeInput
    output: ToolNodeOutput
    executions: RecordList[ToolExecution]
    facts: RecordList[FactRecord]
    authorities: RecordList[ToolStepSelection]
    evidence: RecordList[EvidenceAsset] = ()
    nested_invocations: RecordList[ModelInvocation] = ()

    # Facts made available to post-claim step execution or guard evaluation.
    observation_input_artifacts: RecordList[PinnedRuntimeInput] = Field(
        default=(), exclude_if=lambda value: not value
    )

    def input_envelope(self) -> NodeInputEnvelope:
        return self.inputs.envelope

    def output_envelope(self) -> NodeOutputEnvelope:
        return self.output.envelope

    @property
    def authority_artifacts(self) -> tuple[SyntheticArtifact, ...]:
        return tuple(
            artifact(K.AUTHORITATIVE_RESULT_SELECTION, item.selection_id, item)
            for item in self.authorities
        )


class DecisionMessage(_Message):
    kind: Literal["decision"] = "decision"
    draft: DecisionDraft
    obligations: RecordList[MandatoryRuleObligation]
    traces: RecordList[ApplicabilityTrace]
    invocation: ModelInvocation | None
    evidence: RecordList[EvidenceAsset]
    candidate_diagnostic: ArtifactId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def input_envelope(self) -> NodeInputEnvelope:
        return self.draft.inputs.envelope

    def output_envelope(self) -> NodeOutputEnvelope:
        return self.draft.output_envelope

    def result(self) -> SyntheticDecisionResult:
        candidate = self.draft.candidate
        artifacts = (
            artifact(K.DECISION, self.draft.decision.decision_id, self.draft.decision),
            *(
                ()
                if candidate is None
                else (
                    artifact(
                        K.CANDIDATE_PREDICTION, candidate.prediction_id, candidate
                    ),
                )
            ),
            *(
                ()
                if self.invocation is None
                else (
                    artifact(
                        K.MODEL_INVOCATION,
                        self.invocation.model_invocation_id,
                        self.invocation,
                    ),
                )
            ),
            *(
                artifact(K.RULE_SET_APPLICABILITY_EVALUATION, item.evaluation_id, item)
                for item in self.draft.rule_set_evaluations
            ),
            *(
                artifact(K.RULE_APPLICABILITY_EVALUATION, item.evaluation_id, item)
                for item in self.draft.rule_evaluations
            ),
            *(
                artifact(K.APPLICABILITY_TRACE, item.applicability_trace_id, item)
                for item in self.traces
            ),
            *(artifact(K.EVIDENCE, item.evidence_id, item) for item in self.evidence),
        )
        if (
            tuple(item.pin for item in artifacts)
            != self.output_envelope().output_artifacts
        ):
            raise ValueError(
                "decision message must reproduce its exact durable output manifest"
            )
        return SyntheticDecisionResult(
            self.draft,
            self.obligations,
            self.traces,
            self.invocation,
            self.evidence,
            artifacts,
        )


type NodeMessage = Annotated[
    ContextMessage
    | DiscoveryMessage
    | GroundingMessage
    | ObservationMessage
    | ToolMessage
    | DecisionMessage,
    Field(discriminator="kind"),
]
NODE_MESSAGE = TypeAdapter[NodeMessage](NodeMessage)

MESSAGE_NODES: dict[NodeId, type[_Message]] = {
    NodeId.N01: ContextMessage,
    NodeId.N02: DiscoveryMessage,
    NodeId.N03: GroundingMessage,
    NodeId.N04: ObservationMessage,
    NodeId.N05: ToolMessage,
    NodeId.N06: DecisionMessage,
}
