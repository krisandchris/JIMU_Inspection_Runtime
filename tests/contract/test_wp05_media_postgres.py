"""Use physical media and isolated PostgreSQL with real scoped workflow keys."""

import asyncio
import json
import multiprocessing
import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import ImageDimensions, MediaRef
from jimu_runtime.gateway.invocation import TransportResult
from jimu_runtime.gateway.journal import DispatchJournal, ModelDispatchResult
from jimu_runtime.inspection.media import (
    LocalMediaStore,
    MediaError,
    MediaLimits,
    persistence,
)
from jimu_runtime.inspection.media.persistence import _key
from jimu_runtime.workflow.jobs import DurableJobs
from jimu_runtime.workflow.postgres import create_postgres_engine
from tests.contract.test_wp04_dispatch_journal import (
    _ERROR,
    _intent,
    _model_outcome,
    _ready,
    _start,
)
from tests.fixtures.wp03.jobs.support import database, wait_for_db_time
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.unit.test_wp05_media_decoder import pixels

_DIMENSIONS = ImageDimensions(width=4, height=3)


def ref(content, media_id="photo"):
    return MediaRef(media_id=media_id, sha256=sha256(content).hexdigest())


async def publish(store, scope, content, media_id="photo"):
    return await store.publish_original(
        scope,
        ref(content, media_id),
        mime_type="image/png",
        dimensions=_DIMENSIONS,
        content=content,
    )


def _reread_in_new_process(url, root, scope, media_ref, sender):
    async def run():
        engine = create_postgres_engine(url)
        try:
            reopened = LocalMediaStore(root, engine)
            asset = await reopened.read_original(scope, media_ref)
            sender.send((asset.asset, asset.content))
        finally:
            await engine.dispose()
            sender.close()

    asyncio.run(run())


async def _require_process_reopen(url, root, scope, asset, content):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_reread_in_new_process,
        args=(url, root, scope, asset.media_ref, sender),
    )
    process.start()
    sender.close()
    try:
        assert await asyncio.to_thread(receiver.poll, 10)
        assert receiver.recv() == (asset, content)
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 10)
        receiver.close()
        process.close()


def test_original_retry_scope_and_reopen(tmp_path):
    async def run():
        async with database() as (engine, url):
            _, scope, _ = await _ready(engine)
            store = LocalMediaStore(tmp_path, engine)
            content = pixels()
            different = pixels(color="white")
            results = await asyncio.gather(
                *(publish(store, scope, content) for _ in range(5))
            )
            first = results[0]
            assert all(item == first for item in results)
            assert first.stage.object_version == f"sha256:{ref(content).sha256}"
            assert (tmp_path / first.storage.object_key).read_bytes() == content
            assert (
                await store.cleanup_unbound_original(scope, first.media_ref.media_id)
                is False
            )

            for foreign in (
                scope.model_copy(update={"project_id": "other"}),
                scope.model_copy(update={"client_id": "other"}),
            ):
                with pytest.raises(MediaError, match="original_media_unavailable"):
                    await store.read_original(foreign, first.media_ref)
                asset = await publish(store, foreign, different)
                assert asset.storage != first.storage
                assert (
                    await store.read_original(foreign, asset.media_ref)
                ).content == different
            with pytest.raises(MediaError, match="original_media_identity_conflict"):
                await publish(store, scope, different)

            digest = _key("original", scope.project_id, scope.client_id, "rollback")
            directory = tmp_path / "media-v1"
            (directory / digest).write_bytes(b"files left by a rolled-back publication")
            (directory / f"{digest}.stage").write_bytes(b"partial")
            assert await store.cleanup_unbound_original(scope, "rollback")
            assert not (directory / digest).exists()
            assert not (directory / f"{digest}.stage").exists()
            await publish(store, scope, content, "rollback")

            await _require_process_reopen(url, tmp_path, scope, first, content)
            recovery = create_postgres_engine(url)
            try:
                reopened = LocalMediaStore(tmp_path, recovery)
                result = await reopened.read_original(scope, first.media_ref)
                assert result.asset == first and result.content == content
                path = tmp_path / first.storage.object_key
                path.chmod(0o600)
                path.write_bytes(different.ljust(len(content), b"0")[: len(content)])
                with pytest.raises(MediaError, match="media_content_mismatch"):
                    await reopened.read_original(scope, first.media_ref)
                with pytest.raises(MediaError, match="media_content_mismatch"):
                    await publish(reopened, scope, content)
                assert not await reopened.cleanup_unbound_original(
                    scope, first.media_ref.media_id
                )
            finally:
                await recovery.dispose()

    asyncio.run(run())


