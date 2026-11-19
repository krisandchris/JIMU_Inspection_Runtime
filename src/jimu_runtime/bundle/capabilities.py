"""Check Bundle requirements against a deployment-owned capability snapshot."""

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import (
    CapabilityFamily,
    CapabilityFeature,
    ModelCapability,
)
from jimu_runtime.domain.definition_support import (
    ExternalCapabilityRequirement,
    ReviewReportRef,
    UTCDateTime,
    immutable_sequence,
)
from jimu_runtime.domain.definitions.models import ModelBinding, ModelBindingContentRef
from jimu_runtime.domain.policies import ProviderPolicyArtifact
from jimu_runtime.domain.primitives import ArtifactId, Version
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.runtime.bundle_manifest import (
    BundleManifest,
    CapabilityRequirements,
)
from jimu_runtime.domain.runtime.releases import (
    CapabilityAssessment,
    CapabilityReport,
    SupportedCapability,
    UnavailableCapability,
)
from jimu_runtime.domain.tool_mappings import ProfileContentRef, RegionContentRef
from jimu_runtime.domain.validation.issues import (
    CheckResult,
    IssuePath,
    ValidationIssue,
    ValidationReport,
)
from jimu_runtime.domain.validation.policies import (
    PolicyRegistration,
    PromptRegistration,
    resolve_policy,
    validate_model_policies,
)
from jimu_runtime.domain.validation.producer_inputs import SystemCapabilityRegistration
from jimu_runtime.domain.validation.review_inputs import (
    ReviewedDeployment,
    ReviewMetadata,
)
from jimu_runtime.domain.validation.schema_descriptors import SchemaCheckError

type Directory[T] = Annotated[
    tuple[T, ...], Field(max_length=1024), BeforeValidator(immutable_sequence)
]
type Features = Annotated[
    tuple[CapabilityFeature, ...],
    Field(max_length=7),
    BeforeValidator(immutable_sequence),
]
type CapabilityIdentity = tuple[CapabilityFamily, str, str]

_UTC = TypeAdapter[UTCDateTime](UTCDateTime)
_REQUIREMENTS = TypeAdapter[CapabilityRequirements](CapabilityRequirements)


def _issue(code: str, message: str, path: IssuePath = ()) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, path=path, related_refs=())


def _version(version: Version) -> tuple[int, ...]:
    return tuple(int(component) for component in version.split("."))


def _identity(
    item: "ExternalCapabilityRequirement | CapabilityRegistration",
) -> CapabilityIdentity:
    return item.family, item.capability_id, item.contract_version


def _review_identity(ref: ReviewReportRef) -> tuple[str, str, str]:
    return ref.review_registry_id, ref.report_id, ref.version


class _CurrentRegistration(ContractModel):
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    revoked_at: UTCDateTime | None

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.valid_from >= self.valid_until:
            raise ValueError("registration validity window must be ordered")
        return self

    def is_current(self, at: UTCDateTime) -> bool:
        return self.valid_from <= at < self.valid_until and (
            self.revoked_at is None or at < self.revoked_at
        )


class CapabilityEvaluation(_CurrentRegistration):
    """An approved, host-verified review of features of the enclosing registration.

    The trusted registry verifies exact report identity and digest before this
    association is added. Bundle data can only request an exact report pin.
    """

    ref: ReviewReportRef
    reviewed_features: Features

    @model_validator(mode="after")
    def unique_features(self) -> Self:
        if len(set(self.reviewed_features)) != len(self.reviewed_features):
            raise ValueError("evaluated features must be unique")
        return self


class CapabilityRegistration(_CurrentRegistration):
    family: CapabilityFamily
    capability_id: ArtifactId
    contract_version: Version
    implementation_version: Version
    supported_features: Features
    evaluations: Directory[CapabilityEvaluation]

    @model_validator(mode="after")
    def consistent_evaluations(self) -> Self:
        features = set(self.supported_features)
        if len(features) != len(self.supported_features):
            raise ValueError("registered capability features must be unique")
        reviews = {_review_identity(entry.ref) for entry in self.evaluations}
        if len(reviews) != len(self.evaluations):
            raise ValueError(
                "registered capability evaluation identities must be unique"
            )
        if any(
            not features.issuperset(entry.reviewed_features)
            for entry in self.evaluations
        ):
            raise ValueError("evaluated features must belong to the registration")
        return self


