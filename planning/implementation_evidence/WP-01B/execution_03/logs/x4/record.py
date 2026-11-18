"""Record authorized CPU command evidence without changing package dependencies."""

import datetime
import json
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
name = sys.argv[1]
args = sys.argv[2:]
start = datetime.datetime.now(datetime.UTC).isoformat()
result = subprocess.run(args, capture_output=True, text=True)
end = datetime.datetime.now(datetime.UTC).isoformat()
path = (
    root
    / "planning/implementation_evidence/WP-01B/execution_03/logs/x4"
    / f"{name}.json"
)
if path.exists():
    raise RuntimeError("Refuse to replace prior evidence")
path.write_text(
    json.dumps(
        dict(
            argv=args,
            cwd=str(root),
            start_utc=start,
            end_utc=end,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        ),
        ensure_ascii=True,
        indent=2,
    )
    + "\n"
)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
raise SystemExit(result.returncode)
