"""Host-managed executable bindings for already admitted Tool definitions."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from math import floor
from os.path import isabs
from pathlib import Path
from stat import S_IRUSR, S_ISREG, S_IXUSR
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.definition_enums import ToolProviderKind
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.policies import ResourceRequirements
from jimu_runtime.domain.primitives import ArtifactId, RecordId, Version
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import ConfiguredToolPin
from jimu_runtime.domain.runtime.facts import ObjectRef, ProfileContentRef
from jimu_runtime.domain.runtime.invocations import InvocationError, ModelInvocation
from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem, MediaRef
from jimu_runtime.domain.runtime.tool_executions import (
    ToolArtifactRefs,
    ToolError,
    ToolInvocationOrigin,
    ToolResourceUsage,
    ToolResult,
)
from jimu_runtime.domain.schema_types import ToolInputSchemaRef, ToolOutputSchemaRef
from jimu_runtime.workers.lifecycle import (
    IsolatedWorker,
    WorkerFailure,
    WorkerProcessError,
    WorkerStart,
)
from jimu_runtime.workers.ownership import acquire_with_compensation
from jimu_runtime.workers.resource_inventory import ResourceClass
from jimu_runtime.workers.resources import (
    ResourceAdmission,
    ResourceLease,
    ResourceUnavailable,
)
from jimu_runtime.workers.sealed_memory import create_artifact_memfd, seal_artifact_fd

if TYPE_CHECKING:
    from jimu_runtime.gateway.http_wire import VerifiedImage

type BeforeDispatch = Callable[["ToolDispatch", ResourceLease], Awaitable[bool]]
type SpatialResultCheck = Callable[["ToolDispatch", ToolResult, CoordinateSystem], None]
type PhysicalToolInputCheck = Callable[["ToolDispatch", bytes], None]
type PhysicalToolResultCheck = Callable[
    ["ToolDispatch", bytes, ToolResult, tuple[EvidenceAsset, ...]], None
]
type RemoteToolTransport = Callable[["ToolDispatch"], Awaitable["ToolWorkerResponse"]]
type NestedModelPort = Callable[
    ["NestedModelRequest"], Awaitable["NestedModelResponse"]
]


@dataclass(frozen=True, slots=True)
class CPUWorkerExchange:
    """Trusted host mediation of the supervised process wire."""

    prepare: Callable[["ToolDispatch"], bytes]
    materialize: Callable[
        ["ToolDispatch", bytes], Awaitable[tuple[bytes, tuple[EvidenceAsset, ...]]]
    ]


@dataclass(frozen=True, slots=True)
class NestedModelRequest:
    """Only the request data. The gateway fixes binding and causal identity."""

    prompt: str = field(repr=False)
    media: "VerifiedImage | None" = None


@dataclass(frozen=True, slots=True)
class NestedModelResponse:
    """A gateway result after verification against the durable dispatch receipt."""

    invocation: ModelInvocation | None
    content: bytes | None = field(repr=False)
    error: InvocationError | None


@dataclass(frozen=True, slots=True)
class ToolDispatch:
    tool_execution_id: RecordId
    execution_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    trace_id: RecordId
    profile_ref: ProfileContentRef
    object_ref: ObjectRef | None
    tool_ref: ToolContentRef
    origin: ToolInvocationOrigin
    capability_id: ArtifactId
    contract_version: Version
    implementation_version: Version
    implementation_proof_ref: TrustPin
    input_schema_ref: ToolInputSchemaRef
    output_schema_ref: ToolOutputSchemaRef
    input_refs: ToolArtifactRefs
    resources: ResourceRequirements
    request: bytes
    deadline: datetime
    invoke_model: NestedModelPort | None = None
    physical_request: bytes | None = field(default=None, repr=False)
    original_coordinates: CoordinateSystem | None = None
    object_bbox: BBoxXYXY | None = None
    original_media_ref: MediaRef | None = None
    task_id: RecordId | None = None
    object_member_boxes: tuple[BBoxXYXY, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolWorkerResponse:
    """A full bounded worker response, with independently attributable metadata."""

    content: bytes
    actual_implementation_version: Version | None
    actual_implementation_proof: TrustPin | None
    evidence: tuple[EvidenceAsset, ...] = ()
    nested_invocations: tuple[ModelInvocation, ...] = ()
    invocation_refs: ToolArtifactRefs = ()
    resource_usage: ToolResourceUsage | None = None
    terminal_evidence_id: RecordId | None = None


class ToolWorkerFailure(Exception):
    """A normalized worker failure with the observed dispatch boundary."""

    def __init__(
        self,
        error: ToolError,
        *,
        dispatched: bool,
        response: ToolWorkerResponse | None = None,
        observed_implementation: tuple[Version, TrustPin] | None = None,
    ) -> None:
        super().__init__(error.code)
        self.error = error
        self.dispatched = dispatched
        self.response = response
        self.observed_implementation = observed_implementation


class ToolWorkerPort(Protocol):
    async def run(
        self, dispatch: ToolDispatch, before_dispatch: BeforeDispatch
    ) -> ToolWorkerResponse:
        """Reserve, obtain journal clearance, and dispatch once.

        The worker owns bounded reading and cleanup of occupied resources.
        Full response bytes can accompany an observed process/protocol failure.
        """
        ...


async def _reserve(
    admission: ResourceAdmission,
    dispatch: ToolDispatch,
    scope_id: str,
    owner_id: str,
) -> ResourceLease:
    try:
        return await acquire_with_compensation(
            lambda: admission.reserve(dispatch.resources, scope_id, owner_id=owner_id),
            admission.release_reserved,
        )
    except ResourceUnavailable:
        raise ToolWorkerFailure(
            ToolError(
                error_class="resource_exhausted",
                code="tool_resource_unavailable",
                diagnostic_refs=(),
            ),
            dispatched=False,
        ) from None


async def _release(
    admission: ResourceAdmission,
    lease: ResourceLease,
    *,
    started: bool,
) -> None:
    with suppress(ResourceUnavailable):
        await asyncio.to_thread(
            admission.release if started else admission.release_reserved, lease
        )


class IsolatedCPUWorker:
    """Host-configured command with a supervised, byte-bounded process exchange."""

    def __init__(
        self,
        admission: ResourceAdmission,
        argv: tuple[str, ...],
        scope_id: str,
        owner_id: str,
        version: Version,
        proof: TrustPin,
        implementation_path: Path,
        *,
        mediated: bool = False,
    ) -> None:
        if (
            not argv
            or not isabs(argv[0])
            or any("\x00" in argument for argument in argv)
        ):
            raise ValueError("CPU Tool requires a trusted absolute command")
        self._admission = admission
        self._runner = IsolatedWorker(admission)
        self._argv = argv
        self._scope_id = scope_id
        self._owner_id = owner_id
        self._version = version
        self._proof = proof
        self.mediated = mediated
        if (
            str(implementation_path) not in argv
            or not implementation_path.is_absolute()
        ):
            raise ValueError(
                "CPU Tool proof must name an exact absolute command artifact"
            )
        self._implementation_path = implementation_path

    async def run(
        self, dispatch: ToolDispatch, before_dispatch: BeforeDispatch
    ) -> ToolWorkerResponse:
        if dispatch.resources.resource_class != "cpu" or (
            dispatch.implementation_version,
            dispatch.implementation_proof_ref,
        ) != (self._version, self._proof):
            raise ToolWorkerFailure(
                ToolError(
                    error_class="association", code="tool_cpu_pin", diagnostic_refs=()
                ),
                dispatched=False,
            )
        if self.mediated != (dispatch.physical_request is not None):
            raise ToolWorkerFailure(
                ToolError(
                    error_class="association",
                    code="tool_cpu_exchange_unavailable",
                    diagnostic_refs=(),
                ),
                dispatched=False,
            )
        try:
            with_fd = await acquire_with_compensation(self._sealed_artifact, os.close)
        except OSError:
            raise ToolWorkerFailure(
                ToolError(
                    error_class="unavailable",
                    code="tool_cpu_artifact_changed",
                    diagnostic_refs=(),
                ),
                dispatched=False,
            ) from None
        lease: ResourceLease | None = None
        try:
            lease = await _reserve(
                self._admission, dispatch, self._scope_id, self._owner_id
            )
            if not await before_dispatch(dispatch, lease):
                raise ToolWorkerFailure(
                    ToolError(
                        error_class="cancelled",
                        code="tool_dispatch_denied",
                        diagnostic_refs=(),
                    ),
                    dispatched=False,
                )
            remaining_ms = floor(
                (dispatch.deadline - datetime.now(UTC)).total_seconds() * 1000
            )
            if remaining_ms <= 0:
                raise ToolWorkerFailure(
                    ToolError(
                        error_class="timeout",
                        code="tool_deadline_elapsed",
                        diagnostic_refs=(),
                    ),
                    dispatched=False,
                )
            content = await self._runner.run(
                lease,
                tuple(
                    f"/proc/self/fd/{with_fd}"
                    if value == str(self._implementation_path)
                    else value
                    for value in self._argv
                ),
                dispatch.request
                if dispatch.physical_request is None
                else dispatch.physical_request,
                timeout_ms=min(remaining_ms, lease.timeout_ms),
                output_byte_limit=lease.output_byte_limit,
                pass_fds=(with_fd,),
            )
        except WorkerProcessError as exc:
            error = ToolError.model_validate(
                {
                    "error_class": {
                        WorkerFailure.TIMEOUT: "timeout",
                        WorkerFailure.INTERRUPTED: "transport",
                        WorkerFailure.OUTPUT_LIMIT: "validation",
                        WorkerFailure.INVALID_BUDGET: "validation",
                        WorkerFailure.START_FAILED: "unavailable",
                        WorkerFailure.PROCESS_FAILED: "internal",
                    }[exc.reason],
                    "code": f"tool_worker_{exc.reason.value}",
                    "diagnostic_refs": (),
                }
            )
            observed = (
                (self._version, self._proof)
                if exc.start_state is WorkerStart.STARTED
                else None
            )
            raise ToolWorkerFailure(
                error,
                dispatched=exc.dispatched,
                response=(
                    None
                    if exc.output is None
                    else ToolWorkerResponse(
                        exc.output,
                        self._version if observed else None,
                        self._proof if observed else None,
                    )
                ),
                observed_implementation=observed,
            ) from None
        finally:
            # A reservation without a physical launch can be abandoned here.
            # Active process capacity remains the supervisor's responsibility.
            os.close(with_fd)
            if lease is not None:
                await _release(self._admission, lease, started=False)
        return ToolWorkerResponse(content, self._version, self._proof)

    def _sealed_artifact(self) -> int:
        descriptor = create_artifact_memfd()
        try:
            digest = sha256()
            with (
                os.fdopen(
                    os.open(self._implementation_path, os.O_RDONLY | os.O_NONBLOCK),
                    "rb",
                ) as artifact,
                os.fdopen(os.dup(descriptor), "wb") as snapshot,
            ):
                if not S_ISREG(os.fstat(artifact.fileno()).st_mode):
                    raise OSError("CPU Tool artifact must be a regular file")
                while chunk := artifact.read(1024 * 1024):
                    digest.update(chunk)
                    snapshot.write(chunk)
            if digest.hexdigest() != self._proof.sha256:
                raise OSError("CPU Tool artifact did not match its registered digest")
            os.fchmod(descriptor, S_IRUSR | S_IXUSR)
            os.lseek(descriptor, 0, os.SEEK_SET)
            seal_artifact_fd(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


@dataclass(frozen=True, slots=True)
class RemoteToolWorker:
    """Reserve a remote Tool through the host's bounded transport callback."""

    admission: ResourceAdmission
    scope_id: str
    owner_id: str
    send: RemoteToolTransport

    async def run(
        self, dispatch: ToolDispatch, before_dispatch: BeforeDispatch
    ) -> ToolWorkerResponse:
        if dispatch.resources.resource_class != "remote":
            raise ToolWorkerFailure(
                ToolError(
                    error_class="association",
                    code="tool_remote_resource",
                    diagnostic_refs=(),
                ),
                dispatched=False,
            )
        lease = await _reserve(self.admission, dispatch, self.scope_id, self.owner_id)
        dispatch_id = dispatch.tool_execution_id
        deadline = monotonic() + (dispatch.deadline - datetime.now(UTC)).total_seconds()
        armed = False
        activated = False

        async def settle(response: ToolWorkerResponse | None) -> None:
            if response is not None and response.terminal_evidence_id is not None:
                with suppress(ResourceUnavailable, ValueError):
                    await asyncio.to_thread(
                        self.admission.settle_provider,
                        lease.lease_id,
                        dispatch_id,
                        response.terminal_evidence_id,
                    )

        try:
            if not await before_dispatch(dispatch, lease):
                raise ToolWorkerFailure(
                    ToolError(
                        error_class="cancelled",
                        code="tool_dispatch_denied",
                        diagnostic_refs=(),
                    ),
                    dispatched=False,
                )
            try:
                await acquire_with_compensation(
                    lambda: self.admission.prepare_remote_send(
                        lease, dispatch_id, deadline=deadline
                    ),
                    lambda _: self.admission.abort_remote_before_send(
                        lease, dispatch_id
                    ),
                )
                armed = True
                if monotonic() >= deadline:
                    raise ToolWorkerFailure(
                        ToolError(
                            error_class="timeout",
                            code="tool_deadline_elapsed",
                            diagnostic_refs=(),
                        ),
                        dispatched=False,
                    )
                await acquire_with_compensation(
                    lambda: self.admission.activate_remote(
                        lease, dispatch_id, deadline=deadline
                    ),
                    lambda _: self.admission.release(lease),
                )
            except ResourceUnavailable:
                if monotonic() >= deadline:
                    raise ToolWorkerFailure(
                        ToolError(
                            error_class="timeout",
                            code="tool_deadline_elapsed",
                            diagnostic_refs=(),
                        ),
                        dispatched=False,
                    ) from None
                raise ToolWorkerFailure(
                    ToolError(
                        error_class="resource_exhausted",
                        code="tool_remote_lease_inactive",
                        diagnostic_refs=(),
                    ),
                    dispatched=False,
                ) from None
            activated = True
            if monotonic() >= deadline:
                raise ToolWorkerFailure(
                    ToolError(
                        error_class="timeout",
                        code="tool_deadline_elapsed",
                        diagnostic_refs=(),
                    ),
                    dispatched=False,
                )
            # The trusted transport owns streaming bounds and cancellation.
            try:
                response = await self.send(dispatch)
            except ToolWorkerFailure as exc:
                if exc.dispatched:
                    await settle(exc.response)
                raise
            await settle(response)
            return response
        finally:
            if armed and not activated:
                with suppress(ResourceUnavailable):
                    await asyncio.to_thread(
                        self.admission.abort_remote_before_send, lease, dispatch_id
                    )
            else:
                await _release(self.admission, lease, started=activated)


