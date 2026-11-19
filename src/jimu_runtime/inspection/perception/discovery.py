"""N02 discovers eligible Profiles and preserves the declared Question set."""

from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.capabilities import (
    ModelBindingContentRef,
    ProfileDefinition,
)
from jimu_runtime.domain.definitions.models import (
    PolicyContentRef as SelectionPolicyRef,
)
from jimu_runtime.domain.fact_selectors import QuestionContentRef
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    ProfileHit,
    _DiscoveryGroupFailure,
)
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope
from jimu_runtime.domain.policies import QuestionSelectionPolicyArtifact
from jimu_runtime.domain.runtime.facts import (
    PolicyContentRef,
    ProfileContentRef,
    QuestionSelectionRef,
)
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.selection import AllDeclaredSelectionRecord
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCall
from jimu_runtime.gateway.http_wire import ChatContract
from jimu_runtime.inspection.perception.model import (
    DiscoveryReply,
    ModelAccess,
    associated_invocation,
    build_chat_contract,
    failed_request,
    gateway_binding,
    image_input,
    invocation_ref,
    model_failure,
    node_output,
    prepare_binding,
    runtime_id,
    validate_reply_bytes,
)
from jimu_runtime.workflow.mock_context import snapshot_artifact
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    VerifiedExecutionScope,
    artifact,
)
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode
from jimu_runtime.workflow.worker_nodes import NodeResult, discovery_inputs
from jimu_runtime.workflow.worker_records import DiscoveryMessage


def _question_partitions(
    scope: VerifiedExecutionScope, profile: ProfileDefinition, ref: PolicyContentRef
) -> tuple[tuple[QuestionContentRef, ...], tuple[QuestionContentRef, ...]]:
    policy = scope.resolve(ref, QuestionSelectionPolicyArtifact).policy
    if policy.mode != "all_declared":
        raise AdapterRejected("perception.unsupported_question_selection")
    configured = profile.question_refs
    mandatory, optional = (
        set(policy.mandatory_question_refs),
        set(policy.optional_question_refs),
    )
    if set(configured) != mandatory | optional:
        raise AdapterRejected("perception.invalid_question_partition")
    for question in configured:
        scope.catalogue.resolve(question)
    return (
        tuple(question for question in configured if question in mandatory),
        tuple(question for question in configured if question in optional),
    )


def _check_proposal(
    contract: ChatContract, payload: bytes, eligible: set[str]
) -> list[str]:
    proposed = validate_reply_bytes(payload, contract, DiscoveryReply).profile_hits
    if set(proposed) - eligible or len(set(proposed)) != len(proposed):
        raise AdapterRejected("perception.invalid_profile_proposal")
    return proposed


def verify_discovery_rejection(
    scope: VerifiedExecutionScope,
    failure: _DiscoveryGroupFailure,
    normalized: bytes,
) -> None:
    """Verify a local rejected proposal using exact normalized journal response bytes."""
    if normalized is None:
        raise AdapterRejected("perception.discovery_rejection_bytes_missing")
    contract = build_chat_contract(scope, failure.binding_ref)
    try:
        _check_proposal(
            contract,
            normalized,
            {profile.artifact_id for profile in failure.profile_refs},
        )
    except AdapterRejected as exc:
        if failure.error == model_failure(None, exc.code):
            return
    raise AdapterRejected("perception.discovery_rejection_mismatch")


