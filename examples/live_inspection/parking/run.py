"""Run the live parking scene through the public task API and all six nodes."""

import argparse
import asyncio
import json
from pathlib import Path

from examples.live_inspection.host import bridge_model_endpoints, run_case
from examples.live_inspection.providers.live_wire_bridge import (
    ProfileRoute,
    start_bridge,
)
from examples.live_inspection.scenes.trusted_scope import SCOPE_IMAGE_SHA256
from jimu_runtime.domain.values import RecordValue, StringValue
from jimu_runtime.inspection.geometry.node import GeometryNodeAdapter

from .scene import CAPABILITY, build_source, capability_snapshot
from .tool import ParkingGeometryInput, entries_for, tool_proof
from .wevis_masks import APPROVED_IMAGE_SHA256


async def run(
    output: Path,
    image: Path,
    wevis_segment_url: str,
    *,
    qwen_enable_thinking: bool | None = None,
) -> dict[str, object]:
    def execute_tools(fixture, registered_tools_adapter):
        if fixture.media_store is None:
            raise ValueError("Live parking needs the original-image media store")
        return GeometryNodeAdapter(
            fixture.media_store,
            registered_tools_adapter,
            input_type=ParkingGeometryInput,
        ).execute

    with start_bridge(
        output / "wire",
        profile_routes={
            "nonmotor_vehicle": ProfileRoute(
                "electric bicycle", stage1_target="non_motor_vehicle"
            ),
        },
        enable_region_projection=True,
        qwen_enable_thinking=qwen_enable_thinking,
    ) as bridge:
        return await run_case(
            output,
            image,
            source=build_source(region_projection=True),
            capability_builder=capability_snapshot,
            model_endpoints=bridge_model_endpoints(
                bridge.url, supports_region_projection=True
            ),
            trusted_metadata=RecordValue(
                kind="record",
                value={
                    SCOPE_IMAGE_SHA256: StringValue(
                        kind="string", value=APPROVED_IMAGE_SHA256
                    ),
                },
            ),
            tool_entries=entries_for(output / "tool-wire", wevis_segment_url),
            tool_proof_overrides={CAPABILITY: tool_proof()},
            execute_tools=execute_tools,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument(
        "--wevis-segment-url", default="http://127.0.0.1:18103/v1/segment"
    )
    parser.add_argument("--qwen-disable-thinking", action="store_true")
    arguments = parser.parse_args()
    payload = asyncio.run(
        run(
            arguments.output,
            arguments.image,
            arguments.wevis_segment_url,
            qwen_enable_thinking=False if arguments.qwen_disable_thinking else None,
        )
    )
    print(
        json.dumps(
            {key: payload[key] for key in ("task_id", "execution_id", "bundle_digest")}
        )
    )