@dataclass(frozen=True, slots=True)
class ToolImplementation:
    capability_id: ArtifactId
    contract_version: Version
    version: Version
    proof: TrustPin
    provider_kind: ToolProviderKind
    input_schema_ref: ToolInputSchemaRef
    output_schema_ref: ToolOutputSchemaRef
    resource_class: ResourceClass
    worker: ToolWorkerPort
    spatial_result_check: SpatialResultCheck | None = None
    physical_input_check: PhysicalToolInputCheck | None = None
    physical_result_check: PhysicalToolResultCheck | None = None


class ToolWorkerRegistry:
    """An immutable registry constructed by the host rather than from Bundle data."""

    def __init__(self, entries: tuple[ToolImplementation, ...]) -> None:
        index: dict[tuple[str, ...], ToolImplementation] = {}
        for entry in entries:
            key = (entry.capability_id, entry.contract_version, entry.version)
            if key in index:
                raise ValueError("ambiguous Tool implementation registration")
            index[key] = entry
        self._entries = MappingProxyType(index)

    def resolve(
        self,
        tool_ref: ToolContentRef,
        definition: ToolDefinition,
        pin: ConfiguredToolPin,
        resource_class: ResourceClass,
        expected_proof: TrustPin,
    ) -> ToolImplementation:
        requirement = definition.implementation_requirement
        key = (
            requirement.capability_id,
            requirement.contract_version,
            pin.implementation_version,
        )
        entry = self._entries.get(key)
        if (
            pin.tool_ref != tool_ref
            or (tool_ref.artifact_id, tool_ref.version)
            != (definition.tool_id, definition.version)
            or pin.implementation_version
            not in requirement.allowed_implementation_versions
            or entry is None
            or entry.proof != expected_proof
            or (
                entry.provider_kind,
                entry.input_schema_ref,
                entry.output_schema_ref,
                entry.resource_class,
            )
            != (
                definition.provider_kind,
                definition.input_schema_ref,
                definition.output_schema_ref,
                resource_class,
            )
        ):
            raise LookupError("the exact pinned Tool implementation is unavailable")
        return entry
