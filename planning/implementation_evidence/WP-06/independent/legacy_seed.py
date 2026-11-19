"""Create WP-05 history with the frozen WP-05 package before upgrading PostgreSQL."""

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from cases import read_frame
from harness import Harness, _rebind_artifact
from sqlalchemy.engine import make_url
from worker_entry import open_worker

import jimu_runtime
from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.bundle.compiler import SourceIdentity
from jimu_runtime.workflow.postgres import (
    MigrationTarget,
    apply_migrations,
    create_postgres_engine,
)


async def main() -> None:
    config = json.loads(sys.stdin.readline())
    assert Path(jimu_runtime.__file__).resolve().is_relative_to(Path(sys.prefix))
    owner = create_postgres_engine(make_url(config["migration"]))
    app = create_postgres_engine(make_url(config["application"]))
    try:
        config["target"]["instance_id"] = UUID(config["target"]["instance_id"])
        target = MigrationTarget(**config["target"])
        assert await apply_migrations(owner, target) == tuple(range(1, 9))
        assert await apply_migrations(owner, target) == ()
        original_source = scene.load_source
        directory = Path(config["directory"])

        def short_retention_source():
            source = original_source()
            (policy,) = [
                row
                for row in source.artifacts
                if row.artifact_id == "geometry-evidence"
            ]
            content = json.loads(policy.content)
            assert content["policy"]["retention_duration_seconds"] == 86400
            content["version"] = "1.0.1"
            content["policy"]["retention_duration_seconds"] = 1
            replacement = replace(
                policy,
                content=json.dumps(content, sort_keys=True).encode(),
                version="1.0.1",
                origin=SourceIdentity(
                    artifact_id="independent.wp06.geometry-retention", version="1.0.1"
                ),
            )
            return _rebind_artifact(source, policy, replacement)

        # Only the independent host's signed test source is varied.
        scene.load_source = short_retention_source
        host = Harness(app, directory, config["endpoint"])
        frame = read_frame(scene.DIRECTORY, "base")
        release = host.release(0)
        accepted = await host.accept(host.request("wp06-source", frame), frame)
        eid = accepted.execution.execution_id
        worker = await open_worker(host, accepted.task, eid)
        steps = []
        for _ in range(16):
            tick = await worker.run_once(
                host.scope, eid, owner="wp06.source", lease_ms=5000
            )
            steps.append(tick.value)
            if tick.value == "finalized":
                break
        final = await worker.read_finalization(host.scope, eid)
        assert (
            final is not None and final.coverage.summary_result.value == "violation"
        ), steps
        print(
            json.dumps(
                {
                    "task": accepted.task.task_id,
                    "execution": eid,
                    "bundle": release.bundle_digest,
                    "scope": host.scope.model_dump(mode="json"),
                    "steps": steps,
                    "installed": jimu_runtime.__file__,
                }
            ),
            flush=True,
        )
    finally:
        await app.dispose()
        await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
