
import sys
sys.path.insert(0, "/home/nova/pi_assistant/venv/lib/python3.13/site-packages")
import board
import neopixel
import threading
import time
import math

NUM_LEDS   = 9
BRIGHTNESS = 0.3

left_eye  = neopixel.NeoPixel(board.D24, NUM_LEDS, brightness=BRIGHTNESS, auto_write=False)
right_eye = neopixel.NeoPixel(board.D10, NUM_LEDS, brightness=BRIGHTNESS, auto_write=False)

_state   = "idle"
_step    = 0
_running = False
_thread  = None

def _show(color_l, color_r=None):
    if color_r is None:
        color_r = color_l
    left_eye.fill(color_l)
    right_eye.fill(color_r)
    left_eye.show()
    right_eye.show()

def _eye_loop():
    global _step, _running, _state
    _step = 0
    while _running:
        s = _state
        try:
            if s == "idle":
                val = int((1 + math.sin(_step * 0.1)) * 60)
                _show((0, 0, max(5, val)))
                time.sleep(0.05)

            elif s == "eyes1":
                val = int((1 + math.sin(_step * 0.06)) * 80)
                _show((0, 0, max(5, val)))
                time.sleep(0.05)

            elif s == "eyes2":
                phase = _step % 20
                if   phase < 2:  _show((255, 20, 80))
                elif phase < 4:  _show((80,  5,  25))
                elif phase < 6:  _show((255, 20, 80))
                elif phase < 12: _show((0,   0,   0))
                else:            _show((15,  0,   6))
                time.sleep(0.08)

            elif s == "eyes3":
                rainbow = [
                    (255,0,0),(255,127,0),(255,255,0),(0,255,0),
                    (0,0,255),(75,0,130),(148,0,211),(255,0,255),
                ]
                _show(rainbow[_step % len(rainbow)])
                time.sleep(0.12)

            elif s == "wake":
                _show((255,255,255) if _step%2==0 else (0,0,0))
                time.sleep(0.1)

            elif s == "listening":
                _show((0,255,0) if _step%6<3 else (0,60,0))
                time.sleep(0.1)

            elif s == "speaking":
                _show((0,255,255) if _step%4<2 else (0,60,60))
                time.sleep(0.12)

            elif s == "obstacle":
                _show((255,0,0) if _step%4<2 else (0,0,0))
                time.sleep(0.1)

            elif s == "forward":
                _show((0,255,0))
                time.sleep(0.1)

            elif s == "backward":
                _show((255,200,0))
                time.sleep(0.1)

            elif s == "left":
                _show((0,0,255), (0,0,30))
                time.sleep(0.1)

            elif s == "right":
                _show((0,0,30), (0,0,255))
                time.sleep(0.1)

            elif s == "stop":
                _show((40,40,40))
                time.sleep(0.1)

            elif s == "person":
                _show((128,0,128) if _step%6<3 else (0,0,0))
                time.sleep(0.1)

            elif s == "thinking":
                _show((255,80,0) if _step%4<2 else (60,20,0))
                time.sleep(0.15)

            elif s == "face":
                rainbow = [
                    (255,0,0),(255,127,0),(255,255,0),(0,255,0),
                    (0,0,255),(75,0,130),(148,0,211),(255,0,255),
                ]
                _show(rainbow[_step % len(rainbow)])
                time.sleep(0.12)

            else:
                _show((0,0,0))
                time.sleep(0.1)

            _step += 1

        except Exception as e:
            print(f"[EYES] Error: {e}")
            time.sleep(0.1)

    _show((0,0,0))

def set_state(state):
    global _state, _step
    if _state == state:
        return
    print(f"[EYES] State: {state}")
    _state = state
    _step  = 0

def start_eyes():
    global _running, _thread
    _running = True
    _thread  = threading.Thread(target=_eye_loop, daemon=True)
    _thread.start()
    print("[EYES] Started")

def stop_eyes():
    global _running
    _running = False
    time.sleep(0.3)
    _show((0,0,0))
    print("[EYES] Stopped")
