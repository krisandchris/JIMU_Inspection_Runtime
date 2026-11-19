"""Shared, offline scope and artifact checks for the synthetic inspection adapter."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import TypeAdapter

from jimu_runtime.bundle.catalogue import ArtifactCatalogue, build_catalogue
from jimu_runtime.bundle.integrity import VerifiedBundleFiles, sha256_digest
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import UTCDateTime, reference_identity
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import JSONValue, canonical_bytes
from jimu_runtime.domain.nodes.envelopes import (
    ArtifactOutputRef,
    FailedNodeOutput,
    NodeError,
    NodeInputEnvelope,
    NodeOutputEnvelope,
    SucceededNodeOutput,
)
from jimu_runtime.domain.policies import (
    ExecutionPolicyArtifact,
    ProviderPolicyArtifact,
    RemotePrimaryProviderPolicy,
)
from jimu_runtime.domain.references import ContentRef, RuntimeRecordRef
from jimu_runtime.domain.runtime.applicability import ContextSnapshotRef
from jimu_runtime.domain.runtime.executions import ExecutionPins, NodeId
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    EvidenceRef,
    NodeRunRef,
    ObjectRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask, ContextSnapshot
from jimu_runtime.domain.runtime.invocations import (
    ConfiguredProviderIdentity,
    FailedInvocation,
    InvocationContractSnapshot,
    InvocationError,
    InvocationNodeId,
    ModelInvocation,
    PrimaryProviderRoute,
    ReportedProviderIdentity,
    SucceededInvocation,
)
from jimu_runtime.domain.runtime.media import MediaAsset
from jimu_runtime.domain.schema_types import RawDtoSchemaDescriptor, SchemaDescriptor
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.validation.review_inputs import ReviewedDeployment
from jimu_runtime.domain.validation.schema_descriptors import resolve_descriptor
from jimu_runtime.domain.values import (
    BooleanValue,
    IntegerValue,
    ListValue,
    NumberValue,
    RecordValue,
    StringValue,
    Value,
    validate_typed_value,
)

VERSION: Literal["1.0.0"] = "1.0.0"

K = RuntimeRecordKind


def _fixture_dto(value: JSONValue) -> Value:
    if isinstance(value, dict):
        return RecordValue(
            kind="record",
            value={key: _fixture_dto(child) for key, child in value.items()},
        )
    if isinstance(value, list):
        return ListValue(
            kind="list", value=tuple(_fixture_dto(child) for child in value)
        )
    if isinstance(value, bool):
        return BooleanValue(kind="bool", value=value)
    if isinstance(value, int):
        return IntegerValue(kind="integer", value=value)
    if isinstance(value, float):
        return NumberValue(kind="number", value=value)
    if isinstance(value, str):
        return StringValue(kind="string", value=value)
    raise ValueError("synthetic response must use the admitted non-null DTO types")


def synthetic_id(kind: str, *parts: str) -> str:
    return f"synthetic.{kind}.{sha256_digest(canonical_bytes(list(parts)))}"


def record_digest(record: ContractModel) -> str:
    return sha256_digest(canonical_bytes(record.model_dump(mode="json")))


@dataclass(frozen=True, slots=True)
class SyntheticArtifact:
    ref: RuntimeRecordRef
    record: ContractModel

    @property
    def pin(self) -> ArtifactOutputRef:
        return ArtifactOutputRef(ref=self.ref, digest=record_digest(self.record))


def artifact(
    kind: RuntimeRecordKind, record_id: str, record: ContractModel
) -> SyntheticArtifact:
    return SyntheticArtifact(
        RuntimeRecordRef(record_kind=kind, record_id=record_id), record
    )


def synthetic_issue(
    code: str, message: str, path: tuple[str | int, ...] = ()
) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path, related_refs=())


@dataclass(frozen=True, slots=True)
class SyntheticCall:
    envelope: NodeInputEnvelope
    at: datetime

    def id(self, kind: str, *parts: str) -> str:
        env = self.envelope
        return synthetic_id(
            kind, env.task_id, env.execution_id, env.node_run_id, env.attempt_id, *parts
        )

    @property
    def node_run_ref(self) -> NodeRunRef:
        return NodeRunRef(record_kind=K.NODE_RUN, record_id=self.envelope.node_run_id)

    @property
    def attempt_ref(self) -> AttemptRef:
        return AttemptRef(record_kind=K.ATTEMPT, record_id=self.envelope.attempt_id)

    def output(
        self,
        artifacts: tuple[SyntheticArtifact, ...],
        failure: ValidationIssue | None = None,
    ) -> NodeOutputEnvelope:
        env = self.envelope
        common = {
            "schema_version": VERSION,
            "node_id": env.node_id,
            "node_run_id": env.node_run_id,
            "attempt_id": env.attempt_id,
            "trace": env.trace,
            "output_artifacts": tuple(item.pin for item in artifacts),
            "started_at": self.at,
            "finished_at": self.at,
            "duration_ms": 0,
        }
        if failure is None:
            return SucceededNodeOutput.model_validate(
                {**common, "status": NodeRunStatus.SUCCEEDED}
            )
        return FailedNodeOutput.model_validate(
            {
                **common,
                "status": NodeRunStatus.FAILED,
                "error": NodeError(
                    code=failure.code,
                    error_class="validation",
                    retryable=False,
                    message=failure.message,
                    validation_issues=(failure,),
                ),
            }
        )

    def check_upstream(self, *artifacts: SyntheticArtifact) -> None:
        pins = {item.ref: item.digest for item in self.envelope.upstream_artifacts}
        for item in artifacts:
            if pins.get(item.ref) != item.pin.digest:
                raise ValueError("synthetic input must pin the exact upstream artifact")


@dataclass(frozen=True, slots=True)
class VerifiedExecutionScope:
    """Exact accepted input, verified media metadata and immutable configuration."""

    task: AcceptedTask
    execution_id: str
    pins: ExecutionPins
    media: MediaAsset
    files: VerifiedBundleFiles
    max_file_bytes: int
    max_json_depth: int
    catalogue: ArtifactCatalogue = field(init=False, repr=False)
    schema_descriptors: tuple[SchemaDescriptor, ...] = field(init=False, repr=False)
    execution_config_ref: ContentRef = field(init=False)

    def __post_init__(self) -> None:
        self.task.validate_media(self.media)
        if (
            self.pins.release_id != self.task.release_id
            or self.files.manifest.integrity.root_digest != self.task.bundle_digest
            or self.pins.bundle_digest != self.task.bundle_digest
        ):
            raise ValueError(
                "execution scope requires the accepted task's exact Release and Bundle"
            )
        catalogue = build_catalogue(
            self.files.manifest,
            self.files.payloads,
            max_bytes=self.max_file_bytes,
            max_depth=self.max_json_depth,
        )
        (config,) = tuple(
            entry
            for entry in catalogue.artifacts
            if entry.inventory.path == "policies/execution.json"
            and isinstance(entry.value, ExecutionPolicyArtifact)
        )
        if not isinstance(config.value, ExecutionPolicyArtifact) or (
            config.value.policy.workflow_version != self.pins.workflow_version
        ):
            raise ValueError("workflow version must match its exact Bundle policy")
        object.__setattr__(self, "catalogue", catalogue)
        object.__setattr__(self, "execution_config_ref", config.ref)
        object.__setattr__(
            self, "schema_descriptors", build_schema_descriptors(catalogue)
        )

    def resolve[T: ContractModel](self, ref: ContentRef, model: type[T]) -> T:
        entry = self.catalogue.resolve(ref)
        if not isinstance(entry.value, model):
            raise ValueError(
                f"wrong contract for exact pinned artifact {ref.artifact_id}"
            )
        return entry.value

    def check_call(
        self, envelope: NodeInputEnvelope, node: NodeId, at: datetime
    ) -> SyntheticCall:
        NodeInputEnvelope.model_validate(envelope)
        TypeAdapter[UTCDateTime](UTCDateTime).validate_python(at)
        if (
            envelope.node_id,
            envelope.execution_id,
            envelope.task_id,
            envelope.release_id,
            envelope.bundle_digest,
            envelope.trace.trace_id,
            envelope.node_config_ref,
        ) != (
            node,
            self.execution_id,
            self.task.task_id,
            self.pins.release_id,
            self.pins.bundle_digest,
            self.task.trace.trace_id,
            self.execution_config_ref,
        ) or at < self.task.accepted_at:
            raise ValueError(
                "node call has a foreign execution, task, trace or Bundle pin"
            )
        return SyntheticCall(envelope, at)

    def check_snapshot(
        self, snapshot: ContextSnapshot, ref: ContextSnapshotRef
    ) -> None:
        ContextSnapshot.model_validate(snapshot)
        region = self.resolve(snapshot.region_ref, RegionDefinition)
        config = self.resolve(self.execution_config_ref, ExecutionPolicyArtifact).policy
        if (
            snapshot.region_ref.artifact_id != self.task.context.region_id
            or snapshot.device_ref.record_id != self.task.context.device_id
            or snapshot.camera_ref.record_id != self.task.context.camera_id
            or snapshot.clock != self.task.context.clock
            or reference_identity(snapshot.context_schema_ref)
            != reference_identity(region.context_schema_ref)
            or reference_identity(snapshot.context_provenance_policy_ref)
            != reference_identity(config.context_provenance_policy_ref)
        ):
            raise ValueError(
                "ContextSnapshot must preserve the pinned Region, clock, device and camera"
            )
        if (
            snapshot.context_snapshot_id,
            snapshot.execution_id,
            snapshot.task_id,
            snapshot.release_id,
            snapshot.bundle_digest,
            snapshot.media_ref,
        ) != (
            ref.record_id,
            self.execution_id,
            self.task.task_id,
            self.pins.release_id,
            self.pins.bundle_digest,
            self.media.media_ref,
        ):
            raise ValueError(
                "node requires the selected ContextSnapshot within its scope"
            )


@dataclass(frozen=True, slots=True, init=False)
class SyntheticExecution(VerifiedExecutionScope):
    """The verified scope plus the fixture-only provider configuration."""

    deployment: ReviewedDeployment

    def __init__(
        self,
        task: AcceptedTask,
        execution_id: str,
        pins: ExecutionPins,
        media: MediaAsset,
        files: VerifiedBundleFiles,
        deployment: ReviewedDeployment,
        max_file_bytes: int,
        max_json_depth: int,
    ) -> None:
        if not deployment.provider_id.startswith("fixture."):
            raise ValueError(
                "synthetic adapter requires an explicitly designated fixture deployment"
            )
        object.__setattr__(self, "deployment", deployment)
        VerifiedExecutionScope.__init__(
            self, task, execution_id, pins, media, files, max_file_bytes, max_json_depth
        )

    def response_digest(self, binding_ref: ContentRef, response: JSONValue) -> str:
        binding = self.resolve(binding_ref, ModelBinding)
        descriptor = resolve_descriptor(
            binding.output_schema_ref, self.schema_descriptors
        )
        if not isinstance(descriptor, RawDtoSchemaDescriptor):
            raise ValueError(
                "synthetic perception response requires its pinned DTO contract"
            )
        validate_typed_value(descriptor.type_definition, _fixture_dto(response))
        return sha256_digest(canonical_bytes(response))

    def model_invocation(
        self,
        call: SyntheticCall,
        node: InvocationNodeId,
        purpose: ModelCapability,
        binding_ref: ContentRef,
        profiles: tuple[ProfileContentRef, ...],
        request_digest: str,
        response_digest: str,
        *,
        object_ref: ObjectRef | None = None,
        contract: InvocationContractSnapshot | None = None,
        evidence_refs: tuple[EvidenceRef, ...] = (),
        failure: Literal["synthetic-grounding-failure", "synthetic-discovery-failure"]
        | None = None,
    ) -> ModelInvocation:
        binding = self.resolve(binding_ref, ModelBinding)
        exact_binding_ref = ModelBindingContentRef.model_validate(
            binding_ref.model_dump()
        )
        provider = self.resolve(
            binding.provider_policy_ref, ProviderPolicyArtifact
        ).policy
        (pin,) = tuple(
            item
            for item in self.pins.model_pins
            if reference_identity(item.binding_ref) == reference_identity(binding_ref)
        )
        deployment = self.deployment
        if not isinstance(provider, RemotePrimaryProviderPolicy) or (
            binding.capability is not purpose
            or pin.provider_policy_ref.stable_json()
            != binding.provider_policy_ref.stable_json()
            or pin.fallback_policy_ref.stable_json()
            != binding.fallback_policy_ref.stable_json()
            or pin.primary_deployment_id != provider.primary_deployment_id
            or pin.deployments != provider.deployments
            or deployment.deployment_id != provider.primary_deployment_id
            or deployment.logical_model_id != binding.logical_model_id
            or node != call.envelope.node_id.value
        ):
            raise ValueError(
                "synthetic model requires the bundle's registered fixture primary"
            )
        configured = next(
            item
            for item in provider.deployments
            if item.deployment_id == deployment.deployment_id
        )
        constraints = configured.actual_version_constraints
        if (
            configured.provider_id != deployment.provider_id
            or configured.provider_kind != deployment.provider_kind
            or configured.logical_model_id != deployment.logical_model_id
            or deployment.deployment_version
            not in constraints.allowed_deployment_versions
            or deployment.model_version not in constraints.allowed_model_versions
        ):
            raise ValueError(
                "synthetic deployment identity or versions are outside the execution pin"
            )
        identity = call.id(
            "model",
            binding_ref.stable_json(),
            *(item.stable_json() for item in profiles),
            *(() if object_ref is None else (object_ref.record_id,)),
        )
        request_id = synthetic_id("request", identity, request_digest)
        outcome = (
            SucceededInvocation(status="succeeded", response_digest=response_digest)
            if failure is None
            else FailedInvocation(
                status="failed",
                error=InvocationError(
                    error_class="validation", code=failure, diagnostic_refs=()
                ),
            )
        )
        return ModelInvocation.model_validate(
            {
                "schema_version": VERSION,
                "model_invocation_id": identity,
                "execution_id": self.execution_id,
                "node_id": node,
                "node_run_ref": call.node_run_ref,
                "attempt_ref": call.attempt_ref,
                "configured_provider": ConfiguredProviderIdentity(
                    binding_ref=exact_binding_ref,
                    provider_policy_ref=binding.provider_policy_ref,
                    primary_deployment_id=provider.primary_deployment_id,
                    provider_id=configured.provider_id,
                    provider_kind=configured.provider_kind,
                    logical_model_id=binding.logical_model_id,
                ),
                "actual_provider": ReportedProviderIdentity(
                    version_resolution="reported",
                    version_evidence=request_id,
                    provider_id=deployment.provider_id,
                    provider_kind=deployment.provider_kind,
                    deployment_id=deployment.deployment_id,
                    model_id=deployment.logical_model_id,
                    deployment_version=deployment.deployment_version,
                    model_version=deployment.model_version,
                ),
                "route_trace": PrimaryProviderRoute(
                    route_kind="primary",
                    routing_mode="remote_primary",
                    selected_deployment_id=deployment.deployment_id,
                    fallback_policy_ref=binding.fallback_policy_ref,
                ),
                "purposes": (purpose,),
                "profile_refs": profiles,
                **({} if object_ref is None else {"object_ref": object_ref}),
                **({} if contract is None else {"contract_snapshot": contract}),
                "outcome": outcome,
                "request_digest": request_digest,
                "provider_request_id": request_id,
                "started_at": call.at,
                "completed_at": call.at,
                "evidence_refs": evidence_refs,
                "trace_id": call.envelope.trace.trace_id,
            }
        )
