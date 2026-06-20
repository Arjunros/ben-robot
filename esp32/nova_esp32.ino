#include <HardwareSerial.h>
#include <ESP32Servo.h>

// ── MOTOR PINS ─────────────────────────────────────────────
#define RPWM_L  19
#define LPWM_L  18
#define RPWM_R  15
#define LPWM_R  2
#define EN_L    5
#define EN_R    4
#define PWM_FREQ  1000
#define PWM_RES   8

// ── ULTRASONIC SENSOR ──────────────────────────────────────
#define TRIG_PIN      32
#define ECHO_PIN      25
#define OBSTACLE_DIST 100
#define DETECT_DIST   50
#define COOLDOWN_MS   10000

// ── LATCH PIN ──────────────────────────────────────────────
#define LATCH_PIN     23

// ── SHUTDOWN BUTTON ────────────────────────────────────────
#define SHUTDOWN_BTN  39
#define LONG_PRESS_MS 3000

// ── SERVO PINS ─────────────────────────────────────────────
#define SERVO_L_PIN  26
#define SERVO_R_PIN  27
#define HEAD_LR_PIN  14

HardwareSerial PiSerial(2);
Servo servoL;
Servo servoR;
Servo headServo;

// ── SERVO LIMITS ───────────────────────────────────────────
const int L_MIN      = 180;
const int L_MAX      = 0;
const int L_CENTER   = 180;
const int R_MIN      = 0;
const int R_MAX      = 180;
const int R_CENTER   = 0;
const int HEAD_MIN   = 0;
const int HEAD_MAX   = 180;
const int HEAD_CENTER= 90;

// ── STATE ──────────────────────────────────────────────────
int           motorSpeed       = 255;
int           servoSpeed       = 100;
int           currentPosL      = 90;
int           currentPosR      = 90;
int           currentHeadPos   = 90;
int           targetPosL       = 90;
int           targetPosR       = 90;
int           targetHeadPos    = 90;
int           obstacleDistance = 999;
bool          obstacleAhead    = false;
char          currentDir[12]   = "stop";
char          savedDir[12]     = "stop";
unsigned long lastGreeted      = 0;
unsigned long lastSensorRead   = 0;
unsigned long lastServoUpdate  = 0;
unsigned long lastCmdTime      = 0;
bool          robotMoving      = false;
bool          hardwareEnabled  = true;
bool          shutdownTriggered = false;

// ── BUTTON STATE ───────────────────────────────────────────
unsigned long btnPressStart = 0;
bool          btnWasPressed = false;

// ── MOTOR HELPERS ──────────────────────────────────────────
void setMotor(int pinFwd, int pinBwd, int speed, bool forward) {
  if (forward) {
    ledcWrite(pinFwd, speed);
    ledcWrite(pinBwd, 0);
  } else {
    ledcWrite(pinFwd, 0);
    ledcWrite(pinBwd, speed);
  }
}

// ── STOP MOTORS ────────────────────────────────────────────
void stopMotors() {
  digitalWrite(EN_L, LOW);
  digitalWrite(EN_R, LOW);
  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, 0);
  strcpy(currentDir, "stop");
  robotMoving = false;
  Serial.println("[MOTOR] STOP");
}

// ── EXECUTE MOVE ───────────────────────────────────────────
void executeMove(const char* dir) {
  strncpy(currentDir, dir, 11);
  currentDir[11] = '\0';

  if (strcmp(dir, "stop") != 0) {
    strncpy(savedDir, dir, 11);
    savedDir[11] = '\0';
    robotMoving = true;
  } else {
    robotMoving = false;
  }

  if (strcmp(dir, "forward") == 0 && obstacleAhead && hardwareEnabled) {
    stopMotors();
    PiSerial.print("BLOCKED:"); PiSerial.println(obstacleDistance);
    Serial.println("[MOTOR] BLOCKED");
    return;
  }

  if (strcmp(dir, "stop") == 0) {
    stopMotors();
    return;
  }

  digitalWrite(EN_L, HIGH);
  digitalWrite(EN_R, HIGH);

  if (strcmp(dir, "forward") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, true);
    setMotor(RPWM_R, LPWM_R, motorSpeed, true);
    Serial.println("[MOTOR] FORWARD");
  }
  else if (strcmp(dir, "backward") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, false);
    setMotor(RPWM_R, LPWM_R, motorSpeed, false);
    Serial.println("[MOTOR] BACKWARD");
  }
  else if (strcmp(dir, "left") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, false);
    setMotor(RPWM_R, LPWM_R, motorSpeed, true);
    Serial.println("[MOTOR] LEFT");
  }
  else if (strcmp(dir, "right") == 0) {
    setMotor(RPWM_L, LPWM_L, motorSpeed, true);
    setMotor(RPWM_R, LPWM_R, motorSpeed, false);
    Serial.println("[MOTOR] RIGHT");
  }
}

