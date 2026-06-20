from flask import Flask, request, jsonify, render_template
from qa_store import add_qa, load_qa, save_qa
#from face_utils import register_face, list_faces, delete_face
from werkzeug.utils import secure_filename
from settings import load_settings, save_settings
from flask_cors import CORS
import os, threading, serial, serial.tools.list_ports, json, time
import subprocess


# ── Pose Storage ───────────────────────────────────────────
POSES_FILE = "poses.json"

def load_poses():
    if os.path.exists(POSES_FILE):
        with open(POSES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_poses(poses):
    with open(POSES_FILE, 'w') as f:
        json.dump(poses, f, indent=2)

# ── Flask App ──────────────────────────────────────────────
app = Flask(__name__, template_folder='templates')
CORS(app)

UPLOAD_FOLDER = "faces"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── Face Model Init ────────────────────────────────────────
try:
    from face_utils import app as face_app
    print("[FACE] Model initialized")
except Exception as e:
    print(f"[WARNING] Face model init failed: {e}")

# ── ESP32 Serial (Base movement only) ─────────────────────
# Fixed port via udev rule: /dev/ttyAMA0 → CP2102 (10c4:ea60)
try:
    esp32 = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
    print(f"[ESP32] Connected on /dev/ttyAMA0")
except Exception as e:
    esp32 = None
    print(f"[ESP32] Not connected: {e}")

def reconnect_esp32():
    global esp32
    try:
        if esp32: esp32.close()
        esp32 = serial.Serial('/dev/ttyAMA0', 115200, timeout=1)
        print(f"[ESP32] Reconnected on /dev/ttyAMA0")
        return True
    except:
        esp32 = None
        return False

def send_to_esp32(command: str):
    global esp32
    try:
        if esp32 and esp32.is_open:
            esp32.write((command + '\n').encode())
            print(f"[ESP32] Sent: {command}")
        else:
            print(f"[ESP32] Reconnecting...")
            if reconnect_esp32():
                esp32.write((command + '\n').encode())
    except Exception as e:
        print(f"[ESP32] Error: {e}")
        reconnect_esp32()
# ── Speak Welcome ──────────────────────────────────────────
def speak_welcome():
    from audio_utils import speak
    try:
        from eyes import set_state
        set_state("person")
        time.sleep(1)
        set_state("speaking")
    except: pass
    s = load_settings()
    welcome = s.get('welcome_speech', 'Hello welcome!')
    speak(welcome)
    try:
        from eyes import set_state
        set_state("idle")
    except: pass
    # Resume movement after speaking
    send_to_esp32("RESUME")
    print("[ESP32] Resume sent after welcome")

# ── ESP32 Reader ───────────────────────────────────────────
ultrasonic_enabled = True

def esp32_reader():
    print("[ESP32] Starting reader thread")
    last_data = time.time()
    while True:
        try:
            if esp32 and esp32.is_open:
                if esp32.in_waiting:
                    line = esp32.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    last_data = time.time()
                    print(f"[ESP32] << {line}")
                    if line.startswith("PERSON_DETECTED:"):
                        if ultrasonic_enabled:
                            dist = line.split(":")[1]
                            print(f"[ESP32] Person at {dist}cm speaking welcome")
                            threading.Thread(target=speak_welcome, daemon=True).start()
                        else:
                            print(f"[ESP32] Person detected  ultrasonic disabled, skipping")
                    elif line.startswith("OBSTACLE:"):
                        if ultrasonic_enabled:
                            print(f"[ESP32] Obstacle detected")
                            try:
                                from eyes import set_state
                                set_state("obstacle")
                            except: pass
                            threading.Thread(target=speak_welcome, daemon=True).start()
                        else:
                            print(f"[ESP32] Obstacle detected  ultrasonic disabled, skipping")
                    elif line.startswith("CLEAR:"):
                        print(f"[ESP32] Clear")
                        try:
                            from eyes import set_state
                            set_state("forward")
                        except: pass

                    # -- BUTTON SHUTDOWN FROM ESP32 ----------------
                    elif line.startswith("SHUTDOWN"):
                        print("[ESP32] Button shutdown received  shutting down!")
                        from main import do_shutdown

                        threading.Thread(target=do_shutdown, daemon=True).start()

                    elif any(x in line for x in ["ready","READY","Nova","BLOCKED","LATCH","HW:"]):
                        print(f"[ESP32] {line}")

                if time.time() - last_data > 120:
                    print("[ESP32] No data 120s  reconnecting...")
                    reconnect_esp32()
                    last_data = time.time()
        except Exception as e:
            print(f"[ESP32] Reader error: {e}")
            reconnect_esp32()
            last_data = time.time()
        time.sleep(0.05)

esp32_reader_thread = threading.Thread(target=esp32_reader, daemon=True)
esp32_reader_thread.start()
# ── Mega Serial (Arm/Head servos) ─────────────────────────
# Fixed port via udev rule: /dev/mega → CH340 (1a86:7523)
try:
    mega = serial.Serial('/dev/mega', 9600, timeout=1)
    print(f"[MEGA] Connected on /dev/mega")
except Exception as e:
    mega = None
    print(f"[MEGA] Not connected: {e}")

def reconnect_mega():
    global mega
    try:
        if mega: mega.close()
        mega = serial.Serial('/dev/mega', 9600, timeout=1)
        print(f"[MEGA] Reconnected on /dev/mega")
        return True
    except:
        mega = None
        return False

def send_to_mega(command: str):
    global mega
    try:
        if mega and mega.is_open:
            mega.write((command + '\n').encode())
            print(f"[MEGA] Sent: {command}")
        else:
            print(f"[MEGA] Reconnecting...")
            if reconnect_mega():
                mega.write((command + '\n').encode())
    except Exception as e:
        print(f"[MEGA] Error: {e}")
        reconnect_mega()

# ── Loop Control ───────────────────────────────────────────
loop_running = False
loop_thread  = None

def run_loop():
    global loop_running
    poses = load_poses()
    if not poses:
        print("[LOOP] No poses saved")
        return
    print(f"[LOOP] Starting with {len(poses)} poses")
    while loop_running:
        for pos_num in sorted(poses.keys(), key=int):
            if not loop_running:
                break
            pose   = poses[pos_num]
            hand   = pose.get('hand', 'left')
            servos = pose.get('servos', {})
            print(f"[LOOP] Playing pose {pos_num} hand={hand}")
            for part, value in servos.items():
                send_to_esp32(f"POS:{part}:{value}:{hand}")
                time.sleep(0.05)
            time.sleep(1)
    print("[LOOP] Stopped")

# ── Dashboard ──────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

# ── Health ─────────────────────────────────────────────────
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok", "message": "Pi is alive"})

@app.route('/status', methods=['GET'])
def status():
    return jsonify({"battery": 100, "wifi": True, "connected": True}), 200

# ── Q&A ───────────────────────────────────────────────────
@app.route('/qa/add', methods=['POST'])
def add():
    try:
        data = request.json
        if isinstance(data, list):
            for item in data:
                q = item.get('question','').strip()
                a = item.get('answer','').strip()
                if q and a:
                    add_qa(q, a)
        elif isinstance(data, dict):
            q = data.get('question','').strip()
            a = data.get('answer','').strip()
            if q and a:
                add_qa(q, a)
            else:
                return jsonify({"status": "error", "message": "Empty question or answer"}), 400
        return jsonify({"status": "ok", "message": "Q&A saved"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
@app.route('/hardware', methods=['GET'])
def handle_hardware():
    global ultrasonic_enabled
    state = request.args.get('state', 'on').lower()
    ultrasonic_enabled = (state == 'on')
    print(f"[ULTRASONIC] {'ENABLED' if ultrasonic_enabled else 'DISABLED'}")
    # Tell ESP32 to enable/disable TF-Luna
    send_to_esp32(f"HARDWARE:{'ON' if ultrasonic_enabled else 'OFF'}")
    return jsonify({"status": "ok", "ultrasonic": ultrasonic_enabled}), 200

@app.route('/qa/list', methods=['GET'])
def list_qa():
    try:
        return jsonify(load_qa())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/delete', methods=['POST'])
def delete():
    try:
        q = request.json.get('question','').lower().strip()
        qa = load_qa()
        if q in qa:
            del qa[q]
            save_qa(qa)
            return jsonify({"status": "ok", "message": f"Deleted: {q}"})
        return jsonify({"status": "error", "message": "Question not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/update', methods=['POST'])
def update_qa():
    try:
        data         = request.json
        old_question = data.get('old_question','').strip().lower()
        new_question = data.get('new_question','').strip().lower()
        new_answer   = data.get('new_answer','').strip()
        if not old_question or not new_question or not new_answer:
            return jsonify({"status": "error", "message": "old_question, new_question and new_answer required"}), 400
        qa = load_qa()
        if old_question in qa:
            del qa[old_question]
        qa[new_question] = new_answer
        save_qa(qa)
        print(f"[QA] Updated: '{old_question}' -> '{new_question}'")
        return jsonify({"status": "ok", "message": "Q&A updated"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/qa/test-voice', methods=['POST'])
def test_voice():
    from audio_utils import speak
    s = load_settings()
    lang = s.get('language', 'en')
    if lang == 'ta':
        threading.Thread(target=speak, args=("வணக்கம், நான் உங்கள் உதவியாளர்",)).start()
    else:
        threading.Thread(target=speak, args=("Hello, I am your Pi assistant",)).start()
    return jsonify({"status": "ok"})

# ── Face ──────────────────────────────────────────────────
@app.route('/face/add', methods=['POST'])
def face_add():
    try:
        name     = request.form.get('name', '').strip()
        greeting = request.form.get('greeting', '').strip()
        file     = request.files.get('image')
        if not name or not greeting or not file:
            return jsonify({"status": "error", "message": "name, greeting and image required"}), 400
        filename = secure_filename(f"{name}.jpg")
        path     = os.path.join(UPLOAD_FOLDER, filename)
        file.save(path)
        success = register_face(name, greeting, path)
        if success:
            return jsonify({"status": "ok", "message": f"{name} registered"})
        return jsonify({"status": "error", "message": "No face found in image"}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/face/list', methods=['GET'])
def face_list():
    try:
        return jsonify(list_faces())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/face/delete', methods=['POST'])
def face_delete():
    try:
        name = request.json.get('name', '').strip()
        if delete_face(name):
            return jsonify({"status": "ok", "message": f"Deleted {name}"})
        return jsonify({"status": "error", "message": "Not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/upload-face', methods=['POST'])
def handle_upload_face():
    try:
        face_index = request.form.get('faceIndex', '0')
        speech     = request.form.get('speech', '').strip()
        file       = request.files.get('image')
        if not file:
            return jsonify({"status": "error", "message": "No image file provided"}), 400
        if not speech:
            return jsonify({"status": "error", "message": "No greeting text provided"}), 400
        allowed = {'jpg', 'jpeg', 'png'}
        if not any(file.filename.lower().endswith(f'.{ext}') for ext in allowed):
            return jsonify({"status": "error", "message": "Only JPG, JPEG, PNG supported"}), 400
        face_name = f"Face_{face_index}"
        path = os.path.join(UPLOAD_FOLDER, secure_filename(f"{face_name}.jpg"))
        file.save(path)
        success = register_face(face_name, speech, path)
        if success:
            print(f"[FACE] Registered Face {face_index}")
            return jsonify({"status": "ok", "message": f"Face {face_index} registered"}), 200
        return jsonify({"status": "error", "message": "No face detected in image"}), 400
    except Exception as e:
        print(f"[FACE] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ── Settings ──────────────────────────────────────────────
@app.route('/settings', methods=['GET'])
def get_settings():
    return jsonify(load_settings())

@app.route('/settings', methods=['POST'])
def update_settings():
    try:
        data = request.json
        s = save_settings(data)
        return jsonify({"status": "ok", "settings": s})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/settings/apikey', methods=['POST'])
def set_apikey():
    try:
        key = request.json.get('api_key', '').strip()
        if not key:
            return jsonify({"status": "error", "message": "Empty key"}), 400
        with open("ai_config.json", "w") as f:
            json.dump({"openai_key": key}, f)
        return jsonify({"status": "ok", "message": "API key saved"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ── TechnoBot Audio ───────────────────────────────────────
@app.route('/save-audio', methods=['POST'])
def save_audio():
    try:
        data           = request.get_json()
        robot_name     = data.get('robotName', '').strip()
        welcome_speech = data.get('welcomeSpeech', '').strip()
        qa_list        = data.get('qa', [])
        new_qa = {}
        for item in qa_list:
            q = item.get('question', '').strip().lower()
            a = item.get('answer', '').strip()
            if q and a:
                new_qa[q] = a
        save_qa(new_qa)
        save_settings({"robot_name": robot_name, "welcome_speech": welcome_speech})
        print(f"[TECHNOBOT] Robot: {robot_name}")
        print(f"[TECHNOBOT] Welcome: {welcome_speech}")
        print(f"[TECHNOBOT] Replaced with {len(new_qa)} Q&As")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        print(f"[TECHNOBOT] Error: {e}")
        return jsonify({"error": str(e)}), 400

# ── Base Movement → ESP32 only ────────────────────────────
@app.route('/move', methods=['GET'])
def handle_move():
    direction = request.args.get('dir', 'stop')
    print(f"[SIGNAL] MOVE: {direction}")
    send_to_esp32(f"MOVE:{direction}")
    return "OK", 200

@app.route('/speed', methods=['GET'])
def handle_speed():
    value = request.args.get('value', '50')
    print(f"[SIGNAL] SPEED: {value}")
    send_to_esp32(f"SPEED:{value}")
    return "OK", 200

@app.route('/topspeed', methods=['GET'])
def handle_topspeed():
    value = request.args.get('value', '50')
    print(f"[SIGNAL] TOPSPEED: {value}")
    send_to_esp32(f"TOPSPEED:{value}")
    return "OK", 200

# ── Arm/Head → Mega only ──────────────────────────────────
@app.route('/position', methods=['GET'])
def handle_position():
    part  = request.args.get('part', '')
    value = request.args.get('value', '1000')
    hand  = request.args.get('hand', 'left')
    print(f"[SIGNAL] POSITION: {part} -> {value} ({hand})")
    send_to_esp32(f"POS:{part}:{value}:{hand}")
    return "OK", 200

@app.route('/hand', methods=['GET'])
def handle_hand():
    value = request.args.get('value', 'left')
    print(f"[SIGNAL] HAND: {value}")
    send_to_esp32(f"HAND:{value}")
    return "OK", 200

@app.route('/home', methods=['GET'])
def handle_home():
    print("[SIGNAL] HOME: Reset")
    send_to_esp32("HOME")
    return "OK", 200

@app.route('/save_pose', methods=['GET'])
def handle_save_pose():
    pos  = request.args.get('pos', '')
    hand = request.args.get('hand', 'left')
    parts = ['headLR','headUD','lateral','shoulder','forearm','elbow','wrist','fingers']
    pose_data = {'hand': hand, 'servos': {}}
    for part in parts:
        val = request.args.get(part)
        if val is not None:
            pose_data['servos'][part] = int(val)
    poses = load_poses()
    poses[str(pos)] = pose_data
    save_poses(poses)
    print(f"[POSE] Saved pose {pos}: {pose_data}")
    send_to_esp32(f"SAVE_POSE:{pos}")
    return "OK", 200

@app.route('/eyes1', methods=['GET'])
def handle_eyes1():
    print(f"[SIGNAL] EYES: 1 — calm blue")
    try:
        from eyes import set_state
        set_state("eyes1")
    except Exception as e:
        print(f"[EYES] Error: {e}")
    return "OK", 200

@app.route('/eyes2', methods=['GET'])
def handle_eyes2():
    print(f"[SIGNAL] EYES: 2 — heartbeat")
    try:
        from eyes import set_state
        set_state("eyes2")
    except Exception as e:
        print(f"[EYES] Error: {e}")
    return "OK", 200

@app.route('/eyes3', methods=['GET'])
def handle_eyes3():
    print(f"[SIGNAL] EYES: 3 — rainbow")
    try:
        from eyes import set_state
        set_state("eyes3")
    except Exception as e:
        print(f"[EYES] Error: {e}")
    return "OK", 200

@app.route('/mode1', methods=['GET'])
@app.route('/mode2', methods=['GET'])
@app.route('/mode3', methods=['GET'])
def handle_mode():
    mode  = request.path[-1]
    value = request.args.get('value', 'A')
    print(f"[SIGNAL] MODE {mode}: {value}")
    send_to_esp32(f"MODE:{mode}:{value}")
    return "OK", 200

@app.route('/loop_start', methods=['GET'])
def handle_loop_start():
    global loop_running, loop_thread
    print("[SIGNAL] LOOP: START")
    loop_running = True
    loop_thread  = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    send_to_esp32("LOOP:START")
    return "OK", 200

@app.route('/loop_stop', methods=['GET'])
def handle_loop_stop():
    global loop_running
    print("[SIGNAL] LOOP: STOP")
    loop_running = False
    send_to_esp32("LOOP:STOP")
    return "OK", 200

@app.route('/loop_undo', methods=['GET'])
def handle_loop_undo():
    print("[SIGNAL] LOOP: UNDO")
    poses = load_poses()
    if poses:
        last_key = str(max(poses.keys(), key=int))
        del poses[last_key]
        save_poses(poses)
        print(f"[LOOP] Deleted pose {last_key}")
    send_to_esp32("LOOP:UNDO")
    return "OK", 200

@app.route('/loop_delete', methods=['GET'])
def handle_loop_delete():
    print("[SIGNAL] LOOP: DELETE ALL")
    save_poses({})
    send_to_esp32("LOOP:DELETE")
    return "OK", 200

@app.route('/select_model', methods=['GET'])
def handle_select_model():
    name = request.args.get('name', 'None')
    print(f"[SIGNAL] MODEL: {name}")
    send_to_esp32(f"MODEL:{name}")
    return "OK", 200
@app.route('/shutdown', methods=['GET'])
def handle_shutdown():
    def do_shutdown():
        print("[SHUTDOWN] Starting safe shutdown")
        # -- STOP MOTORS FIRST -----------------
        try:
            send_to_esp32("MOVE:stop")
            time.sleep(1)
        except Exception as e:
            print(e)
        # -- HOME SERVOS -----------------------
        try:
            send_to_esp32("HOME")
            time.sleep(2)
        except Exception as e:
            print(e)
        # -- SIMPLE SPEAK ----------------------
        try:
            from audio_utils import speak
            speak("Shutting down")
        except Exception as e:
            print(e)
        time.sleep(1)
        # -- SEND LATCH OFF TO ESP32 -----------
        try:
            send_to_esp32("LATCH:OFF")
            print("[SHUTDOWN] LATCH:OFF sent  ESP32 will cut power in 15s")
            time.sleep(1)
        except Exception as e:
            print(e)
        print("[SHUTDOWN] Executing poweroff")
        # -- DIRECT SHUTDOWN -------------------
        subprocess.run(
            ["sudo", "/sbin/shutdown", "-h", "now"]
        )
    threading.Thread(target=do_shutdown).start()
    return jsonify({
        "status": "ok"
    }), 200

# ── Start ─────────────────────────────────────────────────
def start_server():
    app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    start_server()
    
