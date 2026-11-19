"""Disposable PostgreSQL and physical local bytes for WP-06 evidence lifecycle."""

import asyncio
import json
import multiprocessing
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import (
    exercise_lifecycle,
)
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import AT, LIMITS
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.definition_enums import EvidenceType
from jimu_runtime.domain.enums import ArtifactKind
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    ExecutionEvidenceScope,
    NonSpatialEvidence,
    ObjectEvidenceScope,
    OriginalImageEvidence,
    ProjectEvidenceAccess,
    RetentionPolicyRef,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.facts import PolicyContentRef
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import (
    CoordinateSystem,
    FinalizedAsset,
    TransformRecord,
)
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.gateway.invocation import TransportResult
from jimu_runtime.gateway.journal import (
    DispatchJournal,
    ToolDispatchResult,
    ToolImplementationObservation,
    tool_receipt_digest,
)
from jimu_runtime.inspection.media import (
    LocalMediaStore,
    MaskSidecarRecord,
    ToolSidecarSpec,
)
from jimu_runtime.inspection.media.persistence import _DispatchKey
from jimu_runtime.inspection.media.retention import (
    EvidenceLifecycleError,
    LocalEvidenceLifecycle,
)
from jimu_runtime.inspection.media.retention_models import (
    EvidenceAccessGrant,
    EvidenceLocator,
)
from jimu_runtime.inspection.media.retention_policy import HistoricalEvidencePolicies
from jimu_runtime.workflow import migrations, repository
from jimu_runtime.workflow.jobs import CompletionDisposition, DurableJobs
from jimu_runtime.workflow.postgres import (
    _apply_catalogue,
    _read_catalogue,
    apply_migrations,
    create_postgres_engine,
)
from tests.contract.test_wp04_dispatch_journal import (
    _intent,
    _model_outcome,
    _ready,
    _start,
)
from tests.contract.test_wp05_media_postgres import _DIMENSIONS, publish, ref
from tests.contract.test_wp05_media_tool_sidecars import (
    ready_tool,
    tool_contract,
    typed_record,
)
from tests.fixtures.wp03.jobs.support import fetch_node, outcome, seed, wait_for_db_time
from tests.fixtures.wp03.postgres.support import isolated_postgres
from tests.unit.test_wp05_media_decoder import pixels

_ACTOR = ActorRef(
    actor_kind="service",
    actor_id="retention-test",
    identity_registry_id="fixture-registry",
)
_BODY = b"the physical compiled prompt"
_DIGEST = sha256(_BODY).hexdigest()


@asynccontextmanager
async def database():
    with isolated_postgres() as db:
        owner = create_postgres_engine(db.migration_url)
        engine = create_postgres_engine(db.application_url)
        try:
            assert await apply_migrations(owner, db.target) == tuple(
                entry.version for entry in _read_catalogue(files(migrations))
            )
            yield engine, db.application_url
        finally:
            await engine.dispose()
            await owner.dispose()


def policy(label, seconds, *, kind="model_request"):
    document = EvidencePolicyArtifact.model_validate_json(
        canonical_bytes(
            {
                "schema_version": "1.0.0",
                "version": "1.0.0",
                "policy_id": label,
                "policy": {
                    "policy_kind": "evidence",
                    "required_evidence_types": [kind],
                    "allowed_evidence_types": [kind],
                    "on_missing": "block_result",
                    "original_media_required": False,
                    "access_scope": "task",
                    "retention_class": {
                        "vocabulary_ref": {
                            "artifact_kind": "vocabulary",
                            "artifact_id": "retention",
                            "version": "1.0.0",
                            "digest": "a" * 64,
                        },
                        "member": "limited",
                    },
                    "retention_duration_seconds": seconds,
                    **(
                        {"transform_required_for_derived": True}
                        if kind == "mask"
                        else {}
                    ),
                },
            }
        )
    )
    body = canonical_bytes(document.model_dump(mode="json"))
    pin = PolicyContentRef(
        artifact_kind=ArtifactKind.POLICY,
        artifact_id=label,
        version="1.0.0",
        digest=sha256(body).hexdigest(),
    )
    return RetentionPolicyRef(
        evidence_policy_ref=pin, retention_class=document.policy.retention_class
    ), body


