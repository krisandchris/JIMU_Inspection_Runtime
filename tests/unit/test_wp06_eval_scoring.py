"""Finite cases for stage scoring, deterministic matching and review revisions."""

import pytest
from fixtures.wp06.evaluation_sample import (
    BOX,
    QUESTION,
    RULE,
    actor,
    adjudication,
    make_vote,
    new_review,
    report_ref,
    reviewed,
    score,
    synthetic_inputs,
)

from jimu_runtime.domain.enums import FactStatus
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.media import BBoxXYXY
from jimu_runtime.evaluation import (
    BadCaseAttribution,
    revise_attribution,
    revise_truth,
)
from jimu_runtime.evaluation.contracts import (
    GeometryPrediction,
    QuestionPrediction,
    RulePrediction,
    canonical_digest,
)
from jimu_runtime.evaluation.review import AttributionEvidence
from jimu_runtime.evaluation.scoring import Ratio
from jimu_runtime.evaluation.truth import TruthEntry


def test_complete_scoring_and_reproduction():
    dataset, truth, predictions, protocol = synthetic_inputs()
    sources = tuple(model.pin() for model in (dataset, truth, predictions, protocol))
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert report == score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert sources == tuple(
        model.pin() for model in (dataset, truth, predictions, protocol)
    )
    assert (
        report.dataset,
        report.truth,
        report.predictions,
        report.scoring_protocol,
    ) == sources
    assert report.samples[0].execution_digest == canonical_digest(
        predictions.entries[0].execution
    )
    model = report.samples[0].models[0]
    assert model.actual_provider.version_resolution == "unavailable"
    assert model.digest == canonical_digest(predictions.entries[0].model_invocations[0])
    assert report.samples[0].tools[0].actual_version == "1.0.0"
    summary = report.summary
    assert (
        summary.labeled_samples,
        summary.unlabeled_samples,
        summary.conflicted_samples,
        summary.abstained_samples,
    ) == (4, 1, 1, 1)
    assert summary.profile_recall == Ratio.of(3, 4)
    assert summary.grounding_recall == Ratio.of(1, 1)
    assert summary.matched_mean_iou == 0.5
    assert summary.questions[0].comparison.accuracy == Ratio.of(1, 1)
    assert summary.geometry[0].comparison.accuracy == Ratio.of(0, 1)
    assert next(
        row.comparison.accuracy for row in summary.rules if row.path == RULE
    ) == Ratio.of(1, 1)
    assert summary.tool_success == Ratio.of(1, 1)
    assert summary.discovery_no_hits == Ratio.of(2, 7)
    assert summary.no_hits_confirmed_target_miss_lower_bound == Ratio.of(1, 2)
    assert summary.no_hits_labeled_target_miss == Ratio.of(1, 1)
    assert summary.no_hits_target_truth_unavailable == 1
    assert summary.task_violation_precision == Ratio.of(1, 1)
    assert summary.task_violation_recall == Ratio.of(1, 3)
    assert summary.erroneous_release_rate == Ratio.of(1, 3)
    assert summary.automatic_release_risk == Ratio.of(1, 2)
    assert summary.automatic_release_coverage == Ratio.of(3, 7)
    assert summary.automatic_release_unlabeled == 1
    assert (
        summary.uncertain_tasks,
        summary.not_evaluated_tasks,
        summary.failed_executions,
    ) == (2, 1, 1)


def test_exact_iou_threshold_and_undefined_accuracy():
    dataset, truth, predictions, protocol = synthetic_inputs()
    stricter = protocol.model_copy(
        update={"version": "2.0.0", "iou_threshold_basis_points": 5001}
    )
    report = score(
        dataset, truth, predictions, stricter, expected_protocol=stricter.pin()
    )
    assert report.samples[0].objects[0].match is None
    assert report.summary.grounding_recall == Ratio.of(0, 1)
    assert report.summary.matched_mean_iou is None
    comparison = report.summary.questions[0].comparison
    assert comparison.unmatched == 1
    assert comparison.accuracy == Ratio.of(0, 0)
    assert comparison.coverage == Ratio.of(0, 1)