def test_original_rejects_invalid_bytes_claims_and_competing_content(tmp_path):
    async def run():
        async with database() as (engine, _):
            _, scope, _ = await _ready(engine)
            store = LocalMediaStore(tmp_path, engine)
            content = pixels()
            different = pixels(color="white")
            with pytest.raises(MediaError, match="media_content_mismatch"):
                await store.publish_original(
                    scope,
                    ref(different),
                    mime_type="image/png",
                    dimensions=_DIMENSIONS,
                    content=content,
                )
            with pytest.raises(MediaError, match="media_dimensions_mismatch"):
                await store.publish_original(
                    scope,
                    ref(content),
                    mime_type="image/png",
                    dimensions=ImageDimensions(width=3, height=4),
                    content=content,
                )
            bounded = LocalMediaStore(
                tmp_path, engine, limits=MediaLimits(max_original_bytes=10)
            )
            with pytest.raises(MediaError, match="media_size_limit"):
                await publish(bounded, scope, content)
            winners = await asyncio.gather(
                publish(store, scope, content),
                publish(store, scope, different),
                return_exceptions=True,
            )
            assert sum(isinstance(item, MediaError) for item in winners) == 1
            (winner,) = [
                item for item in winners if not isinstance(item, BaseException)
            ]
            assert (await store.read_original(scope, winner.media_ref)).content in {
                content,
                different,
            }

    asyncio.run(run())


def response(inner):
    return json.dumps(
        {
            "choices": [
                {
                    "message": {"content": inner.decode()},
                    "index": 0,
                    "finish_reason": "stop",
                }
            ]
        },
        indent=2,
    ).encode()


