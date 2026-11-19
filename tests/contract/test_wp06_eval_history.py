"""Re-score a signed, persisted inspection without calling its models or tools."""

import asyncio
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fixtures.wp06.evaluation_sample import reviewed, synthetic_inputs
from fixtures.wp06.evaluation_sample import score as score_fixture
from pydantic import ValidationError
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable.lifecycle import ACTOR
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import LIMITS
from jimu_runtime.bundle.artifacts import artifact_identity
from jimu_runtime.bundle.semantic_inputs import require_definition
from jimu_runtime.domain.definitions.tools import ToolDefinition
from jimu_runtime.domain.enums import ArtifactKind, DecisionResult, RuntimeRecordKind
from jimu_runtime.domain.producers import ProducerCatalogue, ToolStepProducer
from jimu_runtime.domain.runtime.facts import ToolFactSource
from jimu_runtime.domain.runtime.media import BBoxXYXY
from jimu_runtime.domain.runtime.replay import ExecutionRef, ReplayExecutionSource
from jimu_runtime.domain.values import EnumValue
from jimu_runtime.evaluation import (
    DatasetManifest,
    ScoringProtocol,
    TruthSnapshot,
    score,
)
from jimu_runtime.evaluation.contracts import (
    DatasetSample,
    GeometryLabel,
    LabeledObject,
    QuestionLabel,
    RuleLabel,
    SampleLabels,
    canonical_digest,
)
from jimu_runtime.evaluation.history import (
    EvaluationHistoryError,
    HistoricalEvaluationExporter,
    HistoryExportRequest,
    HistorySelection,
    _locators,
    _TrialState,
    _verify_evidence,
)
from jimu_runtime.evaluation.history_projection import (
    catalogue_from_bundle,
    project_profiles,
)
from jimu_runtime.evaluation.scoring import Ratio
from jimu_runtime.gateway.bridge_tools import ToolExecutionAdapter
from jimu_runtime.inspection.geometry import synthetic_cpu_implementations
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter
from jimu_runtime.inspection.media.retention import LocalEvidenceLifecycle
from jimu_runtime.inspection.media.retention_models import EvidenceAccessGrant
from jimu_runtime.replay.artifact_source import load_historical_source
from jimu_runtime.tools.executor import ToolExecutor
from jimu_runtime.tools.registry import ToolWorkerRegistry
from jimu_runtime.tools.step_attempts import StepAttemptStore
from jimu_runtime.workflow import repository
from jimu_runtime.workflow.pins import resolve_execution_pins
from jimu_runtime.workflow.worker import WorkerStep
from jimu_runtime.workflow.worker_capabilities import read_historical_capabilities
from jimu_runtime.workflow.worker_inspection import (
    VerifiedMediaWorkerHost,
    inspection_worker,
)
from jimu_runtime.workflow.worker_state import load_records
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05.perception import perception_fixture

loopback_only = http_support.loopback_only
_BOX = (40.0, 23.0, 52.0, 33.0)


async def _table_counts(engine, scope, eid):
    counts = {}
    tables = (
        "executions",
        "jobs",
        "dispatch_intents",
        "dispatch_results",
        "produced_outputs",
    )
    async with engine.connect() as connection:
        for table in tables:
            counts[table] = await connection.scalar(
                text(f"""
                    SELECT count(*) FROM jimu_workflow.{table}
                    WHERE (project_id, client_id, execution_id) = (:p, :c, :e)
                """),
                {"p": scope.project_id, "c": scope.client_id, "e": eid},
            )
    return counts


