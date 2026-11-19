"""Frozen input contracts for offline evaluation of one exact execution cohort."""

from collections.abc import Hashable, Iterable
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.bundle.integrity import read_canonical_json, sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definition_support import (
    immutable_sequence,
    require_unique_references,
)
from jimu_runtime.domain.enums import DecisionResult, ExecutionStatus, FactStatus
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_schemas.projection import validate_child_reference
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef, QuestionContentRef
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.decisions import (
    DecisionRecord,
    EvaluatedRuleOutcome,
    RulePath,
)
from jimu_runtime.domain.runtime.executions import (
    ConfiguredModelPin,
    ConfiguredToolPin,
    ExecutionPins,
    ExecutionRecord,
    PinnedRuntimeInput,
)
from jimu_runtime.domain.runtime.facts import EvidenceRef, ProfileContentRef
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import BBoxXYXY, ImageDimensions
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome, GroundingOutcome
from jimu_runtime.domain.runtime.replay import ScoringProtocolPin
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.domain.values import Value

type Entries[T] = Annotated[
    tuple[T, ...], Field(max_length=1024), BeforeValidator(immutable_sequence)
]
type Samples[T] = Annotated[
    tuple[T, ...],
    Field(min_length=1, max_length=100_000),
    BeforeValidator(immutable_sequence),
]
type Objects[T] = Annotated[
    tuple[T, ...], Field(max_length=512), BeforeValidator(immutable_sequence)
]
type ModelAttempts[T] = Annotated[
    tuple[T, ...], Field(max_length=16_384), BeforeValidator(immutable_sequence)
]
type Split = Literal["training", "calibration", "holdout"]
type ReviewState = Literal["labeled", "unlabeled", "conflicted", "abstained"]


def canonical_digest(record: ContractModel) -> Digest:
    """Hash the canonical JSON of an immutable evaluation or source contract."""
    return sha256_digest(canonical_bytes(record.model_dump(mode="json")))


def require_unique(values: Iterable[Hashable], name: str) -> None:
    seen: set[Hashable] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {name}")
        seen.add(value)


def require_successor_version(version: Version, prior: Version) -> None:
    if tuple(map(int, version.split("."))) <= tuple(map(int, prior.split("."))):
        raise ValueError("successor version must follow its prior version")


class SnapshotPin(ContractModel):
    record_id: RecordId
    version: Version
    digest: Digest


class VersionedSnapshot(ContractModel):
    schema_version: Literal["1.0.0"]
    record_id: RecordId
    version: Version

    def pin(self) -> SnapshotPin:
        return SnapshotPin(
            record_id=self.record_id,
            version=self.version,
            digest=canonical_digest(self),
        )


class DatasetSample(ContractModel):
    sample_id: RecordId
    image_sha256: Digest
    image_dimensions: ImageDimensions
    dedup_group: RecordId
    split: Split


class DatasetManifest(VersionedSnapshot):
    scope: IdempotencyScope
    samples: Samples[DatasetSample]

    @model_validator(mode="after")
    def dataset_integrity(self) -> Self:
        require_unique((item.sample_id for item in self.samples), "sample ID")
        require_unique((item.image_sha256 for item in self.samples), "image digest")
        group_splits: dict[str, Split] = {}
        for item in self.samples:
            if group_splits.setdefault(item.dedup_group, item.split) != item.split:
                raise ValueError("deduplication group crosses dataset splits")
        return self


class FactBinding(ContractModel):
    ref: FactSchemaContentRef
    definition: FactSchemaDefinition
    canonical_source: Annotated[str, Field(max_length=1_048_576)] | None = None

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        if self.canonical_source is None:
            digest = canonical_digest(self.definition)
        else:
            encoded = self.canonical_source.encode("utf-8")
            raw = read_canonical_json(encoded, max_bytes=1_048_576, max_depth=64)
            if FactSchemaDefinition.model_validate(raw) != self.definition:
                raise ValueError(
                    "Fact Schema must match its canonical source definition"
                )
            digest = sha256_digest(encoded)
        if (self.ref.artifact_id, self.ref.version, self.ref.digest) != (
            self.definition.schema_id,
            self.definition.version,
            digest,
        ):
            raise ValueError("Fact Schema does not match exact catalogue reference")
        if self.definition.scope is not FactScope.OBJECT:
            raise ValueError("scored object Facts require an object-scoped Fact Schema")
        validate_child_reference(self.definition)
        return self


class QuestionBinding(ContractModel):
    question_ref: QuestionContentRef
    fact: FactBinding


