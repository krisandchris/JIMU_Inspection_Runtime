"""Durable execution and authority for internal N05 child attempts."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import EvidenceType, FactScope
from jimu_runtime.domain.definition_support import FieldPath, reference_identity
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolStep
from jimu_runtime.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
    RuntimeRecordKind,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import (
    FactSelector,
    ToolContentRef,
    ToolPlanContentRef,
)
from jimu_runtime.domain.nodes.envelopes import SucceededNodeOutput
from jimu_runtime.domain.nodes.tool_execution import (
    ToolNodeInput,
    ToolStepHistory,
    ToolStepHistoryEntry,
    observation_facts,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.authority import (
    ActorRef,
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.decisions import RecordList
from jimu_runtime.domain.runtime.evidence import (
    EvidenceAsset,
    ObjectEvidenceScope,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptLink,
    LifecycleReason,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    FactRecord,
    FactRef,
    KnownFact,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
    ToolExecutionRef,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import FinalizedAsset
from jimu_runtime.domain.runtime.tool_executions import (
    FailedToolAttempt,
    FailedToolResult,
    KnownToolOutputState,
    ReturnedToolAttempt,
    ToolArtifactRecordRef,
    ToolArtifactRef,
    ToolExecution,
    ToolPlanStepOrigin,
    UndispatchedToolAttempt,
)
from jimu_runtime.domain.tool_mappings import StepOutputInput
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.domain.validation.transitions import (
    AttemptTransitionGuard,
    NodeTransitionGuard,
)
from jimu_runtime.domain.values import RecordValue
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.jobs import (
    JobClaim,
    _attempt_ref,
    _clock,
    _digest,
    _finish_attempt,
    _Job,
    _job,
    _JobStatus,
    _matches,
    _node_ref,
    _release_job,
    _scope,
    _write_job,
)
from jimu_runtime.workflow.mock_support import artifact
from jimu_runtime.workflow.scheduling import validated_milliseconds
from jimu_runtime.workflow.tool_dependencies import observation_selectors
from jimu_runtime.workflow.worker_records import MESSAGE_KEY, ObservationMessage

TOOL_STEP_BRANCH_PREFIX = "tool-step:"
_DIGEST: TypeAdapter[str] = TypeAdapter(Digest)
_SCOPE = "project_id = :project_id AND client_id = :client_id AND execution_id = :execution_id"


class _AncestorInput(ContractModel):
    input_path: FieldPath
    step_id: ArtifactId
    result_ref: ToolExecutionRef
    selection_ref: AuthoritySelectionRef
    fact_ref: FactRef
    evidence: ToolArtifactRef
    output_name: ArtifactId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    evidence_slot: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class _StepInput(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    inputs: ToolNodeInput
    step: ToolStep
    invocation_input_digest: Digest
    ancestor_inputs: RecordList[_AncestorInput] = Field(
        default=(), max_length=64, exclude_if=lambda value: not value
    )

    guard_selectors: RecordList[FactSelector] = Field(
        default=(), max_length=1024, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def exact_ancestor_inputs(self) -> Self:
        paths = {pin.input_path for pin in self.ancestor_inputs}
        if len(paths) != len(self.ancestor_inputs) or len(
            {pin.evidence.artifact_ref for pin in self.ancestor_inputs}
        ) != len(paths):
            raise ValueError("exact ToolPlan ancestor evidence is required")
        return self

    def has_required_ancestor_inputs(self) -> bool:
        if self.step.tool_ref.artifact_id != "wheel_boundary_geometry":
            return True
        required = {
            item.target.field_path
            for item in self.step.input_bindings
            if isinstance(item.source, StepOutputInput) and item.required
        }
        return required <= {pin.input_path for pin in self.ancestor_inputs}

    def bound_digest(self) -> str:
        # Only the changing parent Attempt identity is excluded from reuse.
        fixed_envelope = self.inputs.envelope.model_copy(
            update={"attempt_id": "pinned"}
        )
        fixed = self.inputs.model_copy(update={"envelope": fixed_envelope})
        return _digest(self.model_copy(update={"inputs": fixed}))

    def matches_reuse(self, following: "_StepInput") -> bool:
        if (
            not self.has_required_ancestor_inputs()
            or not following.has_required_ancestor_inputs()
        ):
            return False
        if self.bound_digest() == following.bound_digest():
            return True
        if len(self.ancestor_inputs) != len(following.ancestor_inputs):
            return False
        pins = tuple(
            new.model_copy(update={"output_name": None, "evidence_slot": None})
            if old.output_name is None and old.evidence_slot is None
            else new
            for old, new in zip(
                self.ancestor_inputs, following.ancestor_inputs, strict=True
            )
        )
        return (
            self.bound_digest()
            == following.model_copy(update={"ancestor_inputs": pins}).bound_digest()
        )


class StepResult(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    execution: ToolExecution
    facts: RecordList[FactRecord] = ()
    evidence: RecordList[EvidenceAsset] = ()
    invocations: RecordList[ModelInvocation] = ()

    @model_validator(mode="after")
    def check_outputs(self) -> Self:
        tool = self.execution
        evidence = {item.evidence_id: item for item in self.evidence}
        required = {
            ref.record_id
            for item in (tool, *self.facts, *self.invocations)
            for ref in item.evidence_refs
        }
        if (
            tuple(ref.record_id for ref in tool.generated_fact_refs)
            != tuple(item.fact_id for item in self.facts)
            or len(evidence) != len(self.evidence)
            or set(evidence) != required
            or any(
                item.artifact_ref.record_id not in evidence
                or evidence[item.artifact_ref.record_id].sha256 != item.digest
                for item in tool.output_refs
                if item.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
            )
            or any(
                item.scope.execution_id != tool.execution_id
                or isinstance(item.scope, ObjectEvidenceScope)
                and (item.scope.object_ref, item.scope.profile_ref)
                != (tool.object_ref, tool.profile_ref)
                or not isinstance(item.stage, FinalizedAsset)
                for item in self.evidence
            )
            or len({item.model_invocation_id for item in self.invocations})
            != len(self.invocations)
            or any(
                (
                    item.execution_id,
                    item.node_id,
                    item.node_run_ref,
                    item.attempt_ref,
                    item.object_ref,
                    item.profile_refs,
                    item.trace_id,
                )
                != (
                    tool.execution_id,
                    "N05",
                    tool.node_run_ref,
                    tool.attempt_ref,
                    tool.object_ref,
                    (tool.profile_ref,),
                    tool.trace_id,
                )
                for item in self.invocations
            )
        ):
            raise ValueError(
                "step completion requires the exact original Fact, evidence and nested invocation group"
            )
        return self


class StepCompletion(ContractModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    result: StepResult
    attempt_status: Literal["succeeded", "failed", "timed_out"]
    selection: ToolStepSelection | None

    @model_validator(mode="after")
    def check_selection(self) -> Self:
        tool = self.result.execution
        winner = self.selection
        if (
            (tool.status == "succeeded") != (winner is not None)
            or (self.attempt_status == "succeeded") != (winner is not None)
            or self.attempt_status == "timed_out"
            and not _reports_timeout(tool)
        ):
            raise ValueError("only a successful step can have a selection")
        if winner is not None:
            origin = tool.invocation_origin
            if (
                not isinstance(origin, ToolPlanStepOrigin)
                or tool.object_ref is None
                or (
                    winner.execution_id,
                    winner.profile_ref,
                    winner.object_id,
                    winner.tool_plan_ref,
                    winner.step_id,
                    winner.selected_attempt_id,
                    winner.selected_result_ref.record_id,
                    winner.selected_fact_refs,
                )
                != (
                    tool.execution_id,
                    tool.profile_ref,
                    tool.object_ref.record_id,
                    origin.tool_plan_ref,
                    origin.step_id,
                    tool.attempt_ref.record_id,
                    tool.tool_execution_id,
                    tool.generated_fact_refs,
                )
            ):
                raise ValueError(
                    "step selection must cover the entire successful child output"
                )
        return self


@dataclass(frozen=True, slots=True)
class AncestorToolEvidence:
    """A host's read-back of one authoritative child step and named input asset."""

    input_path: tuple[str, ...]
    completion: StepCompletion
    asset: EvidenceAsset
    result_evidence_slot: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveredToolStep:
    completion: StepCompletion
    audit_only: bool
    verified_observation_inputs: tuple[PinnedRuntimeInput, ...] = ()