// ── ULTRASONIC ─────────────────────────────────────────────
int readUltrasonic() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 15000);
  if (duration == 0) return 999;
  int distance = duration * 0.034 / 2;
  if (distance > 300) return 999;
  return distance;
}

// ── SENSOR LOGIC ───────────────────────────────────────────
void readObstacleSensor() {
  if (!hardwareEnabled) return;
  if (millis() - lastSensorRead < 100) return;
  lastSensorRead = millis();

  const int SAMPLES = 5;
  long sum = 0;
  int validCount = 0;

  for (int i = 0; i < SAMPLES; i++) {
    int d = readUltrasonic();
    if (d >= 2 && d <= 300) {
      sum += d;
      validCount++;
    }
    delay(2);
  }

  if (validCount == 0) {
    obstacleDistance = 999;
    obstacleAhead = false;
    return;
  }

  int dist = sum / validCount;
  obstacleDistance = dist;

  static int obstacleCounter = 0;
  static int personCounter   = 0;

  if (dist < OBSTACLE_DIST) {
    obstacleCounter++;
  } else {
    obstacleCounter = 0;
  }

  obstacleAhead = (obstacleCounter >= 3);

  if (obstacleAhead && strcmp(currentDir, "forward") == 0) {
    strcpy(savedDir, "stop");
    stopMotors();
    PiSerial.print("OBSTACLE:");
    PiSerial.println(dist);
    Serial.print("[ULTRA] OBSTACLE: ");
    Serial.println(dist);
  }

  if (dist >= 10 && dist <= DETECT_DIST &&
      !robotMoving &&
      strcmp(currentDir, "stop") == 0) {
    personCounter++;
  } else {
    personCounter = 0;
  }

  unsigned long now = millis();
  if (personCounter >= 3 && now - lastGreeted > COOLDOWN_MS) {
    stopMotors();
    strcpy(savedDir, "stop");
    strcpy(currentDir, "stop");
    robotMoving = false;
    lastGreeted = now;
    PiSerial.print("PERSON_DETECTED:");
    PiSerial.println(dist);
    Serial.print("[ULTRA] PERSON: ");
    Serial.println(dist);
    personCounter = 0;
  }
}

// ── SERVO UPDATE ───────────────────────────────────────────
void updateServos() {
  unsigned long now      = millis();
  unsigned long interval = (unsigned long)map(servoSpeed, 0, 100, 50, 5);
  if (now - lastServoUpdate < interval) return;
  lastServoUpdate = now;

  int step = map(servoSpeed, 0, 100, 1, 8);

  if (currentPosL < targetPosL) {
    currentPosL = min(currentPosL + step, targetPosL);
    servoL.write(currentPosL);
  } else if (currentPosL > targetPosL) {
    currentPosL = max(currentPosL - step, targetPosL);
    servoL.write(currentPosL);
  }

  if (currentPosR < targetPosR) {
    currentPosR = min(currentPosR + step, targetPosR);
    servoR.write(currentPosR);
  } else if (currentPosR > targetPosR) {
    currentPosR = max(currentPosR - step, targetPosR);
    servoR.write(currentPosR);
  }

  if (currentHeadPos < targetHeadPos) {
    currentHeadPos = min(currentHeadPos + step, targetHeadPos);
    headServo.write(currentHeadPos);
  } else if (currentHeadPos > targetHeadPos) {
    currentHeadPos = max(currentHeadPos - step, targetHeadPos);
    headServo.write(currentHeadPos);
  }
}