def test_box_ties_and_nonknown_stage_results_are_independent():
    dataset, truth, predictions, protocol = synthetic_inputs()
    truth_a = truth.entries[0].labels
    labels = truth_a.model_copy(
        update={
            "objects": (
                truth_a.objects[0].model_copy(update={"label_id": "truth-z"}),
                truth_a.objects[0].model_copy(update={"label_id": "truth-a"}),
            )
        }
    )
    truth = truth.model_copy(
        update={"entries": (reviewed(dataset, "a", labels), *truth.entries[1:])}
    )
    pred = predictions.entries[0]
    profile = pred.profiles[0]
    obj = profile.objects[0]
    first = obj.model_copy(
        update={
            "box": BOX,
            "questions": (
                QuestionPrediction(question_ref=QUESTION, status=FactStatus.UNKNOWN),
            ),
            "geometry": (
                GeometryPrediction(
                    fact_schema_ref=protocol.geometry_refs[0],
                    status=FactStatus.NOT_EVALUATED,
                ),
            ),
            "rules": (RulePrediction(path=RULE, status="not_evaluated"),),
        }
    )
    second = obj.model_copy(update={"box": BOX, "object_id": "obj-2"})
    for order in ((second, first), (first, second)):
        positive = pred.decision_records[0]
        decision = positive.model_copy(
            update={
                "object_ref": positive.object_ref.model_copy(
                    update={"record_id": "obj-2"}
                )
            }
        )
        sample = pred.model_copy(
            update={
                "profiles": (profile.model_copy(update={"objects": order}),),
                "decision_records": (decision,),
            }
        )
        candidate = predictions.model_copy(
            update={"entries": (*predictions.entries[1:], sample)}
        )
        report = score(
            dataset, truth, candidate, protocol, expected_protocol=protocol.pin()
        )
        assert [
            (check.label_id, check.match.object_id)
            for check in report.samples[0].objects
        ] == [("truth-a", "obj-1"), ("truth-z", "obj-2")]
        summary = report.summary
        assert (
            summary.questions[0].comparison.unknown
            == next(
                row.comparison.not_evaluated
                for row in summary.rules
                if row.path == RULE
            )
            == 1
        )
        assert summary.geometry[0].comparison.not_evaluated == 1
        assert summary.questions[0].comparison.coverage == Ratio.of(1, 2)


def test_very_small_valid_boxes_have_defined_iou():
    dataset, truth, predictions, protocol = synthetic_inputs()
    truth_a = truth.entries[0].labels
    tiny = BBoxXYXY(xyxy=(0.0, 0.0, 5e-324, 5e-324))
    labels = truth_a.model_copy(
        update={"objects": (truth_a.objects[0].model_copy(update={"box": tiny}),)}
    )
    truth = truth.model_copy(
        update={"entries": (reviewed(dataset, "a", labels), *truth.entries[1:])}
    )
    first = predictions.entries[0]
    profile = first.profiles[0]
    obj = profile.objects[0].model_copy(update={"box": tiny})
    entry = first.model_copy(
        update={"profiles": (profile.model_copy(update={"objects": (obj,)}),)}
    )
    predictions = predictions.model_copy(
        update={"entries": (entry, *predictions.entries[1:])}
    )
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert report.samples[0].objects[0].match.iou == 1.0