class Scenario:
    def __init__(self, tmp_path, engine) -> None:
        self.engine = engine
        self.root = tmp_path / "store"
        self.media = LocalMediaStore(self.root, engine)
        self.policies = {}
        self.staged_at = {}
        self.life = LocalEvidenceLifecycle(self.media, self.resolve)

    async def resolve(self, task, ref):
        assert task == self.task
        try:
            return self.policies[ref.digest]
        except KeyError:
            raise ValueError("fixture policy unavailable") from None

    async def initialize(self, *, suffix=""):
        self.scope, self.execution = await seed(self.engine, suffix=suffix)
        self.jobs = DurableJobs(self.engine)
        self.claim = await self.jobs.claim(
            self.scope,
            "evidence-owner",
            30_000,
            execution_id=self.execution,
        )
        self.attempt = await self.jobs.start(self.claim)
        self.node = await fetch_node(
            self.engine, self.scope, self.execution, self.claim.node_run_id
        )
        async with self.engine.connect() as connection:
            self.task = (
                await repository.get_execution(connection, self.scope, self.execution)
            ).task
        self.grant = EvidenceAccessGrant(
            scope=self.scope,
            actor=_ACTOR,
            task_id=self.task.task_id,
            manage_retention=True,
        )
        return self

    async def archive(
        self, evidence_id, pin, *, storage=None, at=None, project_access=False
    ):
        if storage is None:
            storage, registered_at = await self.media.stage_observation_request(
                self.scope,
                self.execution,
                evidence_id,
                _BODY,
            )
            self.staged_at[storage.object_key] = registered_at
        at = at or (self.attempt.started_at - timedelta(seconds=90))
        asset = EvidenceAsset(
            schema_version="1.0.0",
            evidence_id=evidence_id,
            evidence_type=EvidenceType.MODEL_REQUEST,
            scope=ExecutionEvidenceScope(
                scope="execution", execution_id=self.execution
            ),
            storage=storage,
            stage=FinalizedAsset(
                stage="finalized",
                staged_at=at,
                verified_at=at,
                verified_sha256=_DIGEST,
                object_version=f"sha256:{_DIGEST}",
            ),
            sha256=_DIGEST,
            byte_size=len(_BODY),
            mime_type="text/plain",
            producer_id="fixture.request",
            producer_version="1.0.0",
            created_at=at,
            spatial=NonSpatialEvidence(kind="non_spatial"),
            retention=pin,
            access_scope=(
                ProjectEvidenceAccess(scope="project", project_id=self.scope.project_id)
                if project_access
                else TaskEvidenceAccess(
                    scope="task",
                    project_id=self.scope.project_id,
                    task_id=self.task.task_id,
                )
            ),
        )
        async with self.engine.begin() as connection:
            digest = await repository.store_output(
                connection,
                self.scope,
                self.execution,
                self.claim.node_run_id,
                self.attempt.attempt_id,
                output_key=f"evidence.{asset.evidence_id}",
                output_kind="evidence",
                record=asset,
            )
        locator = EvidenceLocator(
            execution_id=self.execution,
            node_run_id=self.claim.node_run_id,
            attempt_id=self.attempt.attempt_id,
            evidence_id=asset.evidence_id,
            output_digest=digest,
        )
        return locator, asset

    async def due(self, asset, seconds=1):
        deadline = self.staged_at[asset.storage.object_key] + timedelta(seconds=seconds)
        await wait_for_db_time(self.engine, deadline)
        return deadline

    async def finish(self):
        assert (
            await self.jobs.complete(self.claim, outcome(self.attempt, self.node))
            is CompletionDisposition.APPLIED
        )
        # The lifecycle uses the production runtime index and active jobs.
        # Minimal fixture doesn't run the other five nodes.
        async with self.engine.begin() as connection:
            await connection.execute(
                text("""
                    UPDATE jimu_workflow.executions SET status = 'completed'
                    WHERE project_id = :p AND client_id = :c AND execution_id = :e
                """),
                {
                    "p": self.scope.project_id,
                    "c": self.scope.client_id,
                    "e": self.execution,
                },
            )


