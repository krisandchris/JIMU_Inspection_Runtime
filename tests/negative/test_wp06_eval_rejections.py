"""Reject split contamination, mismatched pins and unsupported truth or provenance."""

import pytest
from fixtures.wp06.evaluation_sample import (
    adjudication,
    make_vote,
    new_review,
    production_score,
    report_ref,
    reviewed,
    score,
    synthetic_inputs,
    trial_source,
)
from pydantic import ValidationError

from jimu_runtime.domain.enums import DecisionResult
from jimu_runtime.domain.runtime.perception import DiscoveryOutcome
from jimu_runtime.domain.values import EnumValue
from jimu_runtime.evaluation import (
    DatasetManifest,
    PredictionSnapshot,
    TruthSnapshot,
    revise_truth,
)
from jimu_runtime.evaluation.contracts import SamplePrediction, canonical_digest
from jimu_runtime.evaluation.truth import TruthEntry


@pytest.mark.parametrize("which", ["sample_id", "image_sha256", "dedup_group"])
def test_manifest_rejects_duplicate_or_leaked_samples(which):
    dataset, *_ = synthetic_inputs()
    cal = dataset.samples[-1].model_copy(
        update={which: getattr(dataset.samples[0], which)}
    )
    with pytest.raises(ValidationError, match="duplicate|crosses"):
        DatasetManifest.model_validate(
            dataset.model_copy(update={"samples": (*dataset.samples[:-1], cal)})
        )


@pytest.mark.parametrize(
    "which", ["dataset", "protocol", "truth_scope", "trial_scope", "split", "coverage"]
)
def test_score_rejects_mismatched_inputs(which):
    dataset, truth, predictions, protocol = synthetic_inputs()
    pinned = protocol.pin()
    if which == "dataset":
        dataset = dataset.model_copy(update={"version": "2.0.0"})
    elif which == "protocol":
        protocol = protocol.model_copy(update={"version": "2.0.0"})
    elif which == "truth_scope":
        truth = truth.model_copy(
            update={"scope": truth.scope.model_copy(update={"client_id": "elsewhere"})}
        )
    elif which == "trial_scope":
        predictions = predictions.model_copy(
            update={
                "scope": predictions.scope.model_copy(
                    update={"project_id": "elsewhere"}
                )
            }
        )
    elif which == "split":
        truth = truth.model_copy(update={"split": "calibration"})
    elif which == "coverage":
        predictions = predictions.model_copy(
            update={"entries": predictions.entries[:-1]}
        )
    with pytest.raises(ValueError, match="dataset|protocol|scope|split|holdout"):
        score(dataset, truth, predictions, protocol, expected_protocol=pinned)


def test_predictions_and_truth_are_exactly_once():
    _, truth, predictions, _ = synthetic_inputs()
    with pytest.raises(ValidationError, match="duplicate prediction"):
        PredictionSnapshot.model_validate(
            predictions.model_copy(
                update={"entries": (*predictions.entries, predictions.entries[-1])}
            )
        )
    with pytest.raises(ValidationError, match="duplicate truth"):
        TruthSnapshot.model_validate(
            truth.model_copy(update={"entries": (*truth.entries, truth.entries[-1])})
        )


@pytest.mark.parametrize("which", ["release", "model", "tool", "no_hits"])
def test_prediction_rejects_invalid_source(which):
    _, _, trial, _ = synthetic_inputs()
    pred = trial.entries[0]
    if which == "release":
        pred = pred.model_copy(
            update={"task": pred.task.model_copy(update={"release_id": "wrong"})}
        )
    elif which == "model":
        pred = pred.model_copy(update={"model_invocations": ()})
    elif which == "tool":
        bad_tool = pred.tool_executions[0].model_copy(
            update={"execution_id": "elsewhere"}
        )
        pred = pred.model_copy(update={"tool_executions": (bad_tool,)})
    elif which == "no_hits":
        pred = pred.model_copy(
            update={
                "profiles": (),
                "discovery": DiscoveryOutcome.NO_HITS,
                "decision": DecisionResult.NO_VIOLATION,
                "auto_cleared": True,
            }
        )
    with pytest.raises(
        ValidationError,
        match="release|ModelInvocation|ToolExecution|automatic clearance",
    ):
        SamplePrediction.model_validate(pred)


@pytest.mark.parametrize("which", ["image", "version", "bounds"])
def test_bound_prediction_content_is_checked_against_protocol_and_dataset(which):
    dataset, truth, predictions, protocol = synthetic_inputs()
    pred = predictions.entries[0]
    profile = pred.profiles[0]
    obj = profile.objects[0]
    if which == "image":
        media = pred.task.media_ref.model_copy(update={"sha256": "f" * 64})
        pred = pred.model_copy(
            update={"task": pred.task.model_copy(update={"media_ref": media})}
        )
    elif which == "version":
        profile = profile.model_copy(
            update={
                "profile_ref": profile.profile_ref.model_copy(
                    update={"version": "2.0.0"}
                )
            }
        )
        pred = pred.model_copy(update={"profiles": (profile,)})
    elif which == "bounds":
        obj = obj.model_copy(
            update={"box": obj.box.model_copy(update={"xyxy": (0.0, 0.0, 500.0, 10.0)})}
        )
        pred = pred.model_copy(
            update={"profiles": (profile.model_copy(update={"objects": (obj,)}),)}
        )
    predictions = predictions.model_copy(
        update={"entries": (pred, *predictions.entries[1:])}
    )
    with pytest.raises(ValueError, match="image|catalogue|bounds|Decision must belong"):
        score(dataset, truth, predictions, protocol, expected_protocol=protocol.pin())


