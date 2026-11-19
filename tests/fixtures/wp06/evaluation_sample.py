"""Synthetic holdout for the offline evaluator. Run this example as a module."""

import json
from datetime import UTC, datetime
from pathlib import Path

from fixtures.wp01b.r1.support import registration

from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.capabilities import RuleSetContentRef
from jimu_runtime.domain.enums import (
    ArtifactKind,
    DecisionResult,
    ExecutionStatus,
    FactStatus,
)
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef, QuestionContentRef
from jimu_runtime.domain.rules.definitions import RuleContentRef
from jimu_runtime.domain.runtime.applicability import RuleTruth
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.decisions import DecisionRecord, RulePath
from jimu_runtime.domain.runtime.executions import (
    ConfiguredModelPin,
    ConfiguredToolPin,
    ContextSnapshotPin,
    ExecutionPins,
    ExecutionRecord,
)
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    ModelInvocationRef,
    ProfileContentRef,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import BBoxXYXY, ImageDimensions, MediaRef
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome, GroundingOutcome
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.domain.values import BooleanValue, EnumValue
from jimu_runtime.evaluation import (
    DatasetManifest,
    PredictionSnapshot,
    ScoringProtocol,
    TruthSnapshot,
)
from jimu_runtime.evaluation import (
    score as production_score,
)
from jimu_runtime.evaluation.contracts import (
    DatasetSample,
    EvaluationCatalogue,
    ExecutionCohort,
    ExecutionEvidence,
    FactBinding,
    GeometryLabel,
    GeometryPrediction,
    LabeledObject,
    PredictedObject,
    ProfileBinding,
    ProfilePrediction,
    QuestionBinding,
    QuestionLabel,
    QuestionPrediction,
    RuleBinding,
    RuleLabel,
    RulePrediction,
    SampleLabels,
    SamplePrediction,
    SampleSource,
    TrialSource,
    canonical_digest,
)
from jimu_runtime.evaluation.truth import (
    Adjudication,
    ReviewHistory,
    ReviewVote,
    TruthEntry,
)

FIXTURES = Path(__file__).resolve().parents[1]
AT = datetime(2026, 11, 17, 2, 30, 1, tzinfo=UTC)
SCHEMA = "1.0.0"
D = "a" * 64
BUNDLE = "b" * 64
PROFILE = ProfileContentRef(
    artifact_kind=ArtifactKind.PROFILE,
    artifact_id="synthetic.profile",
    version=SCHEMA,
    digest=D,
)
QUESTION = QuestionContentRef(
    artifact_kind=ArtifactKind.QUESTION,
    artifact_id="vehicle-parked",
    version=SCHEMA,
    digest=D,
)
RULE = RulePath(
    rule_set_ref=RuleSetContentRef(
        artifact_kind=ArtifactKind.RULE_SET,
        artifact_id="parking-rules",
        version=SCHEMA,
        digest=D,
    ),
    rule_ref=RuleContentRef(
        artifact_kind=ArtifactKind.RULE,
        artifact_id="parking-violation",
        version=SCHEMA,
        digest=D,
    ),
)
PARKED = BooleanValue(kind="bool", value=True)
INSIDE = EnumValue(kind="enum", value="inside")
OUTSIDE = EnumValue(kind="enum", value="outside")
BOX = BBoxXYXY(xyxy=(0.0, 0.0, 10.0, 10.0))


def fact_binding(name, types):
    _, definition, _ = registration(types, schema_id=name)
    return FactBinding(
        ref=FactSchemaContentRef(
            artifact_kind=ArtifactKind.FACT_SCHEMA,
            artifact_id=name,
            version=definition.version,
            digest=canonical_digest(definition),
        ),
        definition=definition,
    )


PARKED_FACT = fact_binding("vehicle-parked", {"kind": "bool"})
GEOMETRY_FACT = fact_binding(
    "wheel-boundary-relation", {"kind": "enum", "members": ["inside", "outside"]}
)
GEOMETRY = GEOMETRY_FACT.ref
OTHER_RULE = RULE.model_copy(
    update={"rule_ref": RULE.rule_ref.model_copy(update={"artifact_id": "other-rule"})}
)
CATALOGUE = EvaluationCatalogue(
    schema_version=SCHEMA,
    bundle_digest=BUNDLE,
    profiles=(
        ProfileBinding(
            profile_ref=PROFILE,
            questions=(QuestionBinding(question_ref=QUESTION, fact=PARKED_FACT),),
            geometry=(GEOMETRY_FACT,),
            rules=(
                RuleBinding(path=RULE, category="parking"),
                RuleBinding(path=OTHER_RULE, category="wrong-location"),
            ),
        ),
    ),
)