class DeploymentContract(ContractModel):
    capability: ModelCapability
    input_schema_ref: SchemaRef
    output_schema_ref: SchemaRef


class ModelDeploymentRegistration(_CurrentRegistration):
    identity: ReviewedDeployment
    supported_features: Features
    contracts: Directory[DeploymentContract]

    @model_validator(mode="after")
    def unique_feature_and_contract_rows(self) -> Self:
        if len(set(self.supported_features)) != len(self.supported_features):
            raise ValueError("registered deployment features must be unique")
        if not self.contracts or len(set(self.contracts)) != len(self.contracts):
            raise ValueError("registered deployment needs unique contracts")
        return self


type RuntimeProtocolRole = ModelCapability | Literal["tool_grounding", "tool_execution"]


class RuntimeProtocolRegistration(_CurrentRegistration):
    """One Runtime implementation of an exact versioned input/output protocol."""

    role: RuntimeProtocolRole
    input_schema_ref: SchemaRef
    output_schema_ref: SchemaRef
    protocol_version: Version

    @field_validator("role", mode="before")
    @classmethod
    def validate_role(cls, raw: object) -> RuntimeProtocolRole:
        if raw == "tool_grounding":
            return "tool_grounding"
        if raw == "tool_execution":
            return "tool_execution"
        return ModelCapability._decode(raw)


class ModelReviewRegistration(ContractModel):
    """Metadata authenticated by the host review registry, with revocation state."""

    metadata: ReviewMetadata
    revoked_at: UTCDateTime | None


class CapabilitySnapshot(ContractModel):
    """Immutable host registry slice used for one compilation or admission.

    Snapshot pin, clock, version and all registrations originate from trusted
    deployment configuration. Bundle declarations never supply these values.
    """

    pin: TrustPin
    runtime_version: Version
    bundle_schema_versions: Directory[Version]
    valid_from: UTCDateTime
    valid_until: UTCDateTime
    capabilities: Directory[CapabilityRegistration]
    model_deployments: Directory[ModelDeploymentRegistration]
    model_reviews: Directory[ModelReviewRegistration]
    runtime_protocols: Directory[RuntimeProtocolRegistration] = ()
    supported_workflow_versions: Directory[Version] = ()

    @model_validator(mode="after")
    def unique_configuration(self) -> Self:
        if self.valid_from >= self.valid_until:
            raise ValueError("capability snapshot validity window must be ordered")
        if not self.bundle_schema_versions or len(
            set(self.bundle_schema_versions)
        ) != len(self.bundle_schema_versions):
            raise ValueError(
                "supported Bundle schema versions must be nonempty and unique"
            )
        keys = (
            list(self.supported_workflow_versions),
            [
                (entry.role, entry.input_schema_ref, entry.output_schema_ref)
                for entry in self.runtime_protocols
            ],
            [_identity(entry) for entry in self.capabilities],
            [
                (entry.identity.provider_id, entry.identity.deployment_id)
                for entry in self.model_deployments
            ],
            [
                _review_identity(entry.metadata.report_ref)
                for entry in self.model_reviews
            ],
        )
        if any(len(identities) != len(set(identities)) for identities in keys):
            raise ValueError("host registry identities must be unique")
        return self

    def is_current(self, at: UTCDateTime) -> bool:
        return self.valid_from <= at < self.valid_until

    def protocols_at(
        self, validation_at: UTCDateTime
    ) -> tuple[RuntimeProtocolRegistration, ...]:
        at = _UTC.validate_python(validation_at)
        if not self.is_current(at):
            return ()
        return tuple(entry for entry in self.runtime_protocols if entry.is_current(at))

    def supports_workflow_at(
        self, version: Version, validation_at: UTCDateTime
    ) -> bool:
        at = _UTC.validate_python(validation_at)
        return self.is_current(at) and version in self.supported_workflow_versions

    def reviews_at(self, validation_at: UTCDateTime) -> tuple[ReviewMetadata, ...]:
        at = _UTC.validate_python(validation_at)
        if not self.is_current(at):
            return ()
        return tuple(
            item.metadata
            for item in self.model_reviews
            if item.revoked_at is None or at < item.revoked_at
        )

    def system_capabilities_at(
        self, validation_at: UTCDateTime
    ) -> tuple[SystemCapabilityRegistration, ...]:
        at = _UTC.validate_python(validation_at)
        if not self.is_current(at):
            return ()
        return tuple(
            SystemCapabilityRegistration(
                registry_id=self.pin.identity,
                family=CapabilityFamily.SYSTEM_FACT_PRODUCER,
                capability_id=item.capability_id,
                contract_version=item.contract_version,
                available_implementation_versions=(item.implementation_version,),
                supported_features=item.supported_features,
            )
            for item in self.capabilities
            if item.family is CapabilityFamily.SYSTEM_FACT_PRODUCER
            and item.is_current(at)
        )


