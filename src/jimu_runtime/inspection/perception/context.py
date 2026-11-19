"""N01 freezes the trusted Task, source revisions and admitted Region context."""

from dataclasses import dataclass

from jimu_runtime.bundle.artifacts import DRAFT_2020_12
from jimu_runtime.domain.definition_enums import ContextProvenanceSource
from jimu_runtime.domain.definition_support import ContextSchemaRef
from jimu_runtime.domain.definitions.geometry import GeometryResourceDefinition
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.definitions.resources import StaticAssetDescriptor
from jimu_runtime.domain.nodes.context import (
    ContextBuildInput,
    FailedContextBuild,
    RequiredContextFailureReport,
    SuccessfulContextBuild,
    UsableContextReport,
)
from jimu_runtime.domain.nodes.envelopes import NodeError, NodeInputEnvelope
from jimu_runtime.domain.policies import ContextProvenancePolicyArtifact
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.intake import (
    ContextSnapshot,
    ContextValue,
    ContextValueProvenance,
    RegionDefinitionProvenance,
    TaskMetadataProvenance,
)
from jimu_runtime.domain.schema_types import FieldSelector
from jimu_runtime.domain.validation.issues import ValidationIssue
from jimu_runtime.domain.validation.schema_descriptors import resolve_field
from jimu_runtime.domain.values import (
    ListTypeDefinition,
    RecordTypeDefinition,
    RecordValue,
    TypeDefinition,
    Value,
    validate_typed_value,
)
from jimu_runtime.inspection.geometry.region import verify_parking_area
from jimu_runtime.inspection.perception.model import (
    issue,
    node_output,
    original_frame,
    runtime_id,
)
from jimu_runtime.workflow.mock_context import snapshot_artifact, snapshot_ref
from jimu_runtime.workflow.mock_support import VERSION, K, artifact
from jimu_runtime.workflow.worker import AdapterRejected, ClaimedNode
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_records import ContextMessage
from jimu_runtime.workflow.worker_source import OpenExecution


def _region_inputs(opened: OpenExecution) -> dict[str, object]:
    scope = opened.scope
    region = scope.resolve(opened.region_ref, RegionDefinition)
    if region != opened.region or region.geometry_ref is None:
        raise ValueError("verified Region geometry is unavailable")
    geometry = scope.resolve(region.geometry_ref, GeometryResourceDefinition)
    source = scope.catalogue.resolve(geometry.provenance.source_asset_ref)
    if (
        not isinstance(source.value, StaticAssetDescriptor)
        or source.asset_bytes is None
    ):
        raise ValueError("verified Region geometry asset is unavailable")
    area = verify_parking_area(
        catalogue=scope.catalogue,
        region_ref=opened.region_ref,
        region=region,
        geometry_ref=region.geometry_ref,
        geometry=geometry,
        source=source.value,
        source_bytes=source.asset_bytes,
        coordinates=original_frame(scope),
    )
    return {"parking_area": area.model_dump(mode="python")}


def _typed_raw(definition: TypeDefinition, raw: object) -> object:
    if isinstance(definition, RecordTypeDefinition):
        if not isinstance(raw, dict):
            raise ValueError("configured context field requires a record")
        schemas = {field.name: field.type_definition for field in definition.fields}
        if not raw.keys() <= schemas.keys():
            raise ValueError("configured context field contains undeclared fields")
        raw = {key: _typed_raw(schemas[key], value) for key, value in raw.items()}
    elif isinstance(definition, ListTypeDefinition):
        if (
            not isinstance(raw, (list, tuple))
            or not definition.min_items <= len(raw) <= definition.max_items
        ):
            raise ValueError("configured context list falls outside its declared shape")
        raw = tuple(_typed_raw(definition.item_type, member) for member in raw)
    return {"kind": definition.kind, "value": raw}


