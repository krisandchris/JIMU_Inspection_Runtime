"""Bind finite B authority claims to independently supplied C runtime records."""

from collections.abc import Callable
from dataclasses import dataclass

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope, ModelCapability
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.enums import ArtifactKind, AttemptStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.producers import ToolStepProducer
from jimu_runtime.domain.references import ContentRef, RuntimeRecordRef
from jimu_runtime.domain.runtime.applicability import ProfileEligibilityEvaluation
from jimu_runtime.domain.runtime.authority import (
    AuthoritativeResultSelection,
    ObservationBatchSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.bundle_manifest import BundleManifest
from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    ExecutionEvent,
    ExecutionRecord,
    NodeId,
    NodeRun,
    SystemOutputEvent,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    ExecutionEventRef,
    FactRecord,
    NodeRunRef,
    ObjectRef,
    QuestionSelectionRef,
    SystemFactSource,
    ToolExecutionRef,
)
from jimu_runtime.domain.runtime.invocations import (
    ModelInvocation,
    RegistryVerifiedProviderIdentity,
    ReportedProviderIdentity,
)
from jimu_runtime.domain.runtime.observations import ObservationBatch
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.releases import ReleaseRecord
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import (
    ToolExecution,
    ToolPlanStepOrigin,
)
from jimu_runtime.domain.validation.authority import LocalAuthorityHistoryInput
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    EmittedOutput,
    NodeRunDescriptor,
    ObjectDescriptor,
    QuestionSelectionDescriptor,
    SystemAttemptOwner,
    SystemOutputResultDescriptor,
    ToolStepResultDescriptor,
)
from jimu_runtime.domain.validation.issues import ValidationReport
from jimu_runtime.domain.validation.producer_authority import (
    validate_producer_authority_contracts,
)
from jimu_runtime.domain.validation.producer_authority_inputs import (
    Directory,
    ExecutionDefinitionPin,
    ProducerAuthorityValidationInput,
)


class RuntimeAuthorityRecords(ContractModel):
    """Actual records for one explicit history slice of one Execution."""

    execution: ExecutionRecord
    release: ReleaseRecord
    manifest: BundleManifest
    eligibility: ProfileEligibilityEvaluation
    objects: Directory[ObjectRecord]
    node_runs: Directory[NodeRun]
    attempts: Directory[Attempt]
    question_selections: Directory[QuestionSelectionRecord]
    observations: Directory[ObservationBatch]
    tool_executions: Directory[ToolExecution]
    model_invocations: Directory[ModelInvocation]
    execution_events: Directory[ExecutionEvent]
    facts: Directory[FactRecord]
    authority_selections: Directory[AuthoritativeResultSelection]


@dataclass(frozen=True, slots=True)
class RuntimeAuthorityProjection:
    """Validated finite views and the non-superseded supplied revisions."""

    history: LocalAuthorityHistoryInput
    selected_revisions: tuple[AuthoritativeResultSelection, ...]
    report: ValidationReport


def _directory[T](
    items: tuple[T, ...], key: Callable[[T], str], label: str
) -> dict[str, T]:
    result = {key(item): item for item in items}
    if len(result) != len(items):
        raise ValueError(f"duplicate {label} identity")
    return result


def _find[K, T](items: dict[K, T], key: K, label: str) -> T:
    if key not in items:
        raise ValueError(f"missing actual {label} {key}")
    return items[key]


def _reference_key(ref: RuntimeRecordRef) -> tuple[RuntimeRecordKind, str]:
    return (ref.record_kind, ref.record_id)


def _projected[T](
    supplied: tuple[T, ...],
    actual: tuple[T, ...],
    key: Callable[[T], str],
    label: str,
) -> tuple[T, ...]:
    actual_index = _directory(actual, key, label)
    if actual_index != _directory(supplied, key, label):
        raise ValueError(
            f"{label} claims differ from the supplied actual record projection"
        )
    return actual


