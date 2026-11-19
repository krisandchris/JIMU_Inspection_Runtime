"""Regression cases from the independent media lifecycle review."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jimu_runtime.inspection.media.retention import (
    EvidenceLifecycleError,
    LocalEvidenceLifecycle,
)
from jimu_runtime.inspection.media.retention_models import (
    EvidenceDispositionCompletion,
    EvidenceDispositionIntent,
    EvidenceRetentionPin,
    EvidenceReviewRelease,
)
from tests.contract.test_wp06_retention_postgres import (
    _BODY,
    Scenario,
    database,
    policy,
)


def test_project_alias_cannot_widen_task_primary_access(tmp_path):
    async def run():
        async with database() as (engine, _):
            state = await Scenario(tmp_path, engine).initialize()
            pin, raw = policy("task-policy", 1)
            state.policies[pin.evidence_policy_ref.digest] = raw
            primary, asset = await state.archive("request.task-only", pin)
            alias, _ = await state.archive(
                "request.project-alias",
                pin,
                storage=asset.storage,
                project_access=True,
            )
            unrestricted = state.grant.model_copy(update={"task_id": None})
            assert (
                await state.life.read_evidence(unrestricted, primary)
            ).status == "denied"
            denied = await state.life.read_evidence(unrestricted, alias)
            assert (denied.status, denied.content) == ("denied", None)
            assert denied.reason == "evidence_primary_scope_unavailable"
            assert (await state.life.read_evidence(state.grant, alias)).content == _BODY
            with pytest.raises(
                EvidenceLifecycleError, match="retention_scope_unavailable"
            ):
                await state.life.place_hold(
                    unrestricted,
                    alias,
                    "untrusted-hold",
                    reason="review",
                    at=state.staged_at[asset.storage.object_key],
                )

    asyncio.run(run())


def test_request_declared_old_timestamp_cannot_shorten_first_durable_retention(
    tmp_path,
):
    async def run():
        async with database() as (engine, _):
            state = await Scenario(tmp_path, engine).initialize()
            pin, raw = policy("clock-policy", 1)
            state.policies[pin.evidence_policy_ref.digest] = raw
            locator, asset = await state.archive("request.backdated", pin)
            staged_at = state.staged_at[asset.storage.object_key]
            assert asset.stage.verified_at < staged_at
            await state.finish()
            assert (
                await state.life.dispose(state.grant, locator, at=staged_at)
            ).status == "retention_pending"
            result = await state.life.dispose(
                state.grant, locator, at=await state.due(asset)
            )
            assert result.intent.references[0].expires_at == staged_at + timedelta(
                seconds=1
            )

    asyncio.run(run())


def test_release_replay_conflict_and_invalid_release_keeps_current_protection(tmp_path):
    async def run():
        async with database() as (engine, _):
            state = await Scenario(tmp_path, engine).initialize()
            pin, raw = policy("hold-policy", 1)
            state.policies[pin.evidence_policy_ref.digest] = raw
            locator, asset = await state.archive("request.hold", pin)
            now = await state.due(asset)
            await state.life.place_hold(
                state.grant,
                locator,
                "protect-1",
                reason="review",
                at=now,
            )
            await state.life.release_hold(
                state.grant,
                state.execution,
                "protect-1",
                reason="resolved",
                at=now,
            )
            with pytest.raises(
                EvidenceLifecycleError, match="retention_hold_already_released"
            ):
                await state.life.place_hold(
                    state.grant,
                    locator,
                    "protect-1",
                    reason="review",
                    at=now,
                )
            hold = await state.life.place_hold(
                state.grant,
                locator,
                "protect-2",
                reason="review",
                at=now,
            )
            bad = EvidenceReviewRelease(
                hold_id=hold.hold_id,
                actor=state.grant.actor,
                reason="invalid_backdate",
                released_at=now - timedelta(seconds=1),
            )
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                    INSERT INTO jimu_workflow.evidence_retention_hold_releases
                        (project_id, client_id, execution_id, hold_id, record)
                    VALUES (:p, :c, :e, :h, CAST(:record AS json))
                """),
                    {
                        "p": state.scope.project_id,
                        "c": state.scope.client_id,
                        "e": state.execution,
                        "h": hold.hold_id,
                        "record": bad.stable_json(),
                    },
                )
            await state.finish()
            assert (
                await state.life.dispose(state.grant, locator, at=now)
            ).status == "review_hold"
            assert (state.root / asset.storage.object_key).read_bytes() == _BODY

    asyncio.run(run())


