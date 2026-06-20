import time, os, threading, struct
from faster_whisper import WhisperModel
from server import start_server, send_to_esp32
from audio_utils import record_audio, record_question, speak
from qa_store import find_answer

model = WhisperModel("base", device="cpu", compute_type="int8")

JOYSTICK_DEV = "/dev/input/js0"
DEADZONE     = 5000
EVENT_SIZE   = 8
EVENT_FMT    = "IhBB"

joy_axis     = [0] * 8
joy_buttons = [0] * 15
joy_dir      = "stop"
joy_speed    = 70

def set_eye(state):
    try:
        from eyes import set_state
        set_state(state)
    except:
        pass

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

def qa_mode():
    from settings import load_settings
    from ai_fallback import ask_gpt

    print("[MODE] Q&A mode activated")
    set_eye("wake")
    speak("How can I help?")

    set_eye("listening")
    wav_q = record_question()       # 5 seconds for question
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

        s = load_settings()
        lang = s.get("language", "en")
        answer = ask_gpt(question, language=lang)

        set_eye("speaking")
        speak(answer)

    set_eye("idle")

def get_direction():
    y = joy_axis[1]   # left stick Y  ? forward/backward
    x = joy_axis[2]   # right stick X ? left/right

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
    # -- Keepalive resend direction every 300ms ----------
    # -- Keepalive -----------------------------------------
    def keepalive():
        prev_dir = "stop"
        while True:
            time.sleep(0.3)
            if joy_dir != "stop":
                send_to_esp32(f"MOVE:{joy_dir}")
            elif prev_dir != "stop":
                # Direction just changed to stop -- send stop 3 times to clear buffer
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

                    # BASE MOVEMENT 
                    # Left stick Y  Right stick X  base
                    # Only when no modifier held
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
            print("[JOY] Joystick disconnected retrying in 3s...")

        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[JOY] Error: {e}")
        time.sleep(3)

def listening_loop():
    from settings import load_settings

    s = load_settings()
    welcome = s.get("welcome_speech", "System ready")
    robot_name = s.get("robot_name", "Pi Assistant")
    wake_word = robot_name.lower().strip()

    print(f"[MAIN] {robot_name} ready. Wake word: {wake_word!r}")

    set_eye("speaking")
    speak(welcome if welcome else "System ready")
    set_eye("idle")

    while True:
        s = load_settings()
        robot_name = s.get("robot_name", "Pi Assistant")
        wake_word = robot_name.lower().strip()

        wav = record_audio(duration=3)   # 3 seconds for wake word
        text = transcribe(wav)

        print(f"[STT] Heard: {text!r}")

        if wake_word and wake_word in text:
            print(f"[MAIN] Wake word {wake_word!r} detected!")
            qa_mode()

        time.sleep(0.1)

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