def test_model_bodies_require_exact_receipt_and_preserve_distinct_roles(tmp_path):
    async def run():
        async with database() as (engine, url):
            jobs, scope, execution = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, execution, "media-model"
            )
            journal = DispatchJournal(engine)
            store = LocalMediaStore(tmp_path, engine)
            intent = _intent(node.trace.trace_id, "media-model-reply").model_copy(
                update={"retain_complete_response": True}
            )
            assert await journal.begin(claim, attempt.attempt_id, intent)
            parsed = b'{ "verdict": true }'
            raw = response(parsed)
            transport = TransportResult(
                True,
                response_status=200,
                response_bytes=raw,
                parsed_content=parsed,
                response_digest=sha256(raw).hexdigest(),
                observed_response_bytes=len(raw),
                validated_response_contract_digest=intent.response_contract_digest,
                response_dispatch_id=intent.dispatch_id,
                normalized_response_digest=sha256(parsed).hexdigest(),
                normalized_response_bytes=len(parsed),
            )
            mismatched = b"{}"
            with pytest.raises(MediaError, match="model_response_normalized_mismatch"):
                await store.stage_model_response(
                    scope,
                    execution,
                    intent.dispatch_id,
                    replace(
                        transport,
                        parsed_content=mismatched,
                        normalized_response_digest=sha256(mismatched).hexdigest(),
                        normalized_response_bytes=len(mismatched),
                    ),
                )
            with pytest.raises(MediaError, match="model_response_contract_mismatch"):
                await store.stage_model_response(
                    scope,
                    execution,
                    intent.dispatch_id,
                    replace(transport, validated_response_contract_digest="f" * 64),
                )
            await store.stage_model_response(
                scope, execution, intent.dispatch_id, transport
            )
            await store.stage_model_response(
                scope, execution, intent.dispatch_id, transport
            )
            with pytest.raises(MediaError, match="model_response_receipt_unavailable"):
                await store.read_model_response(
                    scope, execution, intent.dispatch_id, late=False
                )
            receipt = _model_outcome(
                intent, attempt, node, raw, "model-request"
            ).model_copy(
                update={
                    "normalized_response_digest": sha256(parsed).hexdigest(),
                    "normalized_response_bytes": len(parsed),
                }
            )
            assert await journal.append_result(
                claim, attempt.attempt_id, intent.dispatch_id, receipt
            )
            recovery = create_postgres_engine(url)
            try:
                reopened = LocalMediaStore(tmp_path, recovery)
                verified = await reopened.read_model_response(
                    scope, execution, intent.dispatch_id, late=False
                )
                assert verified.raw == raw and verified.normalized == parsed
                assert verified.receipt.response_digest == sha256(raw).hexdigest()
                assert (
                    await reopened.cleanup_unbound_dispatch(
                        scope, execution, intent.dispatch_id, "model_raw"
                    )
                    is False
                )
                with pytest.raises(MediaError, match="media_dispatch_unavailable"):
                    await reopened.read_model_response(
                        scope.model_copy(update={"client_id": "foreign"}),
                        execution,
                        intent.dispatch_id,
                        late=False,
                    )
            finally:
                await recovery.dispose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("omission", "late", "retained_normalized"),
    [
        ("policy_disabled", False, True),
        ("credential_echo", False, False),
        ("unvalidated", True, False),
        (None, True, True),
    ],
)
def test_model_diagnostics_survive_incomplete_or_suppressed_roles(
    tmp_path, omission, late, retained_normalized
):
    async def run():
        async with database() as (engine, _):
            jobs, scope, execution = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, execution, "media-model"
            )
            journal = DispatchJournal(engine)
            store = LocalMediaStore(tmp_path, engine)
            intent = _intent(node.trace.trace_id, "media-model-reply").model_copy(
                update={"retain_complete_response": omission is None}
            )
            assert await journal.begin(claim, attempt.attempt_id, intent)
            if late:
                interrupted = ModelDispatchResult(
                    boundary="sent_unknown",
                    error=_ERROR,
                    response_bytes_omission_reason="incomplete",
                )
                assert await journal.append_result(
                    claim, attempt.attempt_id, intent.dispatch_id, interrupted
                )
                before = await store.read_model_response(
                    scope, execution, intent.dispatch_id, late=False
                )
                assert before.raw is None and before.normalized is None
                assert before.receipt.response_digest is None
            parsed = b'{"verdict":true}'
            raw = response(parsed)
            retained_raw = raw if omission is None else None
            error = None if retained_normalized else _ERROR
            transport = TransportResult(
                True,
                error=error,
                response_status=200,
                response_bytes=retained_raw,
                parsed_content=parsed if retained_normalized else None,
                response_digest=sha256(raw).hexdigest(),
                observed_response_bytes=len(raw),
                validated_response_contract_digest=intent.response_contract_digest
                if retained_normalized
                else None,
                response_bytes_omission_reason=omission,
                response_dispatch_id=intent.dispatch_id,
                normalized_response_digest=sha256(parsed).hexdigest()
                if retained_normalized
                else None,
                normalized_response_bytes=len(parsed) if retained_normalized else None,
            )
            await store.stage_model_response(
                scope, execution, intent.dispatch_id, transport
            )
            if retained_normalized:
                receipt = _model_outcome(
                    intent, attempt, node, raw, "model-request"
                ).model_copy(
                    update={
                        "response_bytes_omission_reason": omission,
                        "normalized_response_digest": sha256(parsed).hexdigest(),
                        "normalized_response_bytes": len(parsed),
                    }
                )
            else:
                receipt = ModelDispatchResult(
                    boundary="acknowledged",
                    acknowledgement="http_headers",
                    response_status=200,
                    error=error,
                    response_digest=sha256(raw).hexdigest(),
                    observed_response_bytes=len(raw),
                    response_bytes_omission_reason=omission,
                )
            assert await journal.append_result(
                claim, attempt.attempt_id, intent.dispatch_id, receipt, late=late
            )
            observed = await store.read_model_response(
                scope, execution, intent.dispatch_id, late=late
            )
            assert observed.raw == retained_raw
            assert observed.normalized == (parsed if retained_normalized else None)
            assert observed.receipt.response_bytes_omission_reason == omission
            assert observed.late is late

    asyncio.run(run())


