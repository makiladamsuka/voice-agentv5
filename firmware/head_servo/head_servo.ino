/*
 * Voice Agent V5 ESP32 firmware.
 *
 * Head: PCA9685 @ 0x40 on ESP32 SDA=21 / SCL=22, ch4 pan / ch5 tilt.
 * Base: GPIO35/34 encoder, GPIO25 PWM, GPIO26/27 -> TB6612FNG -> N20 motor.
 *
 * Protocol:
 *   H              -> READY
 *   P80.0 T110.0  -> set head pan/tilt
 *   B+2.0 / B-2.0 -> relative base degrees
 *   B0.0          -> absolute base degrees from zero
 *   J+80 M300     -> open-loop base jog PWM for milliseconds
 *   C1.222        -> set counts per base degree
 *   E-1 / E1      -> encoder sign (+deg command maps to sign*cpd counts)
 *   Z              -> zero base encoder reference
 *   X              -> stop base motor
 *   ?              -> POS <count> DEG <deg> CPD <cpd> BUSY 0|1
 *   S              -> bench sweep
 */

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include <string.h>

const int I2C_SDA_PIN = 21;
const int I2C_SCL_PIN = 22;
const int LED_PIN = 2;

const uint8_t PAN_CH = 4;
const uint8_t TILT_CH = 5;
const float PAN_MIN = 55.0f;
const float PAN_MAX = 90.0f;
const float TILT_MIN = 112.0f;
const float TILT_MAX = 116.0f;
const float PAN_CENTER = 73.0f;
const float TILT_CENTER = 114.0f;
const int PULSE_MIN_US = 450;
const int PULSE_MAX_US = 2600;

const int ENC_A_PIN = 35;
const int ENC_B_PIN = 34;
const int MOTOR_PWM_PIN = 25;
const int MOTOR_AIN1_PIN = 26;
const int MOTOR_AIN2_PIN = 27;

const int LINE_BUF_SIZE = 96;
const int ENCODER_TOLERANCE = 35;
const int COARSE_ERR_COUNTS = 48;
const int COARSE_PWM = 150;
const int FINE_MIN_PWM = 82;
const int PWM_MAX = 150;
const unsigned long STALL_MS = 4000;
const int MOTOR_DIR_SIGN = -1;
const int STALL_MIN_PROGRESS = 1;
const unsigned long MOVE_TIMEOUT_MS = 12000;
const float FINE_KP = 1.8f;
const float FINE_PWM_PER_COUNT = 4.0f;
const float MAX_ABS_BASE_DEG = 140.0f;   // Hard safety envelope from startup zero.
const float ABS_LIMIT_MARGIN_DEG = 4.0f; // Allow tiny transient/noise before fault.
const float OVERSHOOT_ALLOW_RATIO = 0.35f;
const long OVERSHOOT_ALLOW_MIN_COUNTS = 140;
const int OVERSHOOT_CONFIRM_CYCLES = 3;
const unsigned long MOVE_KICK_MS = 260;
const int MOVE_KICK_PWM = 150;
const unsigned long JOG_MAX_MS = 3000;

const int LEDC_FREQ_HZ = 20000;
const int LEDC_RES_BITS = 8;
const int LEDC_PWM_CHANNEL = 0;

Adafruit_PWMServoDriver pwm(0x40);
portMUX_TYPE encoderMux = portMUX_INITIALIZER_UNLOCKED;

char lineBuffer[LINE_BUF_SIZE];
uint8_t lineLen = 0;
float panAngle = PAN_CENTER;
float tiltAngle = TILT_CENTER;
bool pcaReady = false;

volatile long encoderCount = 0;
uint8_t encLastState = 0;
long zeroOffset = 0;
long moveTargetCount = 0;
long moveStartCount = 0;
bool baseBusy = false;
bool moveActive = false;
bool jogActive = false;
unsigned long jogEndMs = 0;
unsigned long moveStartMs = 0;
unsigned long lastProgressMs = 0;
long lastProgressCount = 0;
long lastErrorAbs = 0;
long moveMaxTravelCounts = 0;
int overshootCycles = 0;
float ackBaseDeg = 0.0f;
bool pendingBaseAck = false;
float countsPerBaseDeg = 1.0f;
float encoderSign = 1.0f;

