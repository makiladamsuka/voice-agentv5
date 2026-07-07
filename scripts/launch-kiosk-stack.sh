#!/usr/bin/env bash
# Start the full Pi kiosk stack (3 processes):
#   1. Backend  — CONFIG_PATH=config.kiosk.yaml python start_robot.py start  (:8080)
#   2. Frontend — pnpm start only (no build)                               (:3000)
#   3. Chromium — ./scripts/kiosk.sh fullscreen UI                         (/voice)
#
# Usage:
#   ./scripts/launch-kiosk-stack.sh
#
# Env overrides:
#   CONFIG_PATH=config.kiosk.yaml   backend config (default)
#   FRONTEND_PORT=3000
#   KIOSK_URL=http://127.0.0.1:3000/voice
#   SKIP_KIOSK=1                    backend + frontend only
#   PYTHON_BIN=/path/to/python      override python (default: active venv or v4 venv)
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND="$ROOT/frontend"
ENV_FILE="$ROOT/.env"
LOCAL_ENV="$FRONTEND/.env.local"
# Default Pi setup: voice-agentv4 backend venv (has livekit); do not use voice-agentv5/venv.
V4_PYTHON="${HOME}/Documents/voice-agentv4/backend/venv/bin/python"
CONFIG_PATH="${CONFIG_PATH:-config.kiosk.yaml}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
KIOSK_API_PORT="${KIOSK_API_PORT:-8080}"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
BACKEND_PID=""
FRONTEND_PID=""
KIOSK_PID=""

mkdir -p "$LOG_DIR"

_port_listening() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -tln | grep -q ":${port} "
    return $?
  fi
  curl -sf --max-time 2 "http://127.0.0.1:${port}/" >/dev/null 2>&1
}

_wait_for_port() {
  local port="$1" label="$2" max="${3:-180}" pid="${4:-}"
  echo "Waiting for ${label} on :${port}..."
  for _ in $(seq 1 "$max"); do
    if _port_listening "$port"; then
      echo "  ${label} ready on :${port}"
      return 0
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      echo "Error: ${label} process (PID ${pid}) exited before :${port} was ready." >&2
      return 1
    fi
    sleep 1
  done
  echo "Error: ${label} did not start on :${port} within ${max}s." >&2
  return 1
}

_resolve_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    echo "$PYTHON_BIN"
  elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    echo "${VIRTUAL_ENV}/bin/python"
  elif [[ -x "$V4_PYTHON" ]]; then
    echo "$V4_PYTHON"
  else
    command -v python3
  fi
}

_cleanup() {
  echo ""
  echo "Stopping kiosk stack..."
  [[ -n "$KIOSK_PID" ]] && kill "$KIOSK_PID" 2>/dev/null || true
  [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
  [[ -n "$BACKEND_PID" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  local profile="${KIOSK_PROFILE_DIR:-${HOME}/.config/voice-agent-kiosk-chromium}"
  pkill -f "chromium.*--user-data-dir=${profile}" 2>/dev/null || true
  wait 2>/dev/null || true
  echo "Done."
}

trap _cleanup EXIT INT TERM

# ── Preflight ─────────────────────────────────────────────────────────────────
PYTHON="$(_resolve_python)"
echo "Using Python: ${PYTHON}"

if ! "$PYTHON" -c "import livekit" 2>/dev/null; then
  echo "Error: livekit not found for ${PYTHON}" >&2
  echo "  Activate your v4 venv or set PYTHON_BIN=/path/to/python" >&2
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js 20+ required." >&2
  exit 1
fi

if [[ -f "$ENV_FILE" && ! -f "$LOCAL_ENV" ]]; then
  echo "Creating $LOCAL_ENV from $ENV_FILE"
  {
    grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET)=' "$ENV_FILE" || true
    grep -E '^AGENT_NAME=' "$ENV_FILE" || grep -E '^LIVEKIT_AGENT_NAME=' "$ENV_FILE" || echo "AGENT_NAME=campus-greeting-agent"
    echo "KIOSK_API_URL=http://127.0.0.1:${KIOSK_API_PORT}"
  } > "$LOCAL_ENV"
fi

if [[ ! -f "$LOCAL_ENV" ]]; then
  echo "Missing $LOCAL_ENV — copy frontend/.env.local.example first." >&2
  exit 1
fi

if [[ ! -d "$FRONTEND/.next" ]]; then
  echo "No frontend build found (.next/). Run once:" >&2
  echo "  cd frontend && pnpm build" >&2
  exit 1
fi

if _port_listening "$FRONTEND_PORT"; then
  echo "Error: port ${FRONTEND_PORT} already in use (frontend already running?)." >&2
  exit 1
fi

# ── 1. Backend ────────────────────────────────────────────────────────────────
if _port_listening "$KIOSK_API_PORT"; then
  echo "=== Backend already running on :${KIOSK_API_PORT} — skipping ==="
  BACKEND_PID=""
else
  echo "=== Starting backend (config: ${CONFIG_PATH}) ==="
  (
    cd "$ROOT"
    export CONFIG_PATH
    export PYTHONUNBUFFERED=1
    exec "$PYTHON" start_robot.py start
  ) >>"$LOG_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!
  echo "  Backend PID ${BACKEND_PID}  log: ${LOG_DIR}/backend.log"

  _wait_for_port "$KIOSK_API_PORT" "Backend API" 120 "$BACKEND_PID" || {
    echo "Last backend log lines:" >&2
    tail -n 30 "$LOG_DIR/backend.log" >&2 || true
    exit 1
  }
fi

# ── 2. Frontend (pnpm start — no build) ─────────────────────────────────────
echo "=== Starting frontend (pnpm start, no build) ==="
(
  cd "$FRONTEND"
  export PORT="$FRONTEND_PORT"
  if command -v pnpm >/dev/null 2>&1; then
    exec pnpm start
  else
    exec npm run start
  fi
) >>"$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "  Frontend PID ${FRONTEND_PID}  log: ${LOG_DIR}/frontend.log"

_wait_for_port "$FRONTEND_PORT" "Next.js" 90 "$FRONTEND_PID" || {
  echo "Last frontend log lines:" >&2
  tail -n 30 "$LOG_DIR/frontend.log" >&2 || true
  exit 1
}

# ── 3. Kiosk Chromium ─────────────────────────────────────────────────────────
if [[ "${SKIP_KIOSK:-0}" == "1" ]]; then
  echo "SKIP_KIOSK=1 — backend + frontend running. Press Ctrl+C to stop."
  wait "$BACKEND_PID" "$FRONTEND_PID"
  exit 0
fi

echo "=== Starting kiosk Chromium ==="
export FRONTEND_PORT
"$SCRIPT_DIR/kiosk.sh" &
KIOSK_PID=$!

echo ""
echo "Kiosk stack running:"
echo "  Backend  : http://127.0.0.1:${KIOSK_API_PORT}  (PID ${BACKEND_PID})"
echo "  Frontend : http://127.0.0.1:${FRONTEND_PORT}     (PID ${FRONTEND_PID})"
echo "  Kiosk    : ${KIOSK_URL:-http://127.0.0.1:${FRONTEND_PORT}/voice}  (PID ${KIOSK_PID})"
echo "  Logs     : ${LOG_DIR}/"
echo "Press Ctrl+C to stop all."
echo ""

wait "$BACKEND_PID" "$FRONTEND_PID" "$KIOSK_PID" 2>/dev/null || wait
