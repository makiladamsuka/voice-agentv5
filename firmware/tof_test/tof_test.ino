/*
 * ToF Sensor Test Sketch — VL53L0X × 3 via TCA9548A
 *
 * Hardware: ST GY-VL53L0XV2 (VL53L0X, NOT VL53L1X)
 *
 * Flash this INSTEAD of head_servo_hands to verify wiring.
 * Open Serial Monitor at 115200. You'll see:
 *
 *   TOF L=1234 C=567 R=890 VL=0 VC=-45 VR=12
 *
 * Wiring:
 *   ESP32 GPIO21 (SDA) → TCA9548A SDA + PCA9685 SDA
 *   ESP32 GPIO22 (SCL) → TCA9548A SCL + PCA9685 SCL
 *   TCA9548A Ch0 → VL53L0X LEFT   (0x29)
 *   TCA9548A Ch1 → VL53L0X CENTER (0x29)
 *   TCA9548A Ch2 → VL53L0X RIGHT   (0x29)
 *   TCA9548A ADDR → GND (address 0x70)
 *   All VL53L0X: VIN → 3.3V, GND → GND
 *
 * Library: Adafruit VL53L0X (NOT Pololu VL53L1X)
 *
 * Set ENABLE_CENTER false to test LEFT + RIGHT only (skip mux ch1).
 */

#include <Wire.h>
#include <Adafruit_VL53L0X.h>

const bool ENABLE_CENTER = false;

// VL53L0X: ~1.2 m in HIGH_ACCURACY, ~2 m in LONG_RANGE (not 3 m — needs VL53L1X).
// Readings stuck at 30–80 mm with nothing nearby = housing crosstalk / wrong aim.
const int MIN_VALID_MM = 80;    // below this, likely crosstalk on GY modules
const int MAX_VALID_MM = 2200;  // VL53L0X long-range practical ceiling
const int MAX_TRUST_MM = 1800;  // above this: unreliable — treat as open
const uint32_t TOF_TIMING_BUDGET_US = 66000;  // longer budget → better max distance

const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;
const uint8_t TOF_COUNT = 3;
const uint8_t TOF_MUX_CH[TOF_COUNT] = {0, 1, 2};
const char*   TOF_LABEL[TOF_COUNT]  = {"LEFT", "CENTER", "RIGHT"};

uint8_t tcaAddr = 0;

Adafruit_VL53L0X sensors[TOF_COUNT];
bool sensorOk[TOF_COUNT] = {false, false, false};
int lastReading[TOF_COUNT] = {-1, -1, -1};
int prevReading[TOF_COUNT] = {-1, -1, -1};
uint8_t failStreak[TOF_COUNT] = {0, 0, 0};
float filteredMm[TOF_COUNT] = {0, 0, 0};
bool filteredValid[TOF_COUNT] = {false, false, false};
unsigned long lastGoodMs[TOF_COUNT] = {0, 0, 0};
const unsigned long FILTER_HOLD_MS = 400;
const float FILTER_ALPHA = 0.25f;
unsigned long lastReprobeMs = 0;
const unsigned long REPROBE_MS = 2500;
const uint8_t FAIL_STREAK_MAX = 20;

void i2cRecover() {
    Wire.end();
    delay(10);
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(100000);
    Wire.setTimeOut(500);
    tcaAddr = findTcaAddr();
}

void tcaDeselect() {
    if (tcaAddr == 0) return;
    Wire.beginTransmission(tcaAddr);
    Wire.write(0);
    Wire.endTransmission();
}

bool pingTofOnChannel(uint8_t muxCh) {
    tcaSelect(muxCh);
    delay(20);
    return pingTof();
}

bool initOneSensor(int i, bool announce) {
    if (i == 1 && !ENABLE_CENTER) {
        sensorOk[i] = false;
        return false;
    }
    if (tcaAddr == 0) {
        tcaAddr = findTcaAddr();
        if (tcaAddr == 0) return false;
    }

    tcaSelect(TOF_MUX_CH[i]);
    delay(50);

    if (!pingTof()) {
        sensorOk[i] = false;
        failStreak[i] = 0;
        return false;
    }

    if (sensors[i].begin(0x29, false, &Wire,
            Adafruit_VL53L0X::VL53L0X_SENSE_LONG_RANGE)) {
        sensors[i].setMeasurementTimingBudgetMicroSeconds(TOF_TIMING_BUDGET_US);
        sensors[i].startRangeContinuous(70);
        sensorOk[i] = true;
        failStreak[i] = 0;
        lastReading[i] = -1;
        if (announce) {
            Serial.print("TOF reinit OK: ");
            Serial.println(TOF_LABEL[i]);
        }
        return true;
    }

    sensorOk[i] = false;
    failStreak[i] = 0;
    return false;
}