def test_physical_aliases_hold_and_dispose_once_preserve_outputs(tmp_path):
    async def run():
        async with database() as (engine, url):
            state = await Scenario(tmp_path, engine).initialize()
            short, raw_short = policy("short-retention", 1)
            long, raw_long = policy("long-retention", 2)
            state.policies.update(
                {
                    short.evidence_policy_ref.digest: raw_short,
                    long.evidence_policy_ref.digest: raw_long,
                }
            )
            first, asset = await state.archive("request.alpha", short)
            alias, _ = await state.archive(
                "request.alias",
                long,
                storage=asset.storage,
            )
            current = await state.due(asset)
            assert (await state.life.read_evidence(state.grant, alias)).content == _BODY
            denied = state.grant.model_copy(update={"task_id": None})
            assert (await state.life.read_evidence(denied, first)).status == "denied"
            unrelated = state.grant.model_copy(
                update={
                    "scope": state.scope.model_copy(
                        update={"client_id": "another-client"}
                    )
                }
            )
            assert (await state.life.read_evidence(unrelated, first)).status == "denied"
            assert (
                await state.life.dispose(state.grant, first, at=current)
            ).status == "active_execution"
            await state.finish()
            assert (
                await state.life.dispose(state.grant, first, at=current)
            ).status == "retention_pending"
            hold = await state.life.place_hold(
                state.grant,
                alias,
                "review-1",
                reason="review_case",
                at=current,
            )
            assert (
                await state.life.place_hold(
                    state.grant,
                    alias,
                    "review-1",
                    reason="review_case",
                    at=current,
                )
                == hold
            )
            assert (
                await state.life.dispose(state.grant, first, at=current)
            ).status == "review_hold"
            assert (state.root / asset.storage.object_key).read_bytes() == _BODY
            await state.life.release_hold(
                state.grant,
                state.execution,
                "review-1",
                reason="review_signed_off",
                at=current,
            )
            assert (
                await state.life.dispose(
                    state.grant, first, at=await state.due(asset, 2)
                )
            ).status == "disposed"
            assert not (state.root / asset.storage.object_key).exists()
            assert await state.archive(
                "request.alpha", short, storage=asset.storage
            ) == (first, asset)
            external = create_postgres_engine(url)
            try:
                reopened = LocalEvidenceLifecycle(
                    LocalMediaStore(state.root, external), state.resolve
                )
                access = await reopened.read_evidence(state.grant, alias)
                assert access.status == "expired"
                retained = await reopened.dispose(state.grant, alias, at=current)
                assert retained.completion.outcome == "removed"
                assert {pin.locator for pin in retained.intent.references} == {
                    first,
                    alias,
                }
                async with external.connect() as connection:
                    assert (
                        await connection.scalar(
                            text(
                                "SELECT count(*) FROM jimu_workflow.produced_outputs WHERE output_kind='evidence'"
                            )
                        )
                        == 2
                    )
                with pytest.raises(DBAPIError) as app_policy:
                    async with external.begin() as connection:
                        await connection.execute(
                            text(
                                "DELETE FROM jimu_workflow.evidence_disposition_intents"
                            )
                        )
                assert app_policy.value.orig.sqlstate == "42501"
            finally:
                await external.dispose()

    asyncio.run(run())


def dispose_until_unlinked(url, root, grant, locator, at, policy_body, sender):
    async def run():
        engine = create_postgres_engine(url)

        async def policy_from_fixture(task, ref):
            return policy_body

        async def wait_for_crash(*args, **kwargs):
            sender.send("durable_intent_unlinked")
            await asyncio.Future()

        try:
            life = LocalEvidenceLifecycle(
                LocalMediaStore(root, engine), policy_from_fixture
            )
            life._complete = wait_for_crash
            await life.dispose(grant, locator, at=at)
        finally:
            sender.close()
            await engine.dispose()

    asyncio.run(run())


