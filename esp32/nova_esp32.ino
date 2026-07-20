/*
 * BEN PRO MAX — ESP32-S3 Firmware  (with per-servo MIN/MAX/HOME)
 * Shared I2C: PCA9685 (elbows+fingers) + TF-Luna @0x10
 * Direct servos: lateral L/R, head · BTN7960 motors
 * Latch power · battery monitor (12.8V LiFePO4) · multi-function button
 *
 * Board: "ESP32S3 Dev Module" · USB CDC On Boot: Enabled
 * Library: Adafruit PWM Servo Driver
 */

#include <Wire.h>
#include <ESP32Servo.h>
#include <Adafruit_PWMServoDriver.h>

// ── DIRECT SERVOS ──────────────────────────────────────────
#define LATERAL_L_PIN  1
#define LATERAL_R_PIN  2
#define HEAD_LR_PIN    7

// ── I2C (shared: PCA9685 + TF-Luna) ────────────────────────
#define I2C_SDA        14
#define I2C_SCL        21
#define LUNA_ADDR      0x10
Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(0x40);
#define SERVO_FREQ     50
#define SERVO_MIN_US   500
#define SERVO_MAX_US   2400
#define CH_ELBOW_L     4
#define CH_ELBOW_R     5
#define CH_FINGER_L    2
#define CH_FINGER_R    3
#define NUM_CH         6      // array size must cover highest channel used (5)

// ═══════════════════════════════════════════════════════════
// ── SERVO LIMITS & HOME POSITIONS (degrees) — TUNE HERE ────
// App value 0..2000 maps to that servo's MIN..MAX.
// HOME is where the servo sits at boot, on "HOME", and at shutdown.
// ═══════════════════════════════════════════════════════════
// Head (left-right)
#define HEAD_MIN       30
#define HEAD_MAX       150
#define HEAD_HOME      90
// Elbows
#define ELBOW_L_MIN    10
#define ELBOW_L_MAX    170
#define ELBOW_L_HOME   90
#define ELBOW_R_MIN    10
#define ELBOW_R_MAX    170
#define ELBOW_R_HOME   90
// Laterals (shoulders) — right side is mirrored automatically
#define LAT_L_MIN      10
#define LAT_L_MAX      170
#define LAT_L_HOME     90
#define LAT_R_MIN      10
#define LAT_R_MAX      170
#define LAT_R_HOME     90
// Fingers
#define FING_L_MIN     0
#define FING_L_MAX     180
#define FING_L_HOME    90
#define FING_R_MIN     0
#define FING_R_MAX     180
#define FING_R_HOME    90
// Homing speed (0..100 scale, same as TOPSPEED). Low = slow & gentle.
#define HOME_SPEED     15

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

// ── PI SERIAL LINK ─────────────────────────────────────────
#define PI_TX_PIN      18     // → Pi RXD
#define PI_RX_PIN      17     // ← Pi TXD
HardwareSerial PiSerial(1);

// ── LATCH + BUTTON ─────────────────────────────────────────
#define LATCH_PIN      8      // HIGH holds power
#define BUTTON_PIN     11     // to GND (internal pullup)
#define LONG_PRESS_MS  3000
#define SHORT_MIN_MS   100

// ── BATTERY (IO12, 10K/1K divider, 12.8V pack) ─────────────
#define BATT_ADC_PIN   12

// Two-point raw-ADC calibration (measured on this board):
//   raw 1491 → pack 14.30V (100%)   raw 1197 → pack 11.00V
// If your two measured pack voltages differ, update these 4 numbers.
#define RAW_HI   1491.0
#define VOLT_HI  14.30
#define RAW_LO   1197.0
#define VOLT_LO  11.00
#define BATT_CAL 1.00        // we need to tune this


// ── TF-LUNA distance thresholds ────────────────────────────
#define OBSTACLE_DIST  50
#define DETECT_DIST    150
#define COOLDOWN_MS    10000

Servo lateralL, lateralR, headServo;