class RuleBinding(ContractModel):
    path: RulePath
    category: ArtifactId


class ProfileBinding(ContractModel):
    profile_ref: ProfileContentRef
    questions: Entries[QuestionBinding]
    geometry: Entries[FactBinding]
    rules: Entries[RuleBinding]

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        require_unique_references(tuple(row.question_ref for row in self.questions))
        require_unique_references(tuple(row.ref for row in self.geometry))
        require_unique((row.path for row in self.rules), "Profile Rule path")
        return self


class EvaluationCatalogue(ContractModel):
    schema_version: Literal["1.0.0"]
    bundle_digest: Digest
    profiles: Entries[ProfileBinding]

    @model_validator(mode="after")
    def consistent_catalogue(self) -> Self:
        require_unique_references(tuple(row.profile_ref for row in self.profiles))
        bindings: dict[QuestionContentRef, FactBinding] = {}
        categories: dict[RulePath, str] = {}
        for profile in self.profiles:
            for question in profile.questions:
                if (
                    bindings.setdefault(question.question_ref, question.fact)
                    != question.fact
                ):
                    raise ValueError("exact Question must have one Fact Schema")
            for rule in profile.rules:
                if categories.setdefault(rule.path, rule.category) != rule.category:
                    raise ValueError("exact Rule path must have one category")
        if (
            max(
                len(bindings),
                len(categories),
                len(set(categories.values())),
                len(
                    {fact.ref for profile in self.profiles for fact in profile.geometry}
                ),
            )
            > 1024
        ):
            raise ValueError("catalogue exceeds the supported number of stage metrics")

        return self


class ScoringProtocol(ContractModel):
    schema_version: Literal["1.0.0"]
    protocol_id: ArtifactId
    version: Version
    catalogue: EvaluationCatalogue
    iou_threshold_basis_points: Annotated[int, Field(ge=1, le=10_000)]
    matching: Literal["greedy_iou_then_truth_id_then_prediction_id"]

    @property
    def profile_refs(self) -> tuple[ProfileContentRef, ...]:
        return tuple(row.profile_ref for row in self.catalogue.profiles)

    @property
    def question_refs(self) -> tuple[QuestionContentRef, ...]:
        return tuple(
            dict.fromkeys(
                item.question_ref
                for row in self.catalogue.profiles
                for item in row.questions
            )
        )

    @property
    def geometry_refs(self) -> tuple[FactSchemaContentRef, ...]:
        return tuple(
            dict.fromkeys(
                item.ref for row in self.catalogue.profiles for item in row.geometry
            )
        )

    @property
    def rule_paths(self) -> tuple[RulePath, ...]:
        return tuple(
            dict.fromkeys(
                item.path for row in self.catalogue.profiles for item in row.rules
            )
        )

    def pin(self) -> ScoringProtocolPin:
        return ScoringProtocolPin(
            protocol_id=self.protocol_id,
            version=self.version,
            digest=canonical_digest(self),
        )


class QuestionLabel(ContractModel):
    question_ref: QuestionContentRef
    value: Value


class GeometryLabel(ContractModel):
    fact_schema_ref: FactSchemaContentRef
    value: Value


class RuleLabel(ContractModel):
    path: RulePath
    matched: bool


class LabeledObject(ContractModel):
    label_id: RecordId
    profile_ref: ProfileContentRef
    box: BBoxXYXY
    questions: Entries[QuestionLabel]
    geometry: Entries[GeometryLabel]
    rules: Entries[RuleLabel]

    @model_validator(mode="after")
    def unique_labels(self) -> Self:
        require_unique_references(tuple(item.question_ref for item in self.questions))
        require_unique_references(tuple(item.fact_schema_ref for item in self.geometry))
        require_unique((item.path for item in self.rules), "Rule label")
        return self


