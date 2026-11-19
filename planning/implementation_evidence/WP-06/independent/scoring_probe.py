"""Build a prediction export from durable outputs and compare it with fixture truth."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from cases import CATEGORY
from harness import ACTOR
from lifecycle_probe import lifecycle
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.domain.definition_support import ReviewReportRef
from jimu_runtime.domain.definitions.capabilities import (
    ProfileDefinition,
    QuestionDefinition,
)
from jimu_runtime.domain.enums import ArtifactKind, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.models import FactSchemaDefinition
from jimu_runtime.domain.fact_selectors import FactSchemaContentRef
from jimu_runtime.domain.rules.definitions import RuleSetDefinition
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.decisions import EvaluatedRuleOutcome
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.replay import ExecutionRef, ReplayExecutionSource
from jimu_runtime.evaluation.contracts import (
    DatasetManifest,
    DatasetSample,
    FactBinding,
    QuestionBinding,
    SampleLabels,
    ScoringProtocol,
    canonical_digest,
)
from jimu_runtime.evaluation.history import (
    HistoricalEvaluationExporter,
    HistoricalTrialExport,
    HistoryExportRequest,
    HistorySelection,
)
from jimu_runtime.evaluation.scoring import EvaluationReport, score
from jimu_runtime.evaluation.truth import (
    Adjudication,
    ReviewHistory,
    ReviewVote,
    TruthSnapshot,
)
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.mock_support import VerifiedExecutionScope
from jimu_runtime.workflow.pins import resolve_execution_pins
from jimu_runtime.workflow.repository import get_execution, read_finalization
from jimu_runtime.workflow.worker_records import (
    NODE_MESSAGE,
    GroundingMessage,
    ObservationMessage,
    ToolMessage,
)

VERSION = {"schema_version": "1.0.0", "version": "1.0.0"}
SAMPLE = Path(__file__).with_name("truth.json")


def role(identifier: str) -> ActorRef:
    return ActorRef(
        actor_id=identifier, actor_kind="human", identity_registry_id="fixture.humans"
    )


def labels(dataset, catalogue, profile, questions, geometry, rule, independent):
    sampler = dataset.samples[0].sample_id
    body = SampleLabels.model_validate(
        {
            "present_profiles": (profile,),
            "objects": (
                {
                    "label_id": "gold.object",
                    "profile_ref": profile,
                    "box": {"xyxy": independent["bounding_box"]},
                    "questions": tuple(
                        {
                            "question_ref": slot.question_ref,
                            "value": {
                                "kind": "enum",
                                "value": independent[slot.question_ref.artifact_id],
                            },
                        }
                        for slot in questions
                    ),
                    "geometry": (
                        {
                            "fact_schema_ref": geometry.ref,
                            "value": {
                                "kind": "enum",
                                "value": independent["wheel_boundary_relation"],
                            },
                        },
                    ),
                    "rules": (
                        {"path": rule, "matched": independent["task_violation"]},
                    ),
                },
            ),
            "task_violation": independent["task_violation"],
            "complete_grounding_profiles": (profile,),
            "violation_categories": (CATEGORY,),
        }
    )
    review = ReviewHistory(
        **VERSION,
        record_id="independent.review",
        dataset=dataset.pin(),
        scope=dataset.scope,
        split="holdout",
        sample_id=sampler,
        base_truth=None,
        catalogue_digest=canonical_digest(catalogue),
        previous=None,
        votes=(),
        adjudication=None,
    )
    when = datetime(2026, 1, 1, tzinfo=UTC)
    vote = ReviewVote(
        schema_version="1.0.0",
        vote_id="gold.vote",
        subject=review.subject,
        actor=role("synthetic-annotator"),
        recorded_at=when,
        rationale="Synthetic pixel annotation fixed before observing any prediction.",
        labels=body,
    )
    written = review.append_vote(vote, version="2.0.0")
    adjudicated = written.adjudicate(
        Adjudication(
            schema_version="1.0.0",
            adjudication_id="gold.adjudication",
            subject=written.subject,
            review_revision=written.pin(),
            actor=role("synthetic-adjudicator"),
            recorded_at=when,
            rationale="Second fixture identity accepts the independent annotation.",
            selected_vote_id=vote.vote_id,
            selected_vote_digest=canonical_digest(vote),
        ),
        version="3.0.0",
    )
    assert not review.votes and written.adjudication is None
    report_ref = ReviewReportRef(
        report_id=adjudicated.record_id,
        version=adjudicated.version,
        sha256=adjudicated.pin().digest,
        review_registry_id="fixture.humans",
    )
    return TruthSnapshot(
        **VERSION,
        record_id="independent.truth",
        dataset=dataset.pin(),
        scope=dataset.scope,
        split="holdout",
        prior=None,
        catalogue_digest=canonical_digest(catalogue),
        entries=(adjudicated.reviewed_entry(report_ref),),
    )


async def assert_scoring(host, eid: str, output: Path):
    scope = host.scope
    independent = json.loads(SAMPLE.read_bytes())
    async with host.db.connect() as con:
        locked = await get_execution(con, scope, eid)
        final = await read_finalization(con, scope, eid, ExecutionFinalization)
        raw = (
            await con.execute(
                text("""
            SELECT output_key, output_kind, output_digest, record::text AS body
            FROM jimu_workflow.produced_outputs WHERE execution_id=:eid
            ORDER BY output_key
        """),
                {"eid": eid},
            )
        ).all()
        receipts = (
            (
                await con.execute(
                    text("""
            SELECT record->'invocation' AS invocation
            FROM jimu_workflow.dispatch_results WHERE execution_id=:eid
              AND NOT late AND record->>'invocation' IS NOT NULL
        """),
                    {"eid": eid},
                )
            )
            .scalars()
            .all()
        )
    assert locked is not None and final is not None
    original = await host.media.verified_task_original(scope, locked.task)
    assert locked.task.media_ref.sha256 == independent["original_sha256"]
    assert sha256(original.content).hexdigest() == independent["original_sha256"]
    resolved = resolve_execution_pins(
        host.manager(),
        host.ledger,
        locked.task,
        scope=scope,
        host=host.snapshot,
        validation_at=datetime.now(UTC),
        limits=scene.LIMITS,
    )
    opened = VerifiedExecutionScope(
        locked.task,
        eid,
        resolved.pins,
        original.asset,
        resolved.files,
        scene.LIMITS.max_file_bytes,
        scene.LIMITS.max_json_depth,
    )
    fact_entries = {
        item.ref.artifact_id: item
        for item in opened.catalogue.of_kind(ArtifactKind.FACT_SCHEMA)
    }

    def fact_binding(key: str) -> FactBinding:
        item = fact_entries[key]
        ref = FactSchemaContentRef.model_validate(item.ref.model_dump())
        raw = item.canonical_bytes
        return FactBinding(
            ref=ref,
            definition=FactSchemaDefinition.model_validate_json(raw),
            canonical_source=raw.decode(),
        )

    assert all(
        sha256(row.body.encode()).hexdigest() == row.output_digest for row in raw
    )
    messages = [
        NODE_MESSAGE.validate_json(row.body)
        for row in raw
        if row.output_kind == "worker_message"
    ]
    (object_record,) = [
        obj
        for msg in messages
        if isinstance(msg, GroundingMessage)
        for obj in msg.objects
    ]
    (observation,) = [msg for msg in messages if isinstance(msg, ObservationMessage)]
    assert observation.authority is not None
    (tool_message,) = [msg for msg in messages if isinstance(msg, ToolMessage)]
    (decision,) = final.decisions
    (evaluation,) = decision.rule_evaluations
    assert isinstance(evaluation.result, EvaluatedRuleOutcome)
    profile = object_record.profile_ref
    configured_profile = opened.resolve(profile, ProfileDefinition)
    assert {fact.source.question_ref for fact in observation.facts} == set(
        configured_profile.question_refs
    )
    configured_questions = tuple(
        (ref, opened.resolve(ref, QuestionDefinition))
        for ref in configured_profile.question_refs
    )
    question_bindings = tuple(
        QuestionBinding(
            question_ref=ref,
            fact=fact_binding(question.primary_fact_schema_ref.artifact_id),
        )
        for ref, question in configured_questions
    )
    for binding, (_, question) in zip(
        question_bindings, configured_questions, strict=True
    ):
        assert binding.fact.ref == question.primary_fact_schema_ref
    assert evaluation.rule.rule_set_ref in configured_profile.rule_set_refs
    configured_set = opened.resolve(evaluation.rule.rule_set_ref, RuleSetDefinition)
    assert evaluation.rule.rule_ref in configured_set.rule_refs
    geometry = fact_binding("wheel_boundary_relation")
    dataset = DatasetManifest(
        **VERSION,
        record_id="independent.dataset",
        scope=scope,
        samples=(
            DatasetSample(
                sample_id=independent["sample_id"],
                image_sha256=independent["original_sha256"],
                image_dimensions=original.asset.dimensions,
                dedup_group="synthetic.outside",
                split="holdout",
            ),
        ),
    )
    request = HistoryExportRequest(
        scope=scope,
        actor=ACTOR,
        prediction_id="independent.predictions",
        source_id="independent.source",
        version="1.0.0",
        selections=(
            HistorySelection(
                sample_id=independent["sample_id"],
                source=ReplayExecutionSource(
                    execution_ref=ExecutionRef(
                        record_kind=RuntimeRecordKind.EXECUTION, record_id=eid
                    ),
                    task_id=locked.task.task_id,
                    pins=locked.execution.pins,
                ),
            ),
        ),
    )
    exporter = HistoricalEvaluationExporter(
        host.db, host.manager(), host.ledger, host.media, lifecycle(host), scene.LIMITS
    )
    export = await exporter.export(dataset, request)
    assert export == await exporter.export(dataset, request)
    assert HistoricalTrialExport.model_validate_json(export.stable_json()) == export
    catalogue, predictions, source = export.catalogue, export.predictions, export.source
    assert catalogue.bundle_digest == locked.task.bundle_digest
    (catalogue_profile,) = catalogue.profiles
    assert catalogue_profile.profile_ref == profile
    assert {
        row.question_ref: (row.fact.ref, row.fact.definition)
        for row in catalogue_profile.questions
    } == {
        row.question_ref: (row.fact.ref, row.fact.definition)
        for row in question_bindings
    }
    actual_geometry = {row.ref: row.definition for row in catalogue_profile.geometry}
    assert actual_geometry[geometry.ref] == geometry.definition
    assert {binding.path: binding.category for binding in catalogue_profile.rules} == {
        evaluation.rule: CATEGORY
    }
    truth = labels(
        dataset,
        catalogue,
        profile,
        question_bindings,
        geometry,
        evaluation.rule,
        independent,
    )
    protocol = ScoringProtocol(
        **VERSION,
        protocol_id="independent.parking",
        catalogue=catalogue,
        iou_threshold_basis_points=5000,
        matching="greedy_iou_then_truth_id_then_prediction_id",
    )
    (prediction,) = predictions.entries
    assert prediction.task == locked.task and prediction.execution == locked.execution
    assert prediction.decision_records == final.decisions
    assert prediction.discovery == final.coverage.discovery_outcome
    assert prediction.decision == final.coverage.summary_result
    invocations = tuple(
        ModelInvocation.model_validate_json(json.dumps(row)) for row in receipts
    )
    assert len(invocations) == 4 and len(tool_message.executions) == 3
    assert {canonical_digest(row) for row in prediction.model_invocations} == {
        canonical_digest(row) for row in invocations
    }
    assert {canonical_digest(row) for row in prediction.tool_executions} == {
        canonical_digest(row) for row in tool_message.executions
    }
    (predicted_profile,) = prediction.profiles
    (predicted,) = predicted_profile.objects
    assert (
        predicted.box == object_record.bbox
        and predicted.object_id == object_record.object_id
    )
    assert {
        item.question_ref: (item.status, item.value) for item in predicted.questions
    } == {
        fact.source.question_ref: (fact.status, fact.value)
        for fact in observation.facts
    }
    assert {
        item.fact_schema_ref: (item.status, item.value) for item in predicted.geometry
    } == {
        fact.fact_schema_ref: (fact.status, fact.value) for fact in tool_message.facts
    }
    (predicted_rule,) = predicted.rules
    assert (predicted_rule.path, predicted_rule.predicate) == (
        evaluation.rule,
        evaluation.result.predicate.truth,
    )
    (audit,) = export.history
    assert audit.finalization_digest == canonical_digest(final)
    assert len(audit.dispatches) == 7
    expected_evidence = {}
    for row in raw:
        if row.output_kind == "evidence":
            entry = EvidenceAsset.model_validate_json(row.body)
            digest = canonical_digest(entry)
            assert row.output_key == f"evidence.{entry.evidence_id}"
            assert expected_evidence.setdefault(row.output_key, digest) == digest
    actual_evidence = {
        f"evidence.{item.ref.record_id}": item.digest for item in audit.evidence
    }
    assert actual_evidence == expected_evidence, (actual_evidence, expected_evidence)
    assert (
        len(
            tuple(
                rsp
                for dispatch in audit.dispatches
                for rsp in dispatch.retained_responses
            )
        )
        == 8
    )
    kw = dict(
        expected_protocol=protocol.pin(),
        source=source,
        expected_source=source.pin(),
        expected_truth=truth.pin(),
    )
    report = score(dataset, truth, predictions, protocol, **kw)
    assert report == score(dataset, truth, predictions, protocol, **kw)
    frozen = report.stable_json()
    assert EvaluationReport.model_validate_json(frozen) == report
    summary = report.summary
    for metric, expected in (
        (summary.profile_recall, (1, 1)),
        (summary.grounding_recall, (1, 1)),
        (summary.tool_success, (3, 3)),
        (summary.task_violation_recall, (1, 1)),
        (summary.automatic_release_risk, (0, 0)),
    ):
        assert (metric.numerator, metric.denominator) == expected
    assert (
        summary.matched_mean_iou == 1 and summary.automatic_release_risk.value is None
    )
    assert len(summary.questions) == 3 and len(summary.rules) == 1
    geometry_metric = next(
        row for row in summary.geometry if row.fact_schema_ref == geometry.ref
    )
    for row in (*summary.questions, geometry_metric, *summary.rules):
        comparison = row.comparison
        assert (
            comparison.correct
            == comparison.accuracy.denominator
            == comparison.coverage.denominator
            == 1
        )
    assert summary.task_labeled_samples == 1
    for label, record in (
        ("score", report),
        ("truth", truth),
        ("dataset", dataset),
        ("predictions", predictions),
        ("protocol", protocol),
        ("source", source),
        ("historical-export", export),
    ):
        (output / f"independent-quality.{label}.json").write_text(
            record.stable_json() + "\n"
        )
    print(
        json.dumps(
            {
                "check": "offline_evaluation",
                "known_truth": 1,
                "observed_model_invocations": len(invocations),
                "rule_labels": len(summary.rules),
                "score_digest": report.pin().digest,
                "truth_digest": truth.pin().digest,
                "source_digest": source.pin().digest,
                "matched_mean_iou": summary.matched_mean_iou,
                "erroneous_release": None,
                "labeled_violations_without_release_observation": 1,
                "automatic_release_risk": [0, 0],
                "risk_value": None,
            }
        ),
        flush=True,
    )

    return exporter, dataset, request
