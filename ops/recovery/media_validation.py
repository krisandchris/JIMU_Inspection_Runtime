"""Prove the authorized absence of a committed media object at the fenced snapshot."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
from typing import Any

import psycopg

from jimu_runtime.bundle.archive import BundleLimits, read_bundle
from jimu_runtime.bundle.catalogue import ArtifactCatalogue, build_catalogue
from jimu_runtime.bundle.store import ReleaseLedger, _read_archive
from jimu_runtime.domain.fact_schemas.canonicalization import canonical_bytes
from jimu_runtime.domain.policies import EvidencePolicyArtifact
from jimu_runtime.domain.runtime.evidence import (
    DerivedImageEvidence,
    EvidenceAsset,
    OriginalImageEvidence,
    TaskEvidenceAccess,
)
from jimu_runtime.domain.runtime.intake import AcceptedTask
from jimu_runtime.domain.runtime.media import FinalizedAsset
from jimu_runtime.domain.runtime.releases import ReleaseRecord
from ops.recovery.contracts import MEDIA, FileWitness, RecoveryError

_TERMINAL = frozenset({"completed", "partial_failed", "failed", "cancelled"})
_ERROR = "committed_media_file_missing_or_changed"
_BUNDLE_LIMITS = BundleLimits(
    max_archive_bytes=32 * 1024 * 1024,
    max_expanded_bytes=64 * 1024 * 1024,
    max_file_bytes=16 * 1024 * 1024,
    max_files=2048,
    max_json_depth=128,
)
type Locator = tuple[str, str, str, str, str]
type CatalogueReader = Callable[[str, str], ArtifactCatalogue]

_ALIASES = """
    SELECT project_id, client_id, execution_id, node_run_id, attempt_id, output_key,
           output_digest, record::text
    FROM jimu_workflow.produced_outputs
    WHERE output_kind = 'evidence' AND record -> 'storage' ->> 'object_key' = %s
    LIMIT 1025
"""

_REQUEST_PROTECTION = """
    SELECT NOT EXISTS (
        SELECT 1 FROM jimu_workflow.dispatch_results seen
        CROSS JOIN LATERAL json_array_elements(seen.record -> 'invocation' -> 'evidence_refs') ref(value)
        WHERE (seen.project_id, seen.client_id, seen.execution_id) = (%s,%s,%s)
          AND ref.value ->> 'record_kind' = 'evidence'
          AND ref.value ->> 'record_id' = %s
    ) AND NOT EXISTS (
        SELECT 1 FROM jimu_workflow.dispatch_intents intent
        WHERE (intent.project_id, intent.client_id, intent.execution_id) = (%s,%s,%s)
          AND intent.send_admitted AND intent.record -> 'target' ->> 'node_id' = 'N04'
          AND NOT EXISTS (
              SELECT 1 FROM jimu_workflow.dispatch_results resolved
              WHERE (resolved.project_id, resolved.client_id, resolved.execution_id, resolved.dispatch_id)
                  = (intent.project_id, intent.client_id, intent.execution_id, intent.dispatch_id)
                AND (resolved.boundary = 'not_dispatched' OR resolved.boundary = 'acknowledged'
                     AND json_typeof(resolved.record -> 'invocation') = 'object')
          )
    ) AND NOT EXISTS (
        SELECT 1 FROM jimu_workflow.produced_outputs p
        WHERE (p.project_id,p.client_id,p.execution_id) = (%s,%s,%s)
          AND p.output_kind = 'evidence' AND p.output_key = 'evidence.' || %s
    )
"""

_JOB_PROTECTION = """
    SELECT e.status, NOT EXISTS (
        SELECT 1 FROM jimu_workflow.jobs j
        WHERE (j.project_id,j.client_id,j.execution_id) = (%s,%s,%s) AND j.status <> 'done'
    ), e.task_id, e.release_id, e.bundle_digest, t.record::text
    FROM jimu_workflow.executions e
    JOIN jimu_workflow.tasks t USING (project_id,client_id,task_id)
    WHERE (e.project_id,e.client_id,e.execution_id) = (%s,%s,%s)
"""

_HOLDS = """
    SELECT h.project_id,h.client_id,h.execution_id,h.store_id,h.hold_id,h.record::text,r.record::text
    FROM jimu_workflow.evidence_retention_holds h
    LEFT JOIN jimu_workflow.evidence_retention_hold_releases r
        USING (project_id,client_id,execution_id,hold_id)
    WHERE h.file_key = %s
    LIMIT 1025
