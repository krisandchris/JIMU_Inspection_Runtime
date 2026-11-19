#!/usr/bin/env bash
set -u -o pipefail
umask 077

if [[ $# < 2 || $# > 3 ]]; then
    printf 'usage: bash %s PRODUCT_MANIFEST SHA256 [--all|--static|--pytest|--runtime]\n' "$0" >&2
    exit 2
fi
group=${3:---all}
case "$group" in --all|--static|--pytest|--runtime) ;; *) exit 2;; esac
evidence=$(cd -- "$(dirname -- "$0")" && pwd) || exit 2
root=$(cd -- "$evidence/../../../.." && pwd) || exit 2
wp05="$root/planning/implementation_evidence/WP-05/independent"
wp06="$root/planning/implementation_evidence/WP-06/independent"
wp06_manifest="$root/planning/implementation_evidence/WP-06/primary_freeze_bfaf2ccec140/product_sha256.json"
wp06_hash=bfaf2ccec1408421609f7ce2c9a9dbe15987ba27b57ae75f4bf99ea167a154ed
schema_compare="$root/planning/implementation_evidence/WP-04/independent/unchanged_schemas.py"
recovery="$evidence/../recovery/combined_recovery_probe.py"
manifest=$(realpath -e -- "$1") || exit 2
pin=$2
run_path=$(mktemp -d "$evidence/frozen_run_XXXXXXXX") || exit 1
temp=$(mktemp -d -t jimu-wp07-acceptance-XXXXXXXX) || exit 1
source="$temp/product"
dev="$temp/dev"
scene=examples/nonmotor_vehicle_illegal_parking/compilable
python_path=${WP07_PYTHON:-/usr/bin/python3.12}
docker=(docker --host unix:///var/run/docker.sock)
tag="jimu-wp07-local:monitor-${temp##*-}"
pg="postgres@sha256:4b7183ac05f8ef417db21fd72d71047a4238340c261d3cc3ddb6d579ab5071ae"
pybase="python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
prom="quay.io/prometheus/prometheus@sha256:aafa9d694e6a883c02db516d9f6e5b84e14195f49a2a6456a4cca815edda72b9"
grafana="grafana/grafana@sha256:0615ec4fbd4072004b7d86b46ded63013a74b7d6eb1119273dd89e8fa25c13cf"
export UV_PROJECT_ENVIRONMENT="$dev" UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never
export MYPY_CACHE_DIR="$temp/mypy" RUFF_CACHE_DIR="$temp/ruff" PYTHONDONTWRITEBYTECODE=1
unset DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH BUILDKIT_HOST BUILDX_BUILDER
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
export DOCKER_HOST=unix:///var/run/docker.sock COMPOSE_DISABLE_ENV_FILE=true
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
unset PYTHONPATH PYTHONHOME
failure=0
owns_tag=false

cleanup() {
    local status=$?
    trap - EXIT
    if "$owns_tag" && "${docker[@]}" image inspect "$tag" >/dev/null 2>&1; then
        "${docker[@]}" image rm "$tag" >"$run_path/cleanup-monitor.log" 2>&1 || status=1
    fi
    rm -r -- "$temp" || status=1
    printf 'group=%s\nmanifest_sha256=%s\nexit=%s\n' "$group" "$pin" "$status" >"$run_path/run-status.txt"
    (
        cd "$run_path" || exit 1
        find . -type f ! -name run_outputs.sha256 -print0 |
            sort -z | xargs -0 -r sha256sum
    ) >"$run_path/run_outputs.sha256" || status=1
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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

printf '%s\n' "$run_path"
run extract "$python_path" "$evidence/frozen_archive.py" "$manifest" "$pin" --extract "$source" || exit 1
probe_inputs=(
    "$evidence/frozen_archive.py" "$evidence/run_acceptance.sh"
    "$wp05/history_fixture.py" "$schema_compare" "$wp06_manifest"
)
if [[ $group == --all || $group == --runtime ]]; then
    probe_inputs+=(
        "$evidence/monitor_smoke.py" "$recovery"
        "$wp05"/*.py "$wp05"/fixtures/*.png "$wp05"/fixtures/*.sha256 "$wp05"/gold.json
        "$wp06"/*.py "$wp06"/truth.json
    )
    printf '%s  %s\n' f992fc907a7edf311540a9350232ed5c0306dfec4c9cfde95c34bfabc9db4700 "$evidence/monitor_smoke.py" > "$run_path/monitor_source.sha256"
    run monitor-source-seal sha256sum -c "$run_path/monitor_source.sha256" || exit 1
fi
sha256sum "${probe_inputs[@]}" >"$run_path/probe_inputs.sha256" || exit 1
printf '%s  %s\n' "$wp06_hash" "$wp06_manifest" > "$run_path/prior_manifest.sha256"
run baseline-seal sha256sum -c "$run_path/prior_manifest.sha256" || exit 1
run history-stage "$python_path" "$wp05/history_fixture.py" stage "$root" "$source" || exit 1
run uv-version uv --version
run interpreter "$python_path" -VV
run dependency-lock sha256sum "$source/pyproject.toml" "$source/uv.lock"
run locked-sync uv sync --project "$source" --python "$python_path" --locked --offline || exit 1
run versions "$dev/bin/python" -c 'import importlib.metadata as m, platform; print(platform.platform()); print({p:m.version(p) for p in ("jimu-runtime","pydantic","rfc8785","jsonschema","sqlalchemy","psycopg","psycopg-binary","httpx","pillow","pytest","ruff","mypy","fastapi","starlette","uvicorn")})'

if [[ $group != --static ]]; then
    run docker-version "${docker[@]}" version || exit 1
    run compose-version "${docker[@]}" compose version || exit 1
    run product-image-pins "${docker[@]}" image inspect --format '{{.Id}} {{.Architecture}} {{json .RepoDigests}}' "$pg" "$pybase" || exit 1
    if [[ $group == --all || $group == --runtime ]]; then
        run monitor-image-pins "${docker[@]}" image inspect --format '{{.Id}} {{.Architecture}} {{json .RepoDigests}}' "$prom" "$grafana" || exit 1
    fi
fi

cd "$source" || exit 1
if [[ $group == --all || $group == --static ]]; then
    run ruff "$dev/bin/ruff" check src tests scripts examples ops
    run ruff-format "$dev/bin/ruff" format --check src tests scripts examples ops
    run mypy env "MYPYPATH=$source/src:$source:$source/ops/compose" "$dev/bin/mypy" --explicit-package-bases --strict src/jimu_runtime scripts/generate_schemas.py "$scene/lifecycle.py" "$scene/local_trust.py" "$scene/scene.py" "$scene/__main__.py" ops/recovery ops/compose/synthetic_host.py ops/compose/synthetic_provider.py
    run schema-check "$dev/bin/python" scripts/generate_schemas.py --check
    run schema-first "$dev/bin/python" scripts/generate_schemas.py --output "$temp/schema-first"
    run schema-second "$dev/bin/python" scripts/generate_schemas.py --output "$temp/schema-second"
    run schema-repeat diff --recursive --brief "$temp/schema-first" "$temp/schema-second"
    run schema-frozen-tree diff --recursive --brief schemas/generated "$temp/schema-first"
    run schema-347 "$python_path" "$schema_compare" "$wp06_manifest" "$manifest"
    run observability-schema bash -o pipefail -c '"$1" -m jimu_runtime.observability.schema | cmp - "$2"' _ "$dev/bin/python" ops/observability/schema.json
fi

if [[ $group == --all || $group == --pytest ]]; then
    # The product's PG fixtures bind their own unique containers and loopback,
    # and its Compose contract creates/removes its own uniquely named project.
    run pytest env JIMU_RUN_COMPOSE_TEST=1 "$dev/bin/python" -m pytest -q -ra -o "cache_dir=$temp/pytest" --junitxml "$run_path/pytest.xml" tests "$scene"
fi

if [[ $group == --all || $group == --runtime ]]; then
    if ! "${docker[@]}" image inspect "$tag" >/dev/null 2>&1; then
        owns_tag=true
        if run monitor-build ops/compose/build.sh "$tag"; then
            mkdir -- "$temp/monitor"
            run monitor-runtime-image "${docker[@]}" image inspect --format '{{.Id}} {{.Architecture}}' "$tag"
            run monitoring env PYTHONPATH="$source" JIMU_CAPTURE_DIR="$temp/monitor" JIMU_CAPTURE_RUNTIME="$tag" JIMU_CAPTURE_PROM="$prom" JIMU_CAPTURE_GRAFANA="$grafana" "$dev/bin/python" "$evidence/monitor_smoke.py"
        fi
    else
        printf 'Refusing existing independent monitor tag %s\n' "$tag" >&2
        failure=1
    fi

    wheel_env="$temp/wheel-env"
    if run wheel-sync env "UV_PROJECT_ENVIRONMENT=$wheel_env" uv sync --project "$source" --python "$python_path" --locked --offline --no-install-project &&
        run wheel-build uv build --project "$source" --offline --wheel --out-dir "$temp/wheel"; then
        wheel=("$temp"/wheel/*.whl)
        run wheel-sha sha256sum "${wheel[@]}"
        if run wheel-install uv pip install --offline --no-deps --python "$wheel_env/bin/python" "${wheel[@]}"; then
            public=(env "PYTHONPATH=$source:$wp05:$wp06" "$wheel_env/bin/python")
            run installed-origin "${public[@]}" -c 'import jimu_runtime, sys; from pathlib import Path; p=Path(jimu_runtime.__file__).resolve(); print(p); assert p.is_relative_to(Path(sys.prefix).resolve())'
            mkdir -- "$run_path/combined"
            run combined-recovery "${public[@]}" "$recovery" "$run_path/combined"
        fi
    fi
fi

cd "$root" || exit 1
run history-verify "$python_path" "$wp05/history_fixture.py" verify "$root" "$source"
run probe-inputs-after sha256sum -c "$run_path/probe_inputs.sha256"
run verify-after "$python_path" "$evidence/frozen_archive.py" "$manifest" "$pin" --compare-tree "$source"
printf '%s\n' "$run_path"
exit "$failure"
