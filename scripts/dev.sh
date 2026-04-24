#!/usr/bin/env bash
# scripts/dev.sh — start the backend (API + scheduler) and the Vite dev
# server together with one command. Output from each process is prefixed
# with [api] / [ui]; Ctrl+C stops both cleanly, including uvicorn's
# --reload child and any npm/vite subprocesses.
#
# Usage:
#   ./scripts/dev.sh
#   BACKEND_PORT=8001 FRONTEND_PORT=5174 ./scripts/dev.sh

set -euo pipefail
set -m  # job control: each backgrounded subshell gets its own process group

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)"
cd "$repo_root"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

if [ ! -d "frontend/node_modules" ]; then
  echo "[dev] installing frontend dependencies..."
  (cd frontend && npm install)
fi

prefix() {
  local label="$1" color="$2"
  awk -v tag="$(printf '\033[%sm[%s]\033[0m ' "$color" "$label")" \
    '{ print tag $0; fflush(); }'
}

child_pgids=()

cleanup() {
  trap - INT TERM EXIT
  echo ""
  echo "[dev] shutting down..."
  for pgid in "${child_pgids[@]:-}"; do
    kill -TERM -- "-$pgid" 2>/dev/null || true
  done
  sleep 0.3
  for pgid in "${child_pgids[@]:-}"; do
    kill -KILL -- "-$pgid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "[dev] backend  -> http://127.0.0.1:${BACKEND_PORT}"
echo "[dev] frontend -> http://127.0.0.1:${FRONTEND_PORT}  (proxies /api -> :${BACKEND_PORT})"
echo "[dev] open the frontend URL; Ctrl+C stops both"
echo ""

(
  exec uv run uvicorn autosdr.webhook:app \
    --reload --host 127.0.0.1 --port "$BACKEND_PORT" 2>&1 \
    | prefix "api" "36"
) &
child_pgids+=("$!")

(
  cd frontend
  exec npm run dev -- --port "$FRONTEND_PORT" --strictPort 2>&1 \
    | prefix "ui" "35"
) &
child_pgids+=("$!")

if wait -n 2>/dev/null; then
  : # one side exited; trap will tear the rest down
else
  wait
fi
