"""Physical call receipts, independent of the workflow result fence."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.domain.primitives import RecordId
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.domain.runtime.invocations import FallbackProviderRoute
from jimu_runtime.gateway.journal_records import (
    RESULT_ADAPTER,
    DispatchIntent,
    DispatchLease,
    DispatchResult,
    DispatchTarget,
    ModelDispatchResult,
    ModelDispatchTarget,
    NestedModelDispatchTarget,
    ProviderDispatchObservation,
    ToolDispatchResult,
    ToolDispatchTarget,
    ToolImplementationObservation,
    UnselectedModelDispatchTarget,
    UnselectedNestedModelDispatchTarget,
    tool_receipt_digest,
)
from jimu_runtime.workflow.jobs import JobClaim

__all__ = [
    "DispatchBudgetGuard",
    "DispatchConflict",
    "DispatchIntent",
    "DispatchJournal",
    "DispatchLease",
    "DispatchLiveGuard",
    "DispatchResult",
    "DispatchTarget",
    "DispatchUsage",
    "ModelDispatchResult",
    "ModelDispatchTarget",
    "NestedModelDispatchTarget",
    "ProviderDispatchObservation",
    "StoredDispatch",
    "StoredDispatchResult",
    "ToolDispatchResult",
    "ToolDispatchTarget",
    "ToolImplementationObservation",
    "UnselectedModelDispatchTarget",
    "UnselectedNestedModelDispatchTarget",
    "tool_receipt_digest",
]


class DispatchConflict(ValueError):
    """Incompatible dispatch identity, immutable evidence, or original claim."""


_NESTED_MODELS = (NestedModelDispatchTarget, UnselectedNestedModelDispatchTarget)
_UNSELECTED_MODELS = (
    UnselectedModelDispatchTarget,
    UnselectedNestedModelDispatchTarget,
)
_SELECTED_MODELS = (ModelDispatchTarget, NestedModelDispatchTarget)
_MODELS = (*_SELECTED_MODELS, *_UNSELECTED_MODELS)


_CURSOR = TypeAdapter[str](RecordId)
type DispatchLiveGuard = Callable[[AsyncConnection], Awaitable[bool]]
type DispatchBudgetGuard = Callable[[AsyncConnection], Awaitable[DispatchResult | None]]


@dataclass(frozen=True, slots=True)
class DispatchUsage:
    node_sends: int
    deployment_sends: int | None
    operation_sends: int | None
    tool_step_sends: int | None = None


@dataclass(frozen=True, slots=True)
class StoredDispatchResult:
    result: DispatchResult
    late: bool
    recorded_at: datetime
    fence_qualified: bool | None = None


@dataclass(frozen=True, slots=True)
class StoredDispatch:
    intent: DispatchIntent
    node_run_id: str
    attempt_id: str
    job_id: str
    fence: int
    lease_owner: str
    send_admitted: bool
    recorded_at: datetime
    results: tuple[StoredDispatchResult, ...]


def _scope(scope: IdempotencyScope, execution_id: str) -> dict[str, str]:
    return {
        "project": scope.project_id,
        "client": scope.client_id,
        "execution": execution_id,
    }


def _claim(claim: JobClaim, attempt_id: str) -> dict[str, str | int]:
    return {
        **_scope(claim.scope, claim.execution_id),
        "node": claim.node_run_id,
        "job": claim.job_id,
        "fence": claim.fence,
        "owner": claim.owner,
        "attempt": attempt_id,
    }


def _target_key(target: DispatchTarget) -> str:
    raw = target.model_dump(mode="json")
    names: tuple[str, ...]
    if isinstance(target, _MODELS):
        names = (
            "kind",
            "node_id",
            "configured_provider",
            "purposes",
            "profile_refs",
            "object_ref",
        )
        raw["kind"] = "model"
        if isinstance(target, _NESTED_MODELS):
            raw["kind"] = "nested_model"
            names += ("tool_ref", "tool_plan_ref", "step_id", "bound_input_digest")
    else:
        names = (
            "kind",
            "tool_ref",
            "invocation_origin",
            "profile_ref",
            "object_ref",
            "capability_id",
            "contract_version",
            "implementation_version",
            "implementation_proof_ref",
            "input_schema_ref",
            "output_schema_ref",
            "input_refs",
        )
    canonical = dumps(
        {name: raw[name] for name in names}, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode()).hexdigest()


def _match_result(
    claim: JobClaim, attempt_id: str, intent: DispatchIntent, result: DispatchResult
) -> None:
    target = intent.target
    if isinstance(target, _UNSELECTED_MODELS):
        if not isinstance(result, ModelDispatchResult) or (
            result.boundary != "not_dispatched" or result.invocation is not None
        ):
            raise DispatchConflict("unselected route can only record a rejection")
        return
    if isinstance(result, ModelDispatchResult) and isinstance(target, _SELECTED_MODELS):
        actual = result.actual_provider
        if actual is not None and (
            actual.provider_id,
            actual.provider_kind,
            actual.deployment_id,
            actual.model_id,
        ) != (
            target.selected_provider_id,
            target.selected_provider_kind,
            target.selected_deployment_id,
            target.selected_model_id,
        ):
            raise DispatchConflict(
                "actual provider differs from the selected dispatch target"
            )
        if actual is not None and actual.version_resolution != "unavailable":
            if (
                actual.version_resolution == "registry_verified"
                and actual.version_evidence != target.version_evidence
            ):
                raise DispatchConflict("actual provider lacks the pinned version proof")
            if result.error is None and (
                actual.deployment_version,
                actual.model_version,
            ) != (target.selected_deployment_version, target.selected_model_version):
                raise DispatchConflict(
                    "successful provider versions differ from the frozen dispatch target"
                )
        invocation = result.invocation
        if invocation is not None and (
            invocation.model_invocation_id,
            invocation.execution_id,
            invocation.node_id,
            invocation.node_run_ref.record_id,
            invocation.attempt_ref.record_id,
            invocation.trace_id,
            invocation.request_digest,
            invocation.configured_provider,
            invocation.route_trace,
            invocation.purposes,
            invocation.profile_refs,
            invocation.object_ref,
        ) != (
            intent.dispatch_id,
            claim.execution_id,
            target.node_id,
            claim.node_run_id,
            attempt_id,
            intent.trace_id,
            intent.request_digest,
            target.configured_provider,
            target.route_trace,
            target.purposes,
            target.profile_refs,
            target.object_ref,
        ):
            raise DispatchConflict(
                "invocation differs from the original physical dispatch"
            )
    elif isinstance(result, ToolDispatchResult) and isinstance(
        target, ToolDispatchTarget
    ):
        observed = result.implementation
        if (
            observed is not None
            and (
                observed.implementation_version != target.implementation_version
                or observed.proof_ref != target.implementation_proof_ref
            )
            and result.error is None
        ):
            raise DispatchConflict(
                "successful Tool result differs from the approved implementation"
            )
        execution = result.execution
        if execution is not None and (
            target.input_refs is None
            or target.input_schema_ref is None
            or target.output_schema_ref is None
            or (
                execution.tool_execution_id,
                execution.execution_id,
                execution.node_run_ref.record_id,
                execution.attempt_ref.record_id,
                execution.trace_id,
                execution.profile_ref,
                execution.tool_ref,
                execution.object_ref,
                execution.invocation_origin,
                execution.input_refs,
            )
            != (
                intent.dispatch_id,
                claim.execution_id,
                claim.node_run_id,
                attempt_id,
                intent.trace_id,
                target.profile_ref,
                target.tool_ref,
                target.object_ref,
                target.invocation_origin,
                target.input_refs,
            )
        ):
            raise DispatchConflict(
                "Tool Execution differs from the original physical dispatch"
            )
    else:
        raise DispatchConflict("result and dispatch target types differ")


class DispatchJournal:
    """Grant one pre-send intent per physical ID and retain independent receipts."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def begin(
        self,
        claim: JobClaim,
        attempt_id: str,
        intent: DispatchIntent,
        *,
        live_guard: DispatchLiveGuard | None = None,
        budget_guard: DispatchBudgetGuard | None = None,
    ) -> bool:
        """Grant a send only after committing an intent on the live Attempt.

        A budget guard runs under the exclusive execution lock. Its optional
        non-dispatch result is committed atomically and denies the send.
        """
        if isinstance(intent.target, _UNSELECTED_MODELS):
            raise ValueError("send intent requires a selected model route")
        if intent.resource_lease is None:
            raise ValueError("send intent requires an admitted resource lease")
        return await self._begin(
            claim, attempt_id, intent, None, live_guard, budget_guard
        )

    async def reject(
        self,
        claim: JobClaim,
        attempt_id: str,
        intent: DispatchIntent,
        result: DispatchResult,
        *,
        live_guard: DispatchLiveGuard | None = None,
    ) -> bool:
        """Atomically retain an intent and a proven local pre-send rejection."""
        return await self._begin(
            claim, attempt_id, intent, _checked_rejection(result), live_guard, None
        )

    async def _begin(
        self,
        claim: JobClaim,
        attempt_id: str,
        intent: DispatchIntent,
        rejection: DispatchResult | None,
        live_guard: DispatchLiveGuard | None,
        budget_guard: DispatchBudgetGuard | None,
    ) -> bool:
        intent = DispatchIntent.model_validate(intent)
        recording_rejection = rejection is not None
        target = intent.target
        if (
            isinstance(target, (ToolDispatchTarget, *_NESTED_MODELS))
            and target.parent_attempt_id is not None
            and live_guard is None
        ):
            raise ValueError("nested Tool dispatch requires its live parent guard")
        if rejection is not None:
            _match_result(claim, attempt_id, intent, rejection)
        params = {
            **_claim(claim, attempt_id),
            "operation": intent.operation_id,
            "operation_node": (
                target.parent_node_run_id
                if isinstance(target, (ToolDispatchTarget, *_NESTED_MODELS))
                and target.parent_node_run_id is not None
                else claim.node_run_id
            ),
            "dispatch": intent.dispatch_id,
            "record": intent.stable_json(),
            "input_digest": intent.input_digest,
            "response_contract_digest": intent.response_contract_digest,
            "target_digest": _target_key(target),
        }
        async with self._engine.begin() as connection:
            if not await _lock_execution(connection, params):
                return False
            await _lock_dispatch(connection, claim.execution_id, intent.dispatch_id)
            existing = await _read_intents(connection, params, _SINGLE_DISPATCH)
            if existing:
                _check_owner(existing[0], claim, attempt_id)
                if existing[0].intent != intent:
                    raise DispatchConflict(
                        "dispatch identity has different intent data"
                    )
                if rejection is not None and not any(
                    not item.late and item.result == rejection
                    for item in existing[0].results
                ):
                    raise DispatchConflict(
                        "dispatch identity has different rejection evidence"
                    )
                return False
            if not await _lock_current_job(connection, params):
                return False
            node_kind = (
                target.node_id
                if isinstance(target, _MODELS)
                else target.invocation_origin.node_id
            )
            if not await _match_node(connection, params, node_kind, intent.trace_id):
                raise DispatchConflict(
                    "dispatch target or trace differs from the persisted NodeRun"
                )
            if isinstance(target, _MODELS) and not isinstance(target, _NESTED_MODELS):
                if await _internal_child(connection, params):
                    raise DispatchConflict(
                        "ordinary model dispatch cannot use a Tool child"
                    )
            if (
                isinstance(target, ToolDispatchTarget)
                and target.parent_node_run_id is None
                and await _internal_child(connection, params)
            ):
                raise DispatchConflict(
                    "Tool child dispatch requires its parent binding"
                )
            if live_guard is not None and not await live_guard(connection):
                return False
            if budget_guard is not None:
                denied = await budget_guard(connection)
                if denied is not None:
                    rejection = _checked_rejection(denied)
                    _match_result(claim, attempt_id, intent, rejection)
            if not await _current_dispatch(connection, params, target):
                return False
            if rejection is None and await _has_admitted_send(connection, params):
                raise DispatchConflict(
                    "the operation already has a send in this Attempt"
                )
            await _pin_operation(connection, params)
            if rejection is None and isinstance(target, NestedModelDispatchTarget):
                await _require_nested_fallback_source(connection, params, target)
            await connection.execute(
                text("""
                INSERT INTO jimu_workflow.dispatch_intents
                    (project_id, client_id, execution_id, node_run_id, operation_node_run_id, operation_id,
                     dispatch_id, attempt_id, job_id, fence, lease_owner, send_admitted, record)
                VALUES (:project, :client, :execution, :node, :operation_node, :operation,
                        :dispatch, :attempt, :job, :fence, :owner, :admitted, CAST(:record AS json))
            """),
                {**params, "admitted": rejection is None},
            )
            if rejection is not None:
                await _insert_result(connection, params, rejection, late=False)
            return recording_rejection or rejection is None

    async def append_result(
        self,
        claim: JobClaim,
        attempt_id: str,
        dispatch_id: str,
        result: DispatchResult,
        *,
        late: bool | None = None,
    ) -> bool:
        """Append by exact historical Attempt without acquiring result authority."""
        result = RESULT_ADAPTER.validate_python(result)
        params = {
            **_claim(claim, attempt_id),
            "dispatch": _CURSOR.validate_python(dispatch_id),
        }
        async with self._engine.begin() as connection:
            active = await _lock_execution(connection, params)
            await _lock_dispatch(connection, claim.execution_id, dispatch_id)
            records = await _read_intents(connection, params, _SINGLE_DISPATCH)
            if not records:
                raise DispatchConflict("original dispatch intent is required")
            record = records[0]
            _check_owner(record, claim, attempt_id)
            _match_result(claim, attempt_id, record.intent, result)
            nested = isinstance(record.intent.target, _NESTED_MODELS)
            current = False
            if late is not True and (nested or result.boundary == "acknowledged"):
                current = active and await _current_dispatch(
                    connection, params, record.intent.target
                )
                if result.boundary == "acknowledged":
                    late = not current
            return await _append_checked(
                connection,
                params,
                record,
                result,
                late=late is True,
                fence_qualified=(
                    current
                    and record.send_admitted
                    and result.boundary != "not_dispatched"
                    if nested
                    else None
                ),
            )

    async def is_dispatch_current(
        self, claim: JobClaim, attempt_id: str, intent: DispatchIntent
    ) -> bool:
        """Recheck the exact dispatch scope before exposing any provider output."""
        async with self._engine.connect() as connection:
            return await _current_dispatch(
                connection, _claim(claim, attempt_id), intent.target
            )

    async def is_current(self, claim: JobClaim, attempt_id: str) -> bool:
        """Read current lease eligibility without granting outcome authority."""
        async with self._engine.connect() as connection:
            return await _is_current(connection, _claim(claim, attempt_id))

    async def get_dispatch(
        self, scope: IdempotencyScope, execution_id: str, dispatch_id: str
    ) -> StoredDispatch | None:
        params: dict[str, str | int] = {
            **_scope(scope, execution_id),
            "dispatch": _CURSOR.validate_python(dispatch_id),
        }
        async with self._engine.connect() as connection:
            records = await _read_intents(connection, params, _SINGLE_DISPATCH)
        return records[0] if records else None

    async def usage(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        node_run_id: str,
        *,
        provider_id: str | None = None,
        deployment_id: str | None = None,
        operation_id: str | None = None,
        parent_node_run_id: str | None = None,
        tool_step_id: str | None = None,
        connection: AsyncConnection | None = None,
    ) -> DispatchUsage:
        """Count admitted sends, including interrupted sends with ambiguous delivery.

        A live guard can supply the journal's already-locked transaction.
        """
        if (provider_id is None) != (deployment_id is None):
            raise ValueError("exact provider and deployment must be specified together")
        if (parent_node_run_id is None) != (tool_step_id is None):
            raise ValueError("nested budget requires the parent node and Tool step")
        params = {
            **_scope(scope, execution_id),
            "node": _CURSOR.validate_python(
                node_run_id if parent_node_run_id is None else parent_node_run_id
            ),
            "tool_step": None
            if tool_step_id is None
            else _CURSOR.validate_python(tool_step_id),
            "provider": provider_id,
            "deployment": deployment_id,
            "operation": None
            if operation_id is None
            else _CURSOR.validate_python(operation_id),
        }
        if connection is not None:
            return await _read_usage(connection, params)
        async with self._engine.connect() as transaction:
            return await _read_usage(transaction, params)

    async def get_operation(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        node_run_id: str,
        operation_id: str,
        *,
        limit: int = 128,
        after_dispatch_id: str | None = None,
    ) -> tuple[StoredDispatch, ...]:
        """Read a bounded page of receipts for an exact NodeRun and operation."""
        return await self._operation_page(
            scope,
            execution_id,
            node_run_id,
            operation_id,
            limit,
            after_dispatch_id,
            _OPERATION_DISPATCHES,
        )

    async def get_tool_step_operation(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        parent_node_run_id: str,
        operation_id: str,
        *,
        limit: int = 128,
        after_dispatch_id: str | None = None,
    ) -> tuple[StoredDispatch, ...]:
        """Follow a pinned parent Tool step across independent child NodeRuns."""
        return await self._operation_page(
            scope,
            execution_id,
            parent_node_run_id,
            operation_id,
            limit,
            after_dispatch_id,
            _PARENT_OPERATION_DISPATCHES,
        )

    async def get_nested_model_operation(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        parent_node_run_id: str,
        operation_id: str,
        *,
        limit: int = 128,
        after_dispatch_id: str | None = None,
    ) -> tuple[StoredDispatch, ...]:
        """Read only a parent Tool step's nested model history across child Attempts."""
        return await self._operation_page(
            scope,
            execution_id,
            parent_node_run_id,
            operation_id,
            limit,
            after_dispatch_id,
            _NESTED_MODEL_OPERATION_DISPATCHES,
        )

    async def _operation_page(
        self,
        scope: IdempotencyScope,
        execution_id: str,
        node_run_id: str,
        operation_id: str,
        limit: int,
        after_dispatch_id: str | None,
        selector: str,
    ) -> tuple[StoredDispatch, ...]:
        if type(limit) is not int or not 1 <= limit <= 1024:
            raise ValueError("dispatch page limit must be between 1 and 1024")
        params: dict[str, str | int] = {
            **_scope(scope, execution_id),
            "node": _CURSOR.validate_python(node_run_id),
            "operation": _CURSOR.validate_python(operation_id),
            "after_dispatch": (
                ""
                if after_dispatch_id is None
                else _CURSOR.validate_python(after_dispatch_id)
            ),
            "limit": limit,
        }
        async with self._engine.connect() as connection:
            return await _read_intents(connection, params, selector)