// ── STATE ──────────────────────────────────────────────────
int  motorSpeed = 255;
int  servoSpeed = 100;
int  lunaDistance = 999;
bool obstacleAhead = false;
char currentDir[12] = "stop";
char savedDir[12]  = "stop";
unsigned long lastGreeted=0, lastLuna=0, lastServoUpdate=0, lastCmdTime=0;
bool robotMoving=false, hardwareEnabled=true, shutdownTriggered=false;
unsigned long btnPressStart=0;
bool btnWasPressed=false;

// Per-PCA-channel limits/home (filled in setup from the #defines)
int chMin[NUM_CH], chMax[NUM_CH], chHome[NUM_CH];
int chCurrent[NUM_CH];
int chTarget[NUM_CH];
int latCurrentL=LAT_L_HOME, latTargetL=LAT_L_HOME;
int latCurrentR=LAT_R_HOME, latTargetR=LAT_R_HOME;
int headCurrent=HEAD_HOME,  headTarget=HEAD_HOME;

float battPinV = 0;
int   battPercent = 100;

// Homing state: while homing, servo speed is forced to HOME_SPEED,
// then restored to the user's speed once every joint reaches home.
bool homingActive = false;
int  preHomeSpeed = 100;

// ── HELPERS ────────────────────────────────────────────────
int appSpeedToPWM(int v) {
  v = constrain(v, 0, 200);
  if (v == 0) return 0;
  return map(v, 1, 200, MIN_MOVE_PWM, MAX_MOVE_PWM);
}

// Map app value 0..2000 into a servo's own [mn..mx] range.
// Pass mx < mn to get a mirrored (reversed) mapping.
int mapToRange(int value, int mn, int mx) {
  value = constrain(value, 0, 2000);
  return map(value, 0, 2000, mn, mx);
}

void pcaWriteDeg(int ch, int deg) {
  deg = constrain(deg, 0, 180);
  int us = map(deg, 0, 180, SERVO_MIN_US, SERVO_MAX_US);
  pca.setPWM(ch, 0, (int)(us * 4096L / 20000L));
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
  Serial.print("[MOTOR] "); Serial.println(dir);
}

// ── SMOOTH SERVO UPDATE ────────────────────────────────────
void updateServos() {
  unsigned long now = millis();
  unsigned long interval = (unsigned long)map(servoSpeed, 0, 100, 50, 5);
  if (now - lastServoUpdate < interval) return;
  lastServoUpdate = now;
  int step = map(servoSpeed, 0, 100, 1, 8);

  for (int ch = 0; ch < NUM_CH; ch++) {
    if (chCurrent[ch] < chTarget[ch]) {
      chCurrent[ch] = min(chCurrent[ch]+step, chTarget[ch]);
      pcaWriteDeg(ch, chCurrent[ch]);
    } else if (chCurrent[ch] > chTarget[ch]) {
      chCurrent[ch] = max(chCurrent[ch]-step, chTarget[ch]);
      pcaWriteDeg(ch, chCurrent[ch]);
    }
  }
  if (latCurrentL < latTargetL) { latCurrentL=min(latCurrentL+step,latTargetL); lateralL.write(latCurrentL); }
  else if (latCurrentL > latTargetL) { latCurrentL=max(latCurrentL-step,latTargetL); lateralL.write(latCurrentL); }
  if (latCurrentR < latTargetR) { latCurrentR=min(latCurrentR+step,latTargetR); lateralR.write(latCurrentR); }
  else if (latCurrentR > latTargetR) { latCurrentR=max(latCurrentR-step,latTargetR); lateralR.write(latCurrentR); }
  if (headCurrent < headTarget) { headCurrent=min(headCurrent+step,headTarget); headServo.write(headCurrent); }
  else if (headCurrent > headTarget) { headCurrent=max(headCurrent-step,headTarget); headServo.write(headCurrent); }
}

