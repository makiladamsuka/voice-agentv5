#!/usr/bin/env bash
# Kill kiosk Chromium and reopen the Next.js UI on :3000 (after frontend rebuild).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
URL="${KIOSK_URL:-http://127.0.0.1:${FRONTEND_PORT}}"
PROFILE_DIR="${KIOSK_PROFILE_DIR:-${HOME}/.config/voice-agent-kiosk-chromium}"

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "Stopping kiosk Chromium (profile: ${PROFILE_DIR})..."
pkill -f "chromium.*--user-data-dir=${PROFILE_DIR}" 2>/dev/null || true
sleep 2

exec "${SCRIPT_DIR}/kiosk.sh"