def _checked_rejection(result: DispatchResult) -> DispatchResult:
    result = RESULT_ADAPTER.validate_python(result)
    if result.boundary != "not_dispatched":
        raise ValueError("a pre-send rejection requires known non-dispatch")
    return result


async def _lock_dispatch(
    connection: AsyncConnection, execution_id: str, dispatch_id: str
) -> None:
    digest = sha256(
        f"jimu.dispatch\x00{execution_id}\x00{dispatch_id}".encode()
    ).digest()
    await connection.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(digest[:8], signed=True)},
    )


async def _lock_execution(
    connection: AsyncConnection, params: dict[str, str | int]
) -> bool:
    # Serialize state writers and dispatch admission while allowing
    # foreign-key witnesses to reference this immutable Execution identity.
    eligible = await connection.scalar(
        text("""
        SELECT status = 'running' AND cancel_requested_at IS NULL
        FROM jimu_workflow.executions
        WHERE project_id = :project AND client_id = :client AND execution_id = :execution
        FOR NO KEY UPDATE
    """),
        params,
    )
    return eligible is True


async def _lock_current_job(
    connection: AsyncConnection, params: dict[str, str | int]
) -> bool:
    found = await connection.scalar(
        text("""
        SELECT true FROM jimu_workflow.jobs
        WHERE project_id = :project AND client_id = :client AND execution_id = :execution
            AND node_run_id = :node AND job_id = :job
            AND status = 'leased' AND lease_owner = :owner AND fence = :fence
            AND current_attempt_id = :attempt
        FOR UPDATE
    """),
        params,
    )
    return found is True