def _ancestor_pin(
    step: ToolStep,
    path: tuple[str, ...],
    completion: StepCompletion,
    asset: EvidenceAsset,
    *,
    definition: ToolDefinition | None = None,
    archived: _AncestorInput | None = None,
) -> _AncestorInput:
    edge = next(
        (item for item in step.input_bindings if item.target.field_path == path),
        None,
    )
    source = None if edge is None else edge.source
    previous = completion.result.execution
    selection = completion.selection
    attempt = previous.attempt_result
    if (
        not isinstance(source, StepOutputInput)
        or source.step_id not in step.depends_on
        or selection is None
        or selection.step_id != source.step_id
        or not isinstance(attempt, ReturnedToolAttempt)
        or isinstance(attempt.result, FailedToolResult)
        or asset.evidence_type is not EvidenceType.MASK
        or asset not in completion.result.evidence
        or not isinstance(asset.scope, ObjectEvidenceScope)
        or (asset.scope.execution_id, asset.scope.profile_ref, asset.scope.object_ref)
        != (previous.execution_id, previous.profile_ref, previous.object_ref)
    ):
        raise ValueError("ancestor evidence must resolve an exact selected mask input")
    output_name = None if archived is None else archived.output_name
    evidence_slot = None if archived is None else archived.evidence_slot
    slots: set[str] | None = None
    if definition is not None:
        declaration = next(
            (
                item
                for item in definition.fact_outputs
                if item.value_path == source.selector.field_path
            ),
            None,
        )
        if (
            (definition.tool_id, definition.version)
            != (previous.tool_ref.artifact_id, previous.tool_ref.version)
            or definition.output_schema_ref != source.selector.schema_ref
            or declaration is None
        ):
            raise ValueError("ancestor evidence must use the exact declared output")
        output_name = declaration.output_name
        slots = {path[0] for path in declaration.evidence_paths or ()}
    result = attempt.result
    value = result.payload
    for part in source.selector.field_path:
        value = value.value.get(part) if isinstance(value, RecordValue) else None
    evidence = ToolArtifactRef(
        artifact_ref=ToolArtifactRecordRef(
            record_kind=RuntimeRecordKind.EVIDENCE, record_id=asset.evidence_id
        ),
        digest=asset.sha256,
    )
    if value is None or not any(item == evidence for item in previous.output_refs):
        raise ValueError(
            "ancestor evidence digest does not match the exact selected output"
        )
    matches = tuple(
        item
        for item in completion.result.facts
        if isinstance(item, KnownFact)
        and isinstance(item.source, ToolFactSource)
        and item.source.tool_execution_ref == selection.selected_result_ref
        and (output_name is None or item.source.output_name == output_name)
        and item.value == value
    )
    if len(matches) != 1:
        raise ValueError("ancestor output does not identify a unique selected Fact")
    for fact in matches:
        assert isinstance(fact.source, ToolFactSource)
        fact_ref = FactRef(record_kind=RuntimeRecordKind.FACT, record_id=fact.fact_id)
        matching_slots = tuple(
            slot.name
            for slot in result.evidence
            if (
                slot.name in slots
                if slots is not None
                else slot.name == (evidence_slot or fact.source.output_name)
            )
            and tuple(ref.record_id for ref in slot.evidence_refs)
            == (asset.evidence_id,)
        )
        if (
            fact_ref in selection.selected_fact_refs
            and any(ref.record_id == asset.evidence_id for ref in fact.evidence_refs)
            and any(
                isinstance(state, KnownToolOutputState)
                and state.output_name == fact.source.output_name
                for state in result.output_states
            )
            and len(matching_slots) == 1
        ):
            return _AncestorInput(
                input_path=path,
                step_id=source.step_id,
                result_ref=selection.selected_result_ref,
                selection_ref=AuthoritySelectionRef(
                    record_kind=RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION,
                    record_id=selection.selection_id,
                ),
                fact_ref=fact_ref,
                evidence=evidence,
                output_name=output_name,
                evidence_slot=matching_slots[0] if output_name is not None else None,
            )
    raise ValueError(
        "ancestor evidence is missing the exact selected named Fact and slot"
    )


def _check_ancestor_history(
    bound: _StepInput, entries: tuple[ToolStepHistoryEntry, ...]
) -> None:
    selected = {
        entry.selection.selection_id: entry
        for entry in entries
        if entry.selection is not None
    }
    for pin in bound.ancestor_inputs:
        entry = selected.get(pin.selection_ref.record_id)
        if (
            entry is None
            or entry.selection is None
            or entry.selection.tool_plan_ref != bound.inputs.tool_plan_ref
            or entry.selection.object_id != bound.inputs.object_record.object_id
            or entry.child_attempt.status is not AttemptStatus.SUCCEEDED
        ):
            raise ValueError(
                "ancestor evidence requires an explicitly selected durable child"
            )
        asset = next(
            (
                item
                for item in entry.evidence
                if item.evidence_id == pin.evidence.artifact_ref.record_id
            ),
            None,
        )
        completion = StepCompletion(
            result=StepResult(
                execution=entry.execution,
                facts=entry.facts,
                evidence=entry.evidence,
                invocations=entry.invocations,
            ),
            attempt_status="succeeded",
            selection=entry.selection,
        )
        if (
            asset is None
            or _ancestor_pin(
                bound.step, pin.input_path, completion, asset, archived=pin
            )
            != pin
        ):
            raise ValueError("the bound ancestor result or evidence digest changed")


@dataclass(frozen=True, slots=True)
class StepAttempt:
    parent_claim: JobClaim
    parent_attempt_id: str
    claim: JobClaim
    node: NodeRun
    attempt: Attempt
    inputs: ToolNodeInput
    previous: StepCompletion | None = None


@dataclass(frozen=True, slots=True)
class NestedToolStepIdentity:
    execution_id: str
    parent_node_run_ref: NodeRunRef
    tool_plan_ref: ToolPlanContentRef
    step_id: str
    tool_ref: ToolContentRef
    profile_ref: ProfileContentRef
    object_ref: ObjectRef
    bound_input_digest: str


@dataclass(frozen=True, slots=True)
class NestedToolAttemptLink:
    parent_attempt: Attempt
    child_node: NodeRun
    child_attempt: Attempt
    invocations: tuple[ModelInvocation, ...]


@dataclass(frozen=True, slots=True)
class NestedToolRetryHistory:
    identity: NestedToolStepIdentity
    current: NestedToolAttemptLink
    priors: tuple[NestedToolAttemptLink, ...]


class StepAttemptBudgetExhausted(ValueError):
    """Every authorized physical attempt for this logical step has been spent."""


