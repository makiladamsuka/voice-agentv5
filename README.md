# Voice Agent V5

Raspberry Pi robot stack: face tracking, servos, TFT eyes, LiveKit voice agent, and kiosk UI.

## How to set up and start the robot

Run these steps after cloning or performing a `git pull`.

---

### Step 1: One-time Environment Setup

1. **Python Virtual Environment & Dependencies:**
   ```bash
   cd voice-agentv5
   python3 -m venv --system-site-packages venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Frontend Dependencies (Next.js):**
   ```bash
   cd frontend
   pnpm install   # or npm install
   cd ..
   ```

3. **Configure Environment Variables (`.env`):**
   Create a `.env` file in the root directory:
   ```ini
   # .env
   LIVEKIT_URL=wss://your-livekit-url
   LIVEKIT_API_KEY=your-api-key
   LIVEKIT_API_SECRET=your-api-secret
   LIVEKIT_AGENT_NAME=campus-greeting-agent

   # Optional Vision/OCR keys for poster indexing
   OPENROUTER_API_KEY=your-openrouter-key
   GROQ_API_KEY=your-groq-key
   ```

---

### Step 2: Optional Pre-Setup & Cache Pre-Building

The system dynamically builds intent caches, amplitude envelopes, and poster metadata when running `start_robot.py`. However, you can run these commands manually beforehand to pre-populate caches and optimize first-run performance:

1. **Pre-build TTS Waveform Sidecars (Lip-Sync & Eye Amplitude Cache):**
   ```bash
   python voice/compiler/build_amplitude_cache.py
   ```
   *Computes `.amp.json` waveform sidecars for cached audio files in `assets/audio_cache/` so screen lip-syncing remains smooth on low-power devices like Raspberry Pi.*

2. **Pre-build Navigation Vector Database & Intents:**
   ```bash
   python voice/compiler/build_navigate_intents.py
   ```
   *Parses campus nodes/graph in `data/` and builds navigation intents & ChromaDB vectors.*

3. **Pre-index Event Poster Metadata (OCR / AI Vision):**
   ```bash
   python -c "from pathlib import Path; from voice.event_indexer import index_posters; index_posters(Path('assets'))"
   ```
   *Scans poster images in `assets/events/`, `assets/competitions/`, and `assets/posts/` and extracts dates/locations via Groq or OpenRouter vision API.*

---

### Step 3: Run the System

Run these in **separate terminals** on the Pi. Order matters: backend first, then frontend, then kiosk browser.

#### Terminal 1 — Backend (robot + voice agent + APIs on :8080)

**Recommended on the Pi** (lower CPU for frontend + Chromium):

```bash
cd voice-agentv5
source venv/bin/activate
CONFIG_PATH=config.kiosk.yaml python start_robot.py start
```

| Command | Use when |
|---------|----------|
| `python start_robot.py start` | **Demo / kiosk** — LiveKit agent dispatch (`devmode` off) |
| `python start_robot.py` | Default — same as `dev` |
| `python start_robot.py dev` | Development — LiveKit dev worker |

Wait until you see `Robot running. Press Ctrl+C to exit.` and the ESP32 connects (or dry-run warning if serial is unplugged).

Optional debug dashboard (3D ToF map + MJPEG on :8082):

```bash
DEBUG_VIZ=1 CONFIG_PATH=config.kiosk.yaml python start_robot.py start
```

Measure backend CPU/RAM:

```bash
./scripts/measure_resources.sh 60 1 start_robot.py
```

---

### Terminal 2 — Frontend (Next.js kiosk UI on :3000)

Requires **Node.js 20+** and `pnpm` (or npm).

```bash
cd /home/nema/Documents/voice-agentv5
./scripts/run-frontend-prod.sh   # production — builds then starts (slow first time on Pi)
```

If the UI code did **not** change since the last build, skip the rebuild:

```bash
cd frontend
pnpm start
```

For UI development with hot reload:

```bash
./scripts/run-frontend-dev.sh
```

Kiosk API routes (`/api/map`, `/api/upload-status`, etc.) proxy to Python on **:8080**. LiveKit tokens are minted in Next.js (`/api/connection-details`).

---

### Terminal 3 — Fullscreen kiosk browser (optional)

Opens Chromium at `http://127.0.0.1:3000` (the Next.js UI, **not** :8080).

