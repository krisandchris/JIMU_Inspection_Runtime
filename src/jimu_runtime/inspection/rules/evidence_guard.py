"""Build frozen guard proofs from a single fenced N06 input snapshot."""

from collections.abc import Callable
from dataclasses import dataclass

from jimu_runtime.bundle.catalogue import ArtifactCatalogue
from jimu_runtime.bundle.semantic_inputs import (
    build_fact_schema_registry,
    require_definition,
)
from jimu_runtime.domain.definition_enums import FactScope
from jimu_runtime.domain.definitions.capabilities import ProfileDefinition
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.fact_selectors import (
    FactSchemaContentRef,
    FactSelector,
    ProducerSelector,
)
from jimu_runtime.domain.nodes.decision import DecisionInput
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.primitives import ArtifactId
from jimu_runtime.domain.rules.definitions import RuleDefinition, RuleSetDefinition
from jimu_runtime.domain.runtime.authority import (
    AuthoritativeResultSelection,
    ObservationBatchSelection,
    SystemOutputSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.decisions import (
    BlockedRequiredEvidenceGuard,
    CompleteRequiredEvidenceGuard,
    RequiredEvidenceGuard,
    RequiredEvidenceReason,
    RequiredFactProof,
    RulePath,
)
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.facts import (
    AuthoritySelectionRef,
    EvidenceRef,
    FactRecord,
    FactRef,
    KnownFact,
    QuestionFactSource,
    SystemFactSource,
    ToolFactSource,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import MediaAsset
from jimu_runtime.domain.validation.coverage_sources import CoverageAuthorityInput
from jimu_runtime.domain.validation.runtime_authority import validate_runtime_authority
from jimu_runtime.domain.values import RecordValue
from jimu_runtime.inspection.rules._guard_sources import (
    EvidenceSources,
    check_bundle_snapshot,
    check_input_pin,
    producer_for_fact,
    validate_evidence_pins,
)

type _FactKey = tuple[FactScope, ArtifactId, FactSchemaContentRef, ProducerSelector]


def _key(selector: FactSelector) -> _FactKey:
    return (
        selector.scope,
        selector.fact_key,
        selector.fact_schema_ref,
        selector.producer_selector,
    )


def _result_matches(fact: FactRecord, head: AuthoritativeResultSelection) -> bool:
    if (
        fact.execution_id,
        fact.scope,
        fact.object_id,
        fact.profile_ref,
        fact.attempt_id,
    ) != (
        head.execution_id,
        head.scope,
        head.object_id,
        head.profile_ref,
        head.selected_attempt_id,
    ):
        return False
    source = fact.source
    if isinstance(source, QuestionFactSource) and isinstance(
        head, ObservationBatchSelection
    ):
        return source.observation_batch_ref == head.selected_result_ref
    if isinstance(source, ToolFactSource) and isinstance(head, ToolStepSelection):
        return (source.tool_execution_ref, source.tool_plan_ref, source.step_id) == (
            head.selected_result_ref,
            head.tool_plan_ref,
            head.step_id,
        )
    if isinstance(source, SystemFactSource) and isinstance(head, SystemOutputSelection):
        return (source.system_output_ref, source.requirement, source.output_name) == (
            head.selected_result_ref,
            head.requirement,
            head.output_name,
        )
    return False


def _known_field(fact: FactRecord, selector: FactSelector) -> bool:
    if not isinstance(fact, KnownFact):
        return False
    value = fact.value
    for field in selector.field_path:
        if not isinstance(value, RecordValue) or field not in value.value:
            return False
        value = value.value[field]
    return True


@dataclass(frozen=True, slots=True)
class RuleGuardResult:
    guard: RequiredEvidenceGuard
    optional_fact_proofs: tuple[RequiredFactProof, ...]
    predicate_facts: tuple[FactRecord, ...]
    facts: tuple[FactRecord, ...]
    selections: tuple[AuthoritativeResultSelection, ...]
    evidence: tuple[EvidenceAsset, ...]

    @property
    def fact_refs(self) -> tuple[FactRef, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.guard.required_fact_refs,
                    *(proof.fact_ref for proof in self.optional_fact_proofs),
                )
            )
        )


