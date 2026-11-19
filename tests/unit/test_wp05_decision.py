"""Exercise the configured evaluator against signed fixture definitions and persisted records."""

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import ArtifactKind, DecisionResult
from jimu_runtime.domain.nodes.context import ContextBuildInput, SuccessfulContextBuild
from jimu_runtime.domain.nodes.envelopes import NodeInputEnvelope
from jimu_runtime.domain.runtime.coverage import (
    AncestorNotEvaluatedObligation,
    IncludedObligation,
    RuleSetObligationTarget,
)
from jimu_runtime.domain.runtime.decisions import (
    BlockedRuleOutcome,
    CandidateCategory,
    EvaluatedRuleOutcome,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import EvidenceRef, PolicyContentRef
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.inspection.decision import (
    DecisionSourceSnapshot,
    build_decision,
    build_profile_gate,
    candidate_binding,
    decision_unresolved,
    validate_decision,
)
from jimu_runtime.inspection.perception.candidates import CandidateModelResult
from jimu_runtime.workflow.mock_decision import _original_image
from jimu_runtime.workflow.mock_support import VERSION, K, artifact
from jimu_runtime.workflow.worker import AdapterRejected
from jimu_runtime.workflow.worker_coverage import build_coverage_authority
from jimu_runtime.workflow.worker_nodes import BranchInputs
from jimu_runtime.workflow.worker_records import (
    ContextMessage,
    DecisionMessage,
    EligibilityMessage,
)
from jimu_runtime.workflow.worker_source import OpenExecution, TrustedWorkerSource
from tests.fixtures.wp03.coverage.scenario import Scenario, scenario
from tests.fixtures.wp03.nodes.scenario import RGB_IMAGE


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    return scenario(tmp_path_factory.mktemp("decision"))


def _open(runtime: Scenario) -> tuple[OpenExecution, BranchInputs]:
    records = runtime.runtime
    base = runtime.scope
    snapshot = runtime.sources.context_snapshot
    output = runtime.sources.context_output
    assert records is not None and snapshot is not None
    assert isinstance(output, SuccessfulContextBuild)
    context_run = next(
        node for node in runtime.sources.node_runs if node.node_id is NodeId.N01
    )
    source = TrustedWorkerSource(base.media, snapshot.device_ref, snapshot.camera_ref)
    opened = OpenExecution(
        base,
        runtime.resolved,
        records.release,
        source,
        snapshot.region_ref,
        base.resolve(snapshot.region_ref, RegionDefinition),
        runtime.state.profiles,
    )
    context_envelope = NodeInputEnvelope(
        schema_version=VERSION,
        execution_id=base.execution_id,
        release_id=base.pins.release_id,
        bundle_digest=base.pins.bundle_digest,
        node_id=NodeId.N01,
        task_id=base.task.task_id,
        node_run_id=context_run.node_run_id,
        attempt_id=output.envelope.attempt_id,
        trace=output.envelope.trace,
        node_config_ref=base.execution_config_ref,
        upstream_artifacts=context_run.upstream_inputs,
    )
    inputs = ContextBuildInput(
        schema_version=VERSION,
        envelope=context_envelope,
        accepted_task=base.task,
        media_asset=base.media,
        device_ref=snapshot.device_ref,
        camera_ref=snapshot.camera_ref,
        region_ref=snapshot.region_ref,
        context_schema_ref=snapshot.context_schema_ref,
        context_provenance_policy_ref=snapshot.context_provenance_policy_ref,
    )
    context = ContextMessage(
        schema_version=VERSION, inputs=inputs, output=output, snapshot=snapshot
    )
    gate = build_profile_gate(opened, inputs, output, snapshot)
    assert isinstance(gate, EligibilityMessage)
    return opened, BranchInputs(context=context, gate=gate)


def _gate_trace_pin(previous):
    (trace,) = previous.gate.traces
    return artifact(K.APPLICABILITY_TRACE, trace.applicability_trace_id, trace).pin


def _snapshot(runtime: Scenario) -> DecisionSourceSnapshot:
    records = runtime.runtime
    assert records is not None
    (stage,) = runtime.stages
    return DecisionSourceSnapshot(
        authority=build_coverage_authority(runtime.resolved, records),
        evidence_refs=tuple(
            EvidenceRef(record_kind=K.EVIDENCE, record_id=row.evidence_id)
            for row in stage.evidence
        ),
        evidence=stage.evidence,
        read_evidence=lambda row, limit: RGB_IMAGE
        if row.sha256 == sha256_digest(RGB_IMAGE) and len(RGB_IMAGE) <= limit
        else None,
        max_evidence_bytes=len(RGB_IMAGE),
    )


@pytest.mark.parametrize("mode", ("empty", "failed", "not_dispatched", "absent"))
def test_advisory_omission_and_failure_still_scan_every_required_rule(
    runtime: Scenario, mode: str
) -> None:
    opened, previous = _open(runtime)
    (stage,) = runtime.stages
    sources = _snapshot(runtime)
    inputs = stage.draft.inputs
    envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                *inputs.envelope.upstream_artifacts,
                _gate_trace_pin(previous),
                *(
                    artifact(K.EVIDENCE, row.evidence_id, row).pin
                    for row in sources.evidence
                ),
            )
        }
    )
    candidate = None
    if mode != "absent":
        candidate = CandidateModelResult(
            (),
            stage.invocation if mode != "not_dispatched" else None,
            "candidate.unavailable" if mode in ("failed", "not_dispatched") else None,
        )
    staged = build_decision(
        opened,
        previous,
        envelope,
        inputs.object_record,
        stage.draft.output_envelope.started_at,
        sources,
        candidate,
    )
    message = staged.message
    assert isinstance(message, DecisionMessage)
    record = message.draft.decision
    assert record.result is DecisionResult.VIOLATION
    assert record.explanation.evaluated_rules == stage.draft.inputs.mandatory_rule_paths
    assert (
        record.rule_evaluations[0].result.guard.fact_proofs
        == stage.decision.rule_evaluations[0].result.guard.fact_proofs
    )
    assert message.result().artifacts == staged.artifacts
    if mode in ("empty", "failed"):
        assert message.draft.candidate is not None
        assert message.draft.candidate.categories == ()
        assert message.draft.candidate.failure_code == (
            None if mode == "empty" else "candidate.unavailable"
        )
    else:
        assert message.draft.candidate is None
        assert message.invocation is None
        assert staged.model_refs == ()
    assert message.candidate_diagnostic == (
        "candidate.unavailable" if mode == "not_dispatched" else None
    )
    validate_decision(
        opened,
        previous,
        message,
        sources,
        envelope=envelope,
        object_record=inputs.object_record,
        at=stage.draft.output_envelope.started_at,
        candidate=candidate,
    )
    if candidate is not None and candidate.invocation is not None:
        with pytest.raises(ValueError, match="independently verified"):
            validate_decision(
                opened,
                previous,
                message,
                sources,
                envelope=envelope,
                object_record=inputs.object_record,
                at=stage.draft.output_envelope.started_at,
            )
    with pytest.raises(ValueError, match="different claimed invocation"):
        validate_decision(
            opened,
            previous,
            message,
            sources,
            envelope=envelope.model_copy(update={"attempt_id": "other"}),
            object_record=inputs.object_record,
            at=stage.draft.output_envelope.started_at,
            candidate=candidate,
        )