def test_real_application_role_cannot_add_alias_waiting_on_disposition(
    tmp_path, monkeypatch
):
    async def run():
        async with database() as (engine, _):
            first = await Scenario(tmp_path, engine).initialize()
            second = await Scenario(tmp_path, engine).initialize(suffix="late-alias")
            pin, raw = policy("release-policy", 1)
            first.policies[pin.evidence_policy_ref.digest] = raw
            locator, asset = await first.archive("request.target", pin)
            await first.finish()
            due = await first.due(asset)
            locked, resume = asyncio.Event(), asyncio.Event()
            physical = first.life._physical

            async def pause_before_intent(key, stored):
                if not locked.is_set():
                    locked.set()
                    await resume.wait()
                return await physical(key, stored)

            monkeypatch.setattr(first.life, "_physical", pause_before_intent)
            disposal = asyncio.create_task(
                first.life.dispose(first.grant, locator, at=due)
            )
            await asyncio.wait_for(locked.wait(), timeout=5)
            publisher = asyncio.create_task(
                second.archive("request.new-alias", pin, storage=asset.storage)
            )
            try:
                await asyncio.sleep(0.1)
                assert not publisher.done()
            finally:
                resume.set()
            assert (await disposal).status == "disposed"
            with pytest.raises(IntegrityError) as conflict:
                await publisher
            assert conflict.value.orig.sqlstate == "23514"
            assert not (first.root / asset.storage.object_key).exists()

    asyncio.run(run())


def test_wp05_terminal_reclaim_protects_existing_other_execution_alias(tmp_path):
    async def run():
        async with database() as (engine, _):
            first = await Scenario(tmp_path, engine).initialize()
            second = await Scenario(tmp_path, engine).initialize(suffix="alias-review")
            pin, _ = policy("alias-policy", 1)
            shared, _ = await first.media.stage_observation_request(
                first.scope,
                first.execution,
                "request.shared",
                _BODY,
            )
            absent, _ = await first.media.stage_observation_request(
                first.scope,
                first.execution,
                "request.unused",
                _BODY,
            )
            await second.archive("request.disjoint-name", pin, storage=shared)
            await first.finish()
            assert not await first.media.cleanup_unreferenced_observation_request(
                first.scope,
                first.execution,
                "request.shared",
            )
            assert (first.root / shared.object_key).read_bytes() == _BODY
            assert await first.media.cleanup_unreferenced_observation_request(
                first.scope,
                first.execution,
                "request.unused",
            )
            assert not (first.root / absent.object_key).exists()
            with pytest.raises(IntegrityError) as refused:
                await second.archive("request.reclaimed-alias", pin, storage=absent)
            assert refused.value.orig.sqlstate == "23514"

    asyncio.run(run())


def test_policy_wait_does_not_hold_file_or_execution_row(tmp_path):
    async def run():
        async with database() as (engine, _):
            state = await Scenario(tmp_path, engine).initialize()
            pin, raw = policy("slow-policy", 1)
            locator, asset = await state.archive("request.slow-policy", pin)
            await state.finish()
            now = await state.due(asset)
            reading, resume = asyncio.Event(), asyncio.Event()

            async def read_policy(*args):
                reading.set()
                await resume.wait()
                return raw

            state.life = LocalEvidenceLifecycle(state.media, read_policy)
            task = asyncio.create_task(state.life.dispose(state.grant, locator, at=now))
            await asyncio.wait_for(reading.wait(), timeout=5)
            try:
                async with engine.begin() as connection:
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT execution_id FROM jimu_workflow.executions FOR UPDATE NOWAIT"
                            )
                        )
                        == state.execution
                    )
                await asyncio.wait_for(
                    state.life.place_hold(
                        state.grant,
                        locator,
                        "placed-during-plan",
                        reason="review",
                        at=now,
                    ),
                    timeout=2,
                )
            finally:
                resume.set()
            assert (await task).status == "review_hold"
            assert (state.root / asset.storage.object_key).read_bytes() == _BODY

    asyncio.run(run())