class RequiredEvidenceGuardBuilder:
    """Verify one Object's N06 input once, then build guards for reached Rules.

    The caller supplies the admitted catalogue, scoped durable rows and exact N06
    evidence pins from a single fenced read. The finite authority validator checks
    every supplied history row, while storage and fence ownership stay with the
    caller. The byte reader must use the current task scope and exact finalized
    storage version, returning at most the requested number of bytes or None.
    The byte budget applies to each Rule; availability reads are cached for the
    lifetime of this fixed snapshot.
    """

    def __init__(
        self,
        *,
        catalogue: ArtifactCatalogue,
        authority: CoverageAuthorityInput,
        inputs: DecisionInput,
        task: AcceptedTask,
        media: MediaAsset,
        evidence_refs: tuple[EvidenceRef, ...],
        evidence: tuple[EvidenceAsset, ...],
        read_evidence: Callable[[EvidenceAsset, int], bytes | None],
        max_evidence_bytes: int,
    ) -> None:
        records = authority.records
        inputs = DecisionInput.model_validate(inputs)
        task = AcceptedTask.model_validate(task)
        media = MediaAsset.model_validate(media)
        task.validate_media(media)
        inputs.check_gate(records.eligibility)
        execution = records.execution
        envelope = inputs.envelope
        if (
            (task.task_id, task.trace, task.release_id, task.bundle_digest)
            != (
                execution.task_id,
                execution.trace,
                execution.pins.release_id,
                execution.pins.bundle_digest,
            )
            or (envelope.task_id, envelope.execution_id, envelope.trace)
            != (task.task_id, execution.execution_id, execution.trace)
            or inputs.object_record not in records.objects
        ):
            raise ValueError(
                "N06 input must pin the exact accepted Task, Execution and Object"
            )
        catalogue.resolve(envelope.node_config_ref)
        check_bundle_snapshot(catalogue, authority)
        projection = validate_runtime_authority(
            authority.claims,
            records,
            fact_schemas=build_fact_schema_registry(catalogue),
            parse_limits=authority.parse_limits,
            snapshot_limits=authority.snapshot_limits,
        )
        self._catalogue = catalogue
        self._profile = require_definition(
            catalogue, inputs.object_record.profile_ref, ProfileDefinition
        )
        obj = inputs.object_record
        if (obj.source_media_ref, obj.coordinate_system.dimensions) != (
            media.media_ref,
            media.dimensions,
        ):
            raise ValueError(
                "N06 Object must retain the exact Task original media and dimensions"
            )
        kind = RuntimeRecordKind
        pins = {
            (pin.ref.record_kind, pin.ref.record_id): pin
            for pin in envelope.upstream_artifacts
        }
        check_input_pin(pins, kind.OBJECT, obj.object_id, obj)
        scope_keys = {(None, None), (obj.object_id, obj.profile_ref)}
        scoped_facts = {ref.record_id for ref in inputs.fact_refs}
        all_selections = {ref.record_id for ref in inputs.authority_selection_refs}
        pinned_facts = {
            fact.fact_id: fact for fact in records.facts if fact.fact_id in scoped_facts
        }
        selections = tuple(
            record
            for record in records.authority_selections
            if record.selection_id in all_selections
        )
        for record in (*pinned_facts.values(), *selections):
            if (record.object_id, record.profile_ref) not in scope_keys:
                raise ValueError(
                    "N06 input cites Fact or selection from another Object/Profile"
                )
        for fact in pinned_facts.values():
            check_input_pin(pins, kind.FACT, fact.fact_id, fact)
        for selection in selections:
            check_input_pin(
                pins,
                kind.AUTHORITATIVE_RESULT_SELECTION,
                selection.selection_id,
                selection,
            )
        # Facts may exist without a qualifying head, and an explicitly pinned
        # older revision cannot stand in for the current selected head.
        self._candidate_keys: set[_FactKey] = set()
        self._selected: dict[
            _FactKey, tuple[FactRecord, AuthoritativeResultSelection]
        ] = {}
        fact_keys = {}
        for fact in pinned_facts.values():
            key = (
                fact.scope,
                fact.fact_key,
                fact.fact_schema_ref,
                producer_for_fact(fact),
            )
            self._candidate_keys.add(key)
            fact_keys[fact.fact_id] = key
        for head in projection.selected_revisions:
            if head.selection_id not in all_selections:
                continue
            for fact_ref in head.selected_fact_refs:
                selected_fact = pinned_facts.get(fact_ref.record_id)
                if selected_fact is None:
                    continue
                if not _result_matches(selected_fact, head):
                    raise ValueError(
                        "selected Fact differs from its exact producer result and Attempt"
                    )
                key = fact_keys[selected_fact.fact_id]
                if key in self._selected:
                    raise ValueError(
                        "multiple selected Facts occupy one Rule operand slot"
                    )
                self._selected[key] = (selected_fact, head)
        rows = validate_evidence_pins(evidence_refs, evidence, pins)
        self._evidence = EvidenceSources(
            catalogue=catalogue,
            task=task,
            media=media,
            obj=obj,
            records=records,
            selected=tuple(self._selected.values()),
            evidence=rows,
            read_evidence=read_evidence,
            max_evidence_bytes=max_evidence_bytes,
        )

    def build(self, path: RulePath) -> RuleGuardResult:
        path = RulePath.model_validate(path)
        if path.rule_set_ref not in self._profile.rule_set_refs:
            raise ValueError("RuleSet is outside the exact Object Profile")
        rule_set = require_definition(
            self._catalogue, path.rule_set_ref, RuleSetDefinition
        )
        if path.rule_ref not in rule_set.rule_refs:
            raise ValueError("Rule is outside the exact selected RuleSet")
        rule = require_definition(self._catalogue, path.rule_ref, RuleDefinition)
        policy = require_definition(
            self._catalogue, rule.evidence_policy_ref, EvidencePolicyArtifact
        ).policy
        required: list[RequiredFactProof] = []
        optional: list[RequiredFactProof] = []
        reasons: set[RequiredEvidenceReason] = set()
        operands: dict[str, FactRecord] = {}
        evidence_facts: dict[str, KnownFact] = {}
        proved_facts: dict[str, FactRecord] = {}
        proved_selections: dict[str, AuthoritativeResultSelection] = {}
        for selectors, proofs, is_required in (
            (rule.required_fact_selectors, required, True),
            (rule.optional_fact_selectors, optional, False),
        ):
            for selector in selectors:
                key = _key(selector)
                selected = self._selected.get(key)
                if selected is None:
                    if is_required:
                        reasons.add(
                            RequiredEvidenceReason.PRODUCER_AUTHORITY_UNRESOLVED
                            if key in self._candidate_keys
                            else RequiredEvidenceReason.REQUIRED_FACT_UNAVAILABLE
                        )
                    continue
                fact, head = selected
                operands[fact.fact_id] = fact
                field_known = _known_field(fact, selector)
                if isinstance(fact, KnownFact) and field_known:
                    evidence_facts[fact.fact_id] = fact
                if is_required and not field_known:
                    reasons.add(RequiredEvidenceReason.REQUIRED_FACT_UNAVAILABLE)
                    continue
                proved_facts[fact.fact_id] = fact
                proved_selections[head.selection_id] = head
                proofs.append(
                    RequiredFactProof(
                        selector=selector,
                        fact_ref=FactRef(
                            record_kind=RuntimeRecordKind.FACT, record_id=fact.fact_id
                        ),
                        authority_selection_ref=AuthoritySelectionRef(
                            record_kind=RuntimeRecordKind.AUTHORITATIVE_RESULT_SELECTION,
                            record_id=head.selection_id,
                        ),
                    )
                )
        evidence, missing = self._evidence.required(
            policy, tuple(evidence_facts.values())
        )
        if missing:
            reasons.add(RequiredEvidenceReason.EVIDENCE_UNAVAILABLE)
        fact_refs = tuple(dict.fromkeys(proof.fact_ref for proof in required))
        selection_refs = tuple(
            dict.fromkeys(proof.authority_selection_ref for proof in required)
        )
        evidence_refs = tuple(
            EvidenceRef(
                record_kind=RuntimeRecordKind.EVIDENCE, record_id=asset.evidence_id
            )
            for asset in evidence
        )
        guard: RequiredEvidenceGuard
        if reasons:
            guard = BlockedRequiredEvidenceGuard(
                outcome="blocked",
                required_fact_refs=fact_refs,
                required_evidence_refs=evidence_refs,
                authoritative_result_selection_refs=selection_refs,
                fact_proofs=tuple(required),
                reasons=tuple(sorted(reasons, key=lambda reason: reason.value)),
            )
        else:
            guard = CompleteRequiredEvidenceGuard(
                outcome="complete",
                required_fact_refs=fact_refs,
                required_evidence_refs=evidence_refs,
                authoritative_result_selection_refs=selection_refs,
                fact_proofs=tuple(required),
            )
        return RuleGuardResult(
            guard,
            tuple(optional),
            tuple(operands.values()),
            tuple(proved_facts.values()),
            tuple(proved_selections.values()),
            evidence,
        )
