"""Task and Execution routes backed by durable intake and scheduling."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from jimu_runtime.api.auth import Access, Action, Resource
from jimu_runtime.api.boundary import ApiError, read_json
from jimu_runtime.api.contracts import (
    AcceptedExecution,
    CancellationReceipt,
    TaskAcceptance,
    TaskView,
    TracePage,
)
from jimu_runtime.api.ports import ArtifactReplayPort, InferenceReplayPort
from jimu_runtime.api.route_support import Routing, request_schema, required_key
from jimu_runtime.domain.enums import ExecutionStatus
from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.executions import ExecutionRecord
from jimu_runtime.domain.runtime.intake import TaskSubmission
from jimu_runtime.domain.runtime.replay import (
    ArtifactReplayRequest,
    ArtifactReplayRun,
    InferenceReplayRequest,
    InferenceReplayRun,
)
from jimu_runtime.observability.contracts import EventCursor
from jimu_runtime.workflow.engine import ExecutionFinalization
from jimu_runtime.workflow.intake import IntakeService
from jimu_runtime.workflow.jobs import DurableJobs

_ACTIVE = frozenset((ExecutionStatus.INITIALIZED, ExecutionStatus.RUNNING))
_REPLAY_CONFLICTS = frozenset(
    (
        "artifact_replay_idempotency_conflict",
        "artifact_replay_input_digest_mismatch",
        "artifact_replay_input_missing",
        "artifact_source_execution_pin_mismatch",
        "artifact_source_execution_unsettled",
        "artifact_scoring_protocol_unavailable",
        "inference_replay_key_conflict",
        "inference_replay_source_pin_mismatch",
        "inference_replay_target_pins_unsupported",
        "inference_replay_source_context_pending",
        "inference_replay_source_context_unavailable",
        "inference_replay_historical_capabilities_missing",
    )
)


def _replay_error(error: ValueError | RuntimeError) -> None:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _REPLAY_CONFLICTS:
        raise ApiError(409, code) from None
    if code == "artifact_replay_invalid_idempotency_key":
        raise ApiError(422, "invalid_idempotency_key") from None


def mount_inference(
    router: APIRouter,
    routes: Routing,
    intake: IntakeService,
    jobs: DurableJobs,
) -> None:
    authenticated = Depends(routes.boundary.access)

    @router.post(
        "/inference/tasks",
        status_code=202,
        openapi_extra=request_schema(TaskSubmission),
    )
    async def submit_task(
        request: Request, access: Access = authenticated
    ) -> TaskAcceptance:
        submission = await read_json(request, TaskSubmission, routes.boundary.limits)
        if submission.idempotency_scope != access.scope:
            raise ApiError(403, "access_denied")
        await routes.boundary.require(
            access,
            Resource(
                action=Action.TASK_SUBMIT,
                scope=access.scope,
                resource_id=submission.request_id,
                related_id=submission.context.device_id,
            ),
        )
        result = await intake.submit(submission, principal=access.identity.principal)
        return TaskAcceptance(
            task=result.task,
            execution=AcceptedExecution(
                execution_id=result.execution.execution_id, pins=result.execution.pins
            ),
        )

    @router.get("/inference/tasks/{task_id}")
    async def get_task(task_id: RecordId, access: Access = authenticated) -> TaskView:
        return await routes.task(access, task_id, Action.TASK_READ)

    @router.post("/inference/tasks/{task_id}/cancel", status_code=202)
    async def cancel_task(
        task_id: RecordId, access: Access = authenticated
    ) -> CancellationReceipt:
        selected = await routes.task(access, task_id, Action.TASK_CANCEL)
        execution_id = selected.execution.execution_id
        if not await jobs.request_cancel(access.scope, execution_id):
            raise ApiError(409, "execution_not_cancellable")
        return CancellationReceipt(execution_id=execution_id)

    @router.get("/inference/tasks/{task_id}/result")
    async def get_result(
        task_id: RecordId, access: Access = authenticated
    ) -> ExecutionFinalization:
        selected = await routes.task(access, task_id, Action.TASK_RESULT)
        final = await routes.records.finalization(access.scope, selected)
        if final is not None:
            return final
        if selected.execution.status in _ACTIVE:
            raise ApiError(409, "result_pending")
        raise ApiError(404, "result_unavailable")

    @router.get("/executions/{execution_id}")
    async def get_execution(
        execution_id: RecordId, access: Access = authenticated
    ) -> ExecutionRecord:
        return (
            await routes.execution(access, execution_id, Action.EXECUTION_READ)
        ).execution

    @router.get("/executions/{execution_id}/trace", response_model=TracePage)
    async def get_trace(
        execution_id: RecordId,
        after_event_number: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        through_event_number: Annotated[int | None, Query(ge=0, le=2**63 - 1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        access: Access = authenticated,
    ) -> Response:
        selected = await routes.execution(access, execution_id, Action.EXECUTION_TRACE)
        if (
            through_event_number is not None
            and after_event_number > through_event_number
        ):
            raise ApiError(422, "invalid_trace_cursor")
        page = await routes.records.trace(
            access.scope,
            selected.execution,
            cursor=EventCursor(after=after_event_number, through=through_event_number),
            limit=limit,
            byte_limit=routes.boundary.limits.trace_bytes,
        )
        return Response(page, media_type="application/json")


def mount_replays(
    router: APIRouter,
    routes: Routing,
    *,
    artifact: ArtifactReplayPort | None,
    inference: InferenceReplayPort | None,
) -> None:
    authenticated = Depends(routes.boundary.access)

    if artifact is not None:
        scorer = artifact

        @router.post(
            "/executions/{execution_id}/artifact-replay",
            openapi_extra=request_schema(ArtifactReplayRequest),
        )
        async def artifact_replay(
            execution_id: RecordId,
            request: Request,
            access: Access = authenticated,
        ) -> ArtifactReplayRun:
            selected = await routes.execution(
                access, execution_id, Action.ARTIFACT_REPLAY
            )
            key = required_key(request)
            payload = await read_json(
                request, ArtifactReplayRequest, routes.boundary.limits
            )
            _check_replay_source(payload, selected.execution)
            try:
                result = await scorer.run(access.scope, key, payload)
            except (ValueError, RuntimeError) as error:
                _replay_error(error)
                raise
            if result.request != payload:
                raise ApiError(500, "replay_receipt_mismatch")
            return result

    if inference is not None:
        runner = inference

        @router.post(
            "/executions/{execution_id}/inference-replay",
            status_code=202,
            openapi_extra=request_schema(InferenceReplayRequest),
        )
        async def inference_replay(
            execution_id: RecordId,
            request: Request,
            access: Access = authenticated,
        ) -> InferenceReplayRun:
            selected = await routes.execution(
                access, execution_id, Action.INFERENCE_REPLAY
            )
            key = required_key(request)
            payload = await read_json(
                request, InferenceReplayRequest, routes.boundary.limits
            )
            _check_replay_source(payload, selected.execution)
            try:
                result = await runner.submit(
                    access.scope,
                    payload,
                    idempotency_key=key,
                    principal=access.identity.principal,
                )
            except (ValueError, RuntimeError) as error:
                _replay_error(error)
                raise
            if result.request != payload:
                raise ApiError(500, "replay_receipt_mismatch")
            return result


def _check_replay_source(
    request: ArtifactReplayRequest | InferenceReplayRequest, execution: ExecutionRecord
) -> None:
    try:
        request.source.validate_execution(execution)
    except ValueError:
        raise ApiError(409, "replay_source_mismatch") from None
