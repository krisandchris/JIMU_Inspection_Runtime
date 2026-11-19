"""The installed local deployment, using an exclusively owned Compose project."""

import hashlib
import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "ops/compose/compose.yaml"
_DOCKER = str(_COMPOSE.with_name("local-docker.sh"))
_APPROVED_FRAME = _COMPOSE.with_name("approved-frame.yaml")
_HOST = "/opt/jimu/host/synthetic_host.py"
_EXEC_RUNTIME = ("exec", "-T", "runtime", "python")
_BOOTSTRAP_FAILURE = """
import os, sys
from jimu_runtime.deployment import database

def end_after_roles(*args):
    create(*args)
    os._exit(91)

def end_after_database(*args):
    os._exit(91)

create = database._create_roles
if sys.argv[1] == "roles":
    database._create_roles = end_after_roles
else:
    database._finish_database = end_after_database
database.provision(database.database_target())
"""
_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection
_QUERY_RECEIPTS = '''
import asyncio, json, sys
from sqlalchemy import text
from jimu_runtime.deployment.database import app_url, database_target
from jimu_runtime.workflow.postgres import create_postgres_engine

async def report():
    engine = create_postgres_engine(app_url(database_target()))
    try:
        async with engine.connect() as conn:
            params = dict(zip(('project', 'client', 'execution'), sys.argv[1:], strict=True))
            if params["execution"]:
                rows = await conn.execute(text("""
                    SELECT dispatch_id, receipts.record ->> 'kind' AS kind,
                           receipts.record ->> 'response_digest' AS digest, intents.node_run_id
                    FROM jimu_workflow.dispatch_results receipts
                    JOIN jimu_workflow.dispatch_intents intents
                        USING (project_id, client_id, execution_id, dispatch_id)
                    WHERE project_id = :project AND client_id = :client
                      AND execution_id = :execution AND NOT late
                      AND boundary = 'acknowledged'
                    ORDER BY dispatch_id
                """), params)
            else:
                rows = await conn.execute(text("""
                    SELECT execution_id, dispatch_id
                    FROM jimu_workflow.dispatch_intents
                    WHERE project_id = :project AND client_id = :client AND send_admitted
                    ORDER BY execution_id, dispatch_id
                """), params)
            print(json.dumps([dict(row) for row in rows.mappings()]))
    finally:
        await engine.dispose()

asyncio.run(report())
'''


