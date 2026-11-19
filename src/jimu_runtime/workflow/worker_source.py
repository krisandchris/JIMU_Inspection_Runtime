"""Open the historical configuration and trusted inputs outside PG locks."""

from asyncio import to_thread
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, runtime_checkable

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.capabilities import CapabilitySnapshot
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.policies import EvidencePolicyArtifact, RulePolicyArtifact
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.facts import ProfileContentRef
from jimu_runtime.domain.runtime.intake import (
    AcceptedTask,
    ContextSnapshot,
    IdempotencyScope,
    RegistryRecordRef,
)
from jimu_runtime.domain.runtime.media import MediaAsset
from jimu_runtime.domain.runtime.releases import ReleaseRecord
from jimu_runtime.domain.tool_mappings import RegionContentRef
from jimu_runtime.domain.validation.decision_coverage import (
    BoundRule,
    BoundRuleSet,
    ProfileRuleScope,
)
from jimu_runtime.domain.values import Value
from jimu_runtime.workflow.engine import ProfilePlan
from jimu_runtime.workflow.mock_perception import SyntheticGroundingCase
from jimu_runtime.workflow.mock_support import (
    SyntheticExecution,
    VerifiedExecutionScope,
)
from jimu_runtime.workflow.pins import ResolvedExecutionPins, resolve_execution_pins
from jimu_runtime.workflow.repository import LockedExecution
from jimu_runtime.workflow.worker_capabilities import HistoricalCapabilities


@dataclass(frozen=True, slots=True)
class SyntheticObjectCase:
    profile_ref: ProfileContentRef
    object_index: int
    readings: tuple[Value, ...] | None = None
    outside: bool | None = True


@dataclass(frozen=True, slots=True)
class SyntheticScenario:
    """Host-provided outcomes indexed by pinned Profile and grounding order."""

    discovery_hits: tuple[ProfileContentRef, ...] | None = None
    discovery_failures: tuple[ProfileContentRef, ...] = ()
    grounding: tuple[SyntheticGroundingCase, ...] = ()
    objects: tuple[SyntheticObjectCase, ...] = ()


@dataclass(frozen=True, slots=True)
class TrustedWorkerSource:
    """Scope-verified information about the original and its capture source."""

    media: MediaAsset
    device: RegistryRecordRef
    camera: RegistryRecordRef


@dataclass(frozen=True, slots=True)
class WorkerSource(TrustedWorkerSource):
    """Additional configuration for the legacy synthetic adapter."""

    fixture_deployment_id: str
    scenario: SyntheticScenario = SyntheticScenario()


class WorkerHost(Protocol):
    """Trusted runtime host for one scope, supplying current capabilities and verified inputs."""

    async def capabilities(self, scope: IdempotencyScope) -> CapabilitySnapshot: ...

    async def worker_source(
        self, scope: IdempotencyScope, task: AcceptedTask
    ) -> TrustedWorkerSource: ...


@runtime_checkable
class HistoricalWorkerHost(WorkerHost, Protocol):
    async def worker_source_for_snapshot(
        self, scope: IdempotencyScope, task: AcceptedTask, snapshot: ContextSnapshot
    ) -> TrustedWorkerSource: ...


@dataclass(frozen=True, slots=True)
class OpenExecution:
    scope: VerifiedExecutionScope
    resolved: ResolvedExecutionPins
    release: ReleaseRecord
    source: TrustedWorkerSource
    region_ref: RegionContentRef
    region: RegionDefinition
    profiles: tuple[ProfilePlan, ...]
    historical: HistoricalCapabilities | None = None

    @property
    def synthetic(self) -> SyntheticExecution:
        if not isinstance(self.scope, SyntheticExecution):
            raise ValueError("synthetic node requires a designated fixture deployment")
        return self.scope

    @property
    def scenario(self) -> SyntheticScenario:
        if not isinstance(self.source, WorkerSource):
            raise ValueError("synthetic node requires a designated fixture source")
        return self.source.scenario


def _profile_plan(scope: VerifiedExecutionScope, ref: ProfileContentRef) -> ProfilePlan:
    profile = scope.resolve(ref, ProfileDefinition)
    rule_sets = []
    for set_ref in profile.rule_set_refs:
        rule_set = scope.resolve(set_ref, RuleSetDefinition)
        rules = []
        for rule_ref in rule_set.rule_refs:
            rule = scope.resolve(rule_ref, RuleDefinition)
            rules.append(
                BoundRule(
                    rule_ref=rule_ref,
                    definition=rule,
                    rule_policy=scope.resolve(rule.rule_policy_ref, RulePolicyArtifact),
                    evidence_policy=scope.resolve(
                        rule.evidence_policy_ref, EvidencePolicyArtifact
                    ),
                )
            )
        rule_sets.append(
            BoundRuleSet(rule_set_ref=set_ref, definition=rule_set, rules=tuple(rules))
        )
    return ProfilePlan(
        profile, ProfileRuleScope(profile_ref=ref, rule_sets=tuple(rule_sets))
    )