def test_source_evidence_failure_preserves_predicate_and_blocks_definitive_result(
    runtime: Scenario,
) -> None:
    opened, previous = _open(runtime)
    (stage,) = runtime.stages
    inputs = stage.draft.inputs
    sources = replace(_snapshot(runtime), read_evidence=lambda *_: b"corrupt evidence")
    envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                *inputs.envelope.upstream_artifacts,
                _gate_trace_pin(previous),
                *(
                    artifact(K.EVIDENCE, row.evidence_id, row).pin
                    for row in sources.evidence
                ),
            )
        }
    )
    result = build_decision(
        opened,
        previous,
        envelope,
        inputs.object_record,
        stage.draft.output_envelope.started_at,
        sources,
        None,
    )
    assert isinstance(result.message, DecisionMessage)
    decision = result.message.draft.decision
    assert decision.result is DecisionResult.UNCERTAIN
    evaluation = decision.rule_evaluations[0].result
    assert isinstance(evaluation, BlockedRuleOutcome)
    assert (
        evaluation.predicate is not None and evaluation.predicate.truth.value == "true"
    )
    assert isinstance(stage.decision.rule_evaluations[0].result, EvaluatedRuleOutcome)
    with pytest.raises(ValueError, match="differs from deterministic evaluation"):
        validate_decision(
            opened,
            previous,
            result.message,
            replace(sources, read_evidence=_snapshot(runtime).read_evidence),
            envelope=envelope,
            object_record=inputs.object_record,
            at=stage.draft.output_envelope.started_at,
        )