def test_database_rejects_unelapsed_intent_and_empty_completion_and_checks_membership(
    tmp_path,
):
    async def run():
        async with database() as (engine, _):
            state = await Scenario(tmp_path, engine).initialize()
            pin, _ = policy("record-policy", 1)
            locator, asset = await state.archive("request.metadata", pin)
            other, _ = await state.archive(
                "request.metadata-alias", pin, storage=asset.storage
            )
            now = await state.due(asset)
            recorded = EvidenceDispositionIntent(
                storage=asset.storage,
                sha256=asset.sha256,
                byte_size=asset.byte_size,
                actor=state.grant.actor,
                requested_at=now,
                references=(
                    EvidenceRetentionPin(locator=other, retention=pin, expires_at=now),
                ),
            )
            params = {
                "p": state.scope.project_id,
                "c": state.scope.client_id,
                "e": state.execution,
                "s": asset.storage.store_id,
                "k": asset.storage.object_key.removeprefix("media-v1/"),
            }

            async def write(table, record):
                # Only these fixed fixture statements are allowed.
                query = {
                    "intent": text("""
                        INSERT INTO jimu_workflow.evidence_disposition_intents
                            (project_id, client_id, execution_id, store_id, file_key, record)
                        VALUES (:p, :c, :e, :s, :k, CAST(:data AS json))
                    """),
                    "completion": text("""
                        INSERT INTO jimu_workflow.evidence_disposition_completions
                            (project_id, client_id, execution_id, store_id, file_key, record)
                        VALUES (:p, :c, :e, :s, :k, CAST(:data AS json))
                    """),
                }[table]
                async with engine.begin() as connection:
                    await connection.execute(query, {**params, "data": record})

            premature = recorded.model_copy(
                update={
                    "requested_at": now - timedelta(seconds=1),
                }
            )
            with pytest.raises(IntegrityError) as invalid_intent:
                await write("intent", premature.stable_json())
            assert invalid_intent.value.orig.sqlstate == "23514"
            await write("intent", recorded.stable_json())
            with pytest.raises(IntegrityError) as invalid_completion:
                await write("completion", '{"schema_version":"1.0.0"}')
            assert invalid_completion.value.orig.sqlstate == "23514"
            await write(
                "completion",
                EvidenceDispositionCompletion(
                    outcome="removed",
                    completed_at=now,
                ).stable_json(),
            )
            result = await state.life.read_evidence(state.grant, locator)
            assert (
                result.status == "corrupt"
                and result.reason == "evidence_disposition_mismatch"
            )
            assert (state.root / asset.storage.object_key).read_bytes() == _BODY

    asyncio.run(run())


def test_independent_store_same_display_path_does_not_block_local_disposal(tmp_path):
    async def run():
        async with database() as (engine, _):
            first = await Scenario(tmp_path, engine).initialize()
            second = await Scenario(tmp_path, engine).initialize(
                suffix="different-store"
            )
            pin, raw = policy("store-policy", 1)
            first.policies[pin.evidence_policy_ref.digest] = raw
            locator, local = await first.archive("request.local", pin)
            external = local.storage.model_copy(update={"store_id": "external.store"})
            await second.archive("request.external", pin, storage=external)
            await first.finish()
            assert (
                await first.life.dispose(
                    first.grant,
                    locator,
                    at=await first.due(local),
                )
            ).status == "disposed"

    asyncio.run(run())
