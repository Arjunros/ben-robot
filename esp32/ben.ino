/*
 * BEN PRO MAX — ESP32-S3 Firmware  (v7)
 * I2C: PCA9685 only (elbows + fingers) · TF-Luna on UART2 (GPIO 5/6)
 * Direct servos: lateral L/R, head · BTN7960 motors
 * Latch power · battery monitor (12.8V LiFePO4) · multi-function button
 *
 * v7  — PCA9685 found the real culprit. The pins were right all along
 *        (SDA=14 SCL=21 confirmed) and so was the power sequencing. The
 *        difference between the bench sketch that WORKED and this firmware
 *        was the three peripherals initialised before Wire.begin():
 *            pinMode(BUTTON_PIN=20, INPUT_PULLDOWN)
 *            PiSerial.begin(..., 17, 18)     UART1
 *            LunaSerial.begin(..., 6, 5)     UART2
 *        One of them disturbs the I2C pins through the GPIO matrix. So v7:
 *          · brings up I2C and the PCA9685 FIRST, matching the order of the
 *            bench sketch that worked
 *          · calls pcaAlive() after each subsequent peripheral, so if one of
 *            them does break the bus the log names it in a single boot
 *            instead of needing three reflashes to bisect
 *          · re-initialises the PCA automatically if something knocks it out
 *
 * v6  — PCA9685 POWER SEQUENCING. v5 still found nothing on the bus, while
 *        a bench sketch using the SAME pins and 400kHz worked. The difference
 *        was timing, not speed:
 *          bench:  latch HIGH -> delay(1200) -> Wire.begin -> scan  = found
 *          v5:     latch HIGH -> ~140ms      -> Wire.begin -> scan  = nothing
 *        If the PCA9685's VCC comes off the latched rail it is still powering
 *        up and holding its own reset when v5 scanned. So v6:
 *          · raises the latch as the very FIRST statement in setup()
 *          · waits 1200ms before touching I2C
 *          · retries the handshake up to 5 times, 400ms apart, because a
 *            marginal power-up succeeds on the second attempt and a robot
 *            that boots with dead arms one time in five is worse than one
 *            that waits half a second longer
 *          · only runs the full 126-address scan if all retries failed, so a
 *            healthy boot is no longer delayed by a 6-second scan
 *
 * v5  — PCA9685 fixes after elbow/finger servos were found dead while a
 *        standalone test sketch drove the same four channels correctly:
 *          · I2C bus dropped 400kHz -> 100kHz. NOTE: this turned out NOT to
 *            be the fault — the bench sketch runs 400kHz happily. Kept at
 *            100kHz anyway: nothing here needs the bandwidth, and the slower
 *            bus is more tolerant of long servo-harness wiring.
 *          · pca.begin() return value is now checked. Discarding it meant a
 *            dead I2C bus looked identical to a healthy one.
 *          · new PCATEST command sweeps ch2/3/7/8 directly, bypassing
 *            mapJoint / targets / updateServos, to separate "PCA broken"
 *            from "command never arrived".
 *          · new I2CSCAN command reports what is on the bus at runtime.
 *          · LEDC timers: only 0-1 reserved for ESP32Servo, leaving 2-3 for
 *            the four motor channels. Allocating all four starved
 *            ledcAttach() and could silently kill motor PWM.
 * v4  — battery curve replaced with the electronics team's final table
 * v3  — smooth boot/shutdown homing, pose memory in flash, sub-degree
 *        motion, non-blocking battery sampling, PCA oscillator calibration
 *
 * SERVO RANGE MODEL
 *   Each joint declares the angle at app value 0 (OUT_A) and at app
 *   value 2000 (OUT_B).  OUT_A > OUT_B simply runs the joint in reverse.
 *   HOME is a separate park angle.
 *
 * Board: "ESP32S3 Dev Module" · USB CDC On Boot: Enabled
 * Libraries: Adafruit PWM Servo Driver · ESP32Servo
 *
 * DIAGNOSTIC COMMANDS (type into Serial Monitor, or send over the Pi link)
 *   PCATEST   sweep the four PCA channels directly
 *   I2CSCAN   list I2C devices
 *   BATT?     report pack voltage and percent
 */

#include <Wire.h>
#include <ESP32Servo.h>
#include <Adafruit_PWMServoDriver.h>
#include <Preferences.h>

// ── DIRECT SERVOS ──────────────────────────────────────────
#define LATERAL_L_PIN  1
#define LATERAL_R_PIN  2
#define HEAD_LR_PIN    7

// ── I2C (PCA9685 only) ─────────────────────────────────────
#define I2C_SDA        14
#define I2C_SCL        21
// 100kHz, not 400kHz. See the v5 notes above — this was the fix.
#define I2C_CLOCK      100000UL
Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(0x40);
#define PCA_ADDR       0x40
#define SERVO_FREQ     50
#define SERVO_MIN_US   500
#define SERVO_MAX_US   2400
#define PCA_OSC_FREQ   27000000UL   // tune 25.0M-27.5M if 50Hz is off
// Time between raising the power latch and first touching I2C. The PCA9685
// needs its rail up and its reset released before it will answer. Raise this
// if the boot log still reports "no response at 0x40".
#define PCA_POWERUP_MS 1200
#define PCA_RETRIES    5
#define PCA_RETRY_MS   400
#define CH_ELBOW_L     7
#define CH_ELBOW_R     8
#define CH_FINGER_L    2
#define CH_FINGER_R    3
#define NUM_CH         9

