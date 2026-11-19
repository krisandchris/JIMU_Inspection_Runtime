"""A waiting provider call leaves a bounded worker lane for other ready jobs."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace

from jimu_runtime.deployment.service import Health, Service
from jimu_runtime.domain.runtime.intake import IdempotencyScope


def test_service_runs_two_claims_without_waiting_for_first_and_cancels_both():
    async def exercise():
        both_active = asyncio.Event()
        active = 0
        cancelled = 0
        scopes = []

        async def run_next(scope, *, owner, lease_ms, after_execution_id):
            nonlocal active, cancelled
            scopes.append(scope)
            assert owner == "configured-worker"
            assert lease_ms > 0 and after_execution_id is None
            active += 1
            if active == 2:
                both_active.set()
            try:
                await asyncio.Future()
            finally:
                active -= 1
                cancelled += 1

        scope = IdempotencyScope(project_id="test-project", client_id="test-client")
        service = Service(
            engine=None,
            worker=SimpleNamespace(run_next=run_next),
            scopes=(scope,),
            owner="configured-worker",
            instance_comment="isolated",
        )
        polling = asyncio.create_task(service.run_worker(Health()))
        try:
            await asyncio.wait_for(both_active.wait(), timeout=2)
            assert active == 2 and scopes == [scope, scope]
        finally:
            polling.cancel()
            with suppress(asyncio.CancelledError):
                await polling
        assert active == 0 and cancelled == 2

    asyncio.run(exercise())
