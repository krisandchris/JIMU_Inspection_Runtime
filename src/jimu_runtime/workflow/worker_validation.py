"""Validate outputs from a worker node port before granting persistence authority."""

from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.prompts import PromptTemplateDefinition
from jimu_runtime.domain.definitions.tools import ToolPlanDefinition
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.nodes.envelopes import FailedNodeOutput
from jimu_runtime.domain.nodes.observation import is_region_observation_template
from jimu_runtime.domain.nodes.tool_execution import InvokedToolStep, ToolStepHistory
from jimu_runtime.domain.policies import ToolSkipPolicyArtifact
from jimu_runtime.domain.runtime.executions import Attempt, NodeId, NodeRun
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    FactRef,
    ModelInvocationRef,
)
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.mock_decision import mandatory_paths
from jimu_runtime.workflow.mock_facts import prepare_observation
from jimu_runtime.workflow.mock_support import K, artifact
from jimu_runtime.workflow.tool_dependencies import guard_fact_selectors
from jimu_runtime.workflow.worker_nodes import BranchInputs, NodeResult
from jimu_runtime.workflow.worker_records import (
    MESSAGE_NODES,
    NODE_MESSAGE,
    ContextMessage,
    DecisionMessage,
    DiscoveryMessage,
    GroundingMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_source import OpenExecution


def check_tool_history(message: ToolMessage, history: ToolStepHistory) -> None:
    if (
        history.resolved_executions(message.inputs, message.facts)
        != {item.tool_execution_id: item for item in message.executions}
        or not {
            pin
            for entry in history.entries
            for pin in entry.verified_observation_inputs
        }
        <= set(message.observation_input_artifacts)
        or set(message.authorities)
        != {entry.selection for entry in history.entries if entry.selection is not None}
        or len(set(message.authorities)) != len(message.authorities)
        or set(message.evidence)
        != {item for entry in history.entries for item in entry.evidence}
        or len({item.evidence_id for item in message.evidence}) != len(message.evidence)
        or set(message.nested_invocations)
        != {item for entry in history.entries for item in entry.invocations}
        or len({item.model_invocation_id for item in message.nested_invocations})
        != len(message.nested_invocations)
    ):
        raise ValueError("Tool summary must retain the complete durable step outputs")


def _check_observation_failure(
    message: ObservationMessage, output: FailedNodeOutput
) -> None:
    invocation = message.invocation.outcome
    parse_failure = message.batch.outcome in {"parse_error", "association_error"}
    if invocation.status == "succeeded":
        expected = "validation"
        expected_code = None
        retryable = parse_failure
    else:
        error = invocation.error
        expected_code = error.code
        expected = {
            "transport": "transport",
            "unavailable": "transport",
            "timeout": "timeout",
            "rate_limit": "resource",
            "resource_exhausted": "resource",
        }.get(error.error_class, "validation")
        if expected == "resource" and error.code.endswith("budget_exhausted"):
            expected = "validation"
        retryable = parse_failure or expected != "validation"
    if (
        output.error.error_class != expected
        or output.error.retryable != retryable
        or expected_code is not None
        and output.error.code != expected_code
    ):
        raise ValueError(
            "whole-batch failure must retain its verified invocation disposition"
        )


def check_result(
    opened: OpenExecution,
    previous: BranchInputs,
    branch: BranchKey,
    node: NodeRun,
    attempt: Attempt,
    result: NodeResult,
    *,
    tool_step_history: ToolStepHistory | None = None,
    configured: bool = False,
) -> NodeResult:
    message = NODE_MESSAGE.validate_json(result.message.model_dump_json())
    env, output = message.input_envelope(), message.output_envelope()
    scope = opened.scope
    wanted_upstream = (
        (artifact(K.TASK, scope.task.task_id, scope.task).pin,)
        if node.node_id is NodeId.N01
        else node.upstream_inputs
    )
    if (
        not isinstance(message, MESSAGE_NODES[node.node_id])
        or not attempt.belongs_to_node(node)
        or (
            env.execution_id,
            env.node_run_id,
            env.attempt_id,
            env.trace,
            output.started_at,
        )
        != (
            node.execution_id,
            node.node_run_id,
            attempt.attempt_id,
            node.trace,
            attempt.started_at,
        )
        or tuple(pin.stable_json() for pin in env.upstream_artifacts)
        != tuple(pin.stable_json() for pin in wanted_upstream)
    ):
        raise ValueError("node reply must belong to the exact claimed invocation")
    assert attempt.started_at is not None
    scope.check_call(env, node.node_id, attempt.started_at)

    if isinstance(message, ContextMessage):
        message.output.check_invocation(message.inputs)
        if (
            isinstance(message.output, SuccessfulContextBuild)
            and message.snapshot is not None
        ):
            message.output.check_snapshot(message.inputs, message.snapshot)
        elif (
            isinstance(message.output, SuccessfulContextBuild)
            or message.snapshot is not None
        ):
            raise ValueError(
                "successful context alone may publish its validated snapshot"
            )
        if (
            message.inputs.accepted_task != scope.task
            or message.inputs.media_asset != scope.media
            or message.inputs.device_ref != opened.source.device
            or message.inputs.camera_ref != opened.source.camera
            or message.inputs.region_ref != opened.region_ref
        ):
            raise ValueError(
                "context reply must retain the verified Task and source registry"
            )
    elif isinstance(message, DiscoveryMessage):
        if previous.gate is None:
            raise ValueError("discovery must have a durable eligibility gate")
        message.inputs.check_gate(previous.gate.evaluation)
        message.inputs.check_profiles(
            tuple(
                scope.resolve(ref, ProfileDefinition)
                for ref in message.inputs.eligible_profiles
            )
        )
        message.output.check_records(
            message.inputs,
            message.selections,
            message.invocations,
            group_failures=message.group_failures,
        )
        message.result()
    elif isinstance(message, GroundingMessage):
        if any(obj.marked_frame is not None for obj in message.objects):
            raise ValueError("new grounding outputs must use original-image boxes")
        if previous.discovery is None:
            raise ValueError("grounding requires complete discovery inputs")
        message.inputs.check_discovery(previous.discovery.output)
        message.output.check_records(
            message.inputs,
            message.records,
            message.objects,
            previous.discovery.selections,
            message.producers(),
            tuple(
                scope.resolve(item.hit.profile_ref, ProfileDefinition)
                for item in message.inputs.profiles
            ),
            undispatched_profiles=message.undispatched_profiles,
        )
    else:
        obj = previous.object_record(branch)
        profile = scope.resolve(obj.profile_ref, ProfileDefinition)
        inputs = (
            message.draft.inputs
            if isinstance(message, DecisionMessage)
            else message.inputs
        )
        if inputs.object_record != obj:
            raise ValueError("node reply must use the scoped durable Object")
        if isinstance(message, (ObservationMessage, ToolMessage)) and (
            message.inputs.image_view.marked_media_ref is not None
        ):
            raise ValueError(
                "new observation and tool inputs require original-image boxes"
            )
        if isinstance(message, ObservationMessage):
            if previous.discovery is None:
                raise ValueError("observation needs its durable Question selection")
            (selection,) = tuple(
                item
                for item in previous.discovery.selections
                if item.selection_id == obj.question_selection_ref.record_id
            )
            message.inputs.check_configuration(
                obj.profile_ref,
                profile,
                selection,
                tuple(
                    (ref, scope.resolve(ref, QuestionDefinition))
                    for ref in obj.effective_question_refs
                ),
            )
            retry = previous.observation_retry
            message.inputs.check_batch(
                message.batch,
                (message.invocation,),
                None if retry is None else retry.message.batch,
            )
            # WP-03 historical and WP-04 compatibility adapters have synthetic
            # inline bytes. Configured production execution supplies verified records.
            if configured:
                binding = scope.resolve(profile.observation_binding_ref, ModelBinding)
                original_regions = binding.prompt_template_ref is not None and (
                    is_region_observation_template(
                        scope.resolve(
                            binding.prompt_template_ref, PromptTemplateDefinition
                        ).template_text
                    )
                )
                if original_regions != (
                    message.inputs.image_mode == "original_regions_v1"
                ):
                    raise ValueError(
                        "observation image mode must match the pinned binding"
                    )
                original = message.original_image_evidence
                if original_regions and (
                    original is None
                    or original.sha256 != scope.media.media_ref.sha256
                    or not any(
                        ref.record_kind is K.EVIDENCE
                        and ref.record_id == original.evidence_id
                        for ref in message.invocation.evidence_refs
                    )
                ):
                    raise ValueError(
                        "regional observation must retain its original image evidence"
                    )
                if message.request_evidence or not message.durable_request_evidence:
                    raise ValueError(
                        "configured observation requires durable request evidence"
                    )
                prompt = message.inputs.compiled_prompt_evidence_ref
                schema = message.inputs.contract_snapshot.response_schema_evidence_ref
                if (
                    prompt == schema
                    or {item.evidence_id for item in message.request_assets}
                    != {prompt.record_id, schema.record_id}
                    or len(message.request_assets) != 2
                    or set((prompt, schema)) - set(message.invocation.evidence_refs)
                    or any(
                        item.scope.scope != "object"
                        or item.scope.execution_id != env.execution_id
                        or item.scope.object_ref.record_id != obj.object_id
                        or item.scope.profile_ref != obj.profile_ref
                        for item in message.request_assets
                    )
                    or next(
                        item.sha256
                        for item in message.request_assets
                        if item.evidence_id == schema.record_id
                    )
                    != message.inputs.contract_snapshot.response_schema_sha256
                ):
                    raise ValueError(
                        "observation evidence requires its distinct exact prompt and Schema records"
                    )
                if retry is not None and (
                    message.inputs.retry is None
                    or not isinstance(retry.message.output, FailedNodeOutput)
                    or message.inputs.retry.reason != retry.message.output.error.code
                    or message.inputs.compiled_prompt_evidence_ref
                    != retry.message.inputs.compiled_prompt_evidence_ref
                    or message.inputs.contract_snapshot
                    != retry.message.inputs.contract_snapshot
                    or message.inputs.joint_questions
                    != retry.message.inputs.joint_questions
                    or message.inputs.image_view != retry.message.inputs.image_view
                    or message.inputs.image_mode != retry.message.inputs.image_mode
                ):
                    raise ValueError(
                        "observation retry must preserve the initial request scope and contract"
                    )
            else:
                expected, evidence = prepare_observation(
                    scope, env, obj, previous.snapshot(), attempt.started_at
                )
                if message.inputs != expected or message.evidence != tuple(
                    item.record for item in evidence
                ):
                    raise ValueError(
                        "joint observation must persist the selected synthetic request evidence"
                    )
            authority = message.authority
            if authority is not None and (
                output.status is not NodeRunStatus.SUCCEEDED
                or authority.selected_result_ref != message.batch.batch_ref
                or authority.selected_attempt_id != attempt.attempt_id
                or (authority.execution_id, authority.profile_ref, authority.object_id)
                != (env.execution_id, obj.profile_ref, obj.object_id)
                or {
                    (ref.record_kind, ref.record_id)
                    for ref in authority.selected_fact_refs
                }
                != {(K.FACT, fact.fact_id) for fact in message.facts}
            ):
                raise ValueError(
                    "joint observation selection must name its exact successful result and Facts"
                )
            if isinstance(output, FailedNodeOutput):
                if not configured:
                    raise ValueError(
                        "legacy observation must retain its selected whole batch"
                    )
                _check_observation_failure(message, output)
            batch_records = (
                artifact(
                    K.OBSERVATION_BATCH,
                    message.batch.observation_batch_id,
                    message.batch,
                ),
                artifact(
                    K.MODEL_INVOCATION,
                    message.invocation.model_invocation_id,
                    message.invocation,
                ),
                *(
                    artifact(K.EVIDENCE, item.evidence_id, item)
                    for item in message.evidence
                ),
                *(artifact(K.FACT, item.fact_id, item) for item in message.facts),
            )
            if configured and (
                not {item.pin for item in batch_records} <= set(output.output_artifacts)
                or authority is not None
                and message.authority_artifact not in result.artifacts
            ):
                raise ValueError(
                    "observation manifest must pin its physical inputs and actual results"
                )
        elif isinstance(message, ToolMessage):
            plan = scope.resolve(message.inputs.tool_plan_ref, ToolPlanDefinition)
            completion = message.completion_artifact
            if (
                output.status is NodeRunStatus.SUCCEEDED
                and not message.executions
                and (completion is None or result.artifacts != (completion,))
            ):
                raise ValueError(
                    "ToolPlan completion may only persist its pinned summary"
                )
            observation = previous.observation
            historical_fact_pins = {
                pin.ref.record_id
                for pin in env.upstream_artifacts
                if pin.ref.record_kind is K.FACT
            }
            parent_facts = {
                ref.record_id for ref in message.inputs.validated_observation_fact_refs
            }
            supplement = message.observation_input_artifacts
            if (
                parent_facts != historical_fact_pins
                or supplement
                and historical_fact_pins
            ):
                raise ValueError("N05 must preserve its immutable parent inputs")
            requested_facts = parent_facts or {
                pin.ref.record_id for pin in supplement if pin.ref.record_kind is K.FACT
            }
            selected_facts = (
                ()
                if observation is None
                else tuple(
                    fact
                    for fact in observation.facts
                    if fact.fact_id in requested_facts
                )
            )
            authority = (
                None
                if observation is None or not requested_facts
                else observation.authority
            )
            projected = message.inputs
            if supplement:
                if authority is None or observation is None:
                    raise ValueError(
                        "N05 step audit requires a selected Observation batch"
                    )
                expected_pins = {
                    observation.authority_artifact.pin.stable_json(),
                    *(
                        artifact(K.FACT, fact.fact_id, fact).pin.stable_json()
                        for fact in selected_facts
                    ),
                }
                if (
                    len(supplement) != len(expected_pins)
                    or {pin.stable_json() for pin in supplement} != expected_pins
                ):
                    raise ValueError(
                        "N05 step audit must pin the exact selected Observation inputs"
                    )
                projected = message.inputs.model_copy(
                    update={
                        "validated_observation_fact_refs": tuple(
                            FactRef(record_kind=K.FACT, record_id=fact.fact_id)
                            for fact in selected_facts
                        ),
                        "observation_authority_ref": AuthoritySelectionRef(
                            record_kind=K.AUTHORITATIVE_RESULT_SELECTION,
                            record_id=authority.selection_id,
                        ),
                    }
                )
            projected.check_observation_facts(
                plan,
                selected_facts,
                authority,
                guard_selectors=tuple(
                    selector
                    for step in plan.steps
                    for selector in guard_fact_selectors(
                        scope.resolve(
                            step.skip_policy_ref, ToolSkipPolicyArtifact
                        ).policy
                    )
                ),
            )
            if configured and (
                any(
                    isinstance(step, InvokedToolStep)
                    for step in message.output.summary.steps
                )
                and tool_step_history is None
            ):
                raise ValueError(
                    "configured N05 requires exact child results and pinned input Facts"
                )
            message.output.check_steps(
                message.inputs,
                profile,
                plan,
                message.executions,
                message.facts,
                history=tool_step_history,
            )
            if tool_step_history is not None:
                check_tool_history(message, tool_step_history)
        elif isinstance(message, DecisionMessage):
            if previous.gate is None:
                raise ValueError(
                    "Decision requires the durable Profile eligibility scope"
                )
            message.draft.inputs.check_gate(previous.gate.evaluation)
            paths = (
                next(
                    item.mandatory_rule_paths
                    for item in opened.profiles
                    if item.rules.profile_ref == obj.profile_ref
                )
                if configured
                else mandatory_paths(scope, obj)
            )
            message.draft.inputs.check_mandatory_scope(paths)
            if configured:
                staged = message.result()
                facts, authorities = previous.selected_facts()
                if {ref.record_id for ref in message.draft.inputs.fact_refs} != {
                    fact.fact_id for fact in facts
                } or {
                    ref.record_id
                    for ref in message.draft.inputs.authority_selection_refs
                } != {selection.selection_id for selection in authorities}:
                    raise ValueError(
                        "N06 must read the exact selected upstream Facts and authority"
                    )
                all_artifacts = set(result.artifacts)
                if not set(staged.artifacts) <= all_artifacts:
                    raise ValueError(
                        "Decision manifest must include its actual runtime records"
                    )

    if not {pin for pin in output.output_artifacts} <= {
        item.pin for item in result.artifacts
    }:
        raise ValueError("node result is missing one or more pinned output artifacts")
    actual_refs = tuple(
        ModelInvocationRef(record_kind=K.MODEL_INVOCATION, record_id=item.ref.record_id)
        for item in result.artifacts
        if item.ref.record_kind is K.MODEL_INVOCATION
    )
    if actual_refs != result.model_refs:
        raise ValueError("model invocation index must account for the actual artifacts")
    return NodeResult(message, result.artifacts, result.model_refs)