bool pcaOk = false;                 // set by begin(); reported in status

// Which PCA channels actually have a servo on them.
const bool PCA_USED[NUM_CH] = {false,false,true,true,false,false,false,true,true};

// ── PER-JOINT TRAVEL + HOME (degrees) ──────────────────────
//                         ch:  0    1   fL   fR    4    5    6   eL   eR
float PCA_OUT_A[NUM_CH] = {  0,   0, 180,   0,   0,   0,   0,  90,  90};
float PCA_OUT_B[NUM_CH] = {180, 180,   0, 180, 180, 180, 180,   0, 180};
float PCA_HOME[NUM_CH]  = { 90,  90, 180,   0,  90,  90,  90,  90,  90};

// Direct GPIO servos — mirror-mounted laterals run opposite ways
#define LAT_L_A    180.0f      // app 0
#define LAT_L_B      0.0f      // app 2000
#define LAT_L_HOME 180.0f

#define LAT_R_A      0.0f      // mirrored
#define LAT_R_B    180.0f
#define LAT_R_HOME   0.0f

#define HEAD_A      45.0f
#define HEAD_B     135.0f
#define HEAD_HOME   90.0f

// ── MOTION SMOOTHNESS ──────────────────────────────────────
#define SERVO_TICK_MS      15
#define STEP_MIN_DEG      0.4f
#define STEP_MAX_DEG      4.0f
#define ARRIVE_DEG        0.20f
#define HOMING_SPEED        18
#define HOME_TIMEOUT_MS   6000

// ── MOTORS (BTN7960) ───────────────────────────────────────
#define RPWM_L  35
#define LPWM_L  36
#define RPWM_R  37
#define LPWM_R  38
#define EN_L    16
#define EN_R    15
#define PWM_FREQ       1000
#define PWM_RES        8
#define MIN_MOVE_PWM   120
#define MAX_MOVE_PWM   255
#define DEFAULT_BASE_SPEED   50
#define DEFAULT_SERVO_SPEED  50

// ── PI SERIAL LINK (UART1) ─────────────────────────────────
#define PI_TX_PIN      18     // → Pi RXD
#define PI_RX_PIN      17     // ← Pi TXD
HardwareSerial PiSerial(1);

// ── TF-LUNA UART (UART2) ───────────────────────────────────
#define LUNA_TX_PIN    10 //5
#define LUNA_RX_PIN    11  //6
HardwareSerial LunaSerial(2);

// ── LATCH + BUTTON ─────────────────────────────────────────
#define LATCH_PIN      8
// NOTE: GPIO19/20 are the ESP32-S3's native USB D-/D+ pins. With "USB CDC On
// Boot: Enabled" this can cause flaky serial or phantom button presses. If you
// see either, move the button to a free pin such as 9 or 10.
#define BUTTON_PIN     20
#define LONG_PRESS_MS  3000
#define SHORT_MIN_MS   100

// ══════════════════════════════════════════════════════════
// BATTERY  (IO12, 10K/1K divider, 12.8V LiFePO4 pack)
// ══════════════════════════════════════════════════════════
#define BATT_ADC_PIN   12
#define BATT_SAMPLES   16
#define BATT_PERIOD_MS 3000

// STEP 1: raw ADC count → pack volts.
#define RAW_HI   1491.0f
#define VOLT_HI  14.30f
#define RAW_LO   1197.0f
#define VOLT_LO  11.00f

// STEP 2: pack volts → percent.  Electronics team's final curve.
#define BATT_POINTS 8
const float BATT_V[BATT_POINTS]   = {14.00f,13.50f,13.00f,12.50f,12.00f,11.50f,11.00f,10.50f};
const int   BATT_PCT[BATT_POINTS] = {  100,    80,    60,    50,    40,    30,    10,     0};

// ── TF-LUNA thresholds ─────────────────────────────────────
#define OBSTACLE_DIST  50
#define DETECT_DIST    150
#define COOLDOWN_MS    10000

Servo lateralL, lateralR, headServo;
Preferences prefs;
void processCommand(char* buf);

// ── STATE ──────────────────────────────────────────────────
int  motorSpeed = MAX_MOVE_PWM;
int  servoSpeed = DEFAULT_SERVO_SPEED;
int  lunaDistance = 999;
bool obstacleAhead = false;
char currentDir[12] = "stop";
char savedDir[12]  = "stop";
unsigned long lastGreeted=0, lastServoUpdate=0, lastCmdTime=0;
bool robotMoving=false, hardwareEnabled=true, shutdownTriggered=false;
unsigned long btnPressStart=0;
bool btnWasPressed=false;

float chCurrent[NUM_CH], chTarget[NUM_CH];
float latCurrentL=LAT_L_HOME, latTargetL=LAT_L_HOME;
float latCurrentR=LAT_R_HOME, latTargetR=LAT_R_HOME;
float headCurrent=HEAD_HOME,  headTarget=HEAD_HOME;

float battPackV = 0;
int   battPercent = 100;

// ── LOW-LEVEL SERVO WRITES (microsecond resolution) ────────
static inline float degToUs(float deg) {
  deg = constrain(deg, 0.0f, 180.0f);
  return SERVO_MIN_US + (SERVO_MAX_US - SERVO_MIN_US) * deg / 180.0f;
}

