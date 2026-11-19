"""Distinguish a crossed receipt from a real response whose storage failed."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.runtime.executions import NodeId
from jimu_runtime.domain.runtime.intake import IdempotencyScope
from jimu_runtime.gateway.bridge import CapabilityGateway, ModelCallLimits
from jimu_runtime.gateway.invocation import (
    DispatchRequest,
    ProviderObservation,
    TransportResult,
)
from tests.fixtures.wp04.routing_support import AT, routing_fixture


@pytest.mark.parametrize("crossed_receipt", [True, False])
def test_response_attribution_and_storage_are_independent(crossed_receipt):
    async def run():
        route = routing_fixture(version_evidence=False).primary_route()
        request = DispatchRequest("our.dispatch", route, b"model request")
        physical = b'{"result":"yes"}'
        parsed = b'{"value":true}'
        receipt = TransportResult(
            True,
            observation=ProviderObservation(
                provider_request_id="peer.response",
                model_id=route.ownership.model_id,
                deployment_version=route.selected.deployment_version,
                model_version=route.selected.model_version,
            ),
            response_bytes=physical,
            parsed_content=parsed,
            response_status=200,
            observed_response_bytes=len(physical),
            response_digest=sha256_digest(physical),
            validated_response_contract_digest=route.operation.response_contract_digest,
            normalized_response_digest=sha256_digest(parsed),
            normalized_response_bytes=len(parsed),
            response_dispatch_id="other.dispatch"
            if crossed_receipt
            else request.dispatch_id,
        )
        journal = SimpleNamespace(append_result=AsyncMock())
        writer = SimpleNamespace(
            stage_model_response=AsyncMock(side_effect=OSError("disk"))
        )
        gateway = CapabilityGateway(
            None,
            journal,
            None,
            object(),
            None,
            ModelCallLimits(3, 3, 3, ()),
            response_writer=writer,
        )
        gateway._now = AsyncMock(return_value=AT + timedelta(milliseconds=1))
        claimed = SimpleNamespace(
            claim=SimpleNamespace(
                scope=IdempotencyScope(project_id="project", client_id="client"),
                execution_id=route.operation.node_run.execution_id,
            ),
            attempt=SimpleNamespace(attempt_id=route.attempt.attempt_ref.record_id),
            node=SimpleNamespace(node_id=NodeId.N02),
        )
        snapshot = route.operation.contract_snapshot
        assert snapshot is not None
        outcome = await gateway._record(
            claimed, request, receipt, AT, (snapshot.response_schema_evidence_ref,)
        )
        journal.append_result.assert_awaited_once()
        record = journal.append_result.await_args.args[3]
        assert outcome.output is None
        if crossed_receipt:
            writer.stage_model_response.assert_not_awaited()
            assert outcome.error.code == "model.response_dispatch_mismatch"
            assert record.error.code == "model.response_dispatch_mismatch"
            assert record.invocation is None
            assert record.response_digest is None
            assert record.normalized_response_digest is None
            assert record.response_evidence_error is None
        else:
            writer.stage_model_response.assert_awaited_once()
            assert outcome.error.code == "model.response_evidence_store_unavailable"
            assert record.error is None
            assert record.invocation.outcome.status == "succeeded"
            assert record.response_digest == sha256_digest(physical)
            assert record.normalized_response_digest == sha256_digest(parsed)
            assert record.response_evidence_error == outcome.error.code

    asyncio.run(run())
