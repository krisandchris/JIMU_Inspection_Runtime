"""Authorized read-only export of scoped historical samples for offline scoring."""

from asyncio import to_thread
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal, Self

from pydantic import model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jimu_runtime.bundle.archive import BundleLimits
from jimu_runtime.bundle.releases import ReleaseManager
from jimu_runtime.bundle.store import LocalBundleStore
from jimu_runtime.domain.base import ContractModel
from jimu_runtime.domain.enums import RuntimeRecordKind
from jimu_runtime.domain.primitives import Digest, RecordId, Version
from jimu_runtime.domain.runtime.authority import ActorRef
from jimu_runtime.domain.runtime.evidence import EvidenceAsset
from jimu_runtime.domain.runtime.executions import PinnedRuntimeInput
from jimu_runtime.domain.runtime.facts import EvidenceRef
from jimu_runtime.domain.runtime.intake import AcceptedTask, IdempotencyScope
from jimu_runtime.domain.runtime.invocations import ModelInvocation
from jimu_runtime.domain.runtime.replay import ReplayExecutionSource
from jimu_runtime.domain.runtime.tool_executions import ToolExecution
from jimu_runtime.evaluation.contracts import (
    DatasetManifest,
    DatasetSample,
    Entries,
    EvaluationCatalogue,
    ExecutionCohort,
    ExecutionEvidence,
    ModelAttempts,
    PredictionSnapshot,
    ProfilePrediction,
    SamplePrediction,
    Samples,
    SampleSource,
    TrialSource,
    canonical_digest,
    require_unique,
)
from jimu_runtime.evaluation.history_projection import (
    EvaluationHistoryError,
    HistoricalCatalogue,
    catalogue_from_bundle,
    project_profiles,
)
from jimu_runtime.gateway.journal_records import (
    RESULT_ADAPTER,
    DispatchIntent,
    ModelDispatchResult,
    ToolDispatchResult,
    ToolDispatchTarget,
)
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.inspection.media.retention import LocalEvidenceLifecycle
from jimu_runtime.inspection.media.retention_models import (
    EvidenceAccessGrant,
    EvidenceLocator,
)
from jimu_runtime.inspection.media.types import MediaError
from jimu_runtime.replay.artifact_journal import read_journal_witnesses
from jimu_runtime.replay.artifact_source import HistoricalSource, load_historical_source
from jimu_runtime.workflow import repository as repo
from jimu_runtime.workflow.pins import ResolvedExecutionPins, resolve_execution_pins
from jimu_runtime.workflow.worker_capabilities import read_historical_capabilities
from jimu_runtime.workflow.worker_records import (
    DecisionMessage,
    GroundingMessage,
    NodeMessage,
    ObservationMessage,
    ToolMessage,
)
from jimu_runtime.workflow.worker_state import load_records

_MAX_DISPATCHES = 4096
_MAX_EVIDENCE = 1024
_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
_DEFAULT_TRIAL_BYTES = 2 * _MAX_EVIDENCE_BYTES
_K = RuntimeRecordKind


class HistorySelection(ContractModel):
    sample_id: RecordId
    source: ReplayExecutionSource


class HistoryExportRequest(ContractModel):
    """Trusted host selection. The host authorizes scope and actor before calling."""

    scope: IdempotencyScope
    actor: ActorRef
    prediction_id: RecordId
    source_id: RecordId
    version: Version
    selections: Samples[HistorySelection]


class RetainedResponse(ContractModel):
    late: bool
    role: Literal["raw", "normalized"]
    digest: Digest


class DispatchAudit(ContractModel):
    dispatch_id: RecordId
    node_run_id: RecordId
    attempt_id: RecordId
    intent_digest: Digest
    on_time_digest: Digest | None
    late_digest: Digest | None
    retained_responses: Entries[RetainedResponse]
    send_admitted: bool
    fence_qualified: bool | None


class SampleHistoryAudit(ContractModel):
    schema_version: Literal["1.0.0"]
    scope: IdempotencyScope
    sample_id: RecordId
    execution_id: RecordId
    finalization_digest: Digest
    catalogue_digest: Digest
    artifacts: ModelAttempts[PinnedRuntimeInput]
    dispatches: ModelAttempts[DispatchAudit]
    evidence: Entries[ExecutionEvidence]