static inline int usToTicks(float us) {
  return (int)(us * 4096.0f / 20000.0f + 0.5f);   // 20000us period at 50Hz
}

void pcaWriteDeg(int ch, float deg) {
  if (ch < 0 || ch >= NUM_CH) return;
  if (!PCA_USED[ch]) return;
  pca.setPWM(ch, 0, usToTicks(degToUs(deg)));
}

static inline void writeLateralL(float deg){ lateralL.writeMicroseconds((int)(degToUs(deg)+0.5f)); }
static inline void writeLateralR(float deg){ lateralR.writeMicroseconds((int)(degToUs(deg)+0.5f)); }
static inline void writeHead    (float deg){ headServo.writeMicroseconds((int)(degToUs(deg)+0.5f)); }

// ── DIAGNOSTICS ────────────────────────────────────────────
void i2cScan() {
  Serial.println("[I2C] scanning...");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("[I2C]   0x%02X%s\n", a,
                    (a == PCA_ADDR) ? "  <-- PCA9685" : "");
      found++;
    }
  }
  if (!found) {
    Serial.println("[I2C]   nothing found.");
    Serial.println("[I2C]   check SDA=14 SCL=21, common GND, and VCC.");
    Serial.println("[I2C]   VCC powers the CHIP; V+ powers the SERVOS. With");
    Serial.println("[I2C]   only VCC the board answers I2C and moves nothing.");
  }
}

// Cheap liveness probe: does 0x40 still ACK? Used after each peripheral
// comes up, so a peripheral that clobbers the I2C pins is named in the log
// rather than having to be found by commenting lines out one at a time.
bool pcaAlive(const char* stage) {
  Wire.beginTransmission(PCA_ADDR);
  bool ok = (Wire.endTransmission() == 0);
  if (!ok) {
    Serial.printf("[PCA] *** BUS LOST after: %s ***\n", stage);
    Serial.println("[PCA] that step is the culprit — it is disturbing "
                   "SDA=14 / SCL=21");
  } else {
    Serial.printf("[PCA] alive after %s\n", stage);
  }
  return ok;
}

// Sweeps the four real channels directly: no mapJoint, no targets, no
// updateServos(). If these move but the app sliders do not, the PCA is fine
// and the fault is in command routing or in the target system.
void pcaSelfTest() {
  Serial.println("[PCATEST] sweeping ch2,3,7,8 directly");
  const int chs[4]      = {CH_FINGER_L, CH_FINGER_R, CH_ELBOW_L, CH_ELBOW_R};
  const char* names[4]  = {"finger L", "finger R", "elbow L", "elbow R"};
  for (int i = 0; i < 4; i++) {
    Serial.printf("[PCATEST] ch%d %s\n", chs[i], names[i]);
    // Only the middle of the range: a servo already on its mechanical stop
    // buzzes and strains at the extremes, and that is how gears get stripped.
    for (float us = 1200; us <= 1700; us += 10) {
      pca.setPWM(chs[i], 0, usToTicks(us)); delay(20);
    }
    for (float us = 1700; us >= 1200; us -= 10) {
      pca.setPWM(chs[i], 0, usToTicks(us)); delay(20);
    }
    pca.setPWM(chs[i], 0, usToTicks(1450));
    delay(300);
  }
  Serial.println("[PCATEST] done — restoring targets");
  // Put the servos back where the motion system thinks they are, or the next
  // updateServos() tick will jerk them.
  for (int ch = 0; ch < NUM_CH; ch++)
    if (PCA_USED[ch]) pcaWriteDeg(ch, chCurrent[ch]);
}

// ── HELPERS ────────────────────────────────────────────────
int appSpeedToPWM(int v) {
  v = constrain(v, 0, 100);
  if (v == 0) return 0;
  return map(v, 1, 100, MIN_MOVE_PWM, MAX_MOVE_PWM);
}

float mapJoint(int value, float outA, float outB) {
  value = constrain(value, 0, 2000);
  float deg = outA + (outB - outA) * (value / 2000.0f);
  float lo = min(outA, outB), hi = max(outA, outB);
  return constrain(deg, lo, hi);
}

void setMotor(int pinFwd, int pinBwd, int speed, bool fwd) {
  if (fwd) { ledcWrite(pinFwd, speed); ledcWrite(pinBwd, 0); }
  else     { ledcWrite(pinFwd, 0);     ledcWrite(pinBwd, speed); }
}

void stopMotors() {
  digitalWrite(EN_L, LOW); digitalWrite(EN_R, LOW);
  ledcWrite(RPWM_L,0); ledcWrite(LPWM_L,0);
  ledcWrite(RPWM_R,0); ledcWrite(LPWM_R,0);
  strcpy(currentDir, "stop");
  robotMoving = false;
  Serial.println("[MOTOR] STOP");
}