def test_reopens_process_gap_after_unlink_without_changing_prepared_record(tmp_path):
    async def run():
        async with database() as (engine, url):
            state = await Scenario(tmp_path, engine).initialize()
            pin, content = policy("retry-retention", 1)
            state.policies[pin.evidence_policy_ref.digest] = content
            locator, asset = await state.archive("request.interrupted", pin)
            await state.finish()
            at = await state.due(asset)
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=dispose_until_unlinked,
                args=(url, state.root, state.grant, locator, at, content, sender),
            )
            process.start()
            sender.close()
            try:
                assert await asyncio.to_thread(receiver.poll, 10)
                assert receiver.recv() == "durable_intent_unlinked"
            finally:
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 10)
                receiver.close()
                process.close()
            assert not (state.root / asset.storage.object_key).exists()
            access = await state.life.read_evidence(state.grant, locator)
            assert access.status == "disposition_pending"
            with pytest.raises(
                EvidenceLifecycleError, match="retention_disposition_already_committed"
            ):
                await state.life.place_hold(
                    state.grant,
                    locator,
                    "review-late",
                    reason="review_case",
                    at=at,
                )
            state.policies.clear()
            external = create_postgres_engine(url)
            try:
                recovered = LocalEvidenceLifecycle(
                    LocalMediaStore(state.root, external), state.resolve
                )
                result, concurrent = await asyncio.gather(
                    recovered.dispose(state.grant, locator, at=at),
                    state.life.dispose(state.grant, locator, at=at),
                )
                assert result == concurrent
                assert result.intent.requested_at == at
                assert result.completion.outcome == "absent_on_recovery"
                assert await recovered.dispose(state.grant, locator, at=at) == result
            finally:
                await external.dispose()

    asyncio.run(run())


def test_live_reference_and_bad_pin_stop_automatic_disposition(tmp_path):
    async def run():
        async with database() as (engine, _):
            first = await Scenario(tmp_path, engine).initialize()
            short, raw = policy("short", 1)
            first.policies[short.evidence_policy_ref.digest] = raw
            anchor, asset = await first.archive("request.content", short)
            await first.finish()

            at = await first.due(asset)
            first.policies[short.evidence_policy_ref.digest] = b"untrusted policy"
            assert (
                await first.life.dispose(first.grant, anchor, at=at)
            ).status == "policy_unverified"
            first.policies[short.evidence_policy_ref.digest] = raw

            second = await Scenario(tmp_path, engine).initialize(
                suffix="another-execution"
            )
            await second.archive("request.shared", short, storage=asset.storage)
            assert (
                await first.life.dispose(first.grant, anchor, at=at)
            ).status == "shared_reference"
            assert (first.root / asset.storage.object_key).read_bytes() == _BODY
            with pytest.raises(
                EvidenceLifecycleError, match="retention_clock_is_in_future"
            ):
                await first.life.dispose(first.grant, anchor, at=at + timedelta(days=1))

    asyncio.run(run())


def read_in_child(url, root, grant, locator, sender):
    async def run():
        engine = create_postgres_engine(url)

        async def disabled_policy(*args):
            raise AssertionError("historical byte access does not resolve policy")

        try:
            life = LocalEvidenceLifecycle(
                LocalMediaStore(root, engine), disabled_policy
            )
            result = await life.read_evidence(grant, locator)
            sender.send((result.status, result.content))
        finally:
            sender.close()
            await engine.dispose()

    asyncio.run(run())


def test_reopen_bytes_in_process_and_distinguish_corrupt_missing_from_expired(tmp_path):
    async def run():
        async with database() as (engine, url):
            state = await Scenario(tmp_path, engine).initialize()
            pin, raw = policy("status-retention", 1)
            state.policies[pin.evidence_policy_ref.digest] = raw
            locator, asset = await state.archive("request.physical", pin)
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=read_in_child,
                args=(url, state.root, state.grant, locator, sender),
            )
            process.start()
            sender.close()
            try:
                assert await asyncio.to_thread(receiver.poll, 10)
                assert receiver.recv() == ("available", _BODY)
                await asyncio.to_thread(process.join, 10)
                assert process.exitcode == 0
            finally:
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join, 10)
                receiver.close()
                process.close()

            await state.finish()
            at = await state.due(asset)
            path = state.root / asset.storage.object_key
            path.chmod(0o600)
            path.write_bytes(b"!" + _BODY[1:])
            assert (
                await state.life.read_evidence(state.grant, locator)
            ).status == "corrupt"
            assert (
                await state.life.dispose(state.grant, locator, at=at)
            ).status == "corrupt"
            path.unlink()
            assert (
                await state.life.read_evidence(state.grant, locator)
            ).status == "missing"
            assert (
                await state.life.dispose(state.grant, locator, at=at)
            ).status == "missing"
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM jimu_workflow.evidence_disposition_intents"
                        )
                    )
                    == 0
                )

    asyncio.run(run())


