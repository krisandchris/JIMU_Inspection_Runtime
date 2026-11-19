"""Transport contract and audit projection for one physical model dispatch."""

from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from math import isfinite
from typing import Literal, Protocol, cast

from pydantic import TypeAdapter

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import (
    NonNegativeBudget,
    ShortText,
    UTCDateTime,
)
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.invocations import (
    ActualProviderIdentity,
    FailedInvocation,
    FallbackProviderRoute,
    InvocationError,
    InvocationNodeId,
    InvocationOutcome,
    ModelInvocation,
    RegistryVerifiedProviderIdentity,
    ReportedProviderIdentity,
    SucceededInvocation,
    UnavailableProviderIdentity,
    UndispatchedInvocation,
)
from jimu_runtime.domain.validation.invocations import (
    NestedModelStepFallbackWitness,
    validate_model_invocation,
)
from jimu_runtime.gateway.routing import PlannedRoute

type DispatchBoundary = Literal["not_dispatched", "sent_unknown", "acknowledged"]
type ResponseBytesOmissionReason = Literal[
    "incomplete", "policy_disabled", "credential_echo", "unvalidated"
]

_RECORD_ID = TypeAdapter[RecordId](RecordId)
_SLOT_ID = TypeAdapter[ArtifactId](ArtifactId)
_BYTE_COUNT = TypeAdapter[NonNegativeBudget](NonNegativeBudget)
_DIGEST = TypeAdapter[Digest](Digest)
_UTC = TypeAdapter[UTCDateTime](UTCDateTime)
_OMISSION_REASON = TypeAdapter[ResponseBytesOmissionReason](ResponseBytesOmissionReason)


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    dispatch_id: RecordId
    route: PlannedRoute
    payload: bytes = field(repr=False)
    pre_send_allowed: Callable[[], Awaitable[bool]] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _RECORD_ID.validate_python(self.dispatch_id)
        if self.pre_send_allowed is not None and not callable(self.pre_send_allowed):
            raise ValueError("dispatch authorization hook must be a trusted callable")
        if type(self.payload) is not bytes:
            raise ValueError("dispatch needs immutable exact wire bytes")
        if len(self.payload) > self.route.resource_requirements.input_byte_limit:
            raise ValueError("dispatch exceeds its pinned input byte limit")
        if any(
            self.dispatch_id == entry.invocation.model_invocation_id
            for entry in self.route.history
        ):
            raise ValueError("dispatch ID must differ from all previous invocations")

    @property
    def request_digest(self) -> Digest:
        return sha256_digest(self.payload)


class ProviderObservation(ContractModel):
    """Bounded, sanitized fields observed by the trusted transport."""

    provider_request_id: RecordId | None = None
    model_id: ShortText | None = None
    deployment_version: Version | None = None
    model_version: Version | None = None


