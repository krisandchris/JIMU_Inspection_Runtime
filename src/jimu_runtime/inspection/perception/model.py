"""Release-pinned model preparation and exact dispatch association."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from jimu_runtime.bundle.artifacts import SchemaResource
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.bundle.schema_validation import compile_offline_response_schema
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ModelBindingContentRef
from jimu_runtime.domain.definitions.models import ModelBinding
from jimu_runtime.domain.definitions.models import (
    ModelBindingContentRef as GatewayBindingRef,
)
from jimu_runtime.domain.definitions.prompts import (
    PromptTemplateContentRef,
    PromptTemplateDefinition,
    placeholder_names,
)
from jimu_runtime.domain.enums import NodeRunStatus
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.fact_selectors import QuestionContentRef
from jimu_runtime.domain.nodes.envelopes import (
    FailedNodeOutput,
    NodeError,
    NodeInputEnvelope,
    NodeOutputEnvelope,
    SucceededNodeOutput,
)
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.references import ContentRef, SchemaRef
from jimu_runtime.domain.runtime.facts import ModelInvocationRef
from jimu_runtime.domain.runtime.invocations import (
    InvocationError,
    ModelInvocation,
    SucceededInvocation,
)
from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.gateway.bridge import ModelCall, require_model_output
from jimu_runtime.gateway.http_wire import ChatContract, VerifiedImage, strict_json
from jimu_runtime.gateway.invocation import DispatchOutcome
from jimu_runtime.workflow.mock_support import (
    K,
    SyntheticArtifact,
    VerifiedExecutionScope,
)
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode


class DiscoveryReply(ContractModel):
    profile_hits: Annotated[list[ArtifactId], Field(max_length=1024)]


class GroundingReply(ContractModel):
    boxes: Annotated[list[BBoxXYXY], Field(max_length=1024)]


class CandidateScore(ContractModel):
    category_id: ArtifactId
    score: Annotated[float, Field(allow_inf_nan=False)]


class CandidateReply(ContractModel):
    categories: Annotated[list[CandidateScore], Field(max_length=1024)]


class ObservationReply(ContractModel):
    """The exact dynamic slot list is supplied by the admitted Schema."""


_RESPONSE_MODELS: dict[ModelCapability, type[ContractModel]] = {
    ModelCapability.PROFILE_DISCOVERY: DiscoveryReply,
    ModelCapability.OBJECT_GROUNDING: GroundingReply,
    ModelCapability.OBJECT_OBSERVATION: ObservationReply,
    ModelCapability.CANDIDATE_PREDICTION: CandidateReply,
}
_RESOURCE_SCOPE = TypeAdapter(ArtifactId)


def _schema(
    scope: VerifiedExecutionScope, ref: ContentRef
) -> tuple[SchemaResource, bytes]:
    member = scope.catalogue.resolve(ref)
    if (
        not isinstance(member.value, SchemaResource)
        or sha256_digest(member.canonical_bytes) != ref.digest
    ):
        raise AdapterRejected("perception.schema_pin_mismatch")
    return member.value, member.canonical_bytes


def build_chat_contract(
    scope: VerifiedExecutionScope,
    binding_ref: ModelBindingContentRef,
    effective_questions: tuple[QuestionContentRef, ...] = (),
) -> ChatContract:
    """Register stock HTTP against the exact admitted binding and schema bytes."""
    binding = scope.resolve(binding_ref, ModelBinding)
    model = _RESPONSE_MODELS.get(binding.capability)
    if model is None or bool(effective_questions) != (
        binding.capability is ModelCapability.OBJECT_OBSERVATION
    ):
        raise AdapterRejected("perception.unsupported_model_contract")
    _schema(scope, binding.input_schema_ref)
    _, raw = _schema(scope, binding.output_schema_ref)
    for ref in effective_questions:
        scope.catalogue.resolve(ref)
    return ChatContract(
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
        response_model=model,
        accepts_image=True,
        response_schema=raw,
        question_slots=tuple(ref.artifact_id for ref in effective_questions),
    )


def schema_resource(scope: VerifiedExecutionScope, ref: ContentRef) -> SchemaResource:
    return _schema(scope, ref)[0]


def response_schema_bytes(scope: VerifiedExecutionScope, ref: SchemaRef) -> bytes:
    return _schema(scope, ref)[1]


def literal_prompt(
    scope: VerifiedExecutionScope,
    ref: PromptTemplateContentRef,
    purpose: ModelCapability,
    response_schema_ref: SchemaRef,
) -> str:
    template = scope.resolve(ref, PromptTemplateDefinition)
    if (
        template.purpose is not purpose
        or reference_identity(template.response_schema_ref)
        != reference_identity(response_schema_ref)
        or template.variables
        or template.partial_refs
        or placeholder_names(template.template_text)
    ):
        raise AdapterRejected("perception.unsupported_prompt_contract")
    return template.template_text.replace("{{", "{").replace("}}", "}")


@dataclass(frozen=True, slots=True)
class PreparedBinding:
    scope: VerifiedExecutionScope
    definition: ModelBinding
    template: str

    def prompt(
        self, model_input: dict[str, JSONValue], instructions: dict[str, JSONValue]
    ) -> str:
        schema, _ = _schema(self.scope, self.definition.input_schema_ref)
        compile_offline_response_schema(schema.document).validate(model_input)
        return "\n".join(
            (
                self.template,
                canonical_bytes(model_input).decode("utf-8"),
                canonical_bytes(instructions).decode("utf-8"),
            )
        )


def prepare_binding(
    scope: VerifiedExecutionScope,
    ref: ModelBindingContentRef,
    purpose: ModelCapability,
) -> PreparedBinding:
    definition = scope.resolve(ref, ModelBinding)
    _schema(scope, definition.output_schema_ref)
    if definition.capability is not purpose or definition.prompt_template_ref is None:
        raise AdapterRejected("perception.model_binding_purpose_mismatch")
    return PreparedBinding(
        scope,
        definition,
        literal_prompt(
            scope, definition.prompt_template_ref, purpose, definition.output_schema_ref
        ),
    )


type ImageLoader = Callable[[VerifiedExecutionScope], Awaitable[VerifiedImage]]


type MarkedImageLoader = Callable[
    [VerifiedExecutionScope, ObjectRecord], Awaitable[VerifiedImage]
]


@dataclass(frozen=True, slots=True)
class ModelAccess:
    load_image: ImageLoader
    resource_scope_id: ArtifactId
    load_marked_image: MarkedImageLoader | None = None

    def __post_init__(self) -> None:
        _RESOURCE_SCOPE.validate_python(self.resource_scope_id)

    async def image(self, scope: VerifiedExecutionScope) -> VerifiedImage:
        media = scope.media
        image = await self.load_image(scope)
        if (
            not isinstance(image, VerifiedImage)
            or type(image.content) is not bytes
            or image.sha256 != media.media_ref.sha256
            or image.media_type != media.mime_type
            or len(image.content) != media.byte_size
            or sha256_digest(image.content) != image.sha256
        ):
            raise AdapterRejected("perception.original_media_mismatch")
        return image

    async def observation_image(
        self, scope: VerifiedExecutionScope, obj: ObjectRecord
    ) -> VerifiedImage:
        if (
            obj.execution_id != scope.execution_id
            or obj.source_media_ref != scope.media.media_ref
        ):
            raise AdapterRejected("perception.observation_image_scope_mismatch")
        frame = obj.marked_frame
        if frame is None:
            return await self.image(scope)
        if self.load_marked_image is None:
            raise AdapterRejected("perception.marked_frame_loader_unavailable")
        image = await self.load_marked_image(scope, obj)
        if (
            image.media_type != frame.mime_type
            or image.sha256 != frame.media_ref.sha256
            or type(image.content) is not bytes
            or len(image.content) != frame.byte_size
            or sha256_digest(image.content) != frame.media_ref.sha256
        ):
            raise AdapterRejected("perception.marked_frame_pin_mismatch")
        return image


def image_input(scope: VerifiedExecutionScope) -> dict[str, JSONValue]:
    media = scope.media.media_ref
    return {"image": {"media_id": media.media_id, "sha256": media.sha256}}


def original_frame(scope: VerifiedExecutionScope) -> CoordinateSystem:
    return CoordinateSystem(
        coordinate_system_id=scope.media.media_ref.media_id,
        frame="original_image",
        dimensions=scope.media.dimensions,
    )


def invocation_ref(record: ModelInvocation) -> ModelInvocationRef:
    return ModelInvocationRef(
        record_kind=K.MODEL_INVOCATION, record_id=record.model_invocation_id
    )


def associated_invocation(
    claimed: ClaimedNode, call: ModelCall, result: DispatchOutcome
) -> ModelInvocation | None:
    invocation = result.invocation
    if invocation is None:
        if result.output is not None:
            raise AdapterRejected("perception.output_without_invocation")
        return None
    if (
        invocation.execution_id != claimed.opened.scope.execution_id
        or invocation.node_id != claimed.node.node_id.value
        or invocation.node_run_ref.record_id != claimed.node.node_run_id
        or invocation.attempt_ref.record_id != claimed.attempt.attempt_id
        or invocation.trace_id != claimed.node.trace.trace_id
        or invocation.request_digest != result.request_digest
        or invocation.purposes != call.purposes
        or invocation.profile_refs != call.profile_refs
        or invocation.object_ref != call.object_ref
        or invocation.source_discovery_invocation_ref
        != call.source_discovery_invocation_ref
        or invocation.contract_snapshot != call.contract_snapshot
        or reference_identity(invocation.configured_provider.binding_ref)
        != reference_identity(call.binding_ref)
        or not set(call.evidence_refs) <= set(invocation.evidence_refs)
    ):
        raise AdapterRejected("perception.foreign_invocation")
    if result.output is not None and (
        not isinstance(invocation.outcome, SucceededInvocation)
        or invocation.outcome.response_digest != result.response_digest
        or result.transport_result.response_digest != result.response_digest
        or result.error is not None
        or result.transport_result.parsed_content != result.output
        or result.transport_result.validated_response_contract_digest
        != (
            call.contract_snapshot.response_schema_sha256
            if call.contract_snapshot is not None
            else claimed.opened.scope.resolve(
                call.binding_ref, ModelBinding
            ).output_schema_ref.digest
        )
    ):
        raise AdapterRejected("perception.unverified_output")
    return invocation


def checked_reply[T: ContractModel](
    result: DispatchOutcome, contract: ChatContract, response_model: type[T]
) -> T:
    return validate_reply_bytes(require_model_output(result), contract, response_model)


def validate_reply_bytes[T: ContractModel](
    payload: bytes, contract: ChatContract, response_model: type[T]
) -> T:
    try:
        contract.validate_response_content(
            payload.decode("utf-8"), strict_json(payload, exact_numbers=True)
        )
        return response_model.model_validate_json(payload)
    except ValueError:
        raise AdapterRejected("perception.invalid_response") from None


def gateway_binding(ref: ModelBindingContentRef) -> GatewayBindingRef:
    return GatewayBindingRef.model_validate(ref.model_dump())


def runtime_id(envelope: NodeInputEnvelope, kind: str, *parts: str) -> str:
    digest = sha256_digest(
        canonical_bytes(
            [envelope.execution_id, envelope.node_run_id, envelope.attempt_id, *parts]
        )
    )
    return f"perception.{kind}.{digest}"


def issue(code: str, message: str, path: tuple[str | int, ...] = ()) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path, related_refs=())


def failed_request(result: DispatchOutcome, code: str) -> NodeError:
    return model_failure(result.error, code)


def model_failure(error: InvocationError | None, code: str) -> NodeError:
    category: Literal["validation", "transport", "timeout", "resource"] = "validation"
    if error is not None:
        if error.error_class in {"transport", "unavailable"}:
            category = "transport"
        elif error.error_class == "timeout":
            category = "timeout"
        elif error.error_class in {
            "rate_limit",
            "resource_exhausted",
        } and not error.code.endswith("budget_exhausted"):
            category = "resource"
    message = "Perception invocation failed to produce usable output."
    diagnostic = issue(code if error is None else error.code, message)
    return NodeError(
        code=diagnostic.code,
        error_class=category,
        retryable=category != "validation",
        message=message,
        validation_issues=(diagnostic,),
    )


def node_output(
    claimed: ClaimedNode,
    artifacts: tuple[SyntheticArtifact, ...],
    invocations: tuple[ModelInvocation, ...],
    error: NodeError | None = None,
) -> NodeOutputEnvelope:
    started = claimed.attempt.started_at
    if started is None:
        raise AdapterRejected("perception.attempt_not_started")
    finished = max((started, *(record.completed_at for record in invocations)))
    elapsed = finished - started
    common = {
        "schema_version": "1.0.0",
        "node_id": claimed.node.node_id,
        "node_run_id": claimed.node.node_run_id,
        "attempt_id": claimed.attempt.attempt_id,
        "trace": claimed.node.trace,
        "output_artifacts": tuple(artifact.pin for artifact in artifacts),
        "started_at": started,
        "finished_at": finished,
        "duration_ms": elapsed // timedelta(milliseconds=1),
    }
    return (
        SucceededNodeOutput.model_validate(
            {**common, "status": NodeRunStatus.SUCCEEDED}
        )
        if error is None
        else FailedNodeOutput.model_validate(
            {**common, "status": NodeRunStatus.FAILED, "error": error}
        )
    )