class _Scope:
    def __init__(
        self, records: RuntimeAuthorityRecords, claims: ProducerAuthorityValidationInput
    ) -> None:
        self.records = records
        self.claims = claims
        self.execution = records.execution
        self.eligible = set(
            map(reference_identity, records.eligibility.eligible_profiles)
        )
        self.objects = _directory(
            records.objects, lambda item: item.object_id, "Object"
        )
        self.nodes = _directory(
            records.node_runs, lambda item: item.node_run_id, "NodeRun"
        )
        self.attempts = _directory(
            records.attempts, lambda item: item.attempt_id, "Attempt"
        )
        self.selections = _directory(
            records.question_selections,
            lambda item: item.selection_id,
            "QuestionSelection",
        )
        self.invocations = _directory(
            records.model_invocations,
            lambda item: item.model_invocation_id,
            "ModelInvocation",
        )
        self.facts = _directory(records.facts, lambda item: item.fact_id, "Fact")
        self.events = _directory(
            records.execution_events,
            lambda item: item.execution_event_id,
            "ExecutionEvent",
        )
        self.inventory = {
            reference_identity(entry.content_ref)
            for entry in records.manifest.artifacts
        }
        self.model_pins = {
            reference_identity(pin.binding_ref): pin
            for pin in self.execution.pins.model_pins
        }
        self.tool_pins = {
            reference_identity(pin.tool_ref): pin.implementation_version
            for pin in self.execution.pins.tool_pins
        }
        self.profiles = {
            reference_identity(item.ref): item.definition
            for catalogue in claims.producer_catalogues
            for item in catalogue.context.profiles
        }
        self.declarations = {
            item.declaration_id: item
            for catalogue in claims.producer_catalogues
            for item in catalogue.registration.definition.declarations
        }

    def pin(self) -> ExecutionDefinitionPin:
        records = self.records
        pins = self.execution.pins
        manifest = records.manifest
        release = records.release
        gate = records.eligibility
        context = self.execution.context_snapshot
        if (pins.release_id, pins.bundle_digest) != (
            release.release_id,
            release.bundle_ref.bundle_digest,
        ) or (
            release.bundle_ref.bundle_id,
            release.bundle_ref.bundle_version,
            release.bundle_ref.bundle_digest,
        ) != (
            manifest.bundle_id,
            manifest.bundle_version,
            manifest.integrity.root_digest,
        ):
            raise ValueError(
                "actual Execution, Release and Bundle Manifest pins differ"
            )
        if release.approval_receipt is None:
            raise ValueError("actual Release lacks its approval receipt")
        if context is None or (
            gate.execution_id,
            gate.release_id,
            gate.bundle_digest,
            gate.workflow_version,
            gate.context_snapshot_ref,
        ) != (
            self.execution.execution_id,
            pins.release_id,
            pins.bundle_digest,
            pins.workflow_version,
            context.snapshot_ref,
        ):
            raise ValueError(
                "Profile eligibility must pin the actual Execution and context"
            )
        catalogues = tuple(
            ref
            for ref in manifest.artifacts
            if ref.content_ref.artifact_kind is ArtifactKind.FACT_PRODUCER_CATALOG
        )
        if len(catalogues) != 1 or len(self.claims.producer_catalogues) != 1:
            raise ValueError("finite history requires one exact producer catalogue")
        catalogue = self.claims.producer_catalogues[0]
        if reference_identity(catalogues[0].content_ref) != reference_identity(
            catalogue.registration.ref
        ):
            raise ValueError(
                "supplied producer catalogue differs from exact Manifest entry"
            )
        if set(self.profiles) != set(
            map(reference_identity, gate.configured_profile_refs)
        ):
            raise ValueError("actual Profile gate and producer catalogue scope differ")
        self.check_inventory(
            (
                *gate.configured_profile_refs,
                *(entry.ref for entry in catalogue.context.questions),
                *(entry.ref for entry in catalogue.context.tools),
                *(entry.ref for entry in catalogue.context.plans),
                *(entry.ref for entry in self.claims.history.questions),
                *(entry.ref for entry in self.claims.history.tools),
                *(entry.ref for entry in self.claims.history.plans),
                *(fact.fact_schema_ref for fact in self.facts.values()),
                *(node.config_ref for node in self.nodes.values()),
                *(
                    ref
                    for model in pins.model_pins
                    for ref in (
                        model.binding_ref,
                        model.provider_policy_ref,
                        model.fallback_policy_ref,
                    )
                ),
                *(tool.tool_ref for tool in pins.tool_pins),
            )
        )
        result = ExecutionDefinitionPin(
            execution_id=self.execution.execution_id,
            release_id=release.release_id,
            bundle_digest=release.bundle_ref.bundle_digest,
            producer_catalogue_ref=catalogue.registration.ref,
            configured_profile_refs=tuple(
                item.ref for item in catalogue.context.profiles
            ),
        )
        _projected(
            self.claims.execution_pins,
            (result,),
            lambda pin: pin.execution_id,
            "Execution definition pin",
        )
        return result

    def check_inventory(self, refs: tuple[ContentRef, ...]) -> None:
        if any(reference_identity(ref) not in self.inventory for ref in refs):
            raise ValueError(
                "versioned source references must match the exact Manifest inventory"
            )

    def check_scope(self) -> None:
        records = self.records
        all_records = (
            *records.objects,
            *records.node_runs,
            *records.attempts,
            *records.question_selections,
            *records.observations,
            *records.tool_executions,
            *records.model_invocations,
            *records.execution_events,
            *records.facts,
            *records.authority_selections,
        )
        if any(
            item.execution_id != self.execution.execution_id for item in all_records
        ):
            raise ValueError("actual records must belong to one Execution")
        if any(
            item.trace_id != self.execution.trace.trace_id
            for item in (
                *(node.trace for node in records.node_runs),
                *(event.trace for event in records.execution_events),
            )
        ):
            raise ValueError(
                "NodeRun and ExecutionEvent trace must match actual Execution"
            )
        for attempt in records.attempts:
            if isinstance(attempt.owner, SystemAttemptOwner):
                continue
            node = _find(
                self.nodes,
                attempt.owner.node_run_ref.record_id,
                "Attempt owner NodeRun",
            )
            if not any(
                (link.attempt_ref.record_id, link.attempt_number)
                == (attempt.attempt_id, attempt.attempt_number)
                for link in node.attempt_history
            ):
                raise ValueError(
                    "actual NodeRun Attempt history is missing exact Attempt identity and number"
                )
        for node in records.node_runs:
            for link in node.attempt_history:
                attempt = _find(
                    self.attempts, link.attempt_ref.record_id, "NodeRun Attempt"
                )
                if (
                    not attempt.belongs_to_node(node)
                    or attempt.attempt_number != link.attempt_number
                ):
                    raise ValueError(
                        "actual NodeRun and Attempt history links disagree"
                    )

    def node_attempt(
        self, node_id: str, attempt_id: str, expected: NodeId
    ) -> tuple[NodeRun, Attempt]:
        node = _find(self.nodes, node_id, "NodeRun")
        attempt = _find(self.attempts, attempt_id, "Attempt")
        if node.node_id is not expected or not attempt.belongs_to_node(node):
            raise ValueError(
                f"actual result must reference its {expected.value} NodeRun and Attempt"
            )
        return node, attempt

    def invocation(
        self,
        invocation_id: str,
        node: NodeRun,
        attempt: Attempt,
        purpose: ModelCapability,
    ) -> ModelInvocation:
        invocation = _find(self.invocations, invocation_id, "ModelInvocation")
        if (
            invocation.node_id,
            invocation.node_run_ref.record_id,
            invocation.attempt_ref.record_id,
            invocation.trace_id,
        ) != (
            node.node_id.value,
            node.node_run_id,
            attempt.attempt_id,
            self.execution.trace.trace_id,
        ):
            raise ValueError(
                "actual ModelInvocation must match the exact NodeRun, Attempt and trace"
            )
        if purpose not in invocation.purposes or not any(
            ref.record_id == invocation_id
            for ref in self.execution.model_invocation_refs
        ):
            raise ValueError(
                "actual ModelInvocation must be listed by Execution and have the expected purpose"
            )
        configured = invocation.configured_provider
        pin = _find(
            self.model_pins,
            reference_identity(configured.binding_ref),
            "configured model pin",
        )
        deployments = {item.deployment_id: item for item in pin.deployments}
        primary = _find(deployments, pin.primary_deployment_id, "primary deployment")
        chosen = _find(
            deployments,
            invocation.route_trace.selected_deployment_id,
            "selected deployment",
        )
        if (
            configured.primary_deployment_id,
            reference_identity(configured.provider_policy_ref),
            reference_identity(invocation.route_trace.fallback_policy_ref),
            configured.provider_id,
            configured.provider_kind,
            configured.logical_model_id,
        ) != (
            pin.primary_deployment_id,
            reference_identity(pin.provider_policy_ref),
            reference_identity(pin.fallback_policy_ref),
            primary.provider_id,
            primary.provider_kind,
            primary.logical_model_id,
        ):
            raise ValueError(
                "actual ModelInvocation configured provider differs from immutable Execution pin"
            )
        actual = invocation.actual_provider
        if actual is not None and (actual.provider_id, actual.provider_kind) != (
            chosen.provider_id,
            chosen.provider_kind,
        ):
            raise ValueError(
                "actual ModelInvocation provider differs from pinned deployment"
            )
        if isinstance(
            actual, ReportedProviderIdentity | RegistryVerifiedProviderIdentity
        ) and (
            actual.model_version
            not in chosen.actual_version_constraints.allowed_model_versions
            or actual.deployment_version
            not in chosen.actual_version_constraints.allowed_deployment_versions
        ):
            raise ValueError(
                "actual deployment/model version is outside the Execution pin"
            )
        return invocation

    def question_selection(
        self, selection: QuestionSelectionRecord
    ) -> QuestionSelectionDescriptor | None:
        identity = reference_identity(selection.profile_ref)
        context = self.execution.context_snapshot
        if (
            context is None
            or (selection.context_snapshot_ref, selection.context_snapshot_digest)
            != (context.snapshot_ref, context.snapshot_digest)
            or identity not in self.eligible
        ):
            raise ValueError(
                "actual QuestionSelection lacks eligible Profile and matching context pins"
            )
        profile = _find(self.profiles, identity, "configured Profile")
        if (
            selection.configured_question_refs != profile.question_refs
            or reference_identity(selection.policy_ref)
            != reference_identity(profile.question_selection_policy_ref)
        ):
            raise ValueError(
                "actual QuestionSelection does not match the pinned Profile Questions"
            )
        self.check_inventory(
            (selection.policy_ref, *selection.configured_question_refs)
        )
        node, attempt = self.node_attempt(
            selection.node_run_id, selection.attempt_id, NodeId.N02
        )
        invocation = self.invocation(
            selection.discovery_model_invocation_ref.record_id,
            node,
            attempt,
            ModelCapability.PROFILE_DISCOVERY,
        )
        if (
            selection.profile_ref not in invocation.profile_refs
            or reference_identity(invocation.configured_provider.binding_ref)
            != reference_identity(profile.discovery_binding_ref)
            or invocation.outcome.status != "succeeded"
        ):
            raise ValueError(
                "QuestionSelection requires the Profile's actual successful discovery invocation"
            )
        if not selection.effective_question_refs:
            return None
        return QuestionSelectionDescriptor(
            question_selection_ref=QuestionSelectionRef(
                record_kind=RuntimeRecordKind.QUESTION_SELECTION_RECORD,
                record_id=selection.selection_id,
            ),
            execution_id=selection.execution_id,
            profile_ref=selection.profile_ref,
            eligibility_qualification="validated",
            effective_question_refs=selection.effective_question_refs,
        )

    def object(self, record: ObjectRecord) -> ObjectDescriptor:
        selection = _find(
            self.selections,
            record.question_selection_ref.record_id,
            "Object QuestionSelection",
        )
        record.check_selection(self.execution.execution_id, selection)
        profile = _find(
            self.profiles, reference_identity(record.profile_ref), "Object Profile"
        )
        if record.entity_ref != profile.entity_ref:
            raise ValueError("actual Object entity differs from its pinned Profile")
        self.check_inventory((record.entity_ref,))
        return ObjectDescriptor(
            object_ref=ObjectRef(
                record_kind=RuntimeRecordKind.OBJECT, record_id=record.object_id
            ),
            execution_id=record.execution_id,
            profile_ref=record.profile_ref,
        )

    def observation(self, batch: ObservationBatch) -> None:
        obj = _find(self.objects, batch.object_ref.record_id, "Observation Object")
        if (
            batch.profile_ref,
            batch.question_selection_ref,
            batch.effective_question_refs,
        ) != (obj.profile_ref, obj.question_selection_ref, obj.effective_question_refs):
            raise ValueError(
                "actual ObservationBatch must inherit the Object's exact Question selection"
            )
        node, attempt = self.node_attempt(
            batch.node_run_ref.record_id, batch.attempt_ref.record_id, NodeId.N04
        )
        invocation = self.invocation(
            batch.model_invocation_ref.record_id,
            node,
            attempt,
            ModelCapability.OBJECT_OBSERVATION,
        )
        snapshot = invocation.contract_snapshot
        profile = _find(
            self.profiles, reference_identity(batch.profile_ref), "Observation Profile"
        )
        if (
            invocation.object_ref != batch.object_ref
            or batch.profile_ref not in invocation.profile_refs
            or reference_identity(invocation.configured_provider.binding_ref)
            != reference_identity(profile.observation_binding_ref)
            or snapshot is None
            or snapshot.effective_question_refs != batch.effective_question_refs
        ):
            raise ValueError(
                "actual ObservationBatch invocation must pin its Object, Profile and effective Questions"
            )
        if batch.outcome in ("complete", "partial") and (
            invocation.outcome.status != "succeeded"
            or not isinstance(
                invocation.actual_provider,
                ReportedProviderIdentity | RegistryVerifiedProviderIdentity,
            )
        ):
            raise ValueError(
                "qualifying ObservationBatch requires verified actual invocation versions"
            )
        for question in batch.question_results:
            fact = question.primary_fact
            if _find(self.facts, fact.fact_id, "Observation Fact") != fact:
                raise ValueError(
                    "ObservationBatch embedded Fact differs from the actual Fact"
                )

    def tool(self, tool: ToolExecution) -> ToolStepResultDescriptor | None:
        origin = tool.invocation_origin
        if not isinstance(origin, ToolPlanStepOrigin):
            return None
        self.node_attempt(
            tool.node_run_ref.record_id, tool.attempt_ref.record_id, NodeId.N05
        )
        if tool.trace_id != self.execution.trace.trace_id or tool.object_ref is None:
            raise ValueError(
                "actual Tool step must match the Execution trace and Object scope"
            )
        obj = _find(self.objects, tool.object_ref.record_id, "Tool Object")
        profile = _find(
            self.profiles, reference_identity(tool.profile_ref), "Tool Profile"
        )
        if (
            obj.profile_ref != tool.profile_ref
            or origin.tool_plan_ref != profile.tool_plan_ref
        ):
            raise ValueError(
                "actual Tool step must match the Object Profile and ToolPlan binding"
            )
        witness = tool.authority_observation
        if witness is None:
            return None
        version = tool.actual_implementation_version
        pin = _find(
            self.tool_pins, reference_identity(tool.tool_ref), "configured Tool pin"
        )
        if version != pin or version is None:
            raise ValueError(
                "Tool authority requires the actual pinned implementation version"
            )
        declarations = [
            declaration
            for declaration in self.declarations.values()
            if isinstance(declaration.producer, ToolStepProducer)
            and declaration.profile_ref is not None
            and reference_identity(declaration.profile_ref)
            == reference_identity(tool.profile_ref)
            and declaration.producer.tool_plan_ref == origin.tool_plan_ref
            and declaration.producer.tool_ref == tool.tool_ref
            and declaration.producer.step_id == origin.step_id
        ]
        actual_ids = set(origin.producer_declaration_ids)
        permitted_ids = {declaration.declaration_id for declaration in declarations}
        mapped_names = {output.output_name for output in witness.mapped_outputs}
        declared_names = {
            declaration.output_name
            for declaration in declarations
            if declaration.declaration_id in actual_ids
        }
        if not actual_ids <= permitted_ids or not mapped_names <= declared_names:
            raise ValueError(
                "actual Tool mapped output must reference its exact producer declaration"
            )
        return ToolStepResultDescriptor(
            execution_id=tool.execution_id,
            scope=FactScope.OBJECT,
            object_id=obj.object_id,
            profile_ref=tool.profile_ref,
            attempt_ref=tool.attempt_ref,
            arrival_qualification=witness.arrival_qualification,
            result_ref=ToolExecutionRef(
                record_kind=RuntimeRecordKind.TOOL_EXECUTION,
                record_id=tool.tool_execution_id,
            ),
            invocation_origin="tool_plan_step",
            tool_plan_ref=origin.tool_plan_ref,
            step_id=origin.step_id,
            tool_ref=tool.tool_ref,
            actual_implementation_version=version,
            envelope_validation=witness.envelope_validation,
            association_validation=witness.association_validation,
            mapped_outputs=witness.mapped_outputs,
        )

    def system_output(
        self, event: ExecutionEvent
    ) -> SystemOutputResultDescriptor | None:
        payload = event.payload
        if (
            not isinstance(payload, SystemOutputEvent)
            or payload.authority_output is None
        ):
            return None
        witness = payload.authority_output
        attempt = _find(
            self.attempts, payload.attempt_ref.record_id, "system output Attempt"
        )
        if (
            not isinstance(attempt.owner, SystemAttemptOwner)
            or attempt.owner.requirement != payload.requirement
            or attempt.status is not AttemptStatus.SUCCEEDED
            or attempt.started_at is None
            or event.recorded_at < attempt.started_at
        ):
            raise ValueError("system output lacks the actual successful System Attempt")
        if isinstance(witness.output, EmittedOutput):
            fact = _find(
                self.facts, witness.output.fact_ref.record_id, "system output Fact"
            )
            if (
                not isinstance(fact.source, SystemFactSource)
                or fact.source.system_output_ref.record_id != event.execution_event_id
                or fact.source.requirement != payload.requirement
                or fact.source.output_name != payload.output_name
                or fact.attempt_id != payload.attempt_ref.record_id
                or (fact.scope, fact.object_id, fact.profile_ref, fact.status)
                != (
                    witness.scope,
                    witness.object_id,
                    witness.profile_ref,
                    witness.output.fact_status,
                )
            ):
                raise ValueError(
                    "system output Fact differs from the actual system event"
                )
        return SystemOutputResultDescriptor.model_validate(
            {
                "execution_id": event.execution_id,
                "scope": witness.scope,
                **(
                    {"object_id": witness.object_id, "profile_ref": witness.profile_ref}
                    if witness.scope is FactScope.OBJECT
                    else {}
                ),
                "arrival_qualification": witness.arrival_qualification,
                "result_ref": ExecutionEventRef(
                    record_kind=RuntimeRecordKind.EXECUTION_EVENT,
                    record_id=event.execution_event_id,
                ),
                "attempt_ref": payload.attempt_ref,
                "requirement": payload.requirement,
                "output_name": payload.output_name,
                "actual_implementation_version": payload.actual_implementation_version,
                "output": witness.output,
            }
        )

    def history(self) -> LocalAuthorityHistoryInput:
        history = self.claims.history
        self.check_scope()
        _projected(
            tuple(entry.fact for entry in history.candidates.entries),
            self.records.facts,
            lambda item: item.fact_id,
            "candidate Fact",
        )
        checked_revisions = _projected(
            (*history.prior_selections, *history.proposed_selections),
            self.records.authority_selections,
            lambda item: item.selection_id,
            "authority revision",
        )
        revision_by_id = {item.selection_id: item for item in checked_revisions}
        nodes = tuple(
            NodeRunDescriptor.model_validate(
                {
                    "execution_id": node.execution_id,
                    "status": node.status,
                    "node_run_ref": NodeRunRef(
                        record_kind=RuntimeRecordKind.NODE_RUN,
                        record_id=node.node_run_id,
                    ),
                    "node_id": node.node_id.value,
                }
            )
            for node in self.nodes.values()
        )
        attempts = tuple(
            AttemptDescriptor(
                execution_id=attempt.execution_id,
                status=attempt.status,
                attempt_ref=AttemptRef(
                    record_kind=RuntimeRecordKind.ATTEMPT, record_id=attempt.attempt_id
                ),
                owner=attempt.owner,
                attempt_number=attempt.attempt_number,
            )
            for attempt in self.attempts.values()
        )
        selections = tuple(
            descriptor
            for selection in self.selections.values()
            if (descriptor := self.question_selection(selection)) is not None
        )
        for batch in self.records.observations:
            self.observation(batch)
        return LocalAuthorityHistoryInput(
            candidates=history.candidates,
            prior_selections=tuple(
                revision_by_id[item.selection_id] for item in history.prior_selections
            ),
            proposed_selections=tuple(
                revision_by_id[item.selection_id]
                for item in history.proposed_selections
            ),
            objects=_projected(
                history.objects,
                tuple(self.object(obj) for obj in self.objects.values()),
                lambda item: item.object_ref.record_id,
                "Object",
            ),
            node_runs=_projected(
                history.node_runs,
                nodes,
                lambda item: item.node_run_ref.record_id,
                "NodeRun",
            ),
            attempts=_projected(
                history.attempts,
                attempts,
                lambda item: item.attempt_ref.record_id,
                "Attempt",
            ),
            question_selections=_projected(
                history.question_selections,
                selections,
                lambda item: item.question_selection_ref.record_id,
                "QuestionSelection",
            ),
            observation_results=_projected(
                history.observation_results,
                tuple(
                    batch.to_authority_descriptor()
                    for batch in self.records.observations
                ),
                lambda item: item.result_ref.record_id,
                "ObservationBatch",
            ),
            tool_results=_projected(
                history.tool_results,
                tuple(
                    result
                    for tool in self.records.tool_executions
                    if (result := self.tool(tool)) is not None
                ),
                lambda item: item.result_ref.record_id,
                "Tool step",
            ),
            system_results=_projected(
                history.system_results,
                tuple(
                    system_result
                    for event in self.events.values()
                    if (system_result := self.system_output(event)) is not None
                ),
                lambda item: item.result_ref.record_id,
                "System output",
            ),
            questions=history.questions,
            tools=history.tools,
            plans=history.plans,
        )

    def selected_heads(self) -> tuple[AuthoritativeResultSelection, ...]:
        revisions = self.records.authority_selections
        previous = {
            selection.expected_previous_selection_ref.record_id
            for selection in revisions
            if selection.expected_previous_selection_ref is not None
        }
        heads = tuple(item for item in revisions if item.selection_id not in previous)
        for head in heads:
            if isinstance(head, ObservationBatchSelection | ToolStepSelection):
                attempt = _find(
                    self.attempts, head.selected_attempt_id, "selected Attempt"
                )
                if isinstance(attempt.owner, SystemAttemptOwner):
                    raise ValueError("selected result requires a NodeRun Attempt")
                node = _find(
                    self.nodes,
                    attempt.owner.node_run_ref.record_id,
                    "selected result NodeRun",
                )
                if _reference_key(head.selected_result_ref) not in {
                    _reference_key(ref) for ref in node.output_refs
                }:
                    raise ValueError(
                        "selected head result is absent from actual NodeRun output refs"
                    )
        return heads


