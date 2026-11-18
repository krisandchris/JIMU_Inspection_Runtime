"""Capture dependencies read by this independent frozen regression suite."""

import datetime
import hashlib
import json
import sys
from pathlib import Path

root = Path.cwd()
paths = list((root / "src").rglob("*.py"))
for name in ("x3", "x2", "a1", "r1"):
    paths.extend(
        path
        for path in (root / "tests/fixtures/wp01b" / name).rglob("*")
        if path.is_file() and path.suffix in (".py", ".json")
    )
paths.extend(
    root / name
    for name in (
        "pyproject.toml",
        "uv.lock",
        "tests/negative/test_b_producer_authority_review_regressions.py",
    )
)
values = {
    str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(set(paths))
}
path = (
    root
    / "planning/implementation_evidence/WP-01B/execution_03/logs/x3_review"
    / f"final_{sys.argv[1]}_sha256.json"
)
if path.exists():
    raise RuntimeError("Refuse to replace snapshot")
path.write_text(
    json.dumps(
        dict(utc=datetime.datetime.now(datetime.UTC).isoformat(), sha256=values),
        indent=2,
    )
    + "\n"
)
print(
    json.dumps(
        dict(
            files=len(values),
            checker_sha256=values[
                "src/jimu_runtime/domain/validation/producer_authority.py"
            ],
            test_sha256=values[
                "tests/negative/test_b_producer_authority_review_regressions.py"
            ],
        ),
        indent=2,
    )
)
