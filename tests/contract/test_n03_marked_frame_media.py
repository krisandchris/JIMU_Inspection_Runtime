"""Verify physical frame durability and isolation against PostgreSQL authority."""

import asyncio
import json
import os
from hashlib import sha256

import psycopg
import pytest
from pydantic import ValidationError

from jimu_runtime.domain.enums import NodeRunStatus, RuntimeRecordKind
from jimu_runtime.domain.nodes.envelopes import ArtifactOutputRef
from jimu_runtime.domain.references import RuntimeRecordRef
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.media import BBoxXYXY, CoordinateSystem, MediaRef
from jimu_runtime.domain.runtime.perception import MarkedGroundingFrame, ObjectRecord
from jimu_runtime.domain.validation.transitions import NodeTransitionGuard
from jimu_runtime.gateway.http_wire import VerifiedImage
from jimu_runtime.inspection.media import LocalMediaStore, MediaError
from jimu_runtime.inspection.media.group_frame import marked_group_image
from jimu_runtime.inspection.media.persistence import _key
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.jobs import CompletionDisposition, DurableJobs
from jimu_runtime.workflow.mock_support import record_digest
from ops.recovery.contracts import MEDIA, FileWitness, RecoveryError
from ops.recovery.media_validation import _verify_marked_frames
from tests.contract.test_wp04_dispatch_journal import _start
from tests.fixtures.wp01c.nodes.cases import cases
from tests.fixtures.wp03.jobs.support import outcome
from tests.fixtures.wp05.perception import IMAGE, perception_fixture


