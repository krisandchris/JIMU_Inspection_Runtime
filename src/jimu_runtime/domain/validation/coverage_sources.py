"""Resolve supplied Decision/Coverage views from actual node and runtime records."""

from __future__ import annotations

from dataclasses import dataclass

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import AttemptStatus, NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.registry import FactSchemaRegistry
from jimu_runtime.domain.nodes.context import ContextBuildOutput, SuccessfulContextBuild
from jimu_runtime.domain.nodes.discovery import (
    DiscoveryAttemptOutput,
    DiscoveryInput,
    DiscoveryOutput,
    SkippedDiscoveryOutput,
)
from jimu_runtime.domain.nodes.envelopes import NodeOutputEnvelope
from jimu_runtime.domain.nodes.grounding import _GroundingInvocationGap
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.authority import AuthoritativeResultSelection
from jimu_runtime.domain.runtime.candidates import (
    CandidateParseLimits,
    CandidateSnapshotLimits,
)
from jimu_runtime.domain.runtime.coverage import (
    ApplicableNotEvaluatedObligation,
    BlockedCoverageScope,
    CoverageReason,
    DependencyNotEvaluatedObligation,
    ExecutionCoverage,
    ProfileObligationTarget,
)
from jimu_runtime.domain.runtime.decisions import RecordList
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    ExecutionRecord,
    NodeId,
    NodeRun,
)
from jimu_runtime.domain.runtime.facts import (
    AttemptRef,
    FactRecord,
    NodeRunRef,
    ObjectRef,
)
from jimu_runtime.domain.runtime.intake import ContextSnapshot
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    GroundingRecord,
    ObjectRecord,
)
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.validation.authority_inputs import (
    AttemptDescriptor,
    NodeAttemptOwner,
    NodeRunDescriptor,
    ObjectDescriptor,
)
from jimu_runtime.domain.validation.decision_coverage import (
    DecisionCoverageRecords,
    _get,
    _GroundingFailureWitness,
    _GroundingNotEvaluatedWitness,
    _index,
    _validate_decision_coverage,
)
from jimu_runtime.domain.validation.producer_authority_inputs import (
    ProducerAuthorityValidationInput,
)
from jimu_runtime.domain.validation.runtime_authority import (
    RuntimeAuthorityRecords,
    validate_runtime_authority,
)


@dataclass(frozen=True, slots=True)
class CoverageAuthorityInput:
    claims: ProducerAuthorityValidationInput
    records: RuntimeAuthorityRecords
    fact_schemas: FactSchemaRegistry
    parse_limits: CandidateParseLimits
    snapshot_limits: CandidateSnapshotLimits


class ObjectDecisionBlocker(ContractModel):
    object_ref: ObjectRef
    node_run_ref: NodeRunRef
    attempt_ref: AttemptRef | None = None


class CoverageSources(ContractModel):
    """Explicitly selected sources for one finite coverage check."""

    execution: ExecutionRecord
    context_output: ContextBuildOutput | None
    context_snapshot: ContextSnapshot | None
    discovery_output: DiscoveryOutput | None
    discovery_input: DiscoveryInput | None
    question_selections: RecordList[QuestionSelectionRecord]
    grounding_records: RecordList[GroundingRecord]
    objects: RecordList[ObjectRecord]
    node_runs: RecordList[NodeRun]
    attempts: RecordList[Attempt]
    decision_blockers: RecordList[ObjectDecisionBlocker]