const int8_t ENC_QUAD_TABLE[16] = {
  0, 1, -1, 0,
  -1, 0, 0, 1,
  1, 0, 0, -1,
  0, -1, 1, 0
};

float clampf(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

int mapAngleToUs(float deg, float degMin, float degMax) {
  float t = (deg - degMin) / (degMax - degMin);
  t = clampf(t, 0.0f, 1.0f);
  return PULSE_MIN_US + (int)(t * (float)(PULSE_MAX_US - PULSE_MIN_US));
}

void setServoPulseUs(uint8_t ch, int pulseUs) {
  pulseUs = constrain(pulseUs, PULSE_MIN_US, PULSE_MAX_US);
  uint32_t tick = ((uint32_t)pulseUs * 4096UL) / 20000UL;
  if (tick >= 4096) tick = 4095;
  pwm.setPWM(ch, 0, tick);
}

void serviceEncoder() {
  uint8_t a = digitalRead(ENC_A_PIN) & 1;
  uint8_t b = digitalRead(ENC_B_PIN) & 1;
  uint8_t state = (a << 1) | b;
  if (state == encLastState) return;
  uint8_t idx = (encLastState << 2) | state;
  int8_t delta = ENC_QUAD_TABLE[idx];
  if (delta != 0) {
    encoderCount += delta;
    encLastState = state;
  }
}

void serviceEncoderLocked() {
  portENTER_CRITICAL(&encoderMux);
  serviceEncoder();
  portEXIT_CRITICAL(&encoderMux);
}

void syncEncoderState() {
  uint8_t a = digitalRead(ENC_A_PIN) & 1;
  uint8_t b = digitalRead(ENC_B_PIN) & 1;
  encLastState = (a << 1) | b;
}

long readEncoderCount() {
  portENTER_CRITICAL(&encoderMux);
  long v = encoderCount;
  portEXIT_CRITICAL(&encoderMux);
  return v;
}

float countsToDeg(long counts) {
  if (countsPerBaseDeg < 0.05f || encoderSign == 0.0f) return 0.0f;
  return (float)(counts - zeroOffset) / (countsPerBaseDeg * encoderSign);
}

long degToCounts(float deg) {
  return zeroOffset + (long)(deg * countsPerBaseDeg * encoderSign);
}

bool setCountsPerBaseDeg(float cpd) {
  if (cpd < 0.05f || cpd > 200.0f) return false;
  countsPerBaseDeg = cpd;
  return true;
}

bool setEncoderSign(float sign) {
  if (sign < 0.0f) {
    encoderSign = -1.0f;
  } else if (sign > 0.0f) {
    encoderSign = 1.0f;
  } else {
    return false;
  }
  return true;
}

void motorPwmWrite(int magnitude) {
  magnitude = constrain(magnitude, 0, PWM_MAX);
  int duty = (magnitude * 255) / PWM_MAX;
  ledcWrite(LEDC_PWM_CHANNEL, duty);
}

void motorStop() {
  ledcWrite(LEDC_PWM_CHANNEL, 0);
  digitalWrite(MOTOR_AIN1_PIN, LOW);
  digitalWrite(MOTOR_AIN2_PIN, LOW);
}

void motorDrive(int pwm) {
  pwm = constrain(pwm * MOTOR_DIR_SIGN, -PWM_MAX, PWM_MAX);
  if (pwm == 0) {
    motorStop();
    return;
  }
  if (pwm > 0) {
    digitalWrite(MOTOR_AIN1_PIN, LOW);
    digitalWrite(MOTOR_AIN2_PIN, HIGH);
    motorPwmWrite(pwm);
  } else {
    digitalWrite(MOTOR_AIN1_PIN, HIGH);
    digitalWrite(MOTOR_AIN2_PIN, LOW);
    motorPwmWrite(-pwm);
  }
}

void stopBaseMotion() {
  moveActive = false;
  jogActive = false;
  baseBusy = false;
  motorStop();
}

void printServoAck() {
  Serial.print(F("OK P"));
  Serial.print((int)round(panAngle));
  Serial.print(F(" T"));
  Serial.println((int)round(tiltAngle));
}

void printBaseAck(float deg) {
  Serial.print(F("OK B"));
  Serial.println(deg, 1);
}

void printBaseBusy() {
  Serial.println(F("ERR B busy"));
}

void writeAngles(float pan, float tilt, bool emitAck) {
  panAngle = clampf(pan, PAN_MIN, PAN_MAX);
  tiltAngle = clampf(tilt, TILT_MIN, TILT_MAX);
  if (!pcaReady) {
    if (emitAck) Serial.println(F("ERR PCA9685"));
    return;
  }
  setServoPulseUs(PAN_CH, mapAngleToUs(panAngle, PAN_MIN, PAN_MAX));
  setServoPulseUs(TILT_CH, mapAngleToUs(tiltAngle, TILT_MIN, TILT_MAX));
  digitalWrite(LED_PIN, HIGH);
  digitalWrite(LED_PIN, LOW);
  if (emitAck) printServoAck();
}

bool startBaseMoveToCount(long targetCount, float ackDeg) {
  if (baseBusy) {
    printBaseBusy();
    return false;
  }
  float targetDeg = countsToDeg(targetCount);
  if (fabs(targetDeg) > MAX_ABS_BASE_DEG) {
    Serial.println(F("ERR B limit"));
    return false;
  }
  moveTargetCount = targetCount;
  ackBaseDeg = ackDeg;
  moveStartCount = readEncoderCount();
  lastProgressCount = moveStartCount;
  lastErrorAbs = labs(targetCount - moveStartCount);
  moveMaxTravelCounts = lastErrorAbs + ENCODER_TOLERANCE * 3;
  moveActive = true;
  baseBusy = true;
  moveStartMs = millis();
  lastProgressMs = moveStartMs;
  overshootCycles = 0;
  return true;
}

bool startBaseRelativeDeg(float deltaDeg) {
  long pos = readEncoderCount();
  long deltaCounts = (long)(deltaDeg * countsPerBaseDeg * encoderSign);
  const long MIN_MOVE_COUNTS = 30;
  if (deltaCounts != 0 && labs(deltaCounts) < MIN_MOVE_COUNTS) {
    deltaCounts = (deltaCounts > 0 ? MIN_MOVE_COUNTS : -MIN_MOVE_COUNTS);
  }
  return startBaseMoveToCount(pos + deltaCounts, deltaDeg);
}

bool startBaseJog(int pwm, unsigned long durationMs) {
  if (baseBusy) {
    printBaseBusy();
    return false;
  }
  pwm = constrain(pwm, -PWM_MAX, PWM_MAX);
  durationMs = constrain(durationMs, 1UL, JOG_MAX_MS);
  if (pwm == 0) {
    Serial.println(F("ERR J zero"));
    return false;
  }
  jogActive = true;
  baseBusy = true;
  jogEndMs = millis() + durationMs;
  motorDrive(pwm);
  Serial.println(F("OK J"));
  return true;
}

bool startBaseAbsoluteDeg(float deg) {
  return startBaseMoveToCount(degToCounts(deg), deg);
}

void zeroBaseReference() {
  if (baseBusy) {
    printBaseBusy();
    return;
  }
  portENTER_CRITICAL(&encoderMux);
  encoderCount = 0;
  zeroOffset = 0;
  syncEncoderState();
  portEXIT_CRITICAL(&encoderMux);
  moveTargetCount = 0;
  ackBaseDeg = 0.0f;
  Serial.println(F("OK Z"));
}

void printStatus() {
  long pos = readEncoderCount();
  Serial.print(F("POS "));
  Serial.print(pos);
  Serial.print(F(" DEG "));
  Serial.print(countsToDeg(pos), 2);
  Serial.print(F(" CPD "));
  Serial.print(countsPerBaseDeg, 3);
  Serial.print(F(" BUSY "));
  Serial.println(baseBusy ? 1 : 0);
}

void runSweep() {
  const int steps = 28;
  for (int i = 0; i <= steps; i++) {
    float pan = PAN_MIN + (PAN_MAX - PAN_MIN) * ((float)i / (float)steps);
    writeAngles(pan, TILT_CENTER, true);
    delay(80);
  }
  for (int i = 0; i <= steps; i++) {
    float tilt = TILT_MIN + (TILT_MAX - TILT_MIN) * ((float)i / (float)steps);
    writeAngles(PAN_CENTER, tilt, true);
    delay(80);
  }
  writeAngles(PAN_CENTER, TILT_CENTER, true);
}

void handleLine() {
  if (lineLen == 0) return;
  lineBuffer[lineLen] = '\0';

  if (lineLen == 1) {
    if (lineBuffer[0] == 'H') {
      Serial.println(F("READY"));
      lineLen = 0;
      return;
    }
    if (lineBuffer[0] == '?') {
      printStatus();
      lineLen = 0;
      return;
    }
    if (lineBuffer[0] == 'S') {
      runSweep();
      lineLen = 0;
      return;
    }
    if (lineBuffer[0] == 'Z') {
      zeroBaseReference();
      lineLen = 0;
      return;
    }
    if (lineBuffer[0] == 'X') {
      stopBaseMotion();
      lineLen = 0;
      return;
    }
  }

  float pan = panAngle;
  float tilt = tiltAngle;
  float baseDeg = 0.0f;
  float cpdValue = 0.0f;
  bool hasPan = false;
  bool hasTilt = false;
  bool hasBase = false;
  bool baseRelative = false;
  bool hasCpd = false;
  bool hasEncSign = false;
  bool hasJog = false;
  int jogPwm = 0;
  unsigned long jogMs = 250;
  float encSignValue = 1.0f;

  char buf[LINE_BUF_SIZE];
  strncpy(buf, lineBuffer, LINE_BUF_SIZE - 1);
  buf[LINE_BUF_SIZE - 1] = '\0';
  char *token = strtok(buf, " ");
  while (token != NULL) {
    char c = token[0];
    if (c == 'P' || c == 'p') {
      pan = atof(token + 1);
      hasPan = true;
    } else if (c == 'T' || c == 't') {
      tilt = atof(token + 1);
      hasTilt = true;
    } else if (c == 'B' || c == 'b') {
      hasBase = true;
      baseRelative = token[1] == '+' || token[1] == '-';
      baseDeg = atof(token + 1);
    } else if (c == 'C' || c == 'c') {
      hasCpd = true;
      cpdValue = atof(token + 1);
    } else if (c == 'E' || c == 'e') {
      hasEncSign = true;
      encSignValue = atof(token + 1);
    } else if (c == 'J' || c == 'j') {
      hasJog = true;
      jogPwm = atoi(token + 1);
    } else if (c == 'M' || c == 'm') {
      jogMs = (unsigned long)atol(token + 1);
    }
    token = strtok(NULL, " ");
  }

  if (hasCpd) {
    if (setCountsPerBaseDeg(cpdValue)) {
      Serial.print(F("OK C"));
      Serial.println(countsPerBaseDeg, 3);
    } else {
      Serial.println(F("ERR C range"));
    }
  }
  if (hasEncSign) {
    if (setEncoderSign(encSignValue)) {
      Serial.print(F("OK E"));
      Serial.println(encoderSign, 0);
    } else {
      Serial.println(F("ERR E range"));
    }
  }
  if (hasPan || hasTilt) {
    writeAngles(pan, tilt, true);
  }
  if (hasBase) {
    if (baseRelative) {
      startBaseRelativeDeg(baseDeg);
    } else {
      startBaseAbsoluteDeg(baseDeg);
    }
  }
  if (hasJog) {
    startBaseJog(jogPwm, jogMs);
  }
  if (!hasPan && !hasTilt && !hasBase && !hasCpd && !hasEncSign && !hasJog) {
    Serial.println(F("ERR unknown"));
  }
  lineLen = 0;
}

void updateBaseJog() {
  if (!jogActive) return;
  if ((long)(millis() - jogEndMs) >= 0) {
    stopBaseMotion();
  }
}

void updateBaseMotor() {
  if (!moveActive) return;

  unsigned long now = millis();
  if (now - moveStartMs > MOVE_TIMEOUT_MS) {
    stopBaseMotion();
    Serial.println(F("ERR B timeout"));
    return;
  }

  long pos = readEncoderCount();
  long error = moveTargetCount - pos;
  long errAbs = labs(error);
  long traveled = labs(pos - moveStartCount);
  float posDeg = countsToDeg(pos);

  if (fabs(posDeg) > (MAX_ABS_BASE_DEG + ABS_LIMIT_MARGIN_DEG)) {
    stopBaseMotion();
    Serial.println(F("ERR B limit"));
    return;
  }

  long overshootAllowance = max(
    OVERSHOOT_ALLOW_MIN_COUNTS,
    (long)((float)moveMaxTravelCounts * OVERSHOOT_ALLOW_RATIO)
  );
  long plannedDelta = moveTargetCount - moveStartCount;
  bool passedTarget = false;
  if (plannedDelta > 0) {
    passedTarget = pos > (moveTargetCount + ENCODER_TOLERANCE);
  } else if (plannedDelta < 0) {
    passedTarget = pos < (moveTargetCount - ENCODER_TOLERANCE);
  }
  if (traveled > moveMaxTravelCounts + overshootAllowance && passedTarget) {
    overshootCycles++;
  } else {
    overshootCycles = 0;
  }
  if (overshootCycles >= OVERSHOOT_CONFIRM_CYCLES) {
    stopBaseMotion();
    Serial.println(F("ERR B overshoot"));
    return;
  }

  if (errAbs <= ENCODER_TOLERANCE) {
    stopBaseMotion();
    pendingBaseAck = true;
    return;
  }

  // Short breakaway pulse to overcome static friction at move start.
  if (now - moveStartMs < MOVE_KICK_MS) {
    int kick = max(FINE_MIN_PWM, MOVE_KICK_PWM);
    motorDrive(error > 0 ? kick : -kick);
    return;
  }

  if (errAbs < lastErrorAbs - 1 || labs(pos - lastProgressCount) >= STALL_MIN_PROGRESS) {
    lastProgressCount = pos;
    lastProgressMs = now;
    lastErrorAbs = errAbs;
  } else if (now - lastProgressMs > STALL_MS) {
    stopBaseMotion();
    Serial.println(F("ERR B stall"));
    return;
  }

  int pwmCap = (int)constrain((float)errAbs * FINE_PWM_PER_COUNT, (float)FINE_MIN_PWM, (float)COARSE_PWM);

  if (errAbs > COARSE_ERR_COUNTS) {
    motorDrive((error > 0 ? pwmCap : -pwmCap));
    return;
  }

  int out = (int)(FINE_KP * (float)error);
  if (out == 0) {
    out = (error > 0 ? FINE_MIN_PWM : -FINE_MIN_PWM);
  } else if (labs(out) < FINE_MIN_PWM) {
    out = (out > 0 ? FINE_MIN_PWM : -FINE_MIN_PWM);
  }
  if (labs(out) > pwmCap) {
    out = (out > 0 ? pwmCap : -pwmCap);
  }
  motorDrive(out);
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  pinMode(ENC_A_PIN, INPUT);
  pinMode(ENC_B_PIN, INPUT);
  pinMode(MOTOR_AIN1_PIN, OUTPUT);
  pinMode(MOTOR_AIN2_PIN, OUTPUT);
  syncEncoderState();

  ledcSetup(LEDC_PWM_CHANNEL, LEDC_FREQ_HZ, LEDC_RES_BITS);
  ledcAttachPin(MOTOR_PWM_PIN, LEDC_PWM_CHANNEL);
  motorStop();

  Serial.begin(115200);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(100000);
  Wire.setTimeOut(50);
  Wire.beginTransmission(0x40);
  if (Wire.endTransmission() == 0) {
    pwm.begin();
    pwm.setOscillatorFrequency(27000000);
    pwm.setPWMFreq(50);
    delay(10);
    pcaReady = true;
  } else {
    Serial.println(F("WARN PCA9685 not found at 0x40"));
  }

  panAngle = PAN_CENTER;
  tiltAngle = TILT_CENTER;
  zeroOffset = 0;
  Serial.println(F("FW head_servo_v5_base"));
  Serial.println(F("READY"));
  Serial.flush();
}

void loop() {
  serviceEncoderLocked();

  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      handleLine();
    } else if (lineLen < LINE_BUF_SIZE - 1) {
      lineBuffer[lineLen++] = c;
    }
  }

  updateBaseMotor();
  updateBaseJog();

  if (pendingBaseAck) {
    pendingBaseAck = false;
    printBaseAck(ackBaseDeg);
  }

  delay(1);
}
