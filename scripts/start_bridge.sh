#!/usr/bin/env bash
set -eo pipefail

SDK_ROOT="${ASTRIBOT_SDK_ROOT:-/home/astribot/Downloads/astribot_sdk_aarch64}"
BRIDGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${BRIDGE_CONFIG:-${BRIDGE_ROOT}/config/default.yaml}"

if [ ! -f "${SDK_ROOT}/env.sh" ]; then
  echo "[start_bridge] SDK not found: ${SDK_ROOT}" >&2
  exit 1
fi

set +u
# shellcheck disable=SC1091
source "${SDK_ROOT}/env.sh"
set -u

export ASTRIBOT_SDK_ROOT="${SDK_ROOT}"
export BRIDGE_CONFIG="${CONFIG}"
export PYTHONPATH="${BRIDGE_ROOT}:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

HOST="${BRIDGE_HOST:-0.0.0.0}"
PORT="${BRIDGE_PORT:-8080}"
LOG_DIR="${BRIDGE_LOG_DIR:-${BRIDGE_ROOT}/logs}"
RUN_DIR="${BRIDGE_RUN_DIR:-${BRIDGE_ROOT}/run}"

mkdir -p "${LOG_DIR}" "${RUN_DIR}"

if command -v lsof >/dev/null 2>&1; then
  existing_pid="$(lsof -ti tcp:"${PORT}" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  if [ -n "${existing_pid}" ]; then
    echo "[start_bridge] port ${PORT} already in use by pid ${existing_pid}" >&2
    echo "[start_bridge] stop the old bridge before starting a new one" >&2
    exit 1
  fi
fi

cd "${BRIDGE_ROOT}"
exec python3 -m uvicorn bridge.app:app --host "${HOST}" --port "${PORT}" "$@"