class _CoverageSourcesCheck:
    def __init__(
        self,
        coverage: ExecutionCoverage,
        records: DecisionCoverageRecords,
        sources: CoverageSources,
    ) -> None:
        self.coverage = coverage
        self.records = records
        self.sources = sources
        self.nodes = _index(sources.node_runs, lambda item: item.node_run_id)
        self.attempts = _index(sources.attempts, lambda item: item.attempt_id)
        self.objects = _index(sources.objects, lambda item: item.object_id)
        self.grounding = _index(
            sources.grounding_records, lambda item: item.profile_ref
        )

    def check_context(self) -> None:
        execution = self.sources.execution
        if (
            execution.execution_id,
            execution.pins.release_id,
            execution.pins.bundle_digest,
        ) != (
            self.coverage.execution_id,
            self.coverage.release_id,
            self.coverage.bundle_digest,
        ):
            raise ValueError("Coverage must match the actual Execution pin")
        output, snapshot, selected = (
            self.sources.context_output,
            self.sources.context_snapshot,
            self.records.applicability,
        )
        if selected is None:
            if (
                snapshot is not None
                or execution.context_snapshot is not None
                or isinstance(output, SuccessfulContextBuild)
            ):
                raise ValueError(
                    "blocked N01 coverage forbids a usable ContextSnapshot"
                )
            if output is not None:
                self.check_envelope(output.envelope)
                scope = self.coverage.applicability_scope
                if (
                    not isinstance(scope, BlockedCoverageScope)
                    or output.envelope.node_run_id
                    != scope.blocker_node_run_ref.record_id
                ):
                    raise ValueError(
                        "blocked context must retain its exact actual N01 output"
                    )
            return
        if (
            not isinstance(output, SuccessfulContextBuild)
            or snapshot is None
            or execution.context_snapshot is None
        ):
            raise ValueError(
                "evaluated gate requires actual N01 ContextSnapshot output and Execution pin"
            )
        self.check_envelope(output.envelope)
        app = selected.scope
        expected = (app.context_snapshot_ref, app.context_snapshot_digest)
        if (
            (output.context_snapshot_ref, output.context_snapshot_digest) != expected
            or (
                execution.context_snapshot.snapshot_ref,
                execution.context_snapshot.snapshot_digest,
            )
            != expected
            or (
                snapshot.context_snapshot_id,
                snapshot.execution_id,
                snapshot.task_id,
                snapshot.release_id,
                snapshot.bundle_digest,
            )
            != (
                expected[0].record_id,
                execution.execution_id,
                execution.task_id,
                execution.pins.release_id,
                execution.pins.bundle_digest,
            )
            or output.envelope.node_run_id
            != app.context_node_run.node_run_ref.record_id
        ):
            raise ValueError(
                "selected gate must use the actual N01/Execution ContextSnapshot ref and digest"
            )
        actual_n01 = self.nodes[output.envelope.node_run_id]
        if not any(
            ref.record_kind is RuntimeRecordKind.CONTEXT_SNAPSHOT
            and ref.record_id == snapshot.context_snapshot_id
            for ref in actual_n01.output_refs
        ):
            raise ValueError(
                "successful N01 must retain its selected ContextSnapshot output"
            )

    def check_n06_scope(self) -> None:
        for decision in self.records.decisions:
            node, _ = self.check_attempt(
                decision.node_run_ref.record_id, decision.attempt_ref.record_id
            )
            self.check_n06_object(node, decision.object_ref)
            if node.status is not NodeRunStatus.SUCCEEDED or not any(
                ref.record_kind is RuntimeRecordKind.DECISION
                and ref.record_id == decision.decision_id
                for ref in node.output_refs
            ):
                raise ValueError(
                    "actual successful N06 NodeRun must retain the selected Decision"
                )
        if self.records.applicability is not None:
            for binding in self.records.applicability.records.attempts:
                owner = binding.attempt.owner
                if not isinstance(owner, NodeAttemptOwner):
                    raise ValueError(
                        "N06 applicability requires its actual NodeRun owner"
                    )
                node, _ = self.check_attempt(
                    owner.node_run_ref.record_id, binding.attempt.attempt_ref.record_id
                )
                self.check_n06_object(node, binding.object_ref)

    def check_n06_object(self, node: NodeRun, object_ref: ObjectRef | None) -> None:
        object_ids = {
            upstream.ref.record_id
            for upstream in node.upstream_inputs
            if upstream.ref.record_kind is RuntimeRecordKind.OBJECT
        }
        expected = set() if object_ref is None else {object_ref.record_id}
        if node.node_id is not NodeId.N06 or object_ids != expected:
            raise ValueError(
                "N06 must retain its exact Object in its actual upstream scope"
            )

    def check_views(self) -> None:
        object_views = {
            ObjectDescriptor(
                object_ref=ObjectRef(
                    record_kind=RuntimeRecordKind.OBJECT, record_id=item.object_id
                ),
                execution_id=item.execution_id,
                profile_ref=item.profile_ref,
            )
            for item in self.sources.objects
        }
        if set(self.records.objects) != object_views:
            raise ValueError("Coverage Object views must match actual Object records")
        nodes = list(self.records.node_runs)
        attempts = list(self.records.attempts)
        if self.records.applicability is not None:
            app = self.records.applicability
            if set(app.records.objects) - object_views:
                raise ValueError(
                    "applicability Object views must match actual Object records"
                )
            nodes.extend([app.scope.context_node_run, *app.records.node_runs])
            attempts.extend(binding.attempt for binding in app.records.attempts)
        for view in nodes:
            node = _get(self.nodes, view.node_run_ref.record_id, "actual NodeRun")
            if view != NodeRunDescriptor(
                node_run_ref=view.node_run_ref,
                execution_id=node.execution_id,
                node_id=node.node_id.value,
                status=node.status,
            ):
                raise ValueError(
                    "Coverage NodeRun view differs from its actual NodeRun"
                )
        for attempt_view in attempts:
            attempt = _get(
                self.attempts, attempt_view.attempt_ref.record_id, "actual Attempt"
            )
            if attempt_view != AttemptDescriptor(
                attempt_ref=attempt_view.attempt_ref,
                execution_id=attempt.execution_id,
                owner=attempt.owner,
                attempt_number=attempt.attempt_number,
                status=attempt.status,
            ):
                raise ValueError(
                    "Coverage Attempt view differs from its actual Attempt"
                )
            if isinstance(attempt.owner, NodeAttemptOwner):
                self.check_attempt(
                    attempt.owner.node_run_ref.record_id, attempt.attempt_id
                )

    def check_attempt(
        self, node_id: RecordId, attempt_id: RecordId
    ) -> tuple[NodeRun, Attempt]:
        node = _get(self.nodes, node_id, "actual NodeRun")
        attempt = _get(self.attempts, attempt_id, "selected actual Attempt")
        if (
            (node.execution_id, attempt.execution_id)
            != (self.coverage.execution_id,) * 2
            or not isinstance(attempt.owner, NodeAttemptOwner)
            or attempt.owner.node_run_ref.record_id != node.node_run_id
            or not any(
                (link.attempt_ref.record_id, link.attempt_number)
                == (attempt_id, attempt.attempt_number)
                for link in node.attempt_history
            )
        ):
            raise ValueError(
                "selected Attempt must belong to the actual NodeRun history"
            )
        return node, attempt

    def check_scheduler_interruption(
        self, node: NodeRun, attempt: Attempt, expected: AttemptStatus, reason: str
    ) -> None:
        """Bind interruption to the final dispatched round and real ordered history."""
        if (
            node.status is not NodeRunStatus.FAILED
            or node.reason is None
            or node.reason.code != reason
            or node.reason != attempt.reason
            or attempt.status is not expected
            or node.output_refs
            or node.started_at is None
            or node.ended_at != attempt.ended_at
            or not node.attempt_history
            or node.attempt_history[-1].attempt_ref.record_id != attempt.attempt_id
        ):
            raise ValueError(
                "scheduler blocker requires its final started Attempt and matching NodeRun interruption"
            )
        previous_end = node.created_at
        for index, link in enumerate(node.attempt_history):
            _, actual = self.check_attempt(node.node_run_id, link.attempt_ref.record_id)
            if (
                actual.started_at is None
                or actual.ended_at is None
                or not previous_end
                <= actual.created_at
                <= actual.started_at
                <= actual.ended_at
                or (index == 0 and actual.started_at != node.started_at)
                or actual.status
                not in (
                    AttemptStatus.FAILED,
                    AttemptStatus.TIMED_OUT,
                    AttemptStatus.SUPERSEDED,
                )
                or (
                    actual.status is AttemptStatus.SUPERSEDED
                    and (actual.reason is None or actual.reason.code != "lease_expired")
                )
            ):
                raise ValueError(
                    "scheduler blocker requires complete ordered history of actual started Attempts"
                )
            previous_end = actual.ended_at

    def check_interruption_if_present(self, node: NodeRun) -> None:
        code = node.reason.code if node.reason is not None else None
        if not node.attempt_history:
            if code in ("lease_expired", "attempt_deadline_elapsed"):
                raise ValueError(
                    "scheduler blocker requires a recorded started Attempt"
                )
            return
        latest = self.attempts.get(node.attempt_history[-1].attempt_ref.record_id)
        if code == "lease_expired" or (
            latest is not None and latest.status is AttemptStatus.SUPERSEDED
        ):
            _, attempt = self.check_attempt(
                node.node_run_id, node.attempt_history[-1].attempt_ref.record_id
            )
            self.check_scheduler_interruption(
                node, attempt, AttemptStatus.SUPERSEDED, "lease_expired"
            )
        elif code == "attempt_deadline_elapsed":
            _, attempt = self.check_attempt(
                node.node_run_id, node.attempt_history[-1].attempt_ref.record_id
            )
            self.check_scheduler_interruption(
                node, attempt, AttemptStatus.TIMED_OUT, code
            )

    def check_envelope(self, envelope: NodeOutputEnvelope) -> None:
        node, attempt = self.check_attempt(envelope.node_run_id, envelope.attempt_id)
        expected_status = {
            NodeRunStatus.SUCCEEDED: (AttemptStatus.SUCCEEDED,),
            NodeRunStatus.FAILED: (AttemptStatus.FAILED, AttemptStatus.TIMED_OUT),
            NodeRunStatus.CANCELLED: (AttemptStatus.CANCELLED,),
        }[envelope.status]
        if (
            node.node_id is not envelope.node_id
            or node.status is not envelope.status
            or (attempt.status not in expected_status or node.trace != envelope.trace)
        ):
            raise ValueError(
                "selected node output must match actual NodeRun/Attempt outcome"
            )

    def check_discovery(self) -> None:
        output = self.sources.discovery_output
        inputs = self.sources.discovery_input
        selected = self.records.applicability
        if output is None:
            if (
                inputs is not None
                or self.coverage.discovery_outcome.value != "not_evaluated"
            ):
                raise ValueError(
                    "evaluated discovery requires its actual selected N02 output"
                )
            if selected is not None:
                if not selected.records.profile_eligibility.eligible_profiles:
                    raise ValueError(
                        "empty eligibility requires the explicit skipped N02 output"
                    )
                for node in self.nodes.values():
                    if node.node_id is not NodeId.N02:
                        continue
                    if node.status is NodeRunStatus.FAILED and (
                        node.reason is None
                        or node.reason.code
                        not in (
                            "lease_expired",
                            "attempt_deadline_elapsed",
                        )
                    ):
                        raise ValueError(
                            "failed N02 without output requires a recorded scheduler interruption"
                        )
                    self.check_interruption_if_present(node)
            return
        if (
            selected is None
            or output.execution_id != self.coverage.execution_id
            or (output.outcome != self.coverage.discovery_outcome.value)
        ):
            raise ValueError(
                "Coverage discovery outcome must match its selected N02 output"
            )
        gate = selected.records.profile_eligibility
        if isinstance(output, SkippedDiscoveryOutput):
            node = _get(self.nodes, output.node_run_id, "skipped N02 NodeRun")
            if (
                inputs is not None
                or node.node_id is not NodeId.N02
                or node.execution_id != gate.execution_id
                or node.status is not NodeRunStatus.SKIPPED
                or node.reason is None
                or node.reason.code != output.reason_code
            ):
                raise ValueError(
                    "empty eligibility requires its actual skipped N02 NodeRun"
                )
            output.check_gate(gate)
            return
        if inputs is None:
            raise ValueError("selected discovery Attempt requires its pinned N02 input")
        inputs.check_gate(gate)
        if inputs.context_snapshot_digest != selected.scope.context_snapshot_digest:
            raise ValueError("N02 input must pin the selected ContextSnapshot digest")
        self.check_envelope(output.envelope)
        if (
            output.model_invocation_refs
            != self.coverage.discovery_model_invocation_refs
            or tuple(hit.profile_ref for hit in output.profile_hits)
            != self.coverage.hit_profile_refs
        ):
            raise ValueError(
                "Coverage must retain selected N02 hit order and invocation refs"
            )
        invocations = _index(
            self.records.model_invocations, lambda item: item.model_invocation_id
        )
        output.check_records(
            inputs,
            self.sources.question_selections,
            tuple(
                _get(invocations, ref.record_id, "selected N02 ModelInvocation")
                for ref in output.model_invocation_refs
            ),
        )

    def check_grounding(self) -> tuple[_GroundingFailureWitness, ...]:
        failures: list[_GroundingFailureWitness] = []
        output = self.sources.discovery_output
        hits = (
            {item.profile_ref: item for item in output.profile_hits}
            if isinstance(output, DiscoveryAttemptOutput)
            else {}
        )
        expected = {
            item.profile_ref
            for item in self.coverage.profiles
            if item.grounding_outcome is not GroundingOutcome.NOT_EVALUATED
        }
        if self.grounding.keys() != expected:
            raise ValueError(
                "Coverage must resolve every evaluated Profile grounding to its selected record"
            )
        for profile in self.coverage.profiles:
            record = self.grounding.get(profile.profile_ref)
            if record is None:
                continue
            hit = _get(hits, profile.profile_ref, "selected N02 Profile hit")
            if (
                (record.outcome, record.object_refs)
                != (profile.grounding_outcome, profile.object_refs)
                or (record.question_selection_ref, record.effective_question_refs)
                != (hit.question_selection_ref, hit.effective_question_refs)
                or record.execution_id != self.coverage.execution_id
            ):
                raise ValueError(
                    "Coverage Profile outcome and Objects must match its actual GroundingRecord"
                )
            node, attempt = self.check_attempt(record.node_run_id, record.attempt_id)
            if node.node_id is not NodeId.N03 or (
                record.outcome is not GroundingOutcome.FAILED
                and attempt.status is not AttemptStatus.SUCCEEDED
            ):
                raise ValueError("GroundingRecord must retain its actual N03 Attempt")
            if not any(
                ref.record_kind is RuntimeRecordKind.GROUNDING_RECORD
                and ref.record_id == record.grounding_record_id
                for ref in node.output_refs
            ):
                raise ValueError(
                    "GroundingRecord must be retained by the actual N03 NodeRun"
                )
            record.check_objects(
                tuple(
                    _get(self.objects, ref.record_id, "grounded Object")
                    for ref in record.object_refs
                )
            )
            if (
                record.outcome is GroundingOutcome.FAILED
                and node.status is NodeRunStatus.SUCCEEDED
            ):
                if (
                    attempt.status is not AttemptStatus.SUCCEEDED
                    or node.attempt_history[-1].attempt_ref.record_id
                    != attempt.attempt_id
                    or not any(
                        other.node_run_id == node.node_run_id
                        and other.attempt_id == attempt.attempt_id
                        and other.outcome is not GroundingOutcome.FAILED
                        for other in self.grounding.values()
                    )
                ):
                    raise ValueError(
                        "mixed grounding failure must belong to the selected successful N03 Attempt with a usable sibling"
                    )
                failures.append(
                    _GroundingFailureWitness(
                        profile_ref=record.profile_ref,
                        grounding_ref=record.grounding_ref,
                        node_run_ref=NodeRunRef(
                            record_kind=RuntimeRecordKind.NODE_RUN,
                            record_id=node.node_run_id,
                        ),
                        attempt_ref=AttemptRef(
                            record_kind=RuntimeRecordKind.ATTEMPT,
                            record_id=attempt.attempt_id,
                        ),
                    )
                )
        return tuple(failures)

    def check_grounding_gaps(
        self, gaps: tuple[_GroundingInvocationGap, ...]
    ) -> tuple[_GroundingNotEvaluatedWitness, ...]:
        """Associate authenticated dispatch gaps to the selected usable N03 round."""
        output = self.sources.discovery_output
        hits = (
            {item.profile_ref for item in output.profile_hits}
            if isinstance(output, DiscoveryAttemptOutput)
            else set()
        )
        expected = {
            (entry.profile_ref, result.blocker_node_run_ref.record_id)
            for entry in self.coverage.mandatory_rule_coverage.obligations
            if isinstance((result := entry.outcome), DependencyNotEvaluatedObligation)
            and result.reason is CoverageReason.GROUNDING_NOT_EVALUATED
            and _get(
                self.nodes, result.blocker_node_run_ref.record_id, "actual N03 blocker"
            ).status
            is NodeRunStatus.SUCCEEDED
        }
        checked = _index(
            tuple(_GroundingInvocationGap.model_validate(gap) for gap in gaps),
            lambda item: (item.profile_ref, item.node_run_id),
        )
        if checked.keys() != expected or len(
            {item.dispatch_id for item in checked.values()}
        ) != len(checked):
            raise ValueError(
                "mixed grounding must resolve each unevaluated Profile to its exact dispatch witness"
            )
        witnesses = []
        for gap in checked.values():
            node, attempt = self.check_attempt(gap.node_run_id, gap.attempt_id)
            if (
                not isinstance(output, DiscoveryAttemptOutput)
                or gap.execution_id != self.coverage.execution_id
                or gap.profile_ref not in hits
                or gap.profile_ref in self.grounding
                or node.node_id is not NodeId.N03
                or node.status is not NodeRunStatus.SUCCEEDED
                or attempt.status is not AttemptStatus.SUCCEEDED
                or node.attempt_history[-1].attempt_ref.record_id != attempt.attempt_id
                or {(item.ref, item.digest) for item in node.upstream_inputs}
                != {
                    (item.ref, item.digest) for item in output.envelope.output_artifacts
                }
                or not any(
                    record.node_run_id == node.node_run_id
                    and record.attempt_id == attempt.attempt_id
                    and record.outcome is not GroundingOutcome.FAILED
                    for record in self.grounding.values()
                )
                or any(
                    row.model_invocation_id == gap.dispatch_id
                    or row.node_id == "N03"
                    and row.attempt_ref.record_id == attempt.attempt_id
                    and gap.profile_ref in row.profile_refs
                    for row in self.records.model_invocations
                )
            ):
                raise ValueError(
                    "mixed grounding witness must retain the original N02 hit and selected successful N03 Attempt without a qualified producer"
                )
            witnesses.append(
                _GroundingNotEvaluatedWitness(
                    profile_ref=gap.profile_ref,
                    node_run_ref=NodeRunRef(
                        record_kind=RuntimeRecordKind.NODE_RUN,
                        record_id=node.node_run_id,
                    ),
                    attempt_ref=AttemptRef(
                        record_kind=RuntimeRecordKind.ATTEMPT,
                        record_id=attempt.attempt_id,
                    ),
                    dispatch_id=gap.dispatch_id,
                )
            )
        return tuple(witnesses)

    def check_blockers(self) -> None:
        selected_discovery = self.sources.discovery_output
        scope = self.coverage.applicability_scope
        if isinstance(scope, BlockedCoverageScope):
            self.check_interruption_if_present(
                _get(
                    self.nodes,
                    scope.blocker_node_run_ref.record_id,
                    "actual N01 blocker",
                )
            )
        missing_grounding_nodes = {
            entry.outcome.blocker_node_run_ref
            for entry in self.coverage.mandatory_rule_coverage.obligations
            if isinstance(entry.outcome, DependencyNotEvaluatedObligation)
            and entry.outcome.reason is CoverageReason.GROUNDING_NOT_EVALUATED
        }
        for node_ref in missing_grounding_nodes:
            self.check_interruption_if_present(
                _get(self.nodes, node_ref.record_id, "actual N03 blocker")
            )
        for obligation in self.coverage.mandatory_rule_coverage.obligations:
            blocker = obligation.outcome
            if not isinstance(blocker, DependencyNotEvaluatedObligation):
                continue
            if (
                blocker.reason
                in (
                    CoverageReason.DISCOVERY_FAILED,
                    CoverageReason.DISCOVERY_NO_HITS,
                    CoverageReason.PROFILE_NOT_DISCOVERED,
                )
                and isinstance(selected_discovery, DiscoveryAttemptOutput)
                and (
                    blocker.blocker_node_run_ref.record_id
                    != selected_discovery.envelope.node_run_id
                )
            ):
                raise ValueError(
                    "discovery blocker must retain its selected N02 NodeRun"
                )
            if blocker.reason in (
                CoverageReason.GROUNDING_FAILED,
                CoverageReason.GROUNDING_NO_TARGET,
                CoverageReason.GROUNDING_PARTIAL,
            ):
                selected_grounding = _get(
                    self.grounding,
                    obligation.profile_ref,
                    "blocker's selected GroundingRecord",
                )
                if (
                    blocker.blocker_node_run_ref.record_id
                    != selected_grounding.node_run_id
                ):
                    raise ValueError(
                        "grounding blocker must retain its Profile's selected N03 NodeRun"
                    )
        expected = set()
        for entry in self.coverage.mandatory_rule_coverage.obligations:
            target, outcome = entry.target, entry.outcome
            if isinstance(outcome, ApplicableNotEvaluatedObligation) or (
                isinstance(outcome, DependencyNotEvaluatedObligation)
                and outcome.reason is CoverageReason.DECISION_NOT_EVALUATED
            ):
                if (
                    isinstance(target, ProfileObligationTarget)
                    or target.object_ref is None
                ):
                    raise ValueError("N06 blocker must identify a real Object scope")
                expected.add((target.object_ref, outcome.blocker_node_run_ref))
        actual = _index(
            self.sources.decision_blockers,
            lambda item: (item.object_ref, item.node_run_ref),
        )
        if actual.keys() != expected:
            raise ValueError(
                "resolve exactly the Object-scoped N06 blockers from Coverage"
            )
        for object_blocker in actual.values():
            node = _get(
                self.nodes,
                object_blocker.node_run_ref.record_id,
                "Object Decision blocker",
            )
            self.check_n06_object(node, object_blocker.object_ref)
            if object_blocker.attempt_ref is None:
                if node.attempt_history:
                    raise ValueError(
                        "dispatched N06 blocker must pin its selected Attempt"
                    )
            else:
                _, attempt = self.check_attempt(
                    node.node_run_id, object_blocker.attempt_ref.record_id
                )
                if object_blocker.attempt_ref != node.attempt_history[-1].attempt_ref:
                    raise ValueError("N06 blocker must pin the final actual Attempt")
                if attempt.status not in (
                    AttemptStatus.FAILED,
                    AttemptStatus.TIMED_OUT,
                    AttemptStatus.CANCELLED,
                    AttemptStatus.SUPERSEDED,
                ):
                    raise ValueError(
                        "N06 blocker must retain its actual interruption or failure Attempt"
                    )
            self.check_interruption_if_present(node)


def validate_decision_coverage_sources(
    coverage: ExecutionCoverage,
    records: DecisionCoverageRecords,
    sources: CoverageSources,
    *,
    authority: CoverageAuthorityInput | None = None,
    grounding_gaps: tuple[_GroundingInvocationGap, ...] = (),
) -> None:
    """Check real supplied records behind static Coverage views and guard proofs."""
    validated_coverage = ExecutionCoverage.model_validate(coverage)
    validated_records = DecisionCoverageRecords.model_validate(records)
    checker = _CoverageSourcesCheck(
        validated_coverage, validated_records, CoverageSources.model_validate(sources)
    )
    checker.check_views()
    checker.check_context()
    checker.check_n06_scope()
    checker.check_discovery()
    grounding_failures = checker.check_grounding()
    grounding_omissions = checker.check_grounding_gaps(grounding_gaps)
    checker.check_blockers()
    if validated_records.facts or validated_records.authoritative_selections:
        if authority is None:
            raise ValueError("Rule guard requires finite runtime authority inputs")
        if authority.records.execution != checker.sources.execution:
            raise ValueError("Rule guard authority must use this actual Execution")
        for obj in authority.records.objects:
            if checker.objects.get(obj.object_id) != obj:
                raise ValueError(
                    "Rule guard authority must use the same actual Object records"
                )
        validate_decision_guard_authority(
            validated_records.facts,
            validated_records.authoritative_selections,
            authority=authority,
        )
    _validate_decision_coverage(
        validated_coverage,
        validated_records,
        grounding_failures,
        omitted_grounding=grounding_omissions,
    )


def validate_decision_guard_authority(
    facts: tuple[FactRecord, ...],
    selections: tuple[AuthoritativeResultSelection, ...],
    *,
    authority: CoverageAuthorityInput,
) -> None:
    """Tie supplied guard Facts and selection records to the verified actual slice."""
    projection = validate_runtime_authority(
        authority.claims,
        authority.records,
        fact_schemas=authority.fact_schemas,
        parse_limits=authority.parse_limits,
        snapshot_limits=authority.snapshot_limits,
    )
    selected = {item.selection_id: item for item in projection.selected_revisions}
    actual_facts = {item.fact_id: item for item in authority.records.facts}
    for item in selections:
        if selected.get(item.selection_id) != item:
            raise ValueError(
                "Rule guard must use the verified selected runtime authority revision"
            )
    for fact in facts:
        if actual_facts.get(fact.fact_id) != fact:
            raise ValueError(
                "Rule guard Fact must be from the verified runtime authority input"
            )
