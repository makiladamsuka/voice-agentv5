#!/usr/bin/env bash
set -euo pipefail

URL="${KIOSK_URL:-http://localhost:3000}"
CHROMIUM="${CHROMIUM:-chromium-browser}"
PROFILE_DIR="${KIOSK_PROFILE_DIR:-${HOME}/.config/voice-agent-kiosk-chromium}"
MAX_WAIT=180

export DISPLAY="${DISPLAY:-:0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

if pgrep -f "chromium.*--kiosk.*${URL}" >/dev/null 2>&1; then
  exit 0
fi

for _ in $(seq 1 "$MAX_WAIT"); do
  if curl -sf --max-time 2 "$URL" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

mkdir -p "$PROFILE_DIR"

# Hide mouse cursor on the touchscreen kiosk.
if command -v unclutter >/dev/null 2>&1; then
  pkill -x unclutter 2>/dev/null || true
  unclutter -idle 0 -root &
fi

# Maximize playback volume for agent TTS on the kiosk speakers.
amixer -q sset Master 100% unmute 2>/dev/null || true
amixer -q sset PCM 100% unmute 2>/dev/null || true
amixer -q sset Headphone 100% unmute 2>/dev/null || true

exec "$CHROMIUM" \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-translate \
  --no-first-run \
  --check-for-update-interval=31536000 \
  --disable-smooth-scrolling \
  --disable-background-timer-throttling \
  --disable-renderer-backgrounding \
  --autoplay-policy=no-user-gesture-required \
  --user-data-dir="$PROFILE_DIR" \
  "$URL"