class SampleLabels(ContractModel):
    # Null means no complete Profile-presence annotation. An empty tuple confirms
    # absence of all Profiles in the protocol catalogue.
    present_profiles: Entries[ProfileContentRef] | None
    objects: Objects[LabeledObject]
    task_violation: bool | None
    complete_grounding_profiles: Entries[ProfileContentRef] = ()
    violation_categories: Entries[ArtifactId] | None = None

    @model_validator(mode="after")
    def coherent_labels(self) -> Self:
        require_unique((item.label_id for item in self.objects), "object label ID")
        if self.present_profiles is not None:
            require_unique_references(self.present_profiles)
            if any(
                obj.profile_ref not in self.present_profiles for obj in self.objects
            ):
                raise ValueError("object label contradicts annotated Profile presence")
        require_unique_references(self.complete_grounding_profiles)
        if self.present_profiles is not None and (
            set(self.present_profiles) & set(self.complete_grounding_profiles)
        ) - {row.profile_ref for row in self.objects}:
            raise ValueError(
                "complete grounding for a present Profile requires labeled objects"
            )

        if self.violation_categories is not None:
            require_unique(self.violation_categories, "truth violation category")
            if (
                self.task_violation is None
                or bool(self.violation_categories) != self.task_violation
            ):
                raise ValueError(
                    "complete category truth must agree with binary task truth"
                )
        if (
            self.present_profiles is None
            and not self.objects
            and self.task_violation is None
        ):
            raise ValueError("labeled samples require an independent truth claim")
        return self


class FactPrediction(ContractModel):
    status: FactStatus
    value: Value | None = None

    @model_validator(mode="after")
    def known_has_value(self) -> Self:
        if (self.value is not None) != (self.status is FactStatus.KNOWN):
            raise ValueError("only known Facts can supply a value")
        return self


class QuestionPrediction(FactPrediction):
    question_ref: QuestionContentRef


class GeometryPrediction(FactPrediction):
    fact_schema_ref: FactSchemaContentRef


class RulePrediction(ContractModel):
    path: RulePath
    status: Literal["evaluated", "blocked", "skipped", "not_evaluated"]
    predicate: RuleTruth | None = None

    @model_validator(mode="after")
    def evaluated_predicate(self) -> Self:
        if (self.predicate is not None) != (self.status == "evaluated"):
            raise ValueError("Rule result requires an effective evaluated predicate")
        return self


class PredictedObject(ContractModel):
    object_id: RecordId
    box: BBoxXYXY
    questions: Entries[QuestionPrediction]
    geometry: Entries[GeometryPrediction]
    rules: Entries[RulePrediction]

    @model_validator(mode="after")
    def unique_predictions(self) -> Self:
        require_unique_references(tuple(item.question_ref for item in self.questions))
        require_unique_references(tuple(item.fact_schema_ref for item in self.geometry))
        require_unique((item.path for item in self.rules), "predicted Rule path")
        return self


class ProfilePrediction(ContractModel):
    profile_ref: ProfileContentRef
    grounding: GroundingOutcome
    objects: Objects[PredictedObject]

    @model_validator(mode="after")
    def grounding_result(self) -> Self:
        if (self.grounding is GroundingOutcome.OBJECTS and not self.objects) or (
            self.grounding not in (GroundingOutcome.OBJECTS, GroundingOutcome.PARTIAL)
            and self.objects
        ):
            raise ValueError("grounding objects require objects or partial outcome")
        return self


