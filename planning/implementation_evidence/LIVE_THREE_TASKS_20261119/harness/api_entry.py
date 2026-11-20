"""Isolated host for the public task HTTP routes, using the real storage and intake."""
from contextlib import asynccontextmanager, contextmanager
import json
import secrets
from unittest.mock import patch
import httpx
from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from jimu_runtime.api.app import create_app
from jimu_runtime.api.auth import Action, Identity
from tests.fixtures.wp05 import perception


@contextmanager
def use_task_api(directory):
    real_intake, real_fixture = perception.IntakeService, perception.perception_fixture
    client = None
    receipt = None
    api_log = []
    def persist():
        (directory / 'api.json').write_text(json.dumps(api_log, indent=2, ensure_ascii=False)+'\n')

    def factory(engine, *args, **kwargs):
        service = real_intake(engine, *args, **kwargs)

        class HttpSubmit:
            async def submit(self, submission, *, principal):
                nonlocal client, receipt
                scope = submission.idempotency_scope
                bearer = secrets.token_urlsafe(32)

                class Auth:
                    async def authenticate(self, credential):
                        return Identity(principal, ACTOR) if secrets.compare_digest(credential, bearer) else None
                    async def allows(self, identity, resource):
                        return resource.scope == scope and identity.actor == ACTOR and resource.action in {
                            Action.ACCESS_SCOPE, Action.TASK_SUBMIT, Action.TASK_READ, Action.TASK_RESULT,
                            Action.EXECUTION_READ, Action.EXECUTION_TRACE,
                        }

                class IntakePort:
                    async def submit(self, task, *, principal):
                        nonlocal receipt
                        receipt = await service.submit(task, principal=principal)
                        return receipt

                client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine, IntakePort(), authority=Auth())),
                    base_url='http://test.local/api/v1/',headers={'Authorization':'Bearer '+bearer,'X-Jimu-Project':scope.project_id,'X-Jimu-Client':scope.client_id})
                result = await client.post('inference/tasks',content=submission.stable_json(), headers={'Content-Type':'application/json'})
                api_log.append({'method':'POST','path':'/api/v1/inference/tasks','status':result.status_code,'body':result.json()})
                persist()
                result.raise_for_status()
                assert receipt is not None and result.json()['execution']['execution_id']==receipt.execution.execution_id
                return receipt
        return HttpSubmit()
    @asynccontextmanager
    async def with_api(*args, **kwargs):
        try:
            async with real_fixture(*args, **kwargs) as fixture:
                try:
                    yield fixture
                finally:
                    assert client is not None and receipt is not None
                    for suffix in ('executions/'+receipt.execution.execution_id,'inference/tasks/'+receipt.task.task_id+'/result','executions/'+receipt.execution.execution_id+'/trace'):
                        response = await client.get(suffix)
                        api_log.append({'method':'GET','path':'/api/v1/'+suffix,'status':response.status_code,'body':response.json()})
                        persist()
                        if response.status_code != 409:
                            response.raise_for_status()
        finally:
            if client:
                await client.aclose()
    with patch.object(perception,'IntakeService',factory),patch.object(perception,'perception_fixture',with_api):
        yield