def test_publication_failure_compensates_only_its_uncommitted_file(
    tmp_path, monkeypatch
):
    async def run():
        async with database() as (engine, _):
            _, scope, _ = await _ready(engine)
            store = LocalMediaStore(tmp_path, engine)
            content = pixels()
            old = await publish(store, scope, content, "healthy")
            publish_file = store._bytes.publish

            def fail_after_publish(*args, **kwargs):
                publish_file(*args, **kwargs)
                raise MediaError("simulated_publication_failure")

            with monkeypatch.context() as patch:
                patch.setattr(store._bytes, "publish", fail_after_publish)
                with pytest.raises(MediaError, match="simulated_publication_failure"):
                    await publish(store, scope, content, "interrupted")
            assert [
                entry
                for entry in (tmp_path / "media-v1").iterdir()
                if not entry.name.startswith(".")
            ] == [tmp_path / old.storage.object_key]
            with pytest.raises(MediaError, match="original_media_unavailable"):
                await store.read_original(scope, ref(content, "interrupted"))
            retry = await publish(store, scope, content, "interrupted")
            assert (
                await store.read_original(scope, retry.media_ref)
            ).content == content

    asyncio.run(run())


def test_response_policy_pins_dispatch_and_distinguishes_missing_roles(tmp_path):
    async def run():
        async with database() as (engine, _):
            jobs, scope, execution = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, execution, "media-model"
            )
            journal = DispatchJournal(engine)
            store = LocalMediaStore(tmp_path, engine)
            parsed = b'{"verdict":true}'
            raw = response(parsed)

            def transport(intent, **data):
                return TransportResult(
                    True,
                    response_status=200,
                    response_digest=sha256(raw).hexdigest(),
                    observed_response_bytes=len(raw),
                    validated_response_contract_digest=intent.response_contract_digest,
                    parsed_content=parsed,
                    normalized_response_digest=sha256(parsed).hexdigest(),
                    normalized_response_bytes=len(parsed),
                    **data,
                )

            disabled = _intent(node.trace.trace_id, "disabled").model_copy(
                update={
                    "retain_complete_response": False,
                    "operation_id": "disabled-operation",
                }
            )
            enabled = _intent(node.trace.trace_id, "enabled").model_copy(
                update={
                    "retain_complete_response": True,
                    "operation_id": "enabled-operation",
                }
            )
            for intent in (disabled, enabled):
                assert await journal.begin(claim, attempt.attempt_id, intent)
            with pytest.raises(MediaError, match="model_response_retention_mismatch"):
                await store.stage_model_response(
                    scope,
                    execution,
                    disabled.dispatch_id,
                    transport(
                        disabled,
                        response_bytes=raw,
                        response_dispatch_id=disabled.dispatch_id,
                    ),
                )
            with pytest.raises(MediaError, match="model_response_dispatch_mismatch"):
                await store.stage_model_response(
                    scope,
                    execution,
                    enabled.dispatch_id,
                    transport(
                        enabled,
                        response_bytes=raw,
                        response_dispatch_id=disabled.dispatch_id,
                    ),
                )
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT count(*) FROM jimu_workflow.dispatch_media")
                    )
                    == 0
                )
            # Binding absent after an independent receipt append is observably missing.
            receipt = _model_outcome(enabled, attempt, node, raw, "enabled-response")
            assert await journal.append_result(
                claim, attempt.attempt_id, enabled.dispatch_id, receipt
            )
            with pytest.raises(MediaError, match="model_response_raw_unavailable"):
                await store.read_model_response(
                    scope, execution, enabled.dispatch_id, late=False
                )
            # Raw may be repaired from an exact observation, but legacy receipts
            # give no normalized proof even when that binding is present.
            await store.stage_model_response(
                scope,
                execution,
                enabled.dispatch_id,
                transport(
                    enabled,
                    response_bytes=raw,
                    response_dispatch_id=enabled.dispatch_id,
                ),
            )
            loaded = await store.read_model_response(
                scope, execution, enabled.dispatch_id, late=False
            )
            assert loaded.raw == raw and loaded.normalized is None

            # A successfully retained normalized body must match the authority
            # receipt even if raw was deliberately suppressed.
            safe = transport(
                disabled,
                response_bytes_omission_reason="policy_disabled",
                response_dispatch_id=disabled.dispatch_id,
            )
            await store.stage_model_response(
                scope, execution, disabled.dispatch_id, safe
            )
            wrong = _model_outcome(
                disabled, attempt, node, raw, "disabled-response"
            ).model_copy(
                update={
                    "response_bytes_omission_reason": "policy_disabled",
                    "normalized_response_digest": "a" * 64,
                    "normalized_response_bytes": len(parsed),
                }
            )
            assert await journal.append_result(
                claim, attempt.attempt_id, disabled.dispatch_id, wrong
            )
            with pytest.raises(MediaError, match="model_response_normalized_mismatch"):
                await store.read_model_response(
                    scope, execution, disabled.dispatch_id, late=False
                )

    asyncio.run(run())