def validate_runtime_authority(
    claims: ProducerAuthorityValidationInput,
    records: RuntimeAuthorityRecords,
    *,
    fact_schemas: FactSchemaRegistry,
    parse_limits: CandidateParseLimits,
    snapshot_limits: CandidateSnapshotLimits,
) -> RuntimeAuthorityProjection:
    """Require exact projections and successful B finite checks before exposing heads."""
    scope = _Scope(
        RuntimeAuthorityRecords.model_validate(records),
        ProducerAuthorityValidationInput.model_validate(claims),
    )
    pin = scope.pin()
    history = scope.history()
    report = validate_producer_authority_contracts(
        ProducerAuthorityValidationInput(
            history=history,
            producer_catalogues=scope.claims.producer_catalogues,
            execution_pins=(pin,),
        ),
        fact_schemas=fact_schemas,
        parse_limits=parse_limits,
        snapshot_limits=snapshot_limits,
    )
    if report.aggregate_status in ("invalid", "incomplete"):
        codes = sorted(
            {
                issue.code
                for check in report.checks
                if check.status in ("invalid", "incomplete")
                for issue in check.issues
            }
        )
        raise ValueError(
            f"finite B authority {report.aggregate_status}: {', '.join(codes)}"
        )
    return RuntimeAuthorityProjection(history, scope.selected_heads(), report)