async def _match_node(
    connection: AsyncConnection,
    params: dict[str, str | int],
    node_id: str,
    trace_id: str,
) -> bool:
    found = await connection.scalar(
        text("""
        SELECT node_id = :node_id AND record -> 'trace' ->> 'trace_id' = :trace_id
        FROM jimu_workflow.node_runs
        WHERE project_id = :project AND client_id = :client AND execution_id = :execution
            AND node_run_id = :node AND status = 'running'
    """),
        {**params, "node_id": node_id, "trace_id": trace_id},
    )
    return found is True


async def _is_current(
    connection: AsyncConnection, params: dict[str, str | int]
) -> bool:
    found = await connection.scalar(
        text("""
        WITH observed AS (SELECT pg_catalog.clock_timestamp() AS now)
        SELECT true
        FROM jimu_workflow.jobs j
        JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id)
        CROSS JOIN observed
        WHERE j.project_id = :project AND j.client_id = :client AND j.execution_id = :execution
            AND j.node_run_id = :node AND j.job_id = :job AND j.fence = :fence
            AND j.lease_owner = :owner AND j.current_attempt_id = :attempt
            AND e.status = 'running' AND e.cancel_requested_at IS NULL
            AND j.status = 'leased' AND j.lease_until > observed.now
            AND j.attempt_deadline > observed.now
    """),
        params,
    )
    return found is True


