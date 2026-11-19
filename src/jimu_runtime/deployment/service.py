"""Run the existing HTTP app and scoped workflow poller on one service lifecycle."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.workflow.postgres import WORKFLOW_APP_ROLE
from jimu_runtime.workflow.worker import WorkflowWorker

_LOG = logging.getLogger(__name__)


async def database_ready(engine: AsyncEngine, instance_comment: str) -> bool:
    try:
        async with asyncio.timeout(2), engine.connect() as conn:
            result = (
                await conn.execute(
                    text("""
                        SELECT current_user, pg_catalog.shobj_description(d.oid, 'pg_database'),
                               pg_catalog.to_regclass('jimu_workflow.jobs') IS NOT NULL,
                               r.rolcanlogin AND NOT (r.rolsuper OR r.rolcreaterole OR
                                   r.rolcreatedb OR r.rolreplication OR r.rolbypassrls)
                               AND NOT EXISTS (
                                   SELECT 1 FROM pg_catalog.pg_auth_members m
                                   WHERE m.member = r.oid
                               )
                               AND NOT pg_catalog.has_database_privilege(d.oid, 'CREATE,TEMPORARY')
                        FROM pg_catalog.pg_database d
                        JOIN pg_catalog.pg_roles r ON r.rolname = current_user
                        WHERE d.datname = pg_catalog.current_database()
                    """)
                )
            ).one()
    except Exception:
        return False
    return tuple(result) == (WORKFLOW_APP_ROLE, instance_comment, True, True)


async def wait_for_database(engine: AsyncEngine, instance_comment: str) -> None:
    """Cover PostgreSQL startup when Compose restarts both persistent services."""
    try:
        async with asyncio.timeout(20):
            while not await database_ready(engine, instance_comment):
                await asyncio.sleep(0.25)
    except TimeoutError:
        raise RuntimeError("provisioned database did not become ready") from None


@dataclass(slots=True)
class Health:
    last_healthy_poll: float | None = None

    def worker_ready(self, freshness: float) -> bool:
        return (
            self.last_healthy_poll is not None
            and monotonic() - self.last_healthy_poll <= freshness
        )


@dataclass(frozen=True, slots=True)
class Service:
    """Trusted runtime dependencies and the exact work queues this process serves."""

    engine: AsyncEngine = field(repr=False)
    worker: WorkflowWorker
    scopes: tuple[IdempotencyScope, ...]
    owner: str
    instance_comment: str
    poll_seconds: float = 0.2
    worker_freshness_seconds: float = 45.0

    def __post_init__(self) -> None:
        if not self.scopes or len(set(self.scopes)) != len(self.scopes):
            raise ValueError("worker requires explicit distinct scopes")
        if not 0.05 <= self.poll_seconds <= 5 or self.worker_freshness_seconds < 10:
            raise ValueError("invalid workflow health budget or poll interval")

    async def run_worker(self, health: Health) -> None:
        # A single awaited provider request must leave a poller available for
        # independently ready tool work. DB claims and resource leases still
        # govern which jobs and model/tool operations may actually execute.
        async with asyncio.TaskGroup() as workers:
            for _ in range(2):
                workers.create_task(self._poll_scope_queues(health))

    async def _poll_scope_queues(self, health: Health) -> None:
        cursors: dict[IdempotencyScope, str | None] = dict.fromkeys(self.scopes)
        while True:
            healthy = True
            for scope in self.scopes:
                try:
                    progress = await self.worker.run_next(
                        scope,
                        owner=self.owner,
                        lease_ms=20000,
                        after_execution_id=cursors[scope],
                    )
                except Exception:
                    _LOG.warning("scoped workflow poll failed")
                    healthy = False
                    continue
                cursors[scope] = progress.after_execution_id
                if progress.issues:
                    _LOG.warning("scoped workflow poll reported source issues")
                    healthy = False
            if healthy:
                health.last_healthy_poll = monotonic()
            await asyncio.sleep(self.poll_seconds)


def mount_service(app: FastAPI, service: Service) -> FastAPI:
    """Add uncredentialed, payload-free probes and start a bounded scoped poller."""
    health = Health()
    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        try:
            async with existing_lifespan(application):
                task = asyncio.create_task(service.run_worker(health))
                try:
                    yield
                finally:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        finally:
            await service.engine.dispose()

    app.router.lifespan_context = lifespan

    def status(ok: bool) -> JSONResponse:
        return JSONResponse(
            {"status": "ok" if ok else "unavailable"}, status_code=200 if ok else 503
        )

    @app.get("/health/live", include_in_schema=False)
    async def live() -> JSONResponse:
        return status(True)

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        return status(await database_ready(service.engine, service.instance_comment))

    @app.get("/health/worker", include_in_schema=False)
    async def worker() -> JSONResponse:
        return status(health.worker_ready(service.worker_freshness_seconds))

    return app