def test_gate_checks_exact_n01_snapshot(runtime: Scenario) -> None:
    opened, previous = _open(runtime)
    assert previous.context is not None and isinstance(
        previous.context.output, SuccessfulContextBuild
    )
    snapshot = previous.snapshot()
    changed = snapshot.model_copy(update={"values": ()})
    with pytest.raises(ValueError, match="selected successful N01"):
        build_profile_gate(
            opened, previous.context.inputs, previous.context.output, changed
        )

    forged = previous.context.output.model_copy(
        update={
            "validation_report": previous.context.output.validation_report.model_copy(
                update={
                    "optional_issues": (
                        ValidationIssue(
                            code="forged.invalid",
                            path=("zone",),
                            message="Untrusted report edit.",
                            related_refs=(),
                        ),
                    ),
                }
            ),
        }
    )
    with pytest.raises(AdapterRejected, match="perception.context_projection_mismatch"):
        build_profile_gate(opened, previous.context.inputs, forged, snapshot)


@pytest.fixture(scope="module")
def negative_runtime(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    return scenario(tmp_path_factory.mktemp("decision-negative"), mode="negative")


@pytest.mark.parametrize("question_missing", (False, True))
def test_negative_requires_guard_even_when_predicate_is_false(
    negative_runtime: Scenario, question_missing: bool
) -> None:
    opened, previous = _open(negative_runtime)
    (stage,) = negative_runtime.stages
    inputs = stage.draft.inputs
    sources = _snapshot(negative_runtime)
    question_id = next(
        fact.fact_id
        for fact in sources.authority.records.facts
        if fact.source.source_kind.value == "vlm_question"
    )
    envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                *(
                    pin
                    for pin in inputs.envelope.upstream_artifacts
                    if not (question_missing and pin.ref.record_id == question_id)
                ),
                _gate_trace_pin(previous),
                *(
                    artifact(K.EVIDENCE, row.evidence_id, row).pin
                    for row in sources.evidence
                ),
            )
        }
    )
    result = build_decision(
        opened,
        previous,
        envelope,
        inputs.object_record,
        stage.draft.output_envelope.started_at,
        sources,
        None,
    )
    assert isinstance(result.message, DecisionMessage)
    decision = result.message.draft.decision
    evaluation = decision.rule_evaluations[0].result
    assert isinstance(evaluation, EvaluatedRuleOutcome | BlockedRuleOutcome)
    assert (
        evaluation.predicate is not None and evaluation.predicate.truth.value == "false"
    )
    if question_missing:
        assert isinstance(evaluation, BlockedRuleOutcome)
        assert "predicate.fact.missing" in evaluation.predicate.diagnostic_codes
        assert decision.result is DecisionResult.UNCERTAIN
    else:
        assert isinstance(evaluation, EvaluatedRuleOutcome)
        assert decision.result is DecisionResult.NO_VIOLATION


def test_collect_all_retains_every_matching_path_and_deduplicates_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jimu_runtime.bundle.compiler import ReferenceSlot, SourceIdentity
    from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
    from tests.fixtures.wp02.compiler.scenario import approve_source_bytes
    from tests.fixtures.wp03.nodes import scenario as node_fixture

    original = node_fixture.parking_source

    def with_second_rule(**kwargs):
        source = original(**kwargs)
        (rule,) = tuple(
            item for item in source.artifacts if item.kind is ArtifactKind.RULE
        )
        (ruleset,) = tuple(
            item for item in source.artifacts if item.kind is ArtifactKind.RULE_SET
        )
        rule_doc = json.loads(rule.content)
        rule_doc["rule_id"] = "same-category-second-rule"
        second = replace(
            rule,
            artifact_id=rule_doc["rule_id"],
            content=canonical_bytes(rule_doc),
            origin=SourceIdentity(
                artifact_id="rule:same-category-second-rule", version=VERSION
            ),
        )
        set_doc = json.loads(ruleset.content)
        set_doc["rule_refs"].append(None)
        expanded = replace(
            ruleset,
            content=canonical_bytes(set_doc),
            reference_slots=(
                *ruleset.reference_slots,
                ReferenceSlot(location=("rule_refs", 1), target=second.identity),
            ),
        )
        return approve_source_bytes(
            replace(
                source,
                artifacts=(
                    *(
                        expanded if item is ruleset else item
                        for item in source.artifacts
                    ),
                    second,
                ),
            )
        )

    monkeypatch.setattr(node_fixture, "parking_source", with_second_rule)
    expanded_runtime = scenario(tmp_path)
    opened, previous = _open(expanded_runtime)
    (stage,) = expanded_runtime.stages
    inputs = stage.draft.inputs
    sources = _snapshot(expanded_runtime)
    envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                *inputs.envelope.upstream_artifacts,
                _gate_trace_pin(previous),
                *(
                    artifact(K.EVIDENCE, row.evidence_id, row).pin
                    for row in sources.evidence
                ),
            )
        }
    )
    result = build_decision(
        opened,
        previous,
        envelope,
        inputs.object_record,
        stage.draft.output_envelope.started_at,
        sources,
        CandidateModelResult((), stage.invocation, None),
    )
    assert isinstance(result.message, DecisionMessage)
    decision = result.message.draft.decision
    assert decision.result is DecisionResult.VIOLATION
    assert decision.conflict_resolution.matched_rules == inputs.mandatory_rule_paths
    assert len(decision.conflict_resolution.matched_rules) == 2
    assert len(decision.conflict_resolution.matched_category_ids) == 1


