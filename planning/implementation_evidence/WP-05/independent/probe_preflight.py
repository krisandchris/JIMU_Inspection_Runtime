"""Verify real input admission before invoking the product Worker."""

import asyncio
import base64
import io
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cases import CASES, read_frame
from harness import Harness
from loopback import ParkingProvider
from PIL import Image
from sqlalchemy import text

from examples.nonmotor_vehicle_illegal_parking.compilable import scene
from jimu_runtime.domain.runtime.media import MediaRef
from jimu_runtime.inspection.media import MediaError
from jimu_runtime.workflow.intake import IntakeAuthorizationError
from jimu_runtime.workflow.postgres import apply_migrations, create_postgres_engine
from jimu_runtime.workflow.repository import get_execution
from tests.fixtures.wp03.postgres.support import POSTGRES_IMAGE, isolated_postgres


def emit(check: str, **fields: object) -> None:
    print(json.dumps({"check": check, **fields}, sort_keys=True), flush=True)


def independent_pixel_check() -> None:
    colors = ((16, 174, 208), (224, 192, 32), (24, 192, 64))
    expected = {
        "base": (42, 192, 0),
        "two": (60, 192, 0),
        "wheel-on-tactile": (42, 192, 20),
    }
    for variant, counts in expected.items():
        frame = read_frame(scene.DIRECTORY, variant)
        with Image.open(io.BytesIO(frame.content)) as actual:
            assert actual.mode == "RGB" and actual.size == (64, 48)
            pixels = Counter(actual.get_flattened_data())
            assert tuple(pixels[color] for color in colors) == counts
        emit(
            "pinned_test_frame",
            variant=variant,
            sha256=frame.digest,
            wheel=counts[0],
            tactile=counts[1],
            overlap=counts[2],
        )


async def endpoint_smoke() -> None:
    frame = read_frame(scene.DIRECTORY, "base")
    provider = ParkingProvider(
        CASES["whole_batch_retry"],
        frame,
        model_id="independent-parking-vision",
        deployment_version="1.0.0",
        model_version="1.0.0",
    )
    source = scene.DIRECTORY / "source" / "response_schema"
    response_schema = json.loads(
        (source / "object_observation-result.json").read_bytes()
    )
    model_input = {
        "image": {"media_id": "wp05.smoke", "sha256": frame.digest},
        "bbox": {"xyxy": [40, 23, 52, 33]},
    }
    instructions = {
        "object_id": "fixture-smoke",
        "questions": dict.fromkeys(
            ("vehicle_type", "parking_state", "parking_position"), "fixture prompt"
        ),
    }
    data_uri = "data:image/png;base64," + base64.b64encode(frame.content).decode()
    request = {
        "model": "independent-parking-vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "\n".join(
                            (
                                "test fixture",
                                json.dumps(model_input),
                                json.dumps(instructions),
                            )
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"strict": True, "schema": response_schema},
        },
    }
    async with provider.serving(), httpx.AsyncClient(trust_env=False) as client:
        assert provider.url is not None
        first = await client.post(provider.url, json=request)
        second = await client.post(provider.url, json=request)
    expected_extra = "unregistered_probe_slot"
    assert first.status_code == second.status_code == 200
    assert expected_extra in json.loads(
        first.json()["choices"][0]["message"]["content"]
    )
    assert expected_extra not in json.loads(
        second.json()["choices"][0]["message"]["content"]
    )
    assert len(provider.requests) == len(provider.replies) == 2 and not provider.errors
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    for reply, observed in zip(provider.replies, (first, second), strict=True):
        assert reply.response_digest == sha256(observed.content).hexdigest()
    emit(
        "loopback_provider_smoke", posted=len(provider.requests), distinct_replies=True
    )


async def intake_smoke() -> None:
    frame = read_frame(scene.DIRECTORY, "base")
    registry = scene.capability_snapshot(scene.load_source())
    identity = registry.model_deployments[0].identity
    provider = ParkingProvider(
        CASES["known_violation"],
        frame,
        model_id="independent-parking-vision",
        deployment_version=identity.deployment_version,
        model_version=identity.model_version,
    )
    with (
        isolated_postgres() as pg,
        TemporaryDirectory(prefix="wp05-independent-intake-") as base,
    ):
        sql_owner = create_postgres_engine(pg.migration_url)
        app = create_postgres_engine(pg.application_url)
        try:
            applied = await apply_migrations(sql_owner, pg.target)
            assert applied[:7] == (1, 2, 3, 4, 5, 6, 7)
            assert await apply_migrations(sql_owner, pg.target) == ()
            async with provider.serving():
                assert provider.url is not None
                host = Harness(app, Path(base), provider.url)
                release_a = host.release(0)
                request = host.request("intake-preflight-a", frame)
                original = await host.accept(request, frame)
                assert original.execution.pins.bundle_digest == release_a.bundle_digest
                release_b = host.release(1)
                second = await host.accept(
                    host.request("intake-preflight-b", frame), frame
                )
                assert (
                    second.execution.pins.bundle_digest
                    == release_b.bundle_digest
                    != release_a.bundle_digest
                )
                assert (
                    await host.service(retries=False).submit(
                        request, principal=host.principal
                    )
                    == original
                )
                retained = await host.media.verified_task_original(
                    host.scope, original.task
                )
                assert retained.content == frame.content
                wrong_scope = host.scope.model_copy(
                    update={"client_id": "different-edge"}
                )
                try:
                    await host.media.read_original(wrong_scope, request.media_ref)
                except MediaError:
                    pass
                else:
                    raise AssertionError("cross-client media was readable")
                wrong = host.request("wrong-hash", frame).model_copy(
                    update={
                        "media_ref": MediaRef(
                            media_id=request.media_ref.media_id, sha256="0" * 64
                        ),
                    }
                )
                try:
                    await host.service(retries=False).submit(
                        wrong, principal=host.principal
                    )
                except IntakeAuthorizationError:
                    pass
                else:
                    raise AssertionError("wrong media digest was accepted")
                async with app.connect() as con:
                    historical = await get_execution(
                        con, host.scope, original.execution.execution_id
                    )
                    assert (
                        historical is not None
                        and historical.execution.pins == original.execution.pins
                    )
                    assert (
                        await get_execution(
                            con, wrong_scope, original.execution.execution_id
                        )
                        is None
                    )
                    jobs = await con.scalar(
                        text("SELECT count(*) FROM jimu_workflow.jobs")
                    )
                assert jobs == 2
                emit(
                    "isolated_scene_signed_release_and_intake",
                    image=POSTGRES_IMAGE,
                    version=pg.server_version,
                    initial_migrations=applied,
                    old=release_a.bundle_digest,
                    new=release_b.bundle_digest,
                    distinct_executions=True,
                    wrong_scope_denied=True,
                    wrong_digest_denied=True,
                    business_jobs=jobs,
                )
                assert not provider.requests, (
                    "intake unexpectedly dispatched a model request"
                )
        finally:
            await app.dispose()
            await sql_owner.dispose()


async def main() -> None:
    independent_pixel_check()
    await endpoint_smoke()
    await intake_smoke()


if __name__ == "__main__":
    asyncio.run(main())
