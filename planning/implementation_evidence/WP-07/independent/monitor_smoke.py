"""Run actual WP07 compose from within a disposable signed WP05 database fixture."""

import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import urlopen

import psycopg
from psycopg import sql
from pytest import MonkeyPatch
from sqlalchemy import URL

from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from tests.contract.test_wp05_worker_inspection import test_configured_worker_executes_signed_parking_scene
from tests.fixtures.wp03.postgres import support
from tests.fixtures.wp05 import perception

CASE = Path(os.environ["JIMU_CAPTURE_DIR"])
ROOT = Path.cwd()
PROJECT = "jimu-monitor-" + secrets.token_hex(6)
NETWORK = PROJECT + "-db"
ROLE = "monitor_reader"
PASSWORD = secrets.token_urlsafe(38)
SCRAPE = secrets.token_urlsafe(38)
GRAFANA = secrets.token_urlsafe(38)
SECRET = CASE / "secrets"
SECRET.mkdir(mode=0o700, exist_ok=True)
ORIGINAL_BOOTSTRAP = support._bootstrap


def execute(*args, env=None, limit=30):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=limit, env=env).stdout.strip()


def bootstrap(port, admin_password, target, owner_password, app_password):
    version = ORIGINAL_BOOTSTRAP(port, admin_password, target, owner_password, app_password)
    with psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=port, user="postgres",
                         password=admin_password, dbname="postgres", sslmode="disable",
                         connect_timeout=3, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(ROLE), sql.Literal(PASSWORD)))
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(target.database), sql.Identifier(ROLE)))
        connection.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(ROLE)))
    return version


async def provision(owner, scope):
    async with owner.begin() as connection:
        await connection.exec_driver_sql("GRANT USAGE ON SCHEMA jimu_workflow TO monitor_reader")
        tables = ("executions", "tasks", "execution_events", "node_runs", "attempts",
                  "late_responses", "produced_outputs", "dispatch_intents",
                  "dispatch_results", "tool_step_attempts", "attempt_results",
                  "tool_step_completions", "intake_intents", "jobs")
        await connection.exec_driver_sql("GRANT SELECT ON " + ", ".join("jimu_workflow." + table for table in tables) + " TO monitor_reader")
        owned = (scope.project_id, scope.client_id)
        # Add a distinct client in this synthetic database for a scope-isolation sentinel.
        await connection.exec_driver_sql("""
            INSERT INTO jimu_workflow.intake_intents
                (project_id, client_id, request_id, idempotency_key, submission_digest, submission, issuer, subject)
                SELECT project_id, 'isolation.sentinel', request_id, idempotency_key, submission_digest, submission, issuer, subject
                FROM jimu_workflow.intake_intents WHERE project_id = %s AND client_id = %s
        """, owned)
        await connection.exec_driver_sql("""
            INSERT INTO jimu_workflow.tasks
                (project_id, client_id, task_id, request_id, release_id, bundle_digest, record, status)
                SELECT project_id, 'isolation.sentinel', task_id, request_id, release_id, bundle_digest, record, 'failed'
                FROM jimu_workflow.tasks WHERE project_id = %s AND client_id = %s
        """, owned)
        assert (await connection.exec_driver_sql("SELECT count(*) FROM jimu_workflow.tasks")).scalar_one() == 2


def write_secret(name, value):
    path = SECRET / name
    path.write_text(value + "\n")
    path.chmod(0o444)
    return str(path)


def read_only_database(database):
    reader = database.application_url.set(username=ROLE, password=PASSWORD)
    with psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=reader.port,
                         user=ROLE, password=PASSWORD, dbname=reader.database, sslmode="disable",
                         connect_timeout=3, autocommit=True) as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert connection.execute("SELECT has_table_privilege(current_user, 'jimu_workflow.tasks', 'INSERT')").fetchone() == (False,)
        assert connection.execute("SELECT has_table_privilege(current_user, 'jimu_workflow.tasks', 'SELECT')").fetchone() == (True,)


RUN_REQUEST = """
import base64, json, sys, time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
request = json.loads(sys.stdin.readline())
try:
    data = request.get('data')
    source = Request(request['url'], headers=request.get('headers', {}),
        data=None if data is None else json.dumps(data).encode())
    with urlopen(source, timeout=5) as response:
        text = response.read().decode()
        try:
            text = json.loads(text)
        except ValueError:
            pass
        print(json.dumps({'code': response.status, 'body': text}))
except HTTPError as error:
    print(json.dumps({'code': error.code}))
"""