def _task_value(metadata: RecordValue, path: tuple[str, ...]) -> Value | None:
    value: Value | None = metadata
    for part in path:
        if not isinstance(value, RecordValue):
            return None
        value = value.value.get(part)
    return value


@dataclass(frozen=True, slots=True)
class _ContextFields:
    values: tuple[ContextValue, ...]
    required: tuple[ValidationIssue, ...]
    optional: tuple[ValidationIssue, ...]


def _fields(opened: OpenExecution, inputs: ContextBuildInput) -> _ContextFields:
    scope = opened.scope
    policy = scope.resolve(
        inputs.context_provenance_policy_ref, ContextProvenancePolicyArtifact
    ).policy
    region_data = None
    if any(
        ContextProvenanceSource.REGION_DEFINITION in field.allowed_sources
        for field in policy.fields
    ):
        try:
            region_data = _region_inputs(opened)
        except ValueError:
            # A missing proof of same-frame geometry must leave the context absent.
            pass
    values: list[ContextValue] = []
    required: list[ValidationIssue] = []
    optional: list[ValidationIssue] = []
    for entry in policy.fields:
        path = entry.field_path
        if entry.schema_ref != inputs.context_schema_ref or not path:
            raise AdapterRejected("perception.context_schema_pin_mismatch")
        diagnostics = required if entry.required else optional
        if entry.time_sensitive:
            diagnostics.append(
                issue(
                    "perception.context_time_proof_unavailable",
                    "No time proof is available for the configured field.",
                    path,
                )
            )
            continue
        definition = resolve_field(
            FieldSelector(schema_ref=entry.schema_ref, field_path=path),
            scope.schema_descriptors,
        ).type_definition
        candidates: list[ContextValue] = []
        unavailable = False
        for source in entry.allowed_sources:
            raw: object
            provenance: ContextValueProvenance
            if source is ContextProvenanceSource.TRUSTED_TASK_METADATA:
                raw = _task_value(scope.task.context.metadata, path)
                provenance = TaskMetadataProvenance(
                    source=source, task_id=scope.task.task_id
                )
            elif source is ContextProvenanceSource.REGION_DEFINITION:
                raw = region_data
                for part in path:
                    raw = raw.get(part) if isinstance(raw, dict) else None
                if raw is not None:
                    try:
                        raw = _typed_raw(definition, raw)
                    except ValueError:
                        unavailable = True
                        continue
                provenance = RegionDefinitionProvenance(
                    source=source, region_ref=inputs.region_ref
                )
            else:
                continue
            if raw is None:
                continue
            try:
                value = validate_typed_value(definition, raw)
            except ValueError:
                unavailable = True
            else:
                candidates.append(
                    ContextValue(field_path=path, value=value, provenance=provenance)
                )
        if candidates and any(
            row.value != candidates[0].value for row in candidates[1:]
        ):
            diagnostics.append(
                issue(
                    "perception.context_source_conflict",
                    "Declared sources disagree on the context field.",
                    path,
                )
            )
        elif unavailable:
            diagnostics.append(
                issue(
                    "perception.context_field_invalid",
                    "The context value does not satisfy its pinned field type.",
                    path,
                )
            )
        elif candidates:
            values.append(candidates[0])
        else:
            diagnostics.append(
                issue(
                    "perception.context_field_unavailable",
                    "A trusted value is unavailable for the configured context field.",
                    path,
                )
            )
    return _ContextFields(tuple(values), tuple(required), tuple(optional))


