"""Deterministic regression reproduction against main's unchanged N05 worker."""

import asyncio
import json
import traceback
from pathlib import Path
from tempfile import TemporaryDirectory

from runtime_harness import run
from trial_scenes import build_mask_compile_input, capability_snapshot

from examples.nonmotor_vehicle_illegal_parking.compilable.scene import DIRECTORY
from tests.fixtures.wp04 import http_support

_PAYLOADS = {
    "profile_discovery-result": {"profile_hits": ["food_service_worker"]},
    "object_grounding-result": {"boxes": [{"xyxy": [2, 3, 18, 20]}]},
    "object_observation-result": {
        "food_service_role": "insufficient_visibility",
        "kitchen_position": "insufficient_visibility",
        "face_visibility": "face_assessable",
        "mask_status": "not_wearing_mask",
    },
}


async def reproduce() -> None:
    calls = []

    async def peer(reader, writer):
        request = await http_support.read_request(reader)
        wire = json.loads(request.body)
        schema = wire["response_format"]["json_schema"]["schema"]
        identifier = schema["$id"].split(":")[-2]
        calls.append(identifier)
        payload = _PAYLOADS[identifier]
        response = http_support.chat_body(
            model=wire["model"], content=json.dumps(payload)
        )
        writer.write(
            http_support.response(
                response,
                headers=(
                    (b"X-Request-ID", b"empty-tool-plan-repro"),
                    (b"X-Jimu-Deployment-Version", b"1.0.0"),
                    (b"X-Jimu-Model-Version", b"1.0.0"),
                ),
            )
        )
        await writer.drain()

    with TemporaryDirectory() as workspace:
        directory = Path(workspace)
        image_path = DIRECTORY / "assets" / "synthetic-frame.png"
        async with http_support.http_peer(set(), peer) as url:
            try:
                await run(
                    directory,
                    url,
                    image_path,
                    build_mask_compile_input,
                    region="isolated_food_service",
                    caps=capability_snapshot,
                )
            except ValueError as error:
                assert (
                    str(error)
                    == "a successful result requires at least one pinned artifact"
                )
                frame = traceback.extract_tb(error.__traceback__)[-1]
                assert frame.filename.endswith("/jimu_runtime/workflow/jobs.py")
                assert frame.name == "_submit"
                assert calls == list(_PAYLOADS)
                assert not (directory / "result.json").exists()
                print(
                    "REPRODUCED: Native N05 without child steps cannot commit a successful result"
                )
                print(
                    "N01-N04 applied; no N06 request or finalization; only loopback canned provider responses"
                )
            else:
                raise AssertionError("Expected N05 empty-output failure")


if __name__ == "__main__":
    asyncio.run(reproduce())
