"""Independent bounded regression command evidence; never rewrites earlier logs."""

import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
checker = root / "src/jimu_runtime/domain/validation/producer_authority.py"
args = sys.argv[2:]
start_hash = hashlib.sha256(checker.read_bytes()).hexdigest()
start = datetime.datetime.now(datetime.UTC).isoformat()
result = subprocess.run(args, capture_output=True, text=True)
end = datetime.datetime.now(datetime.UTC).isoformat()
path = (
    root
    / "planning/implementation_evidence/WP-01B/execution_03/logs/x3_review"
    / f"{sys.argv[1]}.json"
)
if path.exists():
    raise RuntimeError("Refuse to replace earlier evidence")
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
            checker_before_sha256=start_hash,
            checker_after_sha256=hashlib.sha256(checker.read_bytes()).hexdigest(),
        ),
        indent=2,
    )
    + "\n"
)
print(result.stdout, end="")
print(result.stderr, end="", file=sys.stderr)
raise SystemExit(result.returncode)
