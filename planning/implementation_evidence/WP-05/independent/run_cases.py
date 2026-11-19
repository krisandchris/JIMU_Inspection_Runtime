"""Run the installed configured Worker in separate OS processes with a real test database."""

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from audit import AuditScope, audit, check_dispatches
from cases import CASES, read_frame
from harness import Harness
from loopback import ParkingProvider
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from tests.fixtures.wp03.postgres.support import isolated_postgres


def emit(check: str, **fields: object) -> None:
    print(json.dumps({"check": check, **fields}, sort_keys=True), flush=True)


@dataclass(frozen=True)
class WorkerProcess:
    child: asyncio.subprocess.Process
    pid: int
    trace: Path | None

    async def call(self, word: str) -> dict[str, object]:
        assert self.child.stdin is not None and self.child.stdout is not None
        self.child.stdin.write(json.dumps(word).encode() + b"\n")
        await self.child.stdin.drain()
        raw = await asyncio.wait_for(self.child.stdout.readline(), timeout=40)
        if not raw:
            raise RuntimeError(
                "configured Worker stopped before acknowledging its instruction"
            )
        return json.loads(raw)

    async def close(self, *, crash: bool = False) -> None:
        if self.child.returncode is not None:
            return
        if crash:
            os.kill(self.pid, signal.SIGKILL)
        else:
            assert self.child.stdin is not None
            self.child.stdin.close()
        code = await asyncio.wait_for(self.child.wait(), timeout=8)
        assert code in (-9, 137) if crash else code == 0
        emit("worker_process_closed", pid=self.pid, signal="SIGKILL" if crash else None)


async def launch(
    host,
    execution_id,
    owner,
    trace,
    *,
    installed: bool,
    pause: Path | None = None,
    pause_after_admission: bool = False,
    revoke_model: bool = False,
) -> WorkerProcess:
    argv = [sys.executable, str(Path(__file__).with_name("worker_entry.py"))]
    if trace is not None:
        argv = ["strace", "-f", "-qq", "-e", "trace=execve", "-o", str(trace), *argv]
    child = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert child.stdin is not None and child.stdout is not None
    config = {
        "database": host.db.url.render_as_string(hide_password=False),
        "endpoint": host.endpoint.url,
        "root": str(host.root),
        "revision": host.revision,
        "execution_id": execution_id,
        "owner": owner,
        "installed": installed,
        "diagnose": os.environ.get("WP05_PROBE_DIAGNOSTICS") == "1",
        "pause": None if pause is None else str(pause),
        "pause_after_admission": pause_after_admission,
        "revoke_model": revoke_model,
    }
    child.stdin.write(json.dumps(config).encode() + b"\n")
    await child.stdin.drain()
    data = await asyncio.wait_for(child.stdout.readline(), timeout=40)
    if not data:
        raise RuntimeError(
            f"configured Worker failed to start, exit={await child.wait()}"
        )
    ready = json.loads(data)
    emit(
        "worker_process_ready",
        pid=ready["ready"],
        runtime_install=ready["runtime_install"],
    )
    return WorkerProcess(child, ready["ready"], trace)


async def _committed_node(app, execution_id: str, node_id: str) -> bool:
    async with app.connect() as connection:
        return (
            await connection.scalar(
                text("""
            SELECT 1 FROM jimu_workflow.authority_outputs a
            JOIN jimu_workflow.node_runs n USING (project_id, client_id, execution_id, node_run_id)
            WHERE a.execution_id=:e AND n.node_id=:n LIMIT 1
        """),
                {"e": execution_id, "n": node_id},
            )
        ) is not None


async def _failed_batch_committed(app, execution_id: str) -> bool:
    async with app.connect() as connection:
        return (
            await connection.scalar(
                text("""
                SELECT 1 FROM jimu_workflow.produced_outputs
                WHERE execution_id=:e AND output_kind='worker_message'
                  AND record->>'kind'='observation'
                  AND record->>'authority' IS NULL
                  AND record#>>'{batch,outcome}' IN ('parse_error', 'association_error')
                LIMIT 1
                """),
                {"e": execution_id},
            )
        ) is not None