async def _read_job(
    connection: AsyncConnection,
    claim: JobClaim,
    *,
    internal: bool,
    lock: bool = True,
) -> _Job | None:
    row_lock = "FOR UPDATE" if lock else ""
    row = (
        (
            await connection.execute(
                text(f"""
                SELECT * FROM jimu_workflow.jobs WHERE {_SCOPE}
                    AND node_run_id = :node_run_id AND job_id = :job_id
                    AND internal_tool_step = :internal
                {row_lock}
            """),
                {
                    **_scope(claim.scope, claim.execution_id),
                    "node_run_id": claim.node_run_id,
                    "job_id": claim.job_id,
                    "internal": internal,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else _job(row)


def _live(
    job: _Job | None,
    claim: JobClaim,
    attempt_id: str,
    now: datetime,
    *,
    allow_elapsed_deadline: bool = False,
) -> bool:
    return (
        job is not None
        and _matches(claim, job)
        and job.current_attempt_id == attempt_id
        and job.lease_until is not None
        and now < job.lease_until
        and job.attempt_deadline is not None
        and (now < job.attempt_deadline or allow_elapsed_deadline)
    )


def _parent_inputs(
    parent: NodeRun, inputs: ToolNodeInput, *, allow_supplement: bool = False
) -> bool:
    env = inputs.envelope
    exact_envelope = env.node_id is NodeId.N05 and (
        parent.node_id,
        parent.execution_id,
        parent.node_run_id,
        parent.trace,
        reference_identity(parent.config_ref),
        parent.upstream_inputs,
    ) == (
        env.node_id,
        env.execution_id,
        env.node_run_id,
        env.trace,
        reference_identity(env.node_config_ref),
        env.upstream_artifacts[: len(parent.upstream_inputs)]
        if allow_supplement
        else env.upstream_artifacts,
    )
    pinned = {
        (item.ref.record_kind, item.ref.record_id): item.digest
        for item in parent.upstream_inputs
    }
    object_key = (RuntimeRecordKind.OBJECT, inputs.object_record.object_id)
    context = inputs.context_snapshot_ref
    return (
        exact_envelope
        and pinned.get(object_key)
        == sha256_digest(canonical_bytes(inputs.object_record.model_dump(mode="json")))
        and (context.record_kind, context.record_id) in pinned
    )


def _parent_projection(parent: NodeRun, inputs: ToolNodeInput) -> ToolNodeInput:
    if inputs.envelope.upstream_artifacts == parent.upstream_inputs:
        return inputs
    envelope = inputs.envelope.model_copy(
        update={"upstream_artifacts": parent.upstream_inputs}
    )
    return inputs.model_copy(
        update={
            "envelope": envelope,
            "validated_observation_fact_refs": (),
            "observation_authority_ref": None,
        }
    )


async def _check_bound_observation(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    parent: NodeRun,
    bound: _StepInput,
    *,
    parent_inputs: ToolNodeInput | None = None,
) -> tuple[PinnedRuntimeInput, ...]:
    inputs = bound.inputs
    env, obj = inputs.envelope, inputs.object_record
    if not _parent_inputs(parent, inputs, allow_supplement=True):
        raise ValueError("child inputs must inherit the exact N05 parent inputs")
    upstream = parent.upstream_inputs
    supplemental = env.upstream_artifacts[len(upstream) :]
    legacy = any(pin.ref.record_kind is RuntimeRecordKind.FACT for pin in upstream)
    if legacy:
        if supplemental or (
            parent_inputs is not None
            and bound.model_copy(update={"inputs": parent_inputs}).bound_digest()
            != bound.bound_digest()
        ):
            raise ValueError(
                "historical child cannot replace its parent observation inputs"
            )
        return ()
    if parent_inputs is not None and (
        inputs.object_record,
        inputs.image_view,
        inputs.tool_plan_ref,
        inputs.context_snapshot_ref,
    ) != (
        parent_inputs.object_record,
        parent_inputs.image_view,
        parent_inputs.tool_plan_ref,
        parent_inputs.context_snapshot_ref,
    ):
        raise ValueError("child inputs differ from the parent Object or ToolPlan")
    if not observation_selectors(bound.step, obj, bound.guard_selectors):
        if (
            supplemental
            or inputs.validated_observation_fact_refs
            or inputs.observation_authority_ref
        ):
            raise ValueError("N03-only child cannot bind observation inputs")
        return ()

    node = await repo.get_branch(
        connection, scope, env.execution_id, NodeId.N04, f"object:{obj.object_id}"
    )
    object_pin = next(
        pin
        for pin in upstream
        if pin.ref.record_kind is RuntimeRecordKind.OBJECT
        and pin.ref.record_id == obj.object_id
    )
    if (
        node is None
        or object_pin not in node.upstream_inputs
        or node.status
        not in {
            NodeRunStatus.SUCCEEDED,
            NodeRunStatus.FAILED,
            NodeRunStatus.CANCELLED,
            NodeRunStatus.SKIPPED,
            NodeRunStatus.BLOCKED,
        }
    ):
        raise ValueError(
            "observation inputs require a terminal N04 for the same Object"
        )
    message = None
    if node.status is NodeRunStatus.SUCCEEDED:
        output = await repo.read_authority(
            connection,
            scope,
            env.execution_id,
            node.node_run_id,
            SucceededNodeOutput,
        )
        if output is None:
            raise ValueError("successful N04 has no durable output")
        message = await repo.read_output(
            connection,
            scope,
            env.execution_id,
            node.node_run_id,
            output.attempt_id,
            MESSAGE_KEY,
            ObservationMessage,
        )
        if (
            message is None
            or message.output != output
            or message.inputs.object_record != obj
            or message.inputs.envelope.upstream_artifacts != node.upstream_inputs
            or message.authority is None
            or message.authority.selected_attempt_id != output.attempt_id
            or message.authority.selected_result_ref != message.batch.batch_ref
            or not node.attempt_history
            or node.attempt_history[-1].attempt_ref.record_id != output.attempt_id
        ):
            raise ValueError("N04 batch differs from the selected Object output")
    facts = (
        ()
        if message is None
        else observation_facts((bound.step,), message.facts, bound.guard_selectors)
    )
    authority = None if message is None or not facts else message.authority
    fact_pins = tuple(
        artifact(RuntimeRecordKind.FACT, fact.fact_id, fact).pin for fact in facts
    )
    expected = fact_pins
    if authority is not None:
        assert message is not None
        authority_kind = RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION
        stored_selection = await repo.read_output(
            connection,
            scope,
            env.execution_id,
            node.node_run_id,
            message.output.attempt_id,
            f"{authority_kind.value}.{authority.selection_id}",
            ObservationBatchSelection,
        )
        if stored_selection != authority or not set(fact_pins) <= set(
            message.output.output_artifacts
        ):
            raise ValueError("selected N04 Facts require the exact durable authority")
        expected = (*fact_pins, message.authority_artifact.pin)
    inputs.check_observation_selection(facts, authority)
    if tuple(pin.stable_json() for pin in supplemental) != tuple(
        pin.stable_json() for pin in expected
    ):
        raise ValueError("child must pin its exact selected N04 observation inputs")
    return tuple(PinnedRuntimeInput(ref=pin.ref, digest=pin.digest) for pin in expected)


def _child_inputs(
    inputs: ToolNodeInput, child_id: str, attempt_id: str
) -> ToolNodeInput:
    env = inputs.envelope.model_copy(
        update={"node_run_id": child_id, "attempt_id": attempt_id}
    )
    return inputs.model_copy(update={"envelope": env})


def _read_input(row: RowMapping) -> _StepInput:
    result = _StepInput.model_validate_json(row["bound_input"])
    env = result.inputs.envelope
    if (
        result.bound_digest() != row["bound_input_digest"]
        or result.invocation_input_digest != row["invocation_input_digest"]
        or (env.execution_id, env.node_run_id, env.attempt_id, result.step.step_id)
        != (
            row["execution_id"],
            row["parent_node_run_id"],
            row["original_parent_attempt_id"],
            row["step_id"],
        )
    ):
        raise ValueError("durable tool step input identity or digest mismatch")
    return result


def _read_completion(row: RowMapping) -> StepCompletion:
    completion = StepCompletion.model_validate_json(row["completion"])
    tool = completion.result.execution
    if (
        _digest(completion) != row["result_digest"]
        or completion.attempt_status != row["attempt_status"]
        or (
            tool.execution_id,
            tool.node_run_ref.record_id,
            tool.attempt_ref.record_id,
            tool.tool_execution_id,
        )
        != (
            row["execution_id"],
            row["child_node_run_id"],
            row["child_attempt_id"],
            row["tool_execution_id"],
        )
    ):
        raise ValueError("durable tool step completion identity or digest mismatch")
    return completion


_COMPLETED_COLUMNS = """
    c.*, a.parent_attempt_id AS original_parent_attempt_id,
    a.invocation_input_digest, a.record::text AS bound_input, c.record::text AS completion
"""
_COMPLETED_SOURCE = """
    jimu_workflow.tool_step_completions AS c
    JOIN jimu_workflow.tool_step_attempts AS a
        USING (project_id, client_id, execution_id, child_node_run_id, child_attempt_id, parent_node_run_id, step_id, bound_input_digest)
"""
_PARENT_PARAMS = """
    c.project_id = :project_id AND c.client_id = :client_id AND c.execution_id = :execution_id
    AND c.parent_node_run_id = :parent_node_run_id
"""


async def _bind_completion(
    connection: AsyncConnection,
    parameters: dict[str, str],
    row: RowMapping,
) -> None:
    await connection.execute(
        text("""
            INSERT INTO jimu_workflow.tool_step_parent_outputs
                (project_id, client_id, execution_id, parent_node_run_id, parent_attempt_id,
                 step_id, bound_input_digest, child_node_run_id, child_attempt_id, result_digest)
            VALUES (:project_id, :client_id, :execution_id, :parent_node_run_id, :parent_attempt_id,
                 :step_id, :bound_input_digest, :child_node_run_id, :child_attempt_id, :result_digest)
        """),
        {
            **parameters,
            **{
                key: row[key]
                for key in ("child_node_run_id", "child_attempt_id", "result_digest")
            },
        },
    )


class StepAttemptStore:
    def __init__(self, engine: AsyncEngine, *, actor: ActorRef) -> None:
        if actor.actor_kind != "service":
            raise ValueError(
                "tool-step authority requires an explicit service identity"
            )
        self._engine = engine
        self._actor = actor

    async def recover_selected(
        self, claim: JobClaim, inputs: ToolNodeInput
    ) -> tuple[RecoveredToolStep, ...] | None:
        """Bind selected children and terminal outputs from the parked parent.

        A planned observation deferral continues the logical round with its
        terminal failures. Ordinary retries carry authoritative results only.
        Recovery does not authorize use of their bytes or a new physical dispatch.
        The normal step entry point still verifies the exact invocation input.
        """
        env = inputs.envelope
        params = {
            **_scope(claim.scope, claim.execution_id),
            "parent_node_run_id": claim.node_run_id,
            "parent_attempt_id": env.attempt_id,
        }
        if (env.execution_id, env.node_run_id) != (
            claim.execution_id,
            claim.node_run_id,
        ):
            raise ValueError("recovery requires the exact parent claim")
        async with self._engine.begin() as connection:
            locked = await repo.lock_execution(
                connection, claim.scope, claim.execution_id
            )
            if (
                locked is None
                or locked.cancel_requested_at is not None
                or locked.execution.status is not ExecutionStatus.RUNNING
                or not _live(
                    await _read_job(connection, claim, internal=False),
                    claim,
                    env.attempt_id,
                    await _clock(connection),
                )
            ):
                return None
            parent = await repo.get_node(
                connection, claim.scope, claim.execution_id, claim.node_run_id
            )
            current = await repo.get_attempt(
                connection,
                claim.scope,
                claim.execution_id,
                claim.node_run_id,
                env.attempt_id,
            )
            if parent is None or current is None or not _parent_inputs(parent, inputs):
                raise ValueError("recovery requires the durable N05 input")
            parked_attempt_id = None
            if len(parent.attempt_history) >= 2:
                previous_id = parent.attempt_history[-2].attempt_ref.record_id
                previous = await repo.get_attempt(
                    connection,
                    claim.scope,
                    claim.execution_id,
                    claim.node_run_id,
                    previous_id,
                )
                if previous is not None and previous.observation_dependency_deferred:
                    parked_attempt_id = previous_id
            rows = (
                (
                    await connection.execute(
                        text(f"""
                    SELECT {_COMPLETED_COLUMNS}, b.parent_attempt_id AS published_parent_attempt_id
                    FROM {_COMPLETED_SOURCE}
                    LEFT JOIN jimu_workflow.tool_step_parent_outputs AS b
                        USING (project_id, client_id, execution_id, parent_node_run_id, step_id,
                            bound_input_digest, child_node_run_id, child_attempt_id, result_digest)
                    WHERE {_PARENT_PARAMS}
                        AND (b.parent_attempt_id = :parent_attempt_id
                            OR b.parent_attempt_id = :parked_attempt_id
                            OR c.attempt_status = 'succeeded')
                    ORDER BY c.step_id,
                        CASE WHEN b.parent_attempt_id = :parent_attempt_id THEN 0
                            WHEN b.parent_attempt_id = :parked_attempt_id THEN 1 ELSE 2 END
                """),
                        {**params, "parked_attempt_id": parked_attempt_id},
                    )
                )
                .mappings()
                .all()
            )
            grouped: dict[str, RowMapping] = {}
            for row in rows:
                grouped.setdefault(row["step_id"], row)
            recovered = []
            for row in grouped.values():
                bound, completion = _read_input(row), _read_completion(row)
                verified = await _check_bound_observation(
                    connection, claim.scope, parent, bound, parent_inputs=inputs
                )
                if row["published_parent_attempt_id"] != env.attempt_id:
                    original = await repo.get_attempt(
                        connection,
                        claim.scope,
                        claim.execution_id,
                        claim.node_run_id,
                        bound.inputs.envelope.attempt_id,
                    )
                    if (
                        original is None
                        or completion.selection is None
                        and (
                            parked_attempt_id is None
                            or row["published_parent_attempt_id"] != parked_attempt_id
                            or not original.observation_dependency_deferred
                        )
                        or original.ended_at is None
                        or original.attempt_number >= current.attempt_number
                    ):
                        raise ValueError(
                            "recovery requires a selected child or an earlier deferred attempt"
                        )
                    await _bind_completion(
                        connection,
                        {
                            **params,
                            "step_id": bound.step.step_id,
                            "bound_input_digest": bound.bound_digest(),
                        },
                        row,
                    )
                recovered.append(
                    RecoveredToolStep(
                        completion, not bound.has_required_ancestor_inputs(), verified
                    )
                )
            if recovered:
                await load_parent_step_history(connection, claim.scope, inputs)
            return tuple(recovered)

    async def begin(
        self,
        parent_claim: JobClaim,
        inputs: ToolNodeInput,
        step: ToolStep,
        input_digest: str,
        *,
        timeout_ms: int,
        max_attempts: int | None = None,
        ancestor_evidence: tuple[AncestorToolEvidence, ...] = (),
        ancestor_tools: tuple[tuple[ToolContentRef, ToolDefinition], ...] = (),
        guard_selectors: tuple[FactSelector, ...] = (),
    ) -> StepAttempt | StepCompletion | None:
        """Reuse the bound result or persist a new child identity before invocation."""
        timeout_ms = validated_milliseconds(timeout_ms)
        if max_attempts is not None and (
            type(max_attempts) is not int or not 1 <= max_attempts <= 1024
        ):
            raise ValueError("tool step attempt budget must be between 1 and 1024")
        definitions = dict(ancestor_tools)
        if any(
            proof.completion.result.execution.tool_ref not in definitions
            for proof in ancestor_evidence
        ):
            raise ValueError("ancestor proof requires its exact pinned Tool definition")
        bound = _StepInput(
            inputs=inputs,
            step=step,
            invocation_input_digest=_DIGEST.validate_python(input_digest),
            guard_selectors=guard_selectors,
            ancestor_inputs=tuple(
                _ancestor_pin(
                    step,
                    proof.input_path,
                    proof.completion,
                    proof.asset,
                    definition=definitions[proof.completion.result.execution.tool_ref],
                )
                for proof in ancestor_evidence
            ),
        )
        if not bound.has_required_ancestor_inputs():
            raise ValueError("new Tool execution requires exact ancestor evidence")
        env = inputs.envelope
        parameters = {
            **_scope(parent_claim.scope, parent_claim.execution_id),
            "parent_node_run_id": parent_claim.node_run_id,
            "parent_attempt_id": env.attempt_id,
            "step_id": step.step_id,
            "bound_input_digest": bound.bound_digest(),
        }
        async with self._engine.begin() as connection:
            locked = await repo.lock_execution(
                connection, parent_claim.scope, parent_claim.execution_id
            )
            if (
                locked is None
                or locked.cancel_requested_at is not None
                or locked.execution.status is not ExecutionStatus.RUNNING
            ):
                return None
            parent_job = await _read_job(connection, parent_claim, internal=False)
            now = await _clock(connection)
            if not _live(parent_job, parent_claim, env.attempt_id, now):
                return None
            assert (
                parent_job is not None
                and parent_job.lease_until is not None
                and parent_job.attempt_deadline is not None
            )
            parent = await repo.get_node(
                connection,
                parent_claim.scope,
                parent_claim.execution_id,
                parent_claim.node_run_id,
            )
            pins = locked.execution.pins
            if (
                parent is None
                or not _parent_inputs(parent, inputs, allow_supplement=True)
                or (env.task_id, env.release_id, env.bundle_digest)
                != (locked.execution.task_id, pins.release_id, pins.bundle_digest)
                or not any(pin.tool_ref == step.tool_ref for pin in pins.tool_pins)
            ):
                raise ValueError(
                    "step must be bound to the pinned N05 parent inputs and Tool implementation"
                )
            await _check_bound_observation(
                connection, parent_claim.scope, parent, bound
            )
            if bound.ancestor_inputs:
                history = await load_parent_step_history(
                    connection, parent_claim.scope, _parent_projection(parent, inputs)
                )
                _check_ancestor_history(
                    bound, () if history is None else history.entries
                )
                for proof in ancestor_evidence:
                    if history is None or not any(
                        (
                            entry.execution,
                            entry.selection,
                            entry.facts,
                            entry.evidence,
                            entry.invocations,
                        )
                        == (
                            proof.completion.result.execution,
                            proof.completion.selection,
                            proof.completion.result.facts,
                            proof.completion.result.evidence,
                            proof.completion.result.invocations,
                        )
                        for entry in history.entries
                    ):
                        raise ValueError(
                            "supplied Tool evidence differs from the durable selection"
                        )
            existing = (
                (
                    await connection.execute(
                        text(f"""
                        SELECT {_COMPLETED_COLUMNS} FROM {_COMPLETED_SOURCE}
                        JOIN jimu_workflow.tool_step_parent_outputs AS b
                            USING (project_id, client_id, execution_id, parent_node_run_id, step_id,
                                bound_input_digest, child_node_run_id, child_attempt_id, result_digest)
                        WHERE {_PARENT_PARAMS} AND c.step_id = :step_id
                            AND b.parent_attempt_id = :parent_attempt_id
                    """),
                        parameters,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if not _read_input(existing).matches_reuse(bound):
                    raise ValueError(
                        "current parent step input differs from its immutable result binding"
                    )
                return _read_completion(existing)
            active = await connection.execute(
                text(f"""
                    SELECT 1 FROM jimu_workflow.tool_step_attempts WHERE {_SCOPE}
                        AND parent_node_run_id = :parent_node_run_id AND parent_attempt_id = :parent_attempt_id
                        AND step_id = :step_id
                """),
                parameters,
            )
            if active.first() is not None:
                # A caller cannot recover a possibly dispatched child as a fresh call.
                return None
            reusable = (
                (
                    await connection.execute(
                        text(f"""
                        SELECT {_COMPLETED_COLUMNS} FROM {_COMPLETED_SOURCE}
                        WHERE {_PARENT_PARAMS} AND c.step_id = :step_id
                            AND c.attempt_status = 'succeeded'
                    """),
                        parameters,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if reusable is not None:
                original_input = _read_input(reusable)
                if not original_input.matches_reuse(bound):
                    raise ValueError(
                        "changing the inputs of an authoritative step requires an explicit new NodeRun"
                    )
                original = await repo.get_attempt(
                    connection,
                    parent_claim.scope,
                    parent_claim.execution_id,
                    parent_claim.node_run_id,
                    original_input.inputs.envelope.attempt_id,
                )
                current = await repo.get_attempt(
                    connection,
                    parent_claim.scope,
                    parent_claim.execution_id,
                    parent_claim.node_run_id,
                    env.attempt_id,
                )
                if (
                    original is None
                    or current is None
                    or original.ended_at is None
                    or original.attempt_number >= current.attempt_number
                ):
                    raise ValueError(
                        "reused tool step must come from an earlier terminal parent round"
                    )
                completion = _read_completion(reusable)
                await _bind_completion(connection, parameters, reusable)
                return completion
            count = (
                await connection.execute(
                    text(f"""
                        SELECT count(*) FROM jimu_workflow.tool_step_attempts WHERE {_SCOPE}
                            AND parent_node_run_id = :parent_node_run_id AND step_id = :step_id
                    """),
                    parameters,
                )
            ).scalar_one()
            if count >= (
                max_attempts if max_attempts is not None else parent_job.max_attempts
            ):
                raise StepAttemptBudgetExhausted(
                    "tool step attempt budget is exhausted"
                )
            previous = await self._previous_failure(connection, parameters)
            return await self._start_child(
                connection,
                parent_claim,
                parent,
                bound,
                parameters,
                now,
                parent_job.attempt_deadline,
                timeout_ms,
                previous,
            )

    async def _previous_failure(
        self,
        connection: AsyncConnection,
        parameters: dict[str, str],
    ) -> StepCompletion | None:
        row = (
            (
                await connection.execute(
                    text(f"""
                    SELECT {_COMPLETED_COLUMNS} FROM {_COMPLETED_SOURCE}
                    WHERE {_PARENT_PARAMS} AND c.bound_input_digest = :bound_input_digest
                        AND c.attempt_status IN ('failed', 'timed_out')
                        AND c.child_attempt_id = (
                            SELECT s.child_attempt_id
                            FROM jimu_workflow.tool_step_attempts AS s
                            JOIN jimu_workflow.attempts AS p
                                ON (s.project_id, s.client_id, s.execution_id,
                                    s.parent_node_run_id, s.parent_attempt_id)
                                = (p.project_id, p.client_id, p.execution_id, p.node_run_id, p.attempt_id)
                            WHERE (s.project_id, s.client_id, s.execution_id, s.parent_node_run_id, s.step_id)
                                = (:project_id, :client_id, :execution_id, :parent_node_run_id, :step_id)
                            ORDER BY p.attempt_number DESC LIMIT 1
                        )
                """),
                    parameters,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        _read_input(row)
        return _read_completion(row)

    async def _start_child(
        self,
        connection: AsyncConnection,
        parent_claim: JobClaim,
        parent: NodeRun,
        bound: _StepInput,
        parameters: dict[str, str],
        now: datetime,
        parent_deadline: datetime,
        timeout_ms: int,
        previous: StepCompletion | None,
    ) -> StepAttempt:
        scope, eid = parent_claim.scope, parent.execution_id
        deadline = min(parent_deadline, now + timedelta(milliseconds=timeout_ms))
        child_id, attempt_id, job_id = uuid4().hex, uuid4().hex, uuid4().hex
        child = NodeRun(
            schema_version="1.0.0",
            node_run_id=child_id,
            execution_id=eid,
            node_id=NodeId.N05,
            trace=parent.trace,
            config_ref=parent.config_ref,
            upstream_inputs=bound.inputs.envelope.upstream_artifacts,
            input_digest=bound.bound_digest(),
            status=NodeRunStatus.PENDING,
            created_at=now,
            attempt_history=(),
            output_refs=(),
        )
        await repo.insert_node(
            connection,
            scope,
            eid,
            child,
            branch_key=f"{TOOL_STEP_BRANCH_PREFIX}{child_id}",
        )
        ready = child.model_copy(update={"status": NodeRunStatus.READY})
        await repo.transition_node(
            connection,
            scope,
            eid,
            child,
            ready,
            now=now,
            guard=NodeTransitionGuard.PREREQUISITES_AVAILABLE,
        )
        values = {
            **parameters,
            "child_node_run_id": child_id,
            "child_attempt_id": attempt_id,
            "child_job_id": job_id,
            "lease_owner": parent_claim.owner,
            "lease_until": parent_deadline,
            "now": now,
            "timeout_ms": timeout_ms,
        }
        await connection.execute(
            text("""
                INSERT INTO jimu_workflow.jobs (project_id, client_id, execution_id, node_run_id, job_id,
                    internal_tool_step, status, fence, lease_owner, lease_until, ready_at, max_attempts,
                    timeout_ms, retry_backoff_ms, retryable_error_classes, retry_on_lease_loss)
                VALUES (:project_id, :client_id, :execution_id, :child_node_run_id, :child_job_id,
                    true, 'leased', 1, :lease_owner, :lease_until, :now, 1, :timeout_ms, '{}', '{}', false)
            """),
            values,
        )
        attempt = Attempt(
            schema_version="1.0.0",
            attempt_id=attempt_id,
            execution_id=eid,
            owner=NodeAttemptOwner(owner_kind="node", node_run_ref=_node_ref(child_id)),
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            created_at=now,
            started_at=now,
        )
        await repo.insert_attempt(
            connection,
            scope,
            eid,
            child_id,
            attempt,
            job_id=job_id,
            fence=1,
            lease_owner=parent_claim.owner,
        )
        running = child.model_copy(
            update={
                "status": NodeRunStatus.RUNNING,
                "started_at": now,
                "attempt_history": (
                    AttemptLink(attempt_ref=_attempt_ref(attempt_id), attempt_number=1),
                ),
            }
        )
        await repo.transition_node(
            connection,
            scope,
            eid,
            ready,
            running,
            now=now,
            guard=NodeTransitionGuard.FIRST_ATTEMPT_STARTED,
            appended_attempt=attempt,
        )
        await connection.execute(
            text(f"""
                UPDATE jimu_workflow.jobs SET current_attempt_id = :child_attempt_id,
                    attempt_count = 1, attempt_deadline = :deadline
                WHERE {_SCOPE} AND node_run_id = :child_node_run_id AND job_id = :child_job_id
            """),
            {**values, "deadline": deadline},
        )
        await connection.execute(
            text("""
                INSERT INTO jimu_workflow.tool_step_attempts
                    (project_id, client_id, execution_id, parent_node_run_id, parent_job_id,
                     parent_attempt_id, parent_fence, child_node_run_id, child_job_id, child_attempt_id, child_fence,
                     step_id, invocation_input_digest, bound_input_digest, record)
                VALUES (:project_id, :client_id, :execution_id, :parent_node_run_id, :parent_job_id,
                     :parent_attempt_id, :parent_fence, :child_node_run_id, :child_job_id, :child_attempt_id, 1,
                     :step_id, :invocation_input_digest, :bound_input_digest, CAST(:record AS json))
            """),
            {
                **values,
                "parent_job_id": parent_claim.job_id,
                "parent_fence": parent_claim.fence,
                "invocation_input_digest": bound.invocation_input_digest,
                "record": bound.stable_json(),
            },
        )
        claim = JobClaim(
            scope, eid, child_id, job_id, 1, parent_claim.owner, parent_deadline
        )
        return StepAttempt(
            parent_claim,
            bound.inputs.envelope.attempt_id,
            claim,
            running,
            attempt,
            _child_inputs(bound.inputs, child_id, attempt_id),
            previous,
        )

    async def _lock_step(
        self,
        connection: AsyncConnection,
        handle: StepAttempt,
        *,
        lock: bool = True,
        timeout_result: bool = False,
    ) -> tuple[_Job, _StepInput, datetime] | None:
        parent, child = handle.parent_claim, handle.claim
        if (parent.scope, parent.execution_id) != (child.scope, child.execution_id):
            return None
        read_execution = repo.lock_execution if lock else repo.get_execution
        locked = await read_execution(connection, parent.scope, parent.execution_id)
        if (
            locked is None
            or locked.cancel_requested_at is not None
            or locked.execution.status is not ExecutionStatus.RUNNING
        ):
            return None
        parent_job = await _read_job(connection, parent, internal=False, lock=lock)
        child_job = await _read_job(connection, child, internal=True, lock=lock)
        row = (
            (
                await connection.execute(
                    text(f"""
                    SELECT *, parent_attempt_id AS original_parent_attempt_id, record::text AS bound_input
                    FROM jimu_workflow.tool_step_attempts WHERE {_SCOPE}
                        AND parent_node_run_id = :parent_node_run_id AND parent_job_id = :parent_job_id
                        AND parent_attempt_id = :parent_attempt_id AND parent_fence = :parent_fence
                        AND child_node_run_id = :child_node_run_id AND child_job_id = :child_job_id
                        AND child_attempt_id = :child_attempt_id AND child_fence = :child_fence
                """),
                    {
                        **_scope(parent.scope, parent.execution_id),
                        "parent_node_run_id": parent.node_run_id,
                        "parent_job_id": parent.job_id,
                        "parent_attempt_id": handle.parent_attempt_id,
                        "parent_fence": parent.fence,
                        "child_node_run_id": child.node_run_id,
                        "child_job_id": child.job_id,
                        "child_attempt_id": handle.attempt.attempt_id,
                        "child_fence": child.fence,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        now = await _clock(connection)
        if (
            row is None
            or child_job is None
            or not _live(
                child_job,
                child,
                handle.attempt.attempt_id,
                now,
                allow_elapsed_deadline=timeout_result,
            )
            or not _live(parent_job, parent, handle.parent_attempt_id, now)
        ):
            return None
        bound = _read_input(row)
        node = await repo.get_node(
            connection, child.scope, child.execution_id, child.node_run_id
        )
        attempt = await repo.get_attempt(
            connection,
            child.scope,
            child.execution_id,
            child.node_run_id,
            handle.attempt.attempt_id,
        )
        if (
            node != handle.node
            or attempt != handle.attempt
            or node is None
            or node.input_digest != bound.bound_digest()
            or handle.inputs
            != _child_inputs(bound.inputs, child.node_run_id, handle.attempt.attempt_id)
        ):
            raise ValueError(
                "supplied tool step handle differs from durable inputs or child ownership"
            )
        return child_job, bound, now

    async def dispatch_live(
        self, connection: AsyncConnection, handle: StepAttempt
    ) -> bool:
        """Check both fences inside the journal's existing dispatch transaction."""
        return await self._lock_step(connection, handle, lock=False) is not None

    async def nested_retry_history(
        self,
        handle: StepAttempt,
    ) -> NestedToolRetryHistory | None:
        """Read provenance while both fences hold; ambiguous earlier steps fail closed."""
        claim, parent = handle.claim, handle.parent_claim
        scope, eid = claim.scope, claim.execution_id
        async with self._engine.begin() as connection:
            current = await self._lock_step(connection, handle)
            if current is None:
                return None
            _, bound, _ = current
            parent_attempt = await repo.get_attempt(
                connection,
                scope,
                eid,
                parent.node_run_id,
                handle.parent_attempt_id,
            )
            if (
                parent_attempt is None
                or parent_attempt.status is not AttemptStatus.RUNNING
            ):
                return None
            rows = (
                (
                    await connection.execute(
                        text("""
                        SELECT s.*, s.parent_attempt_id AS original_parent_attempt_id,
                            s.record::text AS bound_input,
                            c.attempt_status, c.tool_execution_id, c.result_digest,
                            c.record::text AS completion
                        FROM jimu_workflow.tool_step_attempts AS s
                        JOIN jimu_workflow.attempts AS p
                            ON (s.project_id, s.client_id, s.execution_id,
                                s.parent_node_run_id, s.parent_attempt_id)
                            = (p.project_id, p.client_id, p.execution_id, p.node_run_id, p.attempt_id)
                        LEFT JOIN jimu_workflow.tool_step_completions AS c
                            ON (c.project_id, c.client_id, c.execution_id, c.child_node_run_id, c.child_attempt_id)
                            = (s.project_id, s.client_id, s.execution_id, s.child_node_run_id, s.child_attempt_id)
                        WHERE (s.project_id, s.client_id, s.execution_id, s.parent_node_run_id, s.step_id)
                            = (:project_id, :client_id, :execution_id, :parent_node_run_id, :step_id)
                            AND p.attempt_number < :ordinal
                        ORDER BY p.attempt_number
                    """),
                        {
                            **_scope(scope, eid),
                            "parent_node_run_id": parent.node_run_id,
                            "step_id": bound.step.step_id,
                            "ordinal": parent_attempt.attempt_number,
                        },
                    )
                )
                .mappings()
                .all()
            )
            priors = []
            for row in rows:
                if (
                    row["completion"] is None
                    or _read_input(row).bound_digest() != bound.bound_digest()
                ):
                    return None
                completion = _read_completion(row)
                old_parent = await repo.get_attempt(
                    connection,
                    scope,
                    eid,
                    parent.node_run_id,
                    row["original_parent_attempt_id"],
                )
                cid, aid = row["child_node_run_id"], row["child_attempt_id"]
                child = await repo.get_node(connection, scope, eid, cid)
                attempt = await repo.get_attempt(connection, scope, eid, cid, aid)
                if (
                    old_parent is None
                    or old_parent.status
                    not in (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT)
                    or child is None
                    or child.node_id is not NodeId.N05
                    or child.status is not NodeRunStatus.FAILED
                    or child.input_digest != bound.bound_digest()
                    or child.upstream_inputs != bound.inputs.envelope.upstream_artifacts
                    or attempt is None
                    or attempt.status
                    not in (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT)
                    or attempt.status.value != completion.attempt_status
                ):
                    return None
                await _verify_child_artifacts(
                    connection, scope, child, attempt, completion
                )
                priors.append(
                    NestedToolAttemptLink(
                        old_parent, child, attempt, completion.result.invocations
                    )
                )
            identity = NestedToolStepIdentity(
                eid,
                _node_ref(parent.node_run_id),
                bound.inputs.tool_plan_ref,
                bound.step.step_id,
                bound.step.tool_ref,
                bound.inputs.object_record.profile_ref,
                ObjectRef(
                    record_kind=RuntimeRecordKind.OBJECT,
                    record_id=bound.inputs.object_record.object_id,
                ),
                bound.bound_digest(),
            )
            if await self._lock_step(connection, handle, lock=False) is None:
                return None
            return NestedToolRetryHistory(
                identity,
                NestedToolAttemptLink(parent_attempt, handle.node, handle.attempt, ()),
                tuple(priors),
            )

    async def complete(
        self, handle: StepAttempt, result: StepResult
    ) -> StepCompletion | None:
        """Commit an original whole-step result and active parent binding atomically."""
        claim = handle.claim
        async with self._engine.begin() as connection:
            timeout_result = _reports_timeout(result.execution)
            checked = await self._lock_step(
                connection, handle, timeout_result=timeout_result
            )
            if checked is None:
                return None
            _, bound, now = checked
            if bound.ancestor_inputs:
                parent = await repo.get_node(
                    connection,
                    claim.scope,
                    claim.execution_id,
                    handle.parent_claim.node_run_id,
                )
                if parent is None:
                    return None
                history = await load_parent_step_history(
                    connection, claim.scope, _parent_projection(parent, bound.inputs)
                )
                _check_ancestor_history(
                    bound, () if history is None else history.entries
                )
            result = StepResult.model_validate_json(result.stable_json())
            tool = result.execution
            observation = tool.authority_observation
            if (
                observation is not None
                and observation.arrival_qualification == "late"
                and not timeout_result
            ):
                return None
            state = await repo.get_execution(
                connection, claim.scope, claim.execution_id
            )
            assert state is not None
            version = next(
                pin.implementation_version
                for pin in state.execution.pins.tool_pins
                if pin.tool_ref == bound.step.tool_ref
            )
            if tool.completed_at > now:
                raise ValueError(
                    "tool result completion follows its durable commit clock"
                )
            # Recheck the two fences after parsing. Only a normalized timeout
            # may close a still-owned child after its own deadline.
            checked = await self._lock_step(
                connection, handle, lock=False, timeout_result=timeout_result
            )
            if checked is None:
                return None
            job, _, now = checked
            assert job.attempt_deadline is not None
            timed_out = now >= job.attempt_deadline
            if observation is not None and observation.arrival_qualification != "late":
                # An elapsed fence can witness a timeout, but cannot prove when
                # the physical response arrived.
                arrival = "unresolved" if timed_out else "on_time"
                if observation.arrival_qualification != arrival:
                    tool = tool.model_copy(
                        update={
                            "authority_observation": observation.model_copy(
                                update={"arrival_qualification": arrival}
                            )
                        }
                    )
                    result = result.model_copy(update={"execution": tool})
            _check_result(
                bound, result, handle.node, handle.attempt, version, claim.scope
            )
            won = tool.status == "succeeded"
            selection = None
            if won:
                if tool.object_ref is None:
                    raise ValueError("successful tool step requires its source Object")
                selection = ToolStepSelection(
                    schema_version="1.0.0",
                    selection_group="tool_step",
                    scope=FactScope.OBJECT,
                    selection_id=uuid4().hex,
                    execution_id=tool.execution_id,
                    profile_ref=tool.profile_ref,
                    object_id=tool.object_ref.record_id,
                    selected_attempt_id=handle.attempt.attempt_id,
                    selected_fact_refs=tool.generated_fact_refs,
                    reason="validated_success",
                    authority_revision=1,
                    actor_ref=self._actor,
                    created_at=now,
                    tool_plan_ref=bound.inputs.tool_plan_ref,
                    step_id=bound.step.step_id,
                    selected_result_ref=_tool_ref(tool),
                )
            completion = StepCompletion(
                result=result,
                attempt_status="succeeded"
                if won
                else "timed_out"
                if timed_out
                else "failed",
                selection=selection,
            )
            status = AttemptStatus(completion.attempt_status)
            guard = {
                AttemptStatus.SUCCEEDED: AttemptTransitionGuard.RESPONSE_VALIDATED_WITH_COMMIT_RIGHTS,
                AttemptStatus.TIMED_OUT: AttemptTransitionGuard.DEADLINE_ELAPSED,
                AttemptStatus.FAILED: AttemptTransitionGuard.NORMALIZED_FAILURE,
            }[status]
            reason = (
                None
                if won
                else LifecycleReason(
                    code="tool_deadline_elapsed"
                    if timed_out
                    else "tool_step_unsuccessful"
                )
            )
            await _finish_attempt(
                connection,
                claim.scope,
                handle.node,
                handle.attempt,
                status,
                guard,
                now,
                reason=reason,
            )
            following = handle.node.model_copy(
                update={
                    "status": NodeRunStatus.SUCCEEDED if won else NodeRunStatus.FAILED,
                    "ended_at": now,
                    "reason": reason,
                    "output_refs": tuple(
                        ref for ref, _ in _produced_records(completion)
                    ),
                }
            )
            await repo.transition_node(
                connection,
                claim.scope,
                claim.execution_id,
                handle.node,
                following,
                now=now,
                guard=NodeTransitionGuard.AUTHORITATIVE_OUTPUT_VALIDATED
                if won
                else NodeTransitionGuard.NONRETRYABLE_FAILURE,
            )
            await _write_job(
                connection, claim.scope, _release_job(job, _JobStatus.DONE, now)
            )
            await self._save_completion(connection, handle, bound, completion)
            return completion

    async def _save_completion(
        self,
        connection: AsyncConnection,
        handle: StepAttempt,
        bound: _StepInput,
        completion: StepCompletion,
    ) -> None:
        claim = handle.claim
        for ref, record in _produced_records(completion):
            await repo.store_output(
                connection,
                claim.scope,
                claim.execution_id,
                claim.node_run_id,
                handle.attempt.attempt_id,
                output_key=_output_key(ref),
                output_kind=ref.record_kind.value,
                record=record,
            )
        parameters = {
            **_scope(claim.scope, claim.execution_id),
            "parent_node_run_id": handle.parent_claim.node_run_id,
            "parent_attempt_id": handle.parent_attempt_id,
            "child_node_run_id": claim.node_run_id,
            "child_attempt_id": handle.attempt.attempt_id,
            "step_id": bound.step.step_id,
            "bound_input_digest": bound.bound_digest(),
            "attempt_status": completion.attempt_status,
            "tool_execution_id": completion.result.execution.tool_execution_id,
            "result_digest": _digest(completion),
            "record": completion.stable_json(),
        }
        inserted = await connection.execute(
            text("""
                INSERT INTO jimu_workflow.tool_step_completions
                    (project_id, client_id, execution_id, child_node_run_id, child_attempt_id,
                     parent_node_run_id, step_id, bound_input_digest, attempt_status, tool_execution_id, result_digest, record)
                VALUES (:project_id, :client_id, :execution_id, :child_node_run_id, :child_attempt_id,
                     :parent_node_run_id, :step_id, :bound_input_digest, :attempt_status, :tool_execution_id, :result_digest,
                     CAST(:record AS json))
                RETURNING child_node_run_id, child_attempt_id, result_digest
            """),
            parameters,
        )
        await _bind_completion(connection, parameters, inserted.mappings().one())


def _reports_timeout(tool: ToolExecution) -> bool:
    outcome = tool.attempt_result
    if isinstance(outcome, (FailedToolAttempt, UndispatchedToolAttempt)):
        return outcome.error.error_class == "timeout"
    return (
        isinstance(outcome, ReturnedToolAttempt)
        and isinstance(outcome.result, FailedToolResult)
        and outcome.result.error.error_class == "timeout"
    )


def _tool_ref(tool: ToolExecution) -> ToolExecutionRef:
    return ToolExecutionRef(
        record_kind=RuntimeRecordKind.TOOL_EXECUTION, record_id=tool.tool_execution_id
    )


def _output_key(ref: RuntimeRecordRef) -> str:
    return f"{ref.record_kind.value}.{ref.record_id}"


def _produced_records(
    completion: StepCompletion,
) -> tuple[tuple[RuntimeRecordRef, ContractModel], ...]:
    result = completion.result
    records: list[tuple[RuntimeRecordRef, ContractModel]] = [
        (_tool_ref(result.execution), result.execution)
    ]
    records.extend(
        (
            RuntimeRecordRef(
                record_kind=RuntimeRecordKind.FACT, record_id=item.fact_id
            ),
            item,
        )
        for item in result.facts
    )
    records.extend(
        (
            RuntimeRecordRef(
                record_kind=RuntimeRecordKind.EVIDENCE, record_id=item.evidence_id
            ),
            item,
        )
        for item in result.evidence
    )
    records.extend(
        (
            RuntimeRecordRef(
                record_kind=RuntimeRecordKind.MODEL_INVOCATION,
                record_id=item.model_invocation_id,
            ),
            item,
        )
        for item in result.invocations
    )
    if completion.selection is not None:
        records.append(
            (
                RuntimeRecordRef(
                    record_kind=RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION,
                    record_id=completion.selection.selection_id,
                ),
                completion.selection,
            )
        )
    return tuple(records)


async def _verify_child_artifacts(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    child: NodeRun,
    attempt: Attempt,
    completion: StepCompletion,
) -> None:
    produced = _produced_records(completion)
    if tuple(ref.stable_json() for ref in child.output_refs) != tuple(
        ref.stable_json() for ref, _ in produced
    ):
        raise ValueError(
            "step completion differs from its durable child output manifest"
        )
    for ref, record in produced:
        stored = await repo.read_output(
            connection,
            scope,
            child.execution_id,
            child.node_run_id,
            attempt.attempt_id,
            _output_key(ref),
            type(record),
        )
        if stored != record:
            raise ValueError(
                "whole-step completion differs from its child-owned durable artifacts"
            )


_TOOL_INPUT_KINDS = {
    RuntimeRecordKind.CONTEXT_SNAPSHOT,
    RuntimeRecordKind.EVIDENCE,
    RuntimeRecordKind.FACT,
    RuntimeRecordKind.MODEL_INVOCATION,
    RuntimeRecordKind.OBJECT,
    RuntimeRecordKind.TOOL_EXECUTION,
}


def _check_result(
    bound: _StepInput,
    result: StepResult,
    child: NodeRun,
    attempt: Attempt,
    implementation_version: str,
    scope: IdempotencyScope,
) -> None:
    tool, origin = result.execution, result.execution.invocation_origin
    inputs = bound.inputs
    env, obj = inputs.envelope, inputs.object_record
    expected_inputs = {
        ToolArtifactRecordRef.model_validate(pin.ref.model_dump()): pin.digest
        for pin in env.upstream_artifacts
        if pin.ref.record_kind in _TOOL_INPUT_KINDS
    }
    for pin in bound.ancestor_inputs:
        key, digest = pin.evidence.artifact_ref, pin.evidence.digest
        if expected_inputs.setdefault(key, digest) != digest:
            raise ValueError("ancestor evidence conflicts with the parent input pin")
    if (
        not isinstance(origin, ToolPlanStepOrigin)
        or (origin.tool_plan_ref, origin.step_id, tool.tool_ref)
        != (inputs.tool_plan_ref, bound.step.step_id, bound.step.tool_ref)
        or (
            tool.execution_id,
            tool.profile_ref,
            tool.object_ref,
            tool.trace_id,
            tool.node_run_ref.record_id,
            tool.attempt_ref.record_id,
        )
        != (
            env.execution_id,
            obj.profile_ref,
            ObjectRef(record_kind=RuntimeRecordKind.OBJECT, record_id=obj.object_id),
            env.trace.trace_id,
            child.node_run_id,
            attempt.attempt_id,
        )
        or attempt.started_at is None
        or tool.started_at < attempt.started_at
        or tool.input_refs
        != tuple(
            ToolArtifactRef(artifact_ref=key, digest=digest)
            for key, digest in expected_inputs.items()
        )
        or any(
            item.access_scope.project_id != scope.project_id
            or isinstance(item.access_scope, TaskEvidenceAccess)
            and item.access_scope.task_id != env.task_id
            for item in result.evidence
        )
        or tool.actual_implementation_version is not None
        and tool.actual_implementation_version != implementation_version
    ):
        raise ValueError(
            "tool result differs from its immutable scope, configuration or child owner"
        )
    qualification = tool.authority_observation
    if tool.status == "succeeded" and (
        qualification is None
        or (
            qualification.arrival_qualification,
            qualification.envelope_validation,
            qualification.association_validation,
        )
        != ("on_time", "trusted", "trusted")
        or tool.actual_implementation_version != implementation_version
    ):
        raise ValueError(
            "successful tool step requires a trusted on-time result from the pinned implementation"
        )
    mapped = {
        mapping.output_name: mapping.target_fact_key
        for mapping in bound.step.fact_output_mappings
    }
    names = set()
    for fact in result.facts:
        source = fact.source
        if (
            not isinstance(source, ToolFactSource)
            or (
                source.tool_execution_ref,
                source.tool_plan_ref,
                source.step_id,
                source.tool_ref,
            )
            != (_tool_ref(tool), origin.tool_plan_ref, origin.step_id, tool.tool_ref)
            or (
                fact.execution_id,
                fact.object_id,
                fact.profile_ref,
                fact.attempt_id,
                fact.trace_id,
            )
            != (
                tool.execution_id,
                obj.object_id,
                tool.profile_ref,
                attempt.attempt_id,
                tool.trace_id,
            )
            or fact.producer_declaration_id not in origin.producer_declaration_ids
            or mapped.get(source.output_name) != fact.fact_key
            or source.output_name in names
        ):
            raise ValueError(
                "tool Fact does not resolve to the exact original step producer and mapping"
            )
        names.add(source.output_name)
    if tool.status == "succeeded" and (
        qualification is None
        or {output.output_name for output in qualification.mapped_outputs}
        != mapped.keys()
    ):
        raise ValueError(
            "successful tool step requires the complete configured output group"
        )


async def load_child_nodes(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
) -> tuple[NodeRun, ...]:
    rows = await connection.execute(
        text(f"""
            SELECT child_node_run_id FROM jimu_workflow.tool_step_attempts WHERE {_SCOPE}
            ORDER BY child_node_run_id
        """),
        _scope(scope, execution_id),
    )
    result = []
    for identity in rows.scalars():
        node = await repo.get_node(connection, scope, execution_id, identity)
        if node is None or node.node_id is not NodeId.N05:
            raise ValueError("tool step ledger requires a durable N05 child NodeRun")
        result.append(node)
    return tuple(result)


async def load_parent_step_history(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    inputs: ToolNodeInput,
) -> ToolStepHistory | None:
    """Resolve a parent's explicit child results or use the legacy same-round path."""
    env = inputs.envelope
    parameters = {
        **_scope(scope, env.execution_id),
        "parent_node_run_id": env.node_run_id,
        "parent_attempt_id": env.attempt_id,
    }
    bound_count = (
        await connection.execute(
            text(f"""
                SELECT count(*) FROM jimu_workflow.tool_step_attempts WHERE {_SCOPE}
                    AND parent_node_run_id = :parent_node_run_id AND parent_attempt_id = :parent_attempt_id
            """),
            parameters,
        )
    ).scalar_one()
    rows = (
        (
            await connection.execute(
                text(f"""
                SELECT {_COMPLETED_COLUMNS} FROM {_COMPLETED_SOURCE}
                JOIN jimu_workflow.tool_step_parent_outputs AS b
                    USING (project_id, client_id, execution_id, parent_node_run_id, step_id,
                        bound_input_digest, child_node_run_id, child_attempt_id, result_digest)
                WHERE {_PARENT_PARAMS} AND b.parent_attempt_id = :parent_attempt_id
                ORDER BY c.step_id
            """),
                parameters,
            )
        )
        .mappings()
        .all()
    )
    if not rows and not bound_count:
        return None
    parent = await repo.get_node(connection, scope, env.execution_id, env.node_run_id)
    current = await repo.get_attempt(
        connection, scope, env.execution_id, env.node_run_id, env.attempt_id
    )
    locked = await repo.get_execution(connection, scope, env.execution_id)
    if (
        parent is None
        or current is None
        or locked is None
        or not _parent_inputs(parent, inputs)
    ):
        raise ValueError(
            "tool step history requires the exact persisted parent invocation"
        )
    version_by_tool = {
        pin.tool_ref: pin.implementation_version
        for pin in locked.execution.pins.tool_pins
    }
    entries = []
    bounds = []
    for row in rows:
        bound, completion = _read_input(row), _read_completion(row)
        verified = await _check_bound_observation(
            connection, scope, parent, bound, parent_inputs=inputs
        )
        original = await repo.get_attempt(
            connection,
            scope,
            env.execution_id,
            env.node_run_id,
            bound.inputs.envelope.attempt_id,
        )
        cid, aid = row["child_node_run_id"], row["child_attempt_id"]
        child = await repo.get_node(connection, scope, env.execution_id, cid)
        attempt = await repo.get_attempt(connection, scope, env.execution_id, cid, aid)
        version = version_by_tool.get(bound.step.tool_ref)
        if (
            original is None
            or child is None
            or attempt is None
            or version is None
            or child.input_digest != bound.bound_digest()
            or child.upstream_inputs != bound.inputs.envelope.upstream_artifacts
            or attempt.status.value != completion.attempt_status
        ):
            raise ValueError(
                "tool step history references missing owners, pins, or exact input"
            )
        _check_result(bound, completion.result, child, attempt, version, scope)
        await _verify_child_artifacts(connection, scope, child, attempt, completion)
        entries.append(
            ToolStepHistoryEntry(
                original,
                child,
                attempt,
                completion.result.execution,
                completion.result.facts,
                completion.selection,
                completion.result.evidence,
                completion.result.invocations,
                verified,
            )
        )
        bounds.append(bound)
    for bound in bounds:
        if bound.ancestor_inputs:
            _check_ancestor_history(bound, tuple(entries))
    return ToolStepHistory(inputs, parent, current, tuple(entries))