class HistoricalTrialExport(ContractModel):
    """Portable historical export with independently pinned per-sample audit witnesses."""

    schema_version: Literal["1.0.0"]
    catalogue: EvaluationCatalogue
    predictions: PredictionSnapshot
    source: TrialSource
    history: Samples[SampleHistoryAudit]

    @model_validator(mode="after")
    def verify_witnesses(self) -> Self:
        sources = self.source.reconcile(self.predictions)
        require_unique(
            (row.sample_id for row in self.history), "historical witness sample"
        )
        if (
            {row.sample_id for row in self.history} != sources.keys()
            or self.catalogue.bundle_digest != self.predictions.cohort.bundle_digest
        ):
            raise ValueError("historical export must cover the exact trial and Bundle")
        executions = {
            row.sample_id: row.execution.execution_id
            for row in self.predictions.entries
        }
        catalogue_digest = canonical_digest(self.catalogue)
        for witness in self.history:
            row = sources[witness.sample_id]
            if (
                witness.scope,
                witness.execution_id,
                witness.evidence,
                witness.catalogue_digest,
                canonical_digest(witness),
            ) != (
                self.source.scope,
                executions[witness.sample_id],
                row.evidence,
                catalogue_digest,
                row.history_digest,
            ):
                raise ValueError(
                    "historical witness differs from the independently pinned sample"
                )
        return self


@dataclass(frozen=True, slots=True)
class _ExportedSample:
    prediction: SamplePrediction
    source: SampleSource
    audit: SampleHistoryAudit


@dataclass(slots=True)
class _TrialState:
    remaining: int
    sample_remaining: int = _MAX_EVIDENCE_BYTES
    catalogue: HistoricalCatalogue | None = None
    cohort: ExecutionCohort | None = None

    def begin_sample(self) -> None:
        self.sample_remaining = _MAX_EVIDENCE_BYTES

    def allowance(self) -> int:
        if self.remaining <= 0:
            raise EvaluationHistoryError("evaluation_trial_evidence_bytes_limit")
        if self.sample_remaining <= 0:
            raise EvaluationHistoryError("evaluation_sample_evidence_bytes_limit")
        return min(self.remaining, self.sample_remaining)

    def charge(self, size: int) -> None:
        if size > self.remaining:
            raise EvaluationHistoryError("evaluation_trial_evidence_bytes_limit")
        if size > self.sample_remaining:
            raise EvaluationHistoryError("evaluation_sample_evidence_bytes_limit")
        self.remaining -= size
        self.sample_remaining -= size

    def reject_at_limit(self) -> None:
        # Media readers return too_large before loading any bytes.
        if self.remaining <= self.sample_remaining:
            raise EvaluationHistoryError("evaluation_trial_evidence_bytes_limit")
        raise EvaluationHistoryError("evaluation_sample_evidence_bytes_limit")

    def bind(self, resolved: ResolvedExecutionPins) -> HistoricalCatalogue:
        digest = resolved.pins.bundle_digest
        if (
            self.catalogue is not None
            and self.catalogue.catalogue.bundle_digest != digest
        ):
            raise EvaluationHistoryError("evaluation_mixed_bundle_catalogues")
        cohort = ExecutionCohort.from_execution(resolved.pins)
        if self.cohort is not None and self.cohort != cohort:
            raise EvaluationHistoryError("evaluation_mixed_execution_cohorts")
        if self.catalogue is None:
            self.catalogue = catalogue_from_bundle(resolved.catalogue, digest)
            self.cohort = cohort
        return self.catalogue


def _scope(scope: IdempotencyScope, execution_id: str) -> dict[str, str]:
    return {
        "project": scope.project_id,
        "client": scope.client_id,
        "execution": execution_id,
    }


