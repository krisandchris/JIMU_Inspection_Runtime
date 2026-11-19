"""N03 creates Objects from independently dispatched Profile grounding."""

from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import TypedDict

from jimu_runtime.bundle.schema_validation import compile_offline_response_schema
from jimu_runtime.domain.definition_enums import ModelCapability
from jimu_runtime.domain.definitions.capabilities import (
    ModelGroundingBinding,
    ProfileDefinition,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.envelopes import NodeError, NodeInputEnvelope
from jimu_runtime.domain.nodes.grounding import (
    GroundingInput,
    GroundingOutput,
    GroundingProfileInput,
    _GroundingInvocationGap,
)
from jimu_runtime.domain.references import SchemaRef
from jimu_runtime.domain.runtime.facts import ObjectRef, ProfileContentRef
from jimu_runtime.domain.runtime.grounding_groups import observation_groups
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.media import BBoxXYXY, ImageDimensions, MediaRef
from jimu_runtime.domain.runtime.perception import (
    GroundingOutcome,
    GroundingRecord,
    GroundingRecordRef,
    MarkedGroundingFrame,
    ModelGroundingProvenance,
    ObjectRecord,
)
from jimu_runtime.domain.tool_mappings import ImageInput
from jimu_runtime.gateway.bridge import (
    CapabilityGateway,
    ModelCall,
)
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.inspection.media.group_frame import (
    MARKED_FRAME_RENDERER_VERSION,
    marked_group_image,
)
from jimu_runtime.inspection.media.types import MediaError
from jimu_runtime.inspection.perception.model import (
    GroundingReply,
    ModelAccess,
    associated_invocation,
    build_chat_contract,
    checked_reply,
    failed_request,
    gateway_binding,
    image_input,
    invocation_ref,
    node_output,
    original_frame,
    prepare_binding,
    runtime_id,
    schema_resource,
    validate_reply_bytes,
)
from jimu_runtime.workflow.mock_support import (
    VERSION,
    K,
    VerifiedExecutionScope,
    artifact,
)
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_records import GroundingMessage

type StageMarkedFrame = Callable[
    [ClaimedNode, ObjectRecord, VerifiedImage], Awaitable[None]
]


class _GroupFields(TypedDict, total=False):
    grounding_member_boxes: tuple[BBoxXYXY, ...]
    marked_frame: MarkedGroundingFrame


# Bound repeated full-frame PNG work and staged bytes for the entire N03 call.
# At the maximum allowed source image area, this admits ten marked frames.
_MAX_MARKED_FRAME_PIXELS = 240_000_000
_MAX_MARKED_FRAME_BYTES = 256 * 1024 * 1024


@dataclass(slots=True)
class _MarkedFrameBudget:
    pixels: int = 0
    byte_size: int = 0

    def reserve_pixels(self, dimensions: ImageDimensions, count: int) -> None:
        self.pixels += dimensions.width * dimensions.height * count
        if self.pixels > _MAX_MARKED_FRAME_PIXELS:
            raise AdapterRejected("perception.marked_frame_pixel_budget_exceeded")

    def reserve_bytes(self, image: VerifiedImage) -> None:
        self.byte_size += len(image.content)
        if self.byte_size > _MAX_MARKED_FRAME_BYTES:
            raise AdapterRejected("perception.marked_frame_byte_budget_exceeded")


def _grouped_identities(
    envelope: NodeInputEnvelope,
    profile_ref: ProfileContentRef,
    groups: tuple[tuple[BBoxXYXY, ...], ...],
) -> Iterator[tuple[str, str]]:
    """Return identities in canonical group order, including repeated singleton boxes."""
    profile = profile_ref.stable_json()
    repeats: Counter[tuple[BBoxXYXY, ...]] = Counter()
    for members in groups:
        occurrence = repeats[members]
        repeats[members] += 1
        identity = canonical_bytes([list(member.xyxy) for member in members]).decode()
        yield (
            runtime_id(envelope, "object", profile, identity, str(occurrence)),
            runtime_id(envelope, "marked-frame", profile, identity, str(occurrence)),
        )


def verify_grouped_grounding_response(
    scope: VerifiedExecutionScope,
    message: GroundingMessage,
    record: GroundingRecord,
    normalized: bytes,
) -> None:
    """Bind an accepted grouped projection to its exact provider output."""
    profile = scope.resolve(record.profile_ref, ProfileDefinition)
    grouping = profile.grounding_grouping
    binding = profile.grounding_binding
    if grouping is None or not isinstance(binding, ModelGroundingBinding):
        raise AdapterRejected("perception.grouped_grounding_profile_mismatch")
    frame = original_frame(scope)
    if (
        record.grounding_binding != binding
        or record not in message.records
        or message.inputs.original_media_ref != scope.media.media_ref
        or message.inputs.original_coordinate_system != frame
    ):
        raise AdapterRejected("perception.grouped_grounding_record_mismatch")

    contract = build_chat_contract(scope, binding.model_binding_ref)
    boxes = tuple(validate_reply_bytes(normalized, contract, GroundingReply).boxes)
    for box in boxes:
        try:
            box.validate_bounds(frame.dimensions)
        except ValueError:
            raise AdapterRejected("perception.invalid_grounding_boxes") from None
    groups = observation_groups(boxes, grouping)
    objects = tuple(
        obj for obj in message.objects if obj.profile_ref == record.profile_ref
    )
    expected_outcome = (
        GroundingOutcome.OBJECTS if groups else GroundingOutcome.NO_TARGET
    )
    if record.outcome is not expected_outcome or tuple(
        obj.object_id for obj in objects
    ) != tuple(ref.record_id for ref in record.object_refs):
        raise AdapterRejected("perception.grouped_grounding_record_mismatch")
    _verify_grouped_objects(
        message.inputs.envelope, record.profile_ref, objects, groups
    )


def _verify_grouped_objects(
    envelope: NodeInputEnvelope,
    profile_ref: ProfileContentRef,
    objects: tuple[ObjectRecord, ...],
    groups: tuple[tuple[BBoxXYXY, ...], ...],
) -> None:
    if len(groups) != len(objects):
        raise AdapterRejected("perception.grouped_grounding_members_mismatch")
    identities = _grouped_identities(envelope, profile_ref, groups)
    for obj, members, (object_id, media_id) in zip(
        objects, groups, identities, strict=True
    ):
        marked = obj.marked_frame
        if (
            obj.grounding_member_boxes != members
            or obj.bbox != BBoxXYXY.enclosing(members)
            or obj.object_id != object_id
            or marked is None
            or marked.media_ref.media_id != media_id
        ):
            raise AdapterRejected("perception.grouped_grounding_members_mismatch")


def _verify_image_binding(
    claimed: ClaimedNode, binding: ModelGroundingBinding, input_schema: SchemaRef
) -> None:
    entries = binding.input_bindings or ()
    if len(entries) != 1:
        raise AdapterRejected("perception.unsupported_grounding_inputs")
    (entry,) = entries
    if (
        not isinstance(entry.source, ImageInput)
        or entry.source.selector.field_path != ()
        or entry.target.schema_ref != input_schema
        or entry.target.field_path != ("image",)
        or not entry.required
        or entry.on_missing != "block"
    ):
        raise AdapterRejected("perception.unsupported_grounding_inputs")
    scope = claimed.opened.scope
    resource = schema_resource(scope, entry.source.selector.schema_ref)
    compile_offline_response_schema(resource.document).validate(
        image_input(scope)["image"]
    )


async def ground(
    claimed: ClaimedNode,
    envelope: NodeInputEnvelope,
    gateway: CapabilityGateway,
    access: ModelAccess,
    stage_marked_frame: StageMarkedFrame | None = None,
) -> NodeResult:
    scope = claimed.opened.scope
    discovery = claimed.previous.discovery
    started = claimed.attempt.started_at
    if discovery is None or started is None:
        raise AdapterRejected("perception.grounding_inputs_unavailable")
    checked = discovery.result()
    checked.output.check_records(
        discovery.inputs,
        checked.selections,
        checked.invocations,
        group_failures=discovery.group_failures,
    )
    scope.check_call(envelope, envelope.node_id, started).check_upstream(
        *checked.artifacts
    )
    hits = discovery.output.profile_hits
    definitions = tuple(
        scope.resolve(hit.profile_ref, ProfileDefinition) for hit in hits
    )
    if stage_marked_frame is None and any(
        profile.grounding_grouping is not None for profile in definitions
    ):
        raise AdapterRejected("perception.marked_frame_storage_unavailable")
    frame = original_frame(scope)
    inputs = GroundingInput(
        schema_version=VERSION,
        envelope=envelope,
        original_media_ref=scope.media.media_ref,
        original_coordinate_system=frame,
        profiles=tuple(
            GroundingProfileInput(hit=hit, grounding_binding=profile.grounding_binding)
            for hit, profile in zip(hits, definitions, strict=True)
        ),
    )
    inputs.check_discovery(checked.output)
    inputs.check_profiles(definitions)
    image = await access.image(scope)
    records: list[GroundingRecord] = []
    objects: list[ObjectRecord] = []
    invocations: list[ModelInvocation] = []
    failure: NodeError | None = None
    undispatched: list[_GroundingInvocationGap] = []
    marked_budget = _MarkedFrameBudget()
    for entry, profile in zip(inputs.profiles, definitions, strict=True):
        hit, grounding = entry.hit, entry.grounding_binding
        if not isinstance(grounding, ModelGroundingBinding):
            raise AdapterRejected("perception.unsupported_grounding_binding")
        ref, purpose = grounding.model_binding_ref, ModelCapability.OBJECT_GROUNDING
        binding = prepare_binding(scope, ref, purpose)
        _verify_image_binding(claimed, grounding, binding.definition.input_schema_ref)
        contract = build_chat_contract(scope, ref)
        call = ModelCall(
            binding_ref=gateway_binding(ref),
            purposes=(purpose,),
            profile_refs=(hit.profile_ref,),
            prompt=binding.prompt(
                image_input(scope), {"profile_id": hit.profile_ref.artifact_id}
            ),
            resource_scope_id=access.resource_scope_id,
            image=image,
        )
        result = await gateway.invoke_model(claimed, call)
        invocation = associated_invocation(claimed, call, result)
        error: NodeError | None = None
        if invocation is None:
            if result.dispatch_id is None:
                raise AdapterRejected("perception.grounding_dispatch_unavailable")
            error = failed_request(result, "perception.grounding_invocation_missing")
            undispatched.append(
                _GroundingInvocationGap(
                    schema_version=VERSION,
                    execution_id=envelope.execution_id,
                    node_run_id=envelope.node_run_id,
                    attempt_id=envelope.attempt_id,
                    profile_ref=hit.profile_ref,
                    binding_ref=ref,
                    dispatch_id=result.dispatch_id,
                    boundary=result.boundary,
                    error=error,
                )
            )
            if failure is None or error.retryable:
                failure = error
            continue
        invocations.append(invocation)
        boxes: tuple[BBoxXYXY, ...] = ()
        if result.output is None:
            error = failed_request(result, "perception.grounding_unavailable")
        else:
            try:
                boxes = tuple(checked_reply(result, contract, GroundingReply).boxes)
                for box in boxes:
                    box.validate_bounds(frame.dimensions)
            except ValueError:
                boxes = ()
                error = failed_request(result, "perception.invalid_grounding_boxes")
        if error is not None and (failure is None or error.retryable):
            failure = error
        record, new_objects = await _profile_objects(
            claimed,
            inputs,
            entry,
            profile,
            grounding,
            invocation,
            boxes,
            error,
            image,
            stage_marked_frame,
            marked_budget,
        )
        records.append(record)
        objects.extend(new_objects)
    if not records:
        assert failure is not None
        raise AdapterRejected(failure.code)
    artifacts = (
        *(
            artifact(K.GROUNDING_RECORD, row.grounding_record_id, row)
            for row in records
        ),
        *(artifact(K.OBJECT, row.object_id, row) for row in objects),
        *(
            artifact(K.MODEL_INVOCATION, row.model_invocation_id, row)
            for row in invocations
        ),
    )
    output = GroundingOutput(
        schema_version=VERSION,
        envelope=node_output(
            claimed,
            artifacts,
            tuple(invocations),
            failure
            if all(row.outcome is GroundingOutcome.FAILED for row in records)
            else None,
        ),
        profile_results=tuple(row.as_profile_result() for row in records),
    )
    output.check_records(
        inputs,
        tuple(records),
        tuple(objects),
        checked.selections,
        tuple(invocations),
        definitions,
        undispatched_profiles=tuple(undispatched),
    )
    return NodeResult(
        GroundingMessage(
            schema_version=VERSION,
            inputs=inputs,
            output=output,
            records=tuple(records),
            objects=tuple(objects),
            invocations=tuple(invocations),
            undispatched_profiles=tuple(undispatched),
        ),
        artifacts,
        tuple(invocation_ref(row) for row in invocations),
    )


async def _profile_objects(
    claimed: ClaimedNode,
    inputs: GroundingInput,
    entry: GroundingProfileInput,
    profile: ProfileDefinition,
    grounding: ModelGroundingBinding,
    invocation: ModelInvocation,
    boxes: tuple[BBoxXYXY, ...],
    error: NodeError | None,
    original: VerifiedImage,
    stage_marked_frame: StageMarkedFrame | None,
    marked_budget: _MarkedFrameBudget,
) -> tuple[GroundingRecord, tuple[ObjectRecord, ...]]:
    hit, env = entry.hit, inputs.envelope
    record_ref = GroundingRecordRef(
        record_kind=K.GROUNDING_RECORD,
        record_id=runtime_id(env, "grounding", hit.profile_ref.stable_json()),
    )
    producer = ModelGroundingProvenance(
        kind="model",
        model_binding_ref=grounding.model_binding_ref,
        model_invocation_ref=invocation_ref(invocation),
    )
    grouping = profile.grounding_grouping
    groups = (
        observation_groups(boxes, grouping)
        if grouping is not None
        else tuple((box,) for box in boxes)
    )
    dimensions = inputs.original_coordinate_system.dimensions
    if grouping is not None:
        marked_budget.reserve_pixels(dimensions, len(groups))
        identities: Iterator[tuple[str, str | None]] = _grouped_identities(
            env, hit.profile_ref, groups
        )
    else:
        profile_identity = hit.profile_ref.stable_json()
        identities = (
            (runtime_id(env, "object", profile_identity, str(index)), None)
            for index in range(len(groups))
        )
    objects: list[ObjectRecord] = []
    for members, (object_id, media_id) in zip(groups, identities, strict=True):
        box = members[0]
        grouped_fields: _GroupFields = {}
        marked = None
        if media_id is not None:
            box = BBoxXYXY.enclosing(members)
            try:
                marked = await marked_group_image(original, dimensions, box)
            except MediaError:
                raise AdapterRejected("perception.marked_frame_render_failed") from None
            marked_budget.reserve_bytes(marked)
            grouped_fields = {
                "grounding_member_boxes": members,
                "marked_frame": MarkedGroundingFrame(
                    media_ref=MediaRef(media_id=media_id, sha256=marked.sha256),
                    byte_size=len(marked.content),
                    mime_type="image/png",
                    renderer_version=MARKED_FRAME_RENDERER_VERSION,
                ),
            }
        obj = ObjectRecord(
            schema_version=VERSION,
            object_id=object_id,
            execution_id=env.execution_id,
            entity_ref=profile.entity_ref,
            grounding_record_ref=record_ref,
            source_media_ref=inputs.original_media_ref,
            coordinate_system=inputs.original_coordinate_system,
            bbox=box,
            grounding_producer=producer,
            visibility="unknown",
            localization_status="localized",
            evidence_refs=(),
            profile_ref=hit.profile_ref,
            question_selection_ref=hit.question_selection_ref,
            effective_question_refs=hit.effective_question_refs,
            **grouped_fields,
        )
        if marked is not None:
            assert stage_marked_frame is not None
            try:
                await stage_marked_frame(claimed, obj, marked)
            except MediaError:
                raise AdapterRejected("perception.marked_frame_stage_failed") from None
        objects.append(obj)
    return GroundingRecord(
        schema_version=VERSION,
        grounding_record_id=record_ref.record_id,
        execution_id=env.execution_id,
        node_run_id=env.node_run_id,
        attempt_id=env.attempt_id,
        grounding_binding=grounding,
        source_media_ref=inputs.original_media_ref,
        coordinate_system=inputs.original_coordinate_system,
        transforms=(),
        outcome=GroundingOutcome.FAILED
        if error
        else GroundingOutcome.OBJECTS
        if objects
        else GroundingOutcome.NO_TARGET,
        object_refs=tuple(
            ObjectRef(record_kind=K.OBJECT, record_id=obj.object_id) for obj in objects
        ),
        evidence_refs=(),
        producer=producer,
        validation_issues=() if error is None else error.validation_issues,
        profile_ref=hit.profile_ref,
        question_selection_ref=hit.question_selection_ref,
        effective_question_refs=hit.effective_question_refs,
    ), tuple(objects)
