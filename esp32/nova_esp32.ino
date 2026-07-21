/*
 * BEN PRO MAX — ESP32-S3 Firmware
 * I2C: PCA9685 only (elbows+fingers) · TF-Luna on UART2 (GPIO 5/6)
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

// ── I2C (PCA9685 only) ─────────────────────────────────────
#define I2C_SDA        14
#define I2C_SCL        21
Adafruit_PWMServoDriver pca = Adafruit_PWMServoDriver(0x40);
#define SERVO_FREQ     50
#define SERVO_MIN_US   500
#define SERVO_MAX_US   2400
#define CH_ELBOW_L     7
#define CH_ELBOW_R     8
#define CH_FINGER_L    2
#define CH_FINGER_R    3
#define NUM_CH         9     // must cover highest channel used (5)

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

// ── PI SERIAL LINK (UART1) ─────────────────────────────────
#define PI_TX_PIN      18     // → Pi RXD
#define PI_RX_PIN      17     // ← Pi TXD
HardwareSerial PiSerial(1);

// ── TF-LUNA UART (UART2) ───────────────────────────────────
// Luna pin 2 (RXD, blue)  ← ESP32 GPIO 5 (TX)
// Luna pin 3 (TXD, green) → ESP32 GPIO 6 (RX)
// Luna pin 5 (config) MUST be left DISCONNECTED for UART mode
// (if it was grounded for I2C, remove that wire!)
#define LUNA_TX_PIN    5
#define LUNA_RX_PIN    6
HardwareSerial LunaSerial(2);

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
void processCommand(char* buf);   // fwd declaration

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

int chCurrent[NUM_CH];
int chTarget[NUM_CH];
int latCurrentL=90, latTargetL=90;
int latCurrentR=90, latTargetR=90;
int headCurrent=90, headTarget=90;

float battPinV = 0;
int   battPercent = 100;

// ── HELPERS ────────────────────────────────────────────────
int appSpeedToPWM(int v) {
  v = constrain(v, 0, 200);
  if (v == 0) return 0;
  return map(v, 1, 200, MIN_MOVE_PWM, MAX_MOVE_PWM);
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

// ── PART → TARGET ──────────────────────────────────────────
void moveServos(const char* part, int value, const char* hand) {
  value = constrain(value, 0, 2000);
  int deg = map(value, 0, 2000, 0, 180);
  bool doL = (strcmp(hand,"left")==0  || strcmp(hand,"both")==0);
  bool doR = (strcmp(hand,"right")==0 || strcmp(hand,"both")==0);

  if (strcmp(part,"lateral")==0) {
    if (doL) latTargetL = deg;
    if (doR) latTargetR = 180 - deg;      // mirrored — flip if wrong
    Serial.printf("[SERVO] lateral(%s) -> %d\n", hand, deg);
    return;
  }
  if (strcmp(part,"headLR")==0 || strcmp(part,"head")==0) {
    headTarget = deg;
    Serial.printf("[SERVO] head -> %d\n", deg);
    return;
  }
  if (strcmp(part,"elbow")==0) {
    if (doL) chTarget[CH_ELBOW_L] = deg;
    if (doR) chTarget[CH_ELBOW_R] = deg;
    Serial.printf("[SERVO] elbow(%s) -> %d\n", hand, deg);
    return;
  }
  // fingers / any finger name → the hand's single finger channel
  if (strstr(part,"finger") != NULL ||
      strcmp(part,"thumb")==0 || strcmp(part,"index")==0 ||
      strcmp(part,"middle")==0 || strcmp(part,"ring")==0 ||
      strcmp(part,"pinky")==0) {
    if (doL) chTarget[CH_FINGER_L] = deg;
    if (doR) chTarget[CH_FINGER_R] = deg;
    Serial.printf("[SERVO] fingers(%s) -> %d\n", hand, deg);
    return;
  }
  Serial.printf("[IGNORE] %s\n", part);
}

void homeServos() {
  for (int ch = 0; ch < NUM_CH; ch++) {
    chTarget[ch] = 90; chCurrent[ch] = 90; pcaWriteDeg(ch, 90);
  }
  latTargetL=latCurrentL=90; lateralL.write(90);
  latTargetR=latCurrentR=90; lateralR.write(90);
  headTarget=headCurrent=90; headServo.write(90);
  Serial.println("[SERVO] HOMED");
}

// ── TF-LUNA (UART2, 115200 · 9-byte frames @ ~100Hz) ───────
// Frame: 0x59 0x59 DistL DistH StrL StrH TempL TempH Checksum
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
    if (idx == 0 && b != 0x59) continue;            // hunt for header
    if (idx == 1 && b != 0x59) { idx = 0; continue; }
    frame[idx++] = b;
    if (idx == 9) {
      idx = 0;
      uint8_t sum = 0;
      for (int i = 0; i < 8; i++) sum += frame[i];
      if (sum != frame[8]) continue;                // bad checksum → drop
      int dist     = frame[2] | (frame[3] << 8);
      int strength = frame[4] | (frame[5] << 8);
      if (dist <= 0 || dist > 800) continue;        // out of range
      if (strength < 100) continue;                 // unreliable reading
      processDistance(dist);
    }
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
  stopMotors(); homeServos();
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
// Non-blocking line reader with a persistent buffer:
//  · processes EVERY complete line (no commands ever discarded)
//  · drops non-printable noise bytes (floating RX line, Pi boot garbage)
//  · never blocks the loop waiting for bytes
void handleSerial() {
  static char buf[96];
  static int  len = 0;

  while (PiSerial.available()) {
    char c = PiSerial.read();

    if (c == '\n' || c == '\r') {          // end of line → process it
      if (len > 0) {
        buf[len] = '\0';
        processCommand(buf);
        len = 0;
      }
      continue;
    }
    if (c < 32 || c > 126) continue;       // drop noise / non-printable
    if (len < 95) buf[len++] = c;
    else len = 0;                          // overflow → discard garbage line
  }
}

void processCommand(char* buf) {
  Serial.print("[CMD] "); Serial.println(buf);

  if (strncmp(buf,"MOVE:",5)==0) { lastCmdTime=millis(); executeMove(buf+5); }
  else if (strncmp(buf,"SPEED:",6)==0) {
    motorSpeed = appSpeedToPWM(atoi(buf+6));
    if (robotMoving) executeMove(currentDir);
  }
  else if (strncmp(buf,"TOPSPEED:",9)==0) servoSpeed = constrain(atoi(buf+9),0,100);
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
    stopMotors(); homeServos();
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

  // TF-Luna on its own UART — nothing shared with the servos
  LunaSerial.begin(115200, SERIAL_8N1, LUNA_RX_PIN, LUNA_TX_PIN);

  // I2C: PCA9685 has the bus entirely to itself
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);

  pca.begin();
  pca.setPWMFreq(SERVO_FREQ);
  Serial.println("[PCA] 9685 ready");

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
  homeServos();

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