// ── MOVE SERVOS ────────────────────────────────────────────
void moveServos(const char* part, int value, const char* hand) {
  value = constrain(value, 0, 2000);

  if (strcmp(part, "lateral") == 0) {
    if (strcmp(hand, "left") == 0) {
      targetPosL = map(value, 0, 2000, L_MIN, L_MAX);
      Serial.print("[SERVO] L -> "); Serial.println(targetPosL);
    }
    else if (strcmp(hand, "right") == 0) {
      targetPosR = map(value, 0, 2000, R_MIN, R_MAX);
      Serial.print("[SERVO] R -> "); Serial.println(targetPosR);
    }
    else if (strcmp(hand, "both") == 0) {
      targetPosL = map(value, 0, 2000, L_MIN, L_MAX);
      targetPosR = map(value, 0, 2000, R_MIN, R_MAX);
      Serial.print("[SERVO] BOTH L="); Serial.print(targetPosL);
      Serial.print(" R="); Serial.println(targetPosR);
    }
  }
  else if (strcmp(part, "headLR") == 0) {
    targetHeadPos = map(value, 0, 2000, HEAD_MIN, HEAD_MAX);
    Serial.print("[SERVO] HEAD -> "); Serial.println(targetHeadPos);
  }
  else {
    Serial.print("[IGNORE] "); Serial.println(part);
  }
}

// ── HOME SERVOS ────────────────────────────────────────────
void homeServos() {
  targetPosL    = L_CENTER;
  targetPosR    = R_CENTER;
  targetHeadPos = HEAD_CENTER;
  currentPosL   = L_CENTER;
  currentPosR   = R_CENTER;
  currentHeadPos= HEAD_CENTER;
  servoL.write(L_CENTER);
  servoR.write(R_CENTER);
  headServo.write(HEAD_CENTER);
  Serial.println("[SERVO] HOMED");
}

// ── LATCH SHUTDOWN SEQUENCE ────────────────────────────────
void doLatchShutdown() {
  if (shutdownTriggered) return;
  shutdownTriggered = true;

  Serial.println("[LATCH] Shutdown sequence started!");
  stopMotors();
  homeServos();

  // Tell Pi to shutdown
  PiSerial.println("SHUTDOWN");
  Serial.println("[LATCH] Sent SHUTDOWN to Pi");

  // Start 15s countdown
  for (int i = 15; i > 0; i--) {
    Serial.print("[LATCH] Power cut in ");
    Serial.print(i);
    Serial.println("s...");
    delay(1000);
  }

  // Cut power
  Serial.println("[LATCH] OFF — cutting power!");
  digitalWrite(LATCH_PIN, LOW);
}

// ── BUTTON CHECK ───────────────────────────────────────────
void checkButton() {
  if (shutdownTriggered) return;

  bool pressed = (digitalRead(SHUTDOWN_BTN) == LOW);

  if (pressed && !btnWasPressed) {
    btnPressStart = millis();
    btnWasPressed = true;
    Serial.println("[BTN] Pressed...");
  }
  else if (!pressed && btnWasPressed) {
    btnWasPressed = false;
    Serial.println("[BTN] Released — short press ignored");
  }
  else if (pressed && btnWasPressed) {
    unsigned long held = millis() - btnPressStart;
    if (held >= LONG_PRESS_MS) {
      Serial.println("[BTN] Long press 3s — initiating shutdown!");
      doLatchShutdown();
    }
  }
}