def test_marked_frame_attempt_authority_and_durable_cleanup(tmp_path):
    async def run():
        async with perception_fixture(
            tmp_path, "http://127.0.0.1:1/v1/chat/completions", with_media=True
        ) as fixture:
            engine = fixture.application
            store = fixture.media_store
            assert store is not None
            task = fixture.accepted.task
            execution_id = fixture.accepted.execution.execution_id
            scope = task.idempotency_scope

            async def transition(connection, scope, locked, run, attempt, result, now):
                if run.node_id is NodeId.N01:
                    pending = run.model_copy(
                        update={
                            "node_run_id": "node-frame",
                            "node_id": NodeId.N03,
                            "created_at": now,
                            "status": NodeRunStatus.PENDING,
                            "started_at": None,
                            "ended_at": None,
                            "attempt_history": (),
                            "output_refs": (),
                        }
                    )
                    await repo.insert_node(
                        connection, scope, execution_id, pending, branch_key="frame"
                    )
                    await repo.transition_node(
                        connection,
                        scope,
                        execution_id,
                        pending,
                        pending.model_copy(update={"status": NodeRunStatus.READY}),
                        now=now,
                        guard=NodeTransitionGuard.PREREQUISITES_AVAILABLE,
                    )
                    await repo.insert_job(
                        connection,
                        scope,
                        execution_id,
                        pending.node_run_id,
                        repo.NewJob(
                            "job-frame",
                            now,
                            locked.scheduling_limits.for_node(NodeId.N03),
                        ),
                    )
                    assert await repo.link_successor(
                        connection,
                        scope,
                        execution_id,
                        run.node_run_id,
                        pending.node_run_id,
                    )

            jobs = DurableJobs(engine, on_terminal=transition)
            root_claim, root_attempt, root_run = await _start(
                engine, jobs, scope, execution_id, "root", node_id=NodeId.N01
            )
            assert (
                await jobs.complete(root_claim, outcome(root_attempt, root_run))
                is CompletionDisposition.APPLIED
            )
            first, failed_attempt, first_run = await _start(
                engine, jobs, scope, execution_id, "first", node_id=NodeId.N03
            )
            original = await store.read_original(scope, task.media_ref)
            dimensions = original.asset.dimensions
            members = (
                BBoxXYXY(xyxy=(1.0, 2.0, 4.0, 5.0)),
                BBoxXYXY(xyxy=(5.0, 6.0, 8.0, 9.0)),
            )
            box = BBoxXYXY.enclosing(members)
            image = await marked_group_image(
                VerifiedImage(IMAGE, "image/png", task.media_ref.sha256),
                dimensions,
                box,
            )
            frame = MarkedGroundingFrame(
                media_ref=MediaRef(media_id="marked-image", sha256=image.sha256),
                byte_size=len(image.content),
                mime_type="image/png",
                renderer_version="1.0.0",
            )
            obj = ObjectRecord.model_validate(
                ObjectRecord.model_validate_json(
                    json.dumps(cases()["object_record"])
                ).model_copy(
                    update={
                        "execution_id": execution_id,
                        "source_media_ref": task.media_ref,
                        "coordinate_system": CoordinateSystem(
                            coordinate_system_id=task.media_ref.media_id,
                            frame="original_image",
                            dimensions=dimensions,
                        ),
                        "bbox": box,
                        "grounding_member_boxes": members,
                        "marked_frame": frame,
                    }
                )
            )
            file_root = tmp_path / "media"

            async def stage(scope, attempt, record=obj, payload=image):
                await store.stage_marked_frame(
                    scope,
                    record,
                    payload,
                    node_run_id=attempt.owner.node_run_ref.record_id,
                    attempt_id=attempt.attempt_id,
                )

            def frame_path(attempt, record=obj):
                return (
                    file_root
                    / "media-v1"
                    / _key(
                        "marked-frame",
                        scope.project_id,
                        scope.client_id,
                        execution_id,
                        attempt.owner.node_run_ref.record_id,
                        attempt.attempt_id,
                        record.object_id,
                    )
                )

            for foreign in (
                scope.model_copy(update={"project_id": "foreign"}),
                scope.model_copy(update={"client_id": "foreign"}),
            ):
                with pytest.raises(
                    MediaError, match="marked_frame_attempt_unavailable"
                ):
                    await stage(foreign, failed_attempt)
            with pytest.raises(MediaError, match="marked_frame_attempt_unavailable"):
                await stage(scope, root_attempt)
            with pytest.raises(MediaError, match="marked_frame_content_mismatch"):
                await stage(
                    scope,
                    failed_attempt,
                    payload=VerifiedImage(IMAGE, "image/png", image.sha256),
                )

            await stage(scope, failed_attempt)
            orphan = frame_path(failed_attempt)
            os.utime(orphan, (1, 1))
            assert (await store.cleanup_unbound_files(minimum_age=1)).removed == 0
            with pytest.raises(MediaError, match="marked_frame_unavailable"):
                await store.read_marked_frame(scope, obj)
            assert (
                await jobs.complete(
                    first, outcome(failed_attempt, first_run, result="transport")
                )
                is CompletionDisposition.RETRY_SCHEDULED
            )
            assert (await store.cleanup_unbound_files(minimum_age=1)).removed == 1
            assert not orphan.exists()
            with pytest.raises(MediaError, match="marked_frame_attempt_unavailable"):
                await stage(scope, failed_attempt)

            await asyncio.sleep(0.025)
            second, attempt, second_run = await _start(
                engine, jobs, scope, execution_id, "retry", node_id=NodeId.N03
            )
            await asyncio.gather(stage(scope, attempt), stage(scope, attempt))
            bound = frame_path(attempt)
            superseded = obj.model_copy(update={"object_id": "superseded-frame-object"})
            await stage(scope, attempt, superseded)
            obsolete = frame_path(attempt, superseded)
            unmarked = ObjectRecord.model_validate(
                superseded.model_dump(
                    exclude={"marked_frame", "grounding_member_boxes"}
                )
            )
            assert "marked_frame" not in json.loads(unmarked.stable_json())
            with pytest.raises(
                ValidationError, match="must be absent rather than null"
            ):
                ObjectRecord.model_validate_json(
                    json.dumps(
                        {**json.loads(unmarked.stable_json()), "marked_frame": None}
                    )
                )
            different = VerifiedImage(IMAGE, "image/png", sha256(IMAGE).hexdigest())
            changed = obj.model_copy(
                update={
                    "marked_frame": frame.model_copy(
                        update={
                            "media_ref": frame.media_ref.model_copy(
                                update={"sha256": different.sha256}
                            ),
                            "byte_size": len(different.content),
                        }
                    )
                }
            )
            with pytest.raises(MediaError, match="marked_frame_identity_conflict"):
                await stage(scope, attempt, changed, different)
            assert bound.read_bytes() == image.content

            async def persist(connection, scope, locked, node, attempt, output, now):
                for record in (obj, unmarked):
                    storage_digest = await repo.store_output(
                        connection,
                        scope,
                        locked.execution.execution_id,
                        node.node_run_id,
                        attempt.attempt_id,
                        output_key=f"object.{record.object_id}",
                        output_kind="object",
                        record=record,
                    )
                    assert storage_digest != record_digest(record)

            assert (
                await jobs.complete(
                    second,
                    outcome(attempt, second_run).model_copy(
                        update={
                            "output_artifacts": tuple(
                                ArtifactOutputRef(
                                    ref=RuntimeRecordRef(
                                        record_kind=RuntimeRecordKind.OBJECT,
                                        record_id=record.object_id,
                                    ),
                                    digest=record_digest(record),
                                )
                                for record in (obj, unmarked)
                            )
                        }
                    ),
                    persist=persist,
                )
                is CompletionDisposition.APPLIED
            )
            os.utime(bound, (1, 1))
            os.utime(obsolete, (1, 1))
            assert (await store.cleanup_unbound_files(minimum_age=1)).removed == 1
            assert not obsolete.exists()
            reopened = LocalMediaStore(file_root, engine)
            assert await reopened.read_marked_frame(scope, obj) == image
            assert (
                await reopened.read_original(scope, task.media_ref)
            ).content == IMAGE
            with pytest.raises(MediaError, match="original_media_unavailable"):
                await reopened.read_original(scope, frame.media_ref)
            with pytest.raises(MediaError, match="marked_frame_unavailable"):
                await reopened.read_marked_frame(
                    scope.model_copy(update={"client_id": "foreign"}), obj
                )
            with pytest.raises(MediaError, match="marked_frame_identity_conflict"):
                await reopened.read_marked_frame(scope, changed)

            def verify_recovery():
                conninfo = engine.url.set(drivername="postgresql").render_as_string(
                    hide_password=False
                )
                with psycopg.connect(conninfo) as connection:
                    witnesses = {
                        f"{MEDIA}/{bound.name}": FileWitness(
                            sha256=image.sha256, size=len(image.content)
                        ),
                    }
                    _verify_marked_frames(connection, witnesses)
                    with connection.transaction(force_rollback=True):
                        key = _key(
                            "marked-frame",
                            scope.project_id,
                            scope.client_id,
                            execution_id,
                            attempt.owner.node_run_ref.record_id,
                            attempt.attempt_id,
                            "wrong-store-object",
                        )
                        connection.execute(
                            """
                            INSERT INTO jimu_workflow.marked_frame_media
                                (project_id, client_id, execution_id, node_run_id, attempt_id,
                                 object_id, object_sha256, media_id, store_id, file_key,
                                 content_sha256, byte_size)
                            SELECT project_id, client_id, execution_id, node_run_id, attempt_id,
                                'wrong-store-object', object_sha256, media_id, 'foreign-store', %s,
                                content_sha256, byte_size
                            FROM jimu_workflow.marked_frame_media WHERE file_key = %s
                            """,
                            (key, bound.name),
                        )
                        with pytest.raises(
                            RecoveryError,
                            match="committed_media_file_missing_or_changed",
                        ):
                            _verify_marked_frames(
                                connection,
                                {
                                    **witnesses,
                                    f"{MEDIA}/{key}": witnesses[
                                        f"{MEDIA}/{bound.name}"
                                    ],
                                },
                            )
                    with pytest.raises(
                        RecoveryError, match="committed_media_file_missing_or_changed"
                    ):
                        _verify_marked_frames(connection, {})

            await asyncio.to_thread(verify_recovery)
            bound.chmod(0o600)
            bound.write_bytes(image.content[:-1] + bytes([image.content[-1] ^ 1]))
            with pytest.raises(MediaError, match="media_content_mismatch"):
                await reopened.read_marked_frame(scope, obj)

    asyncio.run(run())
