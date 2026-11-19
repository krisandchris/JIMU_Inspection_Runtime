#!/usr/bin/env bash
set -u -o pipefail

[[ $# == 2 || $# == 3 ]] || {
    printf 'usage: bash %s PRIMARY_PRODUCT_MANIFEST SHA256 [--all|--runtime|--static|--pytest]\n' "$0"
    exit 2
}
group=${3:---all}
group=${group#--}
case "$group" in all|runtime|static|pytest) ;; *) exit 2;; esac
root=$PWD
evidence="$root/planning/implementation_evidence/WP-06/independent"
wp05="$root/planning/implementation_evidence/WP-05/independent"
legacy="$root/planning/implementation_evidence/WP-05/primary_freeze_124bfcc1984c"
legacy_sha=124bfcc1984c96f17db87fdd848f152a2ebeb69b7a72349e4ecfea2c1bfb29ad
previous="$root/planning/implementation_evidence/WP-04/primary_freeze_924eb788eab1/product_sha256.json"
previous_scripts="$root/planning/implementation_evidence/WP-04/independent"
delta="$root/planning/implementation_evidence/WP-03/independent/verify_snapshot.py"
scene=examples/nonmotor_vehicle_illegal_parking/compilable
manifest=$(realpath -e "$1") || exit 2
manifest_sha=$2
archive="$(dirname "$manifest")/product.zip"
[[ -f "$archive" ]] || { printf 'Primary product ZIP is required.\n'; exit 2; }
run_path=$(mktemp -d "$evidence/frozen_run_XXXXXXXX") || exit 1
temp=$(mktemp -d -t jimu-wp06-acceptance-XXXXXXXX) || exit 1
trap 'rm -r -- "$temp"' EXIT
source="$temp/product"
python_path=${WP06_PYTHON:-/usr/bin/python3.12}
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
    return "$status"
}

run baseline git rev-parse HEAD
probe_inputs=("$evidence"/*.py "$evidence"/run_acceptance.sh "$evidence"/truth.json "$wp05"/*.py "$wp05"/fixtures/*.png "$wp05"/fixtures/*.sha256 "$wp05"/gold.json)
run probe-inputs sha256sum "${probe_inputs[@]}"
sed '/^[0-9a-f]\{64\}  /!d' "$run_path/probe-inputs.log" > "$run_path/probe_inputs.sha256"
run verify-before python3 "$wp05/snapshot.py" verify "$root" "$manifest" "$manifest_sha"
[[ $failure == 0 ]] || exit 1
run extract python3 -m zipfile -e "$archive" "$source"
[[ $failure == 0 ]] || exit 1
if [[ $group == all || $group == pytest ]]; then
    run history-before python3 "$wp05/history_fixture.py" stage "$root" "$source"
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
    run schema-wp05 python3 "$previous_scripts/unchanged_schemas.py" "$legacy/product_sha256.json" "$manifest"
    run product-delta python3 "$delta" --diff "$legacy/product_sha256.json" "$manifest"
    cd "$root" || exit 1
    run git-diff-check git diff --check
fi
if [[ $group == all || $group == runtime ]]; then
    if run wheel-sync env "UV_PROJECT_ENVIRONMENT=$temp/wheel-env" uv sync --project "$source" --python "$python_path" --locked --offline --no-dev --no-install-project &&
        run wheel-build uv build --project "$source" --offline --wheel --out-dir "$temp/wheel"; then
        wheel=("$temp"/wheel/*.whl)
        run wheel-sha sha256sum "${wheel[@]}"
        if run wheel-install uv pip install --offline --no-deps --python "$temp/wheel-env/bin/python" "${wheel[@]}" &&
            run legacy-archive python3 "$evidence/legacy_archive.py" "$legacy" "$legacy_sha" &&
            run legacy-extract python3 -m zipfile -e "$legacy/product.zip" "$temp/legacy" &&
            run legacy-sync env "UV_PROJECT_ENVIRONMENT=$temp/legacy-env" uv sync --project "$temp/legacy" --python "$python_path" --locked --offline --no-dev --no-install-project &&
            run legacy-build uv build --project "$temp/legacy" --offline --wheel --out-dir "$temp/legacy-wheel"; then
            legacy_wheel=("$temp/legacy-wheel"/*.whl)
            run legacy-wheel-sha sha256sum "${legacy_wheel[@]}"
            if run legacy-install uv pip install --offline --no-deps --python "$temp/legacy-env/bin/python" "${legacy_wheel[@]}"; then
                cd "$temp" || exit 1
                runtime=("$temp/wheel-env/bin/python")
                public=(env "PYTHONPATH=$source:$wp05" "${runtime[@]}")
                run installed-origin "${public[@]}" -c 'import jimu_runtime, sys; from pathlib import Path; p=Path(jimu_runtime.__file__).resolve(); print(p); assert p.is_relative_to(Path(sys.prefix).resolve())'
                run replay-runtime env "PYTHONPATH=$source:$wp05" "WP06_LEGACY_PYTHON=$temp/legacy-env/bin/python" "WP06_LEGACY_SOURCE=$temp/legacy:$wp05" "${runtime[@]}" "$evidence/replay_probe.py" "$run_path"
                cd "$root" || exit 1
                run legacy-archive-after python3 "$evidence/legacy_archive.py" "$legacy" "$legacy_sha"
            fi
        fi
    fi
fi
if [[ $group == all || $group == pytest ]]; then
    cd "$source" || exit 1
    run wp06-contracts "$temp/dev/bin/python" -m pytest -q -ra -o "cache_dir=$temp/pytest-wp06" --junitxml "$run_path/wp06.xml" tests/contract/test_wp06_*.py tests/unit/test_wp06_*.py tests/negative/test_wp06_*.py
    run pytest "$temp/dev/bin/python" -m pytest -q -ra -o "cache_dir=$temp/pytest" --junitxml "$run_path/pytest.xml" tests "$scene"
    cd "$root" || exit 1
fi
if [[ $group == all || $group == pytest ]]; then
    run history-after python3 "$wp05/history_fixture.py" verify "$root" "$source"
fi
run probe-inputs-after sha256sum -c "$run_path/probe_inputs.sha256"
run verify-after python3 "$wp05/snapshot.py" verify "$root" "$manifest" "$manifest_sha"
printf '%s\n' "$run_path"
exit "$failure"