def test_open_and_mismatched_reviews_cannot_manufacture_truth():
    dataset, truth, _, _ = synthetic_inputs()
    start = new_review(dataset, "c", truth.pin())
    vote = make_vote(start, "voter", truth.entries[0].labels)
    reviewing = start.append_vote(vote, version="2.0.0")
    with pytest.raises(ValidationError, match="disposition"):
        TruthEntry(
            sample_id="c",
            state="labeled",
            labels=vote.labels,
            review=reviewing,
            review_ref=report_ref(reviewing),
        )
    with pytest.raises(ValidationError, match="independent"):
        reviewing.adjudicate(
            adjudication(reviewing, vote, adjudicator="voter"), version="3.0.0"
        )
    with pytest.raises(ValidationError, match="duplicate"):
        reviewing.append_vote(vote, version="3.0.0")
    complete = reviewing.adjudicate(adjudication(reviewing, vote), version="3.0.0")
    with pytest.raises(ValidationError, match="exact review"):
        complete.reviewed_entry(report_ref(reviewing))
    with pytest.raises(ValueError, match="closed"):
        complete.append_vote(make_vote(complete, "late", None), version="4.0.0")
    changed = truth.model_copy(update={"version": "4.0.0"})
    with pytest.raises(ValueError, match="revision"):
        revise_truth(dataset, changed, complete, report_ref(complete), version="5.0.0")


def test_append_only_versions_cannot_reuse_an_earlier_number():
    dataset, truth, _, _ = synthetic_inputs()
    start = new_review(dataset, "c", truth.pin())
    vote = make_vote(start, "voter", truth.entries[0].labels)
    reviewing = start.append_vote(vote, version="2.0.0")
    with pytest.raises(ValidationError, match="successor version"):
        reviewing.adjudicate(adjudication(reviewing, vote), version="1.0.0")
    complete = reviewing.adjudicate(adjudication(reviewing, vote), version="3.0.0")
    with pytest.raises(ValidationError, match="successor version"):
        revise_truth(dataset, truth, complete, report_ref(complete), version="1.0.0")


def test_reopening_labeled_truth_cannot_remove_it_without_explicit_adjudication():
    dataset, truth, _, _ = synthetic_inputs()
    history = new_review(dataset, "a", truth.pin())
    with pytest.raises(ValueError, match="publishing truth"):
        revise_truth(dataset, truth, history, report_ref(history), version="2.0.0")
    pending = history.append_vote(
        make_vote(history, "abstaining-annotator", None), version="2.0.0"
    )
    with pytest.raises(ValueError, match="explicit adjudication"):
        revise_truth(dataset, truth, pending, report_ref(pending), version="2.0.0")
    withdrawn = pending.adjudicate(adjudication(pending, None), version="3.0.0")
    updated = revise_truth(
        dataset, truth, withdrawn, report_ref(withdrawn), version="2.0.0"
    )
    assert updated.entries[0].state == "abstained"
    assert truth.entries[0].state == "labeled"


def test_votes_and_adjudication_cannot_be_reused_for_another_sample_or_revision():
    dataset, truth, _, _ = synthetic_inputs()
    a = new_review(dataset, "a", truth.pin())
    b = new_review(dataset, "b", truth.pin())
    vote = make_vote(a, "reviewer-a", truth.entries[0].labels)
    with pytest.raises(ValidationError, match="exact review and sample"):
        b.append_vote(vote, version="2.0.0")
    a = a.append_vote(vote, version="2.0.0")
    record = adjudication(a, vote)
    extra = make_vote(a, "reviewer-b", None)
    newer = a.append_vote(extra, version="3.0.0")
    with pytest.raises(ValidationError, match="exact review revision"):
        newer.adjudicate(record, version="4.0.0")
    fake = record.model_copy(update={"selected_vote_digest": "b" * 64})
    with pytest.raises(ValidationError, match="recorded substantive vote"):
        a.adjudicate(fake, version="3.0.0")


def test_annotation_catalogue_cannot_implicitly_expand():
    dataset, truth, predictions, protocol = synthetic_inputs()
    binding = protocol.catalogue.profiles[0]
    additional = binding.model_copy(
        update={
            "profile_ref": binding.profile_ref.model_copy(
                update={"artifact_id": "another-profile"}
            )
        }
    )
    catalogue = protocol.catalogue.model_copy(
        update={"profiles": (binding, additional)}
    )
    changed = protocol.model_copy(update={"catalogue": catalogue, "version": "2.0.0"})
    with pytest.raises(ValueError, match="same annotation catalogue"):
        score(dataset, truth, predictions, changed, expected_protocol=changed.pin())