async def _dispatches(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    history: HistoricalSource,
) -> tuple[HistoricalSource, tuple[DispatchAudit, ...]]:
    rows = await connection.execute(
        text("""
            WITH chosen AS (
                SELECT project_id, client_id, execution_id, dispatch_id, node_run_id,
                       attempt_id, send_admitted, record
                FROM jimu_workflow.dispatch_intents
                WHERE (project_id, client_id, execution_id) = (:project, :client, :execution)
                ORDER BY dispatch_id COLLATE "C" LIMIT :limit
            )
            SELECT i.dispatch_id, i.node_run_id, i.attempt_id, i.send_admitted,
                i.record::text AS intent_body, r.record::text AS on_time_body,
                r.fence_qualified, l.record::text AS late_body
            FROM chosen i
                LEFT JOIN jimu_workflow.dispatch_results r
                    ON (i.project_id, i.client_id, i.execution_id, i.dispatch_id)
                    = (r.project_id, r.client_id, r.execution_id, r.dispatch_id) AND NOT r.late
                LEFT JOIN jimu_workflow.dispatch_results l
                    ON (i.project_id, i.client_id, i.execution_id, i.dispatch_id)
                    = (l.project_id, l.client_id, l.execution_id, l.dispatch_id) AND l.late
            ORDER BY i.dispatch_id COLLATE "C"
        """),
        {**_scope(scope, history.execution.execution_id), "limit": _MAX_DISPATCHES + 1},
    )
    records = rows.all()
    if len(records) > _MAX_DISPATCHES:
        raise EvaluationHistoryError("evaluation_dispatch_limit")
    models = {row.model_invocation_id: row for row in history.models}
    tools = {row.tool_execution_id: row for row in history.tools}
    if len(records) == 0:
        if models or tools:
            raise EvaluationHistoryError("evaluation_dispatch_inventory_missing")
        return history, ()
    audits = []
    witnessed_ids: set[str] = set()
    for row in records:
        intent = DispatchIntent.model_validate_json(row.intent_body)
        on_time = (
            None
            if row.on_time_body is None
            else RESULT_ADAPTER.validate_json(row.on_time_body)
        )
        late = (
            None
            if row.late_body is None
            else RESULT_ADAPTER.validate_json(row.late_body)
        )
        tool = isinstance(intent.target, ToolDispatchTarget)
        if (
            intent.dispatch_id != row.dispatch_id
            or intent.stable_json() != row.intent_body
            or (on_time is None and not row.send_admitted)
            or (
                on_time is not None
                and (
                    on_time.stable_json() != row.on_time_body
                    or isinstance(on_time, ToolDispatchResult) != tool
                    or (on_time.boundary != "not_dispatched" and not row.send_admitted)
                )
            )
            or (
                late is not None
                and (
                    late.stable_json() != row.late_body
                    or isinstance(late, ToolDispatchResult) != tool
                    or not row.send_admitted
                    or late.boundary != "acknowledged"
                    or (on_time is not None and on_time.boundary == "not_dispatched")
                )
            )
        ):
            raise EvaluationHistoryError("evaluation_dispatch_record_mismatch")
        normalized: ModelInvocation | ToolExecution | None = None
        if isinstance(on_time, ToolDispatchResult):
            normalized = on_time.execution
        elif isinstance(on_time, ModelDispatchResult):
            normalized = on_time.invocation
        if (
            row.send_admitted
            and normalized is None
            and (on_time is None or on_time.boundary != "not_dispatched")
        ):
            # The journal has a physical admission whose outcome cannot populate v1 metrics.
            raise EvaluationHistoryError("evaluation_dispatch_unsettled")
        if normalized is not None:
            if (
                normalized.execution_id,
                normalized.node_run_ref.record_id,
                normalized.attempt_ref.record_id,
            ) != (history.execution.execution_id, row.node_run_id, row.attempt_id):
                raise EvaluationHistoryError("evaluation_dispatch_attempt_mismatch")
            if isinstance(normalized, ToolExecution):
                if normalized.tool_execution_id != row.dispatch_id:
                    raise EvaluationHistoryError("evaluation_dispatch_id_mismatch")
                tools.setdefault(row.dispatch_id, normalized)
            else:
                if normalized.model_invocation_id != row.dispatch_id:
                    raise EvaluationHistoryError("evaluation_dispatch_id_mismatch")
                models.setdefault(row.dispatch_id, normalized)
            witnessed_ids.add(row.dispatch_id)
        retained = tuple(
            RetainedResponse(late=is_late, role=role, digest=digest)
            for is_late, result in ((False, on_time), (True, late))
            if isinstance(result, ModelDispatchResult)
            for role, digest in _retained(result)
        )
        audits.append(
            DispatchAudit(
                dispatch_id=row.dispatch_id,
                node_run_id=row.node_run_id,
                attempt_id=row.attempt_id,
                intent_digest=canonical_digest(intent),
                on_time_digest=None if on_time is None else canonical_digest(on_time),
                late_digest=None if late is None else canonical_digest(late),
                send_admitted=row.send_admitted,
                fence_qualified=row.fence_qualified,
                retained_responses=retained,
            )
        )
    if witnessed_ids != set(models) | set(tools):
        raise EvaluationHistoryError("evaluation_dispatch_inventory_unreconciled")
    complete = replace(
        history,
        models=tuple(models[key] for key in sorted(models)),
        tools=tuple(tools[key] for key in sorted(tools)),
    )
    # Compare normalized selected results against the same scoped raw journal, including
    # Tool authority transformations, configured targets and request identity.
    await read_journal_witnesses(connection, scope, complete)
    return complete, tuple(audits)


