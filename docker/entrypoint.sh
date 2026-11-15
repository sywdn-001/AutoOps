#!/bin/sh
# AutoOps Bastion - container entrypoint.
# Reads its knobs from the environment (see bastion-backend/.env.example).
set -e

PORT="${BASTION_PORT:-5000}"
GATEWAY_PORT="${BASTION_GATEWAY_PORT:-2222}"
INSTANCE_DIR="${BASTION_INSTANCE_DIR:-/app/data/instance}"

mkdir -p "$INSTANCE_DIR"

echo "------------------------------------------------------------"
echo "  AutoOps Bastion (Linux container)"
echo "  web UI / API : http://0.0.0.0:$PORT"
echo "  health check : http://127.0.0.1:$PORT/api/health"
if [ "${BASTION_CONTAINER_NO_GATEWAY:-0}" = "1" ]; then
    echo "  SSH gateway  : disabled (BASTION_CONTAINER_NO_GATEWAY=1)"
else
    echo "  SSH gateway  : 0.0.0.0:$GATEWAY_PORT"
fi
echo "  data dir     : $INSTANCE_DIR"
echo "  python       : $(python --version 2>&1)"
echo "------------------------------------------------------------"

set -- run.py --host 0.0.0.0 --port "$PORT"

if [ "${BASTION_CONTAINER_NO_GATEWAY:-0}" = "1" ]; then
    set -- "$@" --no-gateway
else
    set -- "$@" --gateway-host 0.0.0.0 --gateway-port "$GATEWAY_PORT"
fi

if [ -n "${BASTION_LOG_LEVEL:-}" ]; then
    set -- "$@" --log-level "$BASTION_LOG_LEVEL"
fi

cd /app/bastion-backend
exec python "$@"