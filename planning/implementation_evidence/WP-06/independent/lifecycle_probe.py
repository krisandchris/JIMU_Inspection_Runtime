"""Exercise managed signed evidence through a genuine unlink/commit process death."""

import asyncio
import json
import sys
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from harness import ACTOR, Harness
from sqlalchemy import text
from sqlalchemy.engine import make_url

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.inspection.media.retention import LocalEvidenceLifecycle
from jimu_runtime.inspection.media.retention_models import (
    EvidenceAccessGrant,
    EvidenceLocator,
)
from jimu_runtime.inspection.media.retention_policy import HistoricalEvidencePolicies
from jimu_runtime.workflow.postgres import create_postgres_engine


class ObserveUnlink(LocalEvidenceLifecycle):
    async def _complete(self, *args, **kwargs):
        # The product has finished unlinking while holding its real file lock.
        print("durable-intent-then-unlink", flush=True)
        await asyncio.Event().wait()
        return await super()._complete(*args, **kwargs)


def lifecycle(host, cls=LocalEvidenceLifecycle):
    return cls(
        host.media,
        HistoricalEvidencePolicies(host.manager(), host.ledger, scene.LIMITS),
    )


async def _kill_after_unlink(host, grant, locator, at, stored: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__)),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout
    try:
        process.stdin.write(
            json.dumps(
                {
                    "database": host.db.url.render_as_string(hide_password=False),
                    "directory": str(host.root),
                    "endpoint": host.endpoint.url,
                    "grant": grant.model_dump(mode="json"),
                    "locator": locator.model_dump(mode="json"),
                    "at": at.isoformat(),
                }
            ).encode()
            + b"\n"
        )
        await process.stdin.drain()
        result = await asyncio.wait_for(process.stdout.readline(), 30)
        assert result == b"durable-intent-then-unlink\n", result
        assert not stored.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == -9


async def assert_lifecycle(host, source, output):
    app = host.db
    eid = source.execution_ref.record_id
    scope = host.scope
    async with app.connect() as con:
        archived = (
            await con.execute(
                text("""
            SELECT node_run_id, attempt_id, output_digest, record::text AS record
            FROM jimu_workflow.produced_outputs WHERE execution_id=:eid
              AND output_key LIKE 'evidence.%' AND record->>'evidence_type'='geometry'
        """),
                {"eid": eid},
            )
        ).all()
        assert (
            archived and len({(row.output_digest, row.record) for row in archived}) == 1
        )
        row = archived[0]
        moment = await con.scalar(text("SELECT clock_timestamp()"))
    asset = EvidenceAsset.model_validate_json(row.record)
    assert sha256(row.record.encode()).hexdigest() == row.output_digest
    assert asset.retention.evidence_policy_ref.version == "1.0.1"
    loc = EvidenceLocator(
        execution_id=eid,
        node_run_id=row.node_run_id,
        attempt_id=row.attempt_id,
        evidence_id=asset.evidence_id,
        output_digest=row.output_digest,
    )
    grant = EvidenceAccessGrant(
        scope=scope, actor=ACTOR, task_id=source.task_id, manage_retention=True
    )
    store_root = host.root / "media"
    stored = (store_root / asset.storage.object_key).resolve()
    assert stored.is_relative_to(store_root.resolve())
    service = lifecycle(host)
    read = await service.read_evidence(grant, loc)
    assert read.status == "available" and read.asset == asset
    assert (
        read.content == stored.read_bytes()
        and sha256(read.content).hexdigest() == asset.sha256
    )
    assert (
        await service.read_evidence(
            grant.model_copy(update={"task_id": "wrong.task"}), loc
        )
    ).status == "denied"
    hold = await service.place_hold(
        grant, loc, "independent.review-hold", reason="review_case", at=moment
    )
    assert hold.placed_at == moment
    assert (await service.dispose(grant, loc, at=moment)).status == "review_hold"
    assert stored.exists()
    async with app.connect() as con:
        released_at = await con.scalar(text("SELECT clock_timestamp()"))
    await service.release_hold(
        grant, eid, hold.hold_id, reason="review_complete", at=released_at
    )
    await _kill_after_unlink(host, grant, loc, released_at, stored)
    await app.dispose()
    reopened = lifecycle(host)
    assert (await reopened.read_evidence(grant, loc)).status == "disposition_pending"
    first, second = await asyncio.gather(
        reopened.dispose(grant, loc, at=released_at),
        reopened.dispose(grant, loc, at=released_at),
    )
    assert first == second and first.status == "disposed"
    assert first.intent is not None and first.completion is not None
    assert first.completion.outcome == "absent_on_recovery"
    assert first.intent.requested_at == released_at
    assert (
        first.intent.references[0].expires_at
        <= released_at
        <= first.completion.completed_at
    )
    expired = await reopened.read_evidence(grant, loc)
    assert (
        expired.status == "expired"
        and expired.asset == asset
        and expired.content is None
    )
    report = {
        "check": "evidence_lifecycle",
        "held": True,
        "crash_signal": "SIGKILL",
        "recovered": first.completion.outcome,
        "record_preserved": True,
        "evidence_sha256": asset.sha256,
        "expires_at": first.intent.references[0].expires_at.isoformat(),
        "released_at": released_at.isoformat(),
        "completed_at": first.completion.completed_at.isoformat(),
        "policy_version": "1.0.1",
    }
    (output / "evidence-disposition.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report), flush=True)


async def child() -> None:
    config = json.loads(sys.stdin.readline())
    assert (
        Path(__import__("jimu_runtime").__file__)
        .resolve()
        .is_relative_to(Path(sys.prefix))
    )
    engine = create_postgres_engine(make_url(config["database"]))
    try:
        host = Harness(engine, Path(config["directory"]), config["endpoint"])
        await lifecycle(host, ObserveUnlink).dispose(
            EvidenceAccessGrant.model_validate_json(json.dumps(config["grant"])),
            EvidenceLocator.model_validate_json(json.dumps(config["locator"])),
            at=datetime.fromisoformat(config["at"]),
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(child())
