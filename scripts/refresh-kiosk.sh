#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${KIOSK_URL:-http://localhost:3000}"

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

pkill -f "chromium.*--kiosk.*${URL}" 2>/dev/null || true
sleep 1

exec "${SCRIPT_DIR}/kiosk.sh"
