# Ben Robot — Raspberry Pi Assistant with Touch Display

Ben is a voice-controlled assistant robot: wake-word speech recognition, offline
Q&A with ChatGPT fallback, face recognition, motor/servo control through an
ESP32, a phone app over a WiFi hotspot, and a touch display showing live
conversation captions, videos, weather and time.

```
                 ┌──────────────── Phone App ───────────────┐
                 │        connects to BenRobot WiFi          │
                 │        http://192.168.4.1:5000            │
                 └────────────────────┬──────────────────────┘
                                      │ REST
┌───────────────┐   USB    ┌──────────▼──────────┐  serial   ┌──────────────┐
│  USB Speaker  │◄─────────┤   Raspberry Pi 5    ├──────────►│    ESP32     │
│  INMP441 Mic  ├─────────►│  Flask + Whisper +  │ ttyAMA0   │ motors/servos│
│  (I2S HAT)    │          │  Piper + InsightFace│ 115200    │ TF-Luna      │
└───────────────┘          │  + Display server   │           │ latch + btn  │
                           └──────────┬──────────┘           └──────────────┘
                                      │ HTTP (localhost)
                           ┌──────────▼──────────┐
                           │  Touch Display      │
                           │  Firefox kiosk      │
                           │  /display           │
                           └─────────────────────┘
```

---

## Hardware

| Component | Details |
|---|---|
| Computer | Raspberry Pi 5, Raspberry Pi OS Bookworm 64-bit |
| Microphone | INMP441 I2S MEMS (googlevoicehat-soundcard overlay) |
| Speaker | USB PnP sound card + amplified speaker |
| Display | HDMI/DSI touch screen, Firefox kiosk |
| MCU | ESP32 WROOM on `/dev/ttyAMA0` @ 115200 |
| Motors | 2× BTN7960 drivers (drive base) |
| Distance | TF-Luna (I2C) — obstacle stop + person detection |
| Power latch | ESP32 GPIO 23 holds power; GPIO 35 button (external 10K pull-up) |

### Power / shutdown behaviour

* App **Shutdown** → Pi stops motors, homes servos, plays `shutdown.wav`,
  sends `LATCH:OFF` → ESP32 counts 15 s → cuts power.
* **Button hold 3 s** (ESP32 GPIO 35) → ESP32 stops/homes, sends `SHUTDOWN`
  to the Pi, starts its own 15 s countdown. Pi shuts down in parallel.
* The Pi never calls a blocking `shutdown` — it schedules
  `Popen("sleep 2 && shutdown -h now")` then `os._exit(0)`
  (prevents the SIGSEGV-on-shutdown class of bugs).

---

## Software stack

| Layer | Tool |
|---|---|
| Wake word / STT | faster-whisper (`tiny`/`base`, CPU int8), wake-word variant list |
| TTS | Piper (`en_US-amy-medium`, `en_US-ryan-medium`) |
| Q&A | `qa_pairs.json` (app-managed list) → exact/partial match → GPT fallback |
| Faces | InsightFace `buffalo_sc`; images from app in `uploaded_faces/`, speech in `vision_data.json`, embeddings cached in `faces_db.pkl` |
| Server | Flask on `:5000` — full app contract (movement, joints, fingers, poses/loop, Q&A, faces, settings, estop, shutdown) |
| Display | `display_addon.py` + `display.html` — SSE captions, video library, YouTube (yt-dlp), WiFi panel, weather + clock |

Key robustness patterns baked in (learned the hard way):

* **Self-locating paths** — `BASE_DIR = os.path.dirname(os.path.abspath(__file__))`.
  Never hardcode `/home/<user>/…`; clones on any username just work.
* **Name-based audio card detection** (`googlevoice…`, `usb audio…`) with
  `plughw:` — card numbers shuffle between boots.
* **`audio_lock`** around record + speak — no "Device busy" collisions.
* **2 s backoff** after `arecord` failure — no log-flood / zombie spawn.
* **PipeWire masked** — it seizes `/dev/snd` on Bookworm and blocks ALSA.

---

## Fresh SD card setup

### 1. Flash
Raspberry Pi Imager → Pi OS Bookworm 64-bit → enable SSH, username `ben`.

