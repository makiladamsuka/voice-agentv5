#!/bin/bash
# Flash ESP32 firmware via arduino-cli
#
# Usage:
#   ./firmware/flash.sh test          # flash tof_test sketch (sensor validation)
#   ./firmware/flash.sh prod          # flash head_servo_hands (full firmware)
#   ./firmware/flash.sh test --port /dev/ttyUSB1   # specify port
#
# Prerequisites:
#   arduino-cli core install esp32:esp32
#   arduino-cli lib install "Adafruit PWM Servo Driver Library"
#   arduino-cli lib install "Adafruit VL53L0X"

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

BOARD="esp32:esp32:esp32"
PORT=""
SKETCH=""
LABEL=""

# ── Parse arguments ──────────────────────────────────────────────────────
MODE="${1:-help}"
shift || true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port|-p)
            PORT="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ── Auto-detect port ─────────────────────────────────────────────────────
if [ -z "$PORT" ]; then
    for p in /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyACM0; do
        if [ -e "$p" ]; then
            PORT="$p"
            break
        fi
    done
fi

if [ -z "$PORT" ]; then
    echo "ERROR: No serial port found. Specify with --port /dev/ttyUSBx"
    exit 1
fi

# ── Select sketch ────────────────────────────────────────────────────────
case "$MODE" in
    test|tof)
        SKETCH="$SCRIPT_DIR/tof_test"
        LABEL="ToF Sensor Test (tof_test.ino)"
        ;;
    prod|full|main)
        SKETCH="$SCRIPT_DIR/head_servo_hands"
        LABEL="Full Firmware (head_servo_hands.ino)"
        ;;
    help|--help|-h)
        echo "Usage: $0 <mode> [--port /dev/ttyUSBx]"
        echo ""
        echo "Modes:"
        echo "  test   Flash tof_test.ino (sensor wiring validation)"
        echo "  prod   Flash head_servo_hands.ino (full robot firmware)"
        echo ""
        echo "Prerequisites:"
        echo "  arduino-cli core install esp32:esp32"
        echo "  arduino-cli lib install \"Adafruit PWM Servo Driver Library\""
        echo "  arduino-cli lib install \"Adafruit VL53L0X\""
        exit 0
        ;;
    *)
        echo "Unknown mode: $MODE (use 'test' or 'prod')"
        exit 1
        ;;
esac

# ── Check prerequisites ──────────────────────────────────────────────────
if ! command -v arduino-cli &> /dev/null; then
    echo "ERROR: arduino-cli not found. Install it:"
    echo "  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh"
    echo "  sudo mv bin/arduino-cli /usr/local/bin/"
    exit 1
fi

if [ ! -d "$SKETCH" ]; then
    echo "ERROR: Sketch directory not found: $SKETCH"
    exit 1
fi

# ── Check required libraries ─────────────────────────────────────────────
echo "Checking libraries..."
LIBS_OK=true

if ! arduino-cli lib list 2>/dev/null | grep -qi "VL53L0X"; then
    echo "  Installing Adafruit VL53L0X library..."
    arduino-cli lib install "Adafruit VL53L0X"
fi

if [ "$MODE" = "prod" ] || [ "$MODE" = "full" ] || [ "$MODE" = "main" ]; then
    if ! arduino-cli lib list 2>/dev/null | grep -qi "Adafruit PWM Servo"; then
        echo "  Installing Adafruit PWM Servo Driver Library..."
        arduino-cli lib install "Adafruit PWM Servo Driver Library"
    fi
fi

# ── Compile ──────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════"
echo "  Compiling: $LABEL"
echo "  Board:     $BOARD"
echo "  Port:      $PORT"
echo "═══════════════════════════════════════════════"
echo ""

arduino-cli compile --fqbn "$BOARD" "$SKETCH" --warnings default

# ── Upload ───────────────────────────────────────────────────────────────
echo ""
echo "Uploading to $PORT..."
arduino-cli upload --fqbn "$BOARD" --port "$PORT" "$SKETCH"

echo ""
echo "═══════════════════════════════════════════════"
echo "  DONE: $LABEL flashed to $PORT"
echo "═══════════════════════════════════════════════"

if [ "$MODE" = "test" ] || [ "$MODE" = "tof" ]; then
    echo ""
    echo "Next steps:"
    echo "  1. Open serial monitor:  arduino-cli monitor -p $PORT -c baudrate=115200"
    echo "  2. Or run Python monitor: python3 tests/test_tof_sensors.py $PORT"
    echo "  3. Verify sensors show valid readings (VL53L0X / GY-VL53L0XV2)"
    echo "  4. Then flash production:  $0 prod --port $PORT"
fi