def test_model_audit_reports_late_suppression_and_roles_independently(tmp_path):
    async def run():
        async with database() as (engine, _):
            jobs, scope, eid = await _ready(engine)
            claim, attempt, node = await _start(
                engine, jobs, scope, eid, "retention-model"
            )
            life = LocalEvidenceLifecycle(
                LocalMediaStore(tmp_path / "store", engine),
                lambda task, policy_ref: None,
            )
            async with engine.connect() as connection:
                locked = await repository.get_execution(connection, scope, eid)
            grant = EvidenceAccessGrant(
                scope=scope, task_id=locked.task.task_id, actor=_ACTOR
            )
            journal = DispatchJournal(engine)
            normalized = b'{"verdict":true}'
            wire = json.dumps(
                {"choices": [{"message": {"content": normalized.decode()}}]}
            ).encode()
            for enabled in (True, False):
                identifier = f"retention-model-{'kept' if enabled else 'omitted'}"
                intent = _intent(node.trace.trace_id, identifier).model_copy(
                    update={
                        "operation_id": identifier,
                        "retain_complete_response": enabled,
                    }
                )
                assert await journal.begin(claim, attempt.attempt_id, intent)
                omission = None if enabled else "policy_disabled"
                observed = TransportResult(
                    True,
                    response_status=200,
                    response_dispatch_id=identifier,
                    response_bytes=wire if enabled else None,
                    parsed_content=normalized,
                    response_digest=sha256(wire).hexdigest(),
                    observed_response_bytes=len(wire),
                    response_bytes_omission_reason=omission,
                    validated_response_contract_digest=intent.response_contract_digest,
                    normalized_response_digest=sha256(normalized).hexdigest(),
                    normalized_response_bytes=len(normalized),
                )
                await life._media.stage_model_response(scope, eid, identifier, observed)
                receipt = _model_outcome(
                    intent, attempt, node, wire, identifier
                ).model_copy(
                    update={
                        "response_bytes_omission_reason": omission,
                        "normalized_response_digest": sha256(normalized).hexdigest(),
                        "normalized_response_bytes": len(normalized),
                    },
                )
                assert await journal.append_result(
                    claim, attempt.attempt_id, identifier, receipt, late=True
                )
                raw = await life.read_model_audit(
                    grant, eid, identifier, late=True, role="raw"
                )
                if enabled:
                    assert raw.status == "late" and raw.content == wire
                    key = _DispatchKey(scope, eid, identifier).key("model_raw")
                    path = life._media._bytes._directory / key
                    path.chmod(0o600)
                    path.write_bytes(b"!" + wire[1:])
                    damaged = await life.read_model_audit(
                        grant, eid, identifier, late=True, role="raw"
                    )
                    assert damaged.status == "corrupt"
                else:
                    assert (
                        raw.status == "not_retained"
                        and raw.reason == "policy_disabled"
                        and raw.late
                    )
                read = await life.read_model_audit(
                    grant, eid, identifier, late=True, role="normalized"
                )
                assert read.status == "late" and read.content == normalized
                denied = grant.model_copy(update={"task_id": "other"})
                assert (
                    await life.read_model_audit(
                        denied, eid, identifier, late=True, role="raw"
                    )
                ).status == "denied"

    asyncio.run(run())