def actor(actor_id, *, kind="human"):
    return ActorRef(
        actor_kind=kind, actor_id=actor_id, identity_registry_id="synthetic-registry"
    )


def make_vote(history, actor_id, labels):
    return ReviewVote(
        schema_version=SCHEMA,
        vote_id=f"vote-{actor_id}",
        subject=history.subject,
        actor=actor(actor_id),
        recorded_at=AT,
        rationale="Synthetic independent label",
        labels=labels,
    )


def report_ref(review):
    return ReviewReportRef(
        report_id=review.record_id,
        version=review.version,
        sha256=review.pin().digest,
        review_registry_id="synthetic-registry",
    )


def new_review(dataset, sample_id, base_truth=None, *, catalogue=CATALOGUE):
    return ReviewHistory(
        schema_version=SCHEMA,
        record_id=f"review-{sample_id}",
        version="1.0.0",
        dataset=dataset.pin(),
        scope=dataset.scope,
        split="holdout",
        sample_id=sample_id,
        base_truth=base_truth,
        catalogue_digest=canonical_digest(catalogue),
        previous=None,
        votes=(),
        adjudication=None,
    )


def adjudication(history, vote, *, adjudicator="reviewer"):
    return Adjudication(
        schema_version=SCHEMA,
        adjudication_id="adjudication",
        actor=actor(adjudicator),
        recorded_at=AT,
        rationale="Synthetic adjudication of the selected vote",
        subject=history.subject,
        review_revision=history.pin(),
        selected_vote_id=vote.vote_id if vote else None,
        selected_vote_digest=canonical_digest(vote) if vote else None,
    )


def reviewed(dataset, sample_id, labels, *, conflict=False, catalogue=CATALOGUE):
    history = new_review(dataset, sample_id, catalogue=catalogue)
    vote = make_vote(history, "annotator", labels)
    history = history.append_vote(vote, version="2.0.0")
    if labels is not None:
        if conflict:
            history = history.append_vote(
                make_vote(
                    history,
                    "another-annotator",
                    labels.model_copy(update={"task_violation": False}),
                ),
                version="3.0.0",
            )
        else:
            history = history.adjudicate(adjudication(history, vote), version="3.0.0")
    return history.reviewed_entry(report_ref(history))


