import time, os, threading, struct, subprocess
from faster_whisper import WhisperModel
from server import start_server, send_to_esp32
from audio_utils import record_audio, record_question, speak
from qa_store import find_answer, save_qa

model = WhisperModel("base", device="cpu", compute_type="int8")

# ── Joystick config ────────────────────────────────────────
JOYSTICK_DEV = "/dev/input/js0"
DEADZONE     = 5000
EVENT_SIZE   = 8
EVENT_FMT    = "IhBB"

joy_axis     = [0] * 8
joy_buttons  = [0] * 15
joy_dir      = "stop"
joy_speed    = 70

# ── Factory defaults ───────────────────────────────────────
FACTORY_ROBOT_NAME = "Lisa"
FACTORY_WIFI_SSID  = "LisaRobot"
FACTORY_WIFI_PASS  = "lisa1234"
FACTORY_IP         = "192.168.4.1"

# ── Wake word variants ─────────────────────────────────────
# All phonetic variations Whisper might transcribe "Lisa" as
WAKE_WORDS = [
    "lisa",
    "leasa",
    "leeza",
    "lissa",
    "lesa",
    "lysa",
    "liza",
    "lieza",
    "leisa",
    "lisar",
    "lisaa",
    "hey lisa",
    "hi lisa",
    "ok lisa",
    "okay lisa",
    "lisa please",
    "please lisa",
    "dear lisa",
    "elisa",
    "alisa",
]

def is_wake_word(text: str) -> bool:
    text = text.lower().strip()
    for w in WAKE_WORDS:
        if w in text:
            return True
    return False

# ── Eye helper ─────────────────────────────────────────────
def set_eye(state):
    try:
        from eyes import set_state
        set_state(state)
    except:
        pass

# ── Transcribe ─────────────────────────────────────────────
def transcribe(wav_path: str) -> str:
    if not wav_path or not os.path.exists(wav_path):
        return ""
    try:
        segments, _ = model.transcribe(
            wav_path,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500)
        )
        os.remove(wav_path)
        text = " ".join([s.text for s in segments]).lower().strip()
        text = text.replace(".", "").replace(",", "").replace("!", "").replace("?", "")
        return text.strip()
    except Exception as e:
        print(f"[STT] Error: {e}")
        return ""

# ── Q&A mode ───────────────────────────────────────────────
def qa_mode():
    from ai_fallback import ask_gpt
    print("[MODE] Q&A mode activated")
    set_eye("wake")
    speak("Yes?")
    set_eye("listening")
    wav_q = record_question()
    question = transcribe(wav_q)
    print(f"[QUESTION] {question!r}")
    if not question:
        set_eye("speaking")
        speak("I did not catch that.")
        set_eye("idle")
        return
    answer = find_answer(question)
    if answer:
        print("[QA] Found in local store")
        set_eye("speaking")
        speak(answer)
    else:
        print("[QA] Not found locally - asking GPT...")
        set_eye("thinking")
        speak("Let me think about that.")
        answer = ask_gpt(question, language="en")
        set_eye("speaking")
        speak(answer)
    set_eye("idle")

# ── Shutdown ───────────────────────────────────────────────
def do_shutdown():
    print("[SHUTDOWN] Starting safe shutdown")
    try:
        set_eye("obstacle")
    except: pass
    try:
        speak("Shutting down. Goodbye!")
    except Exception as e:
        print(f"[SHUTDOWN] Speak error: {e}")
    try:
        send_to_esp32("MOVE:stop")
        time.sleep(1)
    except Exception as e:
        print(f"[SHUTDOWN] Stop error: {e}")
    try:
        send_to_esp32("HOME")
        time.sleep(2)
    except Exception as e:
        print(f"[SHUTDOWN] Home error: {e}")
    try:
        send_to_esp32("LATCH:OFF")
        print("[SHUTDOWN] LATCH:OFF sent — ESP32 cuts power in 15s")
        time.sleep(1)
    except Exception as e:
        print(f"[SHUTDOWN] Latch error: {e}")
    print("[SHUTDOWN] Executing poweroff")
    subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"])