@dataclass(frozen=True, slots=True)
class CapabilityValidation:
    report: CapabilityReport
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]


def _assess_requirement(
    requirement: ExternalCapabilityRequirement,
    registration: CapabilityRegistration | None,
    at: UTCDateTime,
    path: IssuePath,
) -> tuple[CapabilityAssessment, ValidationIssue | None]:
    if (
        registration is None
        or not registration.is_current(at)
        or registration.implementation_version
        not in requirement.allowed_implementation_versions
        or not set(registration.supported_features).issuperset(
            requirement.required_features
        )
    ):
        return (
            UnavailableCapability(status="unsupported", requirement=requirement),
            _issue(
                "capability.unsupported",
                "No active host registration satisfies the exact capability, version and features.",
                path,
            ),
        )
    if requirement.evaluation_ref is not None and not any(
        item.ref == requirement.evaluation_ref
        and item.is_current(at)
        and set(item.reviewed_features).issuperset(requirement.required_features)
        for item in registration.evaluations
    ):
        return (
            UnavailableCapability(status="not_evaluated", requirement=requirement),
            _issue(
                "capability.evaluation_unavailable",
                "The exact active approved evaluation is unavailable for these capability features.",
                path,
            ),
        )
    return (
        SupportedCapability(
            status="supported",
            requirement=requirement,
            implementation_version=registration.implementation_version,
        ),
        None,
    )


def assess_capabilities(
    manifest: BundleManifest,
    snapshot: CapabilitySnapshot,
    *,
    validation_at: UTCDateTime,
    optional: CapabilityRequirements = (),
) -> CapabilityValidation:
    """Assess required manifest declarations and explicitly sourced optional uses."""
    manifest = BundleManifest.model_validate(manifest)
    optional = _REQUIREMENTS.validate_python(optional)
    snapshot = CapabilitySnapshot.model_validate(snapshot)
    at = _UTC.validate_python(validation_at)
    ids = [
        _identity(requirement)
        for requirement in (*manifest.required_capabilities, *optional)
    ]
    if len(ids) != len(set(ids)):
        raise ValueError(
            "required and optional capability identities must be disjoint and unique"
        )

    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    host: dict[CapabilityIdentity, CapabilityRegistration] = {}
    if snapshot.is_current(at):
        host.update((_identity(item), item) for item in snapshot.capabilities)
    else:
        errors.append(
            _issue(
                "capability.snapshot_time",
                "The host capability snapshot is outside its validity window.",
            )
        )
    if manifest.schema_version not in snapshot.bundle_schema_versions:
        errors.append(
            _issue(
                "capability.bundle_schema",
                "The host does not support the Bundle schema version.",
                ("schema_version",),
            )
        )
    runtime = _version(snapshot.runtime_version)
    compatibility = manifest.compatibility
    if not (
        _version(compatibility.minimum_runtime_version_inclusive)
        <= runtime
        < _version(compatibility.maximum_runtime_version_exclusive)
    ):
        errors.append(
            _issue(
                "capability.runtime_version",
                "The host Runtime version is outside the Bundle range.",
                ("compatibility",),
            )
        )

    required_results: list[CapabilityAssessment] = []
    optional_results: list[CapabilityAssessment] = []
    # Keep report entries for every declaration. Receipt diagnostics have a
    # separate 128-entry budget, so reserve one slot to note further failures.
    for entries, results, issues, field in (
        (
            manifest.required_capabilities,
            required_results,
            errors,
            "required_capabilities",
        ),
        (optional, optional_results, warnings, "optional_capabilities"),
    ):
        for index, requirement in enumerate(entries):
            assessment, issue = _assess_requirement(
                requirement, host.get(_identity(requirement)), at, (field, index)
            )
            results.append(assessment)
            if issue is not None:
                issues.append(issue)
    return CapabilityValidation(
        CapabilityReport(
            required=tuple(required_results), optional=tuple(optional_results)
        ),
        _bounded_issues(errors),
        _bounded_issues(warnings),
    )