async def _diagnostics(app, execution_id: str) -> None:
    async with app.connect() as connection:
        rows = (
            await connection.execute(
                text("""
                SELECT n.node_id, n.status,
                    p.record->'error'->>'code' AS error,
                    r.record->'reason'->>'code' AS attempt_reason
                FROM jimu_workflow.node_runs n
                LEFT JOIN jimu_workflow.produced_outputs p
                    ON (n.execution_id,n.node_run_id) = (p.execution_id,p.node_run_id)
                    AND p.output_key='node.output'
                LEFT JOIN jimu_workflow.attempt_results r
                    ON (p.execution_id,p.node_run_id,p.attempt_id) = (r.execution_id,r.node_run_id,r.attempt_id)
                WHERE n.execution_id=:e
                ORDER BY n.node_id
            """),
                {"e": execution_id},
            )
        ).all()
        children = (
            await connection.execute(
                text("""
            SELECT c.step_id, c.attempt_status,
                   c.record #>> '{result,execution,attempt_result,error,code}' AS error,
                   c.record #>> '{result,execution,attempt_result,result,status}' AS returned_status,
                   c.record #> '{result,execution,attempt_result,result,diagnostics}' AS diagnostics
            FROM jimu_workflow.tool_step_completions c WHERE c.execution_id=:e
            ORDER BY c.step_id
        """),
                {"e": execution_id},
            )
        ).all()
        dispatches = (
            await connection.execute(
                text("""
            SELECT i.record #>> '{target,kind}' AS target,
                   i.record #>> '{target,invocation_origin,step_id}' AS step,
                   r.record->>'boundary' AS boundary,
                   r.record #>> '{error,code}' AS error_code,
                   r.record->>'response_bytes_omission_reason' AS omission
            FROM jimu_workflow.dispatch_intents i
            LEFT JOIN jimu_workflow.dispatch_results r USING (project_id, client_id, execution_id, dispatch_id)
            WHERE i.execution_id=:e ORDER BY i.recorded_at
        """),
                {"e": execution_id},
            )
        ).all()
    emit(
        "durable_node_diagnostics",
        rows=[tuple(row) for row in rows],
        child_steps=[tuple(row) for row in children],
        dispatches=[tuple(row) for row in dispatches],
    )


async def _wait_checkpoint(marker: Path) -> None:
    while not marker.exists():
        await asyncio.sleep(0.025)


async def _context(app, execution_id: str):
    async with app.connect() as connection:
        return await connection.scalar(
            text("""
            SELECT record->'snapshot' FROM jimu_workflow.produced_outputs
            WHERE execution_id=:e AND output_kind='worker_message' AND record->>'kind'='context'
        """),
            {"e": execution_id},
        )


async def _verify_tool_checkpoint(
    app, execution_id: str, after_admission: bool
) -> None:
    async with app.connect() as connection:
        committed = (
            await connection.execute(
                text("""
            SELECT step_id, attempt_status FROM jimu_workflow.tool_step_completions
            WHERE execution_id=:e ORDER BY step_id
        """),
                {"e": execution_id},
            )
        ).all()
        sent = (
            (
                await connection.execute(
                    text("""
            SELECT record #>> '{target,invocation_origin,step_id}' FROM jimu_workflow.dispatch_intents
            WHERE execution_id=:e AND record #>> '{target,kind}'='tool'
        """),
                    {"e": execution_id},
                )
            )
            .scalars()
            .all()
        )
        children = (
            (
                await connection.execute(
                    text("""
                SELECT step_id FROM jimu_workflow.tool_step_attempts WHERE execution_id=:e
                """),
                    {"e": execution_id},
                )
            )
            .scalars()
            .all()
        )
    assert [tuple(row) for row in committed] == [("wheel_segmentation", "succeeded")]
    assert sent == ["wheel_segmentation"]
    expected_children = set(sent)
    if after_admission:
        expected_children.add("tactile_paving_segmentation")
    assert set(children) == expected_children
    emit(
        "before_process_death",
        committed_steps=sent,
        admitted_child_steps=sorted(children),
    )


