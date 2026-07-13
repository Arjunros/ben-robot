from flask import Flask, request, jsonify, render_template
from settings import load_settings, save_settings
from flask_cors import CORS
import os, threading, serial, json, time, subprocess, base64
from datetime import datetime

# ── Self-locating base dir (immune to lisa/ben/nova username differences) ──
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
POSES_FILE  = f"{BASE_DIR}/pose_store.json"
QA_JSON     = f"{BASE_DIR}/qa_pairs.json"
QA_TXT      = f"{BASE_DIR}/qa_pairs.txt"
VISION_JSON = f"{BASE_DIR}/vision_data.json"
VISION_TXT  = f"{BASE_DIR}/vision_data.txt"
FACES_DIR   = f"{BASE_DIR}/uploaded_faces"
os.makedirs(FACES_DIR, exist_ok=True)

# ── Flask App ──────────────────────────────────────────────
app = Flask(__name__, template_folder='templates')
CORS(app)
from display_addon import init_display
init_display(app)

# ── State ──────────────────────────────────────────────────
robot_state = {
    'direction': 'stop', 'speed': 50, 'motion_speed': 1000,
    'eyes': 1, 'mode': 1, 'hand': 'left',
    'volume': 100, 'welcome_speech': '', 'robot_name': 'Luna',
    'loop_running': False, 'poses': [], 'qa_pairs': [],
    'boot_time': datetime.now().isoformat(), 'command_count': 0,
}
vision_data = []
obstacle_avoidance_enabled = True
face_tracking_enabled = False
head_position = 1000
last_move_dir = "stop"

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except: pass
    return default

def save_json(path, data):
    with open(path, 'w') as f: json.dump(data, f, indent=4)

robot_state['qa_pairs'] = load_json(QA_JSON, [])
vision_data = load_json(VISION_JSON, [])
_pose_store = load_json(POSES_FILE, {})
robot_state['poses'] = [
    {'timestamp': k, 'hand': v.get('hand','left'), 'joints': v.get('servos',{})}
    for k, v in sorted(_pose_store.items())
]
print(f"Loaded {len(robot_state['qa_pairs'])} Q&As, "
      f"{len(vision_data)} faces, {len(robot_state['poses'])} poses")

# ── Logger ─────────────────────────────────────────────────
def log_command(category, endpoint, params=None):
    robot_state['command_count'] += 1
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] #{robot_state['command_count']} [{category}] {endpoint}"
    if params:
        shown = {k: (str(v)[:60]+'...' if len(str(v))>60 else v) for k,v in params.items()}
        line += f" {shown}"
    print(line)

# ── ESP32 Serial ───────────────────────────────────────────
ESP32_PORT = '/dev/ttyAMA0'
try:
    esp32 = serial.Serial(ESP32_PORT, 115200, timeout=1)
    print(f"[ESP32] Connected on {ESP32_PORT}")
except Exception as e:
    esp32 = None
    print(f"[ESP32] Not connected: {e}")

def reconnect_esp32():
    global esp32
    try:
        if esp32: esp32.close()
        esp32 = serial.Serial(ESP32_PORT, 115200, timeout=1)
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
            if reconnect_esp32():
                esp32.write((command + '\n').encode())
    except Exception as e:
        print(f"[ESP32] Error: {e}")
        reconnect_esp32()

# ── Eyes helper ────────────────────────────────────────────
def set_eye(state):
    try:
        from eyes import set_state
        set_state(state)
    except: pass

# ── TTS / welcome ──────────────────────────────────────────
def speak_welcome():
    from audio_utils import speak
    set_eye("person"); time.sleep(1); set_eye("speaking")
    s = load_settings()
    speak(s.get('welcome_speech', 'Hello welcome!'))
    set_eye("idle")
    send_to_esp32("RESUME")