def synthetic_inputs():
    template = AcceptedTask.model_validate_json(
        (FIXTURES / "wp01c/intake/accepted-task-valid.json").read_bytes()
    )
    source = json.loads((FIXTURES / "wp01c/invocations/contracts.json").read_text())
    invocation = ModelInvocation.model_validate_json(
        json.dumps(source["models"]["unavailable"])
    )
    tool = ToolExecution.model_validate_json(json.dumps(source["tools"]["step"]))
    configured = invocation.configured_provider
    provider = source["policies"]["provider"]["artifact"]["policy"]
    pins = ExecutionPins.model_validate(
        {
            "release_id": template.release_id,
            "bundle_digest": BUNDLE,
            "runtime_version": "1.0.0",
            "workflow_version": "2.0.0",
            "model_pins": (
                ConfiguredModelPin.model_validate(
                    {
                        "binding_ref": configured.binding_ref,
                        "provider_policy_ref": configured.provider_policy_ref,
                        "fallback_policy_ref": invocation.route_trace.fallback_policy_ref,
                        "primary_deployment_id": configured.primary_deployment_id,
                        "deployments": provider["deployments"],
                    }
                ),
            ),
            "tool_pins": (
                ConfiguredToolPin(
                    tool_ref=tool.tool_ref, implementation_version="1.0.0"
                ),
            ),
            "input_digest": D,
        }
    )
    dataset = DatasetManifest(
        schema_version=SCHEMA,
        record_id="dataset-synthetic",
        version="1.0.0",
        scope=template.idempotency_scope,
        samples=tuple(
            DatasetSample(
                sample_id=key,
                image_sha256=f"{idx:064x}",
                image_dimensions=ImageDimensions(width=100, height=100),
                dedup_group=f"capture-{idx}",
                split="calibration" if key == "cal" else "holdout",
            )
            for idx, key in enumerate(("a", "b", "c", "d", "e", "f", "g", "cal"), 1)
        ),
    )
    label = LabeledObject(
        label_id="label-a",
        profile_ref=PROFILE,
        box=BOX,
        questions=(QuestionLabel(question_ref=QUESTION, value=PARKED),),
        geometry=(GeometryLabel(fact_schema_ref=GEOMETRY, value=OUTSIDE),),
        rules=(RuleLabel(path=RULE, matched=True),),
    )
    with_object = SampleLabels(
        present_profiles=(PROFILE,),
        objects=(label,),
        task_violation=True,
        complete_grounding_profiles=(PROFILE,),
        violation_categories=("parking",),
    )
    without_object = SampleLabels(
        present_profiles=(PROFILE,), objects=(), task_violation=True
    )
    truth = TruthSnapshot(
        schema_version=SCHEMA,
        record_id="truth-synthetic",
        version="1.0.0",
        dataset=dataset.pin(),
        scope=dataset.scope,
        split="holdout",
        prior=None,
        catalogue_digest=canonical_digest(CATALOGUE),
        entries=(
            reviewed(dataset, "a", with_object),
            reviewed(dataset, "b", without_object),
            TruthEntry(sample_id="c", state="unlabeled"),
            reviewed(dataset, "d", without_object, conflict=True),
            reviewed(dataset, "e", without_object),
            reviewed(dataset, "f", None),
            reviewed(
                dataset,
                "g",
                without_object.model_copy(update={"task_violation": False}),
            ),
        ),
    )
    detected = PredictedObject(
        object_id="obj-1",
        box=BBoxXYXY(xyxy=(0.0, 0.0, 5.0, 10.0)),
        questions=(
            QuestionPrediction(
                question_ref=QUESTION, status=FactStatus.KNOWN, value=PARKED
            ),
        ),
        geometry=(
            GeometryPrediction(
                fact_schema_ref=GEOMETRY, status=FactStatus.KNOWN, value=INSIDE
            ),
        ),
        rules=(
            RulePrediction(path=RULE, status="evaluated", predicate=RuleTruth.TRUE),
        ),
    )
    config = {
        "a": (
            ExecutionStatus.COMPLETED,
            DiscoveryOutcome.HITS,
            DecisionResult.VIOLATION,
            False,
        ),
        "b": (
            ExecutionStatus.COMPLETED,
            DiscoveryOutcome.HITS,
            DecisionResult.NO_VIOLATION,
            True,
        ),
        "c": (
            ExecutionStatus.COMPLETED,
            DiscoveryOutcome.HITS,
            DecisionResult.NO_VIOLATION,
            True,
        ),
        "d": (
            ExecutionStatus.COMPLETED,
            DiscoveryOutcome.NO_HITS,
            DecisionResult.UNCERTAIN,
            False,
        ),
        "e": (
            ExecutionStatus.COMPLETED,
            DiscoveryOutcome.NO_HITS,
            DecisionResult.UNCERTAIN,
            False,
        ),
        "f": (ExecutionStatus.FAILED, DiscoveryOutcome.FAILED, None, False),
        "g": (
            ExecutionStatus.COMPLETED,
            DiscoveryOutcome.HITS,
            DecisionResult.NO_VIOLATION,
            True,
        ),
    }
    original_decision = DecisionRecord.model_validate_json(
        (FIXTURES / "wp01c/decisions/decision_record.json").read_bytes()
    )
    decision_record = original_decision.model_copy(
        update={
            "execution_id": "execution-a",
            "release_id": pins.release_id,
            "bundle_digest": BUNDLE,
            "profile_ref": PROFILE,
            "object_ref": original_decision.object_ref.model_copy(
                update={"record_id": "obj-1"}
            ),
            "rule_evaluations": tuple(
                row.model_copy(update={"rule": RULE})
                for row in original_decision.rule_evaluations
            ),
            "explanation": original_decision.explanation.model_copy(
                update={"evaluated_rules": (RULE,), "applicable_rules": (RULE,)}
            ),
            "conflict_resolution": original_decision.conflict_resolution.model_copy(
                update={"matched_rules": (RULE,), "matched_category_ids": ("parking",)}
            ),
        }
    )
    rows = []
    for sample in dataset.samples:
        if sample.split != "holdout":
            continue
        key = sample.sample_id
        status, discovery, decision, auto_cleared = config[key]
        execution_id = f"execution-{key}"
        task = template.model_copy(
            update={
                "task_id": f"task-{key}",
                "bundle_digest": BUNDLE,
                "media_ref": MediaRef(
                    media_id=f"media-{key}", sha256=sample.image_sha256
                ),
            }
        )
        execution = ExecutionRecord(
            schema_version=SCHEMA,
            execution_id=execution_id,
            task_id=task.task_id,
            pins=pins.model_copy(update={"input_digest": f"{101 + len(rows):064x}"}),
            context_snapshot=ContextSnapshotPin(
                snapshot_ref=decision_record.context_snapshot_ref, snapshot_digest=D
            )
            if key == "a"
            else None,
            trace=task.trace,
            status=status,
            created_at=AT,
            started_at=AT,
            ended_at=AT,
            model_invocation_refs=(
                ModelInvocationRef(
                    record_kind="model_invocation",
                    record_id=invocation.model_invocation_id,
                ),
            )
            if key == "a"
            else (),
        )
        objects = (
            (detected,) if key == "a" else (detected.model_copy(update={"rules": ()}),)
        )
        rows.append(
            SamplePrediction(
                sample_id=key,
                task=task,
                execution=execution,
                model_invocations=(
                    invocation.model_copy(update={"execution_id": execution_id}),
                )
                if key == "a"
                else (),
                tool_executions=(
                    tool.model_copy(update={"execution_id": execution_id}),
                )
                if key == "a"
                else (),
                discovery=discovery,
                profiles=(
                    ProfilePrediction(
                        profile_ref=PROFILE,
                        grounding=GroundingOutcome.OBJECTS,
                        objects=objects,
                    ),
                )
                if discovery is DiscoveryOutcome.HITS
                else (),
                decision=decision,
                decision_records=(decision_record,) if key == "a" else (),
                auto_cleared=auto_cleared,
            )
        )
    predictions = PredictionSnapshot(
        schema_version=SCHEMA,
        record_id="predictions-synthetic",
        version="1.0.0",
        dataset=dataset.pin(),
        scope=dataset.scope,
        split="holdout",
        cohort=ExecutionCohort.from_execution(pins),
        producer=actor("synthetic-worker", kind="service"),
        entries=tuple(rows),
    )
    protocol = ScoringProtocol(
        schema_version=SCHEMA,
        protocol_id="parking-synthetic",
        version=SCHEMA,
        catalogue=CATALOGUE,
        iou_threshold_basis_points=5000,
        matching="greedy_iou_then_truth_id_then_prediction_id",
    )
    return dataset, truth, predictions, protocol