async def discover(
    claimed: ClaimedNode,
    envelope: NodeInputEnvelope,
    gateway: CapabilityGateway,
    access: ModelAccess,
) -> NodeResult:
    opened, previous = claimed.opened, claimed.previous
    scope = opened.scope
    inputs = discovery_inputs(opened, previous, envelope)
    gate = previous.gate
    started = claimed.attempt.started_at
    if gate is None or started is None:
        raise AdapterRejected("perception.discovery_inputs_unavailable")
    inputs.check_gate(gate.evaluation)
    profiles = tuple(
        scope.resolve(ref, ProfileDefinition) for ref in inputs.eligible_profiles
    )
    inputs.check_profiles(profiles)
    snapshot = previous.snapshot()
    scope.check_snapshot(snapshot, inputs.context_snapshot_ref)
    scoped_call = scope.check_call(envelope, envelope.node_id, started)
    scoped_call.check_upstream(
        snapshot_artifact(snapshot),
        artifact(
            K.PROFILE_ELIGIBILITY_EVALUATION,
            gate.evaluation.evaluation_id,
            gate.evaluation,
        ),
    )
    groups: dict[ModelBindingContentRef, list[ProfileContentRef]] = {}
    partitions = {}
    for entry, profile in zip(inputs.bindings, profiles, strict=True):
        partitions[entry.profile_ref] = _question_partitions(
            scope, profile, entry.question_selection_policy_ref
        )
        groups.setdefault(entry.discovery_binding_ref, []).append(entry.profile_ref)
    image = await access.image(scope)
    invocations: list[ModelInvocation] = []
    accepted: dict[ProfileContentRef, ModelInvocation] = {}
    completed = 0
    diagnostics: list[_DiscoveryGroupFailure] = []
    for ref, group in groups.items():
        purpose = ModelCapability.PROFILE_DISCOVERY
        binding = prepare_binding(scope, ref, purpose)
        if any(
            entry.prompt_template_ref != binding.definition.prompt_template_ref
            for entry in inputs.bindings
            if entry.discovery_binding_ref == ref
        ):
            raise AdapterRejected("perception.discovery_prompt_pin_mismatch")
        by_id = {profile.artifact_id: profile for profile in group}
        if len(by_id) != len(group):
            raise AdapterRejected("perception.ambiguous_profile_ids")
        contract = build_chat_contract(scope, ref)
        call = ModelCall(
            binding_ref=gateway_binding(ref),
            purposes=(purpose,),
            profile_refs=tuple(group),
            prompt=binding.prompt(
                image_input(scope), {"eligible_profile_ids": list(by_id)}
            ),
            resource_scope_id=access.resource_scope_id,
            image=image,
        )
        result = await gateway.invoke_model(claimed, call)
        invocation = associated_invocation(claimed, call, result)
        if invocation is not None:
            invocations.append(invocation)
        error = None
        if result.output is None:
            error = failed_request(result, "perception.discovery_unavailable")
        else:
            assert invocation is not None
            try:
                proposed = _check_proposal(contract, result.output, set(by_id))
            except AdapterRejected as exc:
                error = failed_request(result, exc.code)
            else:
                completed += 1
                accepted.update((by_id[name], invocation) for name in proposed)
        if error is not None:
            if result.dispatch_id is None:
                raise AdapterRejected("perception.discovery_dispatch_unavailable")
            diagnostics.append(
                _DiscoveryGroupFailure(
                    schema_version=VERSION,
                    execution_id=envelope.execution_id,
                    node_run_id=envelope.node_run_id,
                    attempt_id=envelope.attempt_id,
                    binding_ref=ref,
                    profile_refs=tuple(group),
                    dispatch_id=result.dispatch_id,
                    boundary=result.boundary,
                    error=error,
                    model_invocation_ref=None
                    if invocation is None
                    else invocation_ref(invocation),
                )
            )
    return _discovery_result(
        claimed,
        inputs,
        tuple(invocations),
        accepted,
        partitions,
        completed,
        tuple(diagnostics),
    )


def _discovery_result(
    claimed: ClaimedNode,
    inputs: DiscoveryInput,
    invocations: tuple[ModelInvocation, ...],
    accepted: dict[ProfileContentRef, ModelInvocation],
    partitions: dict[
        ProfileContentRef,
        tuple[tuple[QuestionContentRef, ...], tuple[QuestionContentRef, ...]],
    ],
    completed: int,
    group_failures: tuple[_DiscoveryGroupFailure, ...],
) -> NodeResult:
    selections = []
    hits = []
    for item in inputs.bindings:
        ref = item.profile_ref
        if (invocation := accepted.get(ref)) is None:
            continue
        profile = claimed.opened.scope.resolve(ref, ProfileDefinition)
        mandatory, optional = partitions[ref]
        env = inputs.envelope
        selection = AllDeclaredSelectionRecord(
            schema_version=VERSION,
            selection_id=runtime_id(env, "questions", ref.stable_json()),
            execution_id=env.execution_id,
            profile_ref=ref,
            policy_ref=SelectionPolicyRef.model_validate(
                item.question_selection_policy_ref.model_dump()
            ),
            configured_question_refs=profile.question_refs,
            mandatory_question_refs=mandatory,
            proposed_optional_question_refs=(),
            selected_optional_question_refs=optional,
            effective_question_refs=profile.question_refs,
            excluded_question_reasons=(),
            context_snapshot_ref=inputs.context_snapshot_ref,
            context_snapshot_digest=inputs.context_snapshot_digest,
            node_run_id=env.node_run_id,
            attempt_id=env.attempt_id,
            discovery_model_invocation_ref=invocation_ref(invocation),
            validation_issues=(),
            created_at=invocation.completed_at,
            method="all_declared",
            fallback_applied=False,
        )
        selections.append(selection)
        hits.append(
            ProfileHit(
                profile_ref=ref,
                question_selection_ref=QuestionSelectionRef(
                    record_kind=K.QUESTION_SELECTION_RECORD,
                    record_id=selection.selection_id,
                ),
                effective_question_refs=selection.effective_question_refs,
                model_invocation_ref=invocation_ref(invocation),
            )
        )
    artifacts = (
        *(
            artifact(K.MODEL_INVOCATION, row.model_invocation_id, row)
            for row in invocations
        ),
        *(
            artifact(K.QUESTION_SELECTION_RECORD, row.selection_id, row)
            for row in selections
        ),
    )
    refs = tuple(invocation_ref(row) for row in invocations)
    failure = (
        max((row.error for row in group_failures), key=lambda row: row.retryable)
        if group_failures
        else None
    )
    output = DiscoveryAttemptOutput(
        schema_version=VERSION,
        execution_id=inputs.envelope.execution_id,
        outcome="failed"
        if not completed
        else "partial"
        if failure
        else "hits"
        if hits
        else "no_hits",
        envelope=node_output(
            claimed, artifacts, invocations, failure if not completed else None
        ),
        profile_hits=tuple(hits),
        diagnostics=(),
        model_invocation_refs=refs,
    )
    output.check_records(
        inputs, tuple(selections), invocations, group_failures=group_failures
    )
    return NodeResult(
        DiscoveryMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=output,
            selections=tuple(selections),
            invocations=invocations,
            group_failures=group_failures,
        ),
        artifacts,
        refs,
    )