# ── Volume ─────────────────────────────────────────────────
def hw_set_volume(value):
    try:
        result = subprocess.run(['aplay','-l'], capture_output=True, text=True)
        card = None
        for line in result.stdout.splitlines():
            if 'usb' in line.lower() and line.startswith('card'):
                card = line.split(':')[0].replace('card','').strip()
                break
        if card is None: return
        for ctl in ['Speaker','PCM','Master','Headphone']:
            r = subprocess.run(['amixer','-c',card,'sset',ctl,f'{value}%'],
                               capture_output=True)
            if r.returncode == 0:
                print(f"[VOLUME] {ctl}={value}% (card {card})")
                return
    except Exception as e:
        print(f"[VOLUME] Error: {e}")

# ── WiFi hotspot update (fixed 192.168.4.1) ────────────────
def hw_apply_wifi(ssid, password):
    def _apply():
        time.sleep(2)
        try:
            result = subprocess.run(
                ['sudo','nmcli','-t','-f','NAME,TYPE','connection','show'],
                capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if ':wifi' in line:
                    subprocess.run(['sudo','nmcli','connection','delete',
                                    line.split(':')[0]], capture_output=True)
            subprocess.run([
                'sudo','nmcli','connection','add','type','wifi','ifname','wlan0',
                'con-name', ssid, 'autoconnect','yes','ssid', ssid,
                '802-11-wireless.mode','ap','802-11-wireless.band','bg',
                'ipv4.method','shared','ipv4.addresses','192.168.4.1/24',
                'wifi-sec.key-mgmt','wpa-psk','wifi-sec.psk', password
            ], capture_output=True)
            subprocess.run(['sudo','nmcli','connection','modify', ssid,
                            'connection.autoconnect-priority','100'], capture_output=True)
            subprocess.run(['sudo','nmcli','connection','up', ssid], capture_output=True)
            print(f"[WIFI] Hotspot '{ssid}' @ 192.168.4.1")
        except Exception as e:
            print(f"[WIFI] Error: {e}")
    threading.Thread(target=_apply, daemon=True).start()

# ── Safe shutdown (SIGSEGV-proof) ──────────────────────────
def do_shutdown():
    print("[SHUTDOWN] Starting safe shutdown")
    set_eye("obstacle")
    try:
        send_to_esp32("MOVE:stop"); time.sleep(1)
        send_to_esp32("HOME"); time.sleep(2)
    except: pass
    try:
        wav = f"{BASE_DIR}/shutdown.wav"
        if os.path.exists(wav):
            from audio_utils import get_speaker_device
            subprocess.run(['aplay','-D',get_speaker_device(),wav],
                           timeout=8, capture_output=True)
    except: pass
    send_to_esp32("LATCH:OFF")
    print("[SHUTDOWN] LATCH:OFF sent power cut in 15s")
    time.sleep(1)
    subprocess.run(["shutdown", "-h", "now"])

# ── ESP32 Reader ───────────────────────────────────────────
def esp32_reader():
    print("[ESP32] Reader thread started")
    last_data = time.time()
    while True:
        try:
            if esp32 and esp32.is_open:
                if esp32.in_waiting:
                    line = esp32.readline().decode('utf-8', errors='ignore').strip()
                    if not line: continue
                    last_data = time.time()
                    print(f"[ESP32] << {line}")
                    if line.startswith("PERSON_DETECTED:") and obstacle_avoidance_enabled:
                        threading.Thread(target=speak_welcome, daemon=True).start()
                    elif line.startswith("OBSTACLE:") and obstacle_avoidance_enabled:
                        set_eye("obstacle")
                        threading.Thread(target=speak_welcome, daemon=True).start()
                    elif line.startswith("CLEAR:"):
                        set_eye("forward")
                    elif line.startswith("SHUTDOWN"):
                        print("[ESP32] Button shutdown received!")
                        threading.Thread(target=do_shutdown, daemon=True).start()
                if time.time() - last_data > 120:
                    reconnect_esp32(); last_data = time.time()
        except Exception as e:
            print(f"[ESP32] Reader error: {e}")
            reconnect_esp32(); last_data = time.time()
        time.sleep(0.05)

threading.Thread(target=esp32_reader, daemon=True).start()

# ── Move keepalive (ESP32 watchdog needs refresh every <500ms) ──
def move_keepalive():
    while True:
        time.sleep(0.3)
        if last_move_dir and last_move_dir != "stop":
            send_to_esp32(f"MOVE:{last_move_dir}")
threading.Thread(target=move_keepalive, daemon=True).start()

# ── Loop playback ──────────────────────────────────────────
def run_loop():
    if not robot_state['poses']:
        robot_state['loop_running'] = False
        print("[LOOP] No poses")
        return
    print(f"[LOOP] Playing {len(robot_state['poses'])} poses")
    while robot_state['loop_running']:
        for pose in list(robot_state['poses']):
            if not robot_state['loop_running']: break
            for part, val in pose['joints'].items():
                try: send_to_esp32(f"POS:{part}:{int(val)}:{pose['hand']}")
                except: pass
                time.sleep(0.05)
            time.sleep(robot_state['motion_speed'] / 1000.0)
    print("[LOOP] Stopped")

# ── Face tracking (headLR follows face) ────────────────────
def face_tracking_loop():
    global head_position
    import cv2
    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    cap = None
    print("[TRACK] Face tracking started")
    while face_tracking_enabled:
        try:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(0)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                if not cap.isOpened():
                    time.sleep(3); continue
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1); continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(60,60))
            if len(faces) > 0:
                x, y, w, h = max(faces, key=lambda f: f[2]*f[3])
                offset = ((x + w/2) - frame.shape[1]/2) / (frame.shape[1]/2)
                if abs(offset) > 0.15:
                    step = max(-150, min(150, int(offset * 120)))
                    head_position = max(0, min(2000, head_position - step))
                    send_to_esp32(f"POS:headLR:{head_position}:left")
            time.sleep(0.15)
        except Exception as e:
            print(f"[TRACK] Error: {e}")
            time.sleep(1)
    if cap is not None: cap.release()
    print("[TRACK] Face tracking stopped")