def compose_smoke(database, scope, ids):
    reader = URL.create("postgresql+psycopg", username=ROLE, password=PASSWORD,
                        host="jimu-db", port=5432, database=database.target.database,
                        query={"sslmode": "disable"})
    config = {
        "JIMU_OBSERVABILITY_RUNTIME_IMAGE": os.environ["JIMU_CAPTURE_RUNTIME"],
        "JIMU_OBSERVABILITY_PROMETHEUS_IMAGE": os.environ["JIMU_CAPTURE_PROM"],
        "JIMU_OBSERVABILITY_GRAFANA_IMAGE": os.environ["JIMU_CAPTURE_GRAFANA"],
        "JIMU_OBSERVABILITY_PROJECT_ID": scope.project_id,
        "JIMU_OBSERVABILITY_CLIENT_ID": scope.client_id,
        "JIMU_OBSERVABILITY_DB_NETWORK": NETWORK,
        "JIMU_OBSERVABILITY_DB_URL_FILE": write_secret("db", reader.render_as_string(hide_password=False)),
        "JIMU_OBSERVABILITY_TOKEN_FILE": write_secret("scrape", SCRAPE),
        "JIMU_OBSERVABILITY_GRAFANA_PASSWORD_FILE": write_secret("grafana", GRAFANA),
        "JIMU_OBSERVABILITY_GRAFANA_PORT": "0",
    }
    env = os.environ | config
    compose = ("docker", "compose", "-p", PROJECT, "-f", str(ROOT / "ops/observability/compose.yml"))
    db_container = "jimu-wp03-pg-" + database.target.instance_id.hex
    # Database name comes from the fixture's generated instance id.
    db_matches = execute("docker", "ps", "--filter", f"label=jimu.wp03.instance={database.target.instance_id.hex}", "--format", "{{.Names}}")
    assert db_matches == db_container
    print("compose_project:", PROJECT, flush=True)
    execute("docker", "network", "create", "--internal", NETWORK)
    try:
        execute("docker", "network", "connect", "--alias", "jimu-db", NETWORK, db_container)
        execute(*compose, "config", "--quiet", env=env)
        execute(*compose, "up", "-d", "--pull", "never", env=env, limit=50)
        print("services:", execute(*compose, "ps", "--services", "--status", "running", env=env), flush=True)
        loopback = execute(*compose, "port", "grafana", "3000", env=env)
        assert loopback.startswith("127.0.0.1:") and int(loopback.split(":")[-1]) > 1024
        print("grafana_random_loopback:", loopback, flush=True)
        grafana_headers = {"Authorization": "Basic " + base64.b64encode(f"admin:{GRAFANA}".encode()).decode()}
        # The short-lived credential isn't placed in Docker's process arguments.
        # Invoke the script over stdin with a private transport for requests.

        def http(base, route, auth=None, data=None):
            request = {"url": base + route, "headers": auth or {}, "data": data}
            if data is not None:
                request["headers"]["Content-Type"] = "application/json"
            runner = RUN_REQUEST.replace("json.loads(sys.stdin.readline())", "json.loads(sys.stdin.readline())")
            run = subprocess.run((*compose, "exec", "-T", "observability", "python", "-c", runner),
                                 input=json.dumps(request) + "\n", capture_output=True, text=True,
                                 timeout=12, check=True, env=env)
            return json.loads(run.stdout)

        obs = "http://127.0.0.1:9080"
        prom = "http://prometheus:9090"
        graf = "http://grafana:3000"
        bearer = {"Authorization": f"Bearer {SCRAPE}"}
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            try:
                ready = http(obs, "/readyz", bearer)
                targets = http(prom, "/api/v1/targets?state=active")
                health = http(graf, "/api/health")
                rows = targets.get("body", {}).get("data", {}).get("activeTargets", [])
                if (ready["code"] == 200 and health["code"] == 200
                        and len(rows) == 1 and rows[0]["health"] == "up"):
                    break
            except subprocess.CalledProcessError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("Services did not report scoped readiness, target up, and Grafana healthy")
        assert http(obs, "/livez")["code"] == 200
        assert http(obs, "/readyz")["code"] == http(obs, "/metrics")["code"] == 401
        assert health["body"]["database"] == "ok"
        with urlopen("http://" + loopback + "/api/health", timeout=4) as response:
            assert response.status == 200 and json.load(response)["database"] == "ok"
        print("grafana_host_loopback_health:", 200, flush=True)
        assert rows[0]["scrapeUrl"] == "http://observability:9080/metrics"
        print("observability_ready:", ready["body"], flush=True)
        print("anonymous_metrics_and_ready:", 401, flush=True)
        print("prometheus_target_health:", rows[0]["health"], flush=True)
        print("grafana_health:", health["body"]["database"], "version:", health["body"]["version"], flush=True)

        def query(expr):
            result = http(prom, "/api/v1/query?" + urlencode({"query": expr}))
            assert result["code"] == 200 and result["body"]["status"] == "success"
            return result["body"]["data"]["result"]

        accepted = query("jimu_retained_accepted_tasks")
        tasks = query("jimu_retained_tasks")
        assert len(accepted) == len(tasks) == 1 and accepted[0]["value"][1] == tasks[0]["value"][1] == "1"
        assert set(accepted[0]["metric"]) == {"__name__", "instance", "job"}
        assert set(tasks[0]["metric"]) == {"__name__", "instance", "job", "status"}
        assert tasks[0]["metric"]["status"] == "completed"
        for identifier in (*ids, scope.project_id, scope.client_id, "isolation.sentinel", SCRAPE, PASSWORD):
            assert identifier not in json.dumps([accepted, tasks])
        assert not query("jimu_retained_reported_tokens")
        print("prometheus_scoped_task_count:", accepted[0]["value"][1], "database_task_count: 2", flush=True)
        print("prometheus_task_labels:", sorted(tasks[0]["metric"]), flush=True)
        print("prometheus_unreported_token_series:", 0, flush=True)

        datasource = http(graf, "/api/datasources/uid/jimu-prometheus", grafana_headers)
        assert datasource["code"] == 200
        source = datasource["body"]
        assert source["type"] == "prometheus" and source["url"] == prom and source["readOnly"] is True
        dashboard = http(graf, "/api/dashboards/uid/jimu-runtime", grafana_headers)
        assert dashboard["code"] == 200 and dashboard["body"]["meta"]["provisioned"]
        panels = dashboard["body"]["dashboard"]["panels"]
        loaded = [target for panel in panels for target in panel.get("targets", []) if "expr" in target]
        assert loaded and loaded[0]["expr"] == "jimu_retained_tasks"
        start_ms, end_ms = int(time.time() * 1000) - 120_000, int(time.time() * 1000)
        queries = [
            {"refId": f"P{index}", "expr": target["expr"], "datasource": target["datasource"],
             "datasourceId": source["id"], "instant": True, "range": False, "intervalMs": 1000,
             "maxDataPoints": 120, "editorMode": "code"}
            for index, target in enumerate(loaded)
        ]
        series = http(graf, "/api/ds/query", grafana_headers,
                      {"from": str(start_ms), "to": str(end_ms), "queries": queries})
        assert series["code"] == 200, series["code"]
        results = series["body"]["results"]
        assert set(results) == {entry["refId"] for entry in queries}
        assert all(int(row.get("status", 200)) == 200 and "error" not in row for row in results.values())
        frames = results["P0"]["frames"]
        assert frames and frames[0]["data"]["values"][-1] == [1]
        print("grafana_provisioned_dashboard:", dashboard["body"]["dashboard"]["uid"], "datasource:", source["uid"], flush=True)
        print("grafana_dashboard_queries_executed:", len(queries), "first_panel_value:", frames[0]["data"]["values"][-1][0], flush=True)
    except BaseException:
        (CASE / "service-diagnostics.log").write_text(
            subprocess.run((*compose, "logs", "--no-color", "--tail", "45"),
                           capture_output=True, text=True, env=env, timeout=12).stdout)
        raise
    finally:
        execute(*compose, "down", "-v", "-t", "2", env=env, limit=45)
        execute("docker", "network", "disconnect", NETWORK, db_container)
        execute("docker", "network", "rm", NETWORK)
        for path in SECRET.glob("*"):
            path.unlink()
        SECRET.rmdir()
        assert not execute("docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}")
        assert not execute("docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}")
        assert not execute("docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}")
        print("cleaned_compose_containers_volumes_and_networks:", True, flush=True)


