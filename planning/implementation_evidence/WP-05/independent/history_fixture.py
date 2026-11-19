"""Stage the historical WP-01B schema fixture outside the product inventory."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

HISTORY = Path("planning/implementation_evidence/WP-01B/execution_03")
FIXTURE = HISTORY / "after_sha256.json"
# SEAL.json from the approved inherited Git baseline 2552affeb69fbc25.
SEAL_SHA256 = "af8feb56799837d41a149adebc9edf93041d6e3652f4d0637aae9f93b62817dd"


def verify(mode: str, root: Path, product: Path) -> None:
    seal_file = root / HISTORY / "SEAL.json"
    seal_bytes = seal_file.read_bytes()
    seal_sha256 = sha256(seal_bytes).hexdigest()
    if seal_sha256 != SEAL_SHA256:
        raise ValueError("historical WP-01B seal digest differs from the baseline pin")
    seal = json.loads(seal_bytes)
    expected = seal["files"][FIXTURE.as_posix()]
    origin = root / FIXTURE
    content = origin.read_bytes()
    origin_sha256 = sha256(content).hexdigest()
    if origin_sha256 != expected:
        raise ValueError("historical WP-01B source digest differs from its seal")
    staged = product / FIXTURE
    if mode == "stage":
        staged.parent.mkdir(parents=True, exist_ok=True)
        with staged.open("xb") as target:
            target.write(content)
    staged_sha256 = sha256(staged.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "phase": mode,
                "seal": str(seal_file),
                "seal_sha256": seal_sha256,
                "fixture": str(FIXTURE),
                "expected_sha256": expected,
                "origin_sha256": origin_sha256,
                "staged_sha256": staged_sha256,
            },
            sort_keys=True,
        )
    )
    if staged_sha256 != expected:
        raise ValueError(
            "staged historical WP-01B fixture digest differs from its seal"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("stage", "verify"))
    parser.add_argument("root", type=Path)
    parser.add_argument("product", type=Path)
    arguments = parser.parse_args()
    verify(arguments.mode, arguments.root, arguments.product)