# ═══════════════════════════════════════════════════════════
# ROUTES — new app contract + legacy compatibility
# ═══════════════════════════════════════════════════════════
@app.route('/')
def index():
    try: return render_template('index.html')
    except: return jsonify({"status":"ok","robot":"Luna"}), 200

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status":"ok","message":"Pi is alive"})

@app.route('/status', methods=['GET'])
def handle_status():
    uptime = (datetime.now() - datetime.fromisoformat(robot_state['boot_time'])).total_seconds()
    return jsonify({
        "status":"ok","battery":85,"connected":True,"wifi":True,
        "uptime":int(uptime),"command_count":robot_state['command_count'],
        "current_state":{
            "direction":robot_state['direction'],"speed":robot_state['speed'],
            "eyes":robot_state['eyes'],"mode":robot_state['mode'],
            "hand":robot_state['hand'],"loop_running":robot_state['loop_running'],
            "pose_count":len(robot_state['poses'])}}), 200

# ── Movement ───────────────────────────────────────────────
@app.route('/move', methods=['GET'])
def handle_move():
    global last_move_dir
    direction = request.args.get('dir','stop')
    robot_state['direction'] = direction
    last_move_dir = direction
    esp_dir = 'backward' if direction == 'back' else direction
    log_command('MOVE','/move',{'dir':direction})
    send_to_esp32(f"MOVE:{esp_dir}")
    set_eye(esp_dir if esp_dir != 'stop' else 'idle')
    return jsonify({"status":"ok","command":"move","dir":direction}), 200

@app.route('/speed', methods=['GET'])
def handle_speed():
    value = request.args.get('value','50')
    int_val = int(value) if value.isdigit() else 50
    if int_val > 100:
        robot_state['motion_speed'] = int_val
        send_to_esp32(f"TOPSPEED:{max(0,min(100,int(int_val*100/2000)))}")
        log_command('SPEED','/speed (motion)',{'value':int_val})
    else:
        robot_state['speed'] = int_val
        send_to_esp32(f"SPEED:{int_val}")
        log_command('SPEED','/speed (drive)',{'value':int_val})
    return jsonify({"status":"ok","command":"speed","value":int_val}), 200