async def _current_dispatch(
    connection: AsyncConnection,
    params: dict[str, str | int],
    target: DispatchTarget,
) -> bool:
    if isinstance(target, _NESTED_MODELS):
        return await _match_nested_scope(connection, params, target)
    if (
        isinstance(target, ToolDispatchTarget)
        and target.parent_node_run_id is not None
        and target.parent_attempt_id is not None
    ):
        if target.parent_node_run_id == params["node"]:
            raise DispatchConflict("nested Tool dispatch cannot be its own parent")
        parent_live = await connection.scalar(
            text("""
            WITH observed AS (SELECT pg_catalog.clock_timestamp() AS now)
            SELECT true FROM jimu_workflow.jobs j CROSS JOIN observed
            WHERE j.project_id = :project AND j.client_id = :client
                AND j.execution_id = :execution AND j.node_run_id = :parent_node
                AND j.current_attempt_id = :parent_attempt AND j.status = 'leased'
                AND j.lease_until > observed.now AND j.attempt_deadline > observed.now
        """),
            {
                **params,
                "parent_node": target.parent_node_run_id,
                "parent_attempt": target.parent_attempt_id,
            },
        )
        if parent_live is not True:
            return False
    return await _is_current(connection, params)


async def _internal_child(
    connection: AsyncConnection, params: dict[str, str | int]
) -> bool:
    row = await connection.scalar(
        text("""
        SELECT internal_tool_step FROM jimu_workflow.jobs
        WHERE project_id = :project AND client_id = :client AND execution_id = :execution
            AND node_run_id = :node AND job_id = :job
    """),
        params,
    )
    return row is True


