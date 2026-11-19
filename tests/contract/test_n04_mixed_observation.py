"""Mixed Question types through signed configuration, HTTP and Worker replay."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Literal

import pytest
from pydantic import Field, TypeAdapter

from examples.nonmotor_vehicle_illegal_parking.compilable import lifecycle
from examples.nonmotor_vehicle_illegal_parking.compilable.scene import (
    LIMITS,
    load_source,
    with_source_provenance,
)
from jimu_runtime.bundle.compiler import BundleCompileInput
from jimu_runtime.bundle.integrity import sha256_digest
from jimu_runtime.domain.enums import ArtifactKind, FactStatus
from jimu_runtime.inspection.decision import build_profile_gate
from jimu_runtime.inspection.perception import verify_observation_result
from jimu_runtime.workflow.worker import (
    ClaimedNode,
    WorkerStep,
    WorkflowAdapters,
    WorkflowWorker,
)
from jimu_runtime.workflow.worker_nodes import NodeResult
from jimu_runtime.workflow.worker_records import ObservationMessage
from tests.fixtures.wp04 import http_support
from tests.fixtures.wp05 import perception

loopback_only = http_support.loopback_only

_NOT_VISIBLE = "insufficient_visibility"
_OBSERVATION_SCHEMA = "object_observation-result"
_RECORD_FIELDS = {
    "visible": {"type": "boolean"},
    "side": {"type": "string", "enum": ["left", "right", "rightmost"]},
    "distance": {"type": "number", "minimum": 0.1, "maximum": 0.1},
}


def _typed(kind: str, value_schema: dict) -> dict:
    return {
        "type": "object",
        "properties": {"kind": {"const": kind}, "value": value_schema},
        "required": ["kind", "value"],
        "additionalProperties": False,
    }


def _record(fields: dict) -> dict:
    return {
        "type": "object",
        "properties": fields,
        "required": ["visible"],
        "additionalProperties": False,
    }


def _mixed_source(
    *, rejected_union: Literal["discovery", "assertion"] | None = None
) -> BundleCompileInput:
    source = load_source()
    updated = []
    for row in source.artifacts:
        body = json.loads(row.content)
        if row.kind is ArtifactKind.FACT_SCHEMA and row.artifact_id == "vehicle_type":
            body["type_definition"] = {"kind": "bool"}
        elif (
            row.kind is ArtifactKind.FACT_SCHEMA
            and row.artifact_id == "parking_position"
        ):
            body["type_definition"] = {
                "kind": "record",
                "fields": [
                    {
                        "name": "visible",
                        "required": True,
                        "type_definition": {"kind": "bool"},
                    },
                    {
                        "name": "side",
                        "required": False,
                        "type_definition": {
                            "kind": "enum",
                            "members": _RECORD_FIELDS["side"]["enum"],
                        },
                    },
                    {
                        "name": "distance",
                        "required": False,
                        "type_definition": {
                            "kind": "number",
                            "minimum": 0.1,
                            "maximum": 0.1,
                        },
                    },
                ],
            }
        elif row.artifact_id == "vehicle_type-result":
            body.update(_typed("bool", {"type": "boolean"}))
        elif row.artifact_id == "parking_position-result":
            body.update(
                _typed(
                    "record",
                    _record(
                        {
                            "visible": _typed("bool", _RECORD_FIELDS["visible"]),
                            "side": _typed("enum", _RECORD_FIELDS["side"]),
                            "distance": _typed("number", _RECORD_FIELDS["distance"]),
                        }
                    ),
                )
            )
        elif row.artifact_id == _OBSERVATION_SCHEMA:
            boolean_slot = TypeAdapter(
                bool
                | Annotated[
                    Literal["insufficient_visibility"],
                    Field(
                        title="Visibility", description="The subject cannot be seen."
                    ),
                ]
            ).json_schema()
            if rejected_union == "assertion":
                boolean_slot["anyOf"][1]["minLength"] = 100
            body["properties"]["vehicle_type"] = boolean_slot
            body["properties"]["parking_position"] = {
                "anyOf": [_record(_RECORD_FIELDS), {"const": _NOT_VISIBLE}]
            }
        elif (
            rejected_union == "discovery"
            and row.artifact_id == "profile_discovery-result"
        ):
            body["properties"]["profile_hits"] = {
                "anyOf": [{"type": "boolean"}, {"const": _NOT_VISIBLE}]
            }
        elif (
            row.kind is ArtifactKind.POLICY and row.artifact_id == "question-validation"
        ):
            body["policy"]["max_string_chars"] = 8
        elif row.kind is ArtifactKind.PROMPT_TEMPLATE and row.artifact_id in {
            "object_observation",
            "vehicle_type",
            "parking_position",
        }:
            body["template_text"] = {
                "object_observation": f"Return every answer in its declared JSON type or {_NOT_VISIBLE}.",
                "vehicle_type": "Answer if the vehicle is an electric bicycle as a JSON boolean.",
                "parking_position": "Return whether the curb is visible and its side when known.",
            }[row.artifact_id]
        else:
            updated.append(row)
            continue
        updated.append(replace(row, content=json.dumps(body).encode()))
    return with_source_provenance(replace(source, artifacts=tuple(updated)))


@pytest.mark.parametrize(
    ("case", "schema_id"),
    (("discovery", "profile_discovery-result"), ("assertion", _OBSERVATION_SCHEMA)),
)
def test_visibility_union_rejects_unrelated_schema_or_extra_assertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: Literal["discovery", "assertion"],
    schema_id: str,
) -> None:
    source = _mixed_source(rejected_union=case)
    monkeypatch.setattr(lifecycle, "load_source", lambda: source)
    with pytest.raises(RuntimeError, match=rf"schema_type_unsupported.*{schema_id}"):
        lifecycle.exercise_lifecycle(tmp_path)


def test_mixed_questions_preserve_false_unknown_and_valid_siblings(
    tmp_path: Path, loopback_only: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = (
        {
            "vehicle_type": False,
            "parking_state": "parked",
            "parking_position": {"visible": False, "side": "right", "distance": 0.1},
        },
        {
            "vehicle_type": _NOT_VISIBLE,
            "parking_state": "unparked",
            "parking_position": {"visible": "false"},
        },
        {
            "vehicle_type": True,
            "parking_state": _NOT_VISIBLE,
            "parking_position": {"visible": True},
        },
        {
            "vehicle_type": 0,
            "parking_state": "parked",
            "parking_position": _NOT_VISIBLE,
        },
        {
            "vehicle_type": False,
            "parking_state": "parked",
            "parking_position": {"visible": True, "side": "rightmost"},
        },
    )
    boxes = [{"xyxy": [left, 3, left + 10, 20]} for left in (2, 13, 24, 35, 46)]
    observed: list[ObservationMessage] = []
    wire: dict[str, bytes] = {}
    requests: list[str] = []

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await http_support.read_request(reader)
        body = json.loads(request.body)
        schema = body["response_format"]["json_schema"]["schema"]
        identifier = schema["$id"].split(":")[-2]
        request_digest = sha256_digest(request.body)
        if identifier == "profile_discovery-result":
            reply = {"profile_hits": ["nonmotor_vehicle"]}
        elif identifier == "object_grounding-result":
            reply = {"boxes": boxes}
        else:
            assert identifier == _OBSERVATION_SCHEMA
            assert schema["properties"]["vehicle_type"]["anyOf"][0] == {
                "type": "boolean"
            }
            marker = schema["properties"]["vehicle_type"]["anyOf"][1]
            assert marker["const"] == _NOT_VISIBLE and marker["type"] == "string"
            assert marker["title"] == "Visibility"
            assert schema["properties"]["parking_position"]["anyOf"][0] == _record(
                _RECORD_FIELDS
            )
            prompt = body["messages"][0]["content"][0]["text"]
            assert set(json.loads(prompt.splitlines()[-1])["questions"]) == set(
                replies[0]
            )
            reply = replies[len(requests)]
            requests.append(request_digest)
        content = json.dumps(reply)
        wire[request_digest] = content.encode()
        writer.write(
            http_support.response(
                http_support.chat_body(model=body["model"], content=content),
                headers=(
                    (b"X-Request-ID", f"mixed-n04-{len(wire)}".encode()),
                    (b"X-Jimu-Deployment-Version", b"1.0.0"),
                    (b"X-Jimu-Model-Version", b"1.0.0"),
                ),
            )
        )
        await writer.drain()

    async def run() -> None:
        source = _mixed_source()
        monkeypatch.setattr(perception, "load_source", lambda: source)
        async with (
            http_support.http_peer(loopback_only, peer) as url,
            perception.perception_fixture(tmp_path, url) as fixture,
        ):
            outcomes: list[str] = []

            async def invoke(claimed: ClaimedNode) -> NodeResult:
                try:
                    result = await fixture.adapter.execute(claimed, fixture.gateway)
                except Exception as error:
                    outcomes.append(f"{claimed.node.node_id}: {error}")
                    raise
                outcomes.append(str(claimed.node.node_id))
                return result

            async def verify(claimed: ClaimedNode, message: ObservationMessage) -> None:
                discovery = claimed.previous.discovery
                assert discovery is not None
                (selection,) = discovery.selections
                receipt = await fixture.journal.get_dispatch(
                    claimed.claim.scope,
                    claimed.claim.execution_id,
                    message.invocation.model_invocation_id,
                )
                assert receipt is not None
                (event,) = receipt.results
                slots = event.result
                if (
                    len(requests) == len(replies)
                    and message.invocation.request_digest == requests[-1]
                ):
                    assert (
                        not slots.invalid_question_slots
                        and not slots.missing_question_slots
                    )
                verify_observation_result(
                    claimed.opened.scope,
                    message,
                    claimed.previous.snapshot(),
                    selection,
                    wire[message.invocation.request_digest],
                    slot_receipt=(
                        slots.invalid_question_slots,
                        slots.missing_question_slots,
                    ),
                )
                observed.append(message)

            async def unused(*_: object) -> None:
                pytest.fail("this test requires only N01–N04")

            async def verify_perception(_claimed: ClaimedNode, message: object) -> None:
                assert not getattr(message, "undispatched_profiles", ())

            worker = WorkflowWorker(
                fixture.application,
                fixture.releases,
                fixture.store,
                host=fixture.host,
                bundle_limits=LIMITS,
                adapters=WorkflowAdapters(
                    invoke=invoke,
                    profile_gate=build_profile_gate,
                    validate_observation=verify,
                    validate_decision=unused,
                    validate_perception=verify_perception,
                    verify_invocation=fixture.gateway.verify_invocation,
                ),
            )
            for _ in range(3 + 3 * len(replies)):
                step = await worker.run_once(
                    fixture.accepted.task.idempotency_scope,
                    fixture.accepted.execution.execution_id,
                    owner="mixed-n04-test",
                    lease_ms=10000,
                )
                if len(observed) == len(replies):
                    assert step is WorkerStep.APPLIED
                    break
                assert step is WorkerStep.APPLIED, outcomes
            assert len(observed) == len(replies), outcomes

    asyncio.run(run())
    assert [row.invocation.request_digest for row in observed] == requests
    assert len(set(requests)) == len(replies)
    assert len({row.batch.object_ref for row in observed}) == len(replies)
    assert [row.batch.outcome for row in observed] == [
        "complete",
        "partial",
        "complete",
        "partial",
        "partial",
    ]
    assert all(row.authority is not None for row in observed)
    first, second, third, fourth, fifth = (
        {fact.fact_key: fact for fact in row.facts} for row in observed
    )
    assert first["vehicle_type"].value.model_dump() == {"kind": "bool", "value": False}
    assert first["parking_state"].value.model_dump() == {
        "kind": "enum",
        "value": "parked",
    }
    assert first["parking_position"].value.model_dump() == {
        "kind": "record",
        "value": {
            "visible": {"kind": "bool", "value": False},
            "side": {"kind": "enum", "value": "right"},
            "distance": {"kind": "number", "value": 0.1},
        },
    }
    assert second["vehicle_type"].status is FactStatus.UNKNOWN
    assert second["parking_state"].status is FactStatus.KNOWN
    assert second["parking_position"].status is FactStatus.ERROR
    assert (
        second["parking_position"].reason.reason_code == "perception.question_invalid"
    )
    assert "value" not in second["vehicle_type"].model_dump()
    assert "value" not in second["parking_position"].model_dump()
    assert third["parking_state"].status is FactStatus.UNKNOWN
    assert third["parking_position"].value.model_dump() == {
        "kind": "record",
        "value": {"visible": {"kind": "bool", "value": True}},
    }
    assert fourth["vehicle_type"].status is FactStatus.ERROR
    assert fourth["parking_position"].status is FactStatus.UNKNOWN
    assert "value" not in fourth["vehicle_type"].model_dump()
    assert "value" not in fourth["parking_position"].model_dump()
    assert fifth["vehicle_type"].status is FactStatus.KNOWN
    assert fifth["parking_position"].status is FactStatus.ERROR
