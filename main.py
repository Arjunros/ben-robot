import time, os, threading, struct, subprocess
from faster_whisper import WhisperModel
from server import start_server, send_to_esp32
from audio_utils import record_audio, speak
from qa_store import find_answer, save_qa
from face_utils import scan_face_from_camera

model = WhisperModel("tiny", device="cpu", compute_type="int8")

# ── Joystick config ────────────────────────────────────────
JOYSTICK_DEV = "/dev/input/js0"
DEADZONE     = 5000
EVENT_SIZE   = 8
EVENT_FMT    = "IhBB"

joy_axis     = [0] * 8
joy_dir      = "stop"
joy_speed    = 70

# ── Factory defaults ───────────────────────────────────────
FACTORY_ROBOT_NAME = "Nova"
FACTORY_WIFI_SSID  = "NovaRobot"
FACTORY_WIFI_PASS  = "nova1234"
FACTORY_IP         = "192.168.4.1"

# ── Shutdown button ────────────────────────────────────────
SHUTDOWN_BTN = 25

# ── Eye helper ─────────────────────────────────────────────
def set_eye(state):
    try:
        from eyes import set_state
        set_state(state)
    except: pass

# ── Transcribe ─────────────────────────────────────────────
def transcribe(wav_path: str) -> str:
    if not wav_path or not os.path.exists(wav_path):
        return ""
    try:
        segments, _ = model.transcribe(wav_path, language="en")
        os.remove(wav_path)
        text = " ".join([s.text for s in segments]).lower().strip()
        text = text.replace(".", "").replace(",", "").replace("!", "").replace("?", "")
        return text.strip()
    except Exception as e:
        print(f"[STT] Error: {e}")
        return ""

# ── Q&A mode ───────────────────────────────────────────────
def qa_mode():
    from settings import load_settings
    from ai_fallback import ask_gpt
    print("[MODE] Q&A mode activated")
    set_eye("wake")
    speak("How can I help?")
    set_eye("listening")
    wav_q = record_audio(duration=5)
    question = transcribe(wav_q)
    print(f"[QUESTION] {question!r}")

    # -- Show the question on the display ------------------
    if question:
        try:
            from display_addon import notify_display
            notify_display("question", question)
        except: pass

    if not question:
        set_eye("speaking")
        speak("I did not catch that.")
        set_eye("idle")
        return
    try:
        from display_addon import try_reminder_command
        resp = try_reminder_command(question)
        if resp:
            set_eye("speaking")
            speak(resp)
            set_eye("idle")
            return
    except Exception as e:
        print(f"[REMIND] check failed: {e}")

    # -- Voice-triggered video? ("play a video about lions") --
    try:
        from display_addon import try_video_command
        vq = try_video_command(question)
        if vq:
            set_eye("speaking")
            speak(f"Playing a video about {vq} on my screen")
            set_eye("idle")
            return
    except: pass

    answer = find_answer(question)
    if answer:
        print("[QA] Found in local store")
        set_eye("speaking")
        speak(answer)
    else:
        print("[QA] Not found locally - asking GPT...")
        set_eye("thinking")
        speak("Let me think about that.")
        s = load_settings()
        lang = s.get("language", "en")
        answer = ask_gpt(question, language=lang)
        set_eye("speaking")
        speak(answer)
    set_eye("idle")

# ── Face mode ──────────────────────────────────────────────
def face_mode():
    print("[MODE] Face detection mode activated")
    set_eye("face")
    speak("Please look at the camera")
    try:
        name, greeting = scan_face_from_camera(timeout=7)
        if name:
            # -- Show the greeting card on the display --
            try:
                from display_addon import notify_face
                notify_face(name, f"Hello {name}, {greeting}")
            except Exception as e:
                print(f"[FACE] card failed: {e}")
            set_eye("speaking")
            speak(f"Hello {name}, {greeting}")
        else:
            set_eye("speaking")
            speak("Sorry, I do not recognize you")
    except Exception as e:
        print(f"[FACE] Error: {e}")
    set_eye("idle")

# ── Joystick ───────────────────────────────────────────────
def get_direction():
    y  = joy_axis[1]
    a2 = joy_axis[2]
    a3 = joy_axis[3]
    if abs(y) > DEADZONE:
        return "forward" if y < -DEADZONE else "backward"
    if a2 < -DEADZONE or a3 > DEADZONE:
        return "left"
    if a2 > DEADZONE or a3 < -DEADZONE:
        return "right"
    return "stop"

def joystick_loop():
    global joy_dir, joy_speed
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
                if etype == 2 and number < len(joy_axis):
                    joy_axis[number] = value
                    new_dir = get_direction()
                    if new_dir != joy_dir:
                        joy_dir = new_dir
                        send_to_esp32(f"MOVE:{joy_dir}")
                        print(f"[JOY] {joy_dir}")
                elif etype == 1 and value == 1:
                    if number == 0:
                        joy_dir = "stop"
                        send_to_esp32("MOVE:stop")
                        print("[JOY] stop")
                    elif number == 7:
                        joy_speed = min(100, joy_speed + 10)
                        send_to_esp32(f"SPEED:{joy_speed}")
                        print(f"[JOY] Speed: {joy_speed}%")
                    elif number == 6:
                        joy_speed = max(10, joy_speed - 10)
                        send_to_esp32(f"SPEED:{joy_speed}")
                        print(f"[JOY] Speed: {joy_speed}%")
            js.close()
            print("[JOY] Joystick disconnected — retrying in 3s...")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[JOY] Error: {e}")
        time.sleep(3)