@app.route('/topspeed', methods=['GET'])
def handle_topspeed():
    value = request.args.get('value','50')
    send_to_esp32(f"TOPSPEED:{value}")
    return jsonify({"status":"ok"}), 200

@app.route('/estop', methods=['GET'])
def handle_estop():
    global last_move_dir
    log_command('SYSTEM','/estop',{'action':'EMERGENCY STOP'})
    robot_state['loop_running'] = False
    last_move_dir = "stop"
    for _ in range(3):
        send_to_esp32("MOVE:stop"); time.sleep(0.05)
    return jsonify({"status":"ok","command":"estop"}), 200

@app.route('/obstacle_avoidance', methods=['GET'])
@app.route('/lador', methods=['GET'])
def handle_obstacle_avoidance():
    global obstacle_avoidance_enabled
    value = request.args.get('value','down')
    obstacle_avoidance_enabled = (value == 'up')
    send_to_esp32(f"HARDWARE:{'ON' if obstacle_avoidance_enabled else 'OFF'}")
    log_command('SYSTEM', request.path, {'value':value})
    return jsonify({"status":"ok","command":"obstacle_avoidance","value":value}), 200

# ── Joints / hands ─────────────────────────────────────────
@app.route('/hand', methods=['GET'])
def handle_hand():
    value = request.args.get('value','left')
    robot_state['hand'] = value
    send_to_esp32(f"HAND:{value}")
    return jsonify({"status":"ok","command":"hand","value":value}), 200

@app.route('/position', methods=['GET'])
def handle_position():
    part  = request.args.get('part','elbow')
    value = request.args.get('value','1000')
    hand  = request.args.get('hand','left')
    # New app can send fingers as JSON in value
    if value.strip().startswith('{'):
        try:
            fingers = json.loads(value)
            for fname, fval in fingers.items():
                send_to_esp32(f"POS:{fname.lower()}:{int(fval)}:{hand}")
                time.sleep(0.02)
            log_command('JOINT','/position (fingers)', fingers)
            return jsonify({"status":"ok"}), 200
        except Exception as e:
            return jsonify({"status":"error","message":str(e)}), 400
    int_val = int(value) if value.isdigit() else 1000
    log_command('JOINT','/position',{'part':part,'value':int_val,'hand':hand})
    send_to_esp32(f"POS:{part}:{int_val}:{hand}")
    return jsonify({"status":"ok","command":"position","part":part,
                    "value":int_val,"hand":hand}), 200

@app.route('/fingers', methods=['POST'])
def handle_fingers():
    data = request.get_json(force=True, silent=True) or {}
    hand = data.get('hand','left')
    for finger_name, val in data.items():
        if finger_name == 'hand': continue
        send_to_esp32(f"POS:finger_{finger_name.lower()}:{val}:{hand}")
    log_command('JOINT','POST /fingers', data)
    return jsonify({"status":"ok","command":"fingers","data":data}), 200

@app.route('/home', methods=['GET'])
def handle_home():
    send_to_esp32("HOME")
    log_command('JOINT','/home')
    return jsonify({"status":"ok","command":"home"}), 200

# ── Poses / loop ───────────────────────────────────────────
@app.route('/save_pose', methods=['GET'])
def handle_save_pose():
    params = request.args.to_dict()
    timestamp = params.get('pos', str(int(time.time())))
    hand = params.get('hand','left')
    joints = {}
    for k, v in params.items():
        if k in ('pos','hand','robot','tier'): continue
        try: joints[k] = int(v)
        except ValueError: joints[k] = v
    robot_state['poses'].append({'timestamp':timestamp,'hand':hand,'joints':joints})
    store = load_json(POSES_FILE, {})
    store[timestamp] = {'hand':hand,'servos':joints}
    save_json(POSES_FILE, store)
    log_command('LOOP','/save_pose',{'hand':hand, **joints})
    return jsonify({"status":"ok","command":"save_pose",
                    "pose_count":len(robot_state['poses'])}), 200

@app.route('/loop_start', methods=['GET'])
def handle_loop_start():
    robot_state['loop_running'] = True
    threading.Thread(target=run_loop, daemon=True).start()
    return jsonify({"status":"ok","command":"loop_start"}), 200