void reprobeSensors() {
    unsigned long now = millis();
    if (now - lastReprobeMs < REPROBE_MS) return;
    lastReprobeMs = now;

    if (tcaAddr == 0) {
        tcaAddr = findTcaAddr();
        if (tcaAddr == 0) return;
    }

    bool busGlitch = false;
    for (int i = 0; i < TOF_COUNT; i++) {
        if (i == 1 && !ENABLE_CENTER) continue;

        bool ping = pingTofOnChannel(TOF_MUX_CH[i]);
        if (sensorOk[i] && !ping) {
            sensorOk[i] = false;
            failStreak[i] = 0;
            lastReading[i] = -1;
            busGlitch = true;
            Serial.print("TOF lost: ");
            Serial.println(TOF_LABEL[i]);
        } else if (!sensorOk[i] && ping) {
            initOneSensor(i, true);
        }
    }
    tcaDeselect();

    if (busGlitch) {
        i2cRecover();
    }
}

void tcaSelect(uint8_t channel) {
    if (tcaAddr == 0) return;
    Wire.beginTransmission(tcaAddr);
    Wire.write(1 << channel);
    Wire.endTransmission();
}

uint8_t findTcaAddr() {
    for (uint8_t addr = 0x70; addr <= 0x77; addr++) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) return addr;
    }
    return 0;
}

bool pingTof() {
    Wire.beginTransmission(0x29);
    return Wire.endTransmission() == 0;
}

uint8_t readModelIdL0X() {
    Wire.beginTransmission(0x29);
    Wire.write(0x00);
    Wire.write(0xC0);
    if (Wire.endTransmission(false) != 0) return 0xFF;
    if (Wire.requestFrom((uint8_t)0x29, (uint8_t)1) != 1) return 0xFF;
    return Wire.read();
}

void i2cScan() {
    Serial.println("--- I2C Bus Scan ---");
    int found = 0;
    for (uint8_t addr = 1; addr < 127; addr++) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) {
            Serial.print("  Found device at 0x");
            if (addr < 16) Serial.print("0");
            Serial.print(addr, HEX);
            if (addr == 0x40) Serial.print(" (PCA9685)");
            if (addr >= 0x70 && addr <= 0x77) Serial.print(" (TCA9548A?)");
            if (addr == 0x29) Serial.print(" (VL53L0X visible — mux channel may be open)");
            Serial.println();
            found++;
        }
    }
    Serial.print("Total devices: ");
    Serial.println(found);
    Serial.println("---");
}

void scanMuxChannels() {
    if (tcaAddr == 0) {
        Serial.println("--- TCA9548A Channel Scan ---");
        Serial.println("  (mux not found — skipped)");
        Serial.println("---");
        return;
    }
    Serial.println("--- TCA9548A Channel Scan ---");
    for (uint8_t ch = 0; ch < 8; ch++) {
        tcaSelect(ch);
        delay(50);
        if (pingTof()) {
            Serial.print("  Ch");
            Serial.print(ch);
            Serial.print(": VL53L0X ping OK, model=0x");
            Serial.println(readModelIdL0X(), HEX);
        }
    }
    Wire.beginTransmission(tcaAddr);
    Wire.write(0);
    Wire.endTransmission();
    Serial.println("---");
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println();
    Serial.println("=== ToF Sensor Test ===");
    Serial.println("VL53L0X x3 via TCA9548A (GY-VL53L0XV2)");
    Serial.println("Range: LONG_RANGE + 66ms budget, max ~2 m (VL53L0X cannot do 3 m)");
    Serial.print("Rejecting readings < ");
    Serial.print(MIN_VALID_MM);
    Serial.println(" mm (typical crosstalk/housing false returns)");
    if (!ENABLE_CENTER) {
        Serial.println("Mode: LEFT + RIGHT only (center disabled)");
    }
    Serial.println();

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(100000);
    Wire.setTimeOut(500);

    i2cScan();

    tcaAddr = findTcaAddr();
    if (tcaAddr == 0) {
        Serial.println("WARN: TCA9548A not found at 0x70-0x77");
        Serial.println("  Check mux power, SDA/SCL, ADDR pin to GND");
    } else {
        Serial.print("TCA9548A OK at 0x");
        Serial.println(tcaAddr, HEX);
    }

    scanMuxChannels();

    int okCount = 0;
    int expectedCount = ENABLE_CENTER ? TOF_COUNT : (TOF_COUNT - 1);
    for (int i = 0; i < TOF_COUNT; i++) {
        Serial.print("Initializing ");
        Serial.print(TOF_LABEL[i]);
        Serial.print(" (mux ch");
        Serial.print(TOF_MUX_CH[i]);
        Serial.print(")... ");

        if (i == 1 && !ENABLE_CENTER) {
            Serial.println("SKIPPED (center disabled)");
            continue;
        }

        bool present = pingTofOnChannel(TOF_MUX_CH[i]);
        uint8_t model = present ? readModelIdL0X() : 0xFF;
        Serial.print("[ping=");
        Serial.print(present ? "Y" : "N");
        Serial.print(" model=0x");
        Serial.print(model, HEX);
        Serial.print("] ");

        if (initOneSensor(i, false)) {
            okCount++;
            Serial.println("OK");
        } else {
            Serial.println("FAILED!");
        }
    }
    tcaDeselect();

    Serial.println();
    Serial.print("Result: ");
    Serial.print(okCount);
    Serial.print("/");
    Serial.print(expectedCount);
    Serial.println(" sensors initialized");

    if (okCount == 0) {
        Serial.println("No sensors working. Check:");
        Serial.println("  1. Boards are VL53L0X (GY-VL53L0XV2), not VL53L1X");
        Serial.println("  2. Adafruit VL53L0X library installed (not Pololu VL53L1X)");
        Serial.println("  3. VIN -> 3.3V, GND connected");
        Serial.println("  4. Each sensor SDA/SCL on its own mux channel only");
        Serial.println("Streaming anyway (-1 = no reading)...");
    }

    Serial.println();
    Serial.println("Streaming readings every 200ms...");
    Serial.println("Hot-plug: sensors re-detected every 2.5s (no ESP reset needed)");
    Serial.println("Format: TOF L=mm C=mm R=mm VL=mm/s VC=mm/s VR=mm/s");
    Serial.println();
}

