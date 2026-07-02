# Face Greeting Arm Gesture System

## Overview

The Face Greeting Arm system automatically plays random "hi" arm poses when detecting new faces. It uses face recognition to remember individuals for 30 minutes, preventing repeated greetings.

## Features

- **Face Recognition**: Uses histogram-based face embeddings to identify individuals
- **30-Minute Memory**: Remembers each face for 30 minutes
- **Random Greetings**: Plays random hi poses (hi1, hi2, hi3, hi4)
- **Intelligent Timing**: 
  - Only greets when face is held in frame for 0.5 seconds
  - Skips greeting during voice interaction or bye wave
  - Minimum face size threshold to avoid false positives
- **Automatic Cleanup**: Expired faces are removed from memory every 60 seconds

## System Architecture

### Components

1. **FaceGreetingArmService** (`core/face_greeting_arm.py`)
   - Main service monitoring for new faces
   - Computes face embeddings for recognition
   - Manages memory of greeted faces
   - Triggers greeting poses via Blackboard

2. **ArmController** (`core/arm_controller.py`)
   - Executes greeting poses
   - Smoothly transitions to/from greeting gestures
   - Returns to previous pose after 2 seconds

3. **Blackboard Fields** (`core/blackboard.py`)
   ```python
   arm_greeting_seq: int = 0           # Incremented for each greeting
   arm_greeting_pose: str = ""         # Pose name (hi1, hi2, hi3, hi4)
   arm_greeting_active: bool = False   # True during greeting execution
   ```

### Workflow

```
1. FaceTracker detects face in camera frame
2. FaceGreetingArmService waits 0.5 seconds (hold time)
3. Extract face ROI and compute embedding
4. Check if face is in memory:
   - NOT in memory → Trigger greeting, add to memory
   - In memory + expired (30+ min) → Trigger greeting, update timestamp
   - In memory + NOT expired → Skip (already greeted recently)
5. FaceGreetingArmService writes to Blackboard:
   - Increment arm_greeting_seq
   - Set arm_greeting_pose (random hi1-hi4)
6. ArmController detects sequence change
7. ArmController executes greeting:
   - Set arm_greeting_active = True
   - Smoothly move to greeting pose
   - Hold for 2 seconds
   - Return to previous pose
   - Set arm_greeting_active = False
```

## Configuration

Edit `config.yaml`:

```yaml
face_greeting_arm:
  enabled: true                      # Enable/disable system
  memory_timeout_minutes: 30.0       # How long to remember faces (minutes)
  min_face_area_ratio: 0.012        # Minimum face size (fraction of frame)
  hold_sec: 0.5                     # Face visibility duration before greeting
  embedding_threshold: 0.45         # Face matching strictness (lower = stricter)
  cleanup_interval_sec: 60.0        # How often to remove expired faces
```

### Parameters Explained

- **memory_timeout_minutes**: Time before a face is forgotten
  - Default: 30 minutes
  - Set to 1.0 for testing (greet every minute)
  - Set to 60.0 for longer memory

- **min_face_area_ratio**: Minimum face size to trigger greeting
  - Default: 0.012 (1.2% of frame)
  - Increase if getting false positives on small distant faces
  - Decrease if missing close faces

- **hold_sec**: How long face must be visible before greeting
  - Default: 0.5 seconds
  - Prevents greeting on quick passerbys
  - Increase for more deliberate greetings

- **embedding_threshold**: Face matching sensitivity
  - Default: 0.45
  - Lower values = more strict matching (less likely to match different faces)
  - Higher values = more lenient matching (may match similar faces)
  - Range: 0.2 (very strict) to 0.8 (very lenient)

## Available Hi Poses

From `tests/arm_pose_presets.json`:

- **hi1**: Raised arm, mid-height wave
- **hi2**: Raised arm, high wave
- **hi3**: Extended arm, side wave
- **hi4**: Extended arm, forward wave

The system randomly selects one of these poses for each greeting.

## Testing

### Live Camera Test

```bash
cd /home/nema/Documents/voice-agentv5
python3 tests/test_face_greeting_arm.py --mode live
```

This will:
- Start camera and face detection
- Monitor for new faces
- Print greeting events with timestamps
- Show faces in memory count

### Memory Simulation Test

```bash
python3 tests/test_face_greeting_arm.py --mode memory
```

This will:
- Test face embedding matching
- Test memory expiration (30-minute timeout)
- Test cleanup of expired faces
- No camera required

### Quick Testing Tips

For faster testing, temporarily reduce memory timeout:

```yaml
face_greeting_arm:
  memory_timeout_minutes: 1.0  # Greet every minute instead of 30
```

