"""Exercise bounded audit reads against real scoped PostgreSQL and Linux files."""

import asyncio
import json
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

from jimu_runtime.gateway.invocation import TransportResult
from jimu_runtime.gateway.journal import DispatchJournal, ModelDispatchResult
from jimu_runtime.inspection.media import LocalMediaStore
from jimu_runtime.inspection.media.persistence import _DispatchKey
from jimu_runtime.inspection.media.retention import LocalEvidenceLifecycle
from jimu_runtime.inspection.media.retention_models import (
    AuditResponseAccess,
    EvidenceAccess,
    EvidenceAccessGrant,
)
from jimu_runtime.workflow import repository
from tests.contract.test_wp04_dispatch_journal import (
    _intent,
    _model_outcome,
    _ready,
    _start,
)
from tests.contract.test_wp06_retention_postgres import (
    _ACTOR,
    _BODY,
    Scenario,
    database,
    policy,
)


@contextmanager
def observed_opens(path: Path):
    """Observe actual open(2) entry through Python auditing without replacing I/O."""
    calls = []
    watch = True

    def hook(event, args):
        if watch and event == "open" and args[0] == str(path):
            calls.append(event)

    sys.addaudithook(hook)
    try:
        yield calls
    finally:
        watch = False


def test_evidence_checks_metadata_budget_and_ref_before_opening_bytes(tmp_path):
    async def run():
        async with database() as (engine, _):
            state = await Scenario(tmp_path, engine).initialize()
            pin, _ = policy("size-policy", 1)
            locator, asset = await state.archive("request.size-bound", pin)
            path = state.root / asset.storage.object_key
            with observed_opens(path) as opens:
                for cap in (0, len(_BODY) - 1):
                    large = await state.life.read_evidence(
                        state.grant, locator, max_bytes=cap
                    )
                    assert (large.status, large.content) == ("too_large", None)
                denied = await state.life.read_evidence(
                    state.grant,
                    locator.model_copy(update={"output_digest": "a" * 64}),
                    max_bytes=0,
                )
                assert denied == EvidenceAccess("denied", "evidence_scope_unavailable")
                assert opens == []
                exact = await state.life.read_evidence(
                    state.grant,
                    locator,
                    max_bytes=len(_BODY),
                )
                assert exact.content == _BODY
                assert opens == ["open"]

            # Metadata remains small, but fstat must refuse the now-large file.
            path.chmod(0o600)
            with path.open("ab") as file:
                file.truncate(1024 * 1024)
            result = await state.life.read_evidence(
                state.grant, locator, max_bytes=len(_BODY)
            )
            assert (result.status, result.content) == ("corrupt", None)

    asyncio.run(run())


def test_model_roles_authorize_digest_before_any_state_or_file_read(tmp_path):
    async def run():
        async with database() as (engine, _):
            jobs, scope, eid = await _ready(engine)
            claim, attempt, node = await _start(engine, jobs, scope, eid, "size-model")
            media = LocalMediaStore(tmp_path / "store", engine)
            life = LocalEvidenceLifecycle(media, lambda *args: None)
            async with engine.connect() as connection:
                accepted = (await repository.get_execution(connection, scope, eid)).task
            grant = EvidenceAccessGrant(
                scope=scope, actor=_ACTOR, task_id=accepted.task_id
            )
            journal = DispatchJournal(engine)
            normalized = b'{"value":true}'
            wire = json.dumps(
                {"choices": [{"message": {"content": normalized.decode()}}]},
                indent=2,
            ).encode()
            expected = AuditResponseAccess("denied", "response_digest_unverified")

            async def stage(name, retain):
                intent = _intent(node.trace.trace_id, name).model_copy(
                    update={"operation_id": name, "retain_complete_response": retain}
                )
                assert await journal.begin(claim, attempt.attempt_id, intent)
                omission = None if retain else "policy_disabled"
                transport = TransportResult(
                    True,
                    response_dispatch_id=name,
                    response_status=200,
                    response_bytes=wire if retain else None,
                    parsed_content=normalized,
                    response_digest=sha256(wire).hexdigest(),
                    observed_response_bytes=len(wire),
                    response_bytes_omission_reason=omission,
                    validated_response_contract_digest=intent.response_contract_digest,
                    normalized_response_digest=sha256(normalized).hexdigest(),
                    normalized_response_bytes=len(normalized),
                )
                await media.stage_model_response(scope, eid, name, transport)
                receipt = _model_outcome(intent, attempt, node, wire, name).model_copy(
                    update={
                        "response_bytes_omission_reason": omission,
                        "normalized_response_digest": transport.normalized_response_digest,
                        "normalized_response_bytes": transport.normalized_response_bytes,
                    },
                )
                assert await journal.append_result(
                    claim, attempt.attempt_id, name, receipt, late=True
                )

            await stage("retained-body", True)
            await stage("policy-omitted", False)
            for role, body, wrong in (
                ("raw", wire, sha256(normalized).hexdigest()),
                ("normalized", normalized, sha256(wire).hexdigest()),
            ):
                path = media._bytes._directory / _DispatchKey(
                    scope, eid, "retained-body"
                ).key("model_raw" if role == "raw" else "model_normalized")
                with observed_opens(path) as opens:
                    denied = await life.read_model_audit(
                        grant,
                        eid,
                        "retained-body",
                        late=True,
                        role=role,
                        expected_digest=wrong,
                        max_bytes=0,
                    )
                    assert denied == expected
                    missing = await life.read_model_audit(
                        grant,
                        eid,
                        "unknown-dispatch",
                        late=True,
                        role=role,
                        expected_digest=wrong,
                        max_bytes=0,
                    )
                    assert missing == expected
                    cap = await life.read_model_audit(
                        grant,
                        eid,
                        "retained-body",
                        late=True,
                        role=role,
                        expected_digest=sha256(body).hexdigest(),
                        max_bytes=len(body) - 1,
                    )
                    assert (cap.status, cap.content) == ("too_large", None)
                    assert opens == []
                    exact = await life.read_model_audit(
                        grant,
                        eid,
                        "retained-body",
                        late=True,
                        role=role,
                        expected_digest=sha256(body).hexdigest(),
                        max_bytes=len(body),
                    )
                    assert (exact.status, exact.content) == ("late", body)
                    assert opens == ["open"]

            omitted = await life.read_model_audit(
                grant,
                eid,
                "policy-omitted",
                late=True,
                role="raw",
                expected_digest=sha256(wire).hexdigest(),
                max_bytes=1,
            )
            assert omitted.status == "not_retained"
            assert omitted.reason == "policy_disabled"
            assert isinstance(omitted.receipt, ModelDispatchResult)
            denied = await life.read_model_audit(
                grant,
                eid,
                "policy-omitted",
                late=True,
                role="raw",
                expected_digest=sha256(normalized).hexdigest(),
                max_bytes=1,
            )
            assert denied == expected

    asyncio.run(run())