def _bounded_issues(issues: list[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    if len(issues) <= 128:
        return tuple(issues)
    return (
        *issues[:127],
        _issue(
            "capability.issue_limit",
            "See the complete capability report for additional failed requirements.",
        ),
    )


def _check_deployments(
    binding: ModelBinding,
    catalogue: tuple[PolicyRegistration, ...],
    snapshot: CapabilitySnapshot,
    at: UTCDateTime,
) -> CheckResult:
    check_id = "model.host_deployments"
    try:
        artifact = resolve_policy(binding.provider_policy_ref, catalogue)
    except SchemaCheckError as exc:
        return CheckResult(
            check_id=check_id,
            status=exc.issue.outcome,
            issues=(_issue(exc.issue.code, exc.issue.message, exc.issue.field_path),),
        )
    if not isinstance(artifact, ProviderPolicyArtifact):
        return CheckResult(
            check_id=check_id,
            status="invalid",
            issues=(
                _issue(
                    "model.provider_policy_kind",
                    "ModelBinding provider ref has the wrong policy kind.",
                ),
            ),
        )
    if not snapshot.is_current(at):
        return CheckResult(
            check_id=check_id,
            status="invalid",
            issues=(
                _issue(
                    "capability.snapshot_time",
                    "The host capability snapshot is outside its validity window.",
                ),
            ),
        )

    host = {
        (entry.identity.provider_id, entry.identity.deployment_id): entry
        for entry in snapshot.model_deployments
        if entry.is_current(at)
    }
    contract = DeploymentContract(
        capability=binding.capability,
        input_schema_ref=binding.input_schema_ref,
        output_schema_ref=binding.output_schema_ref,
    )
    issues = []
    for index, required in enumerate(artifact.policy.deployments):
        registered = host.get((required.provider_id, required.deployment_id))
        if (
            registered is None
            or registered.identity.provider_kind != required.provider_kind
            or registered.identity.logical_model_id != required.logical_model_id
            or registered.identity.deployment_version
            not in required.actual_version_constraints.allowed_deployment_versions
            or registered.identity.model_version
            not in required.actual_version_constraints.allowed_model_versions
            or not set(registered.supported_features).issuperset(
                required.required_features
            )
            or contract not in registered.contracts
        ):
            issues.append(
                _issue(
                    "model.host_deployment_mismatch",
                    "No active host deployment matches the configured identity, versions, features and exact I/O contract.",
                    ("deployments", index),
                )
            )
    if len(issues) > 64:
        issues = [
            *issues[:63],
            _issue(
                "model.host_deployment_issue_limit",
                "More configured deployments have no compatible host registration.",
            ),
        ]
    return CheckResult(
        check_id=check_id,
        status="invalid" if issues else "passed",
        issues=tuple(issues),
    )


def validate_model_deployments(
    binding: ModelBinding,
    catalogue: tuple[PolicyRegistration, ...],
    snapshot: CapabilitySnapshot,
    validation_at: UTCDateTime,
    *,
    binding_ref: ModelBindingContentRef,
    profile_ref: ProfileContentRef | None = None,
    region_ref: RegionContentRef | None = None,
    prompts: tuple[PromptRegistration, ...] = (),
    consuming_purpose: ModelCapability | None = None,
) -> ValidationReport:
    """Validate configured routes using host-authenticated F02 review metadata."""
    binding = ModelBinding.model_validate(binding)
    snapshot = CapabilitySnapshot.model_validate(snapshot)
    at = _UTC.validate_python(validation_at)
    definition_report = validate_model_policies(
        binding,
        catalogue,
        snapshot.reviews_at(at),
        at,
        binding_ref=binding_ref,
        profile_ref=profile_ref,
        region_ref=region_ref,
        prompts=prompts,
        consuming_purpose=consuming_purpose,
    )
    # The domain report defers host registry resolution. Here we validate one
    # host snapshot, without invoking a route, provider or quality evaluation.
    checked = tuple(
        check
        for check in definition_report.checks
        if check.check_id != "model.external_trust"
    )
    return ValidationReport(
        scope=definition_report.scope,
        checks=(*checked, _check_deployments(binding, catalogue, snapshot, at)),
    )
