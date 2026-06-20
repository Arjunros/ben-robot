# Nova Robot — Complete Setup Guide

## Overview
Nova is an AI-powered robot built on Raspberry Pi 5 with ESP32 for motor, servo, and sensor control. It supports voice wake-word activation, local Q&A with ChatGPT fallback, joystick teleoperation, obstacle/person detection, LED eye expressions, OTA software updates, and a safe shutdown system with a physical latch-power button.

---

## Hardware Requirements

| Component | Specification |
|-----------|---------------|
| Raspberry Pi 5 | 8GB RAM |
| MicroSD Card | 64GB Class 10 |
| ESP32 Dev Board | CP2102 USB chip |
| Servo Motors | x3 (Left arm, Right arm, Head L/R) |
| Motor Driver | BTN7960 / IBT-2 (x2 channels) |
| Distance Sensor | HC-SR04 Ultrasonic |
| Microphone | USB mic or INMP441 (I2S) |
| Speaker | USB Soundcard |
| LED Eyes | WS2812 NeoPixel Ring x2 |
| Latch Power Circuit | Self-latching relay/MOSFET circuit |
| Shutdown Button | Momentary push button |
| Power Supply | 5V high-current external (motors/servos) |

---

## ESP32 Pin Map

| GPIO | Function |
|------|----------|
| 19 | RPWM_L Left Motor Forward |
| 18 | LPWM_L Left Motor Backward |
| 15 | RPWM_R Right Motor Forward |
| 2  | LPWM_R Right Motor Backward |
| 5  | EN_L Left Motor Enable |
| 4  | EN_R Right Motor Enable |
| 32 | Ultrasonic TRIG |
| 25 | Ultrasonic ECHO |
| 23 | Latch Power Control (HIGH = power held ON) |
| 35 | Shutdown Button (input only, hold 3s) |
| 26 | Servo Left Arm |
| 27 | Servo Right Arm |
| 14 | Servo Head Left/Right |
| 16 | RX2 (Pi Serial TX) |
| 17 | TX2 (Pi Serial RX) |

---

## Raspberry Pi Wiring

| Component | Pi Connection |
|-----------|----------------|
| ESP32 TX (pin 17) | Pi RX (ttyAMA0) |
| ESP32 RX (pin 16) | Pi TX (ttyAMA0) |
| WS2812 Left Eye | GPIO (per eyes.py config) |
| WS2812 Right Eye | GPIO (per eyes.py config) |
| Joystick | USB / /dev/input/js0 |
| Microphone | USB or I2S per audio_utils.py |
| Speaker | USB Soundcard |

---

## Power & Latch System

Nova uses a self-latching power circuit so the robot can fully power itself off — not just the Pi.

```
Power button pressed briefly
  → Latch circuit closes
  → Power flows to ESP32 + Pi
  → ESP32 boots, sets GPIO 23 HIGH immediately
  → Latch stays ON (self-held)
  → Pi boots, piassistant service starts
```

### Shutdown — two paths

**1. App / Web shutdown button**
```
App calls /shutdown
  → Pi stops motors, homes servos, speaks "Shutting down"
  → Pi sends LATCH:OFF to ESP32
  → Pi executes system shutdown
  → ESP32 waits 15 seconds
  → ESP32 sets GPIO 23 LOW → power cuts completely
```

**2. Physical button (GPIO 35, hold 3 seconds)**
```
Button held 3s on ESP32
  → ESP32 stops motors, homes servos
  → ESP32 sends "SHUTDOWN" to Pi over serial
  → ESP32 starts its own 15-second countdown
  → Pi (in parallel) speaks "Shutting down", stops motors,
    homes servos, executes system shutdown
  → After 15s ESP32 sets GPIO 23 LOW → power cuts completely
```

The 15-second delay guarantees the Pi has fully halted (avoiding SD card corruption) before power is physically cut.

---

## Step 1 — Flash Raspberry Pi OS

1. Download Raspberry Pi Imager: https://www.raspberrypi.com/software
2. Select **Raspberry Pi OS Bookworm 64-bit**
3. Advanced settings:
   - Enable SSH
   - Username: `nova`
   - Set password
4. Flash to MicroSD and boot

---

## Step 2 — System Packages

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y python3-pip python3-venv git ffmpeg sox \
    libportaudio2 portaudio19-dev libsndfile1 \
    python3-serial alsa-utils udev \
    python3-lgpio python3-rpi.gpio \
    swig python3-dev libgpiod-dev