@asynccontextmanager
async def composed_database():
    with patch.object(support, "_bootstrap", bootstrap), support.isolated_postgres() as database:
        owner = create_postgres_engine(database.migration_url)
        app = create_postgres_engine(database.application_url)
        try:
            await apply_migrations(owner, database.target)
            yield app
            scope, ids = capture[-1]
            await provision(owner, scope)
            read_only_database(database)
            print("read_only_scoped_postgres:", database.server_version, flush=True)
            compose_smoke(database, scope, ids)
        finally:
            await app.dispose()
            await owner.dispose()


capture = []
original = perception.perception_fixture

@asynccontextmanager
async def fixture(*args, **kwargs):
    async with original(*args, **kwargs) as runtime:
        execution = runtime.accepted.execution
        task = runtime.accepted.task
        capture.append((task.idempotency_scope, (task.task_id, execution.execution_id, execution.trace.trace_id)))
        yield runtime


if __name__ == "__main__":
    # Rebind only the current process's test composition, preserving the repository.
    from tests.contract import test_wp05_worker_inspection as worker_case
    with MonkeyPatch.context() as mp:
        mp.setattr(perception, "migrated_application", composed_database)
        mp.setattr(worker_case, "perception_fixture", fixture)
        workspace = CASE / "workspace"
        workspace = workspace / PROJECT
        workspace.mkdir(parents=True, exist_ok=True)
        test_configured_worker_executes_signed_parking_scene(workspace, set(), "usable", mp)
    print("signed_workflow_and_monitor_smoke: PASS", flush=True)
