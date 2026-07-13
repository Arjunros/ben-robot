import cv2
import numpy as np
import os
import pickle
from insightface.app import FaceAnalysis

FACES_DIR = "faces"
FACES_DB = "faces/faces_db.pkl"
os.makedirs(FACES_DIR, exist_ok=True)

# Load InsightFace model
app = None

def init_face_model():
    """Initialize InsightFace model (call once at startup)"""
    global app
    if app is None:
        print("[FACE] Loading InsightFace model...")
        app = FaceAnalysis(name="buffalo_sc", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(320, 320))
        print("[FACE] Model loaded successfully")

def load_db():
    """Load face database from pickle file"""
    if os.path.exists(FACES_DB):
        with open(FACES_DB, "rb") as f:
            return pickle.load(f)
    return {}

def save_db(db):
    """Save face database to pickle file"""
    with open(FACES_DB, "wb") as f:
        pickle.dump(db, f)

def register_face(name: str, greeting: str, image_path: str) -> bool:
    """
    Register a face with name and greeting message
    Returns True if successful, False otherwise
    """
    global app
    if app is None:
        init_face_model()
   
    # Read image
    img = cv2.imread(image_path)
    if img is None:
        print(f"[FACE] Cannot read image: {image_path}")
        return False
   
    # Detect faces
    faces = app.get(img)
    if not faces:
        print("[FACE] No face found in image")
        return False
   
    # Extract embedding from first detected face
    embedding = faces[0].embedding
   
    # Load database and add new face
    db = load_db()
    db[name] = {"embedding": embedding.tolist(), "greeting": greeting}
    save_db(db)
   
    print(f"[FACE] Registered: {name} with greeting: '{greeting}'")
    return True

def recognize_face(frame) -> tuple:
    """
    Recognize face in frame
    Returns (name, greeting) or (None, None)
    """
    global app
    if app is None:
        init_face_model()
   
    faces = app.get(frame)
    if not faces:
        return None, None
   
    db = load_db()
    if not db:
        return None, None
   
    query_embedding = faces[0].embedding
    best_match = None
    best_score = -1
    best_greeting = None
   
    for name, data in db.items():
        stored_embedding = np.array(data["embedding"])
        score = np.dot(query_embedding, stored_embedding) / (
            np.linalg.norm(query_embedding) * np.linalg.norm(stored_embedding)
        )
        if score > best_score:
            best_score = score
            best_match = name
            best_greeting = data["greeting"]
   
    if best_score > 0.4:  # confidence threshold
        print(f"[FACE] Recognized: {best_match} (score: {best_score:.2f})")
        return best_match, best_greeting
   
    return None, None

def scan_face_from_camera(timeout=10) -> tuple:
    """
    Opens USB camera, scans for face
    Returns (name, greeting) or (None, None)
    """
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[FACE] Cannot open camera")
        return None, None

    print("[FACE] Camera active, scanning...")
    import time
    start = time.time()
    name, greeting = None, None

    while time.time() - start < timeout:
        ret, frame = cap.read()
        if not ret:
            continue
        name, greeting = recognize_face(frame)
        if name:
            break
        time.sleep(0.5)

    cap.release()
    return name, greeting

def list_faces() -> list:
    """List all registered faces"""
    db = load_db()
    return [{"name": k, "greeting": v["greeting"]} for k, v in db.items()]

def delete_face(name: str) -> bool:
    """Delete a face from database"""
    db = load_db()
    if name in db:
        del db[name]
        save_db(db)
        print(f"[FACE] Deleted: {name}")
        return True
    return False

def clear_all_faces():
    """Clear all faces from database"""
    save_db({})
    print("[FACE] All faces cleared")
