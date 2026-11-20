"""Rebuild workflow inputs and planner records from scoped PostgreSQL history."""

from dataclasses import dataclass, field

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncConnection

from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus
from jimu_runtime.domain.nodes.discovery import SkippedDiscoveryOutput
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput, NodeOutputEnvelope
from jimu_runtime.domain.policies import ToolSkipPolicyArtifact
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    NodeId,
    NodeRun,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import NodeRunRef, ObjectRef
from jimu_runtime.domain.runtime.intake import ContextSnapshot, IdempotencyScope
from jimu_runtime.domain.validation.coverage_sources import (
    CoverageSources,
    ObjectDecisionBlocker,
)
from jimu_runtime.replay.inference_repository import (
    StoredInferenceReplay,
    read_inference_replay_for_target,
)
from jimu_runtime.tools.step_attempts import load_child_nodes
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.engine import BranchKey, BranchRun, PlannerState
from jimu_runtime.workflow.jobs import NODE_OUTPUT_KEY
from jimu_runtime.workflow.mock_support import K, record_digest
from jimu_runtime.workflow.worker_nodes import BranchInputs, ObservationRetryWitness
from jimu_runtime.workflow.worker_records import (
    GATE_KEY,
    MESSAGE_KEY,
    MESSAGE_NODES,
    NODE_MESSAGE,
    SKIP_KEY,
    ContextMessage,
    DecisionMessage,
    DiscoveryMessage,
    EligibilityMessage,
    GroundingMessage,
    NodeMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_source import OpenExecution

_OUTPUT = TypeAdapter[NodeOutputEnvelope](NodeOutputEnvelope)


def branch_name(key: BranchKey) -> str:
    return "global" if key.object_id is None else f"object:{key.object_id}"


def _branch(encoded: str, node: NodeRun) -> BranchKey:
    if encoded == "global":
        return BranchKey(node.node_id)
    prefix = "object:"
    if not encoded.startswith(prefix) or not encoded[len(prefix) :]:
        raise ValueError("invalid persisted workflow branch name")
    return BranchKey(node.node_id, encoded[len(prefix) :])


@dataclass(slots=True)
class WorkerRecords:
    locked: repo.LockedExecution
    runs: dict[BranchKey, NodeRun] = field(default_factory=dict)
    messages: dict[BranchKey, NodeMessage] = field(default_factory=dict)
    tool_step_nodes: tuple[NodeRun, ...] = ()
    gate: EligibilityMessage | None = None
    skipped_discovery: SkippedDiscoveryOutput | None = None
    observation_retries: dict[str, ObservationRetryWitness] = field(
        default_factory=dict
    )

    def inputs(self, object_id: str | None = None) -> BranchInputs:
        context = self.messages.get(BranchKey(NodeId.N01))
        discovery = self.messages.get(BranchKey(NodeId.N02))
        grounding = self.messages.get(BranchKey(NodeId.N03))
        observation = self.messages.get(BranchKey(NodeId.N04, object_id))
        tool = self.messages.get(BranchKey(NodeId.N05, object_id))
        return BranchInputs(
            context if isinstance(context, ContextMessage) else None,
            self.gate,
            discovery if isinstance(discovery, DiscoveryMessage) else None,
            grounding if isinstance(grounding, GroundingMessage) else None,
            observation if isinstance(observation, ObservationMessage) else None,
            tool if isinstance(tool, ToolMessage) else None,
        )

    def planner_state(
        self, opened: OpenExecution, *, configured: bool = False
    ) -> PlannerState:
        inputs = self.inputs()
        tool_plans = (
            tuple(
                (ref, opened.scope.resolve(ref, ToolPlanDefinition))
                for ref in dict.fromkeys(
                    profile.definition.tool_plan_ref for profile in opened.profiles
                )
            )
            if configured
            else ()
        )
        skip_policies = tuple(
            (ref, opened.scope.resolve(ref, ToolSkipPolicyArtifact).policy)
            for ref in dict.fromkeys(
                step.skip_policy_ref for _, plan in tool_plans for step in plan.steps
            )
        )
        return PlannerState(
            self.locked.execution,
            opened.scope.pins.bundle_digest,
            opened.profiles,
            tuple(BranchRun(key, run) for key, run in self.runs.items()),
            None if inputs.context is None else inputs.context.output,
            None if self.gate is None else self.gate.evaluation,
            self.skipped_discovery
            if inputs.discovery is None
            else inputs.discovery.output,
            None if inputs.grounding is None else inputs.grounding.output,
            () if inputs.grounding is None else inputs.grounding.objects,
            tuple(
                item.draft
                for item in self.messages.values()
                if isinstance(item, DecisionMessage)
            ),
            grounding_gaps=()
            if inputs.grounding is None
            else inputs.grounding.undispatched_profiles,
            tool_plans=tool_plans,
            tool_skip_policies=skip_policies,
        )

    def coverage_sources(self, attempts: tuple[Attempt, ...]) -> CoverageSources:
        previous = self.inputs()
        context, discovery, grounding = (
            previous.context,
            previous.discovery,
            previous.grounding,
        )
        blockers = []
        for key, run in self.runs.items():
            if (
                key.node_id is NodeId.N06
                and key.object_id is not None
                and run.status
                in (
                    NodeRunStatus.BLOCKED,
                    NodeRunStatus.FAILED,
                    NodeRunStatus.CANCELLED,
                )
            ):
                blockers.append(
                    ObjectDecisionBlocker(
                        object_ref=ObjectRef(
                            record_kind=K.OBJECT, record_id=key.object_id
                        ),
                        node_run_ref=NodeRunRef(
                            record_kind=K.NODE_RUN, record_id=run.node_run_id
                        ),
                        attempt_ref=None
                        if not run.attempt_history
                        else run.attempt_history[-1].attempt_ref,
                    )
                )
        return CoverageSources(
            execution=self.locked.execution,
            context_output=None if context is None else context.output,
            context_snapshot=None if context is None else context.snapshot,
            discovery_output=self.skipped_discovery
            if discovery is None
            else discovery.output,
            discovery_input=None if discovery is None else discovery.inputs,
            question_selections=() if discovery is None else discovery.selections,
            grounding_records=() if grounding is None else grounding.records,
            objects=() if grounding is None else grounding.objects,
            node_runs=(*self.runs.values(), *self.tool_step_nodes),
            attempts=attempts,
            decision_blockers=tuple(blockers),
        )


async def _node_message(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    state: repo.LockedExecution,
    node: NodeRun,
) -> NodeMessage | None:
    if not node.attempt_history or node.status not in (
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.FAILED,
    ):
        return None
    execution = state.execution
    eid, nid = execution.execution_id, node.node_run_id
    aid = node.attempt_history[-1].attempt_ref.record_id
    if node.status is NodeRunStatus.SUCCEEDED:
        output = await repo.read_authority(connection, scope, eid, nid, _OUTPUT)
        if output is None:
            raise ValueError("successful node requires its authoritative output")
    else:
        output = await repo.read_output(
            connection, scope, eid, nid, aid, NODE_OUTPUT_KEY, _OUTPUT
        )
        if output is None:
            return None
    message = await repo.read_output(
        connection, scope, eid, nid, aid, MESSAGE_KEY, NODE_MESSAGE
    )
    if (
        message is None
        and isinstance(output, FailedNodeOutput)
        and output.error.code.startswith("worker_provider_")
        and not output.output_artifacts
    ):
        return None
    if message is None or not isinstance(message, MESSAGE_NODES[node.node_id]):
        raise ValueError("node output requires a matching typed workflow message")
    env = message.input_envelope()
    if (
        output != message.output_envelope()
        or (output.node_run_id, output.attempt_id, output.node_id, output.status)
        != (nid, aid, node.node_id, node.status)
        or (
            env.task_id,
            env.execution_id,
            env.release_id,
            env.bundle_digest,
            reference_identity(env.node_config_ref),
            env.trace,
        )
        != (
            execution.task_id,
            eid,
            execution.pins.release_id,
            execution.pins.bundle_digest,
            reference_identity(node.config_ref),
            node.trace,
        )
        or node.node_id is not NodeId.N01
        and tuple(pin.stable_json() for pin in env.upstream_artifacts)
        != tuple(pin.stable_json() for pin in node.upstream_inputs)
    ):
        raise ValueError(
            "workflow message does not match the exact selected execution, attempt and inputs"
        )
    if (
        isinstance(message, ToolMessage)
        and node.status is NodeRunStatus.SUCCEEDED
        and not message.executions
    ):
        if not node.output_refs or node.output_refs != tuple(
            pin.ref for pin in output.output_artifacts
        ):
            raise ValueError(
                "successful ToolPlan requires its selected output references"
            )
        completion = message.completion_artifact
        if completion is None:
            attempt = await repo.get_attempt(connection, scope, eid, nid, aid)
            if (
                attempt is None
                or attempt.status is not AttemptStatus.SUCCEEDED
                or not attempt.belongs_to_node(node)
            ):
                raise ValueError(
                    "historical ToolPlan success requires its successful Attempt"
                )
        else:
            stored = await repo.read_output(
                connection,
                scope,
                eid,
                nid,
                aid,
                f"{completion.ref.record_kind.value}.{completion.ref.record_id}",
                type(completion.record),
            )
            if stored != completion.record:
                raise ValueError(
                    "selected ToolPlan completion requires its durable summary"
                )
    return message


async def read_observation_for_tools(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution: repo.LockedExecution,
    object_pin: PinnedRuntimeInput,
    node_run_id: str,
) -> tuple[bool, ObservationMessage | None]:
    node = await repo.get_node(
        connection, scope, execution.execution.execution_id, node_run_id
    )
    if (
        node is None
        or node.node_id is not NodeId.N04
        or object_pin.ref.record_kind is not K.OBJECT
        or object_pin not in node.upstream_inputs
    ):
        raise ValueError("N05 needs the same Object's N04 branch")
    if node.status in (
        NodeRunStatus.PENDING,
        NodeRunStatus.READY,
        NodeRunStatus.RUNNING,
    ):
        return False, None
    message = await _node_message(connection, scope, execution, node)
    if not isinstance(message, ObservationMessage):
        return True, None
    observed_object = message.inputs.object_record
    if (
        observed_object.object_id != object_pin.ref.record_id
        or record_digest(observed_object) != object_pin.digest
    ):
        raise ValueError("N04 observation must retain the exact N05 Object pin")
    return True, message


async def read_context_message(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
) -> ContextMessage | None:
    """Read the N01 output selected by the Execution's immutable snapshot pin."""
    pin = locked.execution.context_snapshot
    if pin is None:
        return None
    for name, node in await repo.list_branches(
        connection, scope, locked.execution.execution_id
    ):
        if name != "global" or node.node_id is not NodeId.N01:
            continue
        message = await _node_message(connection, scope, locked, node)
        if not isinstance(message, ContextMessage) or message.snapshot is None:
            break
        snapshot = message.snapshot
        if (
            snapshot.context_snapshot_id != pin.snapshot_ref.record_id
            or record_digest(snapshot) != pin.snapshot_digest
        ):
            raise ValueError("selected ContextSnapshot differs from the execution pin")
        return message
    raise ValueError("execution snapshot requires its selected durable N01 output")


async def read_context_snapshot(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
) -> ContextSnapshot | None:
    message = await read_context_message(connection, scope, locked)
    return message.snapshot if message is not None else None


async def read_replay_capture_message(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
    replay: StoredInferenceReplay,
) -> ContextMessage:
    """Read a capture only through the exact source chain recorded at admission."""
    capture_id = replay.capture_execution_id
    capture_digest = replay.capture_snapshot_digest
    visited: set[str] = set()
    while True:
        target = locked.execution
        if target.execution_id in visited:
            raise ValueError("inference replay source lineage cycle")
        visited.add(target.execution_id)
        replay.run.validate_target_execution(target)
        source_pin = replay.run.request.source
        source = await repo.get_execution(
            connection, scope, source_pin.execution_ref.record_id
        )
        if (
            source is None
            or source.task != locked.task
            or source.execution.created_at > target.created_at
        ):
            raise ValueError("inference replay source execution pin mismatch")
        source_pin.validate_execution(source.execution)
        if source.execution.execution_id == capture_id:
            break
        inherited = (
            await read_inference_replay_for_target(
                connection, scope, source.execution.execution_id
            )
            if source.is_inference_replay
            else None
        )
        if inherited is None or (
            inherited.capture_execution_id,
            inherited.capture_snapshot_digest,
        ) != (capture_id, capture_digest):
            raise ValueError("inference replay capture is outside the source lineage")
        locked, replay = source, inherited

    message = await read_context_message(connection, scope, source)
    if (
        message is None
        or message.snapshot is None
        or record_digest(message.snapshot) != capture_digest
    ):
        raise ValueError("inference replay capture snapshot pin mismatch")
    return message


async def read_replay_capture_snapshot(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
    replay: StoredInferenceReplay,
) -> ContextSnapshot:
    message = await read_replay_capture_message(connection, scope, locked, replay)
    assert message.snapshot is not None
    return message.snapshot


async def read_observation_retry(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    node: NodeRun,
    running_attempt: Attempt,
) -> ObservationRetryWitness | None:
    """Read the latest committed whole-batch failure, never a late receipt."""
    if (
        node.node_id is not NodeId.N04
        or node.status is not NodeRunStatus.RUNNING
        or running_attempt.status is not AttemptStatus.RUNNING
        or not running_attempt.belongs_to_node(node)
        or len(node.attempt_history) < 2
        or node.attempt_history[-1].attempt_ref.record_id != running_attempt.attempt_id
    ):
        return None
    eid, nid = node.execution_id, node.node_run_id
    for link in reversed(node.attempt_history[:-1]):
        aid = link.attempt_ref.record_id
        message = await repo.read_output(
            connection, scope, eid, nid, aid, MESSAGE_KEY, ObservationMessage
        )
        if message is None:
            continue
        attempt = await repo.get_attempt(connection, scope, eid, nid, aid)
        output = await repo.read_output(
            connection, scope, eid, nid, aid, NODE_OUTPUT_KEY, _OUTPUT
        )
        env = message.inputs.envelope
        if (
            attempt is None
            or not attempt.belongs_to_node(node)
            or attempt.attempt_number != link.attempt_number
            or attempt.attempt_number >= running_attempt.attempt_number
            or attempt.status is not AttemptStatus.FAILED
            or not isinstance(output, FailedNodeOutput)
            or not output.error.retryable
            or message.output != output
            or message.authority is not None
            or (env.execution_id, env.node_run_id, env.attempt_id, env.trace)
            != (eid, nid, aid, node.trace)
            or tuple(item.stable_json() for item in env.upstream_artifacts)
            != tuple(item.stable_json() for item in node.upstream_inputs)
            or message.batch.node_run_ref.record_id != nid
            or message.batch.attempt_ref.record_id != aid
        ):
            raise ValueError(
                "observation retry requires committed failed batch evidence"
            )
        return ObservationRetryWitness(message, node, attempt)
    return None


async def load_records(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    locked: repo.LockedExecution,
) -> WorkerRecords:
    eid = locked.execution.execution_id
    data = WorkerRecords(locked)
    data.tool_step_nodes = await load_child_nodes(connection, scope, eid)
    child_ids = {node.node_run_id for node in data.tool_step_nodes}
    for name, node in await repo.list_branches(connection, scope, eid):
        if node.node_run_id in child_ids:
            continue
        key = _branch(name, node)
        data.runs[key] = node
        message = await _node_message(connection, scope, locked, node)
        if message is not None:
            data.messages[key] = message
        if node.node_id is NodeId.N01 and node.status is NodeRunStatus.SUCCEEDED:
            data.gate = await repo.read_engine_output(
                connection, scope, eid, node.node_run_id, GATE_KEY, EligibilityMessage
            )
        if node.node_id is NodeId.N02 and node.status is NodeRunStatus.SKIPPED:
            data.skipped_discovery = await repo.read_engine_output(
                connection,
                scope,
                eid,
                node.node_run_id,
                SKIP_KEY,
                SkippedDiscoveryOutput,
            )
    return data


async def load_attempts(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    data: WorkerRecords,
) -> tuple[Attempt, ...]:
    attempts = []
    for node in (*data.runs.values(), *data.tool_step_nodes):
        for link in node.attempt_history:
            stored = await repo.get_attempt(
                connection,
                scope,
                node.execution_id,
                node.node_run_id,
                link.attempt_ref.record_id,
            )
            if (
                stored is None
                or not stored.belongs_to_node(node)
                or stored.attempt_number != link.attempt_number
            ):
                raise ValueError("workflow requires every referenced durable Attempt")
            attempts.append(stored)
    return tuple(attempts)
