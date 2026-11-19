"""Resolve historical execution configuration after authorized task acceptance."""

from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter

from jimu_runtime.bundle.archive import BundleLimits, read_bundle
from jimu_runtime.bundle.capabilities import (
    CapabilityIdentity,
    CapabilitySnapshot,
    assess_capabilities,
    validate_model_deployments,
)
from jimu_runtime.bundle.catalogue import (
    ArtifactCatalogue,
    build_catalogue,
)
from jimu_runtime.bundle.closure import validate_closure
from jimu_runtime.bundle.integrity import (
    BundleIntegrityError,
    JSONLocation,
    VerifiedBundleFiles,
    sha256_digest,
)
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.schema_views import build_schema_descriptors
from jimu_runtime.bundle.semantic_inputs import (
    build_definition_inputs,
    build_fact_schema_registry,
    profile_model_uses,
    require_definition,
)
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    UTCDateTime,
    reference_identity,
)
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.models import (
    ModelBinding,
    ModelBindingContentRef,
    PolicyContentRef,
)
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.fact_selectors import ToolContentRef
from jimu_runtime.domain.policies import (
    ExecutionPolicy,
    ExecutionPolicyArtifact,
    ProviderPolicyArtifact,
)
from jimu_runtime.domain.primitives import RecordId, Version
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest, BundleRef
from jimu_runtime.domain.runtime.executions import (
    ConfiguredModelPin,
    ConfiguredToolPin,
    ExecutionPins,
)
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    IdempotencyScope,
    TaskContext,
)
from jimu_runtime.domain.runtime.media import MediaRef
from jimu_runtime.domain.runtime.releases import (
    AdmissionReceipt,
    ReleaseStatus,
    SupportedCapability,
)
from jimu_runtime.domain.schema_types import SchemaDescriptor
from jimu_runtime.domain.validation.definition_inputs import (
    DefinitionValidationInput,
    ProfileUseContext,
    RegisteredPrompt,
)
from jimu_runtime.domain.validation.policies import PromptRegistration
from jimu_runtime.domain.validation.producers import requirements_agree
from jimu_runtime.domain.validation.review_inputs import ReviewedDeployment

_EXECUTION_PATH = "policies/execution.json"
_UTC = TypeAdapter[UTCDateTime](UTCDateTime)


class ExecutionPinError(ValueError):
    """One incompatible execution input, with a stable diagnostic and location."""

    def __init__(self, code: str, path: JSONLocation, message: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code} at {path}: {message}")


class ResolvedModelDeployments(ContractModel):
    binding_ref: ModelBindingContentRef
    deployments: tuple[ReviewedDeployment, ...]


@dataclass(frozen=True, slots=True)
class ResolvedExecutionPins:
    pins: ExecutionPins
    policy_ref: PolicyContentRef
    policy: ExecutionPolicy
    files: VerifiedBundleFiles
    catalogue: ArtifactCatalogue
    schema_descriptors: tuple[SchemaDescriptor, ...]
    fact_schemas: FactSchemaRegistry
    definition_inputs: DefinitionValidationInput
    model_deployments: tuple[ResolvedModelDeployments, ...]


class _ExecutionInputV1(ContractModel):
    preimage_version: Literal["jimu.execution-input.v1"] = "jimu.execution-input.v1"
    task_id: RecordId
    request_id: RecordId
    scope: IdempotencyScope
    media_ref: MediaRef
    task_context: TaskContext
    release_id: RecordId
    bundle: BundleRef
    admission_receipt_id: RecordId
    runtime_version: Version
    workflow_version: Version
    execution_policy_ref: PolicyContentRef
    models: tuple[ConfiguredModelPin, ...]
    model_deployments: tuple[ResolvedModelDeployments, ...]
    tools: tuple[ConfiguredToolPin, ...]


