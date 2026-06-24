/*
 * ToF Sensor Test Sketch — VL53L1X × 3 via TCA9548A
 *
 * Flash this INSTEAD of head_servo_hands to verify wiring.
 * Open Serial Monitor at 115200. You'll see:
 *
 *   TOF L=1234 C=567 R=890 VL=0 VC=-45 VR=12
 *
 * Once all 3 sensors read sane values, flash the real firmware.
 *
 * Wiring:
 *   ESP32 GPIO21 (SDA) → TCA9548A SDA + PCA9685 SDA
 *   ESP32 GPIO22 (SCL) → TCA9548A SCL + PCA9685 SCL
 *   TCA9548A Ch0 → VL53L1X LEFT   (0x29)
 *   TCA9548A Ch1 → VL53L1X CENTER  (0x29)
 *   TCA9548A Ch2 → VL53L1X RIGHT   (0x29)
 *   TCA9548A ADDR → GND (gives I2C address 0x70)
 *   All VL53L1X VIN → 3.3V, GND → GND
 */

#include <Wire.h>
#include <VL53L1X.h>

const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;
const uint8_t TCA_ADDR = 0x70;
const uint8_t TOF_COUNT = 3;
const uint8_t TOF_MUX_CH[TOF_COUNT] = {0, 1, 2};
const char*   TOF_LABEL[TOF_COUNT]  = {"LEFT", "CENTER", "RIGHT"};

VL53L1X sensors[TOF_COUNT];
bool sensorOk[TOF_COUNT] = {false, false, false};
int lastReading[TOF_COUNT] = {-1, -1, -1};
int prevReading[TOF_COUNT] = {-1, -1, -1};

void tcaSelect(uint8_t channel) {
    Wire.beginTransmission(TCA_ADDR);
    Wire.write(1 << channel);
    Wire.endTransmission();
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
            if (addr == 0x70) Serial.print(" (TCA9548A)");
            if (addr == 0x29) Serial.print(" (VL53L1X - direct, shouldn't see this with mux)");
            Serial.println();
            found++;
        }
    }
    Serial.print("Total devices: ");
    Serial.println(found);
    Serial.println("---");
}

void scanMuxChannels() {
    Serial.println("--- TCA9548A Channel Scan ---");
    for (uint8_t ch = 0; ch < 8; ch++) {
        tcaSelect(ch);
        delay(10);
        Wire.beginTransmission(0x29);
        uint8_t err = Wire.endTransmission();
        if (err == 0) {
            Serial.print("  Ch");
            Serial.print(ch);
            Serial.println(": VL53L1X found at 0x29");
        }
    }
    // Deselect all channels
    Wire.beginTransmission(TCA_ADDR);
    Wire.write(0);
    Wire.endTransmission();
    Serial.println("---");
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println();
    Serial.println("=== ToF Sensor Test ===");
    Serial.println("VL53L1X x3 via TCA9548A");
    Serial.println();

    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(100000);
    Wire.setTimeOut(50);

    // Step 1: Scan I2C bus (should see 0x40 PCA9685 + 0x70 TCA9548A)
    i2cScan();

    // Step 2: Check TCA9548A
    Wire.beginTransmission(TCA_ADDR);
    if (Wire.endTransmission() != 0) {
        Serial.println("ERROR: TCA9548A not found at 0x70!");
        Serial.println("Check wiring: SDA=21, SCL=22, ADDR pin to GND");
        Serial.println("Halting.");
        while (1) delay(1000);
    }
    Serial.println("TCA9548A OK at 0x70");

    // Step 3: Scan all mux channels for VL53L1X
    scanMuxChannels();

    // Step 4: Initialize each sensor
    int okCount = 0;
    for (int i = 0; i < TOF_COUNT; i++) {
        Serial.print("Initializing ");
        Serial.print(TOF_LABEL[i]);
        Serial.print(" (mux ch");
        Serial.print(TOF_MUX_CH[i]);
        Serial.print(")... ");

        tcaSelect(TOF_MUX_CH[i]);
        delay(10);

        sensors[i].setTimeout(200);
        if (sensors[i].init()) {
            sensors[i].setDistanceMode(VL53L1X::Long);
            sensors[i].setMeasurementTimingBudget(50000);  // 50ms
            sensors[i].startContinuous(100);               // 100ms interval
            sensorOk[i] = true;
            okCount++;
            Serial.println("OK");
        } else {
            Serial.println("FAILED!");
            Serial.print("  -> Check wiring on TCA9548A channel ");
            Serial.println(TOF_MUX_CH[i]);
        }
    }

    Serial.println();
    Serial.print("Result: ");
    Serial.print(okCount);
    Serial.print("/");
    Serial.print(TOF_COUNT);
    Serial.println(" sensors initialized");

    if (okCount == 0) {
        Serial.println("No sensors working. Check:");
        Serial.println("  1. VL53L1X VIN connected to 3.3V (not 5V directly)");
        Serial.println("  2. GND connected");
        Serial.println("  3. SDA/SCL connected to TCA9548A SD0-SD2/SC0-SC2");
        Serial.println("  4. Correct mux channel (0, 1, 2)");
        Serial.println("Halting.");
        while (1) delay(1000);
    }

    Serial.println();
    Serial.println("Streaming readings every 200ms...");
    Serial.println("Format: TOF L=mm C=mm R=mm VL=mm/s VC=mm/s VR=mm/s");
    Serial.println("(-1 = sensor not available or no valid reading)");
    Serial.println();
}

void loop() {
    for (int i = 0; i < TOF_COUNT; i++) {
        prevReading[i] = lastReading[i];

        if (!sensorOk[i]) {
            lastReading[i] = -1;
            continue;
        }

        tcaSelect(TOF_MUX_CH[i]);

        if (sensors[i].dataReady()) {
            int mm = sensors[i].read(false);
            uint8_t status = sensors[i].ranging_data.range_status;

            if (status == 0 && mm >= 20 && mm <= 4000) {
                lastReading[i] = mm;
            } else {
                // Print status for debugging
                if (status != 0 && lastReading[i] >= 0) {
                    // Only print once when status goes bad
                }
                lastReading[i] = -1;
            }
        }
    }

    // Compute velocity (mm per 200ms → mm/s)
    int velocity[TOF_COUNT];
    for (int i = 0; i < TOF_COUNT; i++) {
        if (lastReading[i] > 0 && prevReading[i] > 0) {
            velocity[i] = (lastReading[i] - prevReading[i]) * 5;  // * (1000/200)
        } else {
            velocity[i] = 0;
        }
    }

    // Print in parseable format
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

    delay(200);
}
