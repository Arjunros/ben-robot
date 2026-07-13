import subprocess
import os
import time
import threading

RECORD_PATH = "/tmp/recorded.wav"
RAW_PATH    = "/tmp/raw_recorded.wav"
PIPER_BIN   = '/home/ben/pi_assistant/venv/bin/piper'

# Only one audio operation at a time prevents device conflicts
audio_lock = threading.Lock()

def get_mic_device():
    result = subprocess.run(['arecord', '-l'], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if 'googlevoice' in line.lower():
            parts = line.split(':')
            if parts[0].startswith('card '):
                card = int(parts[0].replace('card ', '').strip())
                print(f"[MIC] Google Voice HAT found on card {card}")
                return f"plughw:{card},0"
    print("[MIC] Using default plughw:0,0")
    return "plughw:0,0"

def get_speaker_device():
    result = subprocess.run(['aplay', '-l'], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if 'usb audio' in line.lower():
            parts = line.split(':')
            if parts[0].startswith('card '):
                card = int(parts[0].replace('card ', '').strip())
                print(f"[SPEAKER] USB found on card {card}")
                return f"plughw:{card},0"
    print("[SPEAKER] Using default plughw:1,0")
    return "plughw:1,0"

def _record_and_convert(duration):
    with audio_lock:
        mic_dev = get_mic_device()
        print(f"[MIC] Recording {duration}s from {mic_dev}...")
        try:
            rec_cmd = [
                'arecord',
                '-D', mic_dev,
                '-c', '1',
                '-r', '48000',
                '-f', 'S16_LE',
                '-d', str(duration),
                RECORD_PATH
            ]
            result = subprocess.run(rec_cmd, capture_output=True)
            if result.returncode != 0:
                print(f"[MIC] arecord error: {result.stderr.decode()}")
                time.sleep(2)   # backoff never flood on mic failure
                return None
            print(f"[MIC] Saved -> {RECORD_PATH}")
            return RECORD_PATH
        except Exception as e:
            print(f"[MIC] Exception: {e}")
            time.sleep(2)
            return None

def record_audio(duration=3):
    # 3 seconds for wake word detection
    return _record_and_convert(duration)

def record_question():
    # 5 seconds for full question after wake word
    return _record_and_convert(5)

def speak(text: str):
    # -- Display caption: show what's being said ------------
    try:
        from display_addon import notify_display
        notify_display("speech_start", text)
    except: pass

    with audio_lock:
        from settings import load_settings
        s     = load_settings()
        voice = s.get('voice', 'female')
        voice_map = {
            'female': '/home/ben/pi_assistant/voices/en_US-amy-medium.onnx',
            'male':   '/home/ben/pi_assistant/voices/en_US-ryan-medium.onnx',
        }
        model       = voice_map.get(voice, voice_map['female'])
        speaker_dev = get_speaker_device()
        print(f"[TTS] Speaking on {speaker_dev}")
        try:
            piper_cmd = f'echo "{text}" | {PIPER_BIN} --model {model} --output_raw 2>/dev/null'
            play_cmd  = (
                f'sox -t raw -r 22050 -e signed-integer -b 16 -c 1 - '
                f'-t wav -r 48000 -e signed-integer -b 16 -c 1 - | '
                f'aplay -D {speaker_dev}'
            )
            result = subprocess.run(
                f'{piper_cmd} | {play_cmd}',
                shell=True,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                print(f"[TTS] Error: {result.stderr}")
        except Exception as e:
            print(f"[TTS] Error: {e}")

    # -- Display caption: clear speaking state --------------
    try:
        from display_addon import notify_display
        notify_display("speech_end")
    except: pass