# ── Shutdown ───────────────────────────────────────────────
def do_shutdown():
    print("[SHUTDOWN] Starting safe shutdown")
    try:
        set_eye("obstacle")
    except: pass
    try:
        speak("Shutting down. Goodbye!")
    except: pass
    send_to_esp32("MOVE:stop")
    time.sleep(1)
    send_to_esp32("HOME")
    time.sleep(2)
    try:
        from eyes import stop_eyes
        stop_eyes()
    except: pass
    send_to_esp32("LATCH:OFF")
    print("[SHUTDOWN] LATCH:OFF sent — ESP32 cuts power in 15s")
    time.sleep(1)
    subprocess.run(['sudo', 'shutdown', '-h', 'now'])

# ── Factory Reset ──────────────────────────────────────────
def do_factory_reset():
    print("[FACTORY] Resetting to factory defaults...")
    set_eye("thinking")
    try:
        speak("Resetting to factory settings. Please wait.")
    except: pass

    # ── Reset settings.json ────────────────────────────────
    from settings import save_settings
    save_settings({
        "robot_name":      FACTORY_ROBOT_NAME,
        "welcome_speech":  f"Hello, I am {FACTORY_ROBOT_NAME}, your robot assistant",
        "language":        "en",
        "voice":           "female",
        "chatgpt_enabled": True
    })
    print("[FACTORY] settings.json reset")

    # ── Clear qa_store.json ────────────────────────────────
    save_qa({})
    print("[FACTORY] qa_store.json cleared")

    # -- Reset WiFi hotspot delete all old ones first -----
    try:
        # Get all saved wifi connections and delete them all
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

        # Create fresh NovaRobot hotspot with fixed IP
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
        print(f"[FACTORY] WiFi ? '{FACTORY_WIFI_SSID}' / '{FACTORY_WIFI_PASS}' @ {FACTORY_IP}")
    except Exception as e:
        print(f"[FACTORY] WiFi reset error: {e}")

    set_eye("idle")
    try:
        speak(f"Factory reset complete. Robot name is {FACTORY_ROBOT_NAME}. WiFi name is {FACTORY_WIFI_SSID}. Password is {FACTORY_WIFI_PASS}.")
    except: pass

    print("[FACTORY] Done — restarting service")
    time.sleep(3)
    subprocess.run(['sudo', 'systemctl', 'restart', 'piassistant'])

# ── Button Monitor ─────────────────────────────────────────
def button_monitor():
    try:
        import gpiod
        print("[BTN] Button monitor started on GPIO 25")

        with gpiod.request_lines(
            '/dev/gpiochip0',
            consumer='shutdown_btn',
            config={SHUTDOWN_BTN: gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                bias=gpiod.line.Bias.PULL_UP
            )}
        ) as request:

            while True:
                val = request.get_value(SHUTDOWN_BTN)

                if val == gpiod.line.Value.INACTIVE:  # Button pressed = LOW
                    press_start = time.time()
                    print("[BTN] Button pressed...")

                    while request.get_value(SHUTDOWN_BTN) == gpiod.line.Value.INACTIVE:
                        time.sleep(0.05)
                        held = time.time() - press_start

                        # ── LONG PRESS (3s+) → SHUTDOWN ───────────
                        if held >= 3.0:
                            print("[BTN] Long press — shutting down!")
                            try:
                                speak("Hold on, shutting down")
                            except: pass
                            do_shutdown()
                            return

                    # Released before 3s
                    held = time.time() - press_start
                    print(f"[BTN] Released after {held:.1f}s")

                    # ── SHORT PRESS → FACTORY RESET ───────────────
                    if held >= 0.1:
                        print("[BTN] Short press — factory reset!")
                        threading.Thread(target=do_factory_reset, daemon=True).start()

                time.sleep(0.1)

    except Exception as e:
        print(f"[BTN] Error: {e}")

# ── Voice listening loop ───────────────────────────────────
def listening_loop():
    from settings import load_settings
    s          = load_settings()
    welcome    = s.get("welcome_speech", "System ready")
    robot_name = s.get("robot_name", "Pi Assistant")
    wake_word  = robot_name.lower().strip()
    print(f"[MAIN] {robot_name} ready. Wake word: {wake_word!r}")
    set_eye("speaking")
    speak(welcome if welcome else "System ready")
    set_eye("idle")
    while True:
        s          = load_settings()
        robot_name = s.get("robot_name", "Pi Assistant")
        wake_word  = robot_name.lower().strip()
        wav  = record_audio(duration=3)
        text = transcribe(wav)
        print(f"[STT] Heard: {text!r}")
        if wake_word and wake_word in text:
            print(f"[MAIN] Wake word {wake_word!r} detected!")
            qa_mode()
        elif "hi" in text.split() or text.startswith("hi"):
            print("[MAIN] Face mode trigger!")
            face_mode()
        time.sleep(0.1)

# ── Entry point ────────────────────────────────────────────
if __name__ == "__main__":
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    print("[HTTP] Server started on port 5000")
    time.sleep(1)

    btn_thread = threading.Thread(target=button_monitor, daemon=True)
    btn_thread.start()
    print("[BTN] Button monitor started")

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
        except: pass
        send_to_esp32("MOVE:stop")
        print("\n[MAIN] Stopped.")