void executeMove(const char* dir) {
  strncpy(currentDir, dir, 11); currentDir[11]='\0';
  if (strcmp(dir,"stop") != 0) {
    strncpy(savedDir, dir, 11); savedDir[11]='\0';
    robotMoving = true;
  } else robotMoving = false;

  if (strcmp(dir,"forward")==0 && obstacleAhead && hardwareEnabled) {
    stopMotors(); strcpy(savedDir,"stop");
    PiSerial.print("BLOCKED:"); PiSerial.println(lunaDistance);
    return;
  }
  if (strcmp(dir,"stop")==0) { stopMotors(); return; }

  digitalWrite(EN_L, HIGH); digitalWrite(EN_R, HIGH);
  if      (strcmp(dir,"forward")==0)  { setMotor(RPWM_L,LPWM_L,motorSpeed,true);  setMotor(RPWM_R,LPWM_R,motorSpeed,true);  }
  else if (strcmp(dir,"backward")==0) { setMotor(RPWM_L,LPWM_L,motorSpeed,false); setMotor(RPWM_R,LPWM_R,motorSpeed,false); }
  else if (strcmp(dir,"left")==0)     { setMotor(RPWM_L,LPWM_L,motorSpeed,false); setMotor(RPWM_R,LPWM_R,motorSpeed,true);  }
  else if (strcmp(dir,"right")==0)    { setMotor(RPWM_L,LPWM_L,motorSpeed,true);  setMotor(RPWM_R,LPWM_R,motorSpeed,false); }
  Serial.printf("[MOTOR] %s pwm=%d\n", dir, motorSpeed);
}

// ── SMOOTH SERVO UPDATE ────────────────────────────────────
static inline bool stepToward(float &cur, float tgt, float step) {
  float d = tgt - cur;
  if (fabsf(d) <= ARRIVE_DEG) { if (cur != tgt) { cur = tgt; return true; } return false; }
  cur += (d > 0) ? min(step, d) : max(-step, d);
  return true;
}

void updateServos() {
  unsigned long now = millis();
  if (now - lastServoUpdate < SERVO_TICK_MS) return;
  lastServoUpdate = now;

  float step = STEP_MIN_DEG +
               (STEP_MAX_DEG - STEP_MIN_DEG) * (constrain(servoSpeed,0,100) / 100.0f);

  for (int ch = 0; ch < NUM_CH; ch++) {
    if (!PCA_USED[ch]) continue;
    if (stepToward(chCurrent[ch], chTarget[ch], step)) pcaWriteDeg(ch, chCurrent[ch]);
  }
  if (stepToward(latCurrentL, latTargetL, step)) writeLateralL(latCurrentL);
  if (stepToward(latCurrentR, latTargetR, step)) writeLateralR(latCurrentR);
  if (stepToward(headCurrent, headTarget, step)) writeHead(headCurrent);
}

bool servosAtTarget() {
  for (int ch = 0; ch < NUM_CH; ch++)
    if (PCA_USED[ch] && fabsf(chTarget[ch] - chCurrent[ch]) > ARRIVE_DEG) return false;
  return fabsf(latTargetL - latCurrentL) <= ARRIVE_DEG &&
         fabsf(latTargetR - latCurrentR) <= ARRIVE_DEG &&
         fabsf(headTarget - headCurrent) <= ARRIVE_DEG;
}

// ── POSITION MEMORY (survives power cut) ───────────────────
void savePositions() {
  prefs.begin("benpose", false);
  prefs.putBytes("pca", chCurrent, sizeof(chCurrent));
  prefs.putFloat("latL", latCurrentL);
  prefs.putFloat("latR", latCurrentR);
  prefs.putFloat("head", headCurrent);
  prefs.putBool("valid", true);
  prefs.end();
  Serial.println("[POSE] saved");
}

void restorePositions() {
  prefs.begin("benpose", true);
  bool valid = prefs.getBool("valid", false);
  if (valid && prefs.getBytesLength("pca") == sizeof(chCurrent)) {
    prefs.getBytes("pca", chCurrent, sizeof(chCurrent));
    latCurrentL = prefs.getFloat("latL", LAT_L_HOME);
    latCurrentR = prefs.getFloat("latR", LAT_R_HOME);
    headCurrent = prefs.getFloat("head", HEAD_HOME);
    Serial.println("[POSE] restored from flash");
  } else {
    for (int ch = 0; ch < NUM_CH; ch++) chCurrent[ch] = PCA_HOME[ch];
    latCurrentL = LAT_L_HOME; latCurrentR = LAT_R_HOME; headCurrent = HEAD_HOME;
    Serial.println("[POSE] no saved pose — assuming home");
  }
  prefs.end();
  for (int ch = 0; ch < NUM_CH; ch++) chTarget[ch] = chCurrent[ch];
  latTargetL = latCurrentL; latTargetR = latCurrentR; headTarget = headCurrent;
}

// ── HOMING ─────────────────────────────────────────────────
void setHomeTargets() {
  for (int ch = 0; ch < NUM_CH; ch++) chTarget[ch] = PCA_HOME[ch];
  latTargetL = LAT_L_HOME;
  latTargetR = LAT_R_HOME;
  headTarget = HEAD_HOME;
}

void homeServos() {                     // app HOME button — glides
  setHomeTargets();
  Serial.println("[SERVO] homing (smooth)");
}

void homeServosSlow(const char* why) {  // boot / shutdown — slow, blocking
  int saved = servoSpeed;
  servoSpeed = HOMING_SPEED;
  setHomeTargets();
  Serial.printf("[SERVO] slow home (%s)...\n", why);
  unsigned long t0 = millis();
  while (millis() - t0 < HOME_TIMEOUT_MS) {
    updateServos();
    if (servosAtTarget()) break;
    delay(2);
  }
  servoSpeed = saved;
  Serial.println("[SERVO] HOMED");
}