class SamplePrediction(ContractModel):
    sample_id: RecordId
    task: AcceptedTask
    execution: ExecutionRecord
    model_invocations: ModelAttempts[ModelInvocation]
    tool_executions: Entries[ToolExecution]
    discovery: DiscoveryOutcome
    profiles: Entries[ProfilePrediction]
    decision: DecisionResult | None
    decision_records: Objects[DecisionRecord] = ()
    auto_cleared: bool | None

    @model_validator(mode="after")
    def execution_source(self) -> Self:
        execution, task = self.execution, self.task
        if (
            execution.task_id,
            execution.pins.release_id,
            execution.pins.bundle_digest,
        ) != (task.task_id, task.release_id, task.bundle_digest):
            raise ValueError(
                "Task and Execution must have the same source and release pins"
            )
        if execution.status in (ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING):
            raise ValueError("evaluation requires a terminal Execution")
        require_unique(
            (entry.profile_ref for entry in self.profiles), "discovered Profile"
        )
        object_ids = tuple(
            obj.object_id for entry in self.profiles for obj in entry.objects
        )
        require_unique(object_ids, "prediction object ID")
        if len(object_ids) > 512:
            raise ValueError("too many objects for one evaluation sample")
        if (self.discovery is DiscoveryOutcome.HITS and not self.profiles) or (
            self.discovery not in (DiscoveryOutcome.HITS, DiscoveryOutcome.PARTIAL)
            and self.profiles
        ):
            raise ValueError("discovered Profiles require hits or partial outcome")
        if self.auto_cleared and (
            self.decision is not DecisionResult.NO_VIOLATION
            or execution.status is not ExecutionStatus.COMPLETED
            or self.discovery is not DiscoveryOutcome.HITS
        ):
            raise ValueError(
                "automatic clearance requires complete negative processing"
            )
        if self.decision is DecisionResult.NO_VIOLATION and (
            self.discovery is not DiscoveryOutcome.HITS
            or execution.status is not ExecutionStatus.COMPLETED
            or any(
                item.grounding is not GroundingOutcome.OBJECTS for item in self.profiles
            )
        ):
            raise ValueError(
                "no_violation requires complete discovery and grounded targets"
            )
        self._validate_decisions()
        self._validate_attempts()
        return self

    def _validate_decisions(self) -> None:
        objects = {
            obj.object_id: (entry.profile_ref, obj)
            for entry in self.profiles
            for obj in entry.objects
        }
        require_unique(
            (row.decision_id for row in self.decision_records), "Decision ID"
        )
        require_unique(
            (row.object_ref for row in self.decision_records), "Decision Object"
        )
        decisions = {row.object_ref.record_id: row for row in self.decision_records}
        if not decisions.keys() <= objects.keys():
            raise ValueError("Decision must belong to a predicted Object")
        positive = False
        for object_id, (profile_ref, obj) in objects.items():
            row = decisions.get(object_id)
            if row is None:
                if any(rule.status != "not_evaluated" for rule in obj.rules):
                    raise ValueError("Rule results require a persisted Object Decision")
                continue
            if (
                row.execution_id,
                row.release_id,
                row.bundle_digest,
                row.profile_ref,
            ) != (
                self.execution.execution_id,
                self.execution.pins.release_id,
                self.execution.pins.bundle_digest,
                profile_ref,
            ):
                raise ValueError(
                    "Decision must belong to the predicted Execution and Profile"
                )
            context = self.execution.context_snapshot
            if context is None or context.snapshot_ref != row.context_snapshot_ref:
                raise ValueError("Decision must address the selected Execution context")
            results: dict[RulePath, tuple[str, RuleTruth | None]] = {
                evaluation.rule: (
                    evaluation.result.outcome,
                    evaluation.result.predicate.truth
                    if isinstance(evaluation.result, EvaluatedRuleOutcome)
                    else None,
                )
                for evaluation in row.rule_evaluations
            }
            results.update(
                {
                    path: ("not_evaluated", None)
                    for path in row.explanation.not_evaluated_rules
                }
            )
            if {
                rule.path: (rule.status, rule.predicate) for rule in obj.rules
            } != results:
                raise ValueError(
                    "predicted Rules must exhaust and match the Decision evaluations"
                )
            positive |= row.result is DecisionResult.VIOLATION
        if positive and self.execution.status not in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.PARTIAL_FAILED,
        ):
            raise ValueError("positive Decision requires a finalized Execution")
        if (self.decision is DecisionResult.VIOLATION) != positive:
            raise ValueError(
                "positive task Decision requires matching persisted Object and Rule evidence"
            )

    def _validate_attempts(self) -> None:
        execution = self.execution
        model_ids = tuple(row.model_invocation_id for row in self.model_invocations)
        require_unique(model_ids, "ModelInvocation ID")
        require_unique(
            (row.tool_execution_id for row in self.tool_executions), "ToolExecution ID"
        )
        if not {ref.record_id for ref in execution.model_invocation_refs} <= set(
            model_ids
        ):
            raise ValueError("missing pinned ModelInvocation records")
        model_pins = {pin.binding_ref: pin for pin in execution.pins.model_pins}
        for row in self.model_invocations:
            configured = row.configured_provider
            pin = model_pins.get(configured.binding_ref)
            if (
                row.execution_id != execution.execution_id
                or pin is None
                or (
                    configured.provider_policy_ref != pin.provider_policy_ref
                    or configured.primary_deployment_id != pin.primary_deployment_id
                    or row.route_trace.fallback_policy_ref != pin.fallback_policy_ref
                )
            ):
                raise ValueError("ModelInvocation contradicts Execution configuration")
            deployments = {
                deployment.deployment_id: deployment for deployment in pin.deployments
            }
            primary = deployments[pin.primary_deployment_id]
            selected = deployments.get(row.route_trace.selected_deployment_id)
            if (
                selected is None
                or (
                    configured.provider_id,
                    configured.provider_kind,
                    configured.logical_model_id,
                )
                != (
                    primary.provider_id,
                    primary.provider_kind,
                    primary.logical_model_id,
                )
                or (
                    row.actual_provider is not None
                    and (
                        row.actual_provider.provider_id,
                        row.actual_provider.provider_kind,
                    )
                    != (selected.provider_id, selected.provider_kind)
                )
            ):
                raise ValueError(
                    "ModelInvocation provider identity contradicts Execution pins"
                )
        tool_refs = {pin.tool_ref for pin in execution.pins.tool_pins}
        if any(
            row.execution_id != execution.execution_id or row.tool_ref not in tool_refs
            for row in self.tool_executions
        ):
            raise ValueError("ToolExecution contradicts Execution configuration")