def _execution_policy(
    catalogue: ArtifactCatalogue,
) -> tuple[PolicyContentRef, ExecutionPolicy]:
    policies = tuple(
        artifact
        for artifact in catalogue.of_kind(ArtifactKind.POLICY)
        if isinstance(artifact.value, ExecutionPolicyArtifact)
    )
    if len(policies) != 1:
        raise ExecutionPinError(
            "execution_policy_cardinality",
            (_EXECUTION_PATH,),
            "The Bundle must contain one execution policy.",
        )
    (artifact,) = policies
    if artifact.inventory.path != _EXECUTION_PATH or artifact.document_location:
        raise ExecutionPinError(
            "execution_policy_locator",
            (artifact.inventory.path, *artifact.document_location),
            "The execution policy must occupy the root of its dedicated document.",
        )
    return (
        PolicyContentRef.model_validate(artifact.ref.model_dump()),
        require_definition(catalogue, artifact.ref, ExecutionPolicyArtifact).policy,
    )


def _capability_key(requirement: ExternalCapabilityRequirement) -> CapabilityIdentity:
    return requirement.family, requirement.capability_id, requirement.contract_version


def _tools(
    catalogue: ArtifactCatalogue,
    manifest: BundleManifest,
    receipt: AdmissionReceipt,
    host: CapabilitySnapshot,
    at: UTCDateTime,
) -> tuple[ConfiguredToolPin, ...]:
    tools = tuple(
        (artifact, require_definition(catalogue, artifact.ref, ToolDefinition))
        for artifact in sorted(
            catalogue.of_kind(ArtifactKind.TOOL),
            key=lambda a: reference_identity(a.ref),
        )
    )
    expected = {_capability_key(req): req for req in manifest.required_capabilities}
    report = receipt.capability_report
    admitted = {_capability_key(row.requirement): row for row in report.required}
    if expected.keys() != admitted.keys() or any(
        not requirements_agree(requirement, admitted[key].requirement)
        for key, requirement in expected.items()
    ):
        raise ExecutionPinError(
            "receipt_capabilities_mismatch",
            ("manifest.json", "required_capabilities"),
            "The admission capabilities must match the manifest's required declarations.",
        )
    tool_keys = {_capability_key(tool.implementation_requirement) for _, tool in tools}
    optional = tuple(row.requirement for row in report.optional)
    if any(_capability_key(req) not in tool_keys for req in optional):
        raise ExecutionPinError(
            "unexpected_optional_capability",
            ("admission_receipt", "capability_report", "optional"),
            "An optional admission assessment has no Tool declaration.",
        )
    current = assess_capabilities(manifest, host, validation_at=at, optional=optional)
    admitted.update((_capability_key(row.requirement), row) for row in report.optional)
    available = {
        _capability_key(row.requirement): row
        for row in (*current.report.required, *current.report.optional)
    }

    pins = []
    for artifact, tool in tools:
        requirement = tool.implementation_requirement
        key = _capability_key(requirement)
        original = admitted.get(key)
        present = available.get(key)
        path = (
            artifact.inventory.path,
            *artifact.document_location,
            "implementation_requirement",
        )
        if not isinstance(original, SupportedCapability) or not requirements_agree(
            requirement, original.requirement
        ):
            raise ExecutionPinError(
                "tool_admission_pin_unavailable",
                path,
                "The receipt does not select a supported implementation for this exact Tool requirement.",
            )
        if (
            not isinstance(present, SupportedCapability)
            or present.implementation_version != original.implementation_version
        ):
            raise ExecutionPinError(
                "tool_implementation_unavailable",
                path,
                "The host cannot provide the Tool implementation selected at admission.",
            )
        pins.append(
            ConfiguredToolPin(
                tool_ref=ToolContentRef.model_validate(artifact.ref.model_dump()),
                implementation_version=original.implementation_version,
            )
        )
    if current.errors:
        issue = current.errors[0]
        raise ExecutionPinError(
            issue.code, ("manifest.json", *issue.path), issue.message
        )
    return tuple(pins)