@pytest.mark.parametrize("scenario", ["violation", "partial", "no_hits"])
def test_scoring_export_reopens_the_verified_history(
    tmp_path: Path, loopback_only, scenario
):
    with_target = scenario != "no_hits"

    async def run():
        calls: Counter[str] = Counter()

        async def peer(reader, writer):
            request = await http_support.read_request(reader)
            body = json.loads(request.body)
            schema = body["response_format"]["json_schema"]["schema"]
            name = schema["$id"].split(":")[-2]
            calls[name] += 1
            answers = {
                "vehicle_type": "electric_bicycle",
                "parking_state": "parked",
                "parking_position": "over_boundary",
            }
            if name == "object_observation-result" and (
                calls[name] == 1 or scenario == "partial"
            ):
                answers["object_id"] = "outside.claimed.object"
            reply = {
                "profile_discovery-result": {
                    "profile_hits": ["nonmotor_vehicle"] if with_target else []
                },
                "object_grounding-result": {"boxes": [{"xyxy": _BOX}]},
                "object_observation-result": answers,
                "candidate_prediction-result": {"categories": []},
            }[name]
            raw = http_support.chat_body(model=body["model"], content=json.dumps(reply))
            writer.write(
                http_support.response(
                    raw,
                    headers=(
                        (b"X-Request-ID", f"historical-{sum(calls.values())}".encode()),
                        (b"X-Jimu-Deployment-Version", b"1.0.0"),
                        (b"X-Jimu-Model-Version", b"1.0.0"),
                    ),
                )
            )
            await writer.drain()

        async with http_support.http_peer(loopback_only, peer) as url:
            async with perception_fixture(tmp_path, url, with_media=True) as fixture:
                accepted = fixture.accepted
                scope, eid = (
                    accepted.task.idempotency_scope,
                    accepted.execution.execution_id,
                )
                media = fixture.media_store
                assert media is not None
                catalogue = fixture.scene.execution.catalogue
                tools = ToolWorkerRegistry(
                    synthetic_cpu_implementations(
                        fixture.resources,
                        "cpu",
                        "inspection-tools",
                        tuple(
                            require_definition(catalogue, row.ref, ToolDefinition)
                            for row in catalogue.of_kind(ArtifactKind.TOOL)
                        ),
                        fixture.tool_proofs,
                        synthetic_image_digests=frozenset(
                            {accepted.task.media_ref.sha256}
                        ),
                    )
                )
                adapter = GeometryNodeAdapter(
                    media,
                    ToolExecutionAdapter(
                        ToolExecutor(tools),
                        StepAttemptStore(fixture.application, actor=ACTOR),
                        fixture.journal,
                        fixture.host.capabilities,
                    ),
                )
                tool_calls = 0

                async def execute_tools(*args):
                    nonlocal tool_calls
                    tool_calls += 1
                    return await adapter.execute(*args)

                async def capture_registry(request_scope, task):
                    source = await fixture.host.worker_source(request_scope, task)
                    return source.device, source.camera

                worker = inspection_worker(
                    fixture.application,
                    fixture.releases,
                    fixture.store,
                    host=VerifiedMediaWorkerHost(
                        media, fixture.host.capabilities, capture_registry
                    ),
                    bundle_limits=LIMITS,
                    media=media,
                    gateway=fixture.gateway,
                    execute_tools=execute_tools,
                    model_resource_scope_id="model",
                )
                for index in range(24):
                    step = await worker.run_once(
                        scope, eid, owner=f"eval-{index}", lease_ms=10000
                    )
                    if step is WorkerStep.FINALIZED:
                        break
                    if step is WorkerStep.RETRY_SCHEDULED:
                        await asyncio.sleep(0.025)
                else:
                    raise AssertionError("signed inspection did not finalize")
                async with fixture.application.connect() as connection:
                    locked = await repository.get_execution(connection, scope, eid)
                assert locked is not None
                execution = locked.execution
                original = await media.verified_task_original(scope, locked.task)
                dataset = DatasetManifest(
                    schema_version="1.0.0",
                    record_id="signed-scene-dataset",
                    version="1.0.0",
                    scope=scope,
                    samples=(
                        DatasetSample(
                            sample_id="scene-a",
                            image_sha256=original.asset.media_ref.sha256,
                            image_dimensions=original.asset.dimensions,
                            dedup_group="synthetic-scene",
                            split="holdout",
                        ),
                    ),
                )
                request = HistoryExportRequest(
                    scope=scope,
                    actor=ACTOR,
                    prediction_id="signed-scene-predictions",
                    source_id="signed-scene-source",
                    version="1.0.0",
                    selections=(
                        HistorySelection(
                            sample_id="scene-a",
                            source=ReplayExecutionSource(
                                execution_ref=ExecutionRef(
                                    record_kind=RuntimeRecordKind.EXECUTION,
                                    record_id=eid,
                                ),
                                task_id=locked.task.task_id,
                                pins=execution.pins,
                            ),
                        ),
                    ),
                )
                evidence = LocalEvidenceLifecycle(media, lambda *_: None)
                exporter = HistoricalEvaluationExporter(
                    fixture.application,
                    fixture.releases,
                    fixture.store,
                    media,
                    evidence,
                    LIMITS,
                )
                calls_before = calls.copy()
                tool_calls_before = tool_calls
                db_before = await _table_counts(fixture.application, scope, eid)
                frozen = await exporter.export(dataset, request)
                # Persist the approved pin separately at the trusted export boundary.
                pin_file = tmp_path / "approved-evaluation-source-pin.json"
                pin_file.write_text(
                    frozen.source.pin().model_dump_json(), encoding="utf-8"
                )
                expected_source = type(frozen.source.pin()).model_validate_json(
                    pin_file.read_bytes()
                )
                assert frozen == await exporter.export(dataset, request)
                serialized = tmp_path / "historical-evaluation-export.json"
                serialized.write_text(frozen.model_dump_json(), encoding="utf-8")
                restored = type(frozen).model_validate_json(serialized.read_bytes())
                assert (
                    restored == frozen and restored.source.pin() == frozen.source.pin()
                )
                edited = json.loads(frozen.model_dump_json())
                edited["history"][0]["finalization_digest"] = "1" * 64
                with pytest.raises(ValidationError, match="historical witness"):
                    type(frozen).model_validate_json(json.dumps(edited))
                edited = json.loads(frozen.model_dump_json())
                edited["catalogue"]["profiles"] = []
                with pytest.raises(ValidationError, match="historical witness"):
                    type(frozen).model_validate_json(json.dumps(edited))
                assert calls == calls_before
                assert tool_calls == tool_calls_before
                assert db_before == await _table_counts(fixture.application, scope, eid)
                (prediction,) = restored.predictions.entries
                assert prediction.auto_cleared is None
                (profile,) = restored.catalogue.profiles
                protocol = ScoringProtocol(
                    schema_version="1.0.0",
                    protocol_id="signed-parking-test",
                    version="1.0.0",
                    catalogue=restored.catalogue,
                    iou_threshold_basis_points=5000,
                    matching="greedy_iou_then_truth_id_then_prediction_id",
                )
                objects = ()
                if with_target:
                    parked = next(
                        row
                        for row in profile.questions
                        if row.question_ref.artifact_id == "parking_state"
                    )
                    relation = next(
                        row
                        for row in profile.geometry
                        if row.ref.artifact_id == "wheel_boundary_relation"
                    )
                    rule = next(
                        row.path
                        for row in profile.rules
                        if row.path.rule_ref.artifact_id
                        == "nonmotor_vehicle_illegal_parking"
                    )
                    objects = (
                        LabeledObject(
                            label_id="gold-obj",
                            profile_ref=profile.profile_ref,
                            box=BBoxXYXY(xyxy=_BOX),
                            questions=(
                                QuestionLabel(
                                    question_ref=parked.question_ref,
                                    value=EnumValue(kind="enum", value="parked"),
                                ),
                            ),
                            geometry=(
                                GeometryLabel(
                                    fact_schema_ref=relation.ref,
                                    value=EnumValue(
                                        kind="enum", value="outside_parking_area"
                                    ),
                                ),
                            ),
                            rules=(RuleLabel(path=rule, matched=True),),
                        ),
                    )
                # Independent fixture labels and boxes. The admitted catalogue only addresses labels.
                labels = SampleLabels(
                    present_profiles=(profile.profile_ref,) if with_target else (),
                    objects=objects,
                    task_violation=with_target,
                    complete_grounding_profiles=(profile.profile_ref,),
                )
                truth = TruthSnapshot(
                    schema_version="1.0.0",
                    record_id="signed-gold-truth",
                    version="1.0.0",
                    dataset=dataset.pin(),
                    scope=scope,
                    split="holdout",
                    prior=None,
                    catalogue_digest=canonical_digest(protocol.catalogue),
                    entries=(
                        reviewed(
                            dataset, "scene-a", labels, catalogue=protocol.catalogue
                        ),
                    ),
                )

                def run_score(export=restored):
                    return score(
                        dataset,
                        truth,
                        export.predictions,
                        protocol,
                        source=export.source,
                        expected_source=expected_source,
                        expected_truth=truth.pin(),
                        expected_protocol=protocol.pin(),
                    )

                report = run_score()
                assert report.pin() == run_score().pin()
                other_export = await exporter.export(
                    dataset,
                    request.model_copy(update={"source_id": "another-valid-source"}),
                )
                other_export = type(other_export).model_validate_json(
                    other_export.model_dump_json()
                )
                assert other_export.source.pin() != expected_source
                with pytest.raises(ValueError, match="independently approved source"):
                    run_score(other_export)

                assert frozen.source.rows[0].history_digest == canonical_digest(
                    frozen.history[0]
                )
                assert (
                    report.summary.automatic_release_observation_coverage
                    == Ratio.of(0, 1)
                )
                assert report.summary.automatic_release_coverage == Ratio.of(0, 0)
                assert report.summary.automatic_release_risk == Ratio.of(0, 0)
                assert report.summary.automatic_release_unobserved == 1
                if with_target:
                    assert report.summary.erroneous_release_rate is None
                    assert (
                        report.summary.labeled_violations_without_release_observation
                        == 1
                    )
                    assert prediction.decision is (
                        DecisionResult.UNCERTAIN
                        if scenario == "partial"
                        else DecisionResult.VIOLATION
                    )
                    assert calls_before["object_observation-result"] == 2
                    assert (
                        sum(
                            row.node_id == "N04" for row in prediction.model_invocations
                        )
                        == 2
                    )
                    assert len(prediction.tool_executions) == 3
                    assert report.summary.task_violation_recall == Ratio.of(
                        int(scenario == "violation"), 1
                    )
                    metric = next(
                        row
                        for row in report.summary.questions
                        if row.question_ref == parked.question_ref
                    )
                    if scenario == "partial":
                        assert metric.comparison.coverage == Ratio.of(0, 1)
                        assert metric.comparison.accuracy == Ratio.of(0, 0)
                    else:
                        assert metric.comparison.accuracy == Ratio.of(1, 1)
                        geometry_metric = next(
                            row
                            for row in report.summary.geometry
                            if row.fact_schema_ref == relation.ref
                        )
                        rule_metric = next(
                            row for row in report.summary.rules if row.path == rule
                        )
                        assert (
                            geometry_metric.comparison.accuracy
                            == geometry_metric.comparison.coverage
                            == Ratio.of(1, 1)
                        )
                        assert (
                            rule_metric.comparison.accuracy
                            == rule_metric.comparison.coverage
                            == Ratio.of(1, 1)
                        )
                else:
                    assert prediction.decision is DecisionResult.UNCERTAIN
                    assert report.summary.no_hits_labeled_target_miss == Ratio.of(0, 1)
                    assert not prediction.tool_executions

                other = scope.model_copy(update={"client_id": "different-client"})
                with pytest.raises(
                    EvaluationHistoryError, match="evaluation_scope_mismatch"
                ) as mismatch:
                    await exporter.export(
                        dataset, request.model_copy(update={"scope": other})
                    )
                assert mismatch.value.sample_id is None
                with pytest.raises(
                    EvaluationHistoryError, match="execution_unavailable"
                ) as unavailable:
                    await exporter.export(
                        dataset.model_copy(update={"scope": other}),
                        request.model_copy(update={"scope": other}),
                    )
                assert (
                    unavailable.value.sample_id,
                    unavailable.value.execution_id,
                ) == ("scene-a", eid)
                assert unavailable.value.__cause__ is not None
                selection = request.selections[0]
                missing_source = selection.source.model_copy(
                    update={
                        "execution_ref": selection.source.execution_ref.model_copy(
                            update={"record_id": "execution-not-found"}
                        ),
                    }
                )
                with pytest.raises(
                    EvaluationHistoryError, match="execution_unavailable"
                ) as missing:
                    await exporter.export(
                        dataset,
                        request.model_copy(
                            update={
                                "selections": (
                                    selection.model_copy(
                                        update={"source": missing_source}
                                    ),
                                ),
                            }
                        ),
                    )
                assert (missing.value.sample_id, missing.value.execution_id) == (
                    "scene-a",
                    "execution-not-found",
                )
                if scenario == "violation":
                    async with fixture.application.connect() as connection:
                        stored = await load_historical_source(
                            connection, scope, request.selections[0].source
                        )
                        records = await load_records(connection, scope, locked)
                        caps = await read_historical_capabilities(
                            connection, scope, execution
                        )
                        evidence_locators = await _locators(connection, scope, eid)
                    assert caps is not None
                    resolved = resolve_execution_pins(
                        fixture.releases,
                        fixture.store,
                        locked.task,
                        scope=scope,
                        host=caps.snapshot,
                        validation_at=caps.validation_at,
                        limits=LIMITS,
                    )
                    assert records.inputs().grounding is not None
                    _verify_projection_failures(
                        stored,
                        records.inputs().grounding.objects,
                        original.asset,
                        resolved,
                    )
                    blocked_original = HistoricalEvaluationExporter(
                        fixture.application,
                        fixture.releases,
                        fixture.store,
                        media,
                        evidence,
                        LIMITS,
                        max_trial_evidence_bytes=original.asset.byte_size - 1,
                    )
                    with patch.object(
                        media,
                        "verified_task_original",
                        wraps=media.verified_task_original,
                    ) as read_original:
                        with pytest.raises(
                            EvaluationHistoryError, match="trial_evidence_bytes_limit"
                        ) as too_large:
                            await blocked_original.export(dataset, request)
                        read_original.assert_not_called()
                    assert (
                        too_large.value.sample_id,
                        too_large.value.execution_id,
                    ) == ("scene-a", eid)
                    mismatched = request.selections[0]
                    pinned = mismatched.source
                    pinned = pinned.model_copy(
                        update={
                            "pins": pinned.pins.model_copy(
                                update={"input_digest": "a" * 64}
                            )
                        }
                    )
                    with pytest.raises(
                        EvaluationHistoryError, match="execution_pin_mismatch"
                    ):
                        await exporter.export(
                            dataset,
                            request.model_copy(
                                update={
                                    "selections": (
                                        mismatched.model_copy(
                                            update={"source": pinned}
                                        ),
                                    )
                                }
                            ),
                        )
                    original_path = (
                        tmp_path / "media" / original.asset.storage.object_key
                    )
                    backup = original_path.with_suffix(".held")
                    original_path.replace(backup)
                    try:
                        with pytest.raises(
                            EvaluationHistoryError, match="original_media_unavailable"
                        ):
                            await exporter.export(dataset, request)
                    finally:
                        backup.replace(original_path)

                    geometry = next(
                        row
                        for row in prediction.tool_executions
                        if row.invocation_origin.step_id == "wheel_boundary_geometry"
                    )
                    sidecar = await media.read_tool_sidecar(
                        scope, eid, geometry.tool_execution_id, "geometry"
                    )
                    locator = next(
                        row
                        for row in evidence_locators
                        if row.evidence_id == sidecar.asset.evidence_id
                    )
                    grant = EvidenceAccessGrant(
                        scope=scope, task_id=locked.task.task_id, actor=ACTOR
                    )
                    # Two sample windows each permit this real stored payload on its own.
                    # The second window must stop before reading once the trial budget is exhausted.
                    state = _TrialState(2 * sidecar.asset.byte_size - 1)
                    known = {sidecar.asset.evidence_id: sidecar.asset}
                    with patch.object(
                        media._bytes, "read", wraps=media._bytes.read
                    ) as bytes_read:
                        state.begin_sample()
                        assert await _verify_evidence(
                            evidence, grant, (locator,), known, state
                        )
                        first_reads = bytes_read.call_count
                        assert first_reads > 0
                        state.begin_sample()
                        with pytest.raises(
                            EvaluationHistoryError, match="trial_evidence_bytes_limit"
                        ):
                            await _verify_evidence(
                                evidence, grant, (locator,), known, state
                            )
                        assert bytes_read.call_count == first_reads
                    geometry_path = (
                        tmp_path / "media" / sidecar.asset.storage.object_key
                    )
                    held = geometry_path.with_suffix(".held")
                    geometry_path.replace(held)
                    try:
                        with pytest.raises(
                            EvaluationHistoryError, match="evaluation_evidence_missing"
                        ):
                            await exporter.export(dataset, request)
                        same_size = held.read_bytes()
                        geometry_path.write_bytes(
                            bytes([same_size[0] ^ 1]) + same_size[1:]
                        )
                        geometry_path.chmod(0o600)
                        with pytest.raises(
                            EvaluationHistoryError, match="evaluation_evidence_corrupt"
                        ):
                            await exporter.export(dataset, request)
                    finally:
                        held.replace(geometry_path)
                assert calls == calls_before
                assert tool_calls == tool_calls_before
                assert db_before == await _table_counts(fixture.application, scope, eid)

    asyncio.run(run())