@dataclass(frozen=True, slots=True)
class TransportResult:
    """Physical observation attributed to the configured peer by a trusted transport.

    Observed response bytes count peer response data. Bytes read before the
    protocol attributes a response to that peer are excluded. Only a transport
    that checked the selected schema may attach its digest to parsed content.
    """

    dispatched: bool
    error: InvocationError | None = None
    observation: ProviderObservation | None = None
    response_bytes: bytes | None = field(default=None, repr=False)
    parsed_content: bytes | None = field(default=None, repr=False)
    retry_after_seconds: float | None = None
    response_status: int | None = None
    observed_response_bytes: int | None = None
    response_digest: Digest | None = None
    response_bytes_omission_reason: ResponseBytesOmissionReason | None = None
    validated_response_contract_digest: Digest | None = None
    invalid_question_slots: tuple[ArtifactId, ...] = ()
    missing_question_slots: tuple[ArtifactId, ...] = ()
    normalized_response_digest: Digest | None = None
    normalized_response_bytes: int | None = None
    response_dispatch_id: RecordId | None = None

    def __post_init__(self) -> None:
        if type(self.dispatched) is not bool:
            raise ValueError("physical dispatch flag must be explicit")
        if any(
            value is not None and type(value) is not bytes
            for value in (self.response_bytes, self.parsed_content)
        ):
            raise ValueError("response bytes must be immutable")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) not in (int, float)
            or not isfinite(self.retry_after_seconds)
            or self.retry_after_seconds < 0
        ):
            raise ValueError("retry delay must be finite and nonnegative")
        if self.error is not None and not isinstance(self.error, InvocationError):
            raise ValueError("transport error must be normalized")
        if self.observation is not None and not isinstance(
            self.observation, ProviderObservation
        ):
            raise ValueError("transport observation must be normalized")
        if self.response_bytes_omission_reason is not None:
            _OMISSION_REASON.validate_python(self.response_bytes_omission_reason)
        if (
            type(self.invalid_question_slots) is not tuple
            or type(self.missing_question_slots) is not tuple
            or len(self.invalid_question_slots) + len(self.missing_question_slots)
            > 1024
        ):
            raise ValueError("question slot diagnostics must be immutable and bounded")
        if self.validated_response_contract_digest is not None:
            _DIGEST.validate_python(self.validated_response_contract_digest)
        if self.response_dispatch_id is not None:
            _RECORD_ID.validate_python(self.response_dispatch_id)
        if (self.normalized_response_digest is None) != (
            self.normalized_response_bytes is None
        ):
            raise ValueError(
                "normalized response identity requires both digest and byte size"
            )
        if self.normalized_response_digest is not None:
            _DIGEST.validate_python(self.normalized_response_digest)
            size = _BYTE_COUNT.validate_python(self.normalized_response_bytes)
            if (
                size <= 0
                or self.response_digest is None
                or self.response_status is None
                or not 200 <= self.response_status <= 299
                or self.error is not None
                or self.parsed_content is not None
                and (
                    size != len(self.parsed_content)
                    or self.normalized_response_digest
                    != sha256_digest(self.parsed_content)
                )
            ):
                raise ValueError(
                    "normalized response identity differs from the trusted parser output"
                )
        if self.parsed_content is not None and self.response_bytes_omission_reason in {
            "credential_echo",
            "incomplete",
            "unvalidated",
        }:
            raise ValueError(
                "unusable or credential-bearing content cannot be retained as parsed output"
            )
        failed_question_slots = (
            *self.invalid_question_slots,
            *self.missing_question_slots,
        )
        if failed_question_slots:
            if (
                len(set(failed_question_slots)) != len(failed_question_slots)
                or self.parsed_content is None
                or self.validated_response_contract_digest is None
                or self.error is not None
            ):
                raise ValueError(
                    "question slot diagnostics need disjoint identities and validated batch content"
                )
            for slot in failed_question_slots:
                _SLOT_ID.validate_python(slot)
        if self.response_digest is not None:
            _DIGEST.validate_python(self.response_digest)
        if (
            self.response_bytes is not None
            and self.response_bytes_omission_reason is not None
        ):
            raise ValueError("retained response bytes have no omission reason")
        if (
            self.response_bytes_omission_reason == "incomplete"
            and self.response_digest is not None
        ):
            raise ValueError("incomplete response cannot have a complete digest")
        if self.response_bytes is not None and (
            self.response_digest is not None
            and sha256_digest(self.response_bytes) != self.response_digest
        ):
            raise ValueError("complete response digest disagrees with response bytes")
        if self.observed_response_bytes is not None:
            _BYTE_COUNT.validate_python(self.observed_response_bytes)
        if self.response_bytes is not None and (
            self.observed_response_bytes is not None
            and len(self.response_bytes) != self.observed_response_bytes
        ):
            raise ValueError(
                "complete response bytes disagree with observed response length"
            )
        if self.response_status is not None:
            if (
                type(self.response_status) is not int
                or not 100 <= self.response_status <= 599
            ):
                raise ValueError("observed response status is invalid")
            if not 200 <= self.response_status <= 299 and self.error is None:
                raise ValueError(
                    "nonsuccess response status requires a normalized error"
                )
        if not self.dispatched and (
            self.error is None
            or any(
                value is not None
                for value in (
                    self.observation,
                    self.response_bytes,
                    self.parsed_content,
                    self.retry_after_seconds,
                    self.response_status,
                    self.observed_response_bytes,
                    self.response_digest,
                    self.response_bytes_omission_reason,
                    self.validated_response_contract_digest,
                    self.normalized_response_digest,
                    self.normalized_response_bytes,
                )
            )
        ):
            raise ValueError(
                "pre-send rejection requires an error and has no physical response"
            )


@dataclass(frozen=True, slots=True)
class TransportCancelled:
    """An observed transport receipt attached to an unchanged cancellation."""

    receipt: TransportResult

    def __post_init__(self) -> None:
        if (
            self.receipt.error is None
            or self.receipt.error.error_class != "cancelled"
            or self.receipt.response_bytes is not None
            or self.receipt.parsed_content is not None
        ):
            raise ValueError(
                "cancellation requires a safe normalized transport receipt"
            )

    def attach(self, error: CancelledError) -> None:
        # Preserve the exact CancelledError type: asyncio.timeout on Python
        # 3.12 only maps that exact exception class to TimeoutError.
        vars(error)["_jimu_transport_cancellation"] = self

    @staticmethod
    def observe(error: CancelledError) -> TransportResult | None:
        observed: object = vars(error).get("_jimu_transport_cancellation")
        return observed.receipt if isinstance(observed, TransportCancelled) else None