@pytest.mark.parametrize(
    ("parent_truth", "with_second", "ambiguity", "optional", "positive", "expected"),
    (
        ("false", False, False, False, False, DecisionResult.NOT_APPLICABLE),
        ("false", True, False, False, False, DecisionResult.NO_VIOLATION),
        ("unknown", True, False, False, False, DecisionResult.UNCERTAIN),
        ("false", True, True, False, True, DecisionResult.VIOLATION),
        ("unknown", True, False, True, False, DecisionResult.UNCERTAIN),
        ("unknown", True, False, True, True, DecisionResult.VIOLATION),
    ),
)
def test_decisive_ruleset_never_runs_its_child_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_truth: str,
    with_second: bool,
    ambiguity: bool,
    optional: bool,
    positive: bool,
    expected: DecisionResult,
) -> None:
    from jimu_runtime.bundle.compiler import ReferenceSlot, SourceIdentity
    from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
    from jimu_runtime.domain.runtime.applicability import NotEvaluatedApplicabilityLevel
    from tests.fixtures.wp02.compiler.scenario import approve_source_bytes
    from tests.fixtures.wp03.nodes import scenario as node_fixture

    original = node_fixture.parking_source

    def revise_hierarchy(**kwargs):
        source = original(**kwargs)
        policy = next(
            item for item in source.artifacts if item.artifact_id == "applicability"
        )
        child_doc = json.loads(policy.content)
        child_doc["policy_id"] = "excluded-child"
        child_doc["policy"]["condition_ast"]["constant"]["value"]["value"] = False
        child = replace(
            policy,
            artifact_id="excluded-child",
            content=canonical_bytes(child_doc),
            origin=SourceIdentity(artifact_id="policy:excluded-child", version=VERSION),
        )
        parent_doc = json.loads(policy.content)
        parent_doc["policy_id"] = "decisive-parent"
        fields = ()
        if parent_truth == "false":
            parent_doc["policy"]["condition_ast"]["constant"]["value"]["value"] = False
        else:
            selector = {
                "source": "region_metadata",
                "schema_ref": None,
                "field_path": ["zone"],
            }
            parent_doc["policy"].update(
                context_allowlist=[selector],
                condition_ast={
                    "kind": "comparison",
                    "operator": "ne",
                    "left": {"kind": "context_ref", "selector": selector},
                    "right": {
                        "kind": "literal",
                        "constant": {
                            "type_definition": {"kind": "string"},
                            "value": {"kind": "string", "value": "fixture-parking"},
                        },
                    },
                },
            )
            fields = (
                ReferenceSlot(
                    location=("policy", "context_allowlist", 0, "schema_ref"),
                    target=(ArtifactKind.CONTEXT_SCHEMA, "context", VERSION),
                    schema_ref=True,
                ),
                ReferenceSlot(
                    location=(
                        "policy",
                        "condition_ast",
                        "left",
                        "selector",
                        "schema_ref",
                    ),
                    target=(ArtifactKind.CONTEXT_SCHEMA, "context", VERSION),
                    schema_ref=True,
                ),
            )
        parent = replace(
            policy,
            artifact_id="decisive-parent",
            content=canonical_bytes(parent_doc),
            reference_slots=fields,
            origin=SourceIdentity(
                artifact_id="policy:decisive-parent", version=VERSION
            ),
        )
        updated = []
        for entry in source.artifacts:
            if entry.kind in (ArtifactKind.RULE_SET, ArtifactKind.RULE):
                target = parent if entry.kind is ArtifactKind.RULE_SET else child
                entry = replace(
                    entry,
                    reference_slots=tuple(
                        replace(slot, target=target.identity)
                        if slot.location == ("applicability_policy_ref",)
                        else slot
                        for slot in entry.reference_slots
                    ),
                )
            updated.append(entry)
        if optional:
            rule_policy = next(
                row for row in source.artifacts if row.artifact_id == "rule"
            )
            policy_doc = json.loads(rule_policy.content)
            policy_doc["policy_id"] = "fixture-optional-rule"
            policy_doc["policy"]["mandatory"] = False
            optional_policy = replace(
                rule_policy,
                artifact_id=policy_doc["policy_id"],
                content=canonical_bytes(policy_doc),
                origin=SourceIdentity(
                    artifact_id="policy:fixture-optional-rule", version=VERSION
                ),
            )
            updated = [
                replace(
                    row,
                    reference_slots=tuple(
                        replace(slot, target=optional_policy.identity)
                        if slot.location == ("rule_policy_ref",)
                        else slot
                        for slot in row.reference_slots
                    ),
                )
                if row.kind is ArtifactKind.RULE
                else row
                for row in updated
            ]
            updated.append(optional_policy)
        if with_second:
            rule = next(
                entry for entry in source.artifacts if entry.kind is ArtifactKind.RULE
            )
            rule_doc = json.loads(rule.content)
            condition = json.loads(child.content)["policy"]["condition_ast"]
            condition["constant"]["value"]["value"] = positive
            rule_doc.update(
                rule_id="fixture-second-rule",
                required_fact_selectors=[],
                condition_ast=condition,
            )
            second = replace(
                rule,
                artifact_id=rule_doc["rule_id"],
                content=canonical_bytes(rule_doc),
                origin=SourceIdentity(
                    artifact_id="rule:fixture-second-rule", version=VERSION
                ),
                reference_slots=tuple(
                    slot for slot in rule.reference_slots if len(slot.location) == 1
                ),
            )
            ruleset = next(
                entry
                for entry in source.artifacts
                if entry.kind is ArtifactKind.RULE_SET
            )
            set_doc = json.loads(ruleset.content)
            set_doc["rule_set_id"] = "reached-second-set"
            second_set = replace(
                ruleset,
                artifact_id=set_doc["rule_set_id"],
                content=canonical_bytes(set_doc),
                origin=SourceIdentity(
                    artifact_id="rule_set:reached-second-set", version=VERSION
                ),
                reference_slots=tuple(
                    replace(slot, target=second.identity)
                    if slot.location == ("rule_refs", 0)
                    else slot
                    for slot in ruleset.reference_slots
                ),
            )
            if ambiguity:
                binding = next(
                    entry
                    for entry in source.artifacts
                    if entry.kind is ArtifactKind.MODEL_BINDING
                    and entry.artifact_id == "candidate_prediction"
                )
                binding_doc = json.loads(binding.content)
                binding_doc["model_binding_id"] = "additional-candidate-binding"
                competing = replace(
                    binding,
                    artifact_id=binding_doc["model_binding_id"],
                    content=canonical_bytes(binding_doc),
                    origin=SourceIdentity(
                        artifact_id="model_binding:additional-candidate-binding",
                        version=VERSION,
                    ),
                )
                second_set = replace(
                    second_set,
                    reference_slots=tuple(
                        replace(slot, target=competing.identity)
                        if slot.location == ("candidate_binding_ref",)
                        else slot
                        for slot in second_set.reference_slots
                    ),
                )
                updated.append(competing)
            profile = next(
                entry for entry in updated if entry.kind is ArtifactKind.PROFILE
            )
            profile_doc = json.loads(profile.content)
            profile_doc["rule_set_refs"].append(None)
            updated = [
                replace(
                    entry,
                    content=canonical_bytes(profile_doc),
                    reference_slots=(
                        *entry.reference_slots,
                        ReferenceSlot(
                            location=("rule_set_refs", 1), target=second_set.identity
                        ),
                    ),
                )
                if entry is profile
                else entry
                for entry in updated
            ]
            updated.extend((second, second_set))
        return approve_source_bytes(
            replace(source, artifacts=(*updated, parent, child))
        )

    monkeypatch.setattr(node_fixture, "parking_source", revise_hierarchy)
    variant = scenario(tmp_path, mode="decision-failed")
    opened, previous = _open(variant)
    records = variant.runtime
    assert records is not None
    (obj,) = records.objects
    node = next(
        item for item in variant.sources.node_runs if item.node_id is NodeId.N06
    )
    (link,) = node.attempt_history
    envelope = NodeInputEnvelope(
        schema_version=VERSION,
        node_id=NodeId.N06,
        task_id=opened.scope.task.task_id,
        execution_id=node.execution_id,
        release_id=opened.scope.pins.release_id,
        bundle_digest=opened.scope.pins.bundle_digest,
        node_run_id=node.node_run_id,
        attempt_id=link.attempt_ref.record_id,
        trace=node.trace,
        upstream_artifacts=(*node.upstream_inputs, _gate_trace_pin(previous)),
        node_config_ref=opened.scope.execution_config_ref,
    )
    assert node.started_at is not None
    evidence = ()
    if with_second:
        active = opened.profiles[0].rules.rule_sets[1].rules[0]
        policy_ref = PolicyContentRef.model_validate(
            active.definition.evidence_policy_ref.model_dump()
        )
        original = _original_image(
            variant.scope,
            opened.scope.check_call(envelope, NodeId.N06, node.started_at),
            obj,
            policy_ref,
        )
        evidence = (original,)
        envelope = envelope.model_copy(
            update={
                "upstream_artifacts": (
                    *envelope.upstream_artifacts,
                    artifact(K.EVIDENCE, original.evidence_id, original).pin,
                ),
            }
        )
    sources = DecisionSourceSnapshot(
        authority=build_coverage_authority(variant.resolved, records),
        evidence_refs=tuple(
            EvidenceRef(record_kind=K.EVIDENCE, record_id=row.evidence_id)
            for row in evidence
        ),
        evidence=evidence,
        read_evidence=lambda *_: RGB_IMAGE,
        max_evidence_bytes=len(RGB_IMAGE),
    )
    if ambiguity:
        assert candidate_binding(opened, obj) is None
        result = _invoke_without_candidate(
            opened, previous, sources, envelope, obj, node
        )
    else:
        result = build_decision(
            opened, previous, envelope, obj, node.started_at, sources, None
        )
    assert isinstance(result.message, DecisionMessage)
    draft = result.message.draft
    assert draft.decision.result is expected
    assert len(draft.decision.rule_evaluations) == int(with_second)
    parent_trace = result.message.traces[0]
    assert parent_trace.effective_truth.value == parent_truth
    if optional:
        assert draft.decision.explanation.not_evaluated_rules == ()
        assert len(draft.rule_evaluations) == 1
        assert all(
            len(trace.levels) < 3 or trace.levels[1] != parent_trace.levels[1]
            for trace in result.message.traces
        )
    else:
        assert (
            draft.decision.explanation.not_evaluated_rules
            == draft.inputs.mandatory_rule_paths[:1]
        )
        child_trace = result.message.traces[1]
        assert child_trace.effective_truth is parent_trace.effective_truth
        assert isinstance(child_trace.levels[-1], NotEvaluatedApplicabilityLevel)
        assert draft.rule_evaluations[0].attempt_ref is None
        assert isinstance(
            result.message.obligations[1].outcome, AncestorNotEvaluatedObligation
        )
    unresolved = decision_unresolved(result.message)
    assert len(unresolved) == (1 if parent_truth == "unknown" else 0)
    if unresolved:
        assert unresolved[0].applicability_trace_ref == parent_trace.trace_ref
    if ambiguity:
        assert result.model_refs == ()
        assert result.message.invocation is None and draft.candidate is None
        assert len(draft.decision.conflict_resolution.matched_rules) == 1
        reopened = DecisionMessage.model_validate_json(result.message.model_dump_json())
        assert reopened.candidate_diagnostic == "candidate_binding_ambiguous"
        validate_decision(
            opened,
            previous,
            reopened,
            sources,
            envelope=envelope,
            object_record=obj,
            at=node.started_at,
        )
        return
    if not with_second:
        return

    checked = _finalize_variant(variant, previous, result.message, node)
    assert previous.gate is not None
    args = (
        checked.coverage,
        previous.gate.evaluation,
        draft.inputs.mandatory_rule_paths,
    )
    outlook = checked.coverage.coverage_status
    assert outlook.value == ("partial" if parent_truth == "unknown" else "complete")
    draft.bind_coverage(*args, applicability_traces=result.message.traces)
    if optional:
        assert bool(checked.coverage.matches) is positive
        return
    if parent_truth == "unknown":
        false_decision = draft.decision.model_copy(
            update={"result": DecisionResult.NO_VIOLATION}
        )
        changed = draft.model_copy(update={"decision": false_decision})
        with pytest.raises(ValueError, match="unresolved mandatory scope"):
            changed.bind_coverage(*args, applicability_traces=result.message.traces)
        return

    # Refusing a negative without the ancestor's exact pinned trace leaves
    # existing N06 calls with fully evaluated coverage backward compatible.
    with pytest.raises(ValueError, match="unresolved mandatory scope"):
        draft.bind_coverage(*args, applicability_traces=result.message.traces[1:])
    forged = parent_trace.model_copy(
        update={"created_at": parent_trace.created_at + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="unresolved mandatory scope"):
        draft.bind_coverage(
            *args, applicability_traces=(forged, *result.message.traces[1:])
        )

    coverage = checked.coverage
    obligations = coverage.mandatory_rule_coverage.obligations
    projected = next(
        row
        for row in obligations
        if isinstance(row.outcome, AncestorNotEvaluatedObligation)
    )
    wrong_ancestor = next(
        row
        for row in obligations
        if isinstance(row.target, RuleSetObligationTarget)
        and isinstance(row.outcome, IncludedObligation)
    )
    bad = projected.model_copy(
        update={
            "outcome": projected.outcome.model_copy(
                update={"ancestor_obligation_id": wrong_ancestor.obligation_id}
            )
        }
    )
    ledger = coverage.mandatory_rule_coverage.model_copy(
        update={
            "obligations": tuple(
                bad if row is projected else row for row in obligations
            )
        }
    )
    with pytest.raises(ValueError, match="unresolved mandatory scope"):
        draft.bind_coverage(
            coverage.model_copy(update={"mandatory_rule_coverage": ledger}),
            previous.gate.evaluation,
            draft.inputs.mandatory_rule_paths,
            applicability_traces=result.message.traces,
        )


def _finalize_variant(variant, previous, message, node):
    from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus
    from jimu_runtime.workflow.engine import plan_work
    from jimu_runtime.workflow.worker_coverage import finalize_execution

    draft = message.draft
    completed = node.model_copy(
        update={
            "status": NodeRunStatus.SUCCEEDED,
            "reason": None,
            "output_refs": tuple(
                row.ref for row in draft.output_envelope.output_artifacts
            ),
        }
    )
    runs = tuple(
        replace(run, run=completed) if run.run == node else run
        for run in variant.state.runs
    )
    state = replace(variant.state, runs=runs, decision_drafts=(draft,))
    attempts = tuple(
        row.model_copy(update={"status": AttemptStatus.SUCCEEDED, "reason": None})
        if row.attempt_id == draft.inputs.envelope.attempt_id
        else row
        for row in variant.sources.attempts
    )
    sources = variant.sources.model_copy(
        update={
            "node_runs": tuple(run.run for run in runs),
            "attempts": attempts,
            "decision_blockers": (),
        }
    )
    records = variant.runtime.model_copy(
        update={"node_runs": sources.node_runs, "attempts": attempts}
    )
    return finalize_execution(
        state,
        plan_work(state).coverage,
        scope=variant.scope,
        sources=sources,
        gate=previous.gate.result(),
        stages=(message.result(),),
        facts=records.facts,
        authorities=records.authority_selections,
        evidence=message.evidence,
        invocations=records.model_invocations,
        authority=build_coverage_authority(variant.resolved, records),
        now=sources.execution.started_at,
        additional_unresolved=decision_unresolved(message),
    )


def _invoke_without_candidate(opened, previous, sources, envelope, obj, node):
    from types import SimpleNamespace

    from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus
    from jimu_runtime.workflow.engine import BranchKey
    from jimu_runtime.workflow.jobs import JobClaim
    from jimu_runtime.workflow.worker import ClaimedNode
    from jimu_runtime.workflow.worker_inspection import InspectionNodes

    async def unreachable(*_args, **_kwargs):
        pytest.fail(
            "ambiguous advisory bindings must not dispatch any physical request"
        )

    called = InspectionNodes(
        SimpleNamespace(),
        SimpleNamespace(stage=unreachable),
        SimpleNamespace(retains_response_evidence=True, invoke_model=unreachable),
        unreachable,
        max_evidence_bytes=sources.max_evidence_bytes,
        model_resource_scope_id="irrelevant",
    )
    # Keep production branching and Decision authority validation. Supply the
    # already-verified immutable Evidence snapshot held by this unit fixture.
    called._decision_sources = lambda _claimed: sources
    previous.grounding = SimpleNamespace(objects=(obj,))
    claimed_node = node.model_copy(
        update={
            "status": NodeRunStatus.RUNNING,
            "ended_at": None,
            "reason": None,
            "upstream_inputs": envelope.upstream_artifacts,
        }
    )
    records = sources.authority.records
    (existing,) = tuple(
        row for row in records.attempts if row.attempt_id == envelope.attempt_id
    )
    attempt = existing.model_copy(
        update={
            "status": AttemptStatus.RUNNING,
            "ended_at": None,
            "reason": None,
        }
    )
    claim = ClaimedNode(
        JobClaim(
            opened.scope.task.idempotency_scope,
            node.execution_id,
            node.node_run_id,
            "fixture-job",
            1,
            "fixture",
            node.started_at,
        ),
        attempt,
        claimed_node,
        BranchKey(NodeId.N06, obj.object_id),
        opened,
        previous,
        records.execution,
        node.started_at,
    )
    return asyncio.run(called.invoke(claim))


@pytest.mark.parametrize(
    "kind", (K.FACT, K.AUTHORITATIVE_RESULT_SELECTION, K.APPLICABILITY_TRACE)
)
def test_pinned_inputs_reject_same_identity_with_wrong_content(
    runtime: Scenario, kind
) -> None:
    opened, previous = _open(runtime)
    (stage,) = runtime.stages
    inputs = stage.draft.inputs
    sources = _snapshot(runtime)
    pins = (
        *inputs.envelope.upstream_artifacts,
        _gate_trace_pin(previous),
        *(artifact(K.EVIDENCE, row.evidence_id, row).pin for row in sources.evidence),
    )
    envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": tuple(
                pin.model_copy(update={"digest": "f" * 64})
                if pin.ref.record_kind is kind
                else pin
                for pin in pins
            ),
        }
    )
    with pytest.raises(
        ValueError, match="input pin differs from the actual runtime record digest"
    ):
        build_decision(
            opened,
            previous,
            envelope,
            inputs.object_record,
            stage.draft.output_envelope.started_at,
            sources,
            None,
        )