def _verify_projection_failures(stored, objects, asset, resolved):
    state = _TrialState(1024)
    with patch(
        "jimu_runtime.evaluation.history.catalogue_from_bundle",
        wraps=catalogue_from_bundle,
    ) as compile_catalogue:
        catalogue = state.bind(resolved)
        different_input = replace(
            resolved, pins=resolved.pins.model_copy(update={"input_digest": "9" * 64})
        )
        assert state.bind(different_input) is catalogue
        different_bundle = replace(
            resolved, pins=resolved.pins.model_copy(update={"bundle_digest": "9" * 64})
        )
        with pytest.raises(EvaluationHistoryError, match="mixed_bundle"):
            state.bind(different_bundle)
        assert compile_catalogue.call_count == 1

    coverage = stored.finalization.coverage
    decisions = stored.finalization.decisions
    fact = next(
        row
        for row in stored.facts
        if isinstance(row.source, ToolFactSource)
        and row.fact_key == "wheel_boundary_relation"
    )
    other_tool = next(
        tool
        for tool in stored.tools
        if tool.tool_execution_id != fact.source.tool_execution_ref.record_id
    )
    forged_source = fact.source.model_copy(
        update={
            "tool_execution_ref": fact.source.tool_execution_ref.model_copy(
                update={"record_id": other_tool.tool_execution_id}
            ),
        }
    )
    forged = fact.model_copy(update={"source": forged_source})
    false_output = fact.model_copy(
        update={
            "source": fact.source.model_copy(update={"output_name": "different-output"})
        }
    )
    with pytest.raises(EvaluationHistoryError, match="tool_fact_mismatch"):
        project_profiles(
            catalogue,
            asset,
            coverage,
            objects,
            (false_output,),
            decisions,
            stored.tools,
        )

    with pytest.raises(EvaluationHistoryError, match="tool_fact_mismatch"):
        project_profiles(
            catalogue, asset, coverage, objects, (forged,), decisions, stored.tools
        )

    with pytest.raises(EvaluationHistoryError, match="coverage_object_mismatch"):
        project_profiles(
            catalogue,
            asset,
            coverage,
            (*objects, objects[0]),
            stored.facts,
            decisions,
            stored.tools,
        )
    unlisted = objects[0].model_copy(update={"object_id": "unexpected-object"})
    with pytest.raises(EvaluationHistoryError, match="coverage_object_mismatch"):
        project_profiles(
            catalogue,
            asset,
            coverage,
            (*objects, unlisted),
            stored.facts,
            decisions,
            stored.tools,
        )
    orphan = decisions[0].model_copy(
        update={
            "object_ref": decisions[0].object_ref.model_copy(
                update={"record_id": unlisted.object_id}
            )
        }
    )
    with pytest.raises(EvaluationHistoryError, match="decision_object_mismatch"):
        project_profiles(
            catalogue, asset, coverage, objects, stored.facts, (orphan,), stored.tools
        )

    # Exercise catalogue construction independently of arrival. Two exact plan
    # outputs mapped to one schema cannot be addressed by the V1 metric.
    bundle = resolved.catalogue
    (entry,) = bundle.of_kind(ArtifactKind.FACT_PRODUCER_CATALOG)
    definitions = require_definition(bundle, entry.ref, ProducerCatalogue)
    other = next(
        row
        for row in definitions.declarations
        if isinstance(row.producer, ToolStepProducer)
        and row.fact_schema_ref != fact.fact_schema_ref
    )
    definitions = definitions.model_copy(
        update={
            "declarations": tuple(
                row.model_copy(update={"fact_schema_ref": fact.fact_schema_ref})
                if row == other
                else row
                for row in definitions.declarations
            ),
        }
    )
    changed = replace(entry, value=definitions)
    duplicate = replace(
        bundle,
        artifacts=tuple(
            changed if item == entry else item for item in bundle.artifacts
        ),
        _by_identity={**bundle._by_identity, artifact_identity(entry.ref): changed},
    )
    with pytest.raises(EvaluationHistoryError, match="geometry_binding_ambiguous"):
        catalogue_from_bundle(duplicate, resolved.pins.bundle_digest)