class ExecutionCohort(ContractModel):
    """Execution configuration shared by the trial. Each input digest stays per sample."""

    release_id: RecordId
    bundle_digest: Digest
    runtime_version: Version
    workflow_version: Version
    model_pins: Entries[ConfiguredModelPin]
    tool_pins: Entries[ConfiguredToolPin]

    @classmethod
    def from_execution(cls, pins: ExecutionPins) -> Self:
        return cls.model_validate(pins.model_dump(exclude={"input_digest"}))


class PredictionSnapshot(VersionedSnapshot):
    dataset: SnapshotPin
    scope: IdempotencyScope
    split: Literal["holdout"]
    cohort: ExecutionCohort
    producer: ActorRef
    entries: Samples[SamplePrediction]

    @model_validator(mode="after")
    def prediction_identity(self) -> Self:
        require_unique(
            (entry.sample_id for entry in self.entries), "prediction sample ID"
        )
        require_unique(
            (entry.execution.execution_id for entry in self.entries), "Execution ID"
        )
        require_unique((entry.task.task_id for entry in self.entries), "Task ID")
        require_unique(
            (
                row.model_invocation_id
                for entry in self.entries
                for row in entry.model_invocations
            ),
            "ModelInvocation ID across samples",
        )
        require_unique(
            (
                row.tool_execution_id
                for entry in self.entries
                for row in entry.tool_executions
            ),
            "ToolExecution ID across samples",
        )

        if any(
            entry.task.idempotency_scope != self.scope
            or ExecutionCohort.from_execution(entry.execution.pins) != self.cohort
            for entry in self.entries
        ):
            raise ValueError("prediction samples must share the exact cohort and scope")
        return self


class ExecutionEvidence(PinnedRuntimeInput):
    ref: EvidenceRef


class SampleSource(ContractModel):
    sample_id: RecordId
    task_digest: Digest
    execution_digest: Digest
    execution_input_digest: Digest
    model_digests: ModelAttempts[Digest]
    tool_digests: Entries[Digest]
    decision_digests: Objects[Digest]
    evidence: Entries[ExecutionEvidence]
    history_digest: Digest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def unique_sources(self) -> Self:
        for records in (self.model_digests, self.tool_digests, self.decision_digests):
            require_unique(records, "journal digest")
        require_unique((item.ref for item in self.evidence), "evidence reference")
        return self


class TrialSource(VersionedSnapshot):
    """Independent authenticated export of complete runtime input and audit inventories."""

    dataset: SnapshotPin
    predictions: SnapshotPin
    scope: IdempotencyScope
    rows: Samples[SampleSource]

    @model_validator(mode="after")
    def source_inventory(self) -> Self:
        require_unique((row.sample_id for row in self.rows), "source sample ID")
        require_unique(
            (item.ref for row in self.rows for item in row.evidence),
            "cross-sample evidence reference",
        )
        return self

    def reconcile(self, predictions: PredictionSnapshot) -> dict[str, SampleSource]:
        if (self.dataset, self.predictions, self.scope) != (
            predictions.dataset,
            predictions.pin(),
            predictions.scope,
        ):
            raise ValueError(
                "trusted source does not pin this exact prediction snapshot"
            )
        by_sample = {row.sample_id: row for row in self.rows}
        if by_sample.keys() != {row.sample_id for row in predictions.entries}:
            raise ValueError("trusted source must exhaust the prediction samples")
        for entry in predictions.entries:
            source = by_sample[entry.sample_id]
            if (
                source.task_digest,
                source.execution_digest,
                source.execution_input_digest,
            ) != (
                canonical_digest(entry.task),
                canonical_digest(entry.execution),
                entry.execution.pins.input_digest,
            ):
                raise ValueError(
                    "Task or Execution input differs from the frozen source"
                )
            if (
                set(source.model_digests)
                != {canonical_digest(row) for row in entry.model_invocations}
                or set(source.tool_digests)
                != {canonical_digest(row) for row in entry.tool_executions}
                or set(source.decision_digests)
                != {canonical_digest(row) for row in entry.decision_records}
            ):
                raise ValueError(
                    "ModelInvocation, ToolExecution or Decision inventory differs from the frozen source"
                )
        return by_sample
