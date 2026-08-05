#!/usr/bin/env bash
set -eo pipefail

BRIDGE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${BRIDGE_PORT:-8080}"
FORCE=0

if [ "${1:-}" = "--force" ]; then
  FORCE=1
fi

find_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti tcp:"${PORT}" -sTCP:LISTEN 2>/dev/null || true
    return
  fi
  if command -v pgrep >/dev/null 2>&1; then
    pgrep -f "uvicorn bridge.app:app --host .* --port ${PORT}" || true
    return
  fi
}

mapfile -t PIDS < <(find_pids)

if [ "${#PIDS[@]}" -eq 0 ]; then
  echo "[stop_bridge] no bridge process found on port ${PORT}"
  exit 0
fi

echo "[stop_bridge] stopping bridge on port ${PORT}: ${PIDS[*]}"
for pid in "${PIDS[@]}"; do
  kill -TERM "${pid}" 2>/dev/null || true
done

for _ in $(seq 1 20); do
  alive=0
  for pid in "${PIDS[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      alive=1
      break
    fi
  done
  if [ "${alive}" -eq 0 ]; then
    echo "[stop_bridge] stopped cleanly"
    exit 0
  fi
  sleep 0.5
done

if [ "${FORCE}" -eq 1 ]; then
  echo "[stop_bridge] forcing stop for remaining processes: ${PIDS[*]}"
  for pid in "${PIDS[@]}"; do
    kill -KILL "${pid}" 2>/dev/null || true
  done
  exit 0
fi

echo "[stop_bridge] process still alive; re-run with --force if needed" >&2
exit 1
