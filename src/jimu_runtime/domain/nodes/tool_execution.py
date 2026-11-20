"""N05 binds one Object to the Profile's fixed ToolPlan and validated inputs."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    AbsentOptional,
    OptionalContract,
    immutable_sequence,
    omit_default_from_schema,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition, ToolStep
from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    QuestionProducerSelector,
    ToolPlanContentRef,
)
from jimu_runtime.domain.nodes.envelopes import (
    NodeInputEnvelope,
    NodeIssues,
    NodeOutputEnvelope,
    check_envelope_pair,
)
from jimu_runtime.domain.nodes.observation import ObjectImageView
from jimu_runtime.domain.primitives import ArtifactId, RecordId
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.authority import (
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    AuthoritySelectionRef,
    EvidenceRefs,
    FactRecord,
    FactRefs,
    NodeRunRef,
    ObjectRef,
    QuestionFactSource,
    ToolExecutionRef,
    ToolFactSource,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import (
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.tool_mappings import FactInput


def matches_observation_selector(fact: FactRecord, selector: FactSelector) -> bool:
    producer = selector.producer_selector
    return (
        isinstance(producer, QuestionProducerSelector)
        and isinstance(fact.source, QuestionFactSource)
        and (selector.fact_key, selector.fact_schema_ref, producer.question_ref)
        == (fact.fact_key, fact.fact_schema_ref, fact.source.question_ref)
    )


def observation_facts(
    steps: tuple[ToolStep, ...],
    facts: tuple[FactRecord, ...],
    guard_selectors: tuple[FactSelector, ...] = (),
) -> tuple[FactRecord, ...]:
    selectors = (
        *guard_selectors,
        *(
            source.selector
            for step in steps
            for binding in step.input_bindings
            if isinstance(source := binding.source, FactInput)
        ),
    )
    return tuple(
        fact
        for fact in facts
        if any(matches_observation_selector(fact, selector) for selector in selectors)
    )


class ToolNodeInput(OptionalContract):
    schema_version: Literal["1.0.0"]
    envelope: NodeInputEnvelope
    object_record: ObjectRecord
    image_view: ObjectImageView
    context_snapshot_ref: ContextSnapshotRef
    tool_plan_ref: ToolPlanContentRef
    validated_observation_fact_refs: FactRefs
    observation_authority_ref: AbsentOptional[AuthoritySelectionRef] = Field(
        default=None,
        validate_default=False,
        exclude_if=lambda value: value is None,
        json_schema_extra=omit_default_from_schema,
    )

    @model_validator(mode="after")
    def check_inputs(self) -> Self:
        if (
            self.envelope.node_id is not NodeId.N05
            or self.object_record.execution_id != self.envelope.execution_id
        ):
            raise ValueError(
                "the bound ToolPlan must belong to this N05 Object Execution"
            )
        if any(
            item.ref.record_kind
            not in {
                RuntimeRecordKind.TASK,
                RuntimeRecordKind.CONTEXT_SNAPSHOT,
                RuntimeRecordKind.OBJECT,
                RuntimeRecordKind.FACT,
                RuntimeRecordKind.EVIDENCE,
                RuntimeRecordKind.OBSERVATION_BATCH,
                RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION,
            }
            for item in self.envelope.upstream_artifacts
        ):
            raise ValueError(
                "N05 may only consume Object, context and validated observation artifacts"
            )
        if (
            self.validated_observation_fact_refs
            and self.observation_authority_ref is None
        ):
            raise ValueError(
                "Tool Plan observation Facts require a pinned authority revision"
            )
        require_unique_runtime_refs(self.validated_observation_fact_refs)
        self.image_view.check_object(self.object_record)
        return self

    def check_dispatch_object(self) -> None:
        """Require the current N03 Object and its ROI on the original image."""
        obj = self.object_record
        if self.image_view.marked_media_ref is not None:
            raise ValueError(
                "N05 dispatch requires the original image without marked media"
            )
        self.image_view.check_object(obj, original_regions=True)
        digest = sha256(canonical_bytes(obj.model_dump(mode="json"))).hexdigest()
        object_pins = tuple(
            (item.ref.record_id, item.digest)
            for item in self.envelope.upstream_artifacts
            if item.ref.record_kind is RuntimeRecordKind.OBJECT
        )
        if object_pins != ((obj.object_id, digest),):
            raise ValueError("N05 dispatch requires the exact pinned N03 Object")

    def check_plan(self, profile: ProfileDefinition, plan: ToolPlanDefinition) -> None:
        obj = self.object_record
        if (profile.profile_id, profile.version, profile.tool_plan_ref) != (
            obj.profile_ref.artifact_id,
            obj.profile_ref.version,
            self.tool_plan_ref,
        ) or (plan.tool_plan_id, plan.version) != (
            self.tool_plan_ref.artifact_id,
            self.tool_plan_ref.version,
        ):
            raise ValueError("N05 may only use the resolved Profile-pinned ToolPlan")

    def check_observation_facts(
        self,
        plan: ToolPlanDefinition,
        facts: tuple[FactRecord, ...],
        selection: ObservationBatchSelection | None,
        *,
        guard_selectors: tuple[FactSelector, ...] = (),
    ) -> None:
        self.check_observation_selection(facts, selection)
        if facts != observation_facts(plan.steps, facts, guard_selectors):
            raise ValueError(
                "N05 observation Facts must come from declared Tool inputs"
            )

    def check_observation_selection(
        self, facts: tuple[FactRecord, ...], selection: ObservationBatchSelection | None
    ) -> None:
        if tuple(fact.fact_id for fact in facts) != tuple(
            ref.record_id for ref in self.validated_observation_fact_refs
        ):
            raise ValueError(
                "resolve the declared validated Observation Fact references"
            )
        if selection is None:
            if self.observation_authority_ref is not None or facts:
                raise ValueError("missing authoritative Observation selection")
            return
        if (
            self.observation_authority_ref is None
            or self.observation_authority_ref.record_id != selection.selection_id
            or (
                self.envelope.execution_id,
                self.object_record.object_id,
                self.object_record.profile_ref,
            )
            != (selection.execution_id, selection.object_id, selection.profile_ref)
            or not set(self.validated_observation_fact_refs)
            <= set(selection.selected_fact_refs)
        ):
            raise ValueError(
                "N05 input Facts must resolve to the pinned Observation authority"
            )
        for fact in facts:
            if not isinstance(fact.source, QuestionFactSource) or (
                fact.execution_id,
                fact.object_id,
                fact.profile_ref,
                fact.attempt_id,
                fact.source.observation_batch_ref,
            ) != (
                selection.execution_id,
                selection.object_id,
                selection.profile_ref,
                selection.selected_attempt_id,
                selection.selected_result_ref,
            ):
                raise ValueError(
                    "N05 observation Facts must come from the selected batch"
                )


class InvokedToolStep(ContractModel):
    disposition: Literal["invoked"]
    step_id: ArtifactId
    tool_execution_ref: ToolExecutionRef


class UninvokedToolStep(ContractModel):
    disposition: Literal["blocked", "skipped"]
    step_id: ArtifactId
    reason_code: ArtifactId
    validation_issues: NodeIssues


type ToolPlanStepResult = Annotated[
    InvokedToolStep | UninvokedToolStep, Field(discriminator="disposition")
]


class ToolPlanRunSummary(ContractModel):
    tool_plan_ref: ToolPlanContentRef
    object_ref: ObjectRef
    status: Literal["succeeded", "partial", "failed"]
    steps: Annotated[
        tuple[ToolPlanStepResult, ...],
        Field(max_length=1024),
        BeforeValidator(immutable_sequence),
    ]
    validation_issues: NodeIssues

    @model_validator(mode="after")
    def check_summary(self) -> Self:
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("ToolPlan summary must identify each step once")
        refs = tuple(
            step.tool_execution_ref
            for step in self.steps
            if isinstance(step, InvokedToolStep)
        )
        require_unique_runtime_refs(refs)
        if self.status != "succeeded" and not self.validation_issues:
            raise ValueError("incomplete ToolPlan must retain failure diagnostics")
        return self


class ToolPlanCompletionRecord(ContractModel):
    """Durable success of a configured N05 Attempt with no invoked steps."""

    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef
    summary: ToolPlanRunSummary

    @model_validator(mode="after")
    def check_no_invocations(self) -> Self:
        if (
            self.summary.status != "succeeded"
            or self.summary.validation_issues
            or any(
                not isinstance(step, UninvokedToolStep)
                or step.disposition != "skipped"
                or step.validation_issues
                for step in self.summary.steps
            )
        ):
            raise ValueError(
                "ToolPlan completion requires only successful skips or an empty plan"
            )
        return self


@dataclass(frozen=True, slots=True)
class ToolStepHistoryEntry:
    """Internal, resolved persistence evidence for one selected parent output."""

    original_parent_attempt: Attempt
    child_node: NodeRun
    child_attempt: Attempt
    execution: ToolExecution
    facts: tuple[FactRecord, ...]
    selection: ToolStepSelection | None
    evidence: tuple[EvidenceAsset, ...]
    invocations: tuple[ModelInvocation, ...]
    verified_observation_inputs: tuple[PinnedRuntimeInput, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolStepHistory:
    """The durable child outputs explicitly bound to one business N05 Attempt."""

    inputs: ToolNodeInput
    parent_node: NodeRun
    parent_attempt: Attempt
    entries: tuple[ToolStepHistoryEntry, ...]

    def resolved_executions(
        self, inputs: ToolNodeInput, facts: tuple[FactRecord, ...]
    ) -> dict[str, ToolExecution]:
        env = inputs.envelope
        parent = self.parent_node
        current = self.parent_attempt
        if (
            self.inputs != inputs
            or parent.node_id is not NodeId.N05
            or (parent.node_run_id, current.attempt_id, parent.execution_id)
            != (env.node_run_id, env.attempt_id, env.execution_id)
            or not current.belongs_to_node(parent)
            or (
                parent.trace,
                reference_identity(parent.config_ref),
                parent.upstream_inputs,
            )
            != (
                env.trace,
                reference_identity(env.node_config_ref),
                env.upstream_artifacts,
            )
        ):
            raise ValueError(
                "tool step history must resolve this exact parent invocation"
            )
        original_rounds = {
            link.attempt_ref.record_id: link.attempt_number
            for link in parent.attempt_history
        }
        if original_rounds.get(current.attempt_id) != current.attempt_number:
            raise ValueError("parent Attempt must occur in the durable NodeRun history")
        indexed_facts = {fact.fact_id: fact for fact in facts}
        executions: dict[str, ToolExecution] = {}
        for entry in self.entries:
            child, attempt, execution = (
                entry.child_node,
                entry.child_attempt,
                entry.execution,
            )
            original = entry.original_parent_attempt
            succeeded = execution.status == "succeeded"
            expected_node_status = (
                NodeRunStatus.SUCCEEDED if succeeded else NodeRunStatus.FAILED
            )
            expected_attempts = (
                (AttemptStatus.SUCCEEDED,)
                if succeeded
                else (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT)
            )
            supplemental = child.upstream_inputs[len(parent.upstream_inputs) :]
            if (
                not original.belongs_to_node(parent)
                or original_rounds.get(original.attempt_id) != original.attempt_number
                or original.attempt_number > current.attempt_number
                or original.attempt_id != current.attempt_id
                and (
                    original.ended_at is None
                    or not (succeeded or original.observation_dependency_deferred)
                )
                or child.node_id is not NodeId.N05
                or child.node_run_id == parent.node_run_id
                or child.execution_id != parent.execution_id
                or (child.trace, child.config_ref) != (parent.trace, parent.config_ref)
                or child.upstream_inputs[: len(parent.upstream_inputs)]
                != parent.upstream_inputs
                or supplemental != entry.verified_observation_inputs
                or not attempt.belongs_to_node(child)
                or len(child.attempt_history) != 1
                or child.attempt_history[0].attempt_ref != execution.attempt_ref
                or child.attempt_history[0].attempt_number != attempt.attempt_number
                or execution.node_run_ref.record_id != child.node_run_id
                or execution.attempt_ref.record_id != attempt.attempt_id
                or child.status is not expected_node_status
                or attempt.status not in expected_attempts
                or succeeded != (entry.selection is not None)
                or execution.tool_execution_id in executions
                or any(indexed_facts.get(fact.fact_id) != fact for fact in entry.facts)
            ):
                raise ValueError(
                    "tool step must retain its complete original child result"
                )
            executions[execution.tool_execution_id] = execution
        return executions


class ToolNodeOutput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeOutputEnvelope
    summary: ToolPlanRunSummary
    tool_fact_refs: FactRefs
    evidence_refs: EvidenceRefs

    @model_validator(mode="after")
    def check_output(self) -> Self:
        if self.envelope.node_id is not NodeId.N05:
            raise ValueError("Tool node output requires N05")
        if (
            self.envelope.status is NodeRunStatus.SUCCEEDED
            and self.summary.status == "failed"
            or self.envelope.status is NodeRunStatus.FAILED
            and self.summary.status == "succeeded"
        ):
            raise ValueError("ToolPlan outcome must agree with the node status")
        require_unique_runtime_refs(self.tool_fact_refs)
        require_unique_runtime_refs(self.evidence_refs)
        return self

    def check_steps(
        self,
        inputs: ToolNodeInput,
        profile: ProfileDefinition,
        plan: ToolPlanDefinition,
        executions: tuple[ToolExecution, ...],
        facts: tuple[FactRecord, ...],
        *,
        history: ToolStepHistory | None = None,
    ) -> None:
        check_envelope_pair(inputs.envelope, self.envelope)
        inputs.check_plan(profile, plan)
        if (
            self.summary.object_ref.record_id != inputs.object_record.object_id
            or self.summary.tool_plan_ref != inputs.tool_plan_ref
            or tuple(summary.step_id for summary in self.summary.steps)
            != tuple(step.step_id for step in plan.steps)
        ):
            raise ValueError(
                "ToolPlan summary must retain Object, fixed Plan and all configured steps"
            )
        refs = tuple(
            step.tool_execution_ref
            for step in self.summary.steps
            if isinstance(step, InvokedToolStep)
        )
        if tuple(execution.tool_execution_id for execution in executions) != tuple(
            ref.record_id for ref in refs
        ):
            raise ValueError("resolve all recorded ToolPlan step invocations")
        actual = {execution.tool_execution_id: execution for execution in executions}
        if history is not None and history.resolved_executions(inputs, facts) != actual:
            raise ValueError(
                "summary must resolve every durably bound tool step exactly"
            )
        fact_by_id = {fact.fact_id: fact for fact in facts}
        all_facts = {
            ref for execution in executions for ref in execution.generated_fact_refs
        }
        all_evidence = {
            ref for execution in executions for ref in execution.evidence_refs
        }
        if (
            len(fact_by_id) != len(facts)
            or fact_by_id.keys() != {ref.record_id for ref in self.tool_fact_refs}
            or (
                set(self.tool_fact_refs) != all_facts
                or not all_evidence <= set(self.evidence_refs)
            )
        ):
            raise ValueError(
                "Tool node output must retain generated Fact and step evidence references"
            )
        unavailable_required = False
        for declared, summary in zip(plan.steps, self.summary.steps, strict=True):
            if isinstance(summary, UninvokedToolStep):
                unavailable_required |= (
                    declared.requirement == "required"
                    and summary.disposition == "blocked"
                )
                continue
            execution = actual[summary.tool_execution_ref.record_id]
            origin = execution.invocation_origin
            if (
                not isinstance(origin, ToolPlanStepOrigin)
                or (
                    origin.tool_plan_ref,
                    origin.step_id,
                    execution.tool_ref,
                    execution.execution_id,
                    execution.profile_ref,
                    execution.object_ref,
                )
                != (
                    inputs.tool_plan_ref,
                    declared.step_id,
                    declared.tool_ref,
                    inputs.envelope.execution_id,
                    inputs.object_record.profile_ref,
                    self.summary.object_ref,
                )
                or history is None
                and (
                    execution.node_run_ref.record_id,
                    execution.attempt_ref.record_id,
                )
                != (self.envelope.node_run_id, self.envelope.attempt_id)
            ):
                raise ValueError(
                    "N05 Tool execution must resolve to an actual bound ToolPlan step"
                )
            unavailable_required |= (
                declared.requirement == "required" and execution.status != "succeeded"
            )
            allowed = {
                mapping.output_name: mapping
                for mapping in declared.fact_output_mappings
            }
            for ref in execution.generated_fact_refs:
                fact = fact_by_id[ref.record_id]
                source = fact.source
                if (
                    not isinstance(source, ToolFactSource)
                    or (
                        source.tool_execution_ref.record_id,
                        source.tool_plan_ref,
                        source.step_id,
                        source.tool_ref,
                        fact.execution_id,
                        fact.profile_ref,
                        fact.object_id,
                        fact.attempt_id,
                    )
                    != (
                        execution.tool_execution_id,
                        origin.tool_plan_ref,
                        origin.step_id,
                        execution.tool_ref,
                        execution.execution_id,
                        execution.profile_ref,
                        self.summary.object_ref.record_id,
                        execution.attempt_ref.record_id,
                    )
                    or source.output_name not in allowed
                    or (fact.fact_key != allowed[source.output_name].target_fact_key)
                ):
                    raise ValueError(
                        "Tool Fact must come from the actual declared N05 output mapping"
                    )
        if unavailable_required and self.summary.status == "succeeded":
            raise ValueError("unavailable required Tool work must remain visible")
