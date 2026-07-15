"""
face_tracker.py — chest-camera face tracking for Ben.

- Detects faces continuously while enabled (Haar cascade — fast on Pi)
- The display shows the live feed with boxes; tapping a face locks it
  as the target (followed frame-to-frame by position)
- The head servo (headLR, 0-2000, center 1000) turns to keep the
  target centered. The camera is chest-mounted (does not move with
  the head), so face-x maps directly to head angle.

Wire-up in server.py's handle_face_tracking():

    import face_tracker
    face_tracker.set_enabled(face_tracking_enabled)

Tune the CONFIG block for your robot (gain sign, camera index).
Requires: sudo apt install python3-opencv
"""

import threading
import time

# ── CONFIG ─────────────────────────────────────────────────
CAM_INDEX     = 0        # cv2.VideoCapture index
FRAME_W       = 640      # processing width (keeps Pi CPU happy)
HEAD_MIN      = 0
HEAD_MAX      = 2000
HEAD_CENTER   = 1000
GAIN          = 700      # head units per full-frame error; raise = snappier
DEADBAND      = 0.06     # ignore errors smaller than this (no jitter)
INVERT        = False    # True if the head turns the wrong way
SEND_HZ       = 6        # max head commands per second
LOST_CLEAR_S  = 5.0      # forget the target after this long unseen
MIN_FACE_PX   = 40       # ignore tiny/far faces

# ── STATE ──────────────────────────────────────────────────
_enabled   = False
_thread    = None
_lock      = threading.Lock()
_boxes     = []          # [{"id","x","y","w","h","sel"}] normalized 0-1
_target    = None        # {"cx","cy"} normalized center of tracked face
_last_seen = 0.0
_jpeg      = None        # latest annotated frame (bytes)
_head_val  = HEAD_CENTER
_send_fn   = None        # injected: send_to_esp32


def _notify(state):
    """Tell the display the tracking state changed (open/close overlay)."""
    try:
        from display_addon import _broadcast
        _broadcast({"type": "face_tracking", "text": "on" if state else "off"})
    except Exception:
        pass


def set_send_fn(fn):
    """Inject the serial sender once, e.g. face_tracker.set_send_fn(send_to_esp32)."""
    global _send_fn
    _send_fn = fn


def set_enabled(on):
    global _enabled, _thread, _target
    on = bool(on)
    if on == _enabled:
        _notify(on)          # re-sync the display anyway
        return
    _enabled = on
    if on:
        _thread = threading.Thread(target=_loop, daemon=True)
        _thread.start()
        print("[TRACK] Face tracking ON")
    else:
        with _lock:
            _target = None
        print("[TRACK] Face tracking OFF")
    _notify(on)


def is_enabled():
    return _enabled


def get_state():
    with _lock:
        return {
            "enabled": _enabled,
            "tracking": _target is not None,
            "boxes": list(_boxes),
        }


def get_jpeg():
    return _jpeg


def select_at(x, y):
    """Lock the face whose box contains (or is nearest to) the tapped
    normalized point. Returns True if a face was selected."""
    global _target, _last_seen
    with _lock:
        best, best_d = None, 1e9
        for b in _boxes:
            cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
            inside = (b["x"] <= x <= b["x"] + b["w"]
                      and b["y"] <= y <= b["y"] + b["h"])
            d = 0 if inside else (cx - x) ** 2 + (cy - y) ** 2
            if d < best_d:
                best, best_d = b, d
        if best is None or best_d > 0.05:   # nothing near the tap
            return False
        _target = {"cx": best["x"] + best["w"] / 2,
                   "cy": best["y"] + best["h"] / 2}
        _last_seen = time.time()
        print(f"[TRACK] Target locked at x={_target['cx']:.2f}")
        return True


def clear_target():
    global _target
    with _lock:
        _target = None
    print("[TRACK] Target cleared")


# ── internals ──────────────────────────────────────────────
def _associate(dets):
    """Follow the target: nearest detection to its last position."""
    global _target, _last_seen
    if _target is None or not dets:
        return None
    best, best_d = None, 1e9
    for b in dets:
        cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        d = (cx - _target["cx"]) ** 2 + (cy - _target["cy"]) ** 2
        if d < best_d:
            best, best_d = b, d
    if best is None or best_d > 0.09:       # jumped too far = not our face
        return None
    _target = {"cx": best["x"] + best["w"] / 2,
               "cy": best["y"] + best["h"] / 2}
    _last_seen = time.time()
    return best


def _steer():
    """P-control: turn the head toward the target's x position."""
    global _head_val
    err = _target["cx"] - 0.5
    if abs(err) < DEADBAND:
        return
    step = err * GAIN
    if INVERT:
        step = -step
    _head_val = max(HEAD_MIN, min(HEAD_MAX, _head_val - step))
    if _send_fn:
        _send_fn(f"POS:headLR:{int(_head_val)}:left")


def _loop():
    global _boxes, _target, _jpeg, _head_val
    import cv2
    # OpenCV 5 removed Haar cascades — use the InsightFace detector
    # (already loaded for recognition, and far better on angled faces)
    import face_utils
    from face_utils import init_face_model
    init_face_model()
    cap = cv2.VideoCapture(CAM_INDEX)
    if not cap.isOpened():
        print(f"[TRACK] Camera {CAM_INDEX} failed to open")
        set_enabled(False)
        return
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    last_send = 0.0

    while _enabled:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.1)
            continue
        h, w = frame.shape[:2]
        if w > FRAME_W:
            frame = cv2.resize(frame, (FRAME_W, int(h * FRAME_W / w)))
            h, w = frame.shape[:2]

        found = []
        for _f in face_utils.app.get(frame):
            _x1, _y1, _x2, _y2 = _f.bbox.astype(int)
            _fw, _fh = _x2 - _x1, _y2 - _y1
            if _fw >= MIN_FACE_PX and _fh >= MIN_FACE_PX:
                found.append((max(0, _x1), max(0, _y1), _fw, _fh))
        dets = [{"id": i,
                 "x": x / w, "y": y / h,
                 "w": fw / w, "h": fh / h, "sel": False}
                for i, (x, y, fw, fh) in enumerate(found)]

        with _lock:
            tracked = _associate(dets)
            if tracked:
                tracked["sel"] = True
            elif _target and time.time() - _last_seen > LOST_CLEAR_S:
                _target = None
                print("[TRACK] Target lost")
            _boxes = dets
            if _target and tracked:
                now = time.time()
                if now - last_send >= 1.0 / SEND_HZ:
                    last_send = now
                    _steer()

        # annotate + encode for the display stream
        for b in dets:
            x1, y1 = int(b["x"] * w), int(b["y"] * h)
            x2, y2 = int((b["x"] + b["w"]) * w), int((b["y"] + b["h"]) * h)
            color = (255, 121, 41) if b["sel"] else (128, 214, 90)  # BGR
            cv2.rectangle(frame, (x1, y1), (x2, y2), color,
                          3 if b["sel"] else 2)
            if b["sel"]:
                cv2.putText(frame, "TRACKING", (x1, max(18, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        okj, buf = cv2.imencode(".jpg", frame,
                                [cv2.IMWRITE_JPEG_QUALITY, 70])
        if okj:
            _jpeg = buf.tobytes()

    cap.release()
    _jpeg = None
    print("[TRACK] Loop ended")