def test_file_lock_survives_real_pg_session_termination(tmp_path, monkeypatch):
    async def run():
        async with database() as (engine, url):
            _, scope, _ = await _ready(engine)
            store = LocalMediaStore(tmp_path, engine)
            retry_engine = create_postgres_engine(url)
            retry_store = LocalMediaStore(tmp_path, retry_engine)
            await retry_store._ensure_database()
            paused, resume = Event(), Event()
            backend = None
            actual_lock, actual_publish = persistence._locked, store._bytes.publish

            async def track(connection, key):
                nonlocal backend
                await actual_lock(connection, key)
                if backend is None:
                    backend = await connection.scalar(text("SELECT pg_backend_pid()"))

            def interrupted_publish(*args, **kwargs):
                paused.set()
                assert resume.wait(10)
                actual_publish(*args, **kwargs)

            monkeypatch.setattr(persistence, "_locked", track)
            monkeypatch.setattr(store._bytes, "publish", interrupted_publish)
            original = pixels()
            replacement = pixels(color="white")
            first = asyncio.create_task(publish(store, scope, original, "race"))
            second = None
            try:
                assert await asyncio.to_thread(paused.wait, 5)
                async with retry_engine.begin() as connection:
                    assert await connection.scalar(
                        text("SELECT pg_terminate_backend(:pid)"), {"pid": backend}
                    )
                second = asyncio.create_task(
                    publish(retry_store, scope, replacement, "race")
                )
                await asyncio.sleep(0.15)
                assert not second.done()
                resume.set()
                with pytest.raises(DBAPIError):
                    await first
                retained = await second
                opened = await retry_store.read_original(scope, retained.media_ref)
                assert opened.content == replacement
                assert not await store.cleanup_unbound_original(scope, "race")
            finally:
                resume.set()
                await asyncio.gather(
                    *(task for task in (first, second) if task is not None),
                    return_exceptions=True,
                )
                await retry_engine.dispose()

    asyncio.run(run())