@app.route('/loop_stop', methods=['GET'])
def handle_loop_stop():
    robot_state['loop_running'] = False
    return jsonify({"status":"ok","command":"loop_stop"}), 200

@app.route('/loop_undo', methods=['GET'])
def handle_loop_undo():
    removed = robot_state['poses'].pop() if robot_state['poses'] else None
    if removed:
        store = load_json(POSES_FILE, {})
        ts = str(removed.get('timestamp'))
        if ts in store: del store[ts]
        elif store: del store[list(store.keys())[-1]]
        save_json(POSES_FILE, store)
    return jsonify({"status":"ok","remaining_poses":len(robot_state['poses'])}), 200

@app.route('/loop_delete', methods=['GET'])
def handle_loop_delete():
    robot_state['poses'] = []
    save_json(POSES_FILE, {})
    return jsonify({"status":"ok","command":"loop_delete"}), 200

@app.route('/run', methods=['GET'])
def handle_run():
    value = request.args.get('value','false')
    if value == 'true':
        robot_state['loop_running'] = True
        threading.Thread(target=run_loop, daemon=True).start()
    else:
        robot_state['loop_running'] = False
    return jsonify({"status":"ok","command":"run","value":value}), 200

@app.route('/loop', methods=['GET'])
def handle_loop():
    return jsonify({"status":"ok","command":"loop"}), 200

# ── Eyes / modes (legacy + current) ────────────────────────
@app.route('/eyes1', methods=['GET'])
@app.route('/eyes2', methods=['GET'])
@app.route('/eyes3', methods=['GET'])
def handle_eyes():
    eye = int(request.path[-1])
    robot_state['eyes'] = eye
    set_eye(f"eyes{eye}")
    return jsonify({"status":"ok","command":"eyes","value":eye}), 200

@app.route('/mode1', methods=['GET'])
@app.route('/mode2', methods=['GET'])
@app.route('/mode3', methods=['GET'])
def handle_mode_n():
    mode = int(request.path[-1])
    robot_state['mode'] = mode
    send_to_esp32(f"MODE:{mode}")
    return jsonify({"status":"ok","command":"mode","value":mode}), 200

@app.route('/mode', methods=['GET'])
def handle_variant_mode():
    value = request.args.get('value','Mode 1')
    import re
    digits = re.findall(r'\d+', value)
    mode = int(digits[0]) if digits else 1
    robot_state['mode'] = mode
    send_to_esp32(f"MODE:{mode}")
    return jsonify({"status":"ok","command":"mode","mode_number":mode}), 200

@app.route('/select_model', methods=['GET'])
def handle_select_model():
    name = request.args.get('name','None')
    send_to_esp32(f"MODEL:{name}")
    return jsonify({"status":"ok"}), 200

# ── Settings (volume / wifi / legacy JSON) ─────────────────
@app.route('/settings', methods=['GET','POST'])
def handle_settings():
    if request.method == 'POST':
        try:
            s = save_settings(request.get_json() or {})
            return jsonify({"status":"ok","settings":s}), 200
        except Exception as e:
            return jsonify({"status":"error","message":str(e)}), 500
    params = request.args.to_dict()
    if 'volume' in params:
        vol = int(params['volume']) if params['volume'].isdigit() else 100
        robot_state['volume'] = vol
        hw_set_volume(vol)
        log_command('VOICE','/settings (volume)',{'volume':vol})
        return jsonify({"status":"ok","volume":vol}), 200
    if 'wifi_ssid' in params:
        ssid = params.get('wifi_ssid','')
        pw   = params.get('wifi_password','')
        log_command('SYSTEM','/settings (wifi)',{'ssid':ssid})
        if ssid and pw and len(pw) >= 8:
            hw_apply_wifi(ssid, pw)
            return jsonify({"status":"ok","message":f"WiFi updating to {ssid}"}), 200
        return jsonify({"status":"error","message":"Password must be 8+ chars"}), 400
    return jsonify(load_settings()), 200

