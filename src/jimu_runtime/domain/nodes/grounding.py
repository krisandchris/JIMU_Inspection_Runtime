"""N03 grounds each accepted Profile hit independently."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definition_support import (
    immutable_sequence,
    require_unique_references,
)
from jimu_runtime.domain.definitions.capabilities import (
    GroundingBinding,
    ModelBindingContentRef,
    ModelGroundingBinding,
    ProfileDefinition,
)
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.nodes.discovery import DiscoveryAttemptOutput, ProfileHit
from jimu_runtime.domain.nodes.envelopes import (
    NodeError,
    NodeInputEnvelope,
    NodeOutputEnvelope,
    check_envelope_pair,
)
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import ProfileContentRef
from jimu_runtime.domain.runtime.grounding_groups import observation_groups
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import CoordinateSystem, MediaRef
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    GroundingRecord,
    ModelGroundingProvenance,
    ObjectRecord,
    ProfileGroundingResult,
)
from jimu_runtime.domain.runtime.selection import QuestionSelectionRecord
from jimu_runtime.domain.runtime.tool_executions import (
    GroundingBindingOrigin,
    ToolExecution,
)


class GroundingProfileInput(ContractModel):
    hit: ProfileHit
    grounding_binding: GroundingBinding


class GroundingInput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeInputEnvelope
    original_media_ref: MediaRef
    original_coordinate_system: CoordinateSystem
    profiles: Annotated[
        tuple[GroundingProfileInput, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_profiles_unique(self) -> Self:
        if (
            self.envelope.node_id is not NodeId.N03
            or self.original_coordinate_system.frame != "original_image"
        ):
            raise ValueError("N03 requires an invocation over an original image")
        require_unique_references(tuple(item.hit.profile_ref for item in self.profiles))
        return self

    def check_discovery(self, discovery: DiscoveryAttemptOutput) -> None:
        if (
            discovery.execution_id != self.envelope.execution_id
            or tuple(item.hit for item in self.profiles) != discovery.profile_hits
        ):
            raise ValueError(
                "grounding input must inherit exactly the accepted N02 Profile hits"
            )

    def check_profiles(self, definitions: tuple[ProfileDefinition, ...]) -> None:
        if len(definitions) != len(self.profiles):
            raise ValueError("grounding requires a resolved definition per hit Profile")
        for item, definition in zip(self.profiles, definitions, strict=True):
            ref = item.hit.profile_ref
            if (ref.artifact_id, ref.version, item.grounding_binding) != (
                definition.profile_id,
                definition.version,
                definition.grounding_binding,
            ):
                raise ValueError("grounding binding must match the pinned Profile")


class _GroundingInvocationGap(ContractModel):
    """Internal worker record for an N03 gap without a qualified ModelInvocation."""

    schema_version: Literal["1.0.0"]
    execution_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    profile_ref: ProfileContentRef
    binding_ref: ModelBindingContentRef
    dispatch_id: RecordId
    boundary: Literal["not_dispatched", "sent_unknown", "acknowledged"]
    error: NodeError


class GroundingOutput(ContractModel):
    schema_version: Literal["1.0.0"]
    envelope: NodeOutputEnvelope
    profile_results: Annotated[
        tuple[ProfileGroundingResult, ...],
        Field(min_length=1, max_length=1024),
        BeforeValidator(immutable_sequence),
    ]

    @model_validator(mode="after")
    def check_manifest(self) -> Self:
        if self.envelope.node_id is not NodeId.N03:
            raise ValueError("grounding output requires an N03 invocation")
        if any(
            item.ref.record_kind
            not in {
                RuntimeRecordKind.GROUNDING_RECORD,
                RuntimeRecordKind.OBJECT,
                RuntimeRecordKind.EVIDENCE,
                RuntimeRecordKind.MODEL_INVOCATION,
                RuntimeRecordKind.TOOL_EXECUTION,
            }
            for item in self.envelope.output_artifacts
        ):
            raise ValueError(
                "N03 may only produce grounding, Object and producer evidence artifacts"
            )

        require_unique_references(
            tuple(result.profile_ref for result in self.profile_results)
        )
        object_ids = [
            ref.record_id
            for result in self.profile_results
            for ref in result.object_refs
        ]
        grounding_ids = [
            result.grounding_record_ref.record_id for result in self.profile_results
        ]
        if len(set(object_ids)) != len(object_ids) or len(set(grounding_ids)) != len(
            grounding_ids
        ):
            raise ValueError(
                "grounding and Object identities must be distinct across Profiles"
            )
        return self

    def check_records(
        self,
        inputs: GroundingInput,
        records: tuple[GroundingRecord, ...],
        objects: tuple[ObjectRecord, ...],
        selections: tuple[QuestionSelectionRecord, ...],
        invocations: tuple[ModelInvocation | ToolExecution, ...],
        definitions: tuple[ProfileDefinition, ...],
        *,
        undispatched_profiles: tuple[_GroundingInvocationGap, ...] = (),
    ) -> None:
        check_envelope_pair(inputs.envelope, self.envelope)
        inputs.check_profiles(definitions)
        if len(selections) != len(inputs.profiles):
            raise ValueError(
                "grounding must retain the Question selection for each hit"
            )
        all_rows = {
            item.hit.profile_ref: (item, selection, definition)
            for item, selection, definition in zip(
                inputs.profiles, selections, definitions, strict=True
            )
        }
        blocked: set[ProfileContentRef] = set()
        dispatch_ids: set[str] = set()
        for diagnostic in undispatched_profiles:
            hit = all_rows.get(diagnostic.profile_ref)
            if (
                hit is None
                or diagnostic.profile_ref in blocked
                or diagnostic.dispatch_id in dispatch_ids
                or (
                    diagnostic.execution_id,
                    diagnostic.node_run_id,
                    diagnostic.attempt_id,
                )
                != (
                    inputs.envelope.execution_id,
                    self.envelope.node_run_id,
                    self.envelope.attempt_id,
                )
                or not isinstance(hit[0].grounding_binding, ModelGroundingBinding)
                or hit[0].grounding_binding.model_binding_ref != diagnostic.binding_ref
            ):
                raise ValueError(
                    "undispatched grounding must pin a disjoint Profile and Attempt"
                )
            blocked.add(diagnostic.profile_ref)
            dispatch_ids.add(diagnostic.dispatch_id)
        for item, selected, _ in all_rows.values():
            item.hit.check_selection(inputs.envelope.execution_id, selected)
        attempted = tuple(row for ref, row in all_rows.items() if ref not in blocked)
        if not len(attempted) == len(self.profile_results) == len(records) == len(
            invocations
        ) or any(
            isinstance(inv, ModelInvocation) and inv.model_invocation_id in dispatch_ids
            for inv in invocations
        ):
            raise ValueError(
                "grounding requires exactly one producer per dispatched Profile"
            )
        if len({obj.object_id for obj in objects}) != len(objects):
            raise ValueError("Object IDs are unique within the Execution")
        object_by_id = {obj.object_id: obj for obj in objects}
        if object_by_id.keys() != {
            ref.record_id
            for result in self.profile_results
            for ref in result.object_refs
        }:
            raise ValueError(
                "grounding Object evidence must exhaust all recorded Object references"
            )
        for (item, selection, definition), result, record, invocation in zip(
            attempted,
            self.profile_results,
            records,
            invocations,
            strict=True,
        ):
            record.check_selection(inputs.envelope.execution_id, selection)
            if result != record.as_profile_result() or (
                record.execution_id,
                record.node_run_id,
                record.attempt_id,
                record.grounding_binding,
                record.source_media_ref,
                record.coordinate_system,
            ) != (
                inputs.envelope.execution_id,
                self.envelope.node_run_id,
                self.envelope.attempt_id,
                item.grounding_binding,
                inputs.original_media_ref,
                inputs.original_coordinate_system,
            ):
                raise ValueError(
                    "grounding output must preserve all hit and invocation provenance"
                )
            localized_objects = tuple(
                object_by_id[ref.record_id] for ref in result.object_refs
            )
            record.check_objects(localized_objects)
            if any(
                obj.entity_ref != definition.entity_ref for obj in localized_objects
            ):
                raise ValueError("Objects must retain the pinned Profile entity")
            _check_profile_groups(definition, localized_objects)
            _check_producer(record, invocation)


def _check_profile_groups(
    definition: ProfileDefinition, objects: tuple[ObjectRecord, ...]
) -> None:
    grouping = definition.grounding_grouping
    groups = tuple(
        obj.grounding_member_boxes
        for obj in objects
        if obj.grounding_member_boxes is not None
    )
    if grouping is None:
        if groups:
            raise ValueError("grouped Objects require grouping in the pinned Profile")
        return
    if len(groups) != len(objects):
        raise ValueError("grouped grounding must preserve the members of every Object")
    if sum(map(len, groups)) > 1024:
        raise ValueError("grouped grounding exceeds the Profile box limit")
    if groups != observation_groups(
        tuple(box for members in groups for box in members), grouping
    ):
        raise ValueError("grounding groups must match the pinned Profile grouping")
    marked_media_ids = tuple(
        obj.marked_frame.media_ref.media_id
        for obj in objects
        if obj.marked_frame is not None
    )
    if len(set(marked_media_ids)) != len(marked_media_ids):
        raise ValueError("historical marked frame media identities must be distinct")


def _check_producer(
    record: GroundingRecord, invocation: ModelInvocation | ToolExecution
) -> None:
    if (
        invocation.execution_id,
        invocation.node_run_ref.record_id,
        invocation.attempt_ref.record_id,
    ) != (record.execution_id, record.node_run_id, record.attempt_id):
        raise ValueError("grounding producer belongs to a different invocation scope")
    producer = record.producer
    status: str
    if isinstance(producer, ModelGroundingProvenance):
        if not isinstance(invocation, ModelInvocation) or (
            invocation.node_id != NodeId.N03.value
            or invocation.object_ref is not None
            or ModelCapability.OBJECT_GROUNDING not in invocation.purposes
            or invocation.profile_refs != (record.profile_ref,)
            or invocation.model_invocation_id != producer.model_invocation_ref.record_id
            or invocation.configured_provider.binding_ref.stable_json()
            != producer.model_binding_ref.stable_json()
        ):
            raise ValueError(
                "grounding model provenance must resolve to the actual Profile invocation"
            )
        status = invocation.outcome.status
    else:
        if not isinstance(invocation, ToolExecution) or (
            not isinstance(invocation.invocation_origin, GroundingBindingOrigin)
            or invocation.invocation_origin.grounding_binding
            != record.grounding_binding
            or invocation.profile_ref != record.profile_ref
            or invocation.tool_ref != producer.tool_ref
            or invocation.actual_implementation_version
            != producer.actual_implementation_version
            or invocation.tool_execution_id != producer.tool_execution_ref.record_id
            or not {ref.record_id for ref in producer.nested_model_invocation_refs}
            <= {
                ref.artifact_ref.record_id
                for ref in invocation.output_refs
                if ref.artifact_ref.record_kind is RuntimeRecordKind.MODEL_INVOCATION
            }
        ):
            raise ValueError(
                "grounding Tool provenance must resolve to its N03 grounding origin"
            )
        status = invocation.status
    if (
        record.outcome in {GroundingOutcome.OBJECTS, GroundingOutcome.NO_TARGET}
        and status != "succeeded"
        or record.outcome is GroundingOutcome.PARTIAL
        and status not in {"succeeded", "partial"}
    ):
        raise ValueError(
            "grounding cannot assert observations absent usable producer output"
        )