def test_gc_discovers_aged_keys_and_root_is_bound_to_real_database(tmp_path):
    async def run():
        async with database() as (engine, _):
            _, scope, _ = await _ready(engine)
            root = tmp_path / "uncreated" / "store"
            store = LocalMediaStore(root, engine)
            retained = await publish(store, scope, pixels(), "protected")
            directory = root / "media-v1"
            victim = directory / f"{'a' * 64}.stage"
            victim.write_bytes(b"staged by a killed writer")
            os.utime(victim, (1, 1))
            os.utime(root / retained.storage.object_key, (1, 1))
            first = await store.cleanup_unbound_files(minimum_age=1, scan_limit=1)
            assert first.scanned == 1 and first.next_key is not None
            second = await store.cleanup_unbound_files(
                minimum_age=1, scan_limit=1, after_key=first.next_key
            )
            assert second.scanned == 1 and second.next_key is None
            assert first.removed + second.removed == 1
            assert not victim.exists()
            assert (root / retained.storage.object_key).exists()
            with isolated_postgres() as foreign:
                external = create_postgres_engine(foreign.application_url)
                try:
                    different = LocalMediaStore(root, external)
                    with pytest.raises(
                        MediaError, match="media_database_identity_mismatch"
                    ):
                        await different.cleanup_unbound_files(minimum_age=1)
                finally:
                    await external.dispose()

    asyncio.run(run())


def test_cancelled_queued_decode_never_runs(tmp_path, monkeypatch):
    async def run():
        async with database() as (engine, _):
            _, scope, _ = await _ready(engine)
            store = LocalMediaStore(
                tmp_path, engine, limits=MediaLimits(max_parallel_decodes=1)
            )
            entered, release = Event(), Event()
            calls = []
            decode = persistence.verify_image

            def blocked(content, *args, **kwargs):
                calls.append(content)
                if len(calls) == 1:
                    entered.set()
                    assert release.wait(8)
                return decode(content, *args, **kwargs)

            monkeypatch.setattr(persistence, "verify_image", blocked)
            active = asyncio.create_task(publish(store, scope, pixels(), "active"))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                queued = asyncio.create_task(
                    publish(store, scope, pixels(color="white"), "queued")
                )
                await asyncio.sleep(0.1)
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
            finally:
                release.set()
                await active
            assert len(calls) == 1

    asyncio.run(run())


def test_scoped_original_metadata_reopens_a_pinned_asset_with_missing_bytes(tmp_path):
    async def run():
        async with database() as (engine, url):
            _, scope, _ = await _ready(engine)
            store = LocalMediaStore(tmp_path, engine)
            content = pixels()
            stored = await publish(store, scope, content)
            assert await store.verified_original(scope, stored.media_ref) == stored
            task = AcceptedTask.model_validate_json(
                (
                    Path(__file__).parents[1]
                    / "fixtures/wp01c/intake/accepted-task-valid.json"
                ).read_bytes()
            ).model_copy(update={"media_ref": stored.media_ref})
            assert task.idempotency_scope == scope
            (tmp_path / stored.storage.object_key).unlink()
            recovery = create_postgres_engine(url)
            try:
                reopened = LocalMediaStore(tmp_path, recovery)
                assert (
                    await reopened.scoped_original_metadata(scope, stored.media_ref)
                    == stored
                )
                for foreign in (
                    scope.model_copy(update={"project_id": "another-project"}),
                    scope.model_copy(update={"client_id": "another-client"}),
                ):
                    with pytest.raises(MediaError, match="original_media_unavailable"):
                        await reopened.scoped_original_metadata(
                            foreign, stored.media_ref
                        )
                wrong_digest = ref(pixels(color="white"))
                with pytest.raises(
                    MediaError, match="original_media_identity_conflict"
                ):
                    await reopened.scoped_original_metadata(scope, wrong_digest)
                with pytest.raises(MediaError, match="media_file_unavailable"):
                    await reopened.verified_original(scope, stored.media_ref)
                with pytest.raises(MediaError, match="media_file_unavailable"):
                    await reopened.verified_task_original(scope, task)
            finally:
                await recovery.dispose()

    asyncio.run(run())


