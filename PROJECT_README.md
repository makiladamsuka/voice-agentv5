# 🤖 NEma — Voice Agent V5

**An AI-Powered Interactive Campus Robot with Voice Navigation, Face Tracking, and a 3D Isometric Map Kiosk**

NEma is a physically embodied AI assistant built on a Raspberry Pi 4. It combines real-time face tracking, motorised head and arm servos, Time-of-Flight (ToF) proximity sensing, an animated TFT eye display, and a full-stack voice-powered kiosk UI — all working together to greet visitors, answer questions, show campus events, and guide them to any room in a multi-floor university building using an interactive 3D isometric map.

---

## 📑 Table of Contents

1. [Project Overview](#project-overview)
2. [System Architecture](#system-architecture)
3. [Hardware Components](#hardware-components)
4. [Backend — Python Robot Stack](#backend--python-robot-stack)
   - [Main Entry Point](#main-entry-point-start_robotpy)
   - [Core Services](#core-services-core)
   - [Voice & NLU Pipeline](#voice--nlu-pipeline-voice)
   - [Wayfinding Engine](#wayfinding-engine)
   - [Hardware Drivers](#hardware-drivers-hardware)
   - [Configuration](#configuration-system)
5. [Frontend — Next.js Kiosk UI](#frontend--nextjs-kiosk-ui)
   - [Kiosk View](#kiosk-view-kioskviewtsx)
   - [3D Isometric Map](#3d-isometric-navigation-map-isometric-maptsx)
   - [API Routes](#api-routes)
   - [Hooks](#hooks)
6. [Map Data Format](#map-data-format-data)
7. [ESP32 Firmware](#esp32-firmware-firmware)
8. [Folder Structure](#complete-folder-structure)
9. [How to Run](#how-to-run)
10. [Technology Stack](#technology-stack)

---

## Project Overview

NEma is a **campus information kiosk robot** deployed in a university faculty building. It serves three primary purposes:

| Feature | Description |
|---------|-------------|
| **Voice Assistant** | Users speak to NEma naturally. It understands questions about campus events, room locations, and general information, then responds with synthesised speech. |
| **Indoor Navigation** | When asked "Where is Laboratory 5?", NEma computes the shortest path using Dijkstra's algorithm across a multi-floor graph and displays an animated 3D isometric map with a glowing route. |
| **Event Display** | NEma shows campus event posters (uploaded by staff via a QR-code portal), and can answer questions about each event using AI-extracted metadata. |

On the physical side, the robot tracks people with its camera-driven servo head, waves hello with articulated arms, and expresses emotions through animated TFT-display eyes.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER (Visitor)                               │
│                    Speaks / Taps Touchscreen                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │   Next.js Kiosk UI (:3000)      │
          │   Browser VAD → Deepgram STT    │
          │   3D Isometric Map (Three.js)   │
          │   Event Carousel / Poster View  │
          └────────────┬────────────────────┘
                       │ WebSocket (ws://localhost:8765)
          ┌────────────▼────────────────┐
          │  Python NLU Server (:8765)  │
          │  ChromaDB Intent Matching   │
          │  Deepgram TTS (cached)      │
          │  Wayfinder Pathfinding      │
          └────────────┬────────────────┘
                       │ Blackboard (shared state)
          ┌────────────▼────────────────┐
          │  Robot Core Services        │
          │  Face Tracking (YuNet)      │
          │  Servo Loop (PID)           │
          │  Emotion Engine             │
          │  Eye Renderer (TFT)         │
          │  Arm Controller             │
          │  Base Controller (spin)     │
          │  ToF Proximity              │
          └────────────┬────────────────┘
                       │ Serial (USB)
          ┌────────────▼────────────────┐
          │  ESP32 Microcontroller      │
          │  PCA9685 Servo Driver       │
          │  ToF Sensor Array (VL53L1X) │
          │  DC Motor + Encoder (Base)  │
          └─────────────────────────────┘
```

---

## Hardware Components

| Component | Role |
|-----------|------|
| **Raspberry Pi 4** | Main computer running Python backend + Node.js frontend |
| **ESP32** | Real-time servo control, motor drive, and ToF sensor streaming via serial |
| **PCA9685** | 16-channel PWM servo driver (I²C) for pan/tilt head and arm servos |
| **Camera (Pi Camera 2)** | Face detection input for tracking |
| **ST7735 TFT Display** | Animated robot eyes with blink, emotion, and color themes |
| **BMI160 IMU** | Head-mounted inertial sensor for horizon stabilisation and yaw fusion |
| **VL53L1X ToF Sensors** | 3-channel Time-of-Flight proximity array (left/center/right) for approach detection |
| **DC Motor + Encoder** | Rotating base plate for 360° body orientation |
| **4× Servos (Arms)** | Two 2-DOF arms for wave greetings and talk gestures |
| **Touchscreen** | Kiosk display for the Next.js UI |

---

## Backend — Python Robot Stack

### Main Entry Point: `start_robot.py`

This is the **single entry point** for the entire robot. It:

1. Loads the YAML configuration file (`config.yaml` or `config.kiosk.yaml`)
2. Connects to the ESP32 over serial (USB)
3. Calibrates the IMU (gyro/accelerometer zero)
4. Homes the arm servos to their resting position
5. Locks the yaw reference (encoder + IMU fusion)
6. Spawns **12+ daemon threads**, each running an independent service
7. Starts the NLU voice server and media server
8. Handles graceful shutdown (homing servos and base before exit)

### Core Services (`core/`)

Each service runs in its own thread, communicating through the **Blackboard** (a thread-safe shared state store).

| File | Service | What It Does |
|------|---------|--------------|
| `blackboard.py` | **Blackboard** | Thread-safe key-value store. Every service reads/writes state here (face position, servo angles, emotion, speech state, etc.). This is the central nervous system of the robot. |
| `face_tracking.py` | **FaceTracker** | Captures camera frames, runs YuNet face detection (ONNX), computes normalised face position (`norm_x`, `norm_y`), and writes results to the Blackboard. Supports multi-face tracking and person memory. |
| `servo_loop.py` | **ServoLoop** | PID control loop (80 Hz) that reads face position from the Blackboard and computes pan/tilt servo commands. Handles tracking, wandering (idle glances), search patterns, and smooth homing. |
| `servo_mixer.py` | **ServoMixer** | The final gatekeeper before serial writes. Merges servo commands from the ServoLoop, base controller, and arm controller. Manages yaw-home tracking, encoder fusion, and ToF proximity-based base turns. |
| `base_controller.py` | **BaseController** | Controls the rotating base plate. Implements head-lead-follow (base turns to follow head direction), wander random turns, proximity-triggered orientation, and return-to-home via IMU closed-loop control. |
| `arm_controller.py` | **ArmController** | Controls 4 arm servos (2 per arm). Implements lean/sway idle movements and coordinates with face greeting and talk gesture services. |
| `emotion_engine.py` | **EmotionEngine** | Maps face tracking events and voice sentiment to emotion states (happy, curious, surprised, sad, attentive). Drives eye expressions and servo behavior modifiers. |
| `eye_renderer.py` | **EyeRenderer** | Renders animated eyes on the ST7735 TFT display at 40 FPS. Supports blinking, squinting, widening, color themes, and pupil tracking that follows the detected face. |
| `animation_engine.py` | **AnimationEngine** | Plays back pre-recorded synchronized hardware timelines (like Pepper-style animations) — coordinated servo movements, eye changes, and base rotations. |
| `face_greeting.py` | **FaceGreetingMonitor** | Detects when a new face appears close enough and triggers a greeting animation (eye widening + optional voice). |
| `face_greeting_arm.py` | **FaceGreetingArmService** | Extends face greeting with arm wave gestures. Uses face embeddings to remember visitors and avoid re-greeting within 30 minutes. |
| `bye_wave_service.py` | **ByeWaveService** | Detects hand wave gestures (via MediaPipe) and responds with a physical arm wave goodbye animation. |
| `talk_gesture_service.py` | **TalkGestureService** | While the robot is speaking, smoothly moves arms through natural gesture poses to make speech feel more lifelike. |
| `speech_sync_service.py` | **SpeechSyncService** | Synchronises robot emotion/gesture timing with audio playback by tracking utterance start/end and amplitude envelopes. |
| `imu_service.py` | **ImuService** | Reads BMI160 IMU data (100 Hz), computes roll/pitch/yaw, provides horizon stabilisation for the head tilt servo, and yaw drift correction for base navigation. |
| `tof_state.py` | **TOF_STATE** | Manages the 3D occupancy map built from ToF sensor sweeps. Tracks nearby objects as the base rotates, fuses readings into persistent obstacle/person tracks. |
| `tof_stream.py` | **TofStreamHandler** | Parses raw ToF distance packets from the ESP32 serial stream and feeds them into the ToF state machine. |
| `debug_dashboard.py` | **DebugDashboard** | Serves a web-based debug visualisation (port 8082) showing a real-time 3D ToF proximity map, camera MJPEG stream, servo/IMU diagnostics, and manual WASD control panel. |
| `surroundings_emotion.py` | **SurroundingsEmotion** | Maps environmental context (person nearby, person leaving, empty room) to ambient emotion states that influence eye expressions when no one is talking. |
| `yaw_pose.py` | **Yaw Pose Utilities** | Functions for encoder/IMU yaw fusion, home-reference locking, and publishing the robot's world-frame orientation to the ToF visualiser. |

### Voice & NLU Pipeline (`voice/`)

The voice system uses a **Browser VAD → Deepgram STT → ChromaDB NLU → Deepgram TTS** pipeline, all running locally without cloud AI inference:

| File | What It Does |
|------|--------------|
| `nlu_server.py` | **NLU WebSocket Server** (port 8765). Receives transcripts from the browser, runs intent matching, generates responses, and sends back audio URLs + UI actions (navigate, show poster, etc.). Built on Starlette/Uvicorn ASGI. |
| `offline_voice/runtime.py` | **OfflineVoiceRuntime** — The core NLU engine. Uses ChromaDB vector database to match user utterances to pre-compiled intents. Supports domain routing (navigate, events, smalltalk), fuzzy matching, and fallback chains. |
| `wayfinding.py` | **Wayfinder** — Multi-floor Dijkstra pathfinding engine (see below). |
| `compiler/intent_compiler.py` | **Intent Compiler** — Pre-processes all intents (navigation targets, event descriptions, smalltalk patterns) into a ChromaDB collection with pre-generated TTS audio files. Runs once on first boot. |
| `compiler/build_navigate_intents.py` | Generates navigation intents from the map graph JSON files. Creates utterance variations like "Where is Lab 5?", "How do I get to Lab 5?", "Take me to Lab 5". |
| `compiler/watchdog.py` | File watcher that auto-recompiles intents when map data or event posters change. |
| `media_server.py` | **MediaServer** (port 8080). Serves poster images, audio cache files, map data, upload endpoints, and Facebook feed proxy. |
| `event_indexer.py` | Uses OpenAI Vision to extract structured metadata (title, date, location, description) from uploaded poster images. |
| `facebook_feed.py` | Fetches and caches posts from a Facebook page API for the event carousel. |
| `amplitude_tts.py` | Analyses pre-generated TTS audio to extract amplitude envelopes for speech-sync animation timing. |
| `local_speaker.py` | Plays TTS audio through the Pi's local speaker (for non-kiosk deployments). |
| `sentiment.py` | VADER sentiment analysis on conversation text, driving robot emotion during dialogue. |
| `greetings.py` | Generates contextual greeting messages based on time of day and face-greeting state. |
| `prompt.py` | System prompt templates for the AI assistant persona. |
| `text_filters.py` | Cleans LLM output (removes markdown, special characters, emoji for TTS). |

### Wayfinding Engine

`voice/wayfinding.py` — The **Wayfinder** class:

1. **Loads all floor maps** from `data/map_graph_floor_*.json`
2. **Builds a unified graph** with scoped node IDs (`floor_1::nodeId`) to avoid collisions
3. **Connects floors** by linking staircases/elevators with matching labels across adjacent floors (with a `FLOOR_CHANGE_COST = 50` penalty)
4. **Auto-heals** disconnected rooms by connecting them to the nearest waypoint
5. **Merges disconnected subgraphs** so every room is reachable
6. **Fuzzy room lookup** using substring matching and `SequenceMatcher` (handles typos and STT mishearings)
7. **Disambiguates** when multiple rooms match (e.g., "auditorium" → shows buttons for Auditorium 1, 2, 3)
8. **Dijkstra pathfinding** returns the shortest path with world coordinates, path IDs, directions text, distance, and estimated walk time
9. **Natural language directions** like "It is on the 3rd floor of the new building, on the left"

### Hardware Drivers (`hardware/`)

| File | What It Does |
|------|--------------|
| `arduino_servo.py` | **ArduinoServoLink** — Serial protocol driver for ESP32. Handles pan/tilt servo commands, arm servo commands, base motor spin commands, encoder reading, ToF data parsing, and firmware detection. Implements rate-limited servo streaming with dead-zone filtering. |

### Configuration System

| File | Purpose |
|------|---------|
| `config.yaml` | Default development config — full resolution, all features enabled |
| `config.kiosk.yaml` | **Kiosk deployment** — lower vision FPS/resolution, throttled loops, bye-wave off. Optimised for running frontend + Chromium on the same Pi. |

Configuration sections: `camera`, `servo`, `base`, `arms`, `imu`, `proximity`, `eyes`, `voice`, `face_greeting`, `face_greeting_arm`, `talk_gesture`, `bye_wave`, `debug_viz`, `surroundings_emotion`, `kiosk`.

---

## Frontend — Next.js Kiosk UI

The frontend is a **Next.js 14** application using React, TypeScript, Tailwind CSS, Three.js (via `@react-three/fiber`), and Framer Motion. It runs in a fullscreen Chromium kiosk browser on the robot's touchscreen.

### Kiosk View (`kiosk-view.tsx`)

The main UI component (~1700 lines). Manages four modes:

| Mode | Description |
|------|-------------|
| **Idle** | Shows a clock, rotating event carousel, and bottom dock navigation |
| **Events** | Category browser (Events / Competitions / Announcements) → poster grid → poster detail with AI-extracted metadata |
| **Maps** | Category hub (Lecture Halls / Labs / Offices) → location list → 3D navigation map with animated route |
| **Talk** | Voice conversation interface with live caption, animated microphone button, and suggested reply buttons |

Key features:
- **NLU Mode & LiveKit Mode**: Supports two voice backends — local NLU WebSocket (default) or LiveKit cloud
- **Event poster upload**: Staff scan a QR code → upload posters via phone → appear on kiosk in real-time
- **Voice-driven navigation**: Say "Where is Lab 5?" → voice reply + 3D map opens with glowing route
- **Suggested buttons**: After ambiguous queries, shows tappable disambiguation buttons

### 3D Isometric Navigation Map (`isometric-map.tsx`)

A **Three.js** isometric 3D building floor plan renderer (~730 lines). Features:

| Feature | Description |
|---------|-------------|
| **Building foundations** | Grid-based floor rendered as individual cells. Buildings are defined with `position`, `size`, and `removed_cells` (for irregular shapes like corridors). |
| **Room blocks** | Each room/lab/office is a 3D box placed at its world coordinates with a themed color (blue for lecture halls, purple for labs, etc.) and an emoji icon label. |
| **Integrated staircases** | Staircase nodes cut a hole in the building foundation and render 3-step descending geometry into the cutout — appearing as part of the building structure, not a separate piece. |
| **Glowing navigation path** | Animated neon-blue tube geometry following the Dijkstra path coordinates, with pulsing opacity. |
| **Floor switching** | Vertical floor buttons with animated spring-physics transitions (drop from above / rise from below) when switching floors. |
| **Multi-floor animation** | When navigating across floors, floor buttons highlight sequentially to show the travel path. |
| **Node click navigation** | In explore mode, tapping any room triggers voice navigation to that location. |
| **Room color theming** | `getRoomTheme()` assigns colors by room type: lecture halls (blue), labs (yellow), offices (blue), washrooms (gray), staircases (dark gray `#3d3d3dff`). |

### API Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/api/map` | GET | Returns map graph JSON for a given floor (nodes, edges, buildings) |
| `/api/navigate` | GET | Runs Wayfinder pathfinding and returns path + directions |
| `/api/locations` | GET | Lists all navigable rooms across all floors |
| `/api/facebook` | GET | Returns cached Facebook page posts for the event carousel |
| `/api/upload-poster` | POST | Handles poster image uploads from the mobile upload portal |
| `/api/upload-status` | GET | Returns list of uploaded files and timestamps (for polling) |
| `/api/tts` | POST | Proxies text-to-speech requests to Deepgram API |
| `/api/network-ip` | GET | Returns the Pi's local IP for QR code generation |

### Hooks

| Hook | Purpose |
|------|---------|
| `useNluVoice.ts` | Core NLU voice hook — manages Browser VAD (Voice Activity Detection), Deepgram STT WebSocket, NLU server WebSocket, audio playback queue, and conversation state machine. |
| `useNluAdapter.ts` | Adapter that normalises the NLU voice hook's interface to match the LiveKit component API, allowing the kiosk to switch between backends seamlessly. |
| `use-voice-config.ts` | Reads voice configuration from environment variables (agent name, microphone settings). |

---

## Map Data Format (`data/`)

Each floor has a JSON file: `map_graph_floor_1.json`, `map_graph_floor_2.json`, etc.

```json
{
  "nodes": [
    {
      "id": "1781198456809",
      "x": 4.5,              // X offset within building
      "z": -3,               // Z offset within building
      "building": "building_2",
      "label": "Laboratory 5",
      "type": "room",         // "room" or "waypoint"
      "size": [2.5, 1, 2.5]  // [width, height, depth]
    }
  ],
  "edges": [
    {
      "id": "e_src_tgt_timestamp",
      "source": "1781198456809",
      "target": "1782225357282",
      "visible": true
    }
  ],
  "buildings": {
    "building_1": {
      "position": [-4.5, 0, -1],  // World position
      "size": [12, 14],            // Grid size (columns, rows)
      "color": "#ffffff",
      "name": "Building 1",
      "removed_cells": ["0_3", "1_3"]  // Removed grid cells for irregular shapes
    }
  },
  "format": "3d"
}
```

- **Nodes** are either `room` (visible on map) or `waypoint` (invisible routing points for pathfinding)
- **Edges** connect nodes for the navigation graph
- **Buildings** define the grid foundation; `removed_cells` carve out corridors and irregular shapes
- **Staircases** are special room nodes whose labels contain "stair" — they connect floors in the pathfinding graph

---

## ESP32 Firmware (`firmware/`)

| Firmware | Description |
|----------|-------------|
| `head_servo/` | Basic pan/tilt servo control with base motor, encoder, and ToF streaming |
| `head_servo_hands/` | Extended firmware with 4 additional arm servo channels |
| `tof_test/` | Diagnostic firmware for ToF sensor testing |

**Serial Protocol:**
- `H` → `READY` (handshake)
- `P85.0 T105.0` → set pan and tilt
- `P85.0 T105.0 A0:90.0 A1:90.0 A2:90.0 A3:90.0` → set head + arms
- `?` → `POS P<pan> T<tilt>` (query position)
- `B L <duration>` / `B R <duration>` → spin base left/right
- ToF data streamed as `D <ch> <mm>` lines

**Wiring:**
- ESP32 GPIO 21 → PCA9685 SDA
- ESP32 GPIO 22 → PCA9685 SCL
- PCA9685 address `0x40`
- PCA9685 channel 4 → pan servo, channel 5 → tilt servo

---

## Complete Folder Structure

```
voice-agentv5/
├── start_robot.py              # Main entry — boots all services
├── config.yaml                 # Dev config (full features)
├── config.kiosk.yaml           # Pi kiosk config (CPU-optimised)
├── requirements.txt            # Python dependencies
├── .env                        # API keys (Deepgram, Groq, OpenRouter)
│
├── core/                       # Robot core services (threads)
│   ├── blackboard.py           #   Thread-safe shared state
│   ├── face_tracking.py        #   YuNet face detection + tracking
│   ├── servo_loop.py           #   PID pan/tilt control (80 Hz)
│   ├── servo_mixer.py          #   Final servo command gatekeeper
│   ├── base_controller.py      #   Rotating base plate control
│   ├── arm_controller.py       #   4-servo arm control
│   ├── emotion_engine.py       #   Face→emotion state machine
│   ├── eye_renderer.py         #   TFT animated eyes (40 FPS)
│   ├── animation_engine.py     #   Synchronized hardware timelines
│   ├── face_greeting.py        #   New-face greeting trigger
│   ├── face_greeting_arm.py    #   Arm wave for face greetings
│   ├── bye_wave_service.py     #   Hand wave → arm wave response
│   ├── talk_gesture_service.py #   Arm gestures while speaking
│   ├── speech_sync_service.py  #   Audio↔gesture timing sync
│   ├── imu_service.py          #   BMI160 IMU (100 Hz)
│   ├── tof_state.py            #   3D ToF occupancy map
│   ├── tof_stream.py           #   ESP32 ToF packet parser
│   ├── debug_dashboard.py      #   Web debug visualiser (:8082)
│   ├── surroundings_emotion.py #   Ambient emotion from context
│   └── yaw_pose.py             #   Encoder/IMU yaw fusion
│
├── voice/                      # Voice & NLU pipeline
│   ├── nlu_server.py           #   WebSocket NLU server (:8765)
│   ├── wayfinding.py           #   Multi-floor Dijkstra pathfinder
│   ├── media_server.py         #   Asset/API server (:8080)
│   ├── offline_voice/
│   │   └── runtime.py          #   ChromaDB NLU intent matcher
│   ├── compiler/
│   │   ├── intent_compiler.py  #   Pre-compile intents + TTS audio
│   │   ├── build_navigate_intents.py  # Navigation intent generation
│   │   └── watchdog.py         #   Auto-recompile on file changes
│   ├── event_indexer.py        #   AI poster metadata extraction
│   ├── facebook_feed.py        #   Facebook page feed fetcher
│   ├── amplitude_tts.py        #   TTS amplitude envelope analysis
│   ├── sentiment.py            #   VADER sentiment for emotions
│   └── tools/                  #   Voice agent tool functions
│       ├── time_tools.py       #     Current time queries
│       ├── search_tools.py     #     Web search (DuckDuckGo)
│       ├── content_tools.py    #     Event/poster content lookup
│       └── appearance_tools.py #     Eye color change commands
│
├── hardware/
│   └── arduino_servo.py        # ESP32 serial protocol driver
│
├── firmware/                   # ESP32 Arduino firmware
│   ├── head_servo/             #   Basic head servo + ToF
│   ├── head_servo_hands/       #   Head + arm servo firmware
│   └── flash.sh                #   Firmware flash script
│
├── data/                       # Campus map graph data
│   ├── map_graph_floor_1.json  #   Floor 1 (nodes, edges, buildings)
│   ├── map_graph_floor_2.json  #   Floor 2
│   ├── map_graph_floor_3.json  #   Floor 3
│   └── map_graph_floor_4.json  #   Floor 4
│
├── frontend/                   # Next.js kiosk application
│   ├── app/                    #   Next.js App Router pages
│   │   ├── layout.tsx          #     Root layout + fonts + metadata
│   │   ├── (app)/              #     Main app page (LiveKit provider)
│   │   ├── admin/              #     Admin configuration page
│   │   ├── upload-portal/      #     Mobile poster upload page
│   │   └── api/                #     API route handlers
│   │       ├── map/            #       Floor map data endpoint
│   │       ├── navigate/       #       Pathfinding endpoint
│   │       ├── locations/      #       Room listing endpoint
│   │       ├── facebook/       #       Facebook feed proxy
│   │       ├── upload-poster/  #       Poster upload handler
│   │       ├── upload-status/  #       Upload polling endpoint
│   │       ├── tts/            #       Text-to-speech proxy
│   │       └── network-ip/     #       Local IP for QR codes
│   ├── components/
│   │   ├── app/
│   │   │   ├── kiosk-view.tsx  #     Main kiosk UI (4 modes)
│   │   │   ├── isometric-map.tsx #   3D Three.js navigation map
│   │   │   ├── image-display.tsx #   Full-screen poster viewer
│   │   │   ├── chat-transcript.tsx # Chat history display
│   │   │   ├── campus-map-embed.tsx # 2D map embed fallback
│   │   │   └── welcome-view.tsx #    Initial welcome screen
│   │   ├── livekit/            #     LiveKit voice components
│   │   └── ui/                 #     Reusable UI components
│   │       ├── GeminiMorphButton/ #  Animated mic button
│   │       └── PopButton/      #     Springy tap button
│   ├── hooks/
│   │   ├── useNluVoice.ts      #     Browser VAD + NLU WebSocket
│   │   ├── useNluAdapter.ts    #     NLU↔LiveKit interface adapter
│   │   └── use-voice-config.ts #     Voice config from env
│   └── styles/                 #     CSS / Tailwind config
│
├── assets/                     # Runtime assets
│   ├── events/                 #   Event poster images
│   ├── competitions/           #   Competition poster images
│   ├── posts/                  #   Announcement poster images
│   └── audio_cache/            #   Pre-generated TTS MP3 files
│
├── scripts/                    # Deployment scripts
│   ├── launch-kiosk-stack.sh   #   Start everything (backend+frontend+browser)
│   ├── run-frontend-dev.sh     #   Frontend dev mode (hot reload)
│   ├── run-frontend-prod.sh    #   Frontend production build
│   ├── kiosk.sh                #   Launch fullscreen Chromium
│   └── refresh-kiosk.sh        #   Reload kiosk after frontend rebuild
│
├── docs/
│   └── KIOSK-CPU-PROFILE.md    # CPU optimisation documentation
│
├── tests/                      # Test scripts and calibration data
└── legacy/                     # Deprecated monolithic code
```

---

## How to Run

### Prerequisites
- **Raspberry Pi 4** (4GB+ RAM recommended) with Raspberry Pi OS
- **Node.js 20+** and `pnpm`
- **Python 3.10+** with `venv`
- **ESP32** with appropriate firmware flashed

### Terminal 1 — Backend
```bash
cd voice-agentv5
source venv/bin/activate
CONFIG_PATH=config.kiosk.yaml python start_robot.py
```

### Terminal 2 — Frontend
```bash
cd voice-agentv5/frontend
pnpm install
pnpm dev          # Development (hot reload)
# or
pnpm build && pnpm start  # Production
```

### Terminal 3 — Kiosk Browser (optional)
```bash
./scripts/kiosk.sh
```

### Ports

| Port | Service |
|------|---------|
| 3000 | Next.js kiosk UI |
| 8080 | Media server (posters, maps, upload APIs) |
| 8765 | NLU WebSocket voice server |
| 8082 | Debug dashboard (optional, `DEBUG_VIZ=1`) |

---

## Technology Stack

### Backend (Python)
| Technology | Purpose |
|------------|---------|
| OpenCV (YuNet) | Real-time face detection |
| ChromaDB | Vector database for NLU intent matching |
| Deepgram | Speech-to-Text (STT) and Text-to-Speech (TTS) |
| FastAPI / Starlette + Uvicorn | WebSocket NLU server |
| VADER Sentiment | Conversation emotion analysis |
| MediaPipe | Hand gesture recognition |
| PySerial | ESP32 serial communication |
| SMBus2 | BMI160 IMU I²C communication |
| Adafruit CircuitPython | TFT display and servo kit drivers |
| Pillow (PIL) | Eye rendering and image processing |
| PyYAML | Configuration file parsing |
| OpenAI | Poster metadata extraction (Vision API) |

### Frontend (TypeScript / React)
| Technology | Purpose |
|------------|---------|
| Next.js 14 | Full-stack React framework |
| Three.js / @react-three/fiber | 3D isometric map rendering |
| @react-three/drei | Three.js utilities (Text, Box, etc.) |
| Framer Motion | UI animations and transitions |
| Tailwind CSS | Utility-first styling |
| LiveKit (optional) | Cloud voice alternative |
| QRCode.react | QR code generation for upload portal |
| Deepgram Browser SDK | Client-side speech-to-text |

### Firmware (C++ / Arduino)
| Technology | Purpose |
|------------|---------|
| Arduino ESP32 | Microcontroller framework |
| PCA9685 | PWM servo driver library |
| VL53L1X | Time-of-Flight sensor driver |

---

*Built with ❤️ for the university campus — making navigation and information accessible to everyone.*