void loop() {
    reprobeSensors();

    if (!ENABLE_CENTER) {
        lastReading[1] = -1;
    }

    for (int i = 0; i < TOF_COUNT; i++) {
        if (i == 1 && !ENABLE_CENTER) continue;
        prevReading[i] = lastReading[i];

        if (!sensorOk[i]) {
            lastReading[i] = -1;
            continue;
        }

        tcaSelect(TOF_MUX_CH[i]);
        delay(5);

        if (!sensors[i].waitRangeComplete()) continue;

        uint16_t mm = sensors[i].readRangeResult();
        uint8_t status = sensors[i].readRangeStatus();

        if (status == 0 && mm >= MIN_VALID_MM && mm <= MAX_VALID_MM && mm <= MAX_TRUST_MM) {
            unsigned long nowMs = millis();
            if (!filteredValid[i]) {
                filteredMm[i] = (float)mm;
                filteredValid[i] = true;
            } else {
                filteredMm[i] = filteredMm[i] * (1.0f - FILTER_ALPHA) + (float)mm * FILTER_ALPHA;
            }
            lastGoodMs[i] = nowMs;
            lastReading[i] = (int)(filteredMm[i] + 0.5f);
            failStreak[i] = 0;
        } else if (filteredValid[i] && (millis() - lastGoodMs[i]) < FILTER_HOLD_MS) {
            lastReading[i] = (int)(filteredMm[i] + 0.5f);
        } else {
            lastReading[i] = -1;
            filteredValid[i] = false;
            if (status == 0 && mm >= MIN_VALID_MM && mm <= MAX_VALID_MM) {
                // above trust ceiling only — not a sensor fault
            } else if (++failStreak[i] >= FAIL_STREAK_MAX) {
                sensorOk[i] = false;
                failStreak[i] = 0;
                Serial.print("TOF stale, will reinit: ");
                Serial.println(TOF_LABEL[i]);
            }
        }
    }
    tcaDeselect();

    int velocity[TOF_COUNT];
    for (int i = 0; i < TOF_COUNT; i++) {
        if (lastReading[i] > 0 && prevReading[i] > 0
                && lastReading[i] <= MAX_TRUST_MM && prevReading[i] <= MAX_TRUST_MM) {
            velocity[i] = (lastReading[i] - prevReading[i]) * 5;
            if (velocity[i] > 300) velocity[i] = 300;
            if (velocity[i] < -300) velocity[i] = -300;
        } else {
            velocity[i] = 0;
        }
    }

    Serial.print("TOF L=");
    Serial.print(lastReading[0]);
    Serial.print(" C=");
    Serial.print(lastReading[1]);
    Serial.print(" R=");
    Serial.print(lastReading[2]);
    Serial.print(" VL=");
    Serial.print(velocity[0]);
    Serial.print(" VC=");
    Serial.print(velocity[1]);
    Serial.print(" VR=");
    Serial.println(velocity[2]);

    delay(100);
}
