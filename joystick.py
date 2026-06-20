#!/usr/bin/env python3
# =====================================================
#  Giya Robot — Joystick Controller
#  Left stick Y      → forward/backward
#  Right stick X     → left/right
#  MR(14) + right Y  → right lateral
#  ML(13) + left Y   → left lateral
#  Btn1   + right Y  → both lateral
#  Btn2   + left Y   → head LR
#  Btn8              → home all servos
#  Btn9              → full stop + home
#  BtnTR(7)          → speed up
#  BtnTL(6)          → speed down
# =====================================================
import struct
import serial
import time
import threading

JOYSTICK_DEV = "/dev/input/js0"
SERIAL_PORT  = "/dev/ttyAMA10"
BAUD_RATE    = 115200
DEADZONE     = 5000

EVENT_SIZE = 8
EVENT_FMT  = "IhBB"

axis    = [0] * 8
buttons = [0] * 15

currentDir   = "stop"
currentSpeed = 70

# Servo positions
latL_pos   = 1000
latR_pos   = 1000
headLR_pos = 1000
last_servo_time = 0

def send(ser, cmd):
    try:
        ser.write((cmd + '\n').encode())
        ser.flush()
        print(f"[JOY] {cmd}")
    except Exception as e:
        print(f"[JOY] Serial error: {e}")

def get_direction():
    y = axis[1]   # left stick Y  → forward/backward
    x = axis[2]   # right stick X → left/right
    if abs(y) > DEADZONE and abs(y) >= abs(x):
        return "forward" if y < -DEADZONE else "backward"
    if abs(x) > DEADZONE:
        return "left" if x < -DEADZONE else "right"
    return "stop"

def axis_to_servo(val):
    return int((val + 32767) / 65534 * 2000)

def joystick_loop(ser):
    global currentDir, currentSpeed
    global latL_pos, latR_pos, headLR_pos, last_servo_time

    try:
        js = open(JOYSTICK_DEV, "rb")
    except Exception as e:
        print(f"[JOY] Cannot open joystick: {e}")
        return

    print(f"[JOY] Joystick connected: {JOYSTICK_DEV}")
    send(ser, f"SPEED:{currentSpeed}")

    while True:
        try:
            event = js.read(EVENT_SIZE)
            if not event:
                break

            t, value, etype, number = struct.unpack(EVENT_FMT, event)
            if etype & 0x80:
                continue

            # ── AXIS EVENTS ───────────────────────────────────
            if etype == 2:
                if number < len(axis):
                    axis[number] = value

                # ── BASE MOVEMENT ─────────────────────────────
                if number in [1, 2]:
                    if not buttons[14] and not buttons[13] and not buttons[1] and not buttons[3]:
                        newDir = get_direction()
                        if newDir != currentDir:
                            currentDir = newDir
                            send(ser, f"MOVE:{currentDir}")

                # ── SERVO CONTROL ─────────────────────────────
                if time.time() - last_servo_time > 0.05:

                    # MR(14) + right stick Y → right lateral
                    if buttons[14] and number == 3 and abs(value) > DEADZONE:
                        latR_pos = max(0, min(2000, axis_to_servo(value)))
                        send(ser, f"POS:lateral:{latR_pos}:right")
                        last_servo_time = time.time()

                    # ML(13) + left stick Y → left lateral
                    elif buttons[13] and number == 1 and abs(value) > DEADZONE:
                        latL_pos = max(0, min(2000, axis_to_servo(value)))
                        send(ser, f"POS:lateral:{latL_pos}:left")
                        last_servo_time = time.time()

                    # Btn1 + right stick Y → both lateral
                    elif buttons[1] and number == 3 and abs(value) > DEADZONE:
                        pos = max(0, min(2000, axis_to_servo(value)))
                        latL_pos = latR_pos = pos
                        send(ser, f"POS:lateral:{pos}:both")
                        last_servo_time = time.time()

                    # Btn2 + left stick Y → head LR
                    elif buttons[3] and number == 1 and abs(value) > DEADZONE:
                        headLR_pos = max(0, min(2000, axis_to_servo(value)))
                        send(ser, f"POS:headLR:{headLR_pos}:left")
                        last_servo_time = time.time()

            # ── BUTTON EVENTS ─────────────────────────────────
            elif etype == 1:
                if number < len(buttons):
                    buttons[number] = value

                if value == 1:
                    if number == 0:      # BtnA → stop
                        currentDir = "stop"
                        send(ser, "MOVE:stop")

                    elif number == 7:    # BtnTR → speed up
                        currentSpeed = min(100, currentSpeed + 10)
                        send(ser, f"SPEED:{currentSpeed}")
                        print(f"[JOY] Speed: {currentSpeed}%")

                    elif number == 6:    # BtnTL → speed down
                        currentSpeed = max(10, currentSpeed - 10)
                        send(ser, f"SPEED:{currentSpeed}")
                        print(f"[JOY] Speed: {currentSpeed}%")

                    elif number == 8:    # Btn8 → home servos
                        latL_pos = latR_pos = headLR_pos = 1000
                        send(ser, "HOME")
                        print("[JOY] Servos homed")

                    elif number == 9:    # Btn9 → full stop + home
                        currentDir = "stop"
                        latL_pos = latR_pos = headLR_pos = 1000
                        send(ser, "MOVE:stop")
                        send(ser, "HOME")
                        print("[JOY] Full stop + home")

                # Modifier released → stop base
                if value == 0 and number in [13, 14, 1, 2]:
                    currentDir = "stop"
                    send(ser, "MOVE:stop")

        except Exception as e:
            print(f"[JOY] Error: {e}")
            break

    js.close()
    send(ser, "MOVE:stop")
    print("[JOY] Joystick disconnected")

if __name__ == "__main__":
    print("[JOY] Connecting to ESP32...")
    try:
        ser = signal.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(1)
        print(f"[JOY] Connected to {SERIAL_PORT}")
    except Exception as e:
        print(f"[JOY] Cannot open serial: {e}")
        exit(1)

    send(ser, "MOVE:stop")
    try:
        joystick_loop(ser)
    except KeyboardInterrupt:
        send(ser, "MOVE:stop")
        ser.close()
        print("\n[JOY] Stopped.")