def test_observation_request_bytes_reuse_shared_scoped_immutable_storage(tmp_path):
    async def run():
        async with database() as (engine, url):
            jobs, scope, execution_id = await _ready(engine, node_id=NodeId.N04)
            first, first_attempt, _ = await _start(
                engine,
                jobs,
                scope,
                execution_id,
                "first-observation",
                duration=1000,
                node_id=NodeId.N04,
            )
            store = LocalMediaStore(tmp_path, engine)
            evidence_id = "request.local"
            body = b"compiled trusted request"
            digest = sha256(body).hexdigest()
            copies = await asyncio.gather(
                *(
                    store.stage_observation_request(
                        scope, execution_id, evidence_id, body
                    )
                    for _ in range(3)
                )
            )
            storage, staged_at = copies[0]
            assert all(item == (storage, staged_at) for item in copies)
            with pytest.raises(
                MediaError, match="observation_request_media_identity_conflict"
            ):
                await store.stage_observation_request(
                    scope, execution_id, evidence_id, b"other"
                )
            with pytest.raises(MediaError, match="media_size_limit"):
                await store.stage_observation_request(
                    scope, execution_id, "too-large", b"x" * (1048576 + 1)
                )
            foreign = scope.model_copy(update={"client_id": "another-client"})
            with pytest.raises(
                MediaError, match="observation_request_media_unavailable"
            ):
                await store.stage_observation_request(
                    foreign, execution_id, evidence_id, body
                )
            recovery = create_postgres_engine(url)
            try:
                reopened = LocalMediaStore(tmp_path, recovery)
                assert (
                    await reopened.read_observation_request(
                        scope, execution_id, evidence_id, storage, digest, len(body)
                    )
                ) == body
                with pytest.raises(
                    MediaError, match="observation_request_media_unavailable"
                ):
                    await reopened.read_observation_request(
                        foreign, execution_id, evidence_id, storage, digest, len(body)
                    )
                with pytest.raises(
                    MediaError, match="observation_request_media_identity_conflict"
                ):
                    await reopened.read_observation_request(
                        scope, execution_id, evidence_id, storage, "a" * 64, len(body)
                    )
                physical = tmp_path / storage.object_key
                os.utime(physical, (1, 1))
                assert (
                    await reopened.cleanup_unbound_files(minimum_age=1)
                ).removed == 0
                assert physical.read_bytes() == body
                await wait_for_db_time(engine, first.lease_until)
                assert await jobs.reconcile(scope, execution_id)
                await asyncio.sleep(0.025)
                second, second_attempt, _ = await _start(
                    engine,
                    jobs,
                    scope,
                    execution_id,
                    "second-observation",
                    node_id=NodeId.N04,
                )
                assert second.fence != first.fence
                assert second_attempt.attempt_id != first_attempt.attempt_id
                assert await reopened.stage_observation_request(
                    scope, execution_id, evidence_id, body
                ) == (storage, staged_at)
                assert not await reopened.cleanup_unreferenced_observation_request(
                    scope, execution_id, evidence_id
                )
                physical.unlink()
                with pytest.raises(MediaError, match="media_file_unavailable"):
                    await reopened.read_observation_request(
                        scope, execution_id, evidence_id, storage, digest, len(body)
                    )
                orphan_storage, _ = await reopened.stage_observation_request(
                    scope, execution_id, "request.orphan", b"unsubmitted prompt"
                )
                jobs = DurableJobs(engine)
                assert await jobs.request_cancel(scope, execution_id)
                assert await jobs.reconcile(scope, execution_id)
                assert not await reopened.cleanup_unreferenced_observation_request(
                    foreign, execution_id, "request.orphan"
                )
                page = await reopened.cleanup_unreferenced_observation_requests(
                    scope, execution_id
                )
                assert page.scanned == 2 and page.removed == 2 and page.next_key is None
                assert not (tmp_path / orphan_storage.object_key).exists()
                with pytest.raises(
                    MediaError, match="observation_request_media_unavailable"
                ):
                    await reopened.stage_observation_request(
                        scope, execution_id, "request.orphan", b"unsubmitted prompt"
                    )
            finally:
                await recovery.dispose()

    asyncio.run(run())