def trial_source(predictions):
    """Mock the independently authenticated runtime exporter for synthetic rows."""
    return TrialSource(
        schema_version=SCHEMA,
        record_id="source-synthetic",
        version=SCHEMA,
        scope=predictions.scope,
        dataset=predictions.dataset,
        predictions=predictions.pin(),
        rows=tuple(
            SampleSource(
                sample_id=row.sample_id,
                task_digest=canonical_digest(row.task),
                execution_digest=canonical_digest(row.execution),
                execution_input_digest=row.execution.pins.input_digest,
                model_digests=tuple(
                    canonical_digest(item) for item in row.model_invocations
                ),
                tool_digests=tuple(
                    canonical_digest(item) for item in row.tool_executions
                ),
                decision_digests=tuple(
                    canonical_digest(item) for item in row.decision_records
                ),
                evidence=(
                    ExecutionEvidence(
                        ref=EvidenceRef(
                            record_kind="evidence",
                            record_id=f"evidence-{row.sample_id}",
                        ),
                        digest=row.execution.pins.input_digest,
                    ),
                ),
            )
            for row in predictions.entries
        ),
    )


def score(dataset, truth, predictions, protocol, *, expected_protocol):
    """Exercise actual scoring after building the synthetic host authority state."""
    source = trial_source(predictions)
    return production_score(
        dataset,
        truth,
        predictions,
        protocol,
        expected_protocol=expected_protocol,
        source=source,
        expected_source=source.pin(),
        expected_truth=truth.pin(),
    )


if __name__ == "__main__":
    dataset, truth, predictions, protocol = synthetic_inputs()
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    print(report.summary.model_dump_json(indent=2))