async def _verify_admitted_child_history(
    connection, execution_id: str, retry: bool
) -> None:
    rows = (
        await connection.execute(
            text("""
                SELECT a.attempt_id, r.status AS terminal_status, c.attempt_status,
                       EXISTS (
                           SELECT 1 FROM jimu_workflow.dispatch_intents AS i
                           WHERE i.execution_id=s.execution_id AND i.attempt_id=s.child_attempt_id
                       ) AS has_dispatch
                FROM jimu_workflow.tool_step_attempts AS s
                JOIN jimu_workflow.attempts AS a
                  ON (a.project_id,a.client_id,a.execution_id,a.node_run_id,a.attempt_id)
                   = (s.project_id,s.client_id,s.execution_id,s.child_node_run_id,s.child_attempt_id)
                LEFT JOIN jimu_workflow.attempt_results AS r
                  ON (r.project_id,r.client_id,r.execution_id,r.node_run_id,r.attempt_id)
                   = (a.project_id,a.client_id,a.execution_id,a.node_run_id,a.attempt_id)
                LEFT JOIN jimu_workflow.tool_step_completions AS c
                  ON (c.project_id,c.client_id,c.execution_id,c.child_node_run_id,c.child_attempt_id)
                   = (s.project_id,s.client_id,s.execution_id,s.child_node_run_id,s.child_attempt_id)
                WHERE s.execution_id=:e AND s.step_id='tactile_paving_segmentation'
                ORDER BY a.attempt_number
            """),
            {"e": execution_id},
        )
    ).all()
    assert len(rows) == (2 if retry else 1), rows
    assert (
        rows[0].terminal_status in {"superseded", "cancelled", "timed_out", "failed"}
        and not rows[0].has_dispatch
    )
    assert rows[0].attempt_status is None
    if retry:
        assert rows[1].attempt_status == "succeeded" and rows[1].has_dispatch
        assert rows[1].attempt_id != rows[0].attempt_id
    emit(
        "admitted_child_history",
        retry_permitted=retry,
        dispatch=[row.has_dispatch for row in rows],
    )


async def _audit_exhausted(connection, task, final, provider, store, frame):
    assert final.coverage.summary_result.value == "uncertain"
    assert final.coverage.object_count == 1 and not final.coverage.matches
    (decision,) = final.decisions
    assert decision.result.value == "uncertain"
    (evaluation,) = decision.rule_evaluations
    assert evaluation.result.guard.reasons
    wire = await check_dispatches(
        connection,
        AuditScope(task.idempotency_scope, final.execution_id),
        provider,
        store,
    )
    assert wire == dict.fromkeys(
        ("discovery", "grounding", "observation", "candidate"), 1
    )
    (os_dispatch,) = (
        await connection.execute(
            text("""
                SELECT tool_execution_id, step_id FROM jimu_workflow.tool_step_completions
                WHERE execution_id=:e
            """),
            {"e": final.execution_id},
        )
    ).all()
    assert os_dispatch.step_id == "wheel_segmentation"
    wheel = await store.read_tool_sidecar(
        task.idempotency_scope,
        final.execution_id,
        os_dispatch.tool_execution_id,
        "wheel_mask",
    )
    assert wheel.asset.spatial.original_media_ref.sha256 == frame.digest
    return {
        "coverage": final.coverage.coverage_status.value,
        "decisions": {"outside": "uncertain"},
        "objects": 1,
        "post": wire,
        "summary": "uncertain",
        "tool_sidecars": 1,
    }


async def _audit_revoked(connection, task, final, provider):
    assert not provider.errors and not provider.requests
    assert final.coverage.discovery_outcome.value == "failed"
    assert final.coverage.object_count == 0 and not final.decisions
    assert final.coverage.summary_result.value == "uncertain"
    receipt = (
        await connection.execute(
            text("""
                SELECT i.record#>>'{target,kind}', r.record->>'boundary',
                       r.record#>>'{error,code}', r.record->>'provider_request_id'
                FROM jimu_workflow.dispatch_intents as i
                JOIN jimu_workflow.dispatch_results as r
                  USING (project_id, client_id, execution_id, dispatch_id)
                WHERE i.execution_id=:e
            """),
            {"e": final.execution_id},
        )
    ).one()
    assert tuple(receipt) == (
        "model_unselected",
        "not_dispatched",
        "model.ownership_unavailable",
        None,
    )
    emit("revoked_historical_ownership", receipt=tuple(receipt), physical_post=0)
    assert final.coverage.release_id == task.release_id
    return {
        "coverage": final.coverage.coverage_status.value,
        "decisions": {},
        "objects": 0,
        "post": {},
        "summary": "uncertain",
        "tool_sidecars": 0,
    }