@app.route('/settings/apikey', methods=['POST'])
def set_apikey():
    try:
        key = request.json.get('api_key','').strip()
        if not key:
            return jsonify({"status":"error","message":"Empty key"}), 400
        with open(f"{BASE_DIR}/ai_config.json","w") as f:
            json.dump({"openai_key": key}, f)
        return jsonify({"status":"ok"}), 200
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route('/online_chat', methods=['GET'])
def handle_online_chat():
    enabled = request.args.get('enabled','false')
    save_settings({'chatgpt_enabled': enabled == 'true'})
    log_command('SYSTEM','/online_chat',{'enabled':enabled})
    return jsonify({"status":"ok","enabled":enabled}), 200

# ── Voice / Q&A ────────────────────────────────────────────
@app.route('/save-audio', methods=['GET','POST'])
def handle_save_audio():
    if request.method == 'POST':
        data = request.get_json() or {}
    else:
        data = request.args
    speech = data.get('welcomeSpeech','')
    robot_name = data.get('robotName','Luna')
    robot_state['welcome_speech'] = speech
    robot_state['robot_name'] = robot_name
    updates = {}
    if robot_name: updates['robot_name'] = robot_name
    if speech: updates['welcome_speech'] = speech
    if updates: save_settings(updates)
    log_command('VOICE','/save-audio',{'robotName':robot_name,'welcomeSpeech':speech})
    if speech:
        def _speak():
            from audio_utils import speak
            speak(speech)
        threading.Thread(target=_speak, daemon=True).start()
    return jsonify({"status":"ok","command":"save-audio"}), 200

def _write_qa_files():
    save_json(QA_JSON, robot_state['qa_pairs'])
    try:
        with open(QA_TXT,'w') as f:
            for idx, item in enumerate(robot_state['qa_pairs']):
                f.write(f"{idx+1}q: {item.get('q','')}\n{idx+1}a: {item.get('a','')}\n")
    except Exception as e:
        print(f"qa txt error: {e}")

@app.route('/qa/add', methods=['POST'])
def handle_qa_add():
    data = request.get_json(force=True, silent=True)
    # New app format: full list replace [{id,q,a}]
    if isinstance(data, list):
        robot_state['qa_pairs'] = data
        _write_qa_files()
        log_command('VOICE','POST /qa/add',{'qa_count':len(data)})
        return jsonify({"status":"ok","saved":len(data)}), 200
    # Legacy format: single {question, answer}
    if isinstance(data, dict):
        q = data.get('question','').strip()
        a = data.get('answer','').strip()
        if q and a:
            new_id = max([int(i.get('id',0)) for i in robot_state['qa_pairs']] + [0]) + 1
            robot_state['qa_pairs'].append({'id':new_id,'q':q,'a':a})
            _write_qa_files()
            return jsonify({"status":"ok"}), 200
    return jsonify({"status":"error","message":"Invalid payload"}), 400

@app.route('/qa/list', methods=['GET'])
def handle_qa_list():
    return jsonify(robot_state['qa_pairs']), 200

@app.route('/delete_qa', methods=['GET'])
def handle_delete_qa():
    qa_id = request.args.get('id')
    if qa_id:
        robot_state['qa_pairs'] = [i for i in robot_state['qa_pairs']
                                   if str(i.get('id')) != str(qa_id)]
        _write_qa_files()
        log_command('VOICE','/delete_qa',{'id':qa_id})
    return jsonify({"status":"ok","id":qa_id}), 200

@app.route('/qa/test-voice', methods=['POST'])
def test_voice():
    from audio_utils import speak
    threading.Thread(target=speak,
                     args=("Hello, I am Luna, your assistant",),
                     daemon=True).start()
    return jsonify({"status":"ok"}), 200

# ── Vision / Faces ─────────────────────────────────────────
def save_base64_image(base64_str, base_filename):
    try:
        ext = ".png"
        if ',' in base64_str:
            header, base64_str = base64_str.split(',', 1)
            if 'jpeg' in header or 'jpg' in header: ext = ".jpg"
            elif 'webp' in header: ext = ".webp"
        filepath = os.path.join(FACES_DIR, base_filename + ext)
        with open(filepath,'wb') as f:
            f.write(base64.b64decode(base64_str))
        return filepath
    except Exception as e:
        print(f"[VISION] base64 save error: {e}")
        return None

