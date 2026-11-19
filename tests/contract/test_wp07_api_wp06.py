"""Bind HTTP replay and evidence routes to the actual WP-06 lifecycle services."""

import asyncio
import json
import secrets
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Action, Identity
from jimu_runtime.api.boundary import ApiLimits
from jimu_runtime.api.lifecycle import LifecycleEvidence
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.runtime.replay import (
    ExecutionRef,
    InferenceReplayRequest,
    ReplayExecutionSource,
)
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow.intake import (
    IntakeAuthorizationError,
    IntakeIdentity,
    IntakeService,
)
from jimu_runtime.workflow.worker import WorkerStep
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05.perception import _LIMITS, IMAGE, perception_fixture

loopback_only = http_support.loopback_only


def test_exact_evidence_and_both_replay_routes(
    tmp_path: Path, loopback_only: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jimu_runtime.inspection.media.retention import LocalEvidenceLifecycle
    from jimu_runtime.inspection.media.retention_models import (
        EvidenceAccessGrant,
        EvidenceLocator,
    )
    from jimu_runtime.replay.artifact import ArtifactReplayService
    from jimu_runtime.replay.inference import InferenceReplayService

    replies = {
        "profile_discovery-result": {"profile_hits": ["nonmotor_vehicle"]},
        "object_grounding-result": {"boxes": [{"xyxy": [40, 23, 52, 33]}]},
        "object_observation-result": {
            "vehicle_type": "electric_bicycle",
            "parking_state": "parked",
            "parking_position": "over_boundary",
        },
        "candidate_prediction-result": {"categories": []},
    }
    calls: Counter[str] = Counter()

    async def peer(reader, writer):
        received = await http_support.read_request(reader)
        body = json.loads(received.body)
        kind = body["response_format"]["json_schema"]["schema"]["$id"].split(":")[-2]
        calls[kind] += 1
        raw = (
            http_support.chat_body(
                model=body["model"], content=json.dumps(replies[kind])
            )
            + b" " * 1024
        )
        writer.write(
            http_support.response(
                raw,
                headers=(
                    (b"X-Request-ID", f"api-fixture-{calls.total()}".encode()),
                    (b"X-Jimu-Deployment-Version", b"1.0.0"),
                    (b"X-Jimu-Model-Version", b"1.0.0"),
                ),
            )
        )
        await writer.drain()

    async def run() -> None:
        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
                task, execution = fixture.accepted.task, fixture.accepted.execution
                scope, eid = task.idempotency_scope, execution.execution_id
                app_db = fixture.application
                media = fixture.media_store
                assert media is not None
                catalogue = fixture.scene.execution.catalogue
                definitions = tuple(
                    require_definition(catalogue, item.ref, ToolDefinition)
                    for item in catalogue.of_kind(ArtifactKind.TOOL)
                )
                registry = ToolWorkerRegistry(
                    synthetic_cpu_implementations(
                        fixture.resources,
                        "cpu",
                        "inspection-tools",
                        definitions,
                        fixture.tool_proofs,
                        synthetic_image_digests=frozenset({task.media_ref.sha256}),
                    )
                )
                tools = GeometryNodeAdapter(
                    media,
                    ToolExecutionAdapter(
                        ToolExecutor(registry),
                        StepAttemptStore(app_db, actor=ACTOR),
                        fixture.journal,
                        fixture.host.capabilities,
                    ),
                )

                async def capture(request_scope, accepted_task):
                    source = await fixture.host.worker_source(
                        request_scope, accepted_task
                    )
                    return source.device, source.camera

                worker = inspection_worker(
                    app_db,
                    fixture.releases,
                    fixture.store,
                    host=VerifiedMediaWorkerHost(
                        media, fixture.host.capabilities, capture
                    ),
                    bundle_limits=LIMITS,
                    media=media,
                    gateway=fixture.gateway,
                    execute_tools=tools.execute,
                    model_resource_scope_id="model",
                )
                for i in range(20):
                    step = await worker.run_once(
                        scope, eid, owner=f"api-wp06-{i}", lease_ms=10000
                    )
                    if step is WorkerStep.FINALIZED:
                        break
                assert step is WorkerStep.FINALIZED
                assert calls == dict.fromkeys(replies, 1)

                source = ReplayExecutionSource(
                    execution_ref=ExecutionRef(record_kind="execution", record_id=eid),
                    task_id=task.task_id,
                    pins=execution.pins,
                )
                artifact = ArtifactReplayService(app_db)
                artifact_request = await artifact.prepare_request(scope, source)
                inference_request = InferenceReplayRequest(
                    schema_version="1.0.0",
                    kind="inference",
                    source=source,
                    target_pins=source.pins,
                )
                principal = fixture.host.principal
                token = secrets.token_urlsafe(32)
                grant = {
                    Action.ACCESS_SCOPE,
                    Action.TASK_READ,
                    Action.TASK_CANCEL,
                    Action.ARTIFACT_REPLAY,
                    Action.INFERENCE_REPLAY,
                    Action.EVIDENCE_READ,
                    Action.EVIDENCE_NORMALIZED,
                    Action.EXECUTION_READ,
                }

                class Authority:
                    async def authenticate(self, credential):
                        return (
                            Identity(principal, ACTOR) if credential == token else None
                        )

                    async def allows(self, identity, target):
                        return (
                            identity.principal is principal
                            and target.scope == scope
                            and target.action in grant
                            and target.task_id in (None, task.task_id)
                        )

                class ReplayHost:
                    async def authorize_inference_replay(
                        self, authenticated, request_scope, request, original
                    ):
                        if (
                            authenticated is not principal
                            or request_scope != scope
                            or original != task
                        ):
                            raise IntakeAuthorizationError()
                        return IntakeIdentity(
                            issuer="https://fixture.invalid/replay",
                            subject=ACTOR.actor_id,
                        )

                    async def verified_original(self, request_scope, ref):
                        return await media.verified_original(request_scope, ref)

                async def no_disposition(_task, _policy):
                    raise AssertionError("HTTP read cannot perform a disposition")

                evidence = LifecycleEvidence(
                    LocalEvidenceLifecycle(media, no_disposition),
                    lambda selected_scope, actor, task_id: EvidenceAccessGrant(
                        scope=selected_scope,
                        actor=actor,
                        task_id=task_id,
                    ),
                    lambda locator: EvidenceLocator.model_validate(
                        locator.model_dump()
                    ),
                )
                intake = IntakeService(
                    app_db,
                    fixture.releases,
                    fixture.store,
                    host=fixture.host,
                    bundle_limits=LIMITS,
                    scheduling_limits=_LIMITS,
                    max_submission_bytes=65536,
                )
                api = create_app(
                    app_db,
                    intake,
                    authority=Authority(),
                    artifact_replay=artifact,
                    inference_replay=InferenceReplayService(
                        app_db,
                        fixture.releases,
                        fixture.store,
                        host=ReplayHost(),
                        bundle_limits=LIMITS,
                    ),
                    evidence=evidence,
                )
                audit_read = AsyncMock(wraps=evidence.read_audit)
                monkeypatch.setattr(evidence, "read_audit", audit_read)
                assert api.openapi()["paths"]["/api/v1/evidence/{evidence_id}"]["get"][
                    "security"
                ] == [
                    {"RuntimeBearer": []},
                ]
                async with app_db.connect() as conn:
                    original = (
                        await conn.execute(
                            text("""
                        SELECT node_run_id, attempt_id, output_digest, record->>'evidence_id' AS evidence_id
                        FROM jimu_workflow.produced_outputs
                        WHERE project_id = :project_id AND client_id = :client_id
                          AND execution_id = :eid AND output_kind = 'evidence'
                          AND record->>'evidence_type' = 'original_image'
                        LIMIT 1
                    """),
                            {**scope.model_dump(), "eid": eid},
                        )
                    ).one()
                    journal = (
                        await conn.execute(
                            text("""
                        SELECT dispatch_id, record->>'response_digest' AS raw_digest,
                            record->>'normalized_response_digest' AS normalized_digest
                        FROM jimu_workflow.dispatch_results
                        WHERE project_id = :project_id AND client_id = :client_id
                          AND execution_id = :eid
                          AND NOT late AND record->>'normalized_response_digest' IS NOT NULL
                        LIMIT 1
                    """),
                            {**scope.model_dump(), "eid": eid},
                        )
                    ).one()
                headers = {
                    "Authorization": "Bearer " + token,
                    "X-Jimu-Project": scope.project_id,
                    "X-Jimu-Client": scope.client_id,
                    "Content-Type": "application/json",
                }
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1/api/v1/",
                    transport=httpx.ASGITransport(app=api),
                    headers=headers,
                ) as client:
                    locator = {
                        "node_run_id": original.node_run_id,
                        "attempt_id": original.attempt_id,
                        "execution_id": eid,
                        "digest": original.output_digest,
                    }
                    verified = await client.get(
                        f"evidence/{original.evidence_id}", params=locator
                    )
                    assert verified.status_code == 200, verified.text
                    assert verified.content == IMAGE
                    assert verified.headers["content-disposition"] == "attachment"

                    audit = f"evidence/{journal.dispatch_id}"
                    normalized = await client.get(
                        audit,
                        params={
                            "execution_id": eid,
                            "representation": "normalized",
                            "digest": journal.normalized_digest,
                        },
                    )
                    assert normalized.status_code == 200, normalized.text
                    raw = {
                        "execution_id": eid,
                        "representation": "raw",
                        "digest": journal.raw_digest,
                    }
                    assert (await client.get(audit, params=raw)).status_code == 404
                    grant.add(Action.EVIDENCE_RAW)
                    allowed = await client.get(audit, params=raw)
                    assert allowed.status_code == 200, allowed.text
                    assert allowed.headers["content-type"] == "application/octet-stream"
                    assert (
                        await client.get(audit, params={**raw, "digest": "0" * 64})
                    ).status_code == 404
                    before_denied_late = audit_read.await_count
                    assert (
                        await client.get(audit, params={**raw, "late": True})
                    ).status_code == 404
                    assert audit_read.await_count == before_denied_late

                    capped_api = create_app(
                        app_db,
                        intake,
                        authority=Authority(),
                        evidence=evidence,
                        limits=ApiLimits(evidence_bytes=128),
                    )
                    raw_read = AsyncMock(wraps=media._read_model_role)
                    monkeypatch.setattr(media, "_read_model_role", raw_read)
                    async with httpx.AsyncClient(
                        base_url="http://127.0.0.1/api/v1/",
                        transport=httpx.ASGITransport(app=capped_api),
                        headers=headers,
                    ) as limited:
                        bad = await limited.get(
                            audit, params={**raw, "digest": "0" * 64}
                        )
                        assert bad.status_code == 404
                        over = await limited.get(audit, params=raw)
                        assert over.status_code == 413
                        raw_read.assert_not_awaited()
                        file_read = Mock(wraps=media._bytes.read)
                        monkeypatch.setattr(type(media._bytes), "read", file_read)
                        over = await limited.get(
                            f"evidence/{original.evidence_id}", params=locator
                        )
                        assert over.status_code == 413
                        file_read.assert_not_called()

                    grant.remove(Action.ARTIFACT_REPLAY)
                    forbidden = await client.post(
                        f"executions/{eid}/artifact-replay",
                        content=artifact_request.stable_json(),
                        headers={"Idempotency-Key": "artifact-fixture"},
                    )
                    assert forbidden.status_code == 404
                    grant.add(Action.ARTIFACT_REPLAY)
                    rescored = await client.post(
                        f"executions/{eid}/artifact-replay",
                        content=artifact_request.stable_json(),
                        headers={"Idempotency-Key": "artifact-fixture"},
                    )
                    assert rescored.status_code == 200, rescored.text
                    assert rescored.json()["kind"] == "artifact"
                    assert calls == dict.fromkeys(replies, 1)
                    replayed = await client.post(
                        f"executions/{eid}/inference-replay",
                        content=inference_request.stable_json(),
                        headers={"Idempotency-Key": "inference-fixture"},
                    )
                    assert replayed.status_code == 202, replayed.text
                    target = replayed.json()["target_execution_ref"]["record_id"]
                    assert target != eid
                    assert (await client.get(f"executions/{target}")).json()[
                        "status"
                    ] == "initialized"
                    task_uri = f"inference/tasks/{task.task_id}"
                    assert (await client.post(task_uri + "/cancel")).status_code == 409
                    assert (await client.get(task_uri)).json()["execution"][
                        "execution_id"
                    ] == eid

    asyncio.run(run())