// ── PART → TARGET ──────────────────────────────────────────
void moveServos(const char* part, int value, const char* hand) {
  bool doL = (strcmp(hand,"left")==0  || strcmp(hand,"both")==0);
  bool doR = (strcmp(hand,"right")==0 || strcmp(hand,"both")==0);

  if (strcmp(part,"lateral")==0) {
    if (doL) latTargetL = mapJoint(value, LAT_L_A, LAT_L_B);
    if (doR) latTargetR = mapJoint(value, LAT_R_A, LAT_R_B);
    Serial.printf("[SERVO] lateral(%s) app=%d -> L%.0f R%.0f\n",
                  hand, value, latTargetL, latTargetR);
    return;
  }
  if (strcmp(part,"headLR")==0 || strcmp(part,"head")==0) {
    headTarget = mapJoint(value, HEAD_A, HEAD_B);
    Serial.printf("[SERVO] head app=%d -> %.0f\n", value, headTarget);
    return;
  }
  if (strcmp(part,"elbow")==0) {
    if (doL) chTarget[CH_ELBOW_L] = mapJoint(value, PCA_OUT_A[CH_ELBOW_L], PCA_OUT_B[CH_ELBOW_L]);
    if (doR) chTarget[CH_ELBOW_R] = mapJoint(value, PCA_OUT_A[CH_ELBOW_R], PCA_OUT_B[CH_ELBOW_R]);
    // Reporting current as well as target: if target and current are already
    // equal, stepToward() does nothing and no PWM is written — which looks
    // exactly like a dead servo.
    Serial.printf("[SERVO] elbow(%s) app=%d -> L%.0f (at %.0f) R%.0f (at %.0f)%s\n",
                  hand, value, chTarget[CH_ELBOW_L], chCurrent[CH_ELBOW_L],
                  chTarget[CH_ELBOW_R], chCurrent[CH_ELBOW_R],
                  pcaOk ? "" : "  [PCA NOT OK]");
    return;
  }
  if (strstr(part,"finger") != NULL ||
      strcmp(part,"thumb")==0 || strcmp(part,"index")==0 ||
      strcmp(part,"middle")==0 || strcmp(part,"ring")==0 ||
      strcmp(part,"pinky")==0) {
    if (doL) chTarget[CH_FINGER_L] = mapJoint(value, PCA_OUT_A[CH_FINGER_L], PCA_OUT_B[CH_FINGER_L]);
    if (doR) chTarget[CH_FINGER_R] = mapJoint(value, PCA_OUT_A[CH_FINGER_R], PCA_OUT_B[CH_FINGER_R]);
    Serial.printf("[SERVO] fingers(%s) app=%d -> L%.0f (at %.0f) R%.0f (at %.0f)%s\n",
                  hand, value, chTarget[CH_FINGER_L], chCurrent[CH_FINGER_L],
                  chTarget[CH_FINGER_R], chCurrent[CH_FINGER_R],
                  pcaOk ? "" : "  [PCA NOT OK]");
    return;
  }
  Serial.printf("[IGNORE] %s\n", part);
}

// ── TF-LUNA (UART2, 9-byte frames) ─────────────────────────
void processDistance(int dist) {
  lunaDistance  = dist;
  obstacleAhead = hardwareEnabled && (dist < OBSTACLE_DIST);

  if (obstacleAhead && strcmp(currentDir,"forward")==0) {
    stopMotors(); strcpy(savedDir,"stop");
    PiSerial.print("BLOCKED:"); PiSerial.println(dist);
  }
  if (hardwareEnabled && dist < DETECT_DIST && !robotMoving &&
      strcmp(currentDir,"stop")==0 && millis()-lastGreeted > COOLDOWN_MS) {
    lastGreeted = millis();
    PiSerial.print("PERSON_DETECTED:"); PiSerial.println(dist);
  }
}

void readLuna() {
  static uint8_t frame[9];
  static int idx = 0;
  while (LunaSerial.available()) {
    uint8_t b = LunaSerial.read();
    if (idx == 0 && b != 0x59) continue;
    if (idx == 1 && b != 0x59) { idx = 0; continue; }
    frame[idx++] = b;
    if (idx == 9) {
      idx = 0;
      uint8_t sum = 0;
      for (int i = 0; i < 8; i++) sum += frame[i];
      if (sum != frame[8]) continue;
      int dist     = frame[2] | (frame[3] << 8);
      int strength = frame[4] | (frame[5] << 8);
      if (dist <= 0 || dist > 800) continue;
      if (strength < 100) continue;
      processDistance(dist);
    }
  }
}

// ══════════════════════════════════════════════════════════
// BATTERY — non-blocking sampling, table-driven percentage
// ══════════════════════════════════════════════════════════
float rawToPackVolts(float raw) {
  return VOLT_LO + (raw - RAW_LO) * (VOLT_HI - VOLT_LO) / (RAW_HI - RAW_LO);
}

int packVoltsToPercent(float v) {
  if (v >= BATT_V[0])              return BATT_PCT[0];               // full
  if (v <= BATT_V[BATT_POINTS-1])  return BATT_PCT[BATT_POINTS-1];   // empty
  for (int i = 1; i < BATT_POINTS; i++) {
    if (v >= BATT_V[i]) {
      float span = BATT_V[i-1] - BATT_V[i];
      float frac = (v - BATT_V[i]) / span;
      return BATT_PCT[i] + (int)(frac * (BATT_PCT[i-1] - BATT_PCT[i]) + 0.5f);
    }
  }
  return 0;
}