async def _match_nested_scope(
    connection: AsyncConnection,
    params: dict[str, str | int],
    target: NestedModelDispatchTarget | UnselectedNestedModelDispatchTarget,
) -> bool:
    assert target.object_ref is not None
    found = await connection.scalar(
        text("""
        WITH observed AS MATERIALIZED (SELECT pg_catalog.clock_timestamp() AS now)
        SELECT true FROM jimu_workflow.tool_step_attempts s
        JOIN jimu_workflow.executions e USING (project_id, client_id, execution_id)
        JOIN jimu_workflow.jobs child
            ON (child.project_id, child.client_id, child.execution_id,
                child.node_run_id, child.job_id, child.fence, child.current_attempt_id) =
                (s.project_id, s.client_id, s.execution_id,
                    s.child_node_run_id, s.child_job_id, s.child_fence, s.child_attempt_id)
        JOIN jimu_workflow.jobs parent
            ON (parent.project_id, parent.client_id, parent.execution_id,
                parent.node_run_id, parent.job_id, parent.fence, parent.current_attempt_id) =
                (s.project_id, s.client_id, s.execution_id,
                    s.parent_node_run_id, s.parent_job_id, s.parent_fence, s.parent_attempt_id)
        CROSS JOIN observed
        WHERE e.status = 'running' AND e.cancel_requested_at IS NULL
            AND child.status = 'leased' AND child.lease_owner = :owner
            AND child.lease_until > observed.now AND child.attempt_deadline > observed.now
            AND parent.status = 'leased'
            AND parent.lease_until > observed.now AND parent.attempt_deadline > observed.now
            AND s.project_id = :project AND s.client_id = :client
            AND s.execution_id = :execution AND s.child_node_run_id = :node
            AND s.child_job_id = :job AND s.child_fence = :fence AND s.child_attempt_id = :attempt
            AND s.parent_node_run_id = :parent_node AND s.parent_attempt_id = :parent_attempt
            AND s.step_id = :step AND s.bound_input_digest = :bound_input
            AND s.record::jsonb #> '{step,tool_ref}' = CAST(:tool AS jsonb)
            AND s.record::jsonb #> '{inputs,tool_plan_ref}' = CAST(:plan AS jsonb)
            AND s.record #>> '{inputs,object_record,object_id}' = :object_id
            AND s.record::jsonb #> '{inputs,object_record,profile_ref}' = CAST(:profile AS jsonb)
    """),
        {
            **params,
            "parent_node": target.parent_node_run_id,
            "parent_attempt": target.parent_attempt_id,
            "step": target.step_id,
            "bound_input": target.bound_input_digest,
            "tool": target.tool_ref.stable_json(),
            "plan": target.tool_plan_ref.stable_json(),
            "object_id": target.object_ref.record_id,
            "profile": target.profile_refs[0].stable_json(),
        },
    )
    return found is True


