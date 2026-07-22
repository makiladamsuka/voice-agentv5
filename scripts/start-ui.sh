#!/usr/bin/env bash
# Start frontend + kiosk Chromium when backend is already running on :8080.
# Usage: ./scripts/start-ui.sh
exec "$(dirname "$0")/launch-kiosk-stack.sh"
