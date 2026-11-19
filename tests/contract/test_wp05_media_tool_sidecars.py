"""Exercise named sidecars with exact PG dispatch and Attempt output identities."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import text

from jimu_runtime.bundle.trust import TrustPin
from jimu_runtime.domain.enums import FactStatus, RuntimeRecordKind
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    RetentionPolicyRef,
)
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.intake import AcceptedTask, TaskSubmission
from jimu_runtime.domain.runtime.media import CoordinateSystem, TransformRecord
from jimu_runtime.domain.runtime.tool_executions import ToolArtifactRef, ToolExecution
from jimu_runtime.domain.schema_types import ToolInputSchemaRef, ToolOutputSchemaRef
from jimu_runtime.gateway.journal import (
    DispatchIntent,
    DispatchJournal,
    DispatchLease,
    ToolDispatchResult,
    ToolDispatchTarget,
    ToolImplementationObservation,
    tool_receipt_digest,
)
from jimu_runtime.inspection.media import (
    LocalMediaStore,
    MaskSidecarRecord,
    MediaError,
    ToolSidecarSpec,
)
from jimu_runtime.tools.audit import TOOL_NON_KNOWN_DIAGNOSTIC
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.jobs import CompletionDisposition, DurableJobs
from jimu_runtime.workflow.postgres import create_postgres_engine
from jimu_runtime.workflow.scheduling import SchedulingLimits
from tests.contract.test_wp04_dispatch_journal import _FIXTURE, _start
from tests.contract.test_wp05_media_postgres import _DIMENSIONS, publish, ref
from tests.fixtures.wp01c.executions.support import execution, node
from tests.fixtures.wp03.jobs.support import (
    branch,
    budget,
    database,
    fetch_node,
    outcome,
)
from tests.unit.test_wp05_media_decoder import pixels

_TASK = Path(__file__).parents[1] / "fixtures/wp01c/intake/accepted-task-valid.json"
_RETENTION = RetentionPolicyRef.model_validate_json(
    json.dumps(
        {
            "evidence_policy_ref": {
                "artifact_kind": "policy",
                "artifact_id": "mask-evidence",
                "version": "1.0.0",
                "digest": "d" * 64,
            },
            "retention_class": {
                "vocabulary_ref": {
                    "artifact_kind": "vocabulary",
                    "artifact_id": "retention",
                    "version": "1.0.0",
                    "digest": "e" * 64,
                },
                "member": "short",
            },
        }
    )
)


async def ready_tool(engine, task):
    scope = task.idempotency_scope
    execution_id = "exec-1"
    limits = SchedulingLimits(
        schema_version="1.0.0", version="1.0.0", default=budget(), overrides=()
    )
    submission = TaskSubmission(
        schema_version=task.schema_version,
        request_id=task.request_id,
        idempotency_scope=scope,
        idempotency_key=task.idempotency_key,
        media_ref=task.media_ref,
        context=task.context,
        trace=task.trace,
    )
    async with engine.begin() as connection:
        now = (await connection.scalar(text("SELECT clock_timestamp()"))).astimezone(
            UTC
        )
        stamp = now.isoformat()
        record = execution(
            created_at=stamp, task_id=task.task_id, execution_id=execution_id
        )
        pins = record.pins.model_copy(
            update={"release_id": task.release_id, "bundle_digest": task.bundle_digest}
        )
        record = record.model_copy(update={"pins": pins})
        root = node(
            execution_id=execution_id,
            node_id="N01",
            status="pending",
            upstream_inputs=[],
            attempt_history=[],
            output_refs=[],
            created_at=stamp,
            started_at=None,
        )
        intent = await repo.reserve_intent(
            connection, scope, submission, issuer="test", subject="worker"
        )
        await repo.accept_intent(
            connection,
            scope,
            intent,
            task,
            record,
            root.config_ref,
            limits,
            root,
            repo.NewJob("job-root", now, limits.for_node(NodeId.N01)),
        )

    async def transition(connection, scope, locked, run, attempt, result, now):
        if run.node_id is NodeId.N01:
            await branch(connection, scope, locked, run, now, NodeId.N05, "media-tool")

    jobs = DurableJobs(engine, on_terminal=transition)
    first = await jobs.claim(scope, "context-worker", 15_000, execution_id=execution_id)
    attempt = await jobs.start(first)
    run = await fetch_node(engine, scope, execution_id, first.node_run_id)
    assert (
        await jobs.complete(first, outcome(attempt, run))
        is CompletionDisposition.APPLIED
    )
    return execution_id, await _start(
        engine,
        jobs,
        scope,
        execution_id,
        "media-tool-worker",
        node_id=NodeId.N05,
    )


def tool_contract(
    execution_id, attempt, node, *, step="wheel_segmentation", input_refs=()
):
    fixture = json.loads(_FIXTURE.read_bytes())
    template = fixture["tools"]["step"]
    template.update(
        tool_execution_id="mask-dispatch",
        execution_id=execution_id,
        trace_id=node.trace.trace_id,
        started_at=attempt.started_at.isoformat(),
        completed_at=attempt.started_at.isoformat(),
    )
    template["attempt_ref"]["record_id"] = attempt.attempt_id
    template["node_run_ref"]["record_id"] = node.node_run_id
    template["invocation_origin"]["step_id"] = step
    template["input_refs"] = [ref.model_dump(mode="json") for ref in input_refs]
    tool = ToolExecution.model_validate_json(json.dumps(template))
    input_schema = ToolInputSchemaRef.model_validate(
        fixture["tool"]["definition"]["input_schema_ref"]
    )
    output_schema = ToolOutputSchemaRef.model_validate(
        fixture["tool"]["definition"]["output_schema_ref"]
    )
    proof = TrustPin(identity="cpu-test", version="1.0.0", sha256="a" * 64)
    intent = DispatchIntent(
        dispatch_id=tool.tool_execution_id,
        operation_id="wheel-mask-operation",
        trace_id=node.trace.trace_id,
        request_digest="5" * 64,
        input_digest="6" * 64,
        response_contract_digest=output_schema.digest,
        resource_lease=DispatchLease(
            lease_id="cpu-lease", owner_id="media-tool-worker", scope_id="cpu"
        ),
        target=ToolDispatchTarget(
            tool_ref=tool.tool_ref,
            invocation_origin=tool.invocation_origin,
            profile_ref=tool.profile_ref,
            object_ref=tool.object_ref,
            capability_id="cpu-inspection",
            contract_version="1.0.0",
            implementation_version="1.0.0",
            implementation_proof_ref=proof,
            input_schema_ref=input_schema,
            output_schema_ref=output_schema,
            input_refs=tool.input_refs,
        ),
    )
    return intent, template, proof


def typed_record(record):
    def project(name, value):
        if isinstance(value, dict):
            return typed_record(value)
        if isinstance(value, int):
            return {"kind": "integer", "value": value}
        return {"kind": "enum" if name == "frame" else "string", "value": value}

    return {
        "kind": "record",
        "value": {name: project(name, value) for name, value in record.items()},
    }


@pytest.mark.parametrize(
    "state",
    [
        "known",
        "recovered",
        "unknown",
        "missing_diagnostic",
        "unknown_payload",
        "unknown_scalar_payload",
        "missing_state_ref",
    ],
)
def test_named_mask_proves_original_grid_scope_and_attempt_output(tmp_path, state):
    async def run():
        async with database() as (engine, url):
            store = LocalMediaStore(tmp_path, engine)
            original = pixels()
            media_ref = ref(original)
            task = AcceptedTask.model_validate_json(_TASK.read_bytes()).model_copy(
                update={"media_ref": media_ref}
            )
            scope = task.idempotency_scope
            await publish(store, scope, original)
            execution_id, (claim, attempt, node) = await ready_tool(engine, task)
            admission = DispatchJournal(engine)
            intent, template, proof = tool_contract(execution_id, attempt, node)
            dispatch_id = intent.dispatch_id
            assert await admission.begin(claim, attempt.attempt_id, intent)
            frame = (await store.verified_task_original(scope, task)).coordinate_system
            target = CoordinateSystem(
                frame="derived_image",
                coordinate_system_id="wheel-mask",
                dimensions=_DIMENSIONS,
            )
            transform = TransformRecord(
                source=frame,
                target=target,
                implementation_id="wheel-segmentation",
                implementation_version="1.0.0",
                affine=(1, 0, 0, 0, 1, 0),
            )
            mask = pixels("L", color=255)
            mask_record = MaskSidecarRecord(
                coordinate_system=frame,
                evidence_id="evidence-wheel-mask",
                sha256=ref(mask).sha256,
                source_media_sha256=media_ref.sha256,
            )
            spec = ToolSidecarSpec(
                evidence_id=mask_record.evidence_id,
                access="task",
                retention=_RETENTION,
                spatial=DerivedImageEvidence(
                    kind="derived_image",
                    original_media_ref=media_ref,
                    transforms=(transform,),
                ),
                mask_record=mask_record,
            )

            async def stage(draft, content=mask, slot="wheel_mask"):
                return await store.stage_tool_sidecar(
                    scope, execution_id, dispatch_id, slot, draft, content
                )

            with pytest.raises(MediaError, match="tool_sidecar_slot_mismatch"):
                await stage(spec, slot="tactile_paving_mask")
            with pytest.raises(MediaError, match="media_binary_mask_required"):
                await stage(spec, pixels("L", color=17))
            with pytest.raises(MediaError, match="mask_sidecar_record_mismatch"):
                await stage(
                    replace(
                        spec,
                        mask_record=mask_record.model_copy(update={"sha256": "b" * 64}),
                    )
                )
            with pytest.raises(MediaError, match="mask_sidecar_record_required"):
                await stage(replace(spec, mask_record=None))
            moved = transform.model_copy(update={"affine": (1, 0, 1, 0, 1, 0)})
            with pytest.raises(MediaError, match="mask_sidecar_original_grid_required"):
                await stage(
                    replace(
                        spec,
                        spatial=spec.spatial.model_copy(
                            update={"transforms": (moved,)}
                        ),
                    )
                )
            stale = transform.model_copy(
                update={
                    "source": frame.model_copy(
                        update={"coordinate_system_id": "another"}
                    )
                }
            )
            with pytest.raises(
                MediaError, match="tool_sidecar_original_frame_mismatch"
            ):
                await stage(
                    replace(
                        spec,
                        spatial=spec.spatial.model_copy(
                            update={"transforms": (stale,)}
                        ),
                    )
                )

            assets = await asyncio.gather(stage(spec), stage(spec))
            asset = assets[0]
            assert assets == [asset, asset]
            assert asset.access_scope.task_id == task.task_id
            assert asset.scope.object_ref == intent.target.object_ref
            with pytest.raises(MediaError, match="dispatch_media_identity_conflict"):
                await stage(replace(spec, access="project"))
            with pytest.raises(MediaError, match="tool_sidecar_output_unavailable"):
                await store.read_tool_sidecar(
                    scope, execution_id, dispatch_id, "wheel_mask"
                )

            if state == "recovered":
                old_dispatch_id = dispatch_id
                old_asset = asset
                competing = intent.model_copy(
                    update={
                        "dispatch_id": "mask-probe",
                        "operation_id": "mask-probe-operation",
                    }
                )
                assert await admission.begin(claim, attempt.attempt_id, competing)
                with pytest.raises(MediaError, match="dispatch_media_evidence_in_use"):
                    await store.stage_tool_sidecar(
                        scope,
                        execution_id,
                        competing.dispatch_id,
                        "wheel_mask",
                        spec,
                        mask,
                    )
                jobs = DurableJobs(engine)
                assert (
                    await jobs.complete(
                        claim, outcome(attempt, node, result="transport")
                    )
                    is CompletionDisposition.RETRY_SCHEDULED
                )
                await asyncio.sleep(0.05)
                claim, attempt, node = await _start(
                    engine,
                    jobs,
                    scope,
                    execution_id,
                    "media-tool-worker",
                    node_id=NodeId.N05,
                )
                intent, template, proof = tool_contract(execution_id, attempt, node)
                dispatch_id = "mask-dispatch-retried"
                intent = intent.model_copy(update={"dispatch_id": dispatch_id})
                template["tool_execution_id"] = dispatch_id
                assert await admission.begin(claim, attempt.attempt_id, intent)
                asset = await stage(spec)
                assert asset.evidence_id == old_asset.evidence_id
                assert asset.storage != old_asset.storage
                assert (tmp_path / old_asset.storage.object_key).read_bytes() == mask
                with pytest.raises(MediaError, match="tool_sidecar_output_unavailable"):
                    await store.read_tool_sidecar_audit(
                        scope, execution_id, old_dispatch_id, "wheel_mask"
                    )

            evidence_ref = EvidenceRef(
                record_kind=RuntimeRecordKind.EVIDENCE, record_id=asset.evidence_id
            ).model_dump(mode="json")
            result = template["attempt_result"]["result"]
            template["evidence_refs"] = [evidence_ref]
            result["evidence_refs"] = [evidence_ref]
            result["evidence"] = [
                {"name": "wheel_mask", "evidence_refs": [evidence_ref]}
            ]
            if state in {"known", "recovered", "unknown_payload"}:
                result["payload"] = typed_record(
                    {"wheel_mask": mask_record.model_dump(mode="json")}
                )
            if state == "unknown_scalar_payload":
                result["payload"] = {"kind": "bool", "value": True}
            output = {
                "output_name": "wheel_mask",
                "evidence_refs": [] if state == "missing_state_ref" else [evidence_ref],
            }
            if state in {"known", "recovered"}:
                output["fact_status"] = "known"
            else:
                template["status"] = result["status"] = "partial"
                output.update(
                    fact_status="unknown",
                    reason={
                        "reason_code": "tool_reported_unknown",
                        "message": TOOL_NON_KNOWN_DIAGNOSTIC,
                        "diagnostic_refs": []
                        if state == "missing_diagnostic"
                        else [evidence_ref],
                    },
                )
            result["output_states"] = [output]
            tool = ToolExecution.model_validate_json(json.dumps(template))
            receipt = ToolDispatchResult(
                boundary="acknowledged",
                acknowledgement="tool_process",
                implementation=ToolImplementationObservation(
                    implementation_version="1.0.0",
                    proof_ref=proof,
                ),
                execution=tool,
                tool_execution_digest=tool_receipt_digest(tool),
            )
            assert await admission.append_result(
                claim, attempt.attempt_id, dispatch_id, receipt
            )
            with pytest.raises(MediaError, match="tool_sidecar_output_unavailable"):
                await store.read_tool_sidecar(
                    scope, execution_id, dispatch_id, "wheel_mask"
                )
            async with engine.begin() as connection:
                await repo.store_output(
                    connection,
                    scope,
                    execution_id,
                    claim.node_run_id,
                    attempt.attempt_id,
                    output_key=f"evidence.{asset.evidence_id}",
                    output_kind="evidence",
                    record=asset,
                )
            recovery = create_postgres_engine(url)
            try:
                reopened = LocalMediaStore(tmp_path, recovery)
                if state == "missing_state_ref":
                    with pytest.raises(
                        MediaError, match="mask_sidecar_output_state_mismatch"
                    ):
                        await reopened.read_tool_sidecar_audit(
                            scope, execution_id, dispatch_id, "wheel_mask"
                        )
                    return
                if state in {"known", "recovered"}:
                    saved = await reopened.read_tool_sidecar(
                        scope, execution_id, dispatch_id, "wheel_mask"
                    )
                    assert saved.asset == asset and saved.content == mask
                else:
                    with pytest.raises(
                        MediaError, match="mask_sidecar_output_not_known"
                    ):
                        await reopened.read_tool_sidecar(
                            scope,
                            execution_id,
                            dispatch_id,
                            "wheel_mask",
                            expected_asset=asset,
                        )
                    if state != "unknown":
                        with pytest.raises(
                            MediaError, match="mask_sidecar_diagnostic_mismatch"
                        ):
                            await reopened.read_tool_sidecar_audit(
                                scope, execution_id, dispatch_id, "wheel_mask"
                            )
                        return
                audit = await reopened.read_tool_sidecar_audit(
                    scope, execution_id, dispatch_id, "wheel_mask"
                )
                assert audit.asset == asset and audit.content == mask
                assert audit.fact_status is (
                    FactStatus.KNOWN
                    if state in {"known", "recovered"}
                    else FactStatus.UNKNOWN
                )
                if state == "unknown":
                    path = tmp_path / asset.storage.object_key
                    path.chmod(0o600)
                    path.write_bytes(mask[:-1] + bytes([mask[-1] ^ 1]))
                    with pytest.raises(MediaError, match="media_content_mismatch"):
                        await reopened.read_tool_sidecar_audit(
                            scope, execution_id, dispatch_id, "wheel_mask"
                        )
                other_object = asset.scope.object_ref.model_copy(
                    update={"record_id": "object-other"}
                )
                foreign_asset = asset.model_copy(
                    update={
                        "scope": asset.scope.model_copy(
                            update={"object_ref": other_object}
                        )
                    },
                )
                with pytest.raises(
                    MediaError, match="tool_sidecar_bound_asset_mismatch"
                ):
                    await reopened.read_tool_sidecar(
                        scope,
                        execution_id,
                        dispatch_id,
                        "wheel_mask",
                        expected_asset=foreign_asset,
                    )
                with pytest.raises(MediaError, match="media_dispatch_unavailable"):
                    await reopened.read_tool_sidecar(
                        scope.model_copy(update={"project_id": "foreign"}),
                        execution_id,
                        dispatch_id,
                        "wheel_mask",
                    )
            finally:
                await recovery.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutate", ["none", "wrong_object", "wrong_source", "bad_receipt"]
)
def test_geometry_bytes_must_match_physical_dispatch_and_receipt(tmp_path, mutate):
    async def run():
        async with database() as (engine, _):
            store = LocalMediaStore(tmp_path, engine)
            original = pixels()
            media_ref = ref(original)
            task = AcceptedTask.model_validate_json(_TASK.read_bytes()).model_copy(
                update={"media_ref": media_ref}
            )
            scope = task.idempotency_scope
            await publish(store, scope, original)
            execution_id, (claim, attempt, node) = await ready_tool(engine, task)
            names = ("wheel_mask", "tactile_paving_mask")
            sources = {
                name: ToolArtifactRef.model_validate_json(
                    json.dumps(
                        {
                            "artifact_ref": {
                                "record_kind": "evidence",
                                "record_id": f"source-{name}",
                            },
                            "digest": str(index) * 64,
                        }
                    ),
                )
                for index, name in enumerate(names)
            }
            intent, template, proof = tool_contract(
                execution_id,
                attempt,
                node,
                step="wheel_boundary_geometry",
                input_refs=tuple(sources.values()),
            )
            dispatch_id = intent.dispatch_id
            journal = DispatchJournal(engine)
            assert await journal.begin(claim, attempt.attempt_id, intent)
            source_frame = (
                await store.verified_task_original(scope, task)
            ).coordinate_system
            frame = CoordinateSystem(
                coordinate_system_id="geometry-grid",
                frame="derived_image",
                dimensions=_DIMENSIONS,
            )
            stdout = b'{"metrics":{"crossing":0,"inside":0,"outside":1,"overlap":0,"tactile":3,"touching":0,"wheel":1},"reason":null,"relation":"outside_parking_area"}'
            observed = json.loads(stdout)
            document = {
                "schema_version": "1.0.0",
                "tool_execution_id": dispatch_id,
                "execution_id": execution_id,
                "attempt_id": attempt.attempt_id,
                "object_ref": intent.target.object_ref.model_dump(mode="json"),
                "profile_ref": intent.target.profile_ref.model_dump(mode="json"),
                "request_sha256": intent.request_digest,
                "response_sha256": sha256(stdout).hexdigest(),
                "implementation": proof.model_dump(mode="json"),
                "parking_area": {"source_asset_sha256": "0" * 64},
                "original_sha256": media_ref.sha256,
                "inputs": {
                    name: {
                        "evidence_id": item.artifact_ref.record_id,
                        "sha256": item.digest,
                    }
                    for name, item in sources.items()
                },
                **observed,
            }
            if mutate == "wrong_object":
                document["object_ref"]["record_id"] = "alien-object"
            elif mutate == "wrong_source":
                document["inputs"]["wheel_mask"]["sha256"] = "f" * 64
            body = json.dumps(document).encode()
            spec = ToolSidecarSpec(
                evidence_id="produced-geometry",
                spatial=DerivedImageEvidence(
                    kind="derived_image",
                    original_media_ref=media_ref,
                    transforms=(
                        TransformRecord(
                            source=source_frame,
                            target=frame,
                            implementation_id="test-geometry",
                            implementation_version="1.0.0",
                            affine=(1, 0, 0, 0, 1, 0),
                        ),
                    ),
                ),
                retention=_RETENTION,
                access="task",
                geometry_digest=sha256(body).hexdigest(),
            )

            async def stage(geometry_spec):
                return await store.stage_tool_sidecar(
                    scope,
                    execution_id,
                    dispatch_id,
                    "geometry",
                    geometry_spec,
                    body,
                )

            if mutate in {"wrong_object", "wrong_source"}:
                error = (
                    "geometry_sidecar_dispatch_mismatch"
                    if mutate == "wrong_object"
                    else "geometry_sidecar_input_mismatch"
                )
                with pytest.raises(MediaError, match=error):
                    await stage(spec)
                return
            with pytest.raises(MediaError, match="geometry_sidecar_digest_mismatch"):
                await stage(replace(spec, geometry_digest="f" * 64))
            asset = await stage(spec)
            evidence_ref = {"record_kind": "evidence", "record_id": asset.evidence_id}
            named = [
                {
                    "name": name,
                    "evidence_refs": [item.artifact_ref.model_dump(mode="json")],
                }
                for name, item in sources.items()
            ]
            refs = [evidence_ref, *(row["evidence_refs"][0] for row in named)]
            result = template["attempt_result"]["result"]
            template["evidence_refs"] = refs
            template["output_refs"] = [
                {"artifact_ref": evidence_ref, "digest": asset.sha256}
            ]
            result.update(
                evidence=[
                    *named,
                    {"name": "geometry", "evidence_refs": [evidence_ref]},
                ],
                evidence_refs=refs,
                output_states=[
                    {
                        "output_name": "wheel_boundary_relation",
                        "fact_status": "known",
                        "evidence_refs": refs,
                    }
                ],
                payload={
                    "kind": "record",
                    "value": {
                        "wheel_boundary_relation": {
                            "kind": "enum",
                            "value": "outside_parking_area",
                        }
                    },
                },
            )
            produced = ToolExecution.model_validate_json(json.dumps(template))
            assert await journal.append_result(
                claim,
                attempt.attempt_id,
                dispatch_id,
                ToolDispatchResult(
                    boundary="acknowledged",
                    acknowledgement="tool_process",
                    implementation=ToolImplementationObservation(
                        implementation_version="1.0.0",
                        proof_ref=proof,
                    ),
                    response_digest="f" * 64
                    if mutate == "bad_receipt"
                    else sha256(stdout).hexdigest(),
                    observed_response_bytes=len(stdout),
                    execution=produced,
                    tool_execution_digest=tool_receipt_digest(produced),
                ),
            )
            async with engine.begin() as connection:
                await repo.store_output(
                    connection,
                    scope,
                    execution_id,
                    claim.node_run_id,
                    attempt.attempt_id,
                    output_key=f"evidence.{asset.evidence_id}",
                    output_kind="evidence",
                    record=asset,
                )
            if mutate == "bad_receipt":
                with pytest.raises(
                    MediaError, match="geometry_sidecar_output_mismatch"
                ):
                    await store.read_tool_sidecar(
                        scope, execution_id, dispatch_id, "geometry"
                    )
            else:
                sidecar = await store.read_tool_sidecar(
                    scope, execution_id, dispatch_id, "geometry"
                )
                assert sidecar.asset == asset and sidecar.content == body

    asyncio.run(run())