Then:
1. Run test
2. Show your face → gets greeting
3. Wait 1 minute
4. Show your face again → gets greeting again

## Integration with start_robot.py

The system is automatically started when both conditions are met:
1. `face_greeting_arm.enabled: true` in config.yaml
2. Arms are enabled (`arms.enabled: true`)

Check logs for:
```
[Bootstrap] FaceGreetingArmService enabled — arm gestures for new faces
[FaceGreetingArm] Initialized with 4 hi poses: ['hi1', 'hi2', 'hi3', 'hi4']
[FaceGreetingArm] Memory timeout: 30.0 minutes
[FaceGreetingArm] Monitoring for new faces to greet with arm gestures.
```

## Troubleshooting

### No Greetings Happening

1. **Check if enabled**:
   ```
   [FaceGreetingArm] Disabled in config.
   ```
   → Set `face_greeting_arm.enabled: true`

2. **Check OpenCV**:
   ```
   [FaceGreetingArm] OpenCV/NumPy not available. Disabled.
   ```
   → Install: `pip install opencv-python numpy`

3. **Check face detection**:
   - View debug stream at `http://localhost:8082`
   - Verify face is being detected (green box)
   - Check face size is above `min_face_area_ratio`

4. **Check if face is in memory**:
   ```
   [FaceGreetingArm] Known face (greeted 1x, 29.5 min until forgotten)
   ```
   → Wait for timeout or restart robot

5. **Check if busy**:
   - System skips greeting during voice interaction
   - System skips greeting during bye wave
   - Wait until agent is idle

### Face Not Recognized

If the system greets the same person multiple times:

1. **Lower embedding_threshold** for stricter matching:
   ```yaml
   embedding_threshold: 0.35  # Was 0.45
   ```

2. **Check lighting**: Poor lighting affects face recognition
3. **Check angle**: Profile views may not match frontal views

### False Matches

If the system thinks different people are the same:

1. **Raise embedding_threshold** for more lenient matching:
   ```yaml
   embedding_threshold: 0.55  # Was 0.45
   ```

2. Consider upgrading to deep learning face recognition:
   - Replace histogram method with `face_recognition` library
   - Better accuracy but requires more computation

## Production Considerations

### Face Recognition Upgrade

The current system uses color histogram features for simplicity. For better accuracy:

1. Install `face_recognition` library:
   ```bash
   pip install face_recognition
   ```

2. Replace `compute_face_embedding()` in `core/face_greeting_arm.py`:
   ```python
   import face_recognition
   
   def compute_face_embedding(face_roi: np.ndarray) -> Optional[np.ndarray]:
       rgb = cv2.cvtColor(face_roi, cv2.COLOR_BGR2RGB)
       encodings = face_recognition.face_encodings(rgb)
       if encodings:
           return encodings[0]
       return None
   ```

3. Update `embedding_distance()`:
   ```python
   def embedding_distance(emb1: np.ndarray, emb2: np.ndarray) -> float:
       return face_recognition.face_distance([emb1], emb2)[0]
   ```

4. Adjust threshold:
   ```yaml
   embedding_threshold: 0.6  # face_recognition uses different scale
   ```

### Privacy Considerations

- No face images are stored (only embeddings)
- Embeddings are in-memory only (lost on restart)
- No face data is transmitted or logged
- Consider adding privacy disclosure if deploying publicly

## Logs and Monitoring

Key log messages:

```
# New face detected
[FaceGreetingArm] New face detected → hi2
[FaceGreetingArm] Added new face to memory (total: 1)
[ArmController] Starting greeting: hi2 → (68.0, 12.0, 53.5, 75.8)

# Known face (skip)
[FaceGreetingArm] Known face (greeted 1x, 27.3 min until forgotten)

# Forgotten face returns
[FaceGreetingArm] Forgotten face returned → hi4

# Memory cleanup
[FaceGreetingArm] Cleaned up 2 expired faces (now 1 in memory)

# Greeting execution
[ArmController] Greeting complete, returning to previous pose
```

## Comparison with Voice Greetings

| Feature | Voice Greeting | Arm Greeting |
|---------|---------------|--------------|
| **File** | `core/face_greeting.py` | `core/face_greeting_arm.py` |
| **Output** | Text-to-speech | Arm pose gesture |
| **Memory** | None (session-based) | 30-minute face recognition |
| **Cooldown** | 60 seconds | Per-face timeout |
| **Recognition** | None (any face) | Face embeddings |
| **When** | LiveKit session active | Anytime arms enabled |

Both systems can run simultaneously:
- Voice greeting speaks when voice session is active
- Arm greeting gestures work independently with face memory
