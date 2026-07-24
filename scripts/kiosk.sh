#!/usr/bin/env bash
# Fullscreen Chromium for the Next.js kiosk UI (NOT the Python API on :8080).
#
# Ports (voice-agentv5):
#   3000  Next.js kiosk UI  <- this script opens this
#   8080  Python MediaServer (start_robot.py) — proxied as /api/* from the UI
#   8082  Debug dashboard + MJPEG (optional, DEBUG_VIZ=1)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"
KIOSK_API_PORT="${KIOSK_API_PORT:-8080}"
URL="${KIOSK_URL:-http://127.0.0.1:${FRONTEND_PORT}/}"
CHROMIUM="${CHROMIUM:-chromium-browser}"
PROFILE_DIR="${KIOSK_PROFILE_DIR:-${HOME}/.config/voice-agent-kiosk-chromium}"
LOG_DIR="${LOG_DIR:-${ROOT}/logs}"
CHROMIUM_LOG="${CHROMIUM_LOG:-${LOG_DIR}/chromium.log}"
MAX_WAIT=180

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

_kiosk_running() {
  pgrep -f "chromium.*--user-data-dir=${PROFILE_DIR}" >/dev/null 2>&1
}

_port_listening() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -tln | grep -q ":${port} "
    return $?
  fi
  curl -sf --max-time 2 "http://127.0.0.1:${port}/" >/dev/null 2>&1
}

if _kiosk_running; then
  echo "Kiosk Chromium already running (profile: ${PROFILE_DIR})"
  echo "  UI: ${URL}"
  echo "  To reload after a frontend rebuild: ./scripts/refresh-kiosk.sh"
  exit 0
fi

echo "Waiting for Next.js on port ${FRONTEND_PORT}..."
ready=0
for _ in $(seq 1 "$MAX_WAIT"); do
  if _port_listening "$FRONTEND_PORT"; then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  echo "Error: nothing listening on port ${FRONTEND_PORT}." >&2
  echo "Start the frontend first: ./scripts/run-frontend-prod.sh" >&2
  exit 1
fi

if ! _port_listening "$KIOSK_API_PORT"; then
  echo "Warning: Python kiosk API not detected on :${KIOSK_API_PORT}." >&2
  echo "  Start backend: CONFIG_PATH=config.kiosk.yaml python start_robot.py" >&2
  echo "  (Map/posters/upload will fail until :8080 is up.)" >&2
fi

mkdir -p "$PROFILE_DIR" "$LOG_DIR"

if command -v unclutter >/dev/null 2>&1; then
  pkill -x unclutter 2>/dev/null || true
  unclutter -idle 0 -root &
fi

amixer -q sset Master 100% unmute 2>/dev/null || true
amixer -q sset PCM 100% unmute 2>/dev/null || true
amixer -q sset Headphone 100% unmute 2>/dev/null || true
amixer -q sset Capture 100% unmute 2>/dev/null || true
amixer -q sset Mic 100% unmute 2>/dev/null || true

echo "Opening kiosk UI: ${URL}"
echo "  (API backend :${KIOSK_API_PORT} — not opened in browser)"
echo "  Chromium log: ${CHROMIUM_LOG} (stderr suppressed in this terminal)"

exec "$CHROMIUM" \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-translate \
  --no-first-run \
  --check-for-update-interval=31536000 \
  --disable-smooth-scrolling \
  --enable-webgl \
  --ignore-gpu-blocklist \
  --enable-gpu-rasterization \
  --renderer-process-limit=1 \
  --num-raster-threads=2 \
  --autoplay-policy=no-user-gesture-required \
  --disable-background-networking \
  --disable-sync \
  --disable-default-apps \
  --disable-component-update \
  --enable-low-end-device-mode \
  --use-gl=egl \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-features=TranslateUI,MediaRouter,BackdropFilter \
  --enable-features=LowEndDeviceMode \
  --use-fake-ui-for-media-stream \
  --log-level=3 \
  --disable-logging \
  --user-data-dir="$PROFILE_DIR" \
  "$URL" \
  >>"$CHROMIUM_LOG" 2>&1
