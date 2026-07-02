# Voice Agent V5

Raspberry Pi robot stack: face tracking, servos, TFT eyes, LiveKit voice agent, and kiosk UI.

## Quick start (Pi)

### 1. Python backend (robot + voice + kiosk APIs on :8080)

```bash
cd /home/nema/Documents/voice-agentv5
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Copy .env with LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
python start_robot.py
```

### 2. Frontend (native Next.js kiosk on :3000 — no Docker)

Requires **Node.js 20+** and `pnpm` (or npm).

```bash
./scripts/run-frontend-dev.sh    # hot reload while editing UI
# or
./scripts/run-frontend-prod.sh   # production build for stable demo
```

On first run, `run-frontend-dev.sh` seeds `frontend/.env.local` from the repo `.env`.

Kiosk API routes (`/api/map`, `/api/upload-status`, etc.) are proxied to the Python MediaServer on **:8080** via `next.config.ts` rewrites. LiveKit token minting stays in Next.js (`/api/connection-details`).

### 3. Fullscreen touchscreen kiosk

```bash
./scripts/kiosk.sh
```

Opens Chromium fullscreen at `http://localhost:3000` (cursor hidden, speaker volume maxed).

## Ports

| Port | Service |
|------|---------|
| 3000 | Next.js kiosk UI |
| 8080 | MediaServer (posters, maps, upload APIs) |
| 8081 | MJPEG camera stream |
| 8082 | Robot debug dashboard |

## Project layout

```
voice-agentv5/
├── start_robot.py       # Main entry
├── config.yaml
├── core/                # Face tracking, servos, eyes, blackboard
├── voice/               # LiveKit agent + MediaServer
├── frontend/            # Next.js kiosk (copied from voice-agentv2)
├── assets/              # Poster images (events, competitions, posts)
├── data/                # Campus map graphs
└── scripts/
    ├── run-frontend-dev.sh
    ├── run-frontend-prod.sh
    ├── kiosk.sh
    └── refresh-kiosk.sh
```

## ESP32 firmware

Wiring defaults:

- ESP32 GPIO 21 -> PCA9685 SDA
- ESP32 GPIO 22 -> PCA9685 SCL
- PCA9685 address `0x40`
- PCA9685 channel 4 -> pan servo
- PCA9685 channel 5 -> tilt servo

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/head_servo
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32 firmware/head_servo
```

Serial protocol: `H` -> `READY`, `P85.0 T105.0` -> set pan/tilt, `?` -> `POS P<pan> T<tilt>`

## Legacy face-tracking head (standalone)

```bash
python face_tracking_head.py --port /dev/ttyUSB0
```

MJPEG preview: `http://<pi-ip>:8081/stream`
