# Voice Agent V5 — Raspberry Pi Kiosk Stack

An end-to-end autonomous Raspberry Pi 4 kiosk stack featuring **YuNet face tracking**, **servos & TFT eyes**, **browser VAD + local ChromaDB NLU voice pipeline**, **48kHz audio sync**, and a **Next.js interactive touchscreen kiosk UI**.

---

## 📋 Table of Contents
1. [Fresh Raspberry Pi Setup (From Scratch)](#1-fresh-raspberry-pi-setup-from-scratch)
2. [Hardware & Boot Configuration (`config.txt`)](#2-hardware--boot-configuration-configtxt)
3. [Audio Optimization & USB Sound Card Setup](#3-audio-optimization--usb-sound-card-setup)
4. [Environment & API Keys (`.env`)](#4-environment--api-keys-env)
5. [Poster Management & NLU Intent Database](#5-poster-management--nlu-intent-database)
6. [Frontend Build & Setup](#6-frontend-build--setup)
7. [Starting the System](#7-starting-the-system)
8. [Project Architecture & Ports](#8-project-architecture--ports)
9. [Troubleshooting & Maintenance](#9-troubleshooting--maintenance)

---

## 1. Fresh Raspberry Pi Setup (From Scratch)

If you are setting up a brand-new Raspberry Pi 4 (Raspberry Pi OS 64-bit Bookworm), run the following system package installation first:

### Step 1: Install System Dependencies (Apt)
```bash
sudo apt update && sudo apt install -y \
  git \
  python3-pip \
  python3-venv \
  python3-picamera2 \
  libportaudio2 \
  alsa-utils \
  pipewire \
  pipewire-alsa \
  ffmpeg \
  i2c-tools \
  unclutter \
  mesa-utils
```

### Step 2: Install Node.js 20+ & pnpm
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pnpm
```

---

## 2. Hardware & Boot Configuration (`config.txt` & `cmdline.txt`)

To ensure smooth 64-bit performance, hardware 3D WebGL rendering, GPU memory allocation, and correct 10.1" kiosk display resolution, set up `/boot/firmware/config.txt` and `/boot/firmware/cmdline.txt`.

### Complete `/boot/firmware/config.txt` (Copy & Paste):
```ini
# --- Hardware Interfaces ---
dtparam=i2c_arm=on
dtparam=spi=on
dtparam=i2c_vc=on

# Enables the second SPI bus (SPI1) for pins D21, D20, D19
dtoverlay=spi1-3cs
dtparam=audio=on

# --- Hardware Detection ---
camera_auto_detect=1
auto_initramfs=1

# --- Video Driver & Memory Settings ---
gpu_mem=256
dtoverlay=cma,cma-256
max_framebuffers=2

# Run in 64-bit mode
arm_64bit=1
disable_overscan=1
arm_boost=1

[all]
# ==============================================================================
# DISPLAY PROFILE 1: 10.1 Inch Kiosk Display (1024x600 Custom Timings) [ACTIVE]
# ==============================================================================
display_auto_detect=0
disable_fw_kms_setup=0
dtoverlay=vc4-kms-v3d
max_usb_current=1
hdmi_force_hotplug=1
config_hdmi_boost=7
hdmi_group=2
hdmi_mode=87
hdmi_drive=2
hdmi_force_edid_audio=1
display_rotate=0
hdmi_timings=1024 1 50 18 50 600 1 15 3 15 0 0 0 60 0 40000000 3

# ==============================================================================
# DISPLAY PROFILE 2: Standard External Monitor / TV (Auto-Detect) [INACTIVE]
# ==============================================================================
#display_auto_detect=1
#disable_fw_kms_setup=0
#dtoverlay=vc4-kms-v3d
#hdmi_force_hotplug=1
#config_hdmi_boost=4

# --- ACTIVE OVERCLOCK (2.1 GHz) ---
over_voltage=6
arm_freq=2100
gpu_freq=750

consoleblank=0
```

### Complete `/boot/firmware/cmdline.txt` (Copy & Paste):
```text
console=serial0,115200 console=tty1 root=PARTUUID=e9d8c97a-02 rootfstype=ext4 fsck.repair=yes rootwait quiet splash plymouth.ignore-serial-consoles cfg80211.ieee80211_regdom=LK
```

> ⚠️ **Important**: Ensure `video=HDMI-A-1:...` is **NOT** present in `/boot/firmware/cmdline.txt` so it does not override or fight the 1024x600 display timings set in `config.txt`.
> 
> Reboot after saving both files:
> ```bash
> sudo reboot
> ```

---

## 3. Audio Optimization & USB Sound Card Setup

To prevent Pi CPU micro-stutters during speech playback, the voice pipeline forces **uncompressed 48000Hz WAV audio** across PipeWire, Web Audio, and ALSA.

### Configure ALSA Defaults for USB Sound Card:
If using an external USB Audio Adapter (recommended to bypass CPU-bound PWM audio resampling):
```bash
cd voice-agentv5
bash scripts/setup_alsa.sh
```
This automatically detects your USB Audio Adapter (e.g. `card 3`) and writes `~/.asoundrc` for zero-resampling 48kHz output.

---

## 4. Environment & API Keys (`.env`)

Clone the repository and install Python dependencies:

```bash
git clone https://github.com/makiladamsuka/voice-agentv5.git
cd voice-agentv5

# Create venv with system site-packages (required for picamera2)
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the root project directory:

```bash
cp .env.example .env 2>/dev/null || touch .env
nano .env
```

### Required `.env` Keys:
```ini
DEEPGRAM_API_KEY=your_deepgram_api_key_here
OPENROUTER_API_KEY=your_openrouter_api_key_here
GROQ_API_KEY=your_groq_api_key_here
```

---

## 5. Poster Management & NLU Intent Database

Because poster images, generated vector databases (`voice/event_db/`), and synthesized WAV audio caches (`assets/audio_cache/`) are runtime-generated and ignored by Git, you must compile the intent database when setting up a new Pi.

### How to Add Event Posters:
1. Place poster image files (`.jpg`, `.png`, `.webp`) in the corresponding asset directories:
   - `assets/events/` (Campus events)
   - `assets/competitions/` (Undergraduate hackathons/contests)
   - `assets/posts/` (General announcements)
2. *Or* upload them at runtime via the Admin Upload Portal in the kiosk UI at `http://localhost:3000/upload-portal`.

### How to Build & Manage the Event Vector Database:
Run the intent compiler script:
```bash
python voice/compiler/intent_compiler.py
```

### What `intent_compiler.py` Does:
1. **AI Extraction**: Uses OpenRouter/Groq to parse title, date, time, location, and description from poster images into `voice/event_db/extracted_events.json`.
2. **Utterance Generation**: Generates standard user questions for each event poster into `voice/event_db/compiled_intents.json`.
3. **TTS Speech Synthesis**: Synthesizes 48kHz uncompressed `.wav` audio files into `assets/audio_cache/` so fallback and intent answers play instantly with zero live TTS latency.
4. **ChromaDB Indexing**: Indexes all intents into the local ChromaDB vector database (`voice/event_db/`).

---

## 6. Frontend Build & Setup

Install Node.js packages and build the production Next.js kiosk application:

```bash
cd frontend
pnpm install
pnpm build
cd ..
```

---

## 7. Starting the System

### Option A: All-In-One Kiosk Stack (Recommended for Kiosks)
Runs the Python backend, Next.js frontend, and fullscreen Chromium kiosk browser concurrently in isolated CPU cores:

```bash
./scripts/launch-kiosk-stack.sh
```

### Option B: Manual Multi-Terminal Startup

#### Terminal 1 — Python Robot & NLU Server (:8080 & :8765):
```bash
source venv/bin/activate
CONFIG_PATH=config.kiosk.yaml python start_robot.py
```

#### Terminal 2 — Frontend Kiosk Web Server (:3000):
```bash
cd frontend
pnpm start
```

#### Terminal 3 — Fullscreen Kiosk Browser:
```bash
./scripts/kiosk.sh
```

---

## 8. Project Architecture & Ports

| Port | Service | Description |
|------|---------|-------------|
| **3000** | Next.js Kiosk UI | Touchscreen user interface & Browser VAD mic capture |
| **8765** | NLU WebSocket | Local ChromaDB intent matcher & voice state manager |
| **8080** | Python MediaServer | Serves static assets, poster uploads, map graph APIs |
| **8082** | Debug Dashboard | Optional 3D ToF map + MJPEG camera stream (`DEBUG_VIZ=1`) |

### Directory Overview:
```
voice-agentv5/
├── start_robot.py             # Main entry point (hardware, vision, voice, APIs)
├── config.kiosk.yaml          # Optimized Pi 4 kiosk configuration
├── requirements.txt           # Python package dependencies
├── assets/
│   ├── events/                # Event poster image files
│   ├── competitions/          # Competition poster image files
│   ├── posts/                 # Announcement poster image files
│   └── audio_cache/           # Pre-synthesized 48kHz WAV audio files (Generated)
├── voice/
│   ├── nlu_server.py          # FastAPI/Starlette NLU WebSocket server
│   ├── event_database.py      # ChromaDB event vector database manager
│   ├── event_indexer.py       # AI Vision poster metadata extractor
│   ├── compiler/
│   │   └── intent_compiler.py # Builds compiled_intents.json & synthesizes audio
│   └── event_db/              # Local ChromaDB index & JSON manifests (Generated)
├── frontend/                  # Next.js kiosk touchscreen interface
└── scripts/
    ├── launch-kiosk-stack.sh  # Master launch script (Backend + Frontend + Browser)
    ├── setup_alsa.sh          # USB audio card ALSA setup
    ├── kiosk.sh               # Fullscreen Chromium launcher
    └── refresh-kiosk.sh       # Reload kiosk window after code edits
```

---

## 9. Troubleshooting & Maintenance

### Reload Kiosk UI After Code Edits:
```bash
./scripts/refresh-kiosk.sh
```

### Re-index Posters Manually:
If you add new posters and want to rebuild the vector database immediately:
```bash
python voice/compiler/intent_compiler.py
```

### Measure System Resources (CPU/RAM/Temp):
```bash
./scripts/measure_resources.sh 60 1 start_robot.py
```
