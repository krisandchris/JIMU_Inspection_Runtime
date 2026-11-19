#!/usr/bin/env bash
set -u -o pipefail

[[ $# == 2 || $# == 3 ]] || { printf 'usage: bash %s PRIMARY_PRODUCT_MANIFEST SHA256 [all|runtime|static|pytest]\n' "$0"; exit 2; }
group=${3:-all}
case "$group" in all|runtime|static|pytest) ;; *) exit 2;; esac
evidence=planning/implementation_evidence/WP-04/independent
verifier=planning/implementation_evidence/WP-03/independent/verify_snapshot.py
previous=planning/implementation_evidence/WP-03/primary_freeze_b8e95556aa80/product_sha256.json
manifest_path=$1
manifest_sha=$2
attempt=$(mktemp -d "$evidence/frozen_run_XXXXXXXX") || exit 1
runtime=$(mktemp -d -t jimu-wp04-acceptance-XXXXXXXX) || exit 1
trap 'rm -r -- "$runtime"' EXIT
export UV_PROJECT_ENVIRONMENT="$runtime/venv"
export MYPY_CACHE_DIR="$runtime/mypy"
export RUFF_CACHE_DIR="$runtime/ruff"
export PYTHONDONTWRITEBYTECODE=1

failure=0
run() {
    local label=$1
    shift
    (
        date -u +%FT%TZ
        printf '%q ' "$@"
        printf '\n'
        "$@"
        local status=$?
        date -u +%FT%TZ
        printf 'exit=%s\n' "$status"
        exit "$status"
    ) |& tee "$attempt/$label.log"
    local status=$?
    if [[ $status != 0 ]]; then failure=1; fi
}

run baseline git rev-parse HEAD
run verify-before python3 "$verifier" "$manifest_path" "$manifest_sha"
if [[ -f $(dirname "$manifest_path")/product.zip ]]; then
    run archive python3 "$verifier" --archive "$(dirname "$manifest_path")"
fi
[[ $failure == 0 ]] || exit 1
run locked-sync uv sync --locked --offline
[[ $failure == 0 ]] || exit 1
run environment uv run --locked --offline python -c 'import importlib.metadata as m, platform, sys; print(platform.platform()); print(sys.version); print({name:m.version(name) for name in ("pydantic","rfc8785","jsonschema","sqlalchemy","psycopg","psycopg-binary","httpx","httpcore","pytest","ruff","mypy")})'
run uv-version uv --version
run dependency-lock sha256sum pyproject.toml uv.lock
if [[ $group == all || $group == runtime ]]; then
    run independent-cpu env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_resources.py"
    run independent-dispatch env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_model.py"
    run independent-fallback env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_fallback.py"
    run independent-revocation env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_bridge.py" revocation
    run independent-inflight-revocation env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_bridge.py" inflight-revocation
    run independent-ceilings env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_bridge.py" ceilings
    run independent-tool env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_tool.py"
    run independent-tool-timeout env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_tool.py" physical-timeout
    run independent-nested-fallback env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_nested.py"
    run independent-nested-forgery env PYTHONPATH=. uv run --locked --offline python "$evidence/probe_nested.py" forged
fi
if [[ $group == all || $group == static ]]; then
    run ruff-product uv run --locked --offline ruff check src tests scripts
    run ruff-format-product uv run --locked --offline ruff format --check src tests scripts
    run ruff-independent uv run --locked --offline ruff check "$evidence"
    run mypy uv run --locked --offline mypy --strict src/jimu_runtime scripts/generate_schemas.py
    run generated-check uv run --locked --offline python scripts/generate_schemas.py --check
    run generated-first uv run --locked --offline python scripts/generate_schemas.py --output "$runtime/generated_first"
    run generated-second uv run --locked --offline python scripts/generate_schemas.py --output "$runtime/generated_second"
    run generated-repeat diff --recursive --brief "$runtime/generated_first" "$runtime/generated_second"
    run generated-working-tree diff --recursive --brief schemas/generated "$runtime/generated_first"
    run generated-baseline python3 "$evidence/unchanged_schemas.py" "$previous" "$manifest_path"
    run product-delta python3 "$verifier" --diff "$previous" "$manifest_path"
    run git-diff-check git diff --check
fi
if [[ $group == all || $group == pytest ]]; then
    run pytest uv run --locked --offline python -m pytest -q -ra -o "cache_dir=$runtime/pytest" --junitxml "$attempt/pytest.xml"
fi
run verify-after python3 "$verifier" "$manifest_path" "$manifest_sha"
if [[ -f $(dirname "$manifest_path")/product.zip ]]; then
    run archive-after python3 "$verifier" --archive "$(dirname "$manifest_path")"
fi
printf '%s\n' "$attempt"
exit "$failure"