```bash
cd /home/nema/Documents/voice-agentv5
./scripts/kiosk.sh
```

After rebuilding the frontend:

```bash
./scripts/refresh-kiosk.sh
```

Kiosk does **not** start on boot by default — run `kiosk.sh` manually when needed.

---

### Verify voice works

1. Backend log should show `Voice agent enabled (LiveKit start)` when the mic is used.
2. Tap the mic on the kiosk UI — backend should log `[VoiceService] Job received: room=...`.
3. `AGENT_NAME` must be `campus-greeting-agent` in `frontend/.env.local` (seeded automatically from `LIVEKIT_AGENT_NAME` in `.env`).

---

## Ports

| Port | Service |
|------|---------|
| 3000 | Next.js kiosk UI |
| 8080 | MediaServer (posters, maps, upload APIs) + voice blackboard hooks |
| 8082 | Debug dashboard + MJPEG `/stream` (`DEBUG_VIZ=1` or `debug_viz.auto_start` in config) |
| 8000 | Bye-wave hand stream (disabled in `config.kiosk.yaml`) |

## Config profiles

| File | Purpose |
|------|---------|
| `config.yaml` | Default / dev tuning |
| `config.kiosk.yaml` | **Pi kiosk** — lower vision FPS/resolution, throttled loops, bye-wave off, debug viz off by default |

**Full breakdown of what kiosk saves vs what still runs (ToF approach, wander, voice throttles):**  
See **[docs/KIOSK-CPU-PROFILE.md](docs/KIOSK-CPU-PROFILE.md)**.

```bash
CONFIG_PATH=config.kiosk.yaml python start_robot.py start
# or
python start_robot.py --config config.kiosk.yaml start
# all-in-one (backend + frontend + Chromium):
./scripts/launch-kiosk-stack.sh
```

## CPU notes

- **Face-only tracking** — YuNet face detection only (no YOLO body detection).
- Use **`config.kiosk.yaml`** when running frontend + Chromium on the same Pi — see [docs/KIOSK-CPU-PROFILE.md](docs/KIOSK-CPU-PROFILE.md).
- Run **production** frontend (`run-frontend-prod.sh` or `pnpm start`) on the Pi, not dev mode, for demos.
- `run-frontend-prod.sh` runs a full `next build` every time; use `cd frontend && pnpm start` when the UI has not changed.

## Project layout

```
voice-agentv5/
├── start_robot.py       # Main entry (hardware, vision, voice, APIs)
├── config.yaml          # Default config
├── config.kiosk.yaml    # Pi kiosk (lower CPU)
├── core/                # Face tracking, servos, eyes, blackboard
├── voice/               # LiveKit agent + MediaServer
├── frontend/            # Next.js kiosk
├── assets/              # Poster images (events, competitions, posts)
├── data/                # Campus map graphs
└── scripts/
    ├── launch-kiosk-stack.sh  # Backend + frontend + Chromium (kiosk config)
    ├── start-ui.sh            # Frontend + kiosk when backend already up
    ├── run-frontend-dev.sh
    ├── run-frontend-prod.sh
    ├── kiosk.sh
    ├── refresh-kiosk.sh
    └── measure_resources.sh
docs/
└── KIOSK-CPU-PROFILE.md       # What kiosk config changes (ToF, wander, voice CPU)
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

Legacy monolith MJPEG: `http://<pi-ip>:8081/stream` (modular stack uses **8082** when debug viz is enabled).
