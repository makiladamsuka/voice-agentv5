# head_servo_hands

**Baseline `head_servo` + four arm servos.** Same head/base protocol as `firmware/head_servo/`
(L/R spin, encoder moves, U/RL limit unlock, etc.) — face tracking keeps working.

## PCA9685 wiring

| PCA ch | Token | Servo |
|--------|-------|-------|
| 4 | P | pan |
| 5 | T | tilt |
| 0 | A0 | MG996R_R — right shoulder raise |
| 2 | A1 | MG996R_L — left shoulder raise |
| 8 | A2 | SG90_R — right arm sweep |
| 9 | A3 | SG90_L — left arm sweep |

## Arm limits (deg) and home

| Servo | Min | Max | Home |
|-------|-----|-----|------|
| A0 (R raise) | 47 | 124 | 47 |
| A1 (L raise) | 0 | 65 | 65 |
| A2 (R sweep) | 44 | 78 | 64 |
| A3 (L sweep) | 70 | 102 | 87 |

Home = captured **raise low + min sweep** pose (`tests/captured_arm_limits.json`).

I2C: SDA=21, SCL=22, address `0x40`, 115200 baud USB serial.

## Flash

```bash
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32 firmware/head_servo_hands
```

Boot banner: `FW head_servo_hands_v5`

Arm PWM is **off at boot** and after **1.5 s** without an `A0..A3` command. Send `AO` to detach immediately. Head pan/tilt (ch 4/5) unchanged.

## Test arms

```bash
python tests/test_servo_arms_manual.py
```

## Note

`firmware/head_servo/head_servo.ino` stays head-only for `main`/baseline.
Flash this sketch when arms are connected; `start_robot.py` needs no code changes.
