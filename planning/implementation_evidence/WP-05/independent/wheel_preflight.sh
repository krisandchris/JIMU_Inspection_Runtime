#!/usr/bin/env bash
set -euo pipefail

evidence="$PWD/planning/implementation_evidence/WP-05/independent"
run=$(mktemp -d "$evidence/wheel_preflight_XXXXXXXX")
temp=$(mktemp -d -t jimu-wp05-wheel-XXXXXXXX)
# Keep the staged bytes in temporary storage so the exact preflight can be reproduced.
printf 'review=%s\nworkspace=%s\n' "$run" "$temp"
{
    date -u +%FT%TZ
    printf '%q ' python3 "$evidence/snapshot.py" stage "$PWD" "$temp/snapshot"
    printf '\n'
    python3 "$evidence/snapshot.py" stage "$PWD" "$temp/snapshot"
} |& tee "$run/source.log"
cp "$temp/snapshot/product_sha256.json" "$run/product_sha256.json"
source="$temp/snapshot/product"
export UV_PROJECT_ENVIRONMENT="$temp/venv"
python_path=${WP05_PYTHON:-/usr/bin/python3.12}
(
    cd "$source"
    set -x
    uv sync --python "$python_path" --locked --offline --no-dev --no-install-project
    uv build --offline --wheel --out-dir "$temp/wheel"
    wheel=("$temp"/wheel/*.whl)
    sha256sum "${wheel[@]}"
    uv pip install --offline --no-deps --python "$temp/venv/bin/python" "${wheel[@]}"
    PYTHONPATH="$source" "$temp/venv/bin/python" -c 'import jimu_runtime, sys; from pathlib import Path; print(Path(jimu_runtime.__file__).resolve()); assert Path(jimu_runtime.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())'
) |& tee "$run/wheel.log"
set +e
(
    cd "$temp"
    set -x
    PYTHONPATH="$source" WP05_PROBE_DIAGNOSTICS=1 "$temp/venv/bin/python" "$evidence/run_cases.py" --installed --trace-dir "$run/exec" "$@"
) |& tee "$run/workflow.log"
status=$?
date -u +%FT%TZ | tee -a "$run/workflow.log"
printf 'exit=%s\n' "$status" | tee -a "$run/workflow.log"
exit "$status"
