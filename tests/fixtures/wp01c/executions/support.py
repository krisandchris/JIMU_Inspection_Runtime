"""Deterministic execution contracts shared by the C4 tests."""

import json
from pathlib import Path

from jimu_runtime.domain.runtime.executions import (
    Attempt,
    ExecutionEvent,
    ExecutionRecord,
    NodeRun,
)

CREATED = "2026-11-18T03:00:00Z"
STARTED = "2026-11-18T03:00:01Z"
ENDED = "2026-11-18T03:00:02Z"
LATE = "2026-11-18T03:00:03Z"


def fixture():
    return json.loads(
        Path(__file__).with_name("contracts.json").read_text(encoding="utf-8")
    )


def ref(kind, identifier):
    return {"record_kind": kind, "record_id": identifier}


def execution(**changes):
    return ExecutionRecord.model_validate_json(
        json.dumps(fixture()["execution"] | changes)
    )


def node(**changes):
    return NodeRun.model_validate_json(json.dumps(fixture()["node"] | changes))


def attempt(**changes):
    return Attempt.model_validate_json(json.dumps(fixture()["attempt"] | changes))


def event(name="late_event", **changes):
    return ExecutionEvent.model_validate_json(json.dumps(fixture()[name] | changes))