class ModelTransport(Protocol):
    """Attempt one send, first consulting the host's pre-send authorization hook.

    This hook is invoked immediately before network I/O after local validation
    or queuing. Cancellation propagates to the journal owner.
    """

    async def dispatch(self, request: DispatchRequest) -> TransportResult: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    request_digest: Digest
    response_digest: Digest | None
    boundary: DispatchBoundary
    error: InvocationError | None
    transport_result: TransportResult
    invocation: ModelInvocation | None
    output: bytes | None = field(default=None, repr=False)
    late: bool = False
    dispatch_id: RecordId | None = None


def _error(
    error_class: Literal["association", "validation", "internal"], code: ArtifactId
) -> InvocationError:
    return InvocationError(error_class=error_class, code=code, diagnostic_refs=())


def _identity(
    route: PlannedRoute,
    seen: ProviderObservation | None,
) -> tuple[ActualProviderIdentity | None, InvocationError | None]:
    selected = route.selected
    expected = route.ownership
    if seen is not None and any(
        actual is not None and actual != pinned
        for actual, pinned in (
            (seen.model_id, expected.model_id),
            (seen.deployment_version, selected.deployment_version),
            (seen.model_version, selected.model_version),
        )
    ):
        return None, _error("association", "model.actual_identity_mismatch")
    observed_id = seen.model_id if seen is not None else None
    if (
        seen is not None
        and observed_id is not None
        and seen.provider_request_id is not None
        and seen.deployment_version is not None
        and seen.model_version is not None
    ):
        return ReportedProviderIdentity(
            provider_kind=selected.provider_kind,
            provider_id=selected.provider_id,
            deployment_id=selected.deployment_id,
            model_id=observed_id,
            version_resolution="reported",
            version_evidence=seen.provider_request_id,
            deployment_version=seen.deployment_version,
            model_version=seen.model_version,
        ), None
    if expected.version_evidence is not None:
        return RegistryVerifiedProviderIdentity(
            provider_kind=selected.provider_kind,
            provider_id=selected.provider_id,
            deployment_id=selected.deployment_id,
            model_id=expected.model_id,
            version_resolution="registry_verified",
            version_evidence=expected.version_evidence,
            deployment_version=selected.deployment_version,
            model_version=selected.model_version,
        ), None
    if observed_id is not None:
        return UnavailableProviderIdentity(
            provider_kind=selected.provider_kind,
            provider_id=selected.provider_id,
            deployment_id=selected.deployment_id,
            model_id=observed_id,
            version_resolution="unavailable",
            version_unavailable_reason="Exact actual provider versions lack trusted evidence.",
        ), _error("association", "model.actual_version_unavailable")
    return None, _error("association", "model.actual_identity_unavailable")


def _model_record(
    request: DispatchRequest,
    outcome: InvocationOutcome,
    actual: ActualProviderIdentity | None,
    observation: ProviderObservation | None,
    started_at: UTCDateTime,
    completed_at: UTCDateTime,
    evidence_refs: tuple[EvidenceRef, ...],
) -> ModelInvocation:
    route = request.route
    operation = route.operation
    optional = {
        key: value
        for key, value in (
            ("actual_provider", actual),
            (
                "provider_request_id",
                observation.provider_request_id if observation is not None else None,
            ),
            ("object_ref", operation.object_ref),
            (
                "source_discovery_invocation_ref",
                operation.source_discovery_invocation_ref,
            ),
            ("contract_snapshot", operation.contract_snapshot),
        )
        if value is not None
    }
    record = ModelInvocation.model_validate(
        {
            "schema_version": "1.0.0",
            "model_invocation_id": request.dispatch_id,
            "execution_id": operation.node_run.execution_id,
            "node_id": cast(InvocationNodeId, operation.node_run.node_id),
            "node_run_ref": operation.node_run.node_run_ref,
            "attempt_ref": route.attempt.attempt_ref,
            "configured_provider": route.configured,
            "route_trace": route.trace,
            "purposes": operation.purposes,
            "profile_refs": operation.profile_refs,
            "outcome": outcome,
            "request_digest": request.request_digest,
            "started_at": started_at,
            "completed_at": completed_at,
            "evidence_refs": evidence_refs,
            "trace_id": operation.trace_id,
            **optional,
        }
    )
    witness = None
    if route.nested_step_owner is not None and isinstance(
        route.trace, FallbackProviderRoute
    ):
        prior_owner = next(
            (
                entry.nested_step_owner
                for entry in route.history
                if entry.invocation.model_invocation_id
                == route.trace.trigger_invocation_ref.record_id
            ),
            None,
        )
        if prior_owner is None:
            raise ValueError(
                "fallback projection requires its exact nested source owner"
            )
        witness = NestedModelStepFallbackWitness(prior_owner, route.nested_step_owner)
    validate_model_invocation(
        record,
        node_run=operation.node_run,
        attempt=route.attempt,
        binding=route.policy.binding,
        provider_policy=route.policy.provider_registration,
        fallback_policy=route.policy.fallback_registration,
        prior_invocations=tuple(entry.invocation for entry in route.history),
        reviews=route.reviews,
        region_ref=operation.region_ref,
        resolved_deployments=route.policy.resolved.deployments,
        nested_step_fallback=witness,
    )
    return record