@app.route('/face_tracking', methods=['GET'])
def handle_face_tracking():
    global face_tracking_enabled
    value = request.args.get('value','false').lower()
    was = face_tracking_enabled
    face_tracking_enabled = (value == 'true')
    log_command('VISION','/face_tracking',{'enabled':face_tracking_enabled})
    if face_tracking_enabled and not was:
        threading.Thread(target=face_tracking_loop, daemon=True).start()
    return jsonify({"status":"ok","value":face_tracking_enabled}), 200

@app.route('/upload_face', methods=['POST'])
def handle_upload_face_single():
    data = request.get_json(force=True, silent=True) or {}
    b64 = data.get('image') or data.get('data') or data.get('face') or ''
    face_id = data.get('id', int(time.time()))
    path = save_base64_image(b64, f"face_{face_id}") if b64 else None
    log_command('VISION','POST /upload_face',{'id':face_id,'saved':bool(path)})
    return jsonify({"status":"ok","id":face_id,"saved_path":path}), 200

@app.route('/save_faces', methods=['POST'])
@app.route('/upload_faces', methods=['POST'])
def handle_save_faces():
    global vision_data
    data = request.get_json(force=True, silent=True) or []
    vision_data = data
    save_json(VISION_JSON, data)
    txt_lines = []
    if isinstance(data, list):
        for idx, item in enumerate(data):
            fid = item.get('id', idx+1)
            b64 = item.get('face') or item.get('data') or item.get('image') or ''
            speech = item.get('speech','')
            filepath = "none"
            if b64:
                saved = save_base64_image(b64, f"face_{fid}")
                if saved: filepath = saved
            txt_lines.append(f'{idx+1}. Image: {filepath} | Speech: "{speech}"\n')
    try:
        with open(VISION_TXT,'w') as f: f.writelines(txt_lines)
    except Exception as e: print(f"vision txt error: {e}")
    log_command('VISION', request.path, {'faces': len(data) if isinstance(data,list) else 1})
    return jsonify({"status":"ok","saved":len(data) if isinstance(data,list) else 1}), 200

@app.route('/delete_face', methods=['GET'])
def handle_delete_face():
    global vision_data
    face_id = request.args.get('id')
    if face_id:
        vision_data = [i for i in vision_data if str(i.get('id')) != str(face_id)]
        save_json(VISION_JSON, vision_data)
        for ext in ('.png','.jpg','.jpeg','.webp'):
            fp = os.path.join(FACES_DIR, f"face_{face_id}{ext}")
            if os.path.exists(fp):
                try: os.remove(fp)
                except: pass
        log_command('VISION','/delete_face',{'id':face_id})
    return jsonify({"status":"ok","id":face_id}), 200

# ── System ─────────────────────────────────────────────────
@app.route('/shutdown', methods=['GET'])
def handle_shutdown():
    log_command('SYSTEM','/shutdown',{'action':'SHUTDOWN'})
    threading.Thread(target=do_shutdown, daemon=True).start()
    return jsonify({"status":"ok","command":"shutdown"}), 200

@app.route('/restart', methods=['GET'])
def handle_restart():
    def _restart():
        send_to_esp32("MOVE:stop"); send_to_esp32("HOME")
        time.sleep(3)
        subprocess.run(["reboot"])
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"status": "ok", "command": "restart"}), 200

# ── Catch-all debug ────────────────────────────────────────
@app.route('/<path:path>', methods=['GET','POST'])
def catch_all(path):
    params = request.args.to_dict() if request.method=='GET' else (request.get_json(silent=True) or {})
    log_command('SYSTEM', f'/{path} (UNHANDLED)', {'method':request.method,'params':params})
    return jsonify({"status":"ok","note":f"/{path} not handled"}), 200

# ── Start ──────────────────────────────────────────────────
def start_server():
    app.run(host='0.0.0.0', port=5000, debug=False)

if __name__ == '__main__':
    start_server()