```

---

## Step 3 — Enable Serial Port for ESP32

```bash
sudo raspi-config
```

Interface Options → Serial Port:
- Login shell over serial: **NO**
- Serial port hardware enabled: **YES**

Reboot and verify:
```bash
sudo reboot
ls -la /dev/ttyAMA0
```

---

## Step 4 — Install Piper TTS

```bash
cd /tmp
wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_arm64.tar.gz
tar -xzf piper_arm64.tar.gz
sudo cp piper/piper /usr/local/bin/
```

Download voice model:
```bash
mkdir -p ~/pi_assistant/voices
cd ~/pi_assistant/voices

wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json

wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium/en_US-ryan-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium/en_US-ryan-medium.onnx.json
```

Test:
```bash
echo "Hello I am Nova" | piper \
  --model ~/pi_assistant/voices/en_US-amy-medium.onnx \
  --output_raw | aplay -r 22050 -f S16_LE -c 1 -
```

---

## Step 5 — Clone Repository

```bash
git clone https://github.com/Arjunros/nova-robot.git ~/pi_assistant
cd ~/pi_assistant
```

---

## Step 6 — Python Virtual Environment

```bash
cd ~/pi_assistant
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If `lgpio` fails to import:
```bash
cp /usr/lib/python3/dist-packages/lgpio.py venv/lib/python3.*/site-packages/
cp /usr/lib/python3/dist-packages/_lgpio*.so venv/lib/python3.*/site-packages/
```

---

## Step 7 — Create Config Files from Defaults

```bash
cd ~/pi_assistant
cp settings.default.json settings.json
cp qa_store.default.json qa_store.json
cp poses.default.json poses.json
```

`settings.json` defaults:
```json
{
  "robot_name": "Nova",
  "welcome_speech": "Hello, I am Nova, your robot assistant",
  "language": "en",
  "voice": "female",
  "chatgpt_enabled": true
}
```

ChatGPT fallback is **enabled by default** with no subscription/expiry gate — if local Q&A has no answer, it always falls through to `ask_gpt()`.

---

## Step 8 — WiFi Hotspot Setup

```bash
sudo nmcli device wifi hotspot \
  ssid NovaRobot \
  password nova1234 \
  ifname wlan0

sudo nmcli connection modify NovaRobot connection.autoconnect yes
```

Pi IP: **192.168.4.1**
App/Browser URL: **http://192.168.4.1:5000**

---

## Step 9 — Install Systemd Service

```bash
sudo cp setup/piassistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable piassistant
sudo systemctl start piassistant
```

Check logs:
```bash
sudo journalctl -u piassistant -f
```

---

## Step 10 — Upload ESP32 Code

1. Install Arduino IDE: https://www.arduino.cc/en/software
2. Add ESP32 board URL in Preferences:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
3. Board Manager → install **ESP32 by Espressif**
4. Library Manager → install **ESP32Servo**
5. Open `esp32/nova_esp32.ino`
6. Board: ESP32 Dev Module
7. Select correct COM port
8. Upload

---

## Step 11 — Verify Everything

```bash
# Service status
sudo systemctl status piassistant

# ESP32 connected
ls -la /dev/ttyAMA0

# Audio devices
arecord -l && aplay -l

# API health check
curl http://localhost:5000/ping
```

Expected:
```json
{"status": "ok", "message": "Pi is alive"}
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ping` | GET | Health check |
| `/status` | GET | Robot status |
| `/save-audio` | POST | Save robot name, welcome speech, Q&As |
| `/qa/list` | GET | List all Q&As |
| `/qa/add` | POST | Add Q&A pair |
| `/qa/delete` | POST | Delete Q&A |
| `/qa/update` | POST | Update existing Q&A |
| `/qa/test-voice` | POST | Test TTS voice |
| `/move` | GET | Motor control (`dir=forward/backward/left/right/stop`) |
| `/speed` | GET | Motor speed (`value=0-100`) |
| `/topspeed` | GET | Servo movement speed (`value=0-100`) |
| `/position` | GET | Servo position (`part=lateral/headLR`, `value=0-2000`, `hand=left/right/both`) |
| `/home` | GET | Home all servos |
| `/settings` | GET/POST | Get or update robot settings |
| `/update` | GET/POST | Pull latest code from GitHub and restart |
| `/version` | GET | Get current installed version |
| `/shutdown` | GET | Safe shutdown with latch power-off |
| `/restart` | GET | Reboot Pi |

---

## ESP32 Serial Protocol (Pi ↔ ESP32)

| Command (Pi → ESP32) | Description |
|------------------------|--------------|
| `MOVE:forward/backward/left/right/stop` | Drive motors |
| `SPEED:0-200` | Set motor speed |
| `TOPSPEED:0-100` | Set servo movement speed |
| `HOME` | Home all servos |
| `RESUME` | Resume last saved direction |
| `POS:lateral:VALUE:left/right/both` | Move arm servo(s) |
| `POS:headLR:VALUE:left` | Move head servo |
| `HARDWARE:ON/OFF` | Enable/disable obstacle sensor logic |
| `LATCH:OFF` | Begin 15s shutdown countdown, then cut power |

