#!/bin/sh
# Assemble an offline image from the reviewed runtime lock and local base image.
set -eu
case "${1:-}" in
  jimu-wp07-local:*) ;;
  *) echo 'Usage: build.sh jimu-wp07-local:UNIQUE_TAG' >&2; exit 2 ;;
esac
if [ "$#" -ne 1 ]; then exit 2; fi
project=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
cd "$project"
docker="$project/ops/compose/local-docker.sh"
if [ "$(uname -sm)" != 'Linux x86_64' ]; then
    echo 'The offline Python build requires a Linux x86-64 host.' >&2
    exit 2
fi
base=python@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
base_id=$("$docker" image inspect "$base" --format '{{.Id}}')
if "$docker" image inspect "$1" >/dev/null 2>&1; then
    echo 'Use an unused application image tag.' >&2
    exit 2
fi
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT HUP INT TERM
mkdir -p "$context/wheel" "$context/host/examples/nonmotor_vehicle_illegal_parking"
UV_PROJECT_ENVIRONMENT="$context/venv" uv sync --locked --offline --no-build --no-dev \
    --no-install-project --python 3.12 --python-platform x86_64-manylinux_2_28
mkdir "$context/vendor"
cp -R "$context"/venv/lib/python3.12/site-packages/. "$context/vendor/"
rm -rf "$context/venv"
uv build --offline --wheel --out-dir "$context/wheel"
cp ops/compose/Dockerfile "$context/"
cp ops/compose/synthetic_host.py ops/compose/synthetic_provider.py "$context/host/"
(
    cd examples/nonmotor_vehicle_illegal_parking
    tar --exclude='__pycache__' --exclude='test_scene.py' -cf "$context/scene.tar" compilable
)
tar -C "$context/host/examples/nonmotor_vehicle_illegal_parking" -xf "$context/scene.tar"
rm "$context/scene.tar"
test "$("$docker" image inspect "$base_id" --format '{{.Os}}/{{.Architecture}}')" = linux/amd64
DOCKER_BUILDKIT=0 "$docker" build --network=none --pull=false --build-arg "PYTHON_IMAGE=$base_id" -t "$1" "$context"
"$docker" image inspect "$1" --format '{{.Id}}'
