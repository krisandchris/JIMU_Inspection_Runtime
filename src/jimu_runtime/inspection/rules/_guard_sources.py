"""Resolve admitted definitions and the provenance of evidence for guard proofs."""

from collections import defaultdict
from collections.abc import Callable
from hashlib import sha256

from pydantic import TypeAdapter

from jimu_runtime.bundle.catalogue import ArtifactCatalogue
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import EvidenceType, FactScope
from jimu_runtime.domain.definition_support import reference_identity
from jimu_runtime.domain.definitions.capabilities import QuestionDefinition
from jimu_runtime.domain.definitions.tools import ToolDefinition, ToolPlanDefinition
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.fact_selectors import (
    ProducerSelector,
    QuestionProducerSelector,
    SystemProducerSelector,
    ToolPlanContentRef,
    ToolProducerSelector,
)
from jimu_runtime.domain.policies import EvidencePolicy, EvidencePolicyArtifact
from jimu_runtime.domain.references import ContentRef
from jimu_runtime.domain.runtime.authority import (
    AuthoritativeResultSelection,
    ToolStepSelection,
)
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    ObjectEvidenceScope,
    OriginalImageEvidence,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.executions import PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import (
    EvidenceRef,
    EvidenceRefs,
    FactRecord,
    KnownFact,
    QuestionFactSource,
    ToolFactSource,
    require_unique_runtime_refs,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import FinalizedAsset, MediaAsset
from jimu_runtime.domain.runtime.perception import ObjectRecord
from jimu_runtime.domain.runtime.tool_executions import (
    PartialToolResult,
    ReturnedToolAttempt,
    SucceededToolResult,
    ToolArtifactRefs,
    ToolExecution,
)
from jimu_runtime.domain.tool_mappings import StepOutputInput
from jimu_runtime.domain.validation.authority_inputs import (
    AuthorityQuestionInput,
    AuthorityToolPlanInput,
)
from jimu_runtime.domain.validation.coverage_sources import CoverageAuthorityInput
from jimu_runtime.domain.validation.definition_inputs import ProfileRegistration
from jimu_runtime.domain.validation.runtime_authority import RuntimeAuthorityRecords
from jimu_runtime.domain.validation.tool_plans import ToolRegistration
from jimu_runtime.domain.values import (
    EnumValue,
    IntegerValue,
    RecordValue,
    StringValue,
    Value,
)

_EVIDENCE_REFS = TypeAdapter[EvidenceRefs](EvidenceRefs)


def check_bundle_snapshot(
    catalogue: ArtifactCatalogue, authority: CoverageAuthorityInput
) -> None:
    """Keep every authority registration tied to the admitted Bundle's definitions."""
    manifest = authority.records.manifest
    if tuple(entry.inventory for entry in catalogue.artifacts) != manifest.artifacts:
        raise ValueError("guard catalogue differs from the admitted Execution Manifest")
    history = authority.claims.history
    for item in authority.claims.producer_catalogues:
        if (
            catalogue.resolve(item.registration.ref).value
            != item.registration.definition
        ):
            raise ValueError(
                "authority catalogue differs from its pinned Bundle artifact"
            )
        definitions: tuple[
            ProfileRegistration
            | AuthorityQuestionInput
            | AuthorityToolPlanInput
            | ToolRegistration,
            ...,
        ] = (
            *item.context.profiles,
            *item.context.questions,
            *item.context.plans,
            *item.context.tools,
            *history.questions,
            *history.plans,
            *history.tools,
        )
        for registration in definitions:
            if catalogue.resolve(registration.ref).value != registration.definition:
                raise ValueError(
                    "authority definition differs from its pinned Bundle artifact"
                )
        for policy in item.context.policies:
            if catalogue.resolve(policy.ref).value != policy.artifact:
                raise ValueError(
                    "authority policy differs from its pinned Bundle artifact"
                )


def check_input_pin(
    pins: dict[tuple[RuntimeRecordKind, str], PinnedRuntimeInput],
    kind: RuntimeRecordKind,
    record_id: str,
    record: ContractModel,
) -> None:
    pin = pins.get((kind, record_id))
    if (
        pin is not None
        and pin.digest
        != sha256(canonical_bytes(record.model_dump(mode="json"))).hexdigest()
    ):
        raise ValueError("N06 input pin differs from the actual runtime record digest")


def producer_for_fact(fact: FactRecord) -> ProducerSelector:
    source = fact.source
    if isinstance(source, QuestionFactSource):
        return QuestionProducerSelector(
            producer_kind=source.source_kind,
            question_ref=source.question_ref,
            output_name="primary",
        )
    if isinstance(source, ToolFactSource):
        return ToolProducerSelector(
            producer_kind=source.source_kind,
            tool_plan_ref=source.tool_plan_ref,
            step_id=source.step_id,
            tool_ref=source.tool_ref,
            output_name=source.output_name,
        )
    return SystemProducerSelector(
        producer_kind=source.source_kind,
        requirement=source.requirement,
        output_name=source.output_name,
    )


def validate_evidence_pins(
    refs: tuple[EvidenceRef, ...],
    rows: tuple[EvidenceAsset, ...],
    pins: dict[tuple[RuntimeRecordKind, str], PinnedRuntimeInput],
) -> dict[EvidenceRef, EvidenceAsset]:
    refs = _EVIDENCE_REFS.validate_python(refs)
    require_unique_runtime_refs(refs)
    if len(rows) > 1024:
        raise ValueError("N06 exceeds the Evidence record limit")
    if any((RuntimeRecordKind.EVIDENCE, ref.record_id) not in pins for ref in refs):
        raise ValueError("N06 Evidence reference requires a metadata digest pin")
    allowed = set(refs)
    result = {}
    for raw in rows:
        asset = EvidenceAsset.model_validate(raw)
        ref = EvidenceRef(
            record_kind=RuntimeRecordKind.EVIDENCE, record_id=asset.evidence_id
        )
        if ref not in allowed:
            raise ValueError("Evidence record was not pinned in the exact N06 input")
        if ref in result:
            raise ValueError("duplicate N06 Evidence record")
        check_input_pin(pins, RuntimeRecordKind.EVIDENCE, ref.record_id, asset)
        result[ref] = asset
    return result


def _field(value: Value | None, path: tuple[str, ...]) -> Value | None:
    for name in path:
        if not isinstance(value, RecordValue) or name not in value.value:
            return None
        value = value.value[name]
    return value


def _content_link(links: ToolArtifactRefs, asset: EvidenceAsset) -> bool:
    return any(
        link.artifact_ref.record_kind is RuntimeRecordKind.EVIDENCE
        and link.artifact_ref.record_id == asset.evidence_id
        and link.digest == asset.sha256
        for link in links
    )


def _named_refs(
    result: SucceededToolResult | PartialToolResult,
    names: set[str],
) -> frozenset[EvidenceRef]:
    return frozenset(
        ref
        for slot in result.evidence
        if slot.name in names and len(slot.evidence_refs) == 1
        for ref in slot.evidence_refs
    )


class EvidenceSources:
    def __init__(
        self,
        *,
        catalogue: ArtifactCatalogue,
        task: AcceptedTask,
        media: MediaAsset,
        obj: ObjectRecord,
        records: RuntimeAuthorityRecords,
        selected: tuple[tuple[FactRecord, AuthoritativeResultSelection], ...],
        evidence: dict[EvidenceRef, EvidenceAsset],
        read_evidence: Callable[[EvidenceAsset, int], bytes | None],
        max_evidence_bytes: int,
    ) -> None:
        if type(max_evidence_bytes) is not int or max_evidence_bytes <= 0:
            raise ValueError("N06 Evidence byte budget must be a positive integer")
        self._catalogue = catalogue
        self._media = media
        self._obj = obj
        self._read = read_evidence
        self._max_evidence_bytes = max_evidence_bytes
        self._by_ref = evidence
        self._by_type: dict[EvidenceType, list[EvidenceRef]] = defaultdict(list)
        self._policies: dict[EvidenceRef, EvidencePolicy] = {}
        self._available: dict[EvidenceRef, bool] = {}
        self._fact_evidence: dict[str, frozenset[EvidenceRef]] = {}
        self._mask_groups: dict[str, frozenset[EvidenceRef] | None] = {}
        self._children: dict[tuple[ToolPlanContentRef, str, str], KnownFact] = {}
        for fact, head in selected:
            if not (
                isinstance(fact, KnownFact)
                and isinstance(fact.source, ToolFactSource)
                and isinstance(head, ToolStepSelection)
            ):
                continue
            key = (
                fact.source.tool_plan_ref,
                fact.source.step_id,
                fact.source.output_name,
            )
            if key in self._children:
                raise ValueError("multiple selected Facts claim one Tool child output")
            self._children[key] = fact
        self._tools = {item.tool_execution_id: item for item in records.tool_executions}
        self._invocations = {
            item.model_invocation_id: item for item in records.model_invocations
        }
        for ref, asset in sorted(
            evidence.items(), key=lambda pair: (pair[1].byte_size, pair[0].record_id)
        ):
            if (
                asset.scope.execution_id != obj.execution_id
                or (
                    isinstance(asset.scope, ObjectEvidenceScope)
                    and (asset.scope.object_ref.record_id, asset.scope.profile_ref)
                    != (obj.object_id, obj.profile_ref)
                )
                or asset.access_scope.project_id != task.idempotency_scope.project_id
                or (
                    isinstance(asset.access_scope, TaskEvidenceAccess)
                    and asset.access_scope.task_id != task.task_id
                )
            ):
                raise ValueError(
                    "N06 Evidence belongs to another Task, Project, Execution or Object/Profile"
                )
            self._by_type[asset.evidence_type].append(ref)

    def _source_policy(self, ref: EvidenceRef, asset: EvidenceAsset) -> EvidencePolicy:
        policy = self._policies.get(ref)
        if policy is None:
            policy = require_definition(
                self._catalogue,
                asset.retention.evidence_policy_ref,
                EvidencePolicyArtifact,
            ).policy
            self._policies[ref] = policy
        return policy

    def _authorized_producer_refs(self, fact: KnownFact) -> frozenset[EvidenceRef]:
        source = fact.source
        direct = set(fact.evidence_refs) & self._by_ref.keys()
        inherited: frozenset[EvidenceRef] = frozenset()
        producer_ids = {(fact.producer_id, fact.producer_version)}
        source_policy_ref: ContentRef | None = None
        if isinstance(source, QuestionFactSource):
            question = require_definition(
                self._catalogue, source.question_ref, QuestionDefinition
            )
            source_policy_ref = question.evidence_policy_ref
            invocation = self._invocations[source.model_invocation_ref.record_id]
            binding = invocation.configured_provider.binding_ref
            producer_ids.add((binding.artifact_id, binding.version))
            invocation_refs = set(invocation.evidence_refs)
            direct = {
                ref
                for ref in direct
                if self._by_ref[ref].evidence_type
                not in (EvidenceType.MODEL_REQUEST, EvidenceType.MODEL_RESPONSE)
                or ref in invocation_refs
            }
        elif isinstance(source, ToolFactSource):
            tool = self._tools[source.tool_execution_ref.record_id]
            definition = require_definition(
                self._catalogue, source.tool_ref, ToolDefinition
            )
            source_policy_ref = definition.evidence_policy_ref
            returned = tool.attempt_result
            if not isinstance(returned, ReturnedToolAttempt) or not isinstance(
                result := returned.result, (SucceededToolResult, PartialToolResult)
            ):
                return frozenset()
            if tool.actual_implementation_version is not None:
                producer_ids.add((fact.producer_id, tool.actual_implementation_version))
            output_names = {
                path[0]
                for output in definition.fact_outputs
                if output.output_name == source.output_name
                for path in (output.evidence_paths or ())
            }
            inherited, reserved = self._upstream_masks(
                fact, source, tool, result, output_names
            )
            named = {
                ref
                for entry in result.evidence
                if entry.name in output_names
                for ref in entry.evidence_refs
            }
            named.update(
                ref
                for state in result.output_states
                if state.output_name == source.output_name
                for ref in state.evidence_refs
            )
            direct.intersection_update(named, tool.evidence_refs)
            direct.difference_update(reserved)
        return inherited | frozenset(
            ref
            for ref in direct
            if (
                isinstance(self._by_ref[ref].scope, ObjectEvidenceScope)
                == (fact.scope is FactScope.OBJECT)
                and (self._by_ref[ref].producer_id, self._by_ref[ref].producer_version)
                in producer_ids
                and (
                    source_policy_ref is None
                    or reference_identity(
                        self._by_ref[ref].retention.evidence_policy_ref
                    )
                    == reference_identity(source_policy_ref)
                )
            )
        )

    def _upstream_masks(
        self,
        fact: KnownFact,
        source: ToolFactSource,
        tool: ToolExecution,
        result: SucceededToolResult | PartialToolResult,
        output_names: set[str],
    ) -> tuple[frozenset[EvidenceRef], frozenset[EvidenceRef]]:
        plan = require_definition(
            self._catalogue, source.tool_plan_ref, ToolPlanDefinition
        )
        steps = {item.step_id: item for item in plan.steps}
        step = steps[source.step_id]
        roles = {
            binding.target.field_path[-1]: binding.source
            for binding in step.input_bindings
            if isinstance(binding.source, StepOutputInput)
            and binding.target.field_path
            and binding.target.field_path[-1] in output_names
        }
        if not roles:
            return frozenset(), frozenset()
        # Reused sidecars must keep the selected producer child and the named
        # input role. Content SHA links alone do not identify a child or role.
        inherited: set[EvidenceRef] = set()
        slots = {entry.name: entry.evidence_refs for entry in result.evidence}
        reserved = frozenset(ref for name in roles for ref in slots.get(name, ()))
        for name, edge in roles.items():
            refs = slots.get(name, ())
            if len(refs) != 1:
                continue
            (ref,) = refs
            asset = self._by_ref.get(ref)
            if (
                asset is None
                or asset.evidence_type is not EvidenceType.MASK
                or ref not in fact.evidence_refs
                or ref not in tool.evidence_refs
                or not _content_link(tool.input_refs, asset)
            ):
                continue
            previous = steps[edge.step_id]
            definition = require_definition(
                self._catalogue, previous.tool_ref, ToolDefinition
            )
            if edge.selector.schema_ref != definition.output_schema_ref:
                raise ValueError("reused Evidence has a different parent Tool Schema")
            # The approved ToolPlan maps a single exact parent value to the
            # role. Ambiguous mappings cannot establish input provenance.
            declarations = tuple(
                output
                for output in definition.fact_outputs
                if output.value_path == edge.selector.field_path
                and any(
                    mapping.output_name == output.output_name
                    and mapping.target_fact_key == output.fact_key
                    for mapping in previous.fact_output_mappings
                )
            )
            if len(declarations) != 1:
                continue
            (declaration,) = declarations
            child = self._children.get(
                (source.tool_plan_ref, edge.step_id, declaration.output_name)
            )
            if child is None or not isinstance(child.source, ToolFactSource):
                continue
            child_tool = self._tools[child.source.tool_execution_ref.record_id]
            returned = child_tool.attempt_result
            if (
                child.source.tool_ref != previous.tool_ref
                or (child.fact_key, child.fact_schema_ref)
                != (declaration.fact_key, declaration.fact_schema_ref)
                or child_tool.status != "succeeded"
                or not isinstance(returned, ReturnedToolAttempt)
                or not isinstance(parent_result := returned.result, SucceededToolResult)
                or child_tool.completed_at > tool.started_at
                or child.value != _field(parent_result.payload, declaration.value_path)
                or ref not in child.evidence_refs
                or ref not in child_tool.evidence_refs
                or ref
                not in _named_refs(
                    parent_result,
                    {path[0] for path in declaration.evidence_paths or ()},
                )
                or not _content_link(child_tool.output_refs, asset)
                or (asset.producer_id, asset.producer_version)
                != (
                    child_tool.tool_ref.artifact_id,
                    child_tool.actual_implementation_version,
                )
                or reference_identity(asset.retention.evidence_policy_ref)
                != reference_identity(definition.evidence_policy_ref)
                or not isinstance(asset.scope, ObjectEvidenceScope)
            ):
                continue
            spatial = asset.spatial
            if (
                asset.mime_type != "image/png"
                or not isinstance(spatial, DerivedImageEvidence)
                or len(spatial.transforms) != 1
            ):
                continue
            (transform,) = spatial.transforms
            coordinates = self._obj.coordinate_system
            expected_fields: dict[tuple[str, ...], str | int] = {
                ("evidence_id",): asset.evidence_id,
                ("sha256",): asset.sha256,
                ("source_media_sha256",): self._media.media_ref.sha256,
                (
                    "coordinate_system",
                    "coordinate_system_id",
                ): coordinates.coordinate_system_id,
                ("coordinate_system", "frame"): coordinates.frame,
                (
                    "coordinate_system",
                    "dimensions",
                    "width",
                ): coordinates.dimensions.width,
                (
                    "coordinate_system",
                    "dimensions",
                    "height",
                ): coordinates.dimensions.height,
            }
            if (
                transform.source == coordinates
                and transform.target.dimensions == coordinates.dimensions
                and transform.affine == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
                and (transform.implementation_id, transform.implementation_version)
                == (
                    definition.implementation_requirement.capability_id,
                    child_tool.actual_implementation_version,
                )
                and all(
                    isinstance(
                        value := _field(child.value, path),
                        (StringValue, EnumValue, IntegerValue),
                    )
                    and value.value == expected
                    for path, expected in expected_fields.items()
                )
            ):
                inherited.add(ref)
        self._mask_groups[fact.fact_id] = (
            frozenset(inherited) if len(inherited) == len(roles) else None
        )
        return frozenset(inherited), reserved

    def _linked_refs(self, fact: KnownFact) -> frozenset[EvidenceRef]:
        refs = self._fact_evidence.get(fact.fact_id)
        if refs is None:
            refs = self._authorized_producer_refs(fact)
            self._fact_evidence[fact.fact_id] = refs
        return refs

    def _qualifies(
        self, ref: EvidenceRef, policy: EvidencePolicy, linked: set[EvidenceRef]
    ) -> bool:
        asset = self._by_ref[ref]
        if not isinstance(asset.stage, FinalizedAsset):
            return False
        original = asset.evidence_type is EvidenceType.ORIGINAL_IMAGE
        if not original and ref not in linked:
            return False
        produced = self._source_policy(ref, asset)
        if (
            asset.evidence_type not in produced.allowed_evidence_types
            or asset.retention.retention_class != produced.retention_class
            or asset.retention.retention_class != policy.retention_class
            or produced.retention_duration_seconds < policy.retention_duration_seconds
            or asset.access_scope.scope != produced.access_scope
            or (policy.access_scope == "task" and asset.access_scope.scope != "task")
        ):
            return False
        spatial = asset.spatial
        if isinstance(spatial, OriginalImageEvidence | DerivedImageEvidence):
            asset.validate_original_media(self._media)
            root = (
                spatial.coordinate_system
                if isinstance(spatial, OriginalImageEvidence)
                else spatial.transforms[0].source
            )
            if root != self._obj.coordinate_system:
                raise ValueError(
                    "N06 spatial Evidence has a different original coordinate frame"
                )
        return True

    def _readable(self, ref: EvidenceRef) -> bool:
        if ref not in self._available:
            asset = self._by_ref[ref]
            try:
                data = self._read(asset, asset.byte_size)
            except OSError:
                data = None
            self._available[ref] = (
                type(data) is bytes
                and len(data) == asset.byte_size
                and sha256(data).hexdigest() == asset.sha256
            )
        return self._available[ref]

    def required(
        self, policy: EvidencePolicy, facts: tuple[KnownFact, ...]
    ) -> tuple[tuple[EvidenceAsset, ...], bool]:
        types = set(policy.required_evidence_types)
        if policy.original_media_required:
            types.add(EvidenceType.ORIGINAL_IMAGE)
        linked: set[EvidenceRef] = set()
        if types - {EvidenceType.ORIGINAL_IMAGE}:
            for fact in facts:
                linked.update(self._linked_refs(fact))
        mask_groups = tuple(
            self._mask_groups[fact.fact_id]
            for fact in facts
            if fact.fact_id in self._mask_groups
        )
        resolved_masks = None
        if mask_groups and all(group is not None for group in mask_groups):
            resolved_masks = frozenset(
                ref for group in mask_groups if group is not None for ref in group
            )
        selected: list[EvidenceAsset] = []
        remaining_bytes = self._max_evidence_bytes
        missing = False
        for kind in sorted(types, key=lambda item: item.value):
            candidates = self._by_type.get(kind, ())
            options: tuple[tuple[EvidenceRef, ...], ...]
            if kind is EvidenceType.MASK and mask_groups:
                options = (
                    ()
                    if resolved_masks is None
                    else (tuple(ref for ref in candidates if ref in resolved_masks),)
                )
            else:
                options = tuple((ref,) for ref in candidates)
            for option in options:
                if not option or any(
                    self._available.get(ref) is False
                    or not self._qualifies(ref, policy, linked)
                    for ref in option
                ):
                    continue
                size = sum(self._by_ref[ref].byte_size for ref in option)
                if size > remaining_bytes:
                    continue
                for ref in option:
                    remaining_bytes -= self._by_ref[ref].byte_size
                    if not self._readable(ref):
                        break
                else:
                    selected.extend(self._by_ref[ref] for ref in option)
                    break
            else:
                missing = True
        return tuple(selected), missing