async def _require_nested_fallback_source(
    connection: AsyncConnection,
    params: dict[str, str | int],
    target: NestedModelDispatchTarget,
) -> None:
    route = target.route_trace
    if not isinstance(route, FallbackProviderRoute):
        return
    proven = await connection.scalar(
        text("""
        SELECT true
        FROM jimu_workflow.dispatch_intents i
        JOIN jimu_workflow.dispatch_results r
            USING (project_id, client_id, execution_id, dispatch_id)
        JOIN jimu_workflow.tool_step_attempts source
            ON (source.project_id, source.client_id, source.execution_id,
                source.child_node_run_id, source.child_attempt_id) =
                (i.project_id, i.client_id, i.execution_id, i.node_run_id, i.attempt_id)
        JOIN jimu_workflow.tool_step_attempts next
            ON (next.project_id, next.client_id, next.execution_id,
                next.child_node_run_id, next.child_attempt_id) =
                (i.project_id, i.client_id, i.execution_id, :node, :attempt)
        JOIN jimu_workflow.attempt_results parent_result
            ON (parent_result.project_id, parent_result.client_id, parent_result.execution_id,
                parent_result.node_run_id, parent_result.attempt_id) =
                (source.project_id, source.client_id, source.execution_id,
                    source.parent_node_run_id, source.parent_attempt_id)
        JOIN jimu_workflow.tool_step_completions c
            ON (c.project_id, c.client_id, c.execution_id, c.child_node_run_id, c.child_attempt_id) =
                (source.project_id, source.client_id, source.execution_id,
                    source.child_node_run_id, source.child_attempt_id)
        WHERE i.project_id = :project AND i.client_id = :client AND i.execution_id = :execution
            AND i.dispatch_id = :source_dispatch AND i.operation_id = :operation
            AND i.operation_node_run_id = :operation_node AND i.send_admitted
            AND i.record #>> '{target,kind}' = 'nested_model'
            AND i.record #>> '{target,selected_deployment_id}' = :source_deployment
            AND source.parent_node_run_id = next.parent_node_run_id
            AND source.step_id = next.step_id AND source.bound_input_digest = next.bound_input_digest
            AND source.parent_fence < next.parent_fence
            AND c.attempt_status IN ('failed', 'timed_out')
            AND parent_result.status IN ('failed', 'timed_out')
            AND r.fence_qualified IS TRUE AND NOT r.late
            AND (r.boundary = 'acknowledged' OR (r.boundary = 'sent_unknown' AND :trigger = 'timeout'))
            AND r.record #>> '{invocation,outcome,status}' = 'failed'
            AND r.record #>> '{invocation,outcome,error,error_class}' = :trigger
            AND EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(c.record::jsonb #> '{result,invocations}') item
                WHERE item = r.record::jsonb -> 'invocation'
            )
    """),
        {
            **params,
            "source_dispatch": route.trigger_invocation_ref.record_id,
            "source_deployment": route.from_deployment_id,
            "trigger": route.trigger_error,
        },
    )
    if proven is not True:
        raise DispatchConflict(
            "nested fallback requires the exact qualified source receipt"
        )


