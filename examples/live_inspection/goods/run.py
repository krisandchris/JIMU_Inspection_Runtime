"""Run the signed sidewalk carton pilot on its registered original RGB image."""

import argparse
import asyncio
import json
from pathlib import Path

from examples.live_inspection.host import CaptureScope, run_case
from examples.live_inspection.scenes import (
    build_sidewalk_goods_compile_input,
    capability_snapshot,
)
from examples.live_inspection.scenes.trusted_scope import (
    SIDEWALK_REGION,
)


def run_goods(
    output_dir: Path,
    image_path: Path,
    *,
    include_placement_policy: bool = True,
    qwen_enable_thinking: bool | None = None,
) -> dict[str, object]:
    capture_scope = (
        CaptureScope.SIDEWALK_POLICY
        if include_placement_policy
        else CaptureScope.SIDEWALK_NO_POLICY
    )
    return asyncio.run(
        run_case(
            output_dir,
            image_path,
            source=build_sidewalk_goods_compile_input(region_projection=True),
            capability_builder=capability_snapshot,
            region_id=SIDEWALK_REGION,
            capture_scope=capture_scope,
            qwen_enable_thinking=qwen_enable_thinking,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("image_path", type=Path)
    parser.add_argument(
        "--omit-placement-policy",
        action="store_true",
        help="Run a control without the trusted synthetic no-placement policy field.",
    )
    parser.add_argument("--qwen-disable-thinking", action="store_true")
    args = parser.parse_args()
    result = run_goods(
        args.output_dir,
        args.image_path,
        include_placement_policy=not args.omit_placement_policy,
        qwen_enable_thinking=False if args.qwen_disable_thinking else None,
    )
    print(
        json.dumps(
            {
                "task_id": result["task_id"],
                "execution_id": result["execution_id"],
                "release_id": result["release_id"],
                "bundle_digest": result["bundle_digest"],
                "result_file": str(args.output_dir / "result.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