// ── SERIAL HANDLER ─────────────────────────────────────────
void handleSerial() {
  if (!PiSerial.available()) return;

  char buf[64];
  int len = 0;
  unsigned long t = millis();

  while (millis() - t < 30 && len < 63) {
    if (PiSerial.available()) {
      char c = PiSerial.read();
      if (c == '\n') break;
      buf[len++] = c;
    }
  }
  buf[len] = '\0';
  if (len > 0 && buf[len-1] == '\r') buf[--len] = '\0';
  if (len == 0) return;

  while (PiSerial.available()) PiSerial.read();

  Serial.print("[CMD] "); Serial.println(buf);

  if (strncmp(buf, "MOVE:", 5) == 0) {
    lastCmdTime = millis();
    if (strcmp(buf+5, "stop") == 0) {
      while (PiSerial.available()) PiSerial.read();
    }
    executeMove(buf + 5);
  }
  else if (strncmp(buf, "SPEED:", 6) == 0) {
    motorSpeed = map(constrain(atoi(buf+6), 0, 200), 0, 200, 0, 255);
    Serial.print("[MOTOR SPEED] "); Serial.println(motorSpeed);
    if (robotMoving) executeMove(currentDir);
  }
  else if (strncmp(buf, "TOPSPEED:", 9) == 0) {
    servoSpeed = constrain(atoi(buf+9), 0, 100);
    Serial.print("[SERVO SPEED] "); Serial.println(servoSpeed);
  }
  else if (strcmp(buf, "HOME") == 0 ||
           strcmp(buf, "HOME:left") == 0 ||
           strcmp(buf, "HOME:right") == 0 ||
           strcmp(buf, "HOME:head") == 0) {
    homeServos();
  }
  else if (strcmp(buf, "RESUME") == 0) {
    executeMove(savedDir);
    PiSerial.println("Resumed");
    Serial.print("[RESUME] "); Serial.println(savedDir);
  }
  else if (strncmp(buf, "POS:", 4) == 0) {
    char* p1 = strchr(buf+4, ':');
    if (!p1) return;
    char* p2 = strchr(p1+1, ':');
    if (!p2) return;
    *p1 = '\0'; *p2 = '\0';
    const char* part = buf+4;
    int         val  = atoi(p1+1);
    const char* hand = p2+1;
    moveServos(part, val, hand);
  }
  else if (strcmp(buf, "HARDWARE:ON") == 0) {
    hardwareEnabled = true;
    obstacleAhead   = false;
    Serial.println("[HW] Enabled");
    PiSerial.println("HW:ON");
  }
  else if (strcmp(buf, "HARDWARE:OFF") == 0) {
    hardwareEnabled = false;
    obstacleAhead   = false;
    Serial.println("[HW] Disabled");
    PiSerial.println("HW:OFF");
  }
  // ── LATCH OFF from Pi (app shutdown) ──────────────────
  else if (strcmp(buf, "LATCH:OFF") == 0) {
    if (shutdownTriggered) return;
    shutdownTriggered = true;
    Serial.println("[LATCH] App shutdown — waiting 15s...");
    PiSerial.println("LATCH:COUNTDOWN");
    stopMotors();
    homeServos();
    for (int i = 15; i > 0; i--) {
      Serial.print("[LATCH] Power cut in ");
      Serial.print(i);
      Serial.println("s...");
      delay(1000);
    }
    Serial.println("[LATCH] OFF — power cut!");
    digitalWrite(LATCH_PIN, LOW);
  }
}

// ── SETUP ──────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(500);

  // Latch HIGH immediately — keep power ON
  pinMode(LATCH_PIN, OUTPUT);
  digitalWrite(LATCH_PIN, HIGH);
  Serial.println("[LATCH] ON — power held");

  // Shutdown button — GPIO 35 is input only on ESP32
  // Connect button between GPIO 35 and GND with 10K pullup to 3.3V
  pinMode(SHUTDOWN_BTN, INPUT);
  Serial.println("[BTN] Shutdown button ready GPIO 35");

  PiSerial.begin(115200, SERIAL_8N1, 16, 17);
  delay(100);
  while (PiSerial.available()) PiSerial.read();
  Serial.println("[SERIAL] Buffer cleared");

  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);
  Serial.println("[ULTRA] SR04 ready");

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);

  servoL.setPeriodHertz(50);
  servoR.setPeriodHertz(50);
  headServo.setPeriodHertz(50);

  servoL.attach(SERVO_L_PIN,    500, 2400);
  servoR.attach(SERVO_R_PIN,    500, 2400);
  headServo.attach(HEAD_LR_PIN, 500, 2400);

  ledcAttach(RPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_L, PWM_FREQ, PWM_RES);
  ledcAttach(RPWM_R, PWM_FREQ, PWM_RES);
  ledcAttach(LPWM_R, PWM_FREQ, PWM_RES);

  ledcWrite(RPWM_L, 0); ledcWrite(LPWM_L, 0);
  ledcWrite(RPWM_R, 0); ledcWrite(LPWM_R, 0);

  pinMode(EN_L, OUTPUT); digitalWrite(EN_L, LOW);
  pinMode(EN_R, OUTPUT); digitalWrite(EN_R, LOW);

  lastCmdTime = millis();
  delay(200);
  homeServos();

  Serial.println("[SYSTEM] Nova ready");
  PiSerial.println("Nova ready.");
}

// ── LOOP ───────────────────────────────────────────────────
void loop() {
  checkButton();
  readObstacleSensor();
  handleSerial();
  updateServos();

  // Watchdog
  if (millis() - lastCmdTime > 500 && robotMoving) {
    Serial.println("[WATCHDOG] No command — stopping motors");
    stopMotors();
  }

  static unsigned long lastPrint = 0;
  if (millis() - lastPrint > 2000) {
    lastPrint = millis();
    Serial.print("Dist:"); Serial.print(obstacleDistance);
    Serial.print(" Obs:"); Serial.print(obstacleAhead);
    Serial.print(" HW:"); Serial.print(hardwareEnabled);
    Serial.print(" Dir:"); Serial.print(currentDir);
    Serial.print(" Btn:"); Serial.println(digitalRead(SHUTDOWN_BTN));
  }
}
