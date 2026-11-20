"""Run the real isolation-only mask inspection, with optional synthetic scope."""

import argparse
import asyncio
import json
from pathlib import Path

from examples.live_inspection.host import CaptureScope, bridge_model_endpoints, run_case
from examples.live_inspection.providers.live_wire_bridge import (
    ProfileRoute,
    start_bridge,
)
from examples.live_inspection.scenes import (
    build_mask_compile_input,
    capability_snapshot,
)
from examples.live_inspection.scenes.trusted_scope import MASK_REGION

_PROFILE = "food_service_worker"


async def run(
    output: Path,
    image: Path,
    *,
    synthetic_roster: bool,
    qwen_enable_thinking: bool | None = None,
) -> dict[str, object]:
    source = build_mask_compile_input(region_projection=True)
    if synthetic_roster:
        return await run_case(
            output,
            image,
            source=source,
            capability_builder=capability_snapshot,
            region_id=MASK_REGION,
            capture_scope=CaptureScope.KITCHEN_ROSTER,
            qwen_enable_thinking=qwen_enable_thinking,
        )
    with start_bridge(
        output / "provider",
        profile_routes={_PROFILE: ProfileRoute("person", stage1_target="person")},
        enable_region_projection=True,
        qwen_enable_thinking=qwen_enable_thinking,
    ) as bridge:
        return await run_case(
            output,
            image,
            source=source,
            capability_builder=capability_snapshot,
            region_id=MASK_REGION,
            model_endpoints=bridge_model_endpoints(
                bridge.url, supports_region_projection=True
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("image", type=Path)
    parser.add_argument("--synthetic-roster", action="store_true")
    parser.add_argument("--qwen-disable-thinking", action="store_true")
    arguments = parser.parse_args()
    result = asyncio.run(
        run(
            arguments.output,
            arguments.image,
            synthetic_roster=arguments.synthetic_roster,
            qwen_enable_thinking=False if arguments.qwen_disable_thinking else None,
        )
    )
    print(
        json.dumps(
            {key: result[key] for key in ("task_id", "execution_id", "final")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