def test_positive_requires_actual_decision_and_rule_owner():
    _, _, predictions, _ = synthetic_inputs()
    pred = predictions.entries[0]
    with pytest.raises(ValidationError, match="positive task Decision"):
        SamplePrediction.model_validate(
            pred.model_copy(
                update={
                    "discovery": DiscoveryOutcome.NOT_EVALUATED,
                    "profiles": (),
                    "decision_records": (),
                }
            )
        )
    decision = pred.decision_records[0].model_copy(
        update={"execution_id": "other-execution"}
    )
    with pytest.raises(ValidationError, match="predicted Execution"):
        SamplePrediction.model_validate(
            pred.model_copy(update={"decision_records": (decision,)})
        )


@pytest.mark.parametrize(
    "mutation", ["input", "tool", "model", "source-pin", "truth-pin"]
)
def test_independently_pinned_input_and_journal_exhaustion(mutation):
    dataset, truth, predictions, protocol = synthetic_inputs()
    source = trial_source(predictions)
    pred = predictions.entries[0]
    expected = source.pin()
    expected_truth = truth.pin()
    if mutation == "input":
        execution = pred.execution.model_copy(
            update={
                "pins": pred.execution.pins.model_copy(
                    update={"input_digest": "d" * 64}
                ),
            }
        )
        pred = pred.model_copy(update={"execution": execution})
    elif mutation == "tool":
        pred = pred.model_copy(update={"tool_executions": ()})
    elif mutation == "model":
        addition = pred.model_invocations[0].model_copy(
            update={"model_invocation_id": "added"}
        )
        pred = pred.model_copy(
            update={"model_invocations": (*pred.model_invocations, addition)}
        )
    elif mutation == "source-pin":
        expected = expected.model_copy(update={"digest": "d" * 64})
    else:
        expected_truth = expected_truth.model_copy(update={"digest": "d" * 64})
    if mutation in ("input", "tool", "model"):
        predictions = predictions.model_copy(
            update={"entries": (pred, *predictions.entries[1:])}
        )
        # Isolate the per-sample reconciliation, keeping the original independent audit rows.
        source = source.model_copy(update={"predictions": predictions.pin()})
        expected = source.pin()
    with pytest.raises(
        ValueError, match="Execution input|inventory|independently approved"
    ):
        production_score(
            dataset,
            truth,
            predictions,
            protocol,
            source=source,
            expected_protocol=protocol.pin(),
            expected_source=expected,
            expected_truth=expected_truth,
        )


@pytest.mark.parametrize("side", ["truth", "prediction"])
def test_invalid_same_shape_value_cannot_pass_fact_schema(side):
    dataset, truth, predictions, protocol = synthetic_inputs()
    invalid = EnumValue(kind="enum", value="out-of-domain")
    if side == "truth":
        labels = truth.entries[0].labels
        obj = labels.objects[0]
        geometry = obj.geometry[0].model_copy(update={"value": invalid})
        obj = obj.model_copy(update={"geometry": (geometry,)})
        entry = reviewed(dataset, "a", labels.model_copy(update={"objects": (obj,)}))
        truth = truth.model_copy(update={"entries": (entry, *truth.entries[1:])})
    else:
        pred = predictions.entries[0]
        profile = pred.profiles[0]
        obj = profile.objects[0]
        geometry = obj.geometry[0].model_copy(update={"value": invalid})
        obj = obj.model_copy(update={"geometry": (geometry,)})
        profile = profile.model_copy(update={"objects": (obj,)})
        pred = pred.model_copy(update={"profiles": (profile,)})
        predictions = predictions.model_copy(
            update={"entries": (pred, *predictions.entries[1:])}
        )
    with pytest.raises(ValueError, match="pinned Fact Schema"):
        score(dataset, truth, predictions, protocol, expected_protocol=protocol.pin())


def test_question_known_in_bundle_is_not_valid_for_another_profile():
    dataset, truth, predictions, protocol = synthetic_inputs()
    binding = protocol.catalogue.profiles[0]
    extra = binding.model_copy(
        update={
            "profile_ref": binding.profile_ref.model_copy(
                update={"artifact_id": "another-profile"}
            )
        }
    )
    binding = binding.model_copy(update={"questions": ()})
    catalogue = protocol.catalogue.model_copy(update={"profiles": (binding, extra)})
    protocol = protocol.model_copy(update={"catalogue": catalogue})
    entries = [
        reviewed(dataset, row.sample_id, row.labels, catalogue=catalogue)
        if row.state == "labeled"
        else TruthEntry(sample_id=row.sample_id, state="unlabeled")
        for row in truth.entries
    ]
    truth = truth.model_copy(
        update={
            "entries": tuple(entries),
            "catalogue_digest": canonical_digest(catalogue),
        }
    )
    with pytest.raises(ValueError, match="outside its Profile binding"):
        score(dataset, truth, predictions, protocol, expected_protocol=protocol.pin())