def _models(
    catalogue: ArtifactCatalogue,
    definitions: DefinitionValidationInput,
    host: CapabilitySnapshot,
    at: UTCDateTime,
) -> tuple[tuple[ConfiguredModelPin, ...], tuple[ResolvedModelDeployments, ...]]:
    prompts = tuple(
        PromptRegistration(ref=entry.ref, definition=entry.definition)
        for entry in definitions.definitions
        if isinstance(entry, RegisteredPrompt)
    )
    uses: dict[
        ModelBindingContentRef, list[tuple[ProfileUseContext, ModelCapability | None]]
    ] = {}
    for profile_site in definitions.use_contexts:
        profile = require_definition(
            catalogue, profile_site.profile_ref, ProfileDefinition
        )
        for use in profile_model_uses(catalogue, profile):
            uses.setdefault(use.binding_ref, []).append((profile_site, use.purpose))

    registrations = {
        (row.identity.provider_id, row.identity.deployment_id): row.identity
        for row in host.model_deployments
        if row.is_current(at)
    }
    pins = []
    identities = []
    for artifact in sorted(
        catalogue.of_kind(ArtifactKind.MODEL_BINDING),
        key=lambda a: reference_identity(a.ref),
    ):
        binding = require_definition(catalogue, artifact.ref, ModelBinding)
        ref = ModelBindingContentRef.model_validate(artifact.ref.model_dump())
        consuming: list[tuple[ProfileUseContext | None, ModelCapability | None]] = [
            *uses.get(ref, ())
        ]
        for site, purpose in consuming or [(None, None)]:
            report = validate_model_deployments(
                binding,
                definitions.policies,
                host,
                at,
                binding_ref=ref,
                profile_ref=None if site is None else site.profile_ref,
                region_ref=None if site is None else site.region_ref,
                prompts=prompts,
                consuming_purpose=purpose,
            )
            for check in report.checks:
                if check.status in ("invalid", "incomplete"):
                    issue = check.issues[0]
                    raise ExecutionPinError(
                        issue.code,
                        (
                            artifact.inventory.path,
                            *artifact.document_location,
                            *issue.path,
                        ),
                        issue.message,
                    )
        provider = require_definition(
            catalogue, binding.provider_policy_ref, ProviderPolicyArtifact
        ).policy
        pins.append(
            ConfiguredModelPin(
                binding_ref=ref,
                provider_policy_ref=binding.provider_policy_ref,
                fallback_policy_ref=binding.fallback_policy_ref,
                primary_deployment_id=provider.primary_deployment_id,
                deployments=provider.deployments,
            )
        )
        identities.append(
            ResolvedModelDeployments(
                binding_ref=ref,
                deployments=tuple(
                    registrations[(item.provider_id, item.deployment_id)]
                    for item in provider.deployments
                ),
            )
        )
    return tuple(pins), tuple(identities)