async def _has_admitted_send(
    connection: AsyncConnection, params: dict[str, str | int]
) -> bool:
    found = await connection.scalar(
        text("""
        SELECT true FROM jimu_workflow.dispatch_intents
        WHERE project_id = :project AND client_id = :client AND execution_id = :execution
            AND node_run_id = :node AND operation_id = :operation AND attempt_id = :attempt
            AND send_admitted
    """),
        params,
    )
    return found is True


async def _read_usage(
    connection: AsyncConnection, params: dict[str, str | None]
) -> DispatchUsage:
    scope_filter = (
        "i.node_run_id = :node"
        if params["tool_step"] is None
        else "i.operation_node_run_id = :node AND i.record -> 'target' ->> 'kind' = 'nested_model'"
    )
    row = (
        await connection.execute(
            text(f"""
        SELECT count(*) AS node_sends, count(*) FILTER (
            WHERE i.record -> 'target' ->> 'selected_provider_id' = :provider
                AND i.record -> 'target' ->> 'selected_deployment_id' = :deployment
        ) AS deployment_sends, count(*) FILTER (
            WHERE i.operation_id = :operation AND (
                CAST(:tool_step AS text) IS NULL OR i.record -> 'target' ->> 'step_id' = :tool_step
            )
        ) AS operation_sends, count(*) FILTER (
            WHERE i.record -> 'target' ->> 'step_id' = :tool_step
        ) AS tool_step_sends
        FROM jimu_workflow.dispatch_intents i
        WHERE i.project_id = :project AND i.client_id = :client AND i.execution_id = :execution
            AND {scope_filter} AND i.send_admitted
            AND NOT EXISTS (
                SELECT 1 FROM jimu_workflow.dispatch_results r
                WHERE r.project_id = i.project_id AND r.client_id = i.client_id
                    AND r.execution_id = i.execution_id AND r.dispatch_id = i.dispatch_id
                    AND NOT r.late AND r.boundary = 'not_dispatched'
            )
    """),
            params,
        )
    ).one()
    return DispatchUsage(
        node_sends=row.node_sends,
        deployment_sends=row.deployment_sends
        if params["provider"] is not None
        else None,
        operation_sends=row.operation_sends
        if params["operation"] is not None
        else None,
        tool_step_sends=row.tool_step_sends
        if params["tool_step"] is not None
        else None,
    )


async def _pin_operation(
    connection: AsyncConnection, params: dict[str, str | int]
) -> None:
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.dispatch_operations
            (project_id, client_id, execution_id, node_run_id, operation_id,
             input_digest, response_contract_digest, target_digest)
        VALUES (:project, :client, :execution, :operation_node, :operation,
                :input_digest, :response_contract_digest, :target_digest)
        ON CONFLICT (project_id, client_id, execution_id, node_run_id, operation_id) DO NOTHING
    """),
        params,
    )
    pinned = await connection.scalar(
        text("""
        SELECT input_digest = :input_digest
            AND response_contract_digest = :response_contract_digest
            AND target_digest = :target_digest
        FROM jimu_workflow.dispatch_operations
        WHERE project_id = :project AND client_id = :client AND execution_id = :execution
            AND node_run_id = :operation_node AND operation_id = :operation
    """),
        params,
    )
    if pinned is not True:
        raise DispatchConflict("logical operation has different immutable pins")


_SINGLE_DISPATCH = "AND i.dispatch_id = :dispatch"
_OPERATION_PAGE = """
AND i.operation_id = :operation AND i.dispatch_id COLLATE "C" > :after_dispatch
ORDER BY i.dispatch_id COLLATE "C" LIMIT :limit
"""
_OPERATION_DISPATCHES = "AND i.node_run_id = :node" + _OPERATION_PAGE
_PARENT_OPERATION_DISPATCHES = (
    """
