"""Signed-scene regression for the real N01 and pre-N02 metadata gate."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from examples.nonmotor_vehicle_illegal_parking.compilable import lifecycle
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    AT,
    REGION,
    load_source,
    with_source_provenance,
)
from jimu_runtime.bundle.compiler import ReferenceSlot
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.definitions.ontology import RegionDefinition
from jimu_runtime.domain.enums import (
    ArtifactKind,
    AttemptStatus,
    ExecutionStatus,
    NodeRunStatus,
)
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.nodes.context import SuccessfulContextBuild
from jimu_runtime.domain.runtime.applicability import (
    ApplicabilityReason,
    EvaluatedApplicabilityLevel,
    RuleTruth,
)
from jimu_runtime.domain.runtime.executions import (
    Attempt,
    AttemptLink,
    ExecutionRecord,
    NodeId,
    NodeRun,
)
from jimu_runtime.domain.runtime.facts import AttemptRef, NodeRunRef, ProfileContentRef
from jimu_runtime.domain.runtime.intake import RegistryRecordRef
from jimu_runtime.domain.tool_mappings import RegionContentRef
from jimu_runtime.domain.validation.authority_inputs import NodeAttemptOwner
from jimu_runtime.inspection.decision import build_profile_gate
from jimu_runtime.inspection.perception.context import build_context
from jimu_runtime.workflow.engine import BranchKey
from jimu_runtime.workflow.jobs import JobClaim
from jimu_runtime.workflow.mock_support import VERSION, K, artifact
from jimu_runtime.workflow.worker import ClaimedNode
from jimu_runtime.workflow.worker_nodes import BranchInputs, invocation_envelope
from jimu_runtime.workflow.worker_records import ContextMessage
from jimu_runtime.workflow.worker_source import (
    OpenExecution,
    TrustedWorkerSource,
    _profile_plan,
)
from tests.fixtures.wp05.perception import release_scope


@pytest.mark.parametrize(
    ("metadata_source", "operator", "absent", "expected", "reason"),
    (
        (
            "context_snapshot",
            "eq",
            False,
            RuleTruth.TRUE,
            ApplicabilityReason.INCLUDED_BY_POLICY,
        ),
        (
            "context_snapshot",
            "ne",
            False,
            RuleTruth.FALSE,
            ApplicabilityReason.EXCLUDED_BY_POLICY,
        ),
        (
            "context_snapshot",
            "ne",
            True,
            RuleTruth.UNKNOWN,
            ApplicabilityReason.METADATA_UNKNOWN,
        ),
        (
            "profile_metadata",
            "eq",
            False,
            RuleTruth.TRUE,
            ApplicabilityReason.INCLUDED_BY_POLICY,
        ),
        (
            "region_metadata",
            "eq",
            False,
            RuleTruth.TRUE,
            ApplicabilityReason.INCLUDED_BY_POLICY,
        ),
    ),
)
def test_signed_scene_uses_metadata_truth_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_source: str,
    operator: str,
    absent: bool,
    expected: RuleTruth,
    reason: ApplicabilityReason,
) -> None:
    source = load_source()
    policy = next(
        item for item in source.artifacts if item.artifact_id == "applicability"
    )
    content = json.loads(policy.content)
    field, shape, value_kind, expected_value = {
        "context_snapshot": ("fixture_enabled", "boolean", "bool", True),
        "profile_metadata": ("profile_id", "string", "string", "nonmotor_vehicle"),
        "region_metadata": ("region_id", "string", "string", REGION),
    }[metadata_source]
    selector = {
        "source": metadata_source,
        "schema_ref": None,
        "field_path": [field],
    }
    content["policy"].update(
        context_allowlist=[selector],
        condition_ast={
            "kind": "comparison",
            "operator": operator,
            "left": {"kind": "context_ref", "selector": selector},
            "right": {
                "kind": "literal",
                "constant": {
                    "type_definition": {
                        "kind": value_kind,
                        **({"max_length": 128} if shape == "string" else {}),
                    },
                    "value": {"kind": value_kind, "value": expected_value},
                },
            },
        },
    )
    target = (ArtifactKind.CONTEXT_SCHEMA, "parking-context", VERSION)
    revised = replace(
        policy,
        content=canonical_bytes(content),
        reference_slots=(
            ReferenceSlot(
                location=("policy", "context_allowlist", 0, "schema_ref"),
                target=target,
                schema_ref=True,
            ),
            ReferenceSlot(
                location=("policy", "condition_ast", "left", "selector", "schema_ref"),
                target=target,
                schema_ref=True,
            ),
        ),
    )
    context_schema = next(
        row for row in source.artifacts if row.kind is ArtifactKind.CONTEXT_SCHEMA
    )
    schema_doc = json.loads(context_schema.content)
    schema_doc["properties"][field] = {
        "type": shape,
        **({"maxLength": 128} if shape == "string" else {}),
    }
    provenance = next(
        row for row in source.artifacts if row.artifact_id == "context-provenance"
    )
    provenance_doc = json.loads(provenance.content)
    provenance_doc["policy"]["fields"].append(
        {
            "field_path": [field],
            "schema_ref": None,
            "required": False,
            "time_sensitive": False,
            "allowed_sources": ["trusted_task_metadata"],
        }
    )
    replacements = {
        policy.identity: revised,
        context_schema.identity: replace(
            context_schema, content=canonical_bytes(schema_doc)
        ),
        provenance.identity: replace(
            provenance,
            content=canonical_bytes(provenance_doc),
            reference_slots=(
                *provenance.reference_slots,
                ReferenceSlot(
                    location=("policy", "fields", 1, "schema_ref"),
                    target=target,
                    schema_ref=True,
                ),
            ),
        ),
    }
    modified = with_source_provenance(
        replace(
            source,
            artifacts=tuple(
                replacements.get(row.identity, row) for row in source.artifacts
            ),
        )
    )
    monkeypatch.setattr(lifecycle, "load_source", lambda: modified)
    if not absent:
        submit = lifecycle.local_submission

        def declare_fixture_flag(media):
            from jimu_runtime.domain.values import (
                BooleanValue,
                RecordValue,
                StringValue,
            )

            original = submit(media)
            value = (
                BooleanValue(kind="bool", value=True)
                if shape == "boolean"
                else StringValue(kind="string", value="different-task-input")
            )
            metadata = RecordValue(kind="record", value={field: value})
            return original.model_copy(
                update={
                    "context": original.context.model_copy(
                        update={"metadata": metadata}
                    )
                }
            )

        monkeypatch.setattr(lifecycle, "local_submission", declare_fixture_flag)
    scene = lifecycle.exercise_lifecycle(tmp_path)
    scope = release_scope(scene)
    (region_item,) = scope.catalogue.of_kind(ArtifactKind.REGION)
    region_ref = RegionContentRef.model_validate(region_item.ref.model_dump())
    region = scope.resolve(region_ref, RegionDefinition)
    assert region.region_id == REGION
    source_rows = TrustedWorkerSource(
        scope.media,
        RegistryRecordRef(
            record_id=scope.task.context.device_id,
            version=VERSION,
            digest=sha256_digest(b"device"),
        ),
        RegistryRecordRef(
            record_id=scope.task.context.camera_id,
            version=VERSION,
            digest=sha256_digest(b"camera"),
        ),
    )
    (profile,) = scope.catalogue.of_kind(ArtifactKind.PROFILE)
    opened = OpenExecution(
        scope,
        scene.execution,
        scene.release,
        source_rows,
        region_ref,
        region,
        (
            _profile_plan(
                scope, ProfileContentRef.model_validate(profile.ref.model_dump())
            ),
        ),
    )
    execution = ExecutionRecord(
        schema_version=VERSION,
        execution_id=scope.execution_id,
        task_id=scope.task.task_id,
        pins=scope.pins,
        trace=scope.task.trace,
        status=ExecutionStatus.RUNNING,
        created_at=AT,
        started_at=AT,
        model_invocation_refs=(),
    )
    node_ref = NodeRunRef(record_kind=K.NODE_RUN, record_id="local.n01")
    attempt = Attempt(
        schema_version=VERSION,
        attempt_id="local.attempt",
        execution_id=scope.execution_id,
        owner=NodeAttemptOwner(owner_kind="node", node_run_ref=node_ref),
        attempt_number=1,
        status=AttemptStatus.RUNNING,
        created_at=AT,
        started_at=AT,
    )
    node = NodeRun(
        schema_version=VERSION,
        node_run_id=node_ref.record_id,
        execution_id=scope.execution_id,
        node_id=NodeId.N01,
        trace=scope.task.trace,
        config_ref=scene.execution.policy_ref,
        upstream_inputs=(artifact(K.TASK, scope.task.task_id, scope.task).pin,),
        input_digest=scope.pins.bundle_digest,
        status=NodeRunStatus.RUNNING,
        created_at=AT,
        started_at=AT,
        attempt_history=(
            AttemptLink(
                attempt_ref=AttemptRef(
                    record_kind=K.ATTEMPT, record_id=attempt.attempt_id
                ),
                attempt_number=1,
            ),
        ),
        output_refs=(),
    )
    branch = BranchKey(NodeId.N01)
    claimed = ClaimedNode(
        JobClaim(
            scope.task.idempotency_scope,
            scope.execution_id,
            node.node_run_id,
            "local.job",
            1,
            "local",
            AT,
        ),
        attempt,
        node,
        branch,
        opened,
        BranchInputs(),
        execution,
        AT,
    )
    context = build_context(
        claimed, invocation_envelope(opened, branch, node, attempt)
    ).message
    assert isinstance(context, ContextMessage) and isinstance(
        context.output, SuccessfulContextBuild
    )
    assert context.snapshot is not None
    gate = build_profile_gate(opened, context.inputs, context.output, context.snapshot)
    (trace,) = gate.traces
    (level,) = trace.levels
    assert isinstance(level, EvaluatedApplicabilityLevel)
    assert level.truth is expected
    assert level.reason_code is reason
    assert level.declared_input_refs[0].field_path == (field,)
    assert bool(gate.evaluation.eligible_profiles) is (expected is RuleTruth.TRUE)
    assert bool(gate.evaluation.excluded_profiles) is (expected is RuleTruth.FALSE)
    assert bool(gate.evaluation.unresolved_profiles) is (expected is RuleTruth.UNKNOWN)