def _retained(
    result: ModelDispatchResult,
) -> tuple[tuple[Literal["raw", "normalized"], str], ...]:
    raw = (
        result.response_digest
        if result.response_bytes_omission_reason is None
        and result.response_evidence_error is None
        else None
    )
    normalized = result.normalized_response_digest
    result_set: list[tuple[Literal["raw", "normalized"], str]] = []
    if raw is not None:
        result_set.append(("raw", raw))
    if normalized is not None:
        result_set.append(("normalized", normalized))
    return tuple(result_set)


async def _verify_responses(
    lifecycle: LocalEvidenceLifecycle,
    grant: EvidenceAccessGrant,
    execution_id: str,
    dispatches: tuple[DispatchAudit, ...],
    trial: _TrialState,
) -> None:
    for dispatch in dispatches:
        for row in dispatch.retained_responses:
            read = await lifecycle.read_model_audit(
                grant,
                execution_id,
                dispatch.dispatch_id,
                late=row.late,
                role=row.role,
                expected_digest=row.digest,
                max_bytes=trial.allowance(),
            )
            if read.status == "too_large":
                trial.reject_at_limit()
            if read.status != "available" or read.content is None:
                raise EvaluationHistoryError(f"evaluation_model_evidence_{read.status}")
            trial.charge(len(read.content))


async def _locators(
    connection: AsyncConnection,
    scope: IdempotencyScope,
    execution_id: str,
) -> tuple[EvidenceLocator, ...]:
    rows = await connection.execute(
        text("""
            SELECT node_run_id, attempt_id, output_key, output_digest
            FROM jimu_workflow.produced_outputs
            WHERE (project_id, client_id, execution_id) = (:project, :client, :execution)
                AND output_kind = 'evidence'
            ORDER BY output_key COLLATE "C", node_run_id COLLATE "C", attempt_id COLLATE "C"
            LIMIT :limit
        """),
        {**_scope(scope, execution_id), "limit": _MAX_EVIDENCE + 1},
    )
    records = rows.all()
    if len(records) > _MAX_EVIDENCE:
        raise EvaluationHistoryError("evaluation_evidence_limit")
    result = []
    for row in records:
        kind, _, evidence_id = row.output_key.partition(".")
        if kind != "evidence" or not evidence_id:
            raise EvaluationHistoryError("evaluation_evidence_key_mismatch")
        result.append(
            EvidenceLocator(
                execution_id=execution_id,
                node_run_id=row.node_run_id,
                attempt_id=row.attempt_id,
                evidence_id=evidence_id,
                output_digest=row.output_digest,
            )
        )
    return tuple(result)


async def _verify_evidence(
    lifecycle: LocalEvidenceLifecycle,
    grant: EvidenceAccessGrant,
    locators: tuple[EvidenceLocator, ...],
    known: dict[str, EvidenceAsset],
    trial: _TrialState,
) -> tuple[ExecutionEvidence, ...]:
    assets: dict[str, EvidenceAsset] = {}
    for locator in locators:
        read = await lifecycle.read_evidence(
            grant, locator, max_bytes=trial.allowance()
        )
        if read.status == "too_large":
            trial.reject_at_limit()
        if read.status != "available" or read.asset is None or read.content is None:
            raise EvaluationHistoryError(f"evaluation_evidence_{read.status}")
        asset = read.asset
        expected = known.get(asset.evidence_id)
        if (expected is not None and expected != asset) or assets.setdefault(
            asset.evidence_id, asset
        ) != asset:
            raise EvaluationHistoryError("evaluation_evidence_identity_mismatch")
        trial.charge(len(read.content))
    if known.keys() - assets.keys():
        raise EvaluationHistoryError("evaluation_evidence_missing")
    return tuple(
        ExecutionEvidence(
            ref=EvidenceRef(record_kind=_K.EVIDENCE, record_id=key),
            digest=canonical_digest(assets[key]),
        )
        for key in sorted(assets)
    )