// ── PART → TARGET (0..2000 app value → each servo's MIN..MAX) ──
void moveServos(const char* part, int value, const char* hand) {
  // A new position command cancels slow-homing and restores normal speed
  if (homingActive) finishHoming();
  bool doL = (strcmp(hand,"left")==0  || strcmp(hand,"both")==0);
  bool doR = (strcmp(hand,"right")==0 || strcmp(hand,"both")==0);

  if (strcmp(part,"lateral")==0) {
    if (doL) latTargetL = mapToRange(value, LAT_L_MIN, LAT_L_MAX);
    if (doR) latTargetR = mapToRange(value, LAT_R_MAX, LAT_R_MIN);  // mirrored — swap MIN/MAX here if direction is wrong
    Serial.printf("[SERVO] lateral(%s) L=%d R=%d\n", hand, latTargetL, latTargetR);
    return;
  }
  if (strcmp(part,"headLR")==0 || strcmp(part,"head")==0) {
    headTarget = mapToRange(value, HEAD_MIN, HEAD_MAX);
    Serial.printf("[SERVO] head -> %d\n", headTarget);
    return;
  }
  if (strcmp(part,"elbow")==0) {
    if (doL) chTarget[CH_ELBOW_L] = mapToRange(value, chMin[CH_ELBOW_L], chMax[CH_ELBOW_L]);
    if (doR) chTarget[CH_ELBOW_R] = mapToRange(value, chMin[CH_ELBOW_R], chMax[CH_ELBOW_R]);
    Serial.printf("[SERVO] elbow(%s) -> %d\n", hand, doL ? chTarget[CH_ELBOW_L] : chTarget[CH_ELBOW_R]);
    return;
  }
  // fingers / any finger name → the hand's single finger channel
  if (strstr(part,"finger") != NULL ||
      strcmp(part,"thumb")==0 || strcmp(part,"index")==0 ||
      strcmp(part,"middle")==0 || strcmp(part,"ring")==0 ||
      strcmp(part,"pinky")==0) {
    if (doL) chTarget[CH_FINGER_L] = mapToRange(value, chMin[CH_FINGER_L], chMax[CH_FINGER_L]);
    if (doR) chTarget[CH_FINGER_R] = mapToRange(value, chMin[CH_FINGER_R], chMax[CH_FINGER_R]);
    Serial.printf("[SERVO] fingers(%s) -> %d\n", hand, doL ? chTarget[CH_FINGER_L] : chTarget[CH_FINGER_R]);
    return;
  }
  Serial.printf("[IGNORE] %s\n", part);
}

// ── HOMING ─────────────────────────────────────────────────
bool servosAtTarget() {
  for (int ch = 0; ch < NUM_CH; ch++)
    if (chCurrent[ch] != chTarget[ch]) return false;
  return latCurrentL==latTargetL && latCurrentR==latTargetR && headCurrent==headTarget;
}

void finishHoming() {
  homingActive = false;
  servoSpeed = preHomeSpeed;        // restore the user's speed
  Serial.println("[SERVO] HOMED");
}

// Slow, smooth home: set targets and let updateServos() glide there
// at HOME_SPEED. Speed is restored automatically when done (see loop).
void homeServos() {
  for (int ch = 0; ch < NUM_CH; ch++) chTarget[ch] = chHome[ch];
  latTargetL = LAT_L_HOME;
  latTargetR = LAT_R_HOME;
  headTarget = HEAD_HOME;
  if (!homingActive) { preHomeSpeed = servoSpeed; homingActive = true; }
  servoSpeed = HOME_SPEED;
  Serial.println("[SERVO] HOMING (slow)...");
}

// Instant home — boot only (positions are unknown at power-on,
// and servos jump to their first command on attach anyway).
void homeServosInstant() {
  for (int ch = 0; ch < NUM_CH; ch++) {
    chTarget[ch] = chCurrent[ch] = chHome[ch];
    pcaWriteDeg(ch, chHome[ch]);
  }
  latTargetL = latCurrentL = LAT_L_HOME; lateralL.write(LAT_L_HOME);
  latTargetR = latCurrentR = LAT_R_HOME; lateralR.write(LAT_R_HOME);
  headTarget = headCurrent = HEAD_HOME;  headServo.write(HEAD_HOME);
  Serial.println("[SERVO] HOMED (boot)");
}

// Blocking slow home — used by the shutdown paths, which sit in
// delay() loops where updateServos() would never run otherwise.
void homeServosBlocking() {
  homeServos();
  unsigned long t0 = millis();
  while (!servosAtTarget() && millis() - t0 < 10000) {
    updateServos();
    delay(2);
  }
  finishHoming();
}