def test_unobserved_clearance_is_distinct_from_negative_clearance_and_legacy_is_stable():
    dataset, truth, predictions, protocol = synthetic_inputs()
    frozen = score_fixture(
        dataset, truth, predictions, protocol, expected_protocol=protocol.pin()
    )
    assert (
        frozen.pin().digest
        == "ac816e6d05c97cef855d57bab3e22a9e196a3da9c4e3a52de6682c5a22ff2767"
    )
    assert (
        type(frozen).model_validate_json(frozen.model_dump_json()).pin() == frozen.pin()
    )
    changed = predictions.entries[0].model_copy(update={"auto_cleared": None})
    mixed = predictions.model_copy(
        update={"entries": (changed, *predictions.entries[1:])}
    )
    report = score_fixture(
        dataset, truth, mixed, protocol, expected_protocol=protocol.pin()
    )
    rates = report.summary
    assert rates.erroneous_release_rate is None
    assert rates.confirmed_erroneous_releases == 1
    assert rates.labeled_violations_without_release_observation == 1
    assert rates.automatic_release_risk == Ratio.of(1, 2)
    assert rates.automatic_release_coverage == Ratio.of(3, 6)
    assert rates.automatic_release_observation_coverage == Ratio.of(6, 7)
    assert rates.automatic_release_unobserved == 1