void readBattery() {
  static unsigned long lastBatt = 0, lastSample = 0;
  static int   samples[BATT_SAMPLES];
  static int   n = 0;
  static bool  primed = false;
  static float filtV = 0;

  if (millis() - lastBatt < BATT_PERIOD_MS) return;   // between bursts
  if (millis() - lastSample < 2) return;              // ~2 ms apart
  lastSample = millis();

  samples[n++] = analogRead(BATT_ADC_PIN);
  if (n < BATT_SAMPLES) return;                       // still collecting
  n = 0;
  lastBatt = millis();

  // median rejects spikes from motor / servo current draw
  for (int i = 1; i < BATT_SAMPLES; i++)
    for (int j = i; j > 0 && samples[j] < samples[j-1]; j--) {
      int t = samples[j]; samples[j] = samples[j-1]; samples[j-1] = t;
    }
  float raw   = samples[BATT_SAMPLES/2];
  float packV = rawToPackVolts(raw);

  if (!primed) { filtV = packV; primed = true; }
  else         filtV += (packV - filtV) * 0.30f;      // gentle low-pass

  battPackV   = filtV;
  battPercent = packVoltsToPercent(filtV);
}

// ── LATCH SHUTDOWN ─────────────────────────────────────────
void doLatchShutdown() {
  if (shutdownTriggered) return;
  shutdownTriggered = true;
  Serial.println("[LATCH] Shutdown sequence!");
  stopMotors();
  homeServosSlow("shutdown");
  savePositions();
  PiSerial.println("SHUTDOWN");
  for (int i = 15; i > 0; i--) { Serial.printf("[LATCH] %ds\n", i); delay(1000); }
  digitalWrite(LATCH_PIN, LOW);
}

// ── MULTI-FUNCTION BUTTON ──────────────────────────────────
void checkButton() {
  if (shutdownTriggered) return;
  bool pressed = (digitalRead(BUTTON_PIN) == HIGH);
  if (pressed && !btnWasPressed) {
    btnPressStart = millis(); btnWasPressed = true;
  }
  else if (pressed && btnWasPressed &&
           millis() - btnPressStart >= LONG_PRESS_MS) {
    Serial.println("[BTN] Long press — shutdown!");
    doLatchShutdown();
  }
  else if (!pressed && btnWasPressed) {
    unsigned long held = millis() - btnPressStart;
    btnWasPressed = false;
    if (held >= SHORT_MIN_MS && held < LONG_PRESS_MS) {
      Serial.println("[BTN] Short press — factory reset!");
      PiSerial.println("FACTORY_RESET");
    }
  }
}

// ── SERIAL HANDLER ─────────────────────────────────────────
// Reads BOTH links: the Pi on UART1, and USB serial so you can type
// diagnostics straight into the Arduino Serial Monitor.
void handleSerial() {
  static char buf[96];
  static int  len = 0;
  while (PiSerial.available()) {
    char c = PiSerial.read();
    if (c == '\n' || c == '\r') {
      if (len > 0) { buf[len] = '\0'; processCommand(buf); len = 0; }
      continue;
    }
    if (c < 32 || c > 126) continue;
    if (len < 95) buf[len++] = c; else len = 0;
  }

  static char ubuf[96];
  static int  ulen = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (ulen > 0) { ubuf[ulen] = '\0'; processCommand(ubuf); ulen = 0; }
      continue;
    }
    if (c < 32 || c > 126) continue;
    if (ulen < 95) ubuf[ulen++] = c; else ulen = 0;
  }
}

void processCommand(char* buf) {
  Serial.print("[CMD] "); Serial.println(buf);

  if (strncmp(buf,"MOVE:",5)==0) { lastCmdTime=millis(); executeMove(buf+5); }
  else if (strncmp(buf,"SPEED:",6)==0) {
    int v = constrain(atoi(buf+6), 0, 100);
    motorSpeed = appSpeedToPWM(v);
    Serial.printf("[SPEED] base %d%% -> pwm %d\n", v, motorSpeed);
    if (robotMoving) executeMove(currentDir);
  }
  else if (strncmp(buf,"TOPSPEED:",9)==0) {
    servoSpeed = constrain(atoi(buf+9), 0, 100);
    Serial.printf("[SPEED] servo %d%%\n", servoSpeed);
  }
  else if (strncmp(buf,"HOME",4)==0) homeServos();
  else if (strcmp(buf,"RESUME")==0) executeMove(savedDir);
  else if (strncmp(buf,"POS:",4)==0) {
    char* p1 = strchr(buf+4, ':'); if (!p1) return;
    char* p2 = strchr(p1+1, ':');  if (!p2) return;
    *p1='\0'; *p2='\0';
    moveServos(buf+4, atoi(p1+1), p2+1);
  }
  else if (strncmp(buf,"EYES:",5)==0) {
    Serial.printf("[EYES] %s\n", buf+5);
  }
  else if (strncmp(buf,"PCATEST",7)==0) pcaSelfTest();
  else if (strncmp(buf,"I2CSCAN",7)==0) i2cScan();
  else if (strcmp(buf,"BATT?")==0) {
    Serial.printf("[BATT] packV=%.2f pct=%d\n", battPackV, battPercent);
    PiSerial.printf("BATT:%d\n", battPercent);
  }
  else if (strcmp(buf,"HARDWARE:ON")==0)  { hardwareEnabled=true;  obstacleAhead=false; PiSerial.println("HW:ON");  }
  else if (strcmp(buf,"HARDWARE:OFF")==0) { hardwareEnabled=false; obstacleAhead=false; PiSerial.println("HW:OFF"); }
  else if (strcmp(buf,"LATCH:OFF")==0) {
    if (shutdownTriggered) return;
    shutdownTriggered = true;
    Serial.println("[LATCH] Pi shutdown — homing then 15s...");
    stopMotors();
    homeServosSlow("pi shutdown");
    savePositions();
    for (int i=15;i>0;i--){ Serial.println(i); delay(1000); }
    digitalWrite(LATCH_PIN, LOW);
  }
}