"""

_DISPOSITIONS = """
    SELECT project_id,client_id,execution_id,store_id,i.record::text,c.record::text
    FROM jimu_workflow.evidence_disposition_intents i
    LEFT JOIN jimu_workflow.evidence_disposition_completions c
        USING (project_id,client_id,execution_id,store_id,file_key)
    WHERE file_key = %s
    LIMIT 2
"""

_DISPATCH_OWNER = """
    SELECT d.node_run_id,d.attempt_id,d.send_admitted,d.record,r.record
    FROM jimu_workflow.dispatch_intents d
    JOIN jimu_workflow.dispatch_results r USING (project_id,client_id,execution_id,dispatch_id)
    WHERE (d.project_id,d.client_id,d.execution_id,d.dispatch_id) = (%s,%s,%s,%s) AND NOT r.late
"""


def _require(condition: object) -> None:
    if not condition:
        raise RecoveryError(_ERROR)


def _instant(value: Any) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Expected timezone for disposed media")
    return moment


def _file_key(kind: str, *parts: str) -> str:
    return sha256(canonical_bytes(["jimu.media.v1", kind, *parts])).hexdigest()


def _catalogue_reader(db: sqlite3.Connection) -> CatalogueReader:
    releases = ReleaseLedger(db)
    limits = _BUNDLE_LIMITS

    @lru_cache(maxsize=1)
    def read(release_id: str, digest: str) -> ArtifactCatalogue:
        raw = releases.get("release", release_id)
        _require(raw is not None)
        assert raw is not None
        release = ReleaseRecord.model_validate_json(raw)
        receipt = release.admission_receipt
        _require(
            release.release_id == release_id
            and release.bundle_ref.bundle_digest == digest
        )
        _require(receipt is not None)
        assert receipt is not None
        entry = releases.get_receipt(receipt.receipt_id)
        _require(
            entry is not None
            and entry.accepted
            and entry.receipt == receipt
            and entry.archive_size <= limits.max_archive_bytes
        )
        assert entry is not None
        files = read_bundle(_read_archive(db, entry), limits)
        _require(files.manifest.integrity.root_digest == digest)
        return build_catalogue(
            files.manifest,
            files.payloads,
            max_bytes=limits.max_file_bytes,
            max_depth=limits.max_json_depth,
        )

    return read


def _check_aliases(
    rows: list[tuple[Any, ...]],
    scope: tuple[str, str, str],
    path: str,
    store: str,
    digest: str,
    size: int,
    staged_at: datetime,
    requested: datetime,
    references: object,
    task: AcceptedTask,
    policies: ArtifactCatalogue,
) -> dict[Locator, EvidenceAsset]:
    _require(isinstance(references, list) and 0 < len(rows) == len(references) <= 1024)
    assert isinstance(references, list)
    expected = {}
    evidence_types = set()
    for (
        project,
        client,
        execution,
        node,
        attempt,
        output_key,
        output_digest,
        raw,
    ) in rows:
        asset = EvidenceAsset.model_validate_json(raw)
        _require(
            (project, client, execution) == scope
            and asset.scope.execution_id == execution
            and asset.access_scope.project_id == project
            and (
                not isinstance(asset.access_scope, TaskEvidenceAccess)
                or asset.access_scope.task_id == task.task_id
            )
            and (
                not isinstance(
                    asset.spatial, (OriginalImageEvidence, DerivedImageEvidence)
                )
                or asset.spatial.original_media_ref == task.media_ref
            )
            and asset.stage.stage == "finalized"
            and isinstance(asset.stage, FinalizedAsset)
            and asset.stage.object_version == f"sha256:{digest}"
            and (
                asset.storage.store_id,
                asset.storage.object_key,
                asset.sha256,
                asset.byte_size,
            )
            == (store, path, digest, size)
            and output_key == f"evidence.{asset.evidence_id}"
            and asset.stable_json() == raw
            and sha256(raw.encode()).hexdigest() == output_digest
        )
        locator = (execution, node, attempt, asset.evidence_id, output_digest)
        _require(locator not in expected)
        expected[locator] = asset
        evidence_types.add(asset.evidence_type)
    _require(len(evidence_types) == 1)
    remaining = set(expected)
    for pin in references:
        loc = pin["locator"]
        locator = tuple(
            loc[key]
            for key in (
                "execution_id",
                "node_run_id",
                "attempt_id",
                "evidence_id",
                "output_digest",
            )
        )
        _require(locator in remaining)
        remaining.remove(locator)
        asset = expected[locator]
        assert isinstance(asset.stage, FinalizedAsset)
        ref = asset.retention.evidence_policy_ref
        body = policies.resolve(ref).canonical_bytes
        policy = EvidencePolicyArtifact.model_validate_json(body)
        _require(
            sha256(body).hexdigest() == ref.digest
            and (policy.policy_id, policy.version) == (ref.artifact_id, ref.version)
            and policy.policy.retention_class == asset.retention.retention_class
            and policy.policy.access_scope == asset.access_scope.scope
            and asset.evidence_type in policy.policy.allowed_evidence_types
            and pin["retention"] == asset.retention.model_dump(mode="json")
            and _instant(pin["expires_at"])
            == max(staged_at, asset.stage.verified_at)
            + timedelta(seconds=policy.policy.retention_duration_seconds)
            <= requested
        )
    return expected


def _released_holds(
    connection: psycopg.Connection,
    key: str,
    store: str,
    scope: tuple[str, str, str],
    aliases: dict[tuple[str, str, str, str, str], EvidenceAsset],
    cutoff: datetime,
) -> None:
    rows = connection.execute(_HOLDS, (key,)).fetchall()
    _require(len(rows) <= 1024)
    for project, client, execution, row_store, hold_id, raw_hold, raw_release in rows:
        _require(
            (project, client, execution) == scope
            and row_store == store
            and raw_release is not None
        )
        hold = json.loads(raw_hold)
        release = json.loads(raw_release)
        locator = tuple(
            hold["locator"][field]
            for field in (
                "execution_id",
                "node_run_id",
                "attempt_id",
                "evidence_id",
                "output_digest",
            )
        )
        _require(
            locator in aliases
            and hold["hold_id"] == hold_id == release["hold_id"]
            and _instant(hold["placed_at"])
            <= _instant(release["released_at"])
            <= cutoff
        )


def _tool_owner(
    connection: psycopg.Connection,
    scope: tuple[str, str, str],
    dispatch: str,
    role: str,
    evidence_id: str,
    claim_digest: str,
    aliases: dict[tuple[str, str, str, str, str], EvidenceAsset],
) -> None:
    rows = connection.execute(_DISPATCH_OWNER, (*scope, dispatch)).fetchall()
    _require(len(rows) == 1)
    node, attempt, admitted, intent, receipt = rows[0]
    primary = [
        asset
        for locator, asset in aliases.items()
        if locator[:4] == (scope[2], node, attempt, evidence_id)
    ]
    _require(len(primary) == 1 and admitted)
    asset = primary[0]
    tool = receipt["execution"]
    target = intent["target"]
    _require(
        sha256(
            canonical_bytes(
                asset.model_dump(mode="json", exclude={"created_at", "stage"})
            )
        ).hexdigest()
        == claim_digest
        and target["kind"] == "tool"
        and receipt["acknowledgement"] == "tool_process"
        and receipt["response_digest"] is not None
        and receipt["observed_response_bytes"] > 0
        and receipt["implementation"]["implementation_version"]
        == target["implementation_version"]
        and receipt["implementation"]["proof_ref"] == target["implementation_proof_ref"]
        and tool["tool_execution_id"] == dispatch == intent["dispatch_id"]
        and tool["execution_id"] == scope[2]
        and tool["attempt_ref"]["record_id"] == attempt
        and tool["node_run_ref"]["record_id"] == node
        and tool["profile_ref"] == target["profile_ref"]
        and tool["object_ref"] == target["object_ref"]
        and any(
            item["name"] == role
            and [ref["record_id"] for ref in item["evidence_refs"]] == [evidence_id]
            for item in tool["attempt_result"]["result"]["evidence"]
        )
        and any(
            item["artifact_ref"]
            == {"record_kind": "evidence", "record_id": evidence_id}
            and item["digest"] == asset.sha256
            for item in tool["output_refs"]
        )
    )


def verify_missing(
    connection: psycopg.Connection,
    names: tuple[str, ...],
    row: tuple[Any, ...],
    catalogue: CatalogueReader,
    *,
    request: bool,
) -> None:
    (
        project,
        client,
        execution,
        store,
        key,
        digest,
        size,
        evidence_id,
        staged_at,
        dispatch,
        role,
        claim,
    ) = row
    scope = (project, client, execution)
    if request:
        expected_key = _file_key("observation-request", *scope, evidence_id)
    else:
        expected_key = _file_key("dispatch", *scope, dispatch, role)
    _require(key == expected_key and evidence_id is not None)
    check = connection.execute(_JOB_PROTECTION, (*scope, *scope)).fetchone()
    _require(check is not None and check[0] in _TERMINAL and check[1])
    path = f"{MEDIA}/{key}"
    aliases = connection.execute(_ALIASES, (path,)).fetchall()
    other_physical = connection.execute(
        """SELECT
             (SELECT count(*) FROM jimu_workflow.original_media WHERE file_key=%s),
             (SELECT count(*) FROM jimu_workflow.dispatch_media WHERE file_key=%s)
        """,
        (key, key),
    ).fetchone()
    _require(other_physical == (0, int(not request)))
    if not request and "observation_request_media" in names:
        _require(
            connection.execute(
                "SELECT 1 FROM jimu_workflow.observation_request_media WHERE file_key=%s",
                (key,),
            ).fetchone()
            is None
        )
    disposition: list[tuple[Any, ...]] = []
    if "evidence_disposition_intents" in names:
        disposition = connection.execute(_DISPOSITIONS, (key,)).fetchall()
    reclaimed = None
    if request:
        reclaimed = connection.execute(
            "SELECT reclaimed_at FROM jimu_workflow.observation_request_media_reclaims WHERE file_key=%s",
            (key,),
        ).fetchone()
    now = connection.execute("SELECT clock_timestamp()").fetchone()
    assert now is not None
    if reclaimed is not None:
        _require(
            not aliases and not disposition and staged_at <= reclaimed[0] <= now[0]
        )
        _require(
            connection.execute(
                _REQUEST_PROTECTION, (*scope, evidence_id, *scope, *scope, evidence_id)
            ).fetchone()
            == (True,)
        )
        if "evidence_retention_holds" in names:
            _require(connection.execute(_HOLDS, (key,)).fetchone() is None)
        return
    _require(len(disposition) == 1)
    dp, dc, de, ds, raw_intent, raw_completion = disposition[0]
    _require((dp, dc, de) == scope and ds == store and raw_completion is not None)
    intent = json.loads(raw_intent)
    completion = json.loads(raw_completion)
    requested_at = _instant(intent["requested_at"])
    _require(
        intent["schema_version"] == completion["schema_version"] == "1.0.0"
        and (
            intent["storage"]["store_id"],
            intent["storage"]["object_key"],
            intent["sha256"],
            intent["byte_size"],
        )
        == (store, path, digest, size)
        and intent["reason"] == "retention_period_elapsed"
        and completion["outcome"] in {"removed", "absent_on_recovery"}
        and staged_at < requested_at <= _instant(completion["completed_at"]) <= now[0]
    )
    assert check is not None
    task = AcceptedTask.model_validate_json(check[5])
    _require(
        (task.idempotency_scope.project_id, task.idempotency_scope.client_id)
        == scope[:2]
        and (task.task_id, task.release_id, task.bundle_digest) == check[2:5]
    )
    owned = _check_aliases(
        aliases,
        scope,
        path,
        store,
        digest,
        size,
        staged_at,
        requested_at,
        intent["references"],
        task,
        catalogue(task.release_id, task.bundle_digest),
    )
    _released_holds(connection, key, store, scope, owned, requested_at)
    if request:
        _require(any(locator[3] == evidence_id for locator in owned))
    else:
        _tool_owner(connection, scope, dispatch, role, evidence_id, claim, owned)


def _verify_marked_frames(
    connection: psycopg.Connection, files: dict[str, FileWitness]
) -> None:
    with connection.cursor(name="recovery_marked_frames") as cursor:
        cursor.execute("""
            SELECT m.project_id, m.client_id, m.execution_id, m.node_run_id,
                   m.attempt_id, m.object_id, m.file_key, m.content_sha256, m.byte_size,
                   m.store_id, original.record #>> '{storage,store_id}' AS original_store_id,
                   a.attempt_id IS NOT NULL AND p.output_key IS NULL AS reclaimable
            FROM jimu_workflow.marked_frame_media m
            JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id)
            JOIN jimu_workflow.tasks task USING (project_id, client_id, task_id)
            LEFT JOIN jimu_workflow.attempt_results a
                USING (project_id, client_id, execution_id, node_run_id, attempt_id)
            LEFT JOIN jimu_workflow.produced_outputs p
                ON (p.project_id, p.client_id, p.execution_id, p.node_run_id,
                    p.attempt_id, p.output_key)
                 = (m.project_id, m.client_id, m.execution_id, m.node_run_id,
                    m.attempt_id, 'object.' || m.object_id)
                AND p.output_kind = 'object' AND p.output_digest = m.object_sha256
            LEFT JOIN jimu_workflow.original_media original
                ON (original.project_id, original.client_id, original.media_id)
                 = (task.project_id, task.client_id, task.record #>> '{media_ref,media_id}')
                AND original.record #>> '{media_ref,sha256}' = task.record #>> '{media_ref,sha256}'
        """)
        for (
            project,
            client,
            execution,
            node,
            attempt,
            obj,
            key,
            digest,
            size,
            store_id,
            original_store_id,
            reclaimable,
        ) in cursor:
            # A marked frame uses the same local store as its pinned Task original.
            _require(store_id == original_store_id)
            _require(
                key
                == _file_key(
                    "marked-frame", project, client, execution, node, attempt, obj
                )
            )
            item = files.get(f"{MEDIA}/{key}")
            _require(
                reclaimable
                if item is None
                else (item.sha256, item.size) == (digest, size)
            )
    missing = connection.execute("""
        SELECT 1 FROM jimu_workflow.produced_outputs p
        -- Canonical Objects omit absent frames. Explicit JSON null is invalid.
        WHERE p.output_kind = 'object' AND p.record -> 'marked_frame' IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM jimu_workflow.marked_frame_media m
                WHERE (p.project_id, p.client_id, p.execution_id, p.node_run_id,
                       p.attempt_id, p.output_key, p.output_digest)
                    = (m.project_id, m.client_id, m.execution_id, m.node_run_id,
                       m.attempt_id, 'object.' || m.object_id, m.object_sha256)
                    AND p.record #>> '{marked_frame,media_ref,media_id}' = m.media_id
                    AND p.record #>> '{marked_frame,media_ref,sha256}' = m.content_sha256
                    AND p.record #>> '{marked_frame,byte_size}' = m.byte_size::text
            )
        LIMIT 1
    """).fetchone()
    _require(missing is None)


def verify_media(
    connection: psycopg.Connection,
    names: tuple[str, ...],
    files: dict[str, FileWitness],
    bundle_db: sqlite3.Connection,
) -> None:
    catalogue = _catalogue_reader(bundle_db)
    if "marked_frame_media" in names:
        _verify_marked_frames(connection, files)
    if "evidence_disposition_intents" in names:
        with connection.cursor(name="recovery_dispositions") as cursor:
            cursor.execute("""
                SELECT i.file_key,c.file_key IS NOT NULL
                FROM jimu_workflow.evidence_disposition_intents i
                LEFT JOIN jimu_workflow.evidence_disposition_completions c
                    USING (project_id,client_id,execution_id,store_id,file_key)
            """)
            for key, completed in cursor:
                if not completed:
                    raise RecoveryError(
                        "pending_media_disposition_requires_reconciliation"
                    )
                _require(f"{MEDIA}/{key}" not in files)
    queries = [
        """SELECT file_key, record -> 'media_ref' ->> 'sha256', (record ->> 'byte_size')::bigint
                  FROM jimu_workflow.original_media"""
    ]
    bindings = [
        (
            """SELECT project_id,client_id,execution_id,store_id,file_key,content_sha256,
                            byte_size,evidence_id,staged_at,dispatch_id,role,evidence_claim_sha256
                     FROM jimu_workflow.dispatch_media""",
            False,
        )
    ]
    if "observation_request_media" in names:
        bindings.append(
            (
                """SELECT project_id,client_id,execution_id,store_id,file_key,content_sha256,
                                   byte_size,evidence_id,staged_at,NULL,NULL,NULL
                            FROM jimu_workflow.observation_request_media""",
                True,
            )
        )
    with connection.cursor(name="recovery_media") as cursor:
        cursor.execute(queries[0])
        for key, digest, size in cursor:
            item = files.get(f"{MEDIA}/{key}")
            _require(item is not None and (item.sha256, item.size) == (digest, size))
    for query, request in bindings:
        with connection.cursor(name="recovery_media") as cursor:
            cursor.execute(query)
            for row in cursor:
                key, digest, size = row[4:7]
                item = files.get(f"{MEDIA}/{key}")
                if item is None:
                    try:
                        verify_missing(
                            connection, names, row, catalogue, request=request
                        )
                    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
                        raise RecoveryError(_ERROR) from None
                else:
                    _require((item.sha256, item.size) == (digest, size))
