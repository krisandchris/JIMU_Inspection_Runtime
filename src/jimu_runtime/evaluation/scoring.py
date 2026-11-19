"""Deterministic offline stage scoring of independently pinned snapshots."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from fractions import Fraction
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import DecisionResult, ExecutionStatus, FactStatus
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef, QuestionContentRef
from jimu_runtime.domain.primitives import ArtifactId, Digest, RecordId, Version
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.runtime.decisions import EvaluatedRuleOutcome, RulePath
from jimu_runtime.domain.runtime.facts import ProfileContentRef
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ActualProviderIdentity
from jimu_runtime.domain.runtime.media import BBoxXYXY
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome, GroundingOutcome
from jimu_runtime.domain.runtime.replay import ScoringProtocolPin
from jimu_runtime.domain.runtime.tool_executions import ImplementationVersionResolution
from jimu_runtime.domain.values import Value, validate_typed_value
from jimu_runtime.evaluation.contracts import (
    DatasetManifest,
    DatasetSample,
    Entries,
    ExecutionCohort,
    ExecutionEvidence,
    FactPrediction,
    LabeledObject,
    ModelAttempts,
    Objects,
    PredictedObject,
    PredictionSnapshot,
    ReviewState,
    RulePrediction,
    SampleLabels,
    SamplePrediction,
    Samples,
    SampleSource,
    ScoringProtocol,
    SnapshotPin,
    TrialSource,
    VersionedSnapshot,
    canonical_digest,
    require_unique,
)
from jimu_runtime.evaluation.truth import TruthEntry, TruthSnapshot

type Count = Annotated[int, Field(ge=0, le=2**53 - 1)]
type Probability = Annotated[float, Field(ge=0.0, le=1.0)]
type Outcome = Literal[
    "correct",
    "incorrect",
    "unknown",
    "error",
    "not_applicable",
    "not_evaluated",
    "missing",
    "unmatched",
    "blocked",
    "skipped",
]


class Ratio(ContractModel):
    numerator: Count
    denominator: Count
    value: Probability | None

    @model_validator(mode="after")
    def check_ratio(self) -> Self:
        expected = self.numerator / self.denominator if self.denominator else None
        if self.numerator > self.denominator or self.value != expected:
            raise ValueError("rate must agree with numerator and denominator")
        return self

    @classmethod
    def of(cls, numerator: int, denominator: int) -> Self:
        return cls(
            numerator=numerator,
            denominator=denominator,
            value=numerator / denominator if denominator else None,
        )


class ComparisonCounts(ContractModel):
    correct: Count
    incorrect: Count
    unknown: Count
    error: Count
    not_applicable: Count
    not_evaluated: Count
    missing: Count
    unmatched: Count
    blocked: Count
    skipped: Count
    accuracy: Ratio
    coverage: Ratio

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        covered = self.correct + self.incorrect
        total = covered + (
            self.unknown
            + self.error
            + self.not_applicable
            + self.not_evaluated
            + self.missing
            + self.unmatched
            + self.blocked
            + self.skipped
        )
        if self.accuracy != Ratio.of(
            self.correct, covered
        ) or self.coverage != Ratio.of(covered, total):
            raise ValueError("comparison rates must agree with outcome counts")
        return self

    @classmethod
    def summarize(cls, outcomes: Iterable[Outcome]) -> Self:
        counts = Counter(outcomes)
        covered = counts["correct"] + counts["incorrect"]
        return cls(
            correct=counts["correct"],
            incorrect=counts["incorrect"],
            unknown=counts["unknown"],
            error=counts["error"],
            not_applicable=counts["not_applicable"],
            not_evaluated=counts["not_evaluated"],
            missing=counts["missing"],
            unmatched=counts["unmatched"],
            blocked=counts["blocked"],
            skipped=counts["skipped"],
            accuracy=Ratio.of(counts["correct"], covered),
            coverage=Ratio.of(covered, counts.total()),
        )


class ProfileCheck(ContractModel):
    profile_ref: ProfileContentRef
    hit: bool


class GroundingCheck(ContractModel):
    profile_ref: ProfileContentRef
    outcome: GroundingOutcome


class QuestionCheck(ContractModel):
    question_ref: QuestionContentRef
    outcome: Outcome


class GeometryCheck(ContractModel):
    fact_schema_ref: FactSchemaContentRef
    outcome: Outcome


class RuleCheck(ContractModel):
    path: RulePath
    outcome: Outcome


class BoxMatch(ContractModel):
    object_id: RecordId
    iou: Probability


class ObjectCheck(ContractModel):
    label_id: RecordId
    profile_ref: ProfileContentRef
    match: BoxMatch | None
    questions: Entries[QuestionCheck]
    geometry: Entries[GeometryCheck]
    rules: Entries[RuleCheck]


class UnmatchedObject(ContractModel):
    object_id: RecordId
    profile_ref: ProfileContentRef
    confirmed_false_positive: bool


class ModelSource(ContractModel):
    invocation_id: RecordId
    digest: Digest
    outcome: Literal["succeeded", "failed", "not_dispatched"]
    actual_provider: ActualProviderIdentity | None


class ToolSource(ContractModel):
    tool_execution_id: RecordId
    digest: Digest
    outcome: Literal["succeeded", "partial", "failed", "not_dispatched"]
    version_resolution: ImplementationVersionResolution
    actual_version: Version | None


class SampleScore(ContractModel):
    sample_id: RecordId
    image_sha256: Digest
    task_id: RecordId
    task_digest: Digest
    execution_id: RecordId
    execution_digest: Digest
    execution_input_digest: Digest
    execution_status: ExecutionStatus
    models: ModelAttempts[ModelSource]
    tools: Entries[ToolSource]
    review_state: ReviewState
    target_present: bool | None
    discovery: DiscoveryOutcome
    profiles: Entries[ProfileCheck]
    grounding: Entries[GroundingCheck]
    objects: Objects[ObjectCheck]
    unmatched_predictions: Objects[UnmatchedObject]
    evidence: Entries[ExecutionEvidence]
    labeled_categories: Entries[ArtifactId] | None
    predicted_categories: Entries[ArtifactId]
    task_violation: bool | None
    decision: DecisionResult | None
    auto_cleared: bool | None

    @model_validator(mode="after")
    def check_object_and_category_counts(self) -> Self:
        require_unique((row.label_id for row in self.objects), "scored object label")
        require_unique(
            tuple(row.match.object_id for row in self.objects if row.match is not None)
            + tuple(row.object_id for row in self.unmatched_predictions),
            "scored predicted object",
        )
        require_unique(self.predicted_categories, "predicted category")
        if self.labeled_categories is not None:
            require_unique(self.labeled_categories, "labeled category")
            if (
                self.task_violation is None
                or bool(self.labeled_categories) != self.task_violation
            ):
                raise ValueError("category and binary truth disagree")
        if bool(self.predicted_categories) != (
            self.decision is DecisionResult.VIOLATION
        ):
            raise ValueError("category and binary prediction disagree")
        return self


class ProfileMetrics(ContractModel):
    profile_ref: ProfileContentRef
    recall: Ratio


class QuestionMetrics(ContractModel):
    question_ref: QuestionContentRef
    comparison: ComparisonCounts


class GeometryMetrics(ContractModel):
    fact_schema_ref: FactSchemaContentRef
    comparison: ComparisonCounts


class RuleMetrics(ContractModel):
    path: RulePath
    comparison: ComparisonCounts


class CategoryMetrics(ContractModel):
    category: ArtifactId
    precision: Ratio
    recall: Ratio


class Summary(ContractModel):
    samples: Count
    labeled_samples: Count
    unlabeled_samples: Count
    conflicted_samples: Count
    abstained_samples: Count
    task_labeled_samples: Count
    task_unknown_truth_samples: Count
    profile_recall: Ratio
    profiles: Entries[ProfileMetrics]
    grounding_recall: Ratio
    grounding_unmatched_predictions: Count
    grounding_false_positives: Count
    grounding_unmatched_without_full_truth: Count
    matched_mean_iou: Probability | None
    # Mean IoU uses the match numerator of grounding_recall as its sample count.
    questions: Entries[QuestionMetrics]
    geometry: Entries[GeometryMetrics]
    rules: Entries[RuleMetrics]
    tool_success: Ratio
    tool_failed: Count
    tool_partial: Count
    tool_not_dispatched: Count
    tool_implementation_unavailable: Count
    model_version_unavailable: Count
    discovery_no_hits: Ratio
    discovery_not_evaluated: Count
    discovery_failed: Count
    discovery_partial: Count
    no_hits_confirmed_target_miss_lower_bound: Ratio
    no_hits_labeled_target_miss: Ratio
    no_hits_target_truth_unavailable: Count
    category_labeled_samples: Count
    category_unlabeled_predictions: Count
    categories: Entries[CategoryMetrics]
    task_violation_precision: Ratio
    task_violation_predictions_without_truth: Count
    task_violation_recall: Ratio
    task_violation_miss_rate: Ratio
    erroneous_release_rate: Ratio | None
    confirmed_erroneous_releases: Count | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    labeled_violations_without_release_observation: Count | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    automatic_release_risk: Ratio
    automatic_release_coverage: Ratio
    automatic_release_observation_coverage: Ratio | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    automatic_release_unobserved: Count | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    automatic_release_unlabeled: Count
    uncertain_tasks: Count
    not_applicable_tasks: Count
    not_evaluated_tasks: Count
    failed_executions: Count
    partial_failed_executions: Count
    cancelled_executions: Count


class EvaluationReport(VersionedSnapshot):
    scope: IdempotencyScope
    split: Literal["holdout"]
    dataset: SnapshotPin
    truth: SnapshotPin
    predictions: SnapshotPin
    scoring_protocol: ScoringProtocolPin
    protocol: ScoringProtocol
    trial_source: SnapshotPin
    cohort: ExecutionCohort
    samples: Samples[SampleScore]
    summary: Summary

    @model_validator(mode="after")
    def verify_summary(self) -> Self:
        for field in ("sample_id", "task_id", "execution_id", "image_sha256"):
            require_unique(
                (getattr(row, field) for row in self.samples), f"report {field}"
            )
        require_unique(
            (item.ref for row in self.samples for item in row.evidence),
            "report evidence reference",
        )
        if (
            self.protocol.pin() != self.scoring_protocol
            or self.protocol.catalogue.bundle_digest != self.cohort.bundle_digest
        ):
            raise ValueError("report protocol or Bundle pin mismatch")
        if self.record_id != _report_id(
            self.dataset,
            self.truth,
            self.predictions,
            self.scoring_protocol,
            self.trial_source,
        ):
            raise ValueError("report identity must bind all exact input pins")
        categories = {
            rule.category
            for profile in self.protocol.catalogue.profiles
            for rule in profile.rules
        }
        if any(
            not set((*row.predicted_categories, *(row.labeled_categories or ())))
            <= categories
            for row in self.samples
        ):
            raise ValueError(
                "report categories must belong to the exact protocol catalogue"
            )
        if self.summary != _summarize(self.samples, self.protocol):
            raise ValueError("report summary differs from sample results")
        return self


def _report_id(
    dataset: SnapshotPin,
    truth: SnapshotPin,
    predictions: SnapshotPin,
    protocol: ScoringProtocolPin,
    source: SnapshotPin,
) -> str:
    digest = sha256_digest(
        canonical_bytes(
            [
                dataset.digest,
                truth.digest,
                predictions.digest,
                protocol.digest,
                source.digest,
            ]
        )
    )
    return f"evaluation.{digest}"


def _check_inputs(
    dataset: DatasetManifest,
    truth: TruthSnapshot,
    predictions: PredictionSnapshot,
    protocol: ScoringProtocol,
    expected_protocol: ScoringProtocolPin,
) -> dict[str, DatasetSample]:
    dataset_pin = dataset.pin()
    if truth.dataset != dataset_pin or predictions.dataset != dataset_pin:
        raise ValueError("truth and predictions must pin this exact dataset snapshot")
    if dataset.scope != truth.scope or dataset.scope != predictions.scope:
        raise ValueError("assessment scope mismatch")
    if truth.split != predictions.split:
        raise ValueError("truth and prediction splits must match")
    if (
        protocol.pin() != expected_protocol
        or protocol.catalogue.bundle_digest != predictions.cohort.bundle_digest
    ):
        raise ValueError("assessment protocol or Bundle digest mismatch")
    if truth.catalogue_digest != canonical_digest(protocol.catalogue):
        raise ValueError("truth and protocol must use the same annotation catalogue")
    samples = {
        sample.sample_id: sample
        for sample in dataset.samples
        if sample.split == truth.split
    }
    if samples.keys() != {entry.sample_id for entry in truth.entries}:
        raise ValueError("truth must account for every holdout sample exactly once")
    if samples.keys() != {entry.sample_id for entry in predictions.entries}:
        raise ValueError(
            "predictions must account for every holdout sample exactly once"
        )
    return samples


def _ref_key(ref: ContentRef) -> tuple[str, str, str]:
    return ref.artifact_id, ref.version, ref.digest


def _rule_key(path: RulePath) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    return _ref_key(path.rule_set_ref), _ref_key(path.rule_ref)


class _Catalogue:
    def __init__(self, protocol: ScoringProtocol) -> None:
        self.profiles = {row.profile_ref: row for row in protocol.catalogue.profiles}
        self.categories = {
            rule.category
            for profile in self.profiles.values()
            for rule in profile.rules
        }

    def check_object(
        self, profile_ref: ProfileContentRef, item: LabeledObject | PredictedObject
    ) -> None:
        profile = self.profiles[profile_ref]
        questions = {row.question_ref: row.fact.definition for row in profile.questions}
        geometry = {row.ref: row.definition for row in profile.geometry}
        rules = {row.path for row in profile.rules}
        if any(row.path not in rules for row in item.rules):
            raise ValueError("object Rule is outside its Profile binding")
        for definition, value in (
            *((questions.get(row.question_ref), row.value) for row in item.questions),
            *((geometry.get(row.fact_schema_ref), row.value) for row in item.geometry),
        ):
            if definition is None:
                raise ValueError(
                    "object Question or Geometry is outside its Profile binding"
                )
            if value is not None:
                try:
                    validate_typed_value(definition.type_definition, value)
                except ValueError as exc:
                    raise ValueError(
                        "object value violates the pinned Fact Schema"
                    ) from exc

    def predicted_categories(self, prediction: SamplePrediction) -> tuple[str, ...]:
        categories: set[str] = set()
        for decision in prediction.decision_records:
            rules = {
                row.path: row.category
                for row in self.profiles[decision.profile_ref].rules
            }
            matched: set[str] = set()
            for row in decision.rule_evaluations:
                if isinstance(row.result, EvaluatedRuleOutcome) and row.result.matched:
                    if row.rule not in rules:
                        raise ValueError(
                            "matched Rule has no category in its Profile binding"
                        )
                    matched.add(rules[row.rule])
            if matched != set(decision.conflict_resolution.matched_category_ids):
                raise ValueError(
                    "Decision categories disagree with its exact Rule bindings"
                )
            categories.update(matched)
        return tuple(sorted(categories))


def _iou(left: BBoxXYXY, right: BBoxXYXY) -> Fraction:
    # Exact arithmetic over binary64 input coordinates avoids rounded ties and
    # zero area after multiplication for very small, valid boxes.
    ax0, ay0, ax1, ay1 = map(Fraction, left.xyxy)
    bx0, by0, bx1, by1 = map(Fraction, right.xyxy)
    width = min(ax1, bx1) - max(ax0, bx0)
    height = min(ay1, by1) - max(ay0, by0)
    if width <= 0 or height <= 0:
        return Fraction(0)
    intersection = width * height
    left_area = (ax1 - ax0) * (ay1 - ay0)
    right_area = (bx1 - bx0) * (by1 - by0)
    return intersection / (left_area + right_area - intersection)


def _matches(
    labels: tuple[LabeledObject, ...],
    predicted: dict[ProfileContentRef, tuple[PredictedObject, ...]],
    threshold: Fraction,
) -> dict[str, tuple[PredictedObject, Fraction]]:
    candidates: list[tuple[Fraction, str, str]] = []
    by_id: dict[str, PredictedObject] = {}
    for label in labels:
        for obj in predicted.get(label.profile_ref, ()):
            iou = _iou(label.box, obj.box)
            if iou >= threshold:
                candidates.append((-iou, label.label_id, obj.object_id))
                by_id[obj.object_id] = obj
    matched: dict[str, tuple[PredictedObject, Fraction]] = {}
    used_predictions: set[str] = set()
    for negative_iou, label_id, object_id in sorted(candidates):
        if label_id not in matched and object_id not in used_predictions:
            matched[label_id] = by_id[object_id], -negative_iou
            used_predictions.add(object_id)
    return matched


def _fact_check(truth: Value, prediction: FactPrediction | None) -> Outcome:
    if prediction is None:
        return "missing"
    match prediction.status:
        case FactStatus.KNOWN:
            return "correct" if prediction.value == truth else "incorrect"
        case FactStatus.UNKNOWN:
            return "unknown"
        case FactStatus.ERROR:
            return "error"
        case FactStatus.NOT_APPLICABLE:
            return "not_applicable"
        case FactStatus.NOT_EVALUATED:
            return "not_evaluated"


def _rule_check(truth: bool, prediction: RulePrediction | None) -> Outcome:
    if prediction is None:
        return "missing"
    if prediction.status != "evaluated":
        return prediction.status
    if prediction.predicate is RuleTruth.UNKNOWN:
        return "unknown"
    return (
        "correct" if truth == (prediction.predicate is RuleTruth.TRUE) else "incorrect"
    )


def _object_score(
    truth: LabeledObject, match: tuple[PredictedObject, Fraction] | None
) -> ObjectCheck:
    pred, iou = match if match is not None else (None, None)
    questions = {row.question_ref: row for row in pred.questions} if pred else {}
    geometry = {row.fact_schema_ref: row for row in pred.geometry} if pred else {}
    rules = {row.path: row for row in pred.rules} if pred else {}
    return ObjectCheck(
        label_id=truth.label_id,
        profile_ref=truth.profile_ref,
        match=BoxMatch(object_id=pred.object_id, iou=float(iou))
        if pred and iou is not None
        else None,
        questions=tuple(
            QuestionCheck(
                question_ref=row.question_ref,
                outcome=_fact_check(row.value, questions.get(row.question_ref))
                if pred
                else "unmatched",
            )
            for row in sorted(
                truth.questions, key=lambda row: _ref_key(row.question_ref)
            )
        ),
        geometry=tuple(
            GeometryCheck(
                fact_schema_ref=row.fact_schema_ref,
                outcome=_fact_check(row.value, geometry.get(row.fact_schema_ref))
                if pred
                else "unmatched",
            )
            for row in sorted(
                truth.geometry, key=lambda row: _ref_key(row.fact_schema_ref)
            )
        ),
        rules=tuple(
            RuleCheck(
                path=row.path,
                outcome=_rule_check(row.matched, rules.get(row.path))
                if pred
                else "unmatched",
            )
            for row in sorted(truth.rules, key=lambda row: _rule_key(row.path))
        ),
    )


def _check_dataset_sample(
    sample: DatasetSample,
    labels: SampleLabels | None,
    pred: SamplePrediction,
    catalogue: _Catalogue,
) -> None:
    if pred.task.media_ref.sha256 != sample.image_sha256:
        raise ValueError("prediction image does not match the frozen dataset sample")
    if labels is not None:
        if (
            labels.violation_categories is not None
            and not set(labels.violation_categories) <= catalogue.categories
        ):
            raise ValueError("category label outside the annotation catalogue")
        for ref in (
            *tuple(labels.present_profiles or ()),
            *labels.complete_grounding_profiles,
        ):
            if ref not in catalogue.profiles:
                raise ValueError("truth Profile outside the exact protocol catalogue")
        for obj in labels.objects:
            if obj.profile_ref not in catalogue.profiles:
                raise ValueError(
                    "labeled object Profile outside the exact protocol catalogue"
                )
            obj.box.validate_bounds(sample.image_dimensions)
            catalogue.check_object(obj.profile_ref, obj)
    for profile in pred.profiles:
        if profile.profile_ref not in catalogue.profiles:
            raise ValueError("prediction Profile outside the exact protocol catalogue")
        for detected in profile.objects:
            detected.box.validate_bounds(sample.image_dimensions)
            catalogue.check_object(profile.profile_ref, detected)


def _sample_score(
    sample: DatasetSample,
    truth: TruthEntry,
    pred: SamplePrediction,
    source: SampleSource,
    catalogue: _Catalogue,
    threshold: Fraction,
) -> SampleScore:
    labels = truth.labels
    _check_dataset_sample(sample, labels, pred, catalogue)
    predictions = {row.profile_ref: row.objects for row in pred.profiles}
    present = labels.present_profiles if labels is not None else None
    targets = labels.objects if labels is not None else ()
    target_present = bool(present) if present is not None else True if targets else None
    matches = _matches(targets, predictions, threshold)
    used = {item.object_id for item, _ in matches.values()}
    complete_grounding = set(labels.complete_grounding_profiles) if labels else set()
    if present is not None:
        complete_grounding.update(catalogue.profiles.keys() - set(present))
    return SampleScore(
        sample_id=sample.sample_id,
        image_sha256=sample.image_sha256,
        task_id=pred.task.task_id,
        task_digest=canonical_digest(pred.task),
        execution_id=pred.execution.execution_id,
        execution_digest=canonical_digest(pred.execution),
        execution_input_digest=pred.execution.pins.input_digest,
        execution_status=pred.execution.status,
        models=tuple(
            ModelSource(
                invocation_id=row.model_invocation_id,
                digest=canonical_digest(row),
                outcome=row.outcome.status,
                actual_provider=row.actual_provider,
            )
            for row in sorted(
                pred.model_invocations, key=lambda row: row.model_invocation_id
            )
        ),
        tools=tuple(
            ToolSource(
                tool_execution_id=row.tool_execution_id,
                digest=canonical_digest(row),
                outcome=row.status,
                version_resolution=row.implementation_version_resolution,
                actual_version=row.actual_implementation_version,
            )
            for row in sorted(
                pred.tool_executions, key=lambda row: row.tool_execution_id
            )
        ),
        review_state=truth.state,
        target_present=target_present,
        discovery=pred.discovery,
        profiles=tuple(
            ProfileCheck(profile_ref=ref, hit=ref in predictions)
            for ref in sorted(
                set(present or ()) | {row.profile_ref for row in targets}, key=_ref_key
            )
        ),
        grounding=tuple(
            GroundingCheck(profile_ref=row.profile_ref, outcome=row.grounding)
            for row in sorted(pred.profiles, key=lambda row: _ref_key(row.profile_ref))
        ),
        objects=tuple(
            _object_score(target, matches.get(target.label_id))
            for target in sorted(targets, key=lambda row: row.label_id)
        ),
        unmatched_predictions=tuple(
            UnmatchedObject(
                object_id=obj.object_id,
                profile_ref=ref,
                confirmed_false_positive=ref in complete_grounding,
            )
            for ref, objects in sorted(
                predictions.items(), key=lambda item: _ref_key(item[0])
            )
            for obj in sorted(objects, key=lambda item: item.object_id)
            if obj.object_id not in used
        ),
        evidence=tuple(sorted(source.evidence, key=lambda item: item.ref.record_id)),
        labeled_categories=labels.violation_categories if labels else None,
        predicted_categories=catalogue.predicted_categories(pred),
        task_violation=labels.task_violation if labels is not None else None,
        decision=pred.decision,
        auto_cleared=pred.auto_cleared,
    )


def _summarize(rows: tuple[SampleScore, ...], protocol: ScoringProtocol) -> Summary:
    reviews: Counter[ReviewState] = Counter(row.review_state for row in rows)
    discovery = Counter(row.discovery for row in rows)
    executions = Counter(row.execution_status for row in rows)
    decisions = Counter(row.decision for row in rows)
    tool_status = Counter(tool.outcome for row in rows for tool in row.tools)
    tool_version = Counter(
        tool.version_resolution for row in rows for tool in row.tools
    )
    profiles: dict[ProfileContentRef, list[bool]] = defaultdict(list)
    questions: dict[QuestionContentRef, list[Outcome]] = defaultdict(list)
    geometry: dict[FactSchemaContentRef, list[Outcome]] = defaultdict(list)
    rules: dict[RulePath, list[Outcome]] = defaultdict(list)
    ious: list[float] = []
    labeled_objects = 0
    for row in rows:
        for profile in row.profiles:
            profiles[profile.profile_ref].append(profile.hit)
        for obj in row.objects:
            labeled_objects += 1
            if obj.match is not None:
                ious.append(obj.match.iou)
            for question in obj.questions:
                questions[question.question_ref].append(question.outcome)
            for fact in obj.geometry:
                geometry[fact.fact_schema_ref].append(fact.outcome)
            for rule in obj.rules:
                rules[rule.path].append(rule.outcome)

    profile_hits = sum(sum(group) for group in profiles.values())
    profile_labels = sum(len(group) for group in profiles.values())
    labeled_tasks = [row for row in rows if row.task_violation is not None]
    no_hits = [row for row in rows if row.discovery is DiscoveryOutcome.NO_HITS]
    no_hits_labeled = [row for row in no_hits if row.target_present is not None]
    true_positives = sum(
        row.task_violation is True and row.decision is DecisionResult.VIOLATION
        for row in labeled_tasks
    )
    labeled_violation_predictions = sum(
        row.decision is DecisionResult.VIOLATION for row in labeled_tasks
    )
    all_violations = sum(row.task_violation is True for row in labeled_tasks)
    erroneous_releases = sum(
        row.task_violation is True and row.auto_cleared is True for row in labeled_tasks
    )
    labeled_auto_clears = sum(row.auto_cleared is True for row in labeled_tasks)
    all_auto_clears = sum(row.auto_cleared is True for row in rows)
    releases_observed = sum(row.auto_cleared is not None for row in rows)
    releases_unobserved = len(rows) - releases_observed
    violations_without_release_observation = sum(
        row.auto_cleared is None and row.task_violation is True for row in rows
    )

    no_hits_target_misses = sum(row.target_present is True for row in no_hits_labeled)
    unmatched = sum(len(row.unmatched_predictions) for row in rows)
    false_positives = sum(
        item.confirmed_false_positive
        for row in rows
        for item in row.unmatched_predictions
    )
    known_categories = [row for row in rows if row.labeled_categories is not None]
    category_metrics = []
    for category in sorted(
        {
            rule.category
            for profile in protocol.catalogue.profiles
            for rule in profile.rules
        }
    ):
        actual = sum(
            category in (row.labeled_categories or ()) for row in known_categories
        )
        predicted = sum(
            category in row.predicted_categories for row in known_categories
        )
        correct = sum(
            category in (row.labeled_categories or ())
            and category in row.predicted_categories
            for row in known_categories
        )
        category_metrics.append(
            CategoryMetrics(
                category=category,
                precision=Ratio.of(correct, predicted),
                recall=Ratio.of(correct, actual),
            )
        )
    return Summary(
        samples=len(rows),
        labeled_samples=reviews["labeled"],
        unlabeled_samples=reviews["unlabeled"],
        conflicted_samples=reviews["conflicted"],
        abstained_samples=reviews["abstained"],
        task_labeled_samples=len(labeled_tasks),
        task_unknown_truth_samples=len(rows) - len(labeled_tasks),
        profile_recall=Ratio.of(profile_hits, profile_labels),
        profiles=tuple(
            ProfileMetrics(
                profile_ref=ref, recall=Ratio.of(sum(profiles[ref]), len(profiles[ref]))
            )
            for ref in sorted(protocol.profile_refs, key=_ref_key)
        ),
        grounding_recall=Ratio.of(len(ious), labeled_objects),
        grounding_unmatched_predictions=unmatched,
        grounding_false_positives=false_positives,
        grounding_unmatched_without_full_truth=unmatched - false_positives,
        matched_mean_iou=sum(ious) / len(ious) if ious else None,
        questions=tuple(
            QuestionMetrics(
                question_ref=ref, comparison=ComparisonCounts.summarize(questions[ref])
            )
            for ref in sorted(protocol.question_refs, key=_ref_key)
        ),
        geometry=tuple(
            GeometryMetrics(
                fact_schema_ref=ref,
                comparison=ComparisonCounts.summarize(geometry[ref]),
            )
            for ref in sorted(protocol.geometry_refs, key=_ref_key)
        ),
        rules=tuple(
            RuleMetrics(path=path, comparison=ComparisonCounts.summarize(rules[path]))
            for path in sorted(protocol.rule_paths, key=_rule_key)
        ),
        tool_success=Ratio.of(
            tool_status["succeeded"],
            tool_status.total() - tool_status["not_dispatched"],
        ),
        tool_failed=tool_status["failed"],
        tool_partial=tool_status["partial"],
        tool_not_dispatched=tool_status["not_dispatched"],
        tool_implementation_unavailable=tool_version["unavailable"],
        model_version_unavailable=sum(
            model.actual_provider is not None
            and model.actual_provider.version_resolution == "unavailable"
            for row in rows
            for model in row.models
        ),
        discovery_no_hits=Ratio.of(len(no_hits), len(rows)),
        discovery_not_evaluated=discovery[DiscoveryOutcome.NOT_EVALUATED],
        discovery_failed=discovery[DiscoveryOutcome.FAILED],
        discovery_partial=discovery[DiscoveryOutcome.PARTIAL],
        no_hits_confirmed_target_miss_lower_bound=Ratio.of(
            no_hits_target_misses, len(no_hits)
        ),
        no_hits_labeled_target_miss=Ratio.of(
            no_hits_target_misses, len(no_hits_labeled)
        ),
        no_hits_target_truth_unavailable=len(no_hits) - len(no_hits_labeled),
        category_labeled_samples=len(known_categories),
        category_unlabeled_predictions=sum(
            len(row.predicted_categories)
            for row in rows
            if row.labeled_categories is None
        ),
        categories=tuple(category_metrics),
        task_violation_precision=Ratio.of(
            true_positives, labeled_violation_predictions
        ),
        task_violation_predictions_without_truth=(
            decisions[DecisionResult.VIOLATION] - labeled_violation_predictions
        ),
        task_violation_recall=Ratio.of(true_positives, all_violations),
        task_violation_miss_rate=Ratio.of(
            all_violations - true_positives, all_violations
        ),
        erroneous_release_rate=None
        if violations_without_release_observation
        else Ratio.of(erroneous_releases, all_violations),
        confirmed_erroneous_releases=erroneous_releases
        if releases_unobserved
        else None,
        labeled_violations_without_release_observation=(
            violations_without_release_observation if releases_unobserved else None
        ),
        automatic_release_risk=Ratio.of(erroneous_releases, labeled_auto_clears),
        automatic_release_coverage=Ratio.of(all_auto_clears, releases_observed),
        automatic_release_observation_coverage=(
            Ratio.of(releases_observed, len(rows)) if releases_unobserved else None
        ),
        automatic_release_unobserved=releases_unobserved
        if releases_unobserved
        else None,
        automatic_release_unlabeled=all_auto_clears - labeled_auto_clears,
        uncertain_tasks=decisions[DecisionResult.UNCERTAIN],
        not_applicable_tasks=decisions[DecisionResult.NOT_APPLICABLE],
        not_evaluated_tasks=decisions[None],
        failed_executions=executions[ExecutionStatus.FAILED],
        partial_failed_executions=executions[ExecutionStatus.PARTIAL_FAILED],
        cancelled_executions=executions[ExecutionStatus.CANCELLED],
    )


def score(
    dataset: DatasetManifest,
    truth: TruthSnapshot,
    predictions: PredictionSnapshot,
    protocol: ScoringProtocol,
    *,
    expected_protocol: ScoringProtocolPin,
    source: TrialSource,
    expected_source: SnapshotPin,
    expected_truth: SnapshotPin,
) -> EvaluationReport:
    """Score the frozen holdout, without reading images or calling models or tools."""
    dataset = DatasetManifest.model_validate(dataset)
    truth = TruthSnapshot.model_validate(truth)
    predictions = PredictionSnapshot.model_validate(predictions)
    protocol = ScoringProtocol.model_validate(protocol)
    expected_protocol = ScoringProtocolPin.model_validate(expected_protocol)
    source = TrialSource.model_validate(source)
    expected_source = SnapshotPin.model_validate(expected_source)
    expected_truth = SnapshotPin.model_validate(expected_truth)
    if source.pin() != expected_source or truth.pin() != expected_truth:
        raise ValueError(
            "assessment requires exact independently approved source and truth pins"
        )
    samples = _check_inputs(dataset, truth, predictions, protocol, expected_protocol)
    by_source = source.reconcile(predictions)
    by_prediction = {row.sample_id: row for row in predictions.entries}
    by_truth = {row.sample_id: row for row in truth.entries}
    catalogue = _Catalogue(protocol)
    threshold = Fraction(protocol.iou_threshold_basis_points, 10_000)
    rows = tuple(
        _sample_score(
            sample,
            by_truth[sample_id],
            by_prediction[sample_id],
            by_source[sample_id],
            catalogue,
            threshold,
        )
        for sample_id, sample in sorted(samples.items())
    )
    dataset_pin, prediction_pin, truth_pin = (
        dataset.pin(),
        predictions.pin(),
        truth.pin(),
    )
    return EvaluationReport(
        schema_version="1.0.0",
        record_id=_report_id(
            dataset_pin, truth_pin, prediction_pin, expected_protocol, expected_source
        ),
        version="1.0.0",
        scope=dataset.scope,
        split=truth.split,
        dataset=dataset_pin,
        truth=truth_pin,
        predictions=prediction_pin,
        scoring_protocol=expected_protocol,
        protocol=protocol,
        trial_source=expected_source,
        cohort=predictions.cohort,
        samples=rows,
        summary=_summarize(rows, protocol),
    )