def test_review_remains_pending_until_independent_adjudication_then_revises_truth():
    dataset, truth, predictions, protocol = synthetic_inputs()
    original = truth.pin()
    start = new_review(dataset, "c", original)
    vote = make_vote(start, "review-voter", truth.entries[0].labels)
    history = start.append_vote(vote, version="2.0.0")
    assert history.reviewed_entry(report_ref(history)).state == "unlabeled"
    decided = history.adjudicate(adjudication(history, vote), version="3.0.0")
    updated = revise_truth(
        dataset, truth, decided, report_ref(decided), version="2.0.0"
    )
    assert history.adjudication is None and decided.previous == history.pin()
    assert truth.pin() == original and updated.prior == original
    assert (
        truth.entries[2].state == "unlabeled" and updated.entries[2].state == "labeled"
    )
    new_report = score(
        dataset, updated, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert new_report.predictions == predictions.pin()
    assert new_report.summary.erroneous_release_rate == Ratio.of(2, 4)


def test_bad_case_attribution_is_independent_and_append_only():
    dataset, truth, predictions, protocol = synthetic_inputs()
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    case = BadCaseAttribution(
        schema_version="1.0.0",
        record_id="case-a",
        version="1.0.0",
        report=report.pin(),
        scope=report.scope,
        sample_id="a",
        attributed_stage="geometry",
        rationale="Wheel boundary differs",
        actor=actor("investigator"),
        recorded_at=predictions.entries[0].execution.ended_at,
        evidence=(
            AttributionEvidence(
                ref=EvidenceRef(record_kind="evidence", record_id="evidence-a"),
                digest=report.samples[0].evidence[0].digest,
            ),
        ),
        supersedes=None,
    )
    case.validate_source(report)
    other_evidence = report.samples[1].evidence[0]
    wrong_sample = case.model_copy(
        update={
            "evidence": (
                AttributionEvidence(
                    ref=other_evidence.ref, digest=other_evidence.digest
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="belong to the scored sample"):
        wrong_sample.validate_source(report)
    revised = case.model_copy(
        update={
            "version": "2.0.0",
            "supersedes": case.pin(),
            "attributed_stage": "inconclusive",
        }
    )
    assert revise_attribution(report, case, revised) == revised
    assert case.attributed_stage == "geometry"
    assert case.report == revised.report == report.pin()


def test_binary_hit_does_not_reward_the_wrong_category_and_unmatched_boxes_are_retained():
    dataset, truth, predictions, protocol = synthetic_inputs()
    labels = truth.entries[0].labels.model_copy(
        update={"violation_categories": ("wrong-location",)}
    )
    truth = truth.model_copy(
        update={"entries": (reviewed(dataset, "a", labels), *truth.entries[1:])}
    )
    first = predictions.entries[0]
    profile = first.profiles[0]
    obj = profile.objects[0].model_copy(update={"object_id": "unmatched", "rules": ()})
    profile = profile.model_copy(update={"objects": (*profile.objects, obj)})
    first = first.model_copy(update={"profiles": (profile,)})
    predictions = predictions.model_copy(
        update={"entries": (first, *predictions.entries[1:])}
    )
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert report.summary.task_violation_precision == Ratio.of(1, 1)
    categories = {row.category: row for row in report.summary.categories}
    assert categories["wrong-location"].recall == Ratio.of(0, 1)
    assert categories["parking"].precision == Ratio.of(0, 1)
    assert report.samples[0].unmatched_predictions[0].object_id == "unmatched"
    assert report.summary.grounding_false_positives == 1
    assert report.summary.grounding_unmatched_without_full_truth == 3


def test_no_hits_without_truth_has_only_a_confirmed_miss_lower_bound():
    dataset, truth, predictions, protocol = synthetic_inputs()
    entries = tuple(
        TruthEntry(sample_id=row.sample_id, state="unlabeled")
        if row.sample_id == "e"
        else row
        for row in truth.entries
    )
    truth = truth.model_copy(update={"entries": entries})
    report = score(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert report.summary.no_hits_confirmed_target_miss_lower_bound == Ratio.of(0, 2)
    assert report.summary.no_hits_labeled_target_miss == Ratio.of(0, 0)
    assert report.summary.no_hits_target_truth_unavailable == 2