// ── SETUP ──────────────────────────────────────────────────
void setup() {
  // LATCH FIRST — before Serial, before anything. If the PCA9685's VCC comes
  // off this latched rail, every millisecond we spend here is time it has to
  // power up and release its internal reset. v5 raised the latch after
  // Serial.begin + delay(500) and then scanned ~140ms later, which was too
  // early; the bench sketch that worked happened to wait 1200ms.
  pinMode(LATCH_PIN, OUTPUT);
  digitalWrite(LATCH_PIN, HIGH);

  Serial.begin(115200);
  delay(PCA_POWERUP_MS);              // USB CDC + PCA9685 settling
  Serial.println("[LATCH] ON");
  Serial.printf("[BOOT] waited %dms for the rail before I2C\n",
                PCA_POWERUP_MS);

  // ── I2C FIRST ───────────────────────────────────────────
  // Before the button and both UARTs. The bench sketch that worked did
  // exactly this; the firmware that failed initialised those peripherals
  // first. Same pins, same board, same wiring — only the order differed.
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(I2C_CLOCK);
  Serial.printf("[I2C] SDA=%d SCL=%d @ %lu Hz\n",
                I2C_SDA, I2C_SCL, I2C_CLOCK);

  pcaOk = false;
  for (int attempt = 1; attempt <= PCA_RETRIES && !pcaOk; attempt++) {
    Wire.beginTransmission(PCA_ADDR);
    if (Wire.endTransmission() == 0) {
      pcaOk = pca.begin();
      Serial.printf("[PCA] attempt %d: 0x%02X responded, begin() %s\n",
                    attempt, PCA_ADDR, pcaOk ? "ok" : "FAILED");
    } else {
      Serial.printf("[PCA] attempt %d/%d: no response at 0x%02X\n",
                    attempt, PCA_RETRIES, PCA_ADDR);
      delay(PCA_RETRY_MS);
    }
  }

  if (!pcaOk) {
    i2cScan();                        // only worth the 6s when it failed
    Serial.println("[PCA] *** begin() FAILED before any other peripheral ***");
    Serial.println("[PCA] so this is wiring or power, not a pin conflict.");
    Serial.println("[PCA] check SDA=14 SCL=21, common GND, VCC, and V+.");
  } else {
    pca.setOscillatorFrequency(PCA_OSC_FREQ);
    pca.setPWMFreq(SERVO_FREQ);
    Serial.println("[PCA] 9685 ready");
  }

  // ── now the peripherals, probing the bus after each ─────
  pinMode(BUTTON_PIN, INPUT_PULLDOWN);
  if (pcaOk) pcaOk = pcaAlive("pinMode(BUTTON_PIN=20)");

  PiSerial.begin(115200, SERIAL_8N1, PI_RX_PIN, PI_TX_PIN);
  delay(100);
  while (PiSerial.available()) PiSerial.read();      // flush boot garbage
  if (pcaOk) pcaOk = pcaAlive("PiSerial.begin(UART1, 17/18)");

  LunaSerial.begin(115200, SERIAL_8N1, LUNA_RX_PIN, LUNA_TX_PIN);
  if (pcaOk) pcaOk = pcaAlive("LunaSerial.begin(UART2, 6/5)");

  // If a peripheral knocked the bus over, try to bring it back. Re-running
  // Wire.begin() re-routes SDA/SCL through the GPIO matrix, which recovers
  // the case where another peripheral stole one of the pins.
  if (!pcaOk) {
    Serial.println("[PCA] attempting bus recovery...");
    Wire.end();
    delay(50);
    Wire.begin(I2C_SDA, I2C_SCL);
    Wire.setClock(I2C_CLOCK);
    Wire.beginTransmission(PCA_ADDR);
    if (Wire.endTransmission() == 0 && pca.begin()) {
      pca.setOscillatorFrequency(PCA_OSC_FREQ);
      pca.setPWMFreq(SERVO_FREQ);
      pcaOk = true;
      Serial.println("[PCA] recovered — but the peripheral named above is");
      Serial.println("[PCA] still fighting for the pin. Move it.");
    } else {
      Serial.println("[PCA] recovery failed. Elbows and fingers are dead.");
      PiSerial.println("FAULT:PCA9685");
    }
  }

  // Only two timers for ESP32Servo (3 direct servos share one 50Hz timer),
  // leaving timers 2-3 for the four motor LEDC channels. v4 allocated all
  // four, which could starve ledcAttach() and silently kill motor PWM.
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  lateralL.setPeriodHertz(50);
  lateralR.setPeriodHertz(50);
  headServo.setPeriodHertz(50);

  // 1) recall the last pose BEFORE attaching / pulsing
  restorePositions();

  // 2) attach and hold that pose → no start-up jerk
  lateralL.attach(LATERAL_L_PIN, SERVO_MIN_US, SERVO_MAX_US);
  lateralR.attach(LATERAL_R_PIN, SERVO_MIN_US, SERVO_MAX_US);
  headServo.attach(HEAD_LR_PIN,  SERVO_MIN_US, SERVO_MAX_US);
  for (int ch = 0; ch < NUM_CH; ch++) if (PCA_USED[ch]) pcaWriteDeg(ch, chCurrent[ch]);
  writeLateralL(latCurrentL);
  writeLateralR(latCurrentR);
  writeHead(headCurrent);
  delay(400);

  // motors off before anything moves
  ledcAttach(RPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(RPWM_R, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_R, PWM_FREQ, PWM_RES);
  ledcWrite(RPWM_L,0); ledcWrite(LPWM_L,0);
  ledcWrite(RPWM_R,0); ledcWrite(LPWM_R,0);
  pinMode(EN_L,OUTPUT); digitalWrite(EN_L,LOW);
  pinMode(EN_R,OUTPUT); digitalWrite(EN_R,LOW);

  analogReadResolution(12);
  analogSetPinAttenuation(BATT_ADC_PIN, ADC_11db);

  lastCmdTime = millis();
  motorSpeed  = appSpeedToPWM(DEFAULT_BASE_SPEED);
  servoSpeed  = DEFAULT_SERVO_SPEED;

  // 3) glide slowly to home — the boot calibration move
  homeServosSlow("boot");
  savePositions();

  Serial.printf("[SYSTEM] Ben Pro Max v7 ready (base %d%% pwm=%d, servo %d%%, "
                "PCA %s)\n",
                DEFAULT_BASE_SPEED, motorSpeed, servoSpeed,
                pcaOk ? "ok" : "FAULT");
  Serial.println("[SYSTEM] type PCATEST or I2CSCAN here to diagnose servos");
  PiSerial.println("BenProMax ready.");
}

// ── LOOP ───────────────────────────────────────────────────
void loop() {
  checkButton();
  readLuna();
  readBattery();
  handleSerial();
  updateServos();

  if (millis() - lastCmdTime > 500 && robotMoving) stopMotors();

  // remember the pose periodically (idle only, only if it changed)
  static unsigned long lastSave = 0;
  static float lastSavedL = -1;
  if (millis() - lastSave > 30000 && servosAtTarget() && !robotMoving) {
    if (fabsf(latCurrentL - lastSavedL) > 1.0f) {
      savePositions();
      lastSavedL = latCurrentL;
    }
    lastSave = millis();
  }

  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    lastPrint = millis();
    Serial.printf("Dist:%d Batt:%d%% (%.2fV) Dir:%s\n",
                  lunaDistance, battPercent, battPackV, currentDir);
    PiSerial.print("Dist:"); PiSerial.print(lunaDistance);
    PiSerial.print(" BATT:"); PiSerial.println(battPercent);
  }
}

