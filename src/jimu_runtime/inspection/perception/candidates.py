"""Advisory candidate model calls for N06; rule evaluation is owned by the Worker."""

from dataclasses import dataclass

from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ModelBindingContentRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue
from jimu_runtime.domain.policies import ResourceRequirementsArtifact
from jimu_runtime.domain.runtime.decisions import CandidateCategory
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import ObjectRef
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCall, ModelCallFailure
from jimu_runtime.gateway.http_wire import ChatContract, VerifiedImage
from jimu_runtime.inspection.media.types import MediaError
from jimu_runtime.inspection.perception.model import (
    CandidateReply,
    ModelAccess,
    associated_invocation,
    build_chat_contract,
    failed_request,
    gateway_binding,
    image_input,
    prepare_binding,
    validate_reply_bytes,
)
from jimu_runtime.workflow.mock_support import K
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode


@dataclass(frozen=True, slots=True)
class CandidateModelResult:
    categories: tuple[CandidateCategory, ...]
    invocation: ModelInvocation | None
    failure_code: str | None


def _target(claimed: ClaimedNode) -> tuple[ObjectRecord, set[str]]:
    if claimed.node.node_id is not NodeId.N06:
        raise AdapterRejected("perception.candidate_wrong_node")
    obj = claimed.previous.object_record(claimed.branch)
    (profile,) = tuple(
        row
        for row in claimed.opened.profiles
        if row.rules.profile_ref == obj.profile_ref
    )
    return obj, {
        rule.definition.violation_category
        for ruleset in profile.rules.rule_sets
        for rule in ruleset.rules
    }


def _read_candidates(
    normalized: bytes,
    contract: ChatContract,
    allowed: set[str],
    invocation: ModelInvocation,
    output_limit: int,
) -> CandidateModelResult:
    try:
        if type(normalized) is not bytes or not 1 <= len(normalized) <= output_limit:
            raise AdapterRejected("perception.invalid_candidate_response_size")
        categories = validate_reply_bytes(
            normalized, contract, CandidateReply
        ).categories
        ids = {row.category_id for row in categories}
        if len(ids) != len(categories) or not ids <= allowed:
            raise AdapterRejected("perception.invalid_candidate_categories")
    except AdapterRejected as exc:
        return CandidateModelResult((), invocation, exc.code)
    return CandidateModelResult(
        tuple(
            CandidateCategory(category_id=category.category_id, score=category.score)
            for category in categories
        ),
        invocation,
        None,
    )


def project_candidate_result(
    claimed: ClaimedNode,
    binding_ref: ModelBindingContentRef,
    invocation: ModelInvocation,
    normalized: bytes | None,
    *,
    failure_code: str | None = None,
) -> CandidateModelResult:
    """Independently reproject an authenticated candidate receipt for Decision validation."""
    obj, allowed = _target(claimed)
    scope = claimed.opened.scope
    if (
        invocation.execution_id != scope.execution_id
        or invocation.node_id != NodeId.N06.value
        or invocation.node_run_ref.record_id != claimed.node.node_run_id
        or invocation.attempt_ref.record_id != claimed.attempt.attempt_id
        or invocation.trace_id != claimed.node.trace.trace_id
        or invocation.purposes != (ModelCapability.CANDIDATE_PREDICTION,)
        or invocation.profile_refs != (obj.profile_ref,)
        or invocation.object_ref
        != ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id)
        or reference_identity(invocation.configured_provider.binding_ref)
        != reference_identity(binding_ref)
    ):
        raise AdapterRejected("perception.foreign_candidate_invocation")
    definition = scope.resolve(binding_ref, ModelBinding)
    if definition.capability is not ModelCapability.CANDIDATE_PREDICTION:
        raise AdapterRejected("perception.model_binding_purpose_mismatch")
    actual = invocation.outcome
    if actual.status != "succeeded":
        if (
            normalized is not None
            or failure_code is not None
            and failure_code != actual.error.code
        ):
            raise AdapterRejected("perception.candidate_failure_pin_mismatch")
        return CandidateModelResult((), invocation, actual.error.code)
    if normalized is None or failure_code is not None:
        raise AdapterRejected("perception.candidate_wire_missing")
    return _read_candidates(
        normalized,
        build_chat_contract(scope, binding_ref),
        allowed,
        invocation,
        scope.resolve(
            definition.resource_requirements_ref, ResourceRequirementsArtifact
        ).policy.output_byte_limit,
    )


def candidate_model_call(
    claimed: ClaimedNode,
    binding_ref: ModelBindingContentRef,
    image_verified: VerifiedImage,
    resource_scope_id: str,
) -> ModelCall:
    """Rebuild the same release-scoped outbound advisory request without calling a provider."""
    obj, allowed = _target(claimed)
    scope = claimed.opened.scope
    purpose = ModelCapability.CANDIDATE_PREDICTION
    binding = prepare_binding(scope, binding_ref, purpose)
    category_ids: list[JSONValue] = [category for category in sorted(allowed)]
    return ModelCall(
        binding_ref=gateway_binding(binding_ref),
        purposes=(purpose,),
        profile_refs=(obj.profile_ref,),
        prompt=binding.prompt(
            image_input(scope),
            {
                "object_id": obj.object_id,
                "bbox": {"xyxy": list(obj.bbox.xyxy)},
                "registered_category_ids": category_ids,
            },
        ),
        resource_scope_id=resource_scope_id,
        image=image_verified,
        object_ref=ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id),
    )


async def predict_candidates(
    claimed: ClaimedNode,
    gateway: CapabilityGateway,
    access: ModelAccess,
    binding_ref: ModelBindingContentRef,
) -> CandidateModelResult:
    """Run the configured model once and keep advisory categories and real provenance."""
    _, allowed = _target(claimed)
    scope = claimed.opened.scope
    purpose = ModelCapability.CANDIDATE_PREDICTION
    binding = prepare_binding(scope, binding_ref, purpose)
    contract = build_chat_contract(scope, binding_ref)
    try:
        image = await access.image(scope)
    except (TimeoutError, ConnectionError, FileNotFoundError):
        return CandidateModelResult((), None, "perception.candidate_image_unavailable")
    except MediaError as exc:
        if str(exc) not in {"original_media_unavailable", "media_file_unavailable"}:
            raise
        return CandidateModelResult((), None, "perception.candidate_image_unavailable")
    call = candidate_model_call(claimed, binding_ref, image, access.resource_scope_id)
    try:
        result = await gateway.invoke_model(claimed, call)
    except ModelCallFailure as exc:
        return CandidateModelResult((), None, exc.code)
    invocation = associated_invocation(claimed, call, result)
    if result.output is None:
        return CandidateModelResult(
            (),
            invocation,
            failed_request(result, "perception.candidate_unavailable").code,
        )
    assert invocation is not None
    return _read_candidates(
        result.output,
        contract,
        allowed,
        invocation,
        scope.resolve(
            binding.definition.resource_requirements_ref, ResourceRequirementsArtifact
        ).policy.output_byte_limit,
    )