def _question_slots_agree(route: PlannedRoute, result: TransportResult) -> bool:
    slots = set(result.invalid_question_slots) | set(result.missing_question_slots)
    if not slots:
        return True
    snapshot = route.operation.contract_snapshot
    return snapshot is not None and slots.issubset(
        ref.artifact_id for ref in snapshot.effective_question_refs
    )


def project_dispatch(
    request: DispatchRequest,
    result: TransportResult,
    *,
    started_at: UTCDateTime,
    completed_at: UTCDateTime,
    evidence_refs: tuple[EvidenceRef, ...] = (),
) -> DispatchOutcome:
    """Build the immutable audit record; only validated, versioned output is usable."""
    response_digest = result.response_digest
    if result.response_bytes is not None and response_digest is None:
        response_digest = sha256_digest(result.response_bytes)
    observation = result.observation
    error = result.error
    actual = None
    if not result.dispatched:
        boundary: DispatchBoundary = "not_dispatched"
    else:
        boundary = (
            "acknowledged"
            if result.response_status is not None
            or response_digest is not None
            or (result.observed_response_bytes or 0) > 0
            or (observation is not None and observation.provider_request_id is not None)
            else "sent_unknown"
        )
        actual, identity_error = _identity(request.route, observation)
        if not request.route.authorization_active(started_at):
            error = _error("association", "model.route_authorization_expired")
        elif error is None:
            error = identity_error
        if error is None:
            if response_digest is None or result.parsed_content is None:
                error = _error("validation", "model.validated_output_missing")
            elif (
                result.validated_response_contract_digest
                != request.route.operation.response_contract_digest
            ):
                error = _error("validation", "model.response_contract_unverified")
            elif not _question_slots_agree(request.route, result):
                error = _error("validation", "model.question_slot_scope_invalid")

    invocation = None
    if not result.dispatched:
        assert error is not None
        outcome: InvocationOutcome = UndispatchedInvocation(
            status="not_dispatched", error=error
        )
    elif error is not None:
        outcome = FailedInvocation(status="failed", error=error)
    else:
        assert response_digest is not None
        outcome = SucceededInvocation(
            status="succeeded", response_digest=response_digest
        )
    if not result.dispatched or actual is not None:
        try:
            started_at = _UTC.validate_python(started_at)
            completed_at = _UTC.validate_python(completed_at)
            if started_at < request.route.approved_at:
                raise ValueError("physical dispatch cannot precede its route approval")
            invocation = _model_record(
                request,
                outcome,
                actual,
                observation,
                started_at,
                completed_at,
                evidence_refs,
            )
        except ValueError:
            # A physical observation can outlive incompatible or revoked audit inputs.
            if error is None:
                error = _error("association", "model.invocation_association_invalid")
    retain_output = invocation is not None and error is None
    if not retain_output and result.parsed_content is not None:
        result = replace(
            result,
            parsed_content=None,
            validated_response_contract_digest=None,
            invalid_question_slots=(),
            missing_question_slots=(),
        )
    return DispatchOutcome(
        request_digest=request.request_digest,
        response_digest=response_digest,
        boundary=boundary,
        error=error,
        transport_result=result,
        invocation=invocation,
        output=result.parsed_content if retain_output else None,
        dispatch_id=request.dispatch_id,
    )
