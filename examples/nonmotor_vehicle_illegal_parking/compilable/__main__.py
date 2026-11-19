"""Compile and verify the synthetic scene without retaining secrets or a ledger."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .lifecycle import exercise_lifecycle


def main() -> None:
    with TemporaryDirectory(prefix="jimu-nonmotor-scene-") as directory:
        result = exercise_lifecycle(Path(directory))
        print(
            json.dumps(
                {
                    "bundle": result.bundle.bundle_ref.model_dump(mode="json"),
                    "release_id": result.release.release_id,
                    "current_release_id": result.current_activation.release_ref.record_id,
                    "current_bundle_digest": result.current_activation.bundle_ref.bundle_digest,
                    "task_id": result.task.task_id,
                    "admitted_activated_reopened": True,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
