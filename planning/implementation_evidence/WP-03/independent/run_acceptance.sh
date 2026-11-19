#!/usr/bin/env bash
set -u -o pipefail

[[ $# == 2 ]] || { printf 'usage: bash %s PRIMARY_PRODUCT_MANIFEST SHA256\n' "$0"; exit 2; }
evidence=planning/implementation_evidence/WP-03/independent
manifest_path=$1
manifest_sha=$2
previous=planning/implementation_evidence/WP-02/primary_freeze_844ed4ac1275/product_sha256.json
attempt=$(mktemp -d "$evidence/frozen_run_XXXXXXXX") || exit 1
generated=$(mktemp -d -t jimu-wp03-schemas-XXXXXXXX) || exit 1
trap 'rm -r -- "$generated"' EXIT
generated_a="$generated/first"
generated_b="$generated/second"

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
run environment uv run --locked python -c 'import importlib.metadata as m, platform, sys; print(platform.platform()); print(sys.version); print({name:m.version(name) for name in ("pydantic","rfc8785","jsonschema","sqlalchemy","psycopg","psycopg-binary","pytest","ruff","mypy")})'
run uv-version uv --version
run verify-before python3 "$evidence/verify_snapshot.py" "$manifest_path" "$manifest_sha"
[[ $failure == 0 ]] || exit 1
run independent-pg env PYTHONPATH=. uv run --locked python "$evidence/probe_wp03.py"
run pytest uv run --locked python -m pytest -q -ra --junitxml "$attempt/pytest.xml"
run ruff-product uv run --locked ruff check src tests scripts
run ruff-format-product uv run --locked ruff format --check src tests scripts
run ruff-independent uv run --locked ruff check "$evidence"
product_failure=$failure
run ruff-all uv run --locked ruff check .
failure=$product_failure
run mypy uv run --locked mypy --strict src/jimu_runtime scripts/generate_schemas.py
run generated-check uv run --locked python scripts/generate_schemas.py --check
run generated-first uv run --locked python scripts/generate_schemas.py --output "$generated_a"
run generated-second uv run --locked python scripts/generate_schemas.py --output "$generated_b"
run generated-independent-diff diff --recursive --brief "$generated_a" "$generated_b"
run generated-working-tree-diff diff --recursive --brief schemas/generated "$generated_a"
run generated-baseline python3 "$evidence/verify_snapshot.py" --schema-baseline "$previous" "$manifest_path"
run git-diff-check git diff --check
run product-delta python3 "$evidence/verify_snapshot.py" --diff "$previous" "$manifest_path"
run verify-after python3 "$evidence/verify_snapshot.py" "$manifest_path" "$manifest_sha"
printf '%s\n' "$attempt"
exit "$failure"
