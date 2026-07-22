# Kiosk CPU profile (`config.kiosk.yaml`)

This document describes what changes when you run the Pi with the **kiosk CPU profile** instead of full `config.yaml`. Use this profile when the same Pi runs **backend + Next.js + Chromium** together.

```bash
CONFIG_PATH=config.kiosk.yaml python start_robot.py start
# or
./scripts/launch-kiosk-stack.sh
```

**Design priority:** stable voice audio and lower CPU — not maximum vision fidelity or base motion during conversation.

---

## Quick summary

| Area | Kiosk behavior |
|------|----------------|
| Voice (LiveKit, local mic/speaker) | **On** — Pi captures mic and plays TTS on speakers |
| Head wander / idle glances | **On** — head still looks around when no face |
| Voice “thinking” head bob | **On** — during `conv_state: thinking` |
| ToF stream + PROX detection | **On** — ESP32 ToF data still ingested |
| ToF approach **base turns** | **On when idle** — **blocked during voice session** |
| ToF approach **head glance** | **On during voice** — head turns toward L/R approach |
| Base idle wander / spins | **On** — follows head + random turns when idle (still frozen during voice) |
| Face tracking during conversation | **Reduced / paused** while listening or speaking |
| Debug 3D dashboard (`:8082`) | **Off by default** — use `DEBUG_VIZ=1` to enable |
| Bye-wave hand gestures | **Off** |

---

## What is reduced or disabled

### Vision and face tracking

| Setting | `config.yaml` (full) | `config.kiosk.yaml` |
|---------|----------------------|---------------------|
| Camera capture | 1920×1080 | 640×480 |
| YuNet detect resolution | 1280×720 | 480×270 |
| Vision FPS | 10 | 5 (2 during voice) |
| MJPEG stream FPS | 8 | 6 |
| Pause camera during voice | — | **Yes** (`vision_pause_on_audio: true`) |

While a voice session is active, face tracking **slows down or pauses** when the user or agent is speaking/listening. This saves CPU for audio and STT.

### Control loop rates

| Service | Full config | Kiosk (normal) | Kiosk (during voice) |
|---------|-------------|----------------|----------------------|
| Servo loop | 80 Hz | 35 Hz | **16 Hz** |
| Arms | 50 Hz | 35 Hz | **12 Hz** |
| Base controller | — | 25 Hz | **10 Hz** |
| IMU sample rate | 100 Hz | 25 Hz | 25 Hz |
| Eye renderer | 40 fps | 12 fps | 12 fps |
| Talk gesture poll | 50 Hz | ~12.5 Hz | ~12.5 Hz |
| Emotion engine | default | default | **6 Hz** |

Rates during voice come from `voice_profile` in `config.kiosk.yaml`:

```yaml
voice_profile:
  arms_loop_hz: 12
  servo_loop_hz: 16
  servo_send_hz: 16
  mixer_loop_hz: 16
  base_loop_hz: 10
  emotion_loop_hz: 6
  freeze_base_during_voice: true
  conv_nod_scale: 0.2
  viz_pose_hz: 0
```

### Motion and features turned off

- **Debug dashboard** — `debug_viz.auto_start: false` (port 8082). Enable with `DEBUG_VIZ=1` or set `auto_start: true`.
- **Viz pose publishing** — `viz_pose_hz: 0` (no pose stream to debug UI unless viz is on).
- **Bye wave** — `bye_wave.enabled: false`.
- **Base wander** — `base.wander_enabled: true` (idle plate follow + random turns; still paused during voice via `freeze_base_during_voice`).
- **Base motion during voice** — `freeze_base_during_voice: true` (no track follow, ToF base turns, memory base moves, etc. while LiveKit session is connected).
- **Face greeting arm gestures** — off in both profiles (`face_greeting_arm.enabled: false`).

### Backend audio path (kiosk voice)

When `voice.local_mic` and `voice.local_speaker` are true in `config.kiosk.yaml`:

- Microphone is captured on the **Pi** (not the browser).
- TTS plays on **Pi speakers** (browser agent audio is muted).
- AEC reverse stream is fed from the local speaker into the mic APM for echo cancellation.

---

## What still works

### ToF approach

ToF sensing and PROX approach detection **still run**:

- ESP32 ToF lines are parsed (`TofStreamHandler`).
- `proximity.enabled: true`, `tof_turn_enabled: true`.
- Investigate / scan behavior is enabled (`proximity.investigate.enabled: true`).

**During an active voice session:**

- **Base does not turn** toward approach (`freeze_base_during_voice`).
- **Head can glance** toward L/R approach zones (short `prox_glance_*` motion).
- If already tracking a face, investigate may run as a **head-only** scan.

**When not in voice**, full approach behavior (including base turns) works as configured.

### “Wondering” / wander

Two different behaviors:

| Behavior | Kiosk |
|----------|-------|
| **Head wander** — idle glances, organic look-around (`ServoLoop` wander mode) | **On** |
| **Thinking wander holds** — longer pauses while “thinking” | **On** (`wander_thinking_hold_chance`, etc.) |
| **Conversation thinking bob** — head motion when `conv_state: thinking` | **On** |
| **Base wander** — plate spins when idle | **On** (blocked during voice session) |
| **Random large base turns** | **On** |

So the robot still **looks around with its head**; it does **not** do idle **base** wandering on kiosk.

### Other behavior still active

- Face tracking (when vision is not paused).
- Person memory and last-seen search.
- Face greeting (voice hello when new face seen).
- Talk gestures during agent speech (slower poll rate).
- Surroundings / conversation emotions.
- Proximity investigate when not blocked by voice/base freeze.

---

## Re-enable behavior (without switching to full `config.yaml`)

Edit `config.kiosk.yaml`:

| Goal | Setting |
|------|---------|
| Base turns on ToF during voice | `voice_profile.freeze_base_during_voice: false` |
| Idle base wandering | `base.wander_enabled: false` to disable again |
| Face track during conversation | `stream.vision_pause_on_audio: false` |
| Debug 3D map + MJPEG | `debug_viz.auto_start: true` or `DEBUG_VIZ=1` |
| Higher face detection rate | Raise `stream.vision_fps`, `camera.main_res`, `camera.detect_res` |
| Faster servos (more CPU) | Raise `servo.loop_hz` and `voice_profile.servo_loop_hz` |
| Bye wave | `bye_wave.enabled: true` |

After config changes, restart the backend:

```bash
# Ctrl+C on launch script, then:
./scripts/launch-kiosk-stack.sh
```

---

## Compare configs at a glance

```bash
# Run with full tuning (heavier — dev machine or Pi without Chromium)
python start_robot.py start

# Run with kiosk tuning (recommended on Pi with UI)
CONFIG_PATH=config.kiosk.yaml python start_robot.py start
```

| File | When to use |
|------|-------------|
| `config.yaml` | Development, higher vision rates, 80 Hz servos, debug viz auto-start |
| `config.kiosk.yaml` | **Pi demo** — frontend + Chromium + voice on one board |

---

## Related files

| Path | Role |
|------|------|
| `config.kiosk.yaml` | Kiosk tuning values |
| `scripts/launch-kiosk-stack.sh` | Backend + frontend + Chromium (uses kiosk config) |
| `scripts/kiosk.sh` | Fullscreen Chromium → `http://127.0.0.1:3000/` |
| `voice/local_audio_io.py` | Pi mic + AEC |
| `voice/local_speaker.py` | Pi speaker + TTS sink |
| `core/face_tracking.py` | Vision pause during audio |
| `core/base_controller.py` | Base freeze during voice, ToF approach / glance |