// ── TF-LUNA (I2C @0x10) ────────────────────────────────────
void readLuna() {
  if (millis() - lastLuna < 100) return;
  lastLuna = millis();

  Wire.beginTransmission(LUNA_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) return;
  Wire.requestFrom((uint8_t)LUNA_ADDR, (uint8_t)2);
  if (Wire.available() < 2) return;
  int dist = Wire.read() | (Wire.read() << 8);
  if (dist <= 0 || dist > 800) return;

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

// ── BATTERY (raw ADC → pack volts → percent) ───────────────
float rawToPackVolts(float raw) {
  return VOLT_LO + (raw - RAW_LO) * (VOLT_HI - VOLT_LO) / (RAW_HI - RAW_LO);
}

int packVoltsToPercent(float v) {
  const float vt[] = {14.30,14.00,13.50,13.00,12.50,12.00,11.50,11.00,10.50,10.00};
  const int   pt[] = {100,  90,   80,   70,   60,   50,   40,   30,   20,   10};
  if (v >= vt[0]) return 100;
  for (int i = 1; i < 10; i++) {
    if (v >= vt[i]) {
      float frac = (v - vt[i]) / (vt[i-1] - vt[i]);
      return pt[i] + (int)(frac * (pt[i-1] - pt[i]));
    }
  }
  return v > 9.0 ? 5 : 0;
}

void readBattery() {
  static unsigned long lastBatt = 0;
  if (millis() - lastBatt < 5000) return;
  lastBatt = millis();
  long sum = 0;
  for (int i = 0; i < 8; i++) { sum += analogRead(BATT_ADC_PIN); delay(2); }
  float raw = sum / 8.0;
  float packV = rawToPackVolts(raw);
  battPinV   = packV;                       // now holds actual pack volts
  battPercent = packVoltsToPercent(packV);
  Serial.printf("[BATT] raw=%.0f packV=%.2f pct=%d\n", raw, packV, battPercent);
}

// ── LATCH SHUTDOWN ─────────────────────────────────────────
void doLatchShutdown() {
  if (shutdownTriggered) return;
  shutdownTriggered = true;
  Serial.println("[LATCH] Shutdown sequence!");
  stopMotors(); homeServosBlocking();       // robot parks slowly at home before power-off
  PiSerial.println("SHUTDOWN");
  for (int i = 15; i > 0; i--) { Serial.printf("[LATCH] %ds\n", i); delay(1000); }
  digitalWrite(LATCH_PIN, LOW);
}

// ── MULTI-FUNCTION BUTTON ──────────────────────────────────
// short (<3s) → Pi factory-reset hotspot · long (>=3s) → shutdown
void checkButton() {
  if (shutdownTriggered) return;
  bool pressed = (digitalRead(BUTTON_PIN) == LOW);
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
void handleSerial() {
  if (!PiSerial.available()) return;
  char buf[96]; int len = 0;
  unsigned long t = millis();
  while (millis() - t < 30 && len < 95) {
    if (PiSerial.available()) {
      char c = PiSerial.read();
      if (c == '\n') break;
      buf[len++] = c;
    }
  }
  buf[len]='\0';
  if (len>0 && buf[len-1]=='\r') buf[--len]='\0';
  if (len==0) return;
  while (PiSerial.available()) PiSerial.read();

  Serial.print("[CMD] "); Serial.println(buf);

  if (strncmp(buf,"MOVE:",5)==0) { lastCmdTime=millis(); executeMove(buf+5); }
  else if (strncmp(buf,"SPEED:",6)==0) {
    motorSpeed = appSpeedToPWM(atoi(buf+6));
    if (robotMoving) executeMove(currentDir);
  }
  else if (strncmp(buf,"TOPSPEED:",9)==0) {
    int v = constrain(atoi(buf+9),0,100);
    if (homingActive) preHomeSpeed = v;   // apply after homing finishes
    else servoSpeed = v;
  }
  else if (strncmp(buf,"HOME",4)==0) homeServos();
  else if (strcmp(buf,"RESUME")==0) executeMove(savedDir);
  else if (strncmp(buf,"POS:",4)==0) {
    char* p1 = strchr(buf+4, ':'); if (!p1) return;
    char* p2 = strchr(p1+1, ':');  if (!p2) return;
    *p1='\0'; *p2='\0';
    moveServos(buf+4, atoi(p1+1), p2+1);
  }
  else if (strcmp(buf,"HARDWARE:ON")==0)  { hardwareEnabled=true;  obstacleAhead=false; PiSerial.println("HW:ON");  }
  else if (strcmp(buf,"HARDWARE:OFF")==0) { hardwareEnabled=false; obstacleAhead=false; PiSerial.println("HW:OFF"); }
  else if (strcmp(buf,"LATCH:OFF")==0) {
    if (shutdownTriggered) return;
    shutdownTriggered = true;
    Serial.println("[LATCH] Pi shutdown — 15s...");
    stopMotors(); homeServosBlocking();     // park slowly at home before power-off
    for (int i=15;i>0;i--){ Serial.println(i); delay(1000); }
    digitalWrite(LATCH_PIN, LOW);
  }
}

// ── SETUP ──────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(LATCH_PIN, OUTPUT);
  digitalWrite(LATCH_PIN, HIGH);
  Serial.println("[LATCH] ON");

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  PiSerial.begin(115200, SERIAL_8N1, PI_RX_PIN, PI_TX_PIN);
  delay(100);
  while (PiSerial.available()) PiSerial.read();

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);

  pca.begin();
  pca.setPWMFreq(SERVO_FREQ);
  Serial.println("[PCA] 9685 ready");

  // Fill per-channel limits/home from the config block
  for (int ch = 0; ch < NUM_CH; ch++) { chMin[ch]=0; chMax[ch]=180; chHome[ch]=90; }
  chMin[CH_ELBOW_L]=ELBOW_L_MIN;  chMax[CH_ELBOW_L]=ELBOW_L_MAX;  chHome[CH_ELBOW_L]=ELBOW_L_HOME;
  chMin[CH_ELBOW_R]=ELBOW_R_MIN;  chMax[CH_ELBOW_R]=ELBOW_R_MAX;  chHome[CH_ELBOW_R]=ELBOW_R_HOME;
  chMin[CH_FINGER_L]=FING_L_MIN;  chMax[CH_FINGER_L]=FING_L_MAX;  chHome[CH_FINGER_L]=FING_L_HOME;
  chMin[CH_FINGER_R]=FING_R_MIN;  chMax[CH_FINGER_R]=FING_R_MAX;  chHome[CH_FINGER_R]=FING_R_HOME;

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  lateralL.setPeriodHertz(50);
  lateralR.setPeriodHertz(50);
  headServo.setPeriodHertz(50);
  lateralL.attach(LATERAL_L_PIN,  SERVO_MIN_US, SERVO_MAX_US);
  lateralR.attach(LATERAL_R_PIN,  SERVO_MIN_US, SERVO_MAX_US);
  headServo.attach(HEAD_LR_PIN,   SERVO_MIN_US, SERVO_MAX_US);

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
  motorSpeed = appSpeedToPWM(100);
  delay(200);
  homeServosInstant();   // ← robot always boots into its home position

  Serial.println("[SYSTEM] Ben Pro Max ready");
  PiSerial.println("BenProMax ready.");
}

// ── LOOP ───────────────────────────────────────────────────
void loop() {
  checkButton();
  readLuna();
  readBattery();
  handleSerial();
  updateServos();

  // When slow-homing finishes, restore the user's servo speed
  if (homingActive && servosAtTarget()) finishHoming();

  if (millis() - lastCmdTime > 500 && robotMoving) stopMotors();

  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    lastPrint = millis();
    Serial.printf("Dist:%d Batt:%d%% (%.3fV) Dir:%s\n",
                  lunaDistance, battPercent, battPinV, currentDir);
    PiSerial.print("Dist:"); PiSerial.print(lunaDistance);
    PiSerial.print(" BATT:"); PiSerial.println(battPercent);
  }
}
