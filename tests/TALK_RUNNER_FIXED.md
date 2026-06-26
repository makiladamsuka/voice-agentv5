# test_talk_runner.py - Fixed!

## What was wrong

The test_talk_runner.py was using `amplitude_fast` (TTS audio amplitude) as a workaround instead of the proper `agent_speaking` flag.

## What was fixed

Changed test_talk_runner.py to:
1. Monitor `agent_speaking` flag directly (not `amplitude_fast`)
2. Removed all amplitude threshold logic and silence counting
3. Updated documentation to reflect proper usage

## How it works now

1. **VoiceService writes agent_speaking flag:**
   - `voice/voice_service.py` line 372: `_bb.write(agent_speaking=True)` when agent starts speaking
   - `voice/voice_service.py` line 377: `_bb.write(agent_speaking=False)` when agent stops

2. **test_talk_runner.py reads agent_speaking flag:**
   - Monitors `bb.read().get("agent_speaking", False)`
   - When True → randomly cycles through talk1, talk2 poses
   - When False → stops and waits

## How to use

### On Raspberry Pi:

```bash
# Terminal 1: Start the robot (includes VoiceService)
cd /home/nema/Documents/voice-agentv5
python start_robot.py

# Terminal 2: Run the talk pose runner
python tests/test_talk_runner.py
```

### Expected behavior:

1. Robot starts, arms go to home position
2. test_talk_runner.py starts monitoring agent_speaking
3. When you connect via LiveKit and the agent speaks:
   - agent_speaking becomes True
   - Arms randomly switch between talk1 and talk2 poses
   - Creates a "talking" animation effect
4. When agent stops speaking:
   - agent_speaking becomes False
   - Arms return to waiting
5. Cycle repeats every time the agent speaks

## Verification

Monitor the flags with:

```bash
python tests/test_blackboard_monitor.py
```

You should see:
```
[AGENT] agent_speaking changed: False -> True 🟢 SPEAKING
[AGENT] agent_speaking changed: True -> False 🔴 SILENT
```

## Configuration

In `config.yaml`, make sure voice service is enabled:

```yaml
voice:
  enabled: true
  devmode: true  # or false for production
```

## Talk poses

The talk poses are defined in `tests/arm_pose_presets.json`:

```json
"talk1": {
  "a0": 89.0,
  "a1": 40.0,
  "a2": 44.0,
  "a3": 80.5
},
"talk2": {
  "a0": 74.0,
  "a1": 34.0,
  "a2": 44.0,
  "a3": 78.9
}
```

You can add more talk poses (talk3, talk4, etc.) and test_talk_runner.py will automatically pick them up.

## Timing parameters

Adjust in command line:

```bash
# Faster pose switching (default is 0.5s)
python tests/test_talk_runner.py --frame-delay 0.3

# More responsive checking (default is 0.05s)
python tests/test_talk_runner.py --poll-interval 0.02

# Enable verbose debug output
python tests/test_talk_runner.py --debug
```

## Architecture

```
┌──────────────────────┐
│   LiveKit Client     │
│  (User speaks to)    │
└──────────┬───────────┘
           │
           │ WebRTC
           ▼
┌──────────────────────┐
│   VoiceService       │◄─── Runs in start_robot.py thread
│  (voice_service.py)  │
└──────────┬───────────┘
           │
           │ writes agent_speaking=True/False
           ▼
┌──────────────────────┐
│     Blackboard       │◄─── Shared state (thread-safe)
│   (blackboard.py)    │
└──────────┬───────────┘
           │
           │ reads agent_speaking
           ▼
┌──────────────────────┐
│  test_talk_runner.py │◄─── Separate test script
│  (randomly switches  │
│   talk poses)        │
└──────────┬───────────┘
           │
           │ writes arm_a0, arm_a1, arm_a2, arm_a3
           ▼
┌──────────────────────┐
│     ServoMixer       │◄─── Runs in start_robot.py thread
│  (reads arm poses)   │
└──────────┬───────────┘
           │
           │ sends servo commands
           ▼
┌──────────────────────┐
│   ESP32 Hardware     │
│   (physical arms)    │
└──────────────────────┘
```

## Troubleshooting

**Problem:** Arms don't move when agent speaks

**Check:**
1. Is start_robot.py running with voice enabled?
   ```bash
   # In config.yaml:
   voice:
     enabled: true
   ```

2. Is test_talk_runner.py showing agent_speaking changes?
   ```
   [DEBUG] ✓ Agent started speaking (agent_speaking=True)
   ```

3. Is ServoMixer getting the arm commands?
   - Check if arm_controller is enabled in config.yaml
   - Check if ESP32 has arm firmware

4. Are the talk poses being loaded?
   ```
   [INFO] ✓ Loaded 2 talk poses: talk1, talk2
   ```

**Problem:** "Blackboard read failed"

**Solution:** Make sure start_robot.py is running first. The Blackboard is created by start_robot.py.

**Problem:** No talk poses found

**Solution:** Check that `tests/arm_pose_presets.json` exists and contains poses starting with "talk" (talk1, talk2, etc.)

## Notes

- The arms will keep their last talk pose after agent stops speaking. This is by design (looks more natural than snapping back to home).
- ArmController will override talk poses if the base starts spinning (lean animation takes priority).
- ByeWaveService will override talk poses during bye animations.
- test_talk_runner.py should be run as a separate process alongside start_robot.py.

## Files involved

- `tests/test_talk_runner.py` - The talk animation runner (FIXED)
- `voice/voice_service.py` - Sets agent_speaking flag (already working)
- `core/blackboard.py` - Shared state bus
- `tests/arm_pose_presets.json` - Talk pose definitions
- `config.yaml` - Voice service configuration
- `start_robot.py` - Main entry point