async def run_case(app, root, name, installed, trace_dir, restart_at) -> None:
    revoked = restart_at == "N01_REVOKED"
    after_admission = restart_at in {"N05_ADMITTED_NO_RETRY", "N05_ADMITTED_RETRY"}
    allow_retry = restart_at == "N05_ADMITTED_RETRY"
    exhausted = restart_at == "N05_ADMITTED_NO_RETRY"
    tool_restart = restart_at == "N05" or after_admission
    stimulus = CASES[name]
    frame = read_frame(scene.DIRECTORY, stimulus.image_variant)
    deployment = (
        scene.capability_snapshot(scene.load_source()).model_deployments[0].identity
    )
    provider = ParkingProvider(
        stimulus,
        frame,
        model_id="independent-parking-vision",
        deployment_version=deployment.deployment_version,
        model_version=deployment.model_version,
    )
    async with provider.serving():
        assert provider.url is not None
        host = Harness(app, root, provider.url)
        initial_release = host.release(
            0,
            allow_second_tool_attempt=allow_retry,
            omit_candidate=name == "candidate_not_configured",
        )
        created = await host.accept(
            host.request(name, frame),
            frame,
            retries=stimulus.first_batch_extra_slot,
            recover_tool=tool_restart,
        )
        execution_id = created.execution.execution_id
        assert created.execution.pins.bundle_digest == initial_release.bundle_digest
        traces = []

        def trace_for(phase: str) -> Path | None:
            if trace_dir is None:
                return None
            path = root / f"{name}-{phase}.execve.raw"
            traces.append(path)
            return path

        marker = root / "checkpoint-before-second-tool"
        process = await launch(
            host,
            execution_id,
            "wp05.first",
            trace_for("first"),
            installed=installed,
            pause=marker if tool_restart else None,
            pause_after_admission=after_admission,
        )
        first_pid = process.pid
        try:
            restart = restart_at is not None
            run_steps = []
            for _ in range(80):
                interrupted = False
                if restart and tool_restart:
                    pending = asyncio.create_task(process.call("step"))
                    checkpoint = asyncio.create_task(_wait_checkpoint(marker))
                    done, _ = await asyncio.wait(
                        (pending, checkpoint), return_when=asyncio.FIRST_COMPLETED
                    )
                    if checkpoint in done:
                        await _verify_tool_checkpoint(
                            app, execution_id, after_admission
                        )
                        assert not await _committed_node(app, execution_id, "N05")
                        await process.close(crash=True)
                        with suppress(RuntimeError, ConnectionError):
                            await pending
                        tick = {"step": "interrupted-after-first-tool"}
                        interrupted = True
                    else:
                        checkpoint.cancel()
                        with suppress(asyncio.CancelledError):
                            await checkpoint
                        tick = pending.result()
                else:
                    tick = await process.call("step")
                run_steps.append(tick["step"])
                if tick["step"] == "finalized":
                    break
                if tick["step"] in {"idle", "lease_lost"}:
                    await asyncio.sleep(0.20)
                checkpoint_reached = interrupted
                if restart and restart_at in {"N01", "N04", "N01_REVOKED"}:
                    checkpoint_reached = await _committed_node(
                        app, execution_id, "N01" if revoked else restart_at
                    )
                if restart and restart_at == "N04_RETRY":
                    checkpoint_reached = await _failed_batch_committed(
                        app, execution_id
                    )
                    if checkpoint_reached:
                        assert not await _committed_node(app, execution_id, "N04")
                if restart and checkpoint_reached:
                    if not interrupted:
                        await process.close(crash=True)
                    new_release = host.release(1)
                    host.revision = "1.1.0"
                    new = await host.accept(
                        host.request(f"{name}-new-release", frame), frame
                    )
                    assert new.execution.pins.bundle_digest == new_release.bundle_digest
                    assert (
                        new.execution.pins.bundle_digest
                        != created.execution.pins.bundle_digest
                    )
                    new_worker = await launch(
                        host,
                        new.execution.execution_id,
                        "wp05.release-b",
                        None,
                        installed=installed,
                    )
                    try:
                        assert (await new_worker.call("step"))["step"] == "applied"
                    finally:
                        await new_worker.close()
                    new_context = await _context(app, new.execution.execution_id)
                    assert (
                        new_context["device_ref"]["version"]
                        == new_context["camera_ref"]["version"]
                        == host.revision
                    )
                    emit(
                        "release_switched",
                        phase=restart_at,
                        pinned_original=created.execution.pins.bundle_digest,
                        newly_active=new.execution.pins.bundle_digest,
                        new_device_and_camera_revision=host.revision,
                    )
                    process = await launch(
                        host,
                        execution_id,
                        "wp05.replacement",
                        trace_for("replacement"),
                        installed=installed,
                        revoke_model=revoked,
                    )
                    assert process.pid != first_pid
                    restart = False
            else:
                raise AssertionError(
                    f"configured Worker did not finalize after {run_steps!r}"
                )
            assert not restart
            published = await process.call("read")
            final = ExecutionFinalization.model_validate_json(
                json.dumps(published["final"])
            )
            assert final.coverage.release_id == created.task.release_id
            assert final.coverage.bundle_digest == initial_release.bundle_digest
            async with app.connect() as connection:
                if after_admission:
                    await _verify_admitted_child_history(
                        connection, execution_id, allow_retry
                    )
                if revoked:
                    result = await _audit_revoked(
                        connection, created.task, final, provider
                    )
                elif exhausted:
                    result = await _audit_exhausted(
                        connection, created.task, final, provider, host.media, frame
                    )
                else:
                    result = await audit(
                        connection,
                        created.task,
                        final,
                        provider,
                        frame,
                        host.media,
                        name,
                    )
            context = await _context(app, execution_id)
            assert (
                context["device_ref"]["version"]
                == context["camera_ref"]["version"]
                == "1.0.0"
            )
            await process.close()
            evidence = [
                {"pid": int(match["pid"]), "tool": match["tool"]}
                for path in traces
                for line in path.read_text().splitlines()
                if (
                    match := re.match(
                        r'(?P<pid>\d+) execve\("[^"]+", \["[^"]+"(?:\.\.\.)?, "-I", '
                        r'"/proc/self/fd/\d+", "(?P<tool>[^"]+)"\], .* = 0$',
                        line,
                    )
                )
            ]
            if trace_dir is not None:
                (trace_dir / f"{name}.cpu.json").write_text(
                    json.dumps(evidence, sort_keys=True, indent=2) + "\n"
                )
                if exhausted:
                    assert [row["tool"] for row in evidence] == [
                        "wheel_segmentation"
                    ], evidence
                else:
                    assert len(evidence) == result["objects"] * 3, evidence
            emit(
                "scenario_terminal_audit",
                name=name,
                device_and_camera_revision=context["device_ref"]["version"],
                product_steps=run_steps,
                observed_cpu_execve=len(evidence),
                after_release_switch=restart_at,
                **result,
            )
        except Exception:
            await _diagnostics(app, execution_id)
            emit(
                "provider_transcript_on_failure",
                errors=provider.errors,
                calls=[(r.role, r.box, r.object_id) for r in provider.requests],
            )
            raise
        finally:
            await process.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", default=["known_violation"])
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--trace-dir", type=Path)
    parser.add_argument(
        "--restart-at",
        choices=[
            "N01",
            "N01_REVOKED",
            "N04",
            "N04_RETRY",
            "N05",
            "N05_ADMITTED_NO_RETRY",
            "N05_ADMITTED_RETRY",
        ],
    )
    options = parser.parse_args()
    if options.trace_dir is not None:
        options.trace_dir = options.trace_dir.absolute()
        options.trace_dir.mkdir(exist_ok=True)
    with (
        isolated_postgres() as db,
        TemporaryDirectory(prefix="wp05-independent-cases-") as directory,
    ):
        owner = create_postgres_engine(db.migration_url)
        app = create_postgres_engine(db.application_url)
        try:
            assert len(await apply_migrations(owner, db.target)) >= 7
            for name in options.cases:
                await run_case(
                    app,
                    Path(directory) / name,
                    name,
                    options.installed,
                    options.trace_dir,
                    options.restart_at,
                )
        finally:
            await app.dispose()
            await owner.dispose()


if __name__ == "__main__":
    asyncio.run(main())