def _command(args: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        pytest.fail(
            f"{args[0]} failed ({result.returncode})\n{result.stdout}{result.stderr}"
        )
    return result.stdout.strip()


def _bootstrap_interrupt(compose: list[str], env: dict[str, str], phase: str) -> None:
    result = subprocess.run(
        [
            *compose,
            "run",
            "--rm",
            "--no-deps",
            "migrate",
            "python",
            "-c",
            _BOOTSTRAP_FAILURE,
            phase,
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert result.returncode == 91, result.stderr


@pytest.fixture
def local_deployment_secrets(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    with TemporaryDirectory(dir=tmp_path) as directory:
        credentials = Path(directory)
        token = token_urlsafe(32)
        for name in (
            "admin_password",
            "owner_password",
            "app_password",
            "fixture_token",
        ):
            path = credentials / name
            path.write_text(
                token if name == "fixture_token" else token_urlsafe(32),
                encoding="ascii",
            )
            path.chmod(0o444)
        yield credentials, token


def _completed(client: httpx.Client, path: str, *, wait: int = 35) -> httpx.Response:
    end = monotonic() + wait
    while True:
        try:
            response = client.get(path)
        except httpx.TransportError:
            if monotonic() >= end:
                raise
        else:
            if response.status_code not in (409, 503) or monotonic() >= end:
                return response
        sleep(0.25)


def test_compose_secret_requires_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jimu_runtime.deployment import database

    secret = tmp_path / "credential"
    monkeypatch.setattr(database, "Path", lambda *_: secret)
    secret.write_text("a" * 512 + "\n", encoding="ascii")
    assert database.read_secret("fixture_token") == "a" * 512
    secret.write_text("a" * 512 + "\nmore", encoding="ascii")
    with pytest.raises(ValueError, match="secret is absent or malformed"):
        database.read_secret("fixture_token")


@pytest.mark.skipif(
    os.environ.get("JIMU_RUN_COMPOSE_TEST") != "1",
    reason="requires preloaded pinned images, a locked cache, and an isolated Docker project",
)
def test_local_compose_workflow_retains_result_trace_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local_deployment_secrets: tuple[Path, str],
) -> None:
    suffix = uuid4().hex[:12]
    project = f"jimu-wp07-{suffix}"
    image = f"jimu-wp07-local:{suffix}"
    credentials, token = local_deployment_secrets
    approved_frame = tmp_path / "approved-frame.png"
    bundled_frame = _ROOT / (
        "examples/nonmotor_vehicle_illegal_parking/compilable/assets/synthetic-frame.png"
    )
    with Image.open(bundled_frame) as fixture:
        rgb = fixture.convert("RGB")
        rgb.putpixel((0, 0), (193, 142, 22))
        rgb.save(approved_frame, format="PNG")
    approved_frame.chmod(0o444)
    frame_digest = hashlib.sha256(approved_frame.read_bytes()).hexdigest()
    assert frame_digest != hashlib.sha256(bundled_frame.read_bytes()).hexdigest()
    env = dict(os.environ)
    env.update(
        JIMU_APP_IMAGE=image,
        JIMU_SECRET_DIR=str(credentials),
        JIMU_APPROVED_FRAME=str(approved_frame),
        DOCKER_HOST="tcp://127.0.0.1:1",
        DOCKER_CONTEXT="unused-fixture-context",
        JIMU_DB_INSTANCE=str(uuid4()),
        JIMU_API_PORT="0",
    )
    compose = [
        _DOCKER,
        "compose",
        "-p",
        project,
        "-f",
        str(_COMPOSE),
        "-f",
        str(_APPROVED_FRAME),
    ]

    def journal_dispatches(
        scope: dict[str, str], execution_id: str = ""
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = json.loads(
            _command(
                [
                    *compose,
                    *_EXEC_RUNTIME,
                    "-c",
                    _QUERY_RECEIPTS,
                    scope["project_id"],
                    scope["client_id"],
                    execution_id,
                ],
                env=env,
            )
        )
        return result

    try:
        build = _command(
            [
                "sh",
                "-c",
                'umask 077; exec "$@"',
                "build",
                str(_COMPOSE.with_name("build.sh")),
                image,
            ],
            env=env,
        )
        print(f"application image {build.splitlines()[-1]}")
        _command([*compose, "up", "-d", "--wait", "database"], env=env)
        _bootstrap_interrupt(compose, env, "roles")
        _bootstrap_interrupt(compose, env, "database")
        print(
            "database bootstrap survived separate interruptions after roles and CREATE DATABASE"
        )
        _command([*compose, "up", "-d", "--wait", "--wait-timeout", "70"], env=env)
        binding = _command([*compose, "port", "runtime", "8080"], env=env)
        host, port = binding.split(":")
        assert host == "127.0.0.1"

        def connect(sock: socket.socket, address: tuple[str, int]) -> None:
            if address != (host, int(port)):
                raise AssertionError(
                    "test connection outside its Compose loopback mapping"
                )
            _CONNECT(sock, address)

        def create_connection(
            address: tuple[str, int],
            timeout: float | None,
            source_address: tuple[str, int] | None = None,
        ) -> socket.socket:
            if address != (host, int(port)):
                raise AssertionError(
                    "test connection outside its Compose loopback mapping"
                )
            return _CREATE_CONNECTION(address, timeout, source_address)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket, "create_connection", create_connection)
        print(_command([*compose, "ps", "-a"], env=env))
        submission = json.loads(
            _command(
                [
                    *compose,
                    "exec",
                    "-T",
                    "runtime",
                    "python",
                    "/opt/jimu/host/synthetic_host.py",
                    "submission",
                ],
                env=env,
            )
        )
        scope = submission["idempotency_scope"]
        assert submission["media_ref"]["sha256"] == frame_digest
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Jimu-Project": scope["project_id"],
            "X-Jimu-Client": scope["client_id"],
        }
        task_path = "/api/v1/inference/tasks"
        with httpx.Client(
            base_url=f"http://{binding}", headers=headers, trust_env=False
        ) as client:
            anonymous = client.build_request("POST", task_path, json=submission)
            del anonymous.headers["authorization"]
            assert client.send(anonymous).status_code == 401
            assert client.post(
                task_path, json=submission, headers={"X-Jimu-Client": "other.client"}
            ).status_code in (403, 404)
            substituted = {
                **submission,
                "media_ref": {**submission["media_ref"], "sha256": "0" * 64},
            }
            assert client.post(task_path, json=substituted).status_code == 403
            accepted = client.post(task_path, json=submission)
            assert accepted.status_code == 202, accepted.text
            task_id = accepted.json()["task"]["task_id"]
            execution_id = accepted.json()["execution"]["execution_id"]
            result_path = f"{task_path}/{task_id}/result"
            trace_path = f"/api/v1/executions/{execution_id}/trace"
            result = _completed(client, result_path)
            assert result.status_code == 200, result.text
            assert [decision["result"] for decision in result.json()["decisions"]] == [
                "violation"
            ], result.text
            trace = client.get(trace_path, params={"limit": 100})
            assert trace.status_code == 200, trace.text
            trace_page = trace.json()
            events = trace_page["events"]
            through = trace_page["through_event_number"]
            assert trace_page["execution_id"] == execution_id
            assert trace_page["trace_id"] == submission["trace"]["trace_id"]
            assert trace_page["next_after_event_number"] is None
            assert through == len(events)
            assert [event["number"] for event in events] == list(range(1, through + 1))
            assert all(
                "payload" not in event and "trace" not in event for event in events
            )
            first = client.get(trace_path, params={"limit": 7})
            assert first.status_code == 200, first.text
            first_page = first.json()
            cursor = first_page["next_after_event_number"]
            assert cursor == 7
            assert first_page["through_event_number"] == through
            remaining = client.get(
                trace_path,
                params={
                    "limit": 100,
                    "after_event_number": cursor,
                    "through_event_number": through,
                },
            )
            assert remaining.status_code == 200, remaining.text
            remaining_page = remaining.json()
            assert remaining_page["execution_id"] == execution_id
            assert remaining_page["trace_id"] == trace_page["trace_id"]
            assert remaining_page["through_event_number"] == through
            assert remaining_page["next_after_event_number"] is None
            assert first_page["events"] + remaining_page["events"] == events
            successful_runs = {
                event["node_run_id"]: event["node_id"]
                for event in events
                if event["kind"] == "node_transition" and event["status"] == "succeeded"
            }
            receipts = journal_dispatches(scope, execution_id)
            model_receipts = [
                receipt for receipt in receipts if receipt["kind"] == "model"
            ]
            assert len(model_receipts) == 4, receipts
            tool_runs = {
                receipt["node_run_id"]
                for receipt in receipts
                if receipt["kind"] == "tool"
            }
            assert len(tool_runs) == 3 and tool_runs <= successful_runs.keys(), receipts
            primary_runs = [
                node for run, node in successful_runs.items() if run not in tool_runs
            ]
            assert sorted(primary_runs) == ["N01", "N02", "N03", "N04", "N05", "N06"]
            receipt = model_receipts[0]
            evidence_path = f"/api/v1/evidence/{receipt['dispatch_id']}"
            material = {
                "execution_id": execution_id,
                "digest": receipt["digest"],
                "representation": "raw",
            }
            evidence = client.get(evidence_path, params=material)
            assert evidence.status_code == 200, evidence.text
            assert hashlib.sha256(evidence.content).hexdigest() == receipt["digest"]
            assert client.get(
                evidence_path,
                params=material,
                headers={"X-Jimu-Client": "other.client"},
            ).status_code in (403, 404)

            for component in ("live", "ready", "worker"):
                assert client.get(f"/health/{component}").json() == {"status": "ok"}
            print(
                f"execution={execution_id} outcome=violation events={len(events)} "
                f"model_receipts={len(model_receipts)} tool_receipts=3 raw_sha256={receipt['digest']}"
            )
            replay_url = f"/api/v1/executions/{execution_id}"
            before_artifact = journal_dispatches(scope)
            assert len(before_artifact) == 7
            artifact_request = json.loads(
                _command(
                    [*compose, *_EXEC_RUNTIME, _HOST, "artifact-request", execution_id],
                    env=env,
                )
            )
            artifact = client.post(
                f"{replay_url}/artifact-replay",
                json=artifact_request,
                headers={"Idempotency-Key": "compose-rescore"},
            )
            assert artifact.status_code == 200, artifact.text
            artifact_id = artifact.json()["replay_run_id"]
            score = json.loads(
                _command(
                    [*compose, *_EXEC_RUNTIME, _HOST, "artifact-score", artifact_id],
                    env=env,
                )
            )
            assert score["source_execution_id"] == execution_id
            assert score["score"]["summary_result"] == "violation"
            assert score["model_calls"] == score["tool_calls"] == 0
            assert journal_dispatches(scope) == before_artifact
            print(
                "artifact replay read verified scoring evidence; no additional model or tool dispatches"
            )

            inference_request = json.loads(
                _command(
                    [
                        *compose,
                        *_EXEC_RUNTIME,
                        _HOST,
                        "inference-request",
                        execution_id,
                    ],
                    env=env,
                )
            )
            inference = client.post(
                f"{replay_url}/inference-replay",
                json=inference_request,
                headers={"Idempotency-Key": "compose-inference"},
            )
            assert inference.status_code == 202, inference.text
            replay_execution = inference.json()["target_execution_ref"]["record_id"]
            assert replay_execution != execution_id
            execution_url = f"/api/v1/executions/{replay_execution}"
            observe_until = monotonic() + 10
            while True:
                pending = client.get(execution_url)
                assert pending.status_code == 200, pending.text
                state = pending.json()["status"]
                if state == "running":
                    break
                assert state == "initialized" and monotonic() < observe_until, (
                    pending.text
                )
                sleep(0.05)
            replay_trace_url = f"{execution_url}/trace"
            prefix_response = client.get(replay_trace_url, params={"limit": 100})
            assert prefix_response.status_code == 200, prefix_response.text
            prefix = prefix_response.json()
            assert any(
                event["kind"] == "attempt_transition" and event["status"] == "running"
                for event in prefix["events"]
            ), prefix
            print(
                f"inference replay {replay_execution} has a persisted Attempt before runtime interruption"
            )
            _command([*compose, "kill", "-s", "SIGKILL", "runtime"], env=env)
            _command([*compose, "restart", "database"], env=env)
            _command(
                [*compose, "up", "-d", "--wait", "--wait-timeout", "70", "runtime"],
                env=env,
            )
        rebound = _command([*compose, "port", "runtime", "8080"], env=env)
        host, port = rebound.split(":")
        assert host == "127.0.0.1"
        with httpx.Client(
            base_url=f"http://{rebound}", headers=headers, trust_env=False
        ) as client:
            for component in ("live", "ready", "worker"):
                assert _completed(client, f"/health/{component}").json() == {
                    "status": "ok"
                }
            assert client.get(result_path).json() == result.json()
            assert (
                client.get(
                    trace_path, params={"limit": 100, "through_event_number": through}
                ).json()
                == trace_page
            )
            assert (
                client.get(evidence_path, params=material).content == evidence.content
            )
            end = monotonic() + 50
            while True:
                replay_state = client.get(execution_url)
                assert replay_state.status_code == 200, replay_state.text
                state = replay_state.json()["status"]
                if state not in ("initialized", "running"):
                    break
                if monotonic() >= end:
                    pytest.fail(
                        "inference Replay did not resume before the local deadline"
                    )
                sleep(0.25)
            assert state == "completed", replay_state.text
            reopened_prefix = client.get(
                replay_trace_url,
                params={
                    "limit": 100,
                    "through_event_number": prefix["through_event_number"],
                },
            )
            assert reopened_prefix.status_code == 200
            assert reopened_prefix.json() == prefix
            finished_trace = client.get(replay_trace_url, params={"limit": 100}).json()
            assert (
                finished_trace["through_event_number"] > prefix["through_event_number"]
            )
            assert finished_trace["events"][-1]["status"] == "completed"
            inference_receipts = journal_dispatches(scope, replay_execution)
            assert (
                sum(receipt["kind"] == "model" for receipt in inference_receipts) >= 4
            )
            assert sum(receipt["kind"] == "tool" for receipt in inference_receipts) >= 3
            assert journal_dispatches(scope, execution_id) == receipts
            print(
                f"inference replay resumed on the same new execution; {len(inference_receipts)} acknowledged dispatches"
            )

            drift_command = [
                *compose,
                "exec",
                "-T",
                "database",
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
            ]
            _command(
                [*drift_command, "GRANT jimu_workflow_owner TO jimu_workflow_app"],
                env=env,
            )
            try:
                assert client.get("/health/ready").status_code == 503
                denied_migrate = subprocess.run(
                    [*compose, "run", "--rm", "--no-deps", "migrate"],
                    cwd=_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=40,
                    check=False,
                )
                assert denied_migrate.returncode != 0
            finally:
                _command(
                    [
                        *drift_command,
                        "REVOKE jimu_workflow_owner FROM jimu_workflow_app",
                    ],
                    env=env,
                )
            assert _completed(client, "/health/ready").status_code == 200
            replay = client.post(task_path, json=submission)
            assert replay.status_code == 202 and replay.json() == accepted.json()
            print(
                "runtime and database restarted, identical task, result, trace, and raw evidence"
            )
    except (Exception, pytest.fail.Exception):
        print(_command([*compose, "ps", "-a"], env=env))
        print(_command([*compose, "logs", "migrate", "runtime"], env=env))
        raise
    finally:
        original_failure = sys.exception()
        try:
            _command([*compose, "down", "--volumes", "--remove-orphans"], env=env)
            assert not _command(
                [
                    _DOCKER,
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ]
            )
            if _command([_DOCKER, "image", "ls", "-q", image]):
                _command([_DOCKER, "image", "rm", image])
        except (Exception, pytest.fail.Exception) as cleanup_error:
            if original_failure is None:
                raise
            print(f"Compose resource cleanup failed separately: {cleanup_error}")