@pytest.mark.parametrize("bad_alias", [None, "source", "type"])
def test_disposes_receipted_tool_mask_but_preserves_shared_original(
    tmp_path, bad_alias
):
    async def run():
        async with database() as (engine, _):
            media = LocalMediaStore(tmp_path / "store", engine)
            original = pixels()
            original_ref = ref(original)
            task = AcceptedTask.model_validate_json(
                (
                    Path(__file__).parents[1]
                    / "fixtures/wp01c/intake/accepted-task-valid.json"
                ).read_bytes()
            ).model_copy(update={"media_ref": original_ref})
            scope = task.idempotency_scope
            grant = EvidenceAccessGrant(
                scope=scope, task_id=task.task_id, actor=_ACTOR, manage_retention=True
            )
            raw_asset = await publish(media, scope, original)
            eid, (claim, attempt, node) = await ready_tool(engine, task)
            frame = (await media.read_original(scope, original_ref)).coordinate_system
            journal = DispatchJournal(engine)
            intent, template, proof = tool_contract(eid, attempt, node)
            assert await journal.begin(claim, attempt.attempt_id, intent)
            pin, pinned = policy("mask-retention", 1, kind="mask")

            async def exact_policy(saved_task, ref):
                assert (saved_task, ref) == (task, pin.evidence_policy_ref)
                return pinned

            life = LocalEvidenceLifecycle(media, exact_policy)
            mask = pixels("L", color=255)
            typed_mask = MaskSidecarRecord(
                coordinate_system=frame,
                evidence_id="mask-retention-asset",
                sha256=sha256(mask).hexdigest(),
                source_media_sha256=original_ref.sha256,
            )
            output = await media.stage_tool_sidecar(
                scope,
                eid,
                intent.dispatch_id,
                "wheel_mask",
                ToolSidecarSpec(
                    evidence_id=typed_mask.evidence_id,
                    spatial=DerivedImageEvidence(
                        kind="derived_image",
                        original_media_ref=original_ref,
                        transforms=(
                            TransformRecord(
                                source=frame,
                                target=CoordinateSystem(
                                    coordinate_system_id="mask-grid",
                                    frame="derived_image",
                                    dimensions=_DIMENSIONS,
                                ),
                                implementation_id="mask-tool",
                                implementation_version="1.0.0",
                                affine=(1, 0, 0, 0, 1, 0),
                            ),
                        ),
                    ),
                    retention=pin,
                    access="task",
                    mask_record=typed_mask,
                ),
                mask,
            )
            ref_doc = {"record_kind": "evidence", "record_id": output.evidence_id}
            template["evidence_refs"] = [ref_doc]
            template["output_refs"] = [
                {"artifact_ref": ref_doc, "digest": output.sha256}
            ]
            result = template["attempt_result"]["result"]
            result["evidence_refs"] = [ref_doc]
            result["evidence"] = [{"name": "wheel_mask", "evidence_refs": [ref_doc]}]
            result["output_states"] = [
                {
                    "output_name": "wheel_mask",
                    "fact_status": "known",
                    "evidence_refs": [ref_doc],
                }
            ]
            result["payload"] = typed_record(
                {"wheel_mask": typed_mask.model_dump(mode="json")}
            )
            tool = ToolExecution.model_validate_json(json.dumps(template))
            physical = json.dumps(result).encode("utf-8")
            ack = ToolDispatchResult(
                boundary="acknowledged",
                response_digest=sha256(physical).hexdigest(),
                observed_response_bytes=len(physical),
                acknowledgement="tool_process",
                execution=tool,
                implementation=ToolImplementationObservation(
                    implementation_version="1.0.0", proof_ref=proof
                ),
                tool_execution_digest=tool_receipt_digest(tool),
            )
            assert await journal.append_result(
                claim, attempt.attempt_id, intent.dispatch_id, ack
            )
            original_evidence = EvidenceAsset(
                schema_version="1.0.0",
                evidence_id="original-asset",
                evidence_type=EvidenceType.ORIGINAL_IMAGE,
                scope=ObjectEvidenceScope(
                    scope="object",
                    execution_id=eid,
                    object_ref=intent.target.object_ref,
                    profile_ref=intent.target.profile_ref,
                ),
                storage=raw_asset.storage,
                sha256=raw_asset.media_ref.sha256,
                mime_type=raw_asset.mime_type,
                byte_size=raw_asset.byte_size,
                stage=raw_asset.stage,
                spatial=OriginalImageEvidence(
                    kind="original_image",
                    original_media_ref=original_ref,
                    coordinate_system=frame,
                ),
                producer_id="test.original",
                producer_version="1.0.0",
                access_scope=output.access_scope,
                retention=pin,
                created_at=raw_asset.stage.staged_at,
            )
            archived = (output, original_evidence)
            if bad_alias is not None:
                alias = output.model_copy(update={"evidence_id": "mask-alias"})
                if bad_alias == "source":
                    alias = alias.model_copy(
                        update={
                            "spatial": output.spatial.model_copy(
                                update={
                                    "original_media_ref": original_ref.model_copy(
                                        update={"media_id": "other-media"}
                                    ),
                                }
                            )
                        }
                    )
                else:
                    alias = alias.model_copy(
                        update={"evidence_type": EvidenceType.GEOMETRY}
                    )
                archived = (*archived, alias)
            locators = []
            async with engine.begin() as connection:
                for asset in archived:
                    digest = await repository.store_output(
                        connection,
                        scope,
                        eid,
                        claim.node_run_id,
                        attempt.attempt_id,
                        output_key=f"evidence.{asset.evidence_id}",
                        output_kind="evidence",
                        record=asset,
                    )
                    locators.append(
                        EvidenceLocator(
                            execution_id=eid,
                            node_run_id=claim.node_run_id,
                            attempt_id=attempt.attempt_id,
                            evidence_id=asset.evidence_id,
                            output_digest=digest,
                        )
                    )
            await DurableJobs(engine).complete(claim, outcome(attempt, node))
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE jimu_workflow.executions SET status = 'completed'")
                )
            due = output.stage.verified_at + timedelta(seconds=1)
            await wait_for_db_time(engine, due)
            assert (await life.read_evidence(grant, locators[0])).content == mask
            expected = "disposed" if bad_alias is None else "shared_reference"
            assert (await life.dispose(grant, locators[0], at=due)).status == expected
            if bad_alias is not None:
                assert (
                    tmp_path / "store" / output.storage.object_key
                ).read_bytes() == mask
                assert (
                    await life.read_evidence(grant, locators[2])
                ).status == "corrupt"
            assert (
                await life.dispose(grant, locators[1], at=due)
            ).status == "shared_reference"
            assert (await life.read_evidence(grant, locators[1])).content == original

    asyncio.run(run())