### 2. System packages
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git ffmpeg sox \
    libportaudio2 portaudio19-dev libsndfile1 python3-serial alsa-utils \
    python3-lgpio swig python3-dev libgpiod-dev firefox curl
sudo reboot   # ALWAYS reboot after a kernel upgrade before testing audio
```

### 3. Enable interfaces
`/boot/firmware/config.txt` must contain:
```
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard
dtparam=uart0=on
```
`sudo raspi-config` → Serial: login shell **No**, hardware **Yes**. Reboot.

### 4. Mask PipeWire (mandatory)
```bash
systemctl --user stop pipewire pipewire.socket pipewire-pulse pipewire-pulse.socket wireplumber
systemctl --user disable --now pipewire pipewire.socket pipewire-pulse pipewire-pulse.socket wireplumber
systemctl --user mask pipewire pipewire.socket pipewire-pulse pipewire-pulse.socket wireplumber
```

### 5. Piper
```bash
cd /tmp && wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_arm64.tar.gz
tar -xzf piper_arm64.tar.gz
sudo cp piper/piper /usr/local/bin/ && sudo cp piper/lib*.so* /usr/local/lib/
sudo cp -r piper/espeak-ng-data /usr/local/share/ && sudo ldconfig
```

### 6. Clone + venv
```bash
git clone https://github.com/Arjunros/ben-robot.git ~/pi_assistant
cd ~/pi_assistant
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# lgpio into the venv:
cp /usr/lib/python3/dist-packages/lgpio.py venv/lib/python3.*/site-packages/
cp /usr/lib/python3/dist-packages/_lgpio*.so venv/lib/python3.*/site-packages/
```

### 7. Voice models
```bash
mkdir -p voices && cd voices
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium/en_US-ryan-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium/en_US-ryan-medium.onnx.json
cd ..
```

### 8. Shutdown audio
```bash
echo "Shutting down. Goodbye!" | piper --model voices/en_US-ryan-medium.onnx --output_raw | \
  sox -t raw -r 22050 -e signed-integer -b 16 -c 1 - \
      -t wav -r 48000 -e signed-integer -b 16 -c 1 shutdown.wav
```

### 9. Hotspot (fixed IP — create BEFORE bringing up)
```bash
sudo nmcli connection add type wifi ifname wlan0 con-name BenRobot autoconnect yes \
  ssid BenRobot 802-11-wireless.mode ap 802-11-wireless.band bg \
  ipv4.method shared ipv4.addresses 192.168.4.1/24 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk ben12345
sudo nmcli connection modify BenRobot connection.autoconnect-priority 100
sudo nmcli connection up BenRobot
ip addr show wlan0 | grep inet   # must be 192.168.4.1
```
Password minimum 8 chars. If the IP comes up 10.42.0.1, delete every wifi
connection and recreate with the block above.

### 10. Services
```bash
sudo cp setup/piassistant.service /etc/systemd/system/
sudo cp setup/robot-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now piassistant robot-display
```
`robot-display` waits for `/ping` then launches Firefox kiosk at
`http://127.0.0.1:5000/display` (localhost — independent of any network).
Disable screen blanking: `sudo raspi-config nonint do_blanking 1`.
Firefox: set `browser.sessionstore.resume_from_crash=false`
(latch power-cuts otherwise trigger the restore prompt).

### 11. ESP32
Flash `esp32/ben_esp32.ino` (Arduino IDE, ESP32 Dev Module).
Wiring: Pi TXD→ESP32 RX2(16), Pi RXD←TX2(17), common GND.
Boot test: `Ben ready.` + `Dist:` heartbeat every 2 s in the service log.

### 12. Verify
```bash
sudo journalctl -u piassistant -f
```
Say **"Ben"** → eyes wake → "How can I help?" → ask a saved question.
Say **"hi"** facing the camera → face recognition greeting.
Open the app → joystick, sliders, fingers, volume, estop, shutdown.

---

## Display features (`/display`)

* **Live captions** — "You: …" (question event) / "Ben: …" (speech_start).
* **Video library** — upload MP4/WebM via the UI; stored in `videos/`.
* **Online video** — "Ben, play a video about lions" → yt-dlp top result.
  Requires `pip install yt-dlp` and internet.