/*
 * ─── IF THE ELBOWS/FINGERS ARE STILL DEAD ──────────────────
 * On boot the log now tells you where you stand:
 *
 *   "[I2C]   0x40  <-- PCA9685"  and  "[PCA] begin ok"
 *        The board is reachable. Type PCATEST. If the servos sweep, the
 *        hardware is fine and the problem is upstream: watch for
 *        "[SERVO] elbow(...)" when you move the app slider. No line means
 *        the command never arrived (check the POS: format and the Pi link).
 *        A line whose target equals its current angle means the joint is
 *        already there, so nothing moves — try the other end of the slider.
 *
 *   "[PCA] attempt 1/5: no response" then a later attempt succeeds
 *        Power sequencing. It works, but the rail is slow — raise
 *        PCA_POWERUP_MS until attempt 1 succeeds every boot.
 *
 *   all 5 attempts fail, "[I2C]   nothing found"
 *        Wiring. SDA=14, SCL=21, common GND, VCC present. Remember VCC
 *        powers the chip and V+ powers the servos: with only VCC the board
 *        answers I2C and moves nothing. Note the bench sketch that worked
 *        used the SAME pins at 400kHz, so if that sketch still runs and this
 *        does not, the difference is timing or power, never bus speed.
 *
 *   begin ok, PCATEST silent, servos buzz
 *        Supply current. Four servos stalling can pull several amps; a 2A
 *        supply browns out. V+ must come from the high-current rail.
 *
 * ─── BATTERY CALIBRATION (two independent steps) ───────────
 * STEP 1  raw ADC → pack volts, via RAW_HI/VOLT_HI and RAW_LO/VOLT_LO.
 *         Board-specific. Use points far apart (14V and 11V).
 * STEP 2  pack volts → percent, via BATT_V[] / BATT_PCT[]. Same on every
 *         unit. Points must run high → low; values between are interpolated.
 * If the percentage is right but the voltage is wrong, fix STEP 1.
 * If the voltage is right but the percentage is wrong, fix STEP 2.
 *
 * ─── SERVO TRAVEL / DIRECTION ──────────────────────────────
 *   OUT_A = angle at slider 0, OUT_B = angle at slider 2000.
 *   Put the larger angle in OUT_A to reverse the joint.
 *   Laterals are mirror-mounted: L runs 180→0, R runs 0→180.
 *
 * ─── KNOWN HARDWARE NOTE ───────────────────────────────────
 *   BUTTON_PIN 20 is the ESP32-S3's native USB D+ pin. With USB CDC enabled
 *   this can cause flaky serial or phantom presses. Move it to GPIO 9 or 10
 *   if you see either.
 */
