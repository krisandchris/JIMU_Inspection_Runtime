#!/usr/bin/env bash
set -u -o pipefail

[[ $# == 2 || $# == 3 ]] || {
    printf 'usage: bash %s PRIMARY_PRODUCT_MANIFEST SHA256 [all|runtime|static|pytest]\n' "$0"
    exit 2
}
group=${3:-all}
case "$group" in all|runtime|static|pytest) ;; *) exit 2;; esac
root=$PWD
evidence="$root/planning/implementation_evidence/WP-05/independent"
previous="$root/planning/implementation_evidence/WP-04/primary_freeze_924eb788eab1/product_sha256.json"
previous_scripts="$root/planning/implementation_evidence/WP-04/independent"
delta="$root/planning/implementation_evidence/WP-03/independent/verify_snapshot.py"
scene=examples/nonmotor_vehicle_illegal_parking/compilable
manifest=$(realpath -e "$1") || exit 2
manifest_sha=$2
archive="$(dirname "$manifest")/product.zip"
[[ -f "$archive" ]] || { printf 'Primary product ZIP is required.\n'; exit 2; }
run_path=$(mktemp -d "$evidence/frozen_run_XXXXXXXX") || exit 1
temp=$(mktemp -d -t jimu-wp05-acceptance-XXXXXXXX) || exit 1
trap 'rm -r -- "$temp"' EXIT
source="$temp/product"
python_path=${WP05_PYTHON:-/usr/bin/python3.12}
export UV_PROJECT_ENVIRONMENT="$temp/dev"
export MYPY_CACHE_DIR="$temp/mypy"
export RUFF_CACHE_DIR="$temp/ruff"
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
    ) |& tee "$run_path/$label.log"
    local status=$?
    if [[ $status != 0 ]]; then failure=1; fi
}

