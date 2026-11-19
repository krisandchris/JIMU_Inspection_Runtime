"""Shared route validation and durable ownership checks."""

from dataclasses import dataclass

from fastapi import Request
from pydantic import TypeAdapter, ValidationError

from jimu_runtime.api.auth import Access, Action, Resource
from jimu_runtime.api.boundary import ApiError, HttpBoundary, single_header
from jimu_runtime.api.contracts import TaskView
from jimu_runtime.api.records import RuntimeRecords
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.definition_support import ShortText
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.workflow.repository import LockedExecution

_KEY = TypeAdapter[str](ShortText)
_ID = TypeAdapter[str](RecordId)


@dataclass(frozen=True, slots=True)
class Routing:
    boundary: HttpBoundary
    records: RuntimeRecords

    async def task(self, access: Access, task_id: str, action: Action) -> TaskView:
        selected = await self.records.primary(access.scope, task_id)
        if selected is None:
            raise ApiError(404, "resource_not_found")
        await self.boundary.require(
            access,
            Resource(
                action=action,
                scope=access.scope,
                task_id=task_id,
                execution_id=selected.execution.execution_id,
            ),
            conceal=True,
        )
        return selected

    async def execution(
        self,
        access: Access,
        execution_id: str,
        action: Action,
        *,
        resource_id: str | None = None,
    ) -> LockedExecution:
        selected = await self.records.execution(access.scope, execution_id)
        if selected is None:
            raise ApiError(404, "resource_not_found")
        await self.boundary.require(
            access,
            Resource(
                action=action,
                scope=access.scope,
                task_id=selected.task.task_id,
                execution_id=execution_id,
                resource_id=resource_id,
            ),
            conceal=True,
        )
        return selected


def required_key(request: Request, *, record_id: bool = False) -> str:
    value = single_header(request, "idempotency-key")
    if value is None:
        raise ApiError(422, "idempotency_key_required")
    try:
        parsed = (_ID if record_id else _KEY).validate_python(value)
    except ValidationError:
        raise ApiError(422, "invalid_idempotency_key") from None
    if "\x00" in parsed:
        raise ApiError(422, "invalid_idempotency_key")
    return parsed


def request_schema(model: type[ContractModel]) -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": f"#/components/schemas/JimuInput_{model.__name__}"
                    }
                }
            },
        }
    }