def validate_context_projection(
    opened: OpenExecution,
    inputs: ContextBuildInput,
    output: SuccessfulContextBuild,
    snapshot: ContextSnapshot,
) -> None:
    """Independently reproject trusted metadata and its optional validation report."""
    scope = opened.scope
    output.check_snapshot(inputs, snapshot)
    if (
        inputs.accepted_task != scope.task
        or inputs.media_asset != scope.media
        or inputs.device_ref != opened.source.device
        or inputs.camera_ref != opened.source.camera
        or inputs.region_ref != opened.region_ref
        or inputs.context_schema_ref.model_dump(exclude={"schema_dialect"})
        != opened.region.context_schema_ref.model_dump()
        or inputs.context_provenance_policy_ref.model_dump()
        != opened.resolved.policy.context_provenance_policy_ref.model_dump()
        or output.context_snapshot_digest != snapshot_artifact(snapshot).pin.digest
    ):
        raise AdapterRejected("perception.context_input_pin_mismatch")
    fields = _fields(opened, inputs)
    if (
        fields.required
        or fields.values != snapshot.values
        or fields.optional != output.validation_report.optional_issues
    ):
        raise AdapterRejected("perception.context_projection_mismatch")


def build_context(claimed: ClaimedNode, envelope: NodeInputEnvelope) -> NodeResult:
    opened = claimed.opened
    scope = opened.scope
    started = claimed.attempt.started_at
    if started is None:
        raise AdapterRejected("perception.context_attempt_not_started")
    scope.check_call(envelope, NodeId.N01, started).check_upstream(
        artifact(K.TASK, scope.task.task_id, scope.task)
    )
    inputs = ContextBuildInput(
        schema_version=VERSION,
        envelope=envelope,
        accepted_task=scope.task,
        media_asset=scope.media,
        device_ref=opened.source.device,
        camera_ref=opened.source.camera,
        region_ref=opened.region_ref,
        context_schema_ref=ContextSchemaRef.model_validate(
            {
                **opened.region.context_schema_ref.model_dump(),
                "schema_dialect": DRAFT_2020_12,
            }
        ),
        context_provenance_policy_ref=PolicyContentRef.model_validate(
            opened.resolved.policy.context_provenance_policy_ref.model_dump()
        ),
    )
    fields = _fields(opened, inputs)
    if fields.required:
        first = fields.required[0]
        failed = FailedContextBuild(
            schema_version=VERSION,
            envelope=node_output(
                claimed,
                (),
                (),
                NodeError(
                    code=first.code,
                    error_class="validation",
                    retryable=False,
                    message=first.message,
                    validation_issues=fields.required,
                ),
            ),
            disposition="required_context_failure",
            validation_report=RequiredContextFailureReport(
                disposition="required_context_failure",
                required_issues=fields.required,
                optional_issues=fields.optional,
            ),
        )
        failed.check_invocation(inputs)
        return NodeResult(
            ContextMessage(
                schema_version=VERSION, inputs=inputs, output=failed, snapshot=None
            ),
            (),
        )
    snapshot = ContextSnapshot(
        schema_version=VERSION,
        context_snapshot_id=runtime_id(envelope, "context"),
        execution_id=scope.execution_id,
        task_id=scope.task.task_id,
        release_id=scope.pins.release_id,
        bundle_digest=scope.pins.bundle_digest,
        media_ref=scope.media.media_ref,
        device_ref=inputs.device_ref,
        camera_ref=inputs.camera_ref,
        region_ref=inputs.region_ref,
        context_schema_ref=inputs.context_schema_ref,
        context_provenance_policy_ref=inputs.context_provenance_policy_ref,
        clock=scope.task.context.clock,
        values=fields.values,
        created_at=started,
    )
    item = snapshot_artifact(snapshot)
    output = SuccessfulContextBuild(
        schema_version=VERSION,
        envelope=node_output(claimed, (item,), ()),
        disposition="usable",
        validation_report=UsableContextReport(
            disposition="usable", optional_issues=fields.optional
        ),
        context_snapshot_ref=snapshot_ref(snapshot),
        context_snapshot_digest=item.pin.digest,
    )
    output.check_snapshot(inputs, snapshot)
    return NodeResult(
        ContextMessage(
            schema_version=VERSION, inputs=inputs, output=output, snapshot=snapshot
        ),
        (item,),
    )
