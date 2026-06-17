# Voice Agent V5 Face Tracking Head

Isolated Raspberry Pi + ESP32 face tracking build.

- Raspberry Pi runs `face_tracking_head.py`.
- Picamera2 and YuNet detect the largest face using the same camera setup as `tftdisplay/trackingeyes2.py`.
- Dual ST7735 TFT eyes use the same SPI pins as `trackingeyes2.py`.
- ESP32 receives `P<pan> T<tilt>` over USB serial and drives two PCA9685 servos.

## Pi dependencies

```bash
sudo apt install python3-picamera2 python3-opencv
python -m pip install pillow adafruit-circuitpython-rgb-display pyserial PyYAML
```

## Run

From this directory:

```bash
python face_tracking_head.py --port /dev/ttyUSB0
```

IMU / BMI160 experiments live on the `feature/imu` branch (`face_tracking_head_imu.py`).

Useful smoke tests:

```bash
python face_tracking_head.py --no-servo
python face_tracking_head.py --no-stream --port /dev/ttyUSB0
```

MJPEG preview defaults to:

```text
http://<pi-ip>:8081/stream
```

## ESP32 firmware

Wiring defaults:

- ESP32 GPIO 21 -> PCA9685 SDA
- ESP32 GPIO 22 -> PCA9685 SCL
- PCA9685 address `0x40`
- PCA9685 channel 4 -> pan servo
- PCA9685 channel 5 -> tilt servo
- External 5 V servo power, common ground with ESP32 and Pi

Install Arduino toolchain once:

```bash
arduino-cli core update-index
arduino-cli core install esp32:esp32@2.0.17
arduino-cli lib install "Adafruit PWM Servo Driver Library" "Adafruit BusIO"
```

Compile and upload from the v5 root:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/head_servo
arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32 firmware/head_servo
```

Serial protocol:

- `H` -> `READY`
- `P85.0 T105.0` -> set pan/tilt degrees
- `S` -> bench sweep
- `?` -> `POS P<pan> T<tilt>`
