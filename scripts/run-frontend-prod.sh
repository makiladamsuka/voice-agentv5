#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND="$ROOT/frontend"
ENV_FILE="$ROOT/.env"
LOCAL_ENV="$FRONTEND/.env.local"

if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required. Install Node 20+ first." >&2
  exit 1
fi

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
if [[ "$NODE_MAJOR" -lt 20 ]]; then
  echo "Node 20+ required (found $(node -v))." >&2
  exit 1
fi

if [[ -f "$ENV_FILE" && ! -f "$LOCAL_ENV" ]]; then
  echo "Creating $LOCAL_ENV from $ENV_FILE"
  {
    grep -E '^(LIVEKIT_URL|LIVEKIT_API_KEY|LIVEKIT_API_SECRET|AGENT_NAME)=' "$ENV_FILE" || true
    echo "KIOSK_API_URL=http://127.0.0.1:8080"
  } > "$LOCAL_ENV"
fi

if [[ ! -f "$LOCAL_ENV" ]]; then
  echo "Missing $LOCAL_ENV — copy frontend/.env.local.example and fill in LiveKit creds." >&2
  exit 1
fi

cd "$FRONTEND"
if [[ ! -d node_modules ]]; then
  if command -v pnpm >/dev/null 2>&1; then
    pnpm install
  else
    npm install --legacy-peer-deps
  fi
fi

if command -v pnpm >/dev/null 2>&1; then
  pnpm build
  exec pnpm start
else
  npm run build
  exec npm run start
fi