# ── Factory Reset ──────────────────────────────────────────
def do_factory_reset():
    print("[FACTORY] Resetting to factory defaults...")
    set_eye("thinking")
    try:
        speak("Resetting to factory settings. Please wait.")
    except: pass

    # Reset settings.json
    from settings import save_settings
    save_settings({
        "robot_name":      FACTORY_ROBOT_NAME,
        "welcome_speech":  f"Hello, I am {FACTORY_ROBOT_NAME}, your robot assistant",
        "language":        "en",
        "voice":           "female",
        "chatgpt_enabled": True
    })
    print("[FACTORY] settings.json reset")

    # Clear qa_store.json
    save_qa({})
    print("[FACTORY] qa_store.json cleared")

    # Delete ALL saved wifi connections then create fresh one
    try:
        result = subprocess.run(
            ['sudo', 'nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
            capture_output=True, text=True
        )
        for line in result.stdout.splitlines():
            if ':wifi' in line:
                conn_name = line.split(':')[0]
                print(f"[FACTORY] Deleting wifi connection: {conn_name}")
                subprocess.run(
                    ['sudo', 'nmcli', 'connection', 'delete', conn_name],
                    capture_output=True
                )

        # Create fresh hotspot with fixed IP
        subprocess.run([
            'sudo', 'nmcli', 'connection', 'add',
            'type', 'wifi',
            'ifname', 'wlan0',
            'con-name', FACTORY_WIFI_SSID,
            'autoconnect', 'yes',
            'ssid', FACTORY_WIFI_SSID,
            '802-11-wireless.mode', 'ap',
            '802-11-wireless.band', 'bg',
            'ipv4.method', 'shared',
            'ipv4.addresses', f'{FACTORY_IP}/24',
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', FACTORY_WIFI_PASS
        ], capture_output=True)

        # Set highest autoconnect priority
        subprocess.run([
            'sudo', 'nmcli', 'connection', 'modify',
            FACTORY_WIFI_SSID,
            'connection.autoconnect-priority', '100'
        ], capture_output=True)

        # Bring it up
        subprocess.run(
            ['sudo', 'nmcli', 'connection', 'up', FACTORY_WIFI_SSID],
            capture_output=True
        )
        print(f"[FACTORY] WiFi → '{FACTORY_WIFI_SSID}' / '{FACTORY_WIFI_PASS}' @ {FACTORY_IP}")
    except Exception as e:
        print(f"[FACTORY] WiFi reset error: {e}")

    set_eye("idle")
    try:
        speak(f"Factory reset complete. Connect to WiFi {FACTORY_WIFI_SSID} with password {FACTORY_WIFI_PASS}.")
    except: pass

    print("[FACTORY] Done — restarting service")
    time.sleep(3)
    subprocess.run(['sudo', 'systemctl', 'restart', 'piassistant'])

# ── Joystick ───────────────────────────────────────────────
def get_direction():
    y = joy_axis[1]
    x = joy_axis[2]
    if abs(y) >= DEADZONE and abs(y) >= abs(x):
        return "forward" if y < -DEADZONE else "backward"
    if abs(x) >= DEADZONE:
        return "left" if x < -DEADZONE else "right"
    return "stop"

def joystick_loop():
    global joy_dir, joy_speed
    last_servo_time = 0
    headLR_pos  = 1000
    latL_pos    = 1000
    latR_pos    = 1000

    def axis_to_servo(val):
        return int((val + 32767) / 65534 * 2000)

    def keepalive():
        prev_dir = "stop"
        while True:
            time.sleep(0.3)
            if joy_dir != "stop":
                send_to_esp32(f"MOVE:{joy_dir}")
            elif prev_dir != "stop":
                for _ in range(3):
                    send_to_esp32("MOVE:stop")
                    time.sleep(0.05)
            prev_dir = joy_dir
    threading.Thread(target=keepalive, daemon=True).start()

    while True:
        try:
            js = open(JOYSTICK_DEV, "rb")
            print("[JOY] Joystick connected")
            send_to_esp32(f"SPEED:{joy_speed}")

            while True:
                event = js.read(EVENT_SIZE)
                if not event:
                    break
                t, value, etype, number = struct.unpack(EVENT_FMT, event)
                if etype & 0x80:
                    continue

                if etype == 2:
                    if number < len(joy_axis):
                        joy_axis[number] = value

                    if number in [1, 2]:
                        if not joy_buttons[14] and not joy_buttons[13] and not joy_buttons[1] and not joy_buttons[3]:
                            new_dir = get_direction()
                            if new_dir != joy_dir:
                                joy_dir = new_dir
                                send_to_esp32(f"MOVE:{joy_dir}")
                                print(f"[JOY] {joy_dir}")
                                set_eye(joy_dir if joy_dir != "stop" else "idle")

                    if time.time() - last_servo_time > 0.05:
                        if joy_buttons[14] and number == 3 and abs(value) > DEADZONE:
                            latR_pos = max(0, min(2000, axis_to_servo(value)))
                            send_to_esp32(f"POS:lateral:{latR_pos}:right")
                            last_servo_time = time.time()

                        elif joy_buttons[13] and number == 1 and abs(value) > DEADZONE:
                            latL_pos = max(0, min(2000, axis_to_servo(value)))
                            send_to_esp32(f"POS:lateral:{latL_pos}:left")
                            last_servo_time = time.time()

                        elif joy_buttons[1] and number == 3 and abs(value) > DEADZONE:
                            pos = max(0, min(2000, axis_to_servo(value)))
                            latL_pos = latR_pos = pos
                            send_to_esp32(f"POS:lateral:{pos}:both")
                            last_servo_time = time.time()

                        elif joy_buttons[3] and number == 1 and abs(value) > DEADZONE:
                            headLR_pos = max(0, min(2000, axis_to_servo(value)))
                            send_to_esp32(f"POS:headLR:{headLR_pos}:left")
                            last_servo_time = time.time()

                elif etype == 1:
                    if number < len(joy_buttons):
                        joy_buttons[number] = value

                    if value == 1:
                        if number == 0:
                            joy_dir = "stop"
                            send_to_esp32("MOVE:stop")
                            set_eye("idle")
                            print("[JOY] stop")

                        elif number == 7:
                            joy_speed = min(100, joy_speed + 10)
                            send_to_esp32(f"SPEED:{joy_speed}")
                            print(f"[JOY] Speed: {joy_speed}%")

                        elif number == 6:
                            joy_speed = max(10, joy_speed - 10)
                            send_to_esp32(f"SPEED:{joy_speed}")
                            print(f"[JOY] Speed: {joy_speed}%")

                        elif number == 8:
                            headLR_pos = latL_pos = latR_pos = 1000
                            send_to_esp32("HOME")
                            print("[JOY] Servos homed")

                        elif number == 9:
                            joy_dir = "stop"
                            headLR_pos = latL_pos = latR_pos = 1000
                            send_to_esp32("MOVE:stop")
                            send_to_esp32("HOME")
                            set_eye("idle")
                            print("[JOY] Full stop + home")

                    if value == 0 and number in [13, 14, 1, 2]:
                        joy_dir = "stop"
                        send_to_esp32("MOVE:stop")
                        set_eye("idle")

            js.close()
            print("[JOY] Joystick disconnected — retrying in 3s...")

        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[JOY] Error: {e}")
        time.sleep(3)

# ── Voice listening loop ───────────────────────────────────
def listening_loop():
    print("[MAIN] Lisa ready. Listening for wake word...")
    set_eye("speaking")
    speak("Hello, I am Lisa, your robot assistant.")
    set_eye("idle")

    while True:
        wav  = record_audio(duration=3)
        text = transcribe(wav)
        print(f"[STT] Heard: {text!r}")

        if is_wake_word(text):
            print(f"[MAIN] Wake word detected in: {text!r}")
            qa_mode()

        time.sleep(0.1)

# ── Entry point ────────────────────────────────────────────
if __name__ == "__main__":
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    print("[HTTP] Server started on port 5000")
    time.sleep(1)

    joy_thread = threading.Thread(target=joystick_loop, daemon=True)
    joy_thread.start()
    print("[JOY] Joystick thread started")

    try:
        from eyes import start_eyes
        start_eyes()
        print("[EYES] Started")
    except Exception as e:
        print(f"[EYES] Not started: {e}")

    try:
        listening_loop()
    except KeyboardInterrupt:
        try:
            from eyes import stop_eyes
            stop_eyes()
        except:
            pass
        send_to_esp32("MOVE:stop")
        print("\n[MAIN] Stopped.")