def test_self_consistent_advisory_candidate_requires_independent_response(
    runtime: Scenario,
) -> None:
    opened, previous = _open(runtime)
    (stage,) = runtime.stages
    inputs = stage.draft.inputs
    sources = _snapshot(runtime)
    env = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                *inputs.envelope.upstream_artifacts,
                _gate_trace_pin(previous),
                *(
                    artifact(K.EVIDENCE, row.evidence_id, row).pin
                    for row in sources.evidence
                ),
            )
        }
    )
    actual = CandidateModelResult((), stage.invocation, None)
    assert stage.invocation is not None
    category_id = stage.decision.conflict_resolution.matched_category_ids[0]
    altered = CandidateModelResult(
        (CandidateCategory(category_id=category_id, score=0.4),),
        stage.invocation,
        None,
    )
    at = stage.draft.output_envelope.started_at
    forged = build_decision(
        opened, previous, env, inputs.object_record, at, sources, altered
    )
    assert isinstance(forged.message, DecisionMessage)
    with pytest.raises(ValueError, match="differs from deterministic evaluation"):
        validate_decision(
            opened,
            previous,
            forged.message,
            sources,
            envelope=env,
            object_record=inputs.object_record,
            at=at,
            candidate=actual,
        )


def test_candidate_version_must_be_available_in_pinned_deployments(
    runtime: Scenario,
) -> None:
    opened, previous = _open(runtime)
    (stage,) = runtime.stages
    invocation = stage.invocation
    assert invocation is not None and invocation.actual_provider is not None
    invocation = invocation.model_copy(
        update={
            "actual_provider": invocation.actual_provider.model_copy(
                update={"model_version": "99.0.0"}
            )
        }
    )
    inputs = stage.draft.inputs
    sources = _snapshot(runtime)
    envelope = inputs.envelope.model_copy(
        update={
            "upstream_artifacts": (
                *inputs.envelope.upstream_artifacts,
                _gate_trace_pin(previous),
                *(
                    artifact(K.EVIDENCE, row.evidence_id, row).pin
                    for row in sources.evidence
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="unpinned provider version"):
        build_decision(
            opened,
            previous,
            envelope,
            inputs.object_record,
            stage.draft.output_envelope.started_at,
            sources,
            CandidateModelResult((), invocation, None),
        )