* **WiFi panel** — scan/join a router network on `wlan0` (client side; the
  AP on the hotspot interface is never touched).
* **Clock + weather** — Open-Meteo, no API key, 10-min cache. Set
  `WEATHER_LAT` / `WEATHER_LON` in `display_addon.py` per deployment.
  Clock works fully offline; weather hides itself when offline.

Kiosk control: `sudo systemctl stop|start robot-display`.

---

## App endpoints (summary)

`/move?dir=` `/speed?value=` (≤100 drive, >100 motion) `/estop`
`/position?part=&value=&hand=` (value may be JSON for fingers)
`POST /fingers` `/home` `/hand?value=`
`/save_pose` `/loop_start|stop|undo|delete` `/run?value=`
`/save-audio?welcomeSpeech=&robotName=` `POST /qa/add` `/delete_qa?id=`
`/online_chat?enabled=` `/settings?volume=` `/settings?wifi_ssid=&wifi_password=`
`/face_tracking?value=` `POST /upload_face` `POST /upload_faces` `/delete_face?id=`
`/obstacle_avoidance?value=up|down` `/status` `/shutdown` `/restart`
Unknown routes hit a catch-all that logs `(UNHANDLED)` — watch for those when
the app updates.

## Pi → ESP32 serial protocol

```
MOVE:forward|backward|left|right|stop     SPEED:0-100   TOPSPEED:0-100
POS:<part>:<0-2000>:<left|right|both>     HOME          RESUME
HARDWARE:ON|OFF                            LATCH:OFF (15 s power cut)
```
ESP32 → Pi: `Dist:<cm>` heartbeat · `PERSON_DETECTED:<cm>` · `BLOCKED:<cm>` ·
`SHUTDOWN` (button).

---

## Updating a robot (OTA)

```bash
cd ~/pi_assistant && git pull
source venv/bin/activate && pip install -r requirements.txt -q
sudo systemctl restart piassistant
```
Push changes from the master/dev unit only; production units pull.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| TTS "speaks" but silent | Wrong hardcoded path (voices or piper) from another unit's clone — check `grep home/ audio_utils.py`; use self-locating paths |
| `Device or resource busy` | PipeWire grabbing ALSA → Step 4 masking; or overlapping speak/record → ensure `audio_lock` present |
| `arecord: Invalid argument` | Raw `hw:` instead of `plughw:`; or kernel half-updated → reboot; or zombies holding `/dev/snd` → `sudo fuser -v /dev/snd/*` |
| Mic dead after `apt upgrade` | Kernel updated without reboot → reboot, retest |
| Log flooded with mic errors | Backoff missing → 2 s sleep after arecord failure |
| Hotspot at 10.42.0.1 | Profile created before IP set → delete all wifi profiles, recreate per Step 9 |
| Old SSID returns after reboot | Stale `Hotspot`/`Hotspot-1` profiles → delete; BenRobot priority 100 |
| Service dies SIGSEGV on shutdown | Blocking shutdown call or `stop_eyes()`/piper in shutdown path → use the Popen + `os._exit(0)` pattern, pre-generated `shutdown.wav` |
| App action does nothing, log shows `(UNHANDLED)` | New app endpoint — add the route to `server.py` |
| Q&A/faces "not saving" | Look for a phantom `/home/<other-user>/pi_assistant` dir created by a hardcoded path |
| Firefox "Restore session?" on boot | Set `browser.sessionstore.resume_from_crash=false` |
| Display blank | `systemctl status robot-display`; server up? `curl 127.0.0.1:5000/ping` |

---

## New-unit manufacturing checklist

1. Flash SD (user `ben`) → Steps 2–5
2. Clone repo → Steps 6–8
3. Hotspot Step 9 → confirm `192.168.4.1`
4. Services Step 10 → reboot → display kiosk appears
5. Flash ESP32 → heartbeat visible in journal
6. Full test: wake word, Q&A, face, app joystick/servos/fingers/volume,
   video on display, app shutdown, button shutdown
7. `sudo apt upgrade` done? → reboot → retest mic before packing
