#!/bin/sh
# Ignore user-selected SSH/TCP contexts before making any Docker request.
set -eu
socket=/var/run/docker.sock
if ! test -S "$socket"; then
    echo "The local Docker Unix socket is unavailable." >&2
    exit 2
fi
unset DOCKER_CONTEXT DOCKER_HOST DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH
unset BUILDKIT_HOST BUILDX_BUILDER
exec docker --host "unix://$socket" "$@"