AND i.operation_node_run_id = :node AND i.operation_node_run_id <> i.node_run_id
AND i.record -> 'target' ->> 'kind' = 'tool'
"""
    + _OPERATION_PAGE
)

_NESTED_MODEL_OPERATION_DISPATCHES = (
    """
AND i.operation_node_run_id = :node AND i.operation_node_run_id <> i.node_run_id
AND i.record -> 'target' ->> 'kind' IN ('nested_model', 'nested_model_unselected')
"""
    + _OPERATION_PAGE
)


async def _read_intents(
    connection: AsyncConnection,
    params: dict[str, str | int],
    selector: str,
) -> tuple[StoredDispatch, ...]:
    rows = await connection.execute(
        text(f"""
        WITH selected AS (
            SELECT i.* FROM jimu_workflow.dispatch_intents i
            WHERE i.project_id = :project AND i.client_id = :client AND i.execution_id = :execution
            {selector}
        )
        SELECT i.dispatch_id, i.node_run_id, i.attempt_id, i.job_id, i.fence,
            i.lease_owner, i.send_admitted, CAST(i.record AS text) AS intent_record, i.recorded_at,
            CAST(r.record AS text) AS result_record, r.late, r.fence_qualified, r.recorded_at AS result_at
        FROM selected i LEFT JOIN jimu_workflow.dispatch_results r
        USING (project_id, client_id, execution_id, dispatch_id)
        ORDER BY i.dispatch_id COLLATE "C", r.recorded_at, r.late
    """),
        params,
    )
    receipts: dict[str, StoredDispatch] = {}
    for row in rows:
        record = receipts.get(row.dispatch_id)
        if record is None:
            record = StoredDispatch(
                intent=DispatchIntent.model_validate_json(row.intent_record),
                node_run_id=row.node_run_id,
                attempt_id=row.attempt_id,
                job_id=row.job_id,
                fence=row.fence,
                lease_owner=row.lease_owner,
                send_admitted=row.send_admitted,
                recorded_at=row.recorded_at.astimezone(UTC),
                results=(),
            )
        if row.result_record is not None:
            result = StoredDispatchResult(
                result=RESULT_ADAPTER.validate_json(row.result_record),
                late=row.late,
                fence_qualified=row.fence_qualified,
                recorded_at=row.result_at.astimezone(UTC),
            )
            record = replace(record, results=(*record.results, result))
        receipts[row.dispatch_id] = record
    return tuple(receipts.values())


def _check_owner(record: StoredDispatch, claim: JobClaim, attempt_id: str) -> None:
    if (
        record.node_run_id,
        record.job_id,
        record.fence,
        record.lease_owner,
        record.attempt_id,
    ) != (claim.node_run_id, claim.job_id, claim.fence, claim.owner, attempt_id):
        raise DispatchConflict("original dispatch Attempt ownership does not match")


async def _insert_result(
    connection: AsyncConnection,
    params: dict[str, str | int],
    result: DispatchResult,
    *,
    late: bool,
    fence_qualified: bool | None = None,
) -> None:
    await connection.execute(
        text("""
        INSERT INTO jimu_workflow.dispatch_results
            (project_id, client_id, execution_id, dispatch_id, late, record, fence_qualified)
        VALUES (:project, :client, :execution, :dispatch, :late, CAST(:record AS json), :fence_qualified)
    """),
        {
            **params,
            "late": late,
            "record": result.stable_json(),
            "fence_qualified": fence_qualified,
        },
    )


async def _append_checked(
    connection: AsyncConnection,
    params: dict[str, str | int],
    record: StoredDispatch,
    result: DispatchResult,
    *,
    late: bool,
    fence_qualified: bool | None,
) -> bool:
    if late and result.boundary != "acknowledged":
        raise ValueError("a late response requires observed acknowledgement")
    for stored in record.results:
        previous = stored.result
        if previous == result:
            return False
        if stored.late == late:
            if previous != result:
                raise DispatchConflict(
                    "dispatch observation already has different evidence"
                )
            return False
        if "not_dispatched" in {previous.boundary, result.boundary}:
            raise DispatchConflict(
                "a known undispatched operation cannot have a response"
            )
        if isinstance(previous, ModelDispatchResult) and isinstance(
            result, ModelDispatchResult
        ):
            if (
                previous.provider_request_id is not None
                and result.provider_request_id is not None
                and previous.provider_request_id != result.provider_request_id
            ):
                raise DispatchConflict(
                    "one physical dispatch cannot claim different provider receipts"
                )
        for earlier, later in (
            (previous.response_digest, result.response_digest),
            (previous.response_status, result.response_status),
        ):
            if earlier is not None and later is not None and earlier != later:
                raise DispatchConflict(
                    "one physical dispatch cannot claim different responses"
                )
    await _insert_result(
        connection, params, result, late=late, fence_qualified=fence_qualified
    )
    return True