| Message (ESP32 → Pi) | Description |
|------------------------|--------------|
| `PERSON_DETECTED:<cm>` | Person detected within range |
| `OBSTACLE:<cm>` | Obstacle detected ahead while moving forward |
| `BLOCKED:<cm>` | Forward move blocked due to obstacle |
| `CLEAR:` | Path is clear again |
| `SHUTDOWN` | Physical button held 3s — Pi should begin shutdown |
| `LATCH:COUNTDOWN` | ESP32 has started its 15s power-cut countdown |
| `HW:ON` / `HW:OFF` | Hardware obstacle detection acknowledged |
| `Nova ready.` | ESP32 boot complete |

---

## Wake Word

- Say the robot name (default: **Nova**) to activate Q&A mode
- Robot asks "How can I help?", listens for 5 seconds, answers from local Q&A store first, then falls back to ChatGPT if no local match
- Robot name can be changed via the app/settings — wake word updates automatically every loop

---

## Joystick Controls

| Control | Action |
|---------|--------|
| Left stick Y | Forward / Backward |
| Right stick X | Left / Right turn |
| Button 0 | Stop |
| Button 6 / 7 | Decrease / Increase speed |
| Button 8 | Home all servos |
| Button 9 | Full stop + home |
| Hold Button 13 + Left stick X | Move left arm servo |
| Hold Button 14 + Right stick X | Move right arm servo |
| Hold Button 1 + Right stick X | Move both arm servos together |
| Hold Button 3 + Left stick X | Move head left/right |

---

## OTA (Over-The-Air) Updates

Nova checks GitHub for new versions automatically:

```
Every 24 hours:
  Pi reads https://raw.githubusercontent.com/Arjunros/nova-robot/main/version.txt
  Compares with local version.txt
  If different:
    → speaks "Software update available"
    → git pull
    → pip install -r requirements.txt
    → systemctl restart piassistant
```

To push an update from your dev machine:
```bash
cd ~/pi_assistant
echo "1.0.1" > version.txt
git add .
git commit -m "Version 1.0.1 - description of changes"
git push
```

To trigger an immediate manual update on a specific robot:
```bash
curl http://localhost:5000/update
```

**Note:** Since this repo is Private, the OTA version-check needs a GitHub token in the Authorization header, or the repo must be made Public for the simple `urllib.request.urlopen()` call to work without auth. See `server.py` → `check_for_updates()`.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No audio output | Check USB soundcard connected. Run `aplay -l` |
| Mic not recording | Check mic wiring/USB connection. Run `arecord -l` |
| ESP32 not connecting | Check `/dev/ttyAMA0` exists. Confirm serial enabled in `raspi-config` |
| Wake word not working | Check `settings.json` has correct `robot_name` |
| Motors not moving | Check `EN_L`/`EN_R` pins HIGH. Check power supply |
| Service not starting | Run `sudo journalctl -u piassistant -n 50` |
| Robot doesn't power off fully | Check ESP32 serial monitor for `[LATCH]` countdown logs. Confirm GPIO 23 wiring to latch circuit |
| Button hold not triggering shutdown | Confirm GPIO 35 wiring — button to GND with external 10K pull-up to 3.3V (GPIO 35 has no internal pull resistor) |
| OTA update fails | Check `git remote -v` has a valid token. Confirm repo accessible from Pi: `git pull` manually |

---

## Manufacturing Checklist

- [ ] Flash OS and configure username `nova`
- [ ] Install all system packages
- [ ] Enable serial port for ESP32
- [ ] Install Piper TTS and voice model
- [ ] Clone repository
- [ ] Setup Python venv and install requirements
- [ ] Create config files from defaults
- [ ] Setup WiFi hotspot
- [ ] Install systemd service
- [ ] Upload ESP32 code (`esp32/nova_esp32.ino`)
- [ ] Wire latch power circuit to GPIO 23
- [ ] Wire shutdown button to GPIO 35 (with external pull-up)
- [ ] Test all API endpoints
- [ ] Test wake word
- [ ] Test motors and servos via joystick
- [ ] Test ultrasonic obstacle/person detection
- [ ] Test LED eyes
- [ ] Test app shutdown (`/shutdown`)
- [ ] Test physical button shutdown (hold 3s)
- [ ] Confirm power fully cuts after 15s in both shutdown paths

---

## Support

For manufacturing or hardware issues, contact the R&D team.