def resolve_execution_pins(
    manager: ReleaseManager,
    store: LocalBundleStore,
    task: AcceptedTask,
    *,
    scope: IdempotencyScope,
    host: CapabilitySnapshot,
    validation_at: UTCDateTime,
    limits: BundleLimits,
) -> ResolvedExecutionPins:
    """Resolve exact configuration for a task from the authorized acceptance API.

    The deployment owns the manager, store, authenticated scope, registry and
    clock. This check neither authorizes the caller nor selects a Release.
    """
    task = AcceptedTask.model_validate(task)
    scope = IdempotencyScope.model_validate(scope)
    if task.idempotency_scope != scope:
        raise ExecutionPinError(
            "task_scope_mismatch",
            ("task", "idempotency_scope"),
            "Task is outside the authenticated scope.",
        )
    if manager.get_task(task.task_id) != task:
        raise ExecutionPinError(
            "task_pin_mismatch",
            ("task", "task_id"),
            "Task does not match the immutable acceptance ledger.",
        )
    release = manager.get_release(task.release_id)
    if (
        release is None
        or release.release_id != task.release_id
        or release.status is not ReleaseStatus.ACCEPTED
        or release.bundle_ref.bundle_digest != task.bundle_digest
    ):
        raise ExecutionPinError(
            "release_pin_mismatch",
            ("task", "release_id"),
            "The accepted Release does not match the historical task pin.",
        )
    admission = release.admission_receipt
    stored = None if admission is None else store.get_receipt(admission.receipt_id)
    if (
        admission is None
        or stored is None
        or not stored.accepted
        or stored.receipt != admission
    ):
        raise ExecutionPinError(
            "admission_pin_mismatch",
            ("release", "admission_receipt"),
            "The Release admission receipt does not match the store.",
        )
    if stored.archive_size > limits.max_archive_bytes:
        raise ExecutionPinError(
            "archive_size_limit",
            ("admission_receipt", "archive_size"),
            "The stored archive exceeds the host budget.",
        )

    archive = store.read_archive(admission.receipt_id)
    try:
        package = read_bundle(archive, limits)
    except BundleIntegrityError as error:
        raise ExecutionPinError(
            error.code,
            (error.path or "archive", *error.json_location),
            str(error),
        ) from error
    manifest = package.manifest
    if (
        manifest.bundle_id,
        manifest.bundle_version,
        manifest.integrity.root_digest,
    ) != (
        release.bundle_ref.bundle_id,
        release.bundle_ref.bundle_version,
        release.bundle_ref.bundle_digest,
    ):
        raise ExecutionPinError(
            "bundle_pin_mismatch",
            ("manifest.json", "integrity", "root_digest"),
            "The verified archive does not match the Release.",
        )
    if (
        len(archive) != stored.archive_size
        or sha256_digest(archive) != stored.archive_sha256
    ):
        raise ExecutionPinError(
            "archive_receipt_mismatch",
            ("admission_receipt", "archive_sha256"),
            "The verified ZIP bytes do not match the stored admission.",
        )
    at = _UTC.validate_python(validation_at)
    host = CapabilitySnapshot.model_validate(host)
    catalogue = build_catalogue(
        manifest,
        package.payloads,
        max_bytes=limits.max_file_bytes,
        max_depth=limits.max_json_depth,
    )
    policy_ref, policy = _execution_policy(catalogue)
    if not host.supports_workflow_at(policy.workflow_version, at):
        raise ExecutionPinError(
            "workflow_version_unavailable",
            (_EXECUTION_PATH, "policy", "workflow_version"),
            "The host does not support the pinned workflow version at validation time.",
        )
    tools = _tools(catalogue, manifest, admission, host, at)
    closure = validate_closure(catalogue)
    descriptors = build_schema_descriptors(catalogue, schema_index=closure.schemas)
    definitions = build_definition_inputs(
        catalogue, closure, descriptors, reviews=host.reviews_at(at), validation_at=at
    )
    models, actual = _models(catalogue, definitions, host, at)

    preimage = _ExecutionInputV1(
        task_id=task.task_id,
        request_id=task.request_id,
        scope=scope,
        media_ref=task.media_ref,
        task_context=task.context,
        release_id=task.release_id,
        bundle=release.bundle_ref,
        admission_receipt_id=admission.receipt_id,
        runtime_version=host.runtime_version,
        workflow_version=policy.workflow_version,
        execution_policy_ref=policy_ref,
        models=models,
        model_deployments=actual,
        tools=tools,
    )
    pins = ExecutionPins(
        release_id=task.release_id,
        bundle_digest=task.bundle_digest,
        runtime_version=host.runtime_version,
        workflow_version=policy.workflow_version,
        model_pins=models,
        tool_pins=tools,
        input_digest=sha256_digest(preimage.stable_json().encode("utf-8")),
    )
    return ResolvedExecutionPins(
        pins=pins,
        policy_ref=policy_ref,
        policy=policy,
        files=package,
        catalogue=catalogue,
        schema_descriptors=descriptors,
        fact_schemas=build_fact_schema_registry(catalogue),
        definition_inputs=definitions,
        model_deployments=actual,
    )