async def open_execution(
    locked: LockedExecution,
    scope: IdempotencyScope,
    manager: ReleaseManager,
    store: LocalBundleStore,
    host: WorkerHost,
    limits: BundleLimits,
    at: datetime,
    *,
    historical: HistoricalCapabilities | None = None,
    require_historical: bool = False,
    historical_context: ContextSnapshot | None = None,
    capture_execution_id: str | None = None,
) -> OpenExecution:
    task, execution = locked.task, locked.execution
    if historical is None and require_historical:
        raise ValueError(
            "gateway execution requires a durable historical capability snapshot"
        )
    capabilities = (
        await host.capabilities(scope) if historical is None else historical.snapshot
    )
    resolved = await to_thread(
        resolve_execution_pins,
        manager,
        store,
        task,
        scope=scope,
        host=capabilities,
        validation_at=at if historical is None else historical.validation_at,
        limits=limits,
    )
    if (resolved.pins, resolved.policy_ref) != (
        execution.pins,
        locked.execution_policy_ref,
    ):
        raise ValueError("worker cannot open a different historical Execution pin")
    activation = await to_thread(manager.get_task_activation, task)
    if activation is None:
        raise ValueError("worker requires the original historical Task Activation")
    release = await to_thread(
        manager.get_release, task.release_id, scope=activation.scope
    )
    if (
        release is None
        or release.bundle_ref != activation.bundle_ref
        or release.approval_receipt is None
    ):
        raise ValueError("worker requires the exact approved historical Release")
    if historical_context is None:
        source = await host.worker_source(scope, task)
    elif isinstance(host, HistoricalWorkerHost):
        source = await host.worker_source_for_snapshot(scope, task, historical_context)
    else:
        source = replace(
            await host.worker_source(scope, task),
            device=historical_context.device_ref,
            camera=historical_context.camera_ref,
        )
    if historical_context is not None and (
        historical_context.task_id != task.task_id
        or historical_context.execution_id
        != (capture_execution_id or execution.execution_id)
        or historical_context.release_id != task.release_id
        or historical_context.bundle_digest != task.bundle_digest
        or historical_context.media_ref != source.media.media_ref
        or (historical_context.device_ref, historical_context.camera_ref)
        != (source.device, source.camera)
    ):
        raise ValueError(
            "historical Worker source differs from the pinned ContextSnapshot"
        )

    bound: VerifiedExecutionScope
    if isinstance(source, WorkerSource):
        deployment = {
            deployment
            for row in resolved.model_deployments
            for deployment in row.deployments
            if deployment.deployment_id == source.fixture_deployment_id
        }
        if len(deployment) != 1:
            raise ValueError("synthetic worker requires one vetted fixture deployment")
        bound = await to_thread(
            SyntheticExecution,
            task,
            execution.execution_id,
            execution.pins,
            source.media,
            resolved.files,
            next(iter(deployment)),
            limits.max_file_bytes,
            limits.max_json_depth,
        )
    else:
        bound = await to_thread(
            VerifiedExecutionScope,
            task,
            execution.execution_id,
            execution.pins,
            source.media,
            resolved.files,
            limits.max_file_bytes,
            limits.max_json_depth,
        )
    if (
        reference_identity(bound.execution_config_ref)
        != reference_identity(resolved.policy_ref)
        or source.device.record_id != task.context.device_id
        or source.camera.record_id != task.context.camera_id
    ):
        raise ValueError(
            "worker requires exact Task registry and configuration references"
        )
    region_refs = tuple(
        RegionContentRef.model_validate(entry.ref.model_dump())
        for entry in bound.catalogue.of_kind(ArtifactKind.REGION)
        if entry.ref.artifact_id == task.context.region_id
    )
    if len(region_refs) != 1:
        raise ValueError("worker requires one exact pinned Region")
    (region_ref,) = region_refs
    profiles = tuple(
        _profile_plan(bound, ProfileContentRef.model_validate(entry.ref.model_dump()))
        for entry in bound.catalogue.of_kind(ArtifactKind.PROFILE)
    )
    cases = source.scenario.objects if isinstance(source, WorkerSource) else ()
    if len({(case.profile_ref, case.object_index) for case in cases}) != len(
        cases
    ) or any(
        case.object_index < 0
        or case.profile_ref not in {plan.rules.profile_ref for plan in profiles}
        for case in cases
    ):
        raise ValueError("synthetic Object cases must name unique Profile indices")
    return OpenExecution(
        bound,
        resolved,
        release,
        source,
        region_ref,
        bound.resolve(region_ref, RegionDefinition),
        profiles,
        historical,
    )