def test_resolves_policy_from_approved_original_bundle_after_another_activation(
    tmp_path,
):
    scene = exercise_lifecycle(tmp_path)
    store = LocalBundleStore(tmp_path / "releases")
    manager = ReleaseManager(
        store,
        authorize=lambda *args: None,
        scope_precedence=("device", "region", "scene", "global"),
        resolve_task_scope=lambda *args: None,
        allow_new_task=lambda *args: False,
        clock=lambda: AT,
    )
    resolver = HistoricalEvidencePolicies(manager, store, LIMITS)
    assert scene.current_activation.bundle_ref != scene.activation.bundle_ref
    entry = next(
        item
        for item in scene.execution.catalogue.of_kind(ArtifactKind.POLICY)
        if isinstance(item.value, EvidencePolicyArtifact)
    )
    reference = PolicyContentRef.model_validate(entry.ref.model_dump())
    assert asyncio.run(resolver(scene.task, reference)) == entry.canonical_bytes
    wrong = reference.model_copy(update={"digest": "a" * 64})
    with pytest.raises(ValueError):
        asyncio.run(resolver(scene.task, wrong))


def test_packaged_011_upgrades_the_actual_migration_chain():
    async def run():
        catalogue = _read_catalogue(files(migrations))
        assert [entry.version for entry in catalogue[:11]] == list(range(1, 12))
        assert catalogue[8].script_name == "009_artifact_replay.sql"
        assert catalogue[9].script_name == "010_inference_replay.sql"
        with isolated_postgres() as db:
            engine = create_postgres_engine(db.migration_url)
            try:
                assert await _apply_catalogue(
                    engine, db.target, catalogue[:10]
                ) == tuple(range(1, 11))
                assert await apply_migrations(engine, db.target) == tuple(
                    item.version for item in catalogue[10:]
                )
                assert await apply_migrations(engine, db.target) == ()
            finally:
                await engine.dispose()

    asyncio.run(run())
