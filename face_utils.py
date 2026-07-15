import cv2
import numpy as np
import os
import json
import pickle
import time
from insightface.app import FaceAnalysis

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
FACES_DIR   = f"{BASE_DIR}/uploaded_faces"      # app saves face_<id>.jpg here
VISION_JSON = f"{BASE_DIR}/vision_data.json"    # [{id, face(b64), speech}]
FACES_DB    = f"{BASE_DIR}/faces_db.pkl"        # embeddings cache

os.makedirs(FACES_DIR, exist_ok=True)

app = None
_last_sync_mtime = 0

def _biggest(faces):
    """InsightFace does not sort — pick the largest face in frame"""
    return max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))

def init_face_model():
    global app
    if app is None:
        print("[FACE] Loading InsightFace model...")
        app = FaceAnalysis(name="buffalo_sc", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(320, 320))
        print("[FACE] Model loaded successfully")

def load_db():
    if os.path.exists(FACES_DB):
        try:
            with open(FACES_DB, "rb") as f:
                return pickle.load(f)
        except: pass
    return {}

def save_db(db):
    with open(FACES_DB, "wb") as f:
        pickle.dump(db, f)

def _find_face_image(fid):
    """App may save .jpg/.png/.webp depending on the upload"""
    for ext in ('.jpg', '.jpeg', '.png', '.webp'):
        p = os.path.join(FACES_DIR, f"face_{fid}{ext}")
        if os.path.exists(p):
            return p
    return None

def sync_from_vision_data(force=False):
    """Rebuild embeddings from vision_data.json + uploaded_faces/
    Only runs when the app has uploaded something new (mtime check)."""
    global _last_sync_mtime, app
    if not os.path.exists(VISION_JSON):
        return
    mtime = os.path.getmtime(VISION_JSON)
    if not force and mtime <= _last_sync_mtime:
        return
    _last_sync_mtime = mtime

    if app is None:
        init_face_model()
    try:
        with open(VISION_JSON) as f:
            data = json.load(f)
    except Exception as e:
        print(f"[FACE] vision_data.json read error: {e}")
        return

    db = {}
    count = 0
    for item in data:
        fid    = item.get('id')
        speech = item.get('speech', '')
        if fid is None or not speech:      # skip empty delete-markers
            continue
        img_path = _find_face_image(fid)
        if not img_path:
            print(f"[FACE] No image file for id {fid} — skipped")
            continue
        img = cv2.imread(img_path)
        if img is None:
            print(f"[FACE] Cannot read {img_path}")
            continue
        faces = app.get(img)
        if not faces:
            print(f"[FACE] No face detected in {os.path.basename(img_path)} — skipped")
            continue
        best = _biggest(faces)
        print(f"[FACE] id {fid}: {len(faces)} face(s), using largest "
              f"({int(best.bbox[2]-best.bbox[0])}px)")
        db[str(fid)] = {"embedding": best.embedding.tolist(),
                        "greeting": speech}
        count += 1

    save_db(db)
    print(f"[FACE] Synced {count} faces from app storage")

def recognize_face(frame) -> tuple:
    """Returns (greeting_name, greeting) or (None, None)"""
    global app
    if app is None:
        init_face_model()
    sync_from_vision_data()

    faces = app.get(frame)
    if not faces:
        return None, None
    db = load_db()
    if not db:
        return None, None

    q = _biggest(faces).embedding
    best_id, best_score, best_greet = None, -1, None
    for fid, data in db.items():
        stored = np.array(data["embedding"])
        score = np.dot(q, stored) / (np.linalg.norm(q) * np.linalg.norm(stored))
        if score > best_score:
            best_score, best_id, best_greet = score, fid, data["greeting"]

    if best_score > 0.4:
        print(f"[FACE] Recognized id {best_id} '{best_greet}' (score {best_score:.2f})")
        return best_greet, best_greet
    print(f"[FACE] Best score {best_score:.2f} — below threshold")
    return None, None

def scan_face_from_camera(timeout=10) -> tuple:
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[FACE] Cannot open camera")
        return None, None
    print("[FACE] Camera active, scanning...")
    start = time.time()
    name, greeting = None, None
    while time.time() - start < timeout:
        ret, frame = cap.read()
        if not ret:
            continue
        name, greeting = recognize_face(frame)
        if name:
            break
        time.sleep(0.3)
    cap.release()
    return name, greeting

def list_faces() -> list:
    return [{"id": k, "greeting": v["greeting"]} for k, v in load_db().items()]

def delete_face(fid: str) -> bool:
    db = load_db()
    if str(fid) in db:
        del db[str(fid)]
        save_db(db)
        return True
    return False

def clear_all_faces():
    save_db({})