run baseline git rev-parse HEAD
probe_inputs=("$evidence"/*.py "$evidence"/fixtures/*.png "$evidence"/fixtures/*.sha256 "$evidence"/gold.json "$evidence"/run_acceptance.sh)
run probe-inputs sha256sum "${probe_inputs[@]}"
sed '/^[0-9a-f]\{64\}  /!d' "$run_path/probe-inputs.log" > "$run_path/probe_inputs.sha256"
run verify-before python3 "$evidence/snapshot.py" verify "$root" "$manifest" "$manifest_sha"
[[ $failure == 0 ]] || exit 1
run extract python3 -m zipfile -e "$archive" "$source"
[[ $failure == 0 ]] || exit 1
if [[ $group == all || $group == pytest ]]; then
    run history-before python3 "$evidence/history_fixture.py" stage "$root" "$source"
    [[ $failure == 0 ]] || exit 1
fi
run uv-version uv --version
run dependency-lock sha256sum "$source/pyproject.toml" "$source/uv.lock"
run interpreter "$python_path" -VV
run locked-sync uv sync --project "$source" --python "$python_path" --locked --offline
[[ $failure == 0 ]] || exit 1
run versions "$temp/dev/bin/python" -c 'import importlib.metadata as m, platform; print(platform.platform()); print({p:m.version(p) for p in ("pydantic","rfc8785","jsonschema","sqlalchemy","psycopg","psycopg-binary","httpx","httpcore","pillow","pytest","ruff","mypy")})'
if [[ $group == all || $group == static ]]; then
    cd "$source" || exit 1
    run ruff "$temp/dev/bin/ruff" check src tests scripts "$scene"
    run ruff-format "$temp/dev/bin/ruff" format --check src tests scripts "$scene"
    run ruff-independent "$temp/dev/bin/ruff" check "$evidence"
    run mypy "$temp/dev/bin/mypy" --strict src/jimu_runtime scripts/generate_schemas.py "$scene/lifecycle.py" "$scene/local_trust.py" "$scene/scene.py" "$scene/__main__.py"
    run schema-check "$temp/dev/bin/python" scripts/generate_schemas.py --check
    run schema-first "$temp/dev/bin/python" scripts/generate_schemas.py --output "$temp/schema-first"
    run schema-second "$temp/dev/bin/python" scripts/generate_schemas.py --output "$temp/schema-second"
    run schema-repeat diff --recursive --brief "$temp/schema-first" "$temp/schema-second"
    run schema-frozen-tree diff --recursive --brief schemas/generated "$temp/schema-first"
    run schema-wp04 python3 "$previous_scripts/unchanged_schemas.py" "$previous" "$manifest"
    run product-delta python3 "$delta" --diff "$previous" "$manifest"
    cd "$root" || exit 1
    run git-diff-check git diff --check
fi
if [[ $group == all || $group == runtime ]]; then
    run wheel-sync env "UV_PROJECT_ENVIRONMENT=$temp/wheel-env" uv sync --project "$source" --python "$python_path" --locked --offline --no-dev --no-install-project
    run wheel-build uv build --project "$source" --offline --wheel --out-dir "$temp/wheel"
    wheel=("$temp"/wheel/*.whl)
    if [[ -f ${wheel[0]} ]]; then
        run wheel-sha sha256sum "${wheel[@]}"
        run wheel-install uv pip install --offline --no-deps --python "$temp/wheel-env/bin/python" "${wheel[@]}"
        cd "$temp" || exit 1
        runtime=("$temp/wheel-env/bin/python")
        public=(env "PYTHONPATH=$source" WP05_PROBE_DIAGNOSTICS=0 "${runtime[@]}")
        run installed-origin "${public[@]}" -c 'import jimu_runtime, sys; from pathlib import Path; p=Path(jimu_runtime.__file__).resolve(); print(p); assert p.is_relative_to(Path(sys.prefix).resolve())'
        run installed-intake "${public[@]}" "$evidence/probe_preflight.py"
        run zero-objects "${public[@]}" "$evidence/run_cases.py" --installed --trace-dir "$run_path/exec-zero" no_hits no_target grounding_failure grounding_timeout
        run business-verdicts "${public[@]}" "$evidence/run_cases.py" --installed --trace-dir "$run_path/exec-business" known_violation wheel_on_tactile multiple_objects partial_valid_violation
        run missing-and-retry "${public[@]}" "$evidence/run_cases.py" --installed --trace-dir "$run_path/exec-retry" missing_slot explicit_visibility whole_batch_retry candidate_not_configured candidate_unavailable candidate_timeout
        run restart-N04-retry "${public[@]}" "$evidence/run_cases.py" --installed --trace-dir "$run_path/exec-restart-N04-retry" --restart-at N04_RETRY whole_batch_retry
        for phase in N01 N01_REVOKED N04 N05 N05_ADMITTED_NO_RETRY N05_ADMITTED_RETRY; do
            run "restart-$phase" "${public[@]}" "$evidence/run_cases.py" --installed --trace-dir "$run_path/exec-restart-$phase" --restart-at "$phase" known_violation
        done
        cd "$root" || exit 1
    else
        failure=1
    fi
fi
if [[ $group == all || $group == pytest ]]; then
    cd "$source" || exit 1
    run predicates "$temp/dev/bin/python" -m pytest -q -ra -o "cache_dir=$temp/pytest-predicates" --junitxml "$run_path/predicates.xml" tests/unit/test_wp05_predicates.py tests/unit/test_wp05_predicates_bundle.py tests/negative/test_wp05_predicates_rejections.py
    run pytest "$temp/dev/bin/python" -m pytest -q -ra -o "cache_dir=$temp/pytest" --junitxml "$run_path/pytest.xml" tests "$scene"
    cd "$root" || exit 1
fi
if [[ $group == all || $group == pytest ]]; then
    run history-after python3 "$evidence/history_fixture.py" verify "$root" "$source"
fi
run probe-inputs-after sha256sum -c "$run_path/probe_inputs.sha256"
run verify-after python3 "$evidence/snapshot.py" verify "$root" "$manifest" "$manifest_sha"
printf '%s\n' "$run_path"
exit "$failure"