def _required_assets(messages: Iterable[NodeMessage]) -> dict[str, EvidenceAsset]:
    assets: dict[str, EvidenceAsset] = {}
    for message in messages:
        if isinstance(
            message,
            (GroundingMessage, ObservationMessage, ToolMessage, DecisionMessage),
        ):
            for asset in message.evidence:
                if assets.setdefault(asset.evidence_id, asset) != asset:
                    raise EvaluationHistoryError(
                        "evaluation_evidence_identity_mismatch"
                    )
    return assets


def _sample_prediction(
    sample: DatasetSample,
    task: AcceptedTask,
    history: HistoricalSource,
    profiles: tuple[ProfilePrediction, ...],
) -> SamplePrediction:
    coverage = history.finalization.coverage
    return SamplePrediction(
        sample_id=sample.sample_id,
        task=task,
        execution=history.execution,
        model_invocations=history.models,
        tool_executions=history.tools,
        discovery=coverage.discovery_outcome,
        profiles=profiles,
        decision=coverage.summary_result,
        decision_records=history.finalization.decisions,
        auto_cleared=None,
    )


class HistoricalEvaluationExporter:
    """Read the selected holdout from a trusted authorized inspection host."""

    def __init__(
        self,
        engine: AsyncEngine,
        releases: ReleaseManager,
        store: LocalBundleStore,
        media: LocalMediaStore,
        evidence: LocalEvidenceLifecycle,
        limits: BundleLimits,
        *,
        max_trial_evidence_bytes: int = _DEFAULT_TRIAL_BYTES,
    ) -> None:
        if max_trial_evidence_bytes < 1:
            raise ValueError("trial evidence budget must be positive")
        self._max_trial_evidence_bytes = max_trial_evidence_bytes
        self._engine = engine
        self._releases = releases
        self._store = store
        self._media = media
        self._evidence = evidence
        self._limits = limits

    async def export(
        self,
        dataset: DatasetManifest,
        request: HistoryExportRequest,
    ) -> HistoricalTrialExport:
        dataset = DatasetManifest.model_validate(dataset)
        request = HistoryExportRequest.model_validate(request)
        scope = request.scope
        if dataset.scope != scope:
            raise EvaluationHistoryError("evaluation_scope_mismatch")
        require_unique(
            (row.sample_id for row in request.selections), "evaluation sample selection"
        )
        require_unique(
            (row.source.execution_ref for row in request.selections),
            "evaluation execution selection",
        )
        samples = {
            row.sample_id: row for row in dataset.samples if row.split == "holdout"
        }
        if samples.keys() != {row.sample_id for row in request.selections}:
            raise EvaluationHistoryError("evaluation_selection_must_exhaust_holdout")
        # All database witnesses in the trial come from a single read snapshot.
        exported = []
        trial = _TrialState(self._max_trial_evidence_bytes)
        async with self._engine.connect() as raw:
            connection = await raw.execution_options(isolation_level="REPEATABLE READ")
            async with connection.begin():
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                for row in sorted(request.selections, key=lambda item: item.sample_id):
                    trial.begin_sample()
                    identity = {
                        "sample_id": row.sample_id,
                        "execution_id": row.source.execution_ref.record_id,
                    }
                    try:
                        exported.append(
                            await self._sample(
                                connection,
                                scope,
                                request.actor,
                                samples[row.sample_id],
                                row.source,
                                trial,
                            )
                        )
                    except EvaluationHistoryError as exc:
                        raise EvaluationHistoryError(exc.code, **identity) from exc
                    except ValueError as exc:
                        code = getattr(exc, "code", "record_validation")
                        raise EvaluationHistoryError(
                            f"evaluation_history_{code}", **identity
                        ) from exc
        assert trial.catalogue is not None and trial.cohort is not None
        predictions = PredictionSnapshot(
            schema_version="1.0.0",
            record_id=request.prediction_id,
            version=request.version,
            scope=scope,
            dataset=dataset.pin(),
            split="holdout",
            cohort=trial.cohort,
            producer=request.actor,
            entries=tuple(row.prediction for row in exported),
        )
        source = TrialSource(
            schema_version="1.0.0",
            record_id=request.source_id,
            version=request.version,
            dataset=dataset.pin(),
            predictions=predictions.pin(),
            scope=scope,
            rows=tuple(row.source for row in exported),
        )
        return HistoricalTrialExport(
            schema_version="1.0.0",
            catalogue=trial.catalogue.catalogue,
            predictions=predictions,
            source=source,
            history=tuple(row.audit for row in exported),
        )

    async def _sample(
        self,
        connection: AsyncConnection,
        scope: IdempotencyScope,
        actor: ActorRef,
        sample: DatasetSample,
        source: ReplayExecutionSource,
        trial: _TrialState,
    ) -> _ExportedSample:
        eid = source.execution_ref.record_id
        locked = await repo.get_execution(connection, scope, eid)
        if locked is None:
            raise EvaluationHistoryError("evaluation_execution_unavailable")
        history = await load_historical_source(connection, scope, source)
        historical = await read_historical_capabilities(
            connection, scope, history.execution
        )
        if historical is None:
            raise EvaluationHistoryError("evaluation_historical_capabilities_missing")
        resolved = await to_thread(
            resolve_execution_pins,
            self._releases,
            self._store,
            locked.task,
            scope=scope,
            host=historical.snapshot,
            validation_at=historical.validation_at,
            limits=self._limits,
        )
        if (resolved.pins, resolved.policy_ref) != (
            history.execution.pins,
            locked.execution_policy_ref,
        ):
            raise EvaluationHistoryError("evaluation_runtime_input_pin_mismatch")
        catalogue = trial.bind(resolved)
        history, dispatches = await _dispatches(connection, scope, history)
        try:
            metadata = await self._media.scoped_original_metadata(
                scope, locked.task.media_ref
            )
            trial.charge(metadata.byte_size)
            media = await self._media.verified_task_original(scope, locked.task)
        except MediaError as exc:
            raise EvaluationHistoryError(
                "evaluation_original_media_unavailable"
            ) from exc
        if media.asset != metadata or len(media.content) != metadata.byte_size:
            raise EvaluationHistoryError("evaluation_original_media_metadata_mismatch")
        if (media.asset.media_ref.sha256, media.asset.dimensions) != (
            sample.image_sha256,
            sample.image_dimensions,
        ):
            raise EvaluationHistoryError("evaluation_dataset_image_mismatch")
        data = await load_records(connection, scope, locked)
        inputs = data.inputs()
        if (
            inputs.context is not None
            and inputs.context.inputs.media_asset != media.asset
        ):
            raise EvaluationHistoryError("evaluation_context_media_mismatch")
        grounding = inputs.grounding
        objects = () if grounding is None else grounding.objects
        coverage = history.finalization.coverage
        decisions = history.finalization.decisions
        profiles = project_profiles(
            catalogue,
            media.asset,
            coverage,
            objects,
            history.facts,
            decisions,
            history.tools,
        )
        grant = EvidenceAccessGrant(
            scope=scope, actor=actor, task_id=locked.task.task_id
        )
        await _verify_responses(self._evidence, grant, eid, dispatches, trial)
        evidence = await _verify_evidence(
            self._evidence,
            grant,
            await _locators(connection, scope, eid),
            _required_assets(data.messages.values()),
            trial,
        )
        refs = {row.ref for row in evidence}
        if (
            not {ref for row in (*objects, *history.facts) for ref in row.evidence_refs}
            | {ref for row in decisions for ref in row.explanation.evidence_refs}
            <= refs
        ):
            raise EvaluationHistoryError("evaluation_required_evidence_missing")
        prediction = _sample_prediction(sample, locked.task, history, profiles)
        audit = SampleHistoryAudit(
            schema_version="1.0.0",
            scope=scope,
            sample_id=sample.sample_id,
            execution_id=eid,
            finalization_digest=canonical_digest(history.finalization),
            catalogue_digest=catalogue.digest,
            artifacts=history.artifacts,
            dispatches=dispatches,
            evidence=evidence,
        )
        sample_source = SampleSource(
            sample_id=sample.sample_id,
            task_digest=canonical_digest(locked.task),
            execution_digest=canonical_digest(history.execution),
            execution_input_digest=resolved.pins.input_digest,
            model_digests=tuple(canonical_digest(row) for row in history.models),
            tool_digests=tuple(canonical_digest(row) for row in history.tools),
            decision_digests=tuple(canonical_digest(row) for row in decisions),
            evidence=evidence,
            history_digest=canonical_digest(audit),
        )
        return _ExportedSample(prediction, sample_source, audit)
