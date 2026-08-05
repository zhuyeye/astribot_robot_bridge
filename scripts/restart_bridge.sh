#!/usr/bin/env bash
set -eo pipefail

BRIDGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STOP_SCRIPT="${BRIDGE_ROOT}/scripts/stop_bridge.sh"
START_SCRIPT="${BRIDGE_ROOT}/scripts/start_bridge.sh"

FORCE_STOP=0
START_ARGS=()

for arg in "$@"; do
  if [ "${arg}" = "--force-stop" ]; then
    FORCE_STOP=1
    continue
  fi
  START_ARGS+=("${arg}")
done

if [ "${FORCE_STOP}" -eq 1 ]; then
  "${STOP_SCRIPT}" --force
else
  "${STOP_SCRIPT}"
fi

exec "${START_SCRIPT}" "${START_ARGS[@]}"
