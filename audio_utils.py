import subprocess
import os
import time
import shutil
import threading
import pwd

# Paths resolve relative to this file — same behavior on /home/ben,
# and portable if the project moves.
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
RECORD_PATH = "/tmp/recorded.wav"
RAW_PATH    = "/tmp/raw_recorded.wav"
TTS_WAV     = "/tmp/tts_out.wav"
PIPER_BIN   = shutil.which("piper") or "/usr/local/bin/piper"
VOICES_DIR  = os.path.join(BASE_DIR, "voices")

# Only one audio operation at a time prevents device conflicts
audio_lock = threading.Lock()

# Set while TTS is playing. Check this in the mic/STT loop so the robot
# doesn't listen to (and transcribe) its own voice:
#     if audio_utils.is_speaking.is_set(): skip this cycle
is_speaking = threading.Event()

# Whisper's classic outputs for silence / room noise — ignore these in STT.
_NOISE_TRANSCRIPTS = {"", ".", "you", "you.", "thank you", "thank you.",
                      "thanks for watching", "thanks for watching."}


def is_noise_transcript(text: str) -> bool:
    """True if an STT result is almost certainly a silence artifact."""
    return text.strip().lower() in _NOISE_TRANSCRIPTS


# ---------------------------------------------------------------------------
# Device detection (cached — no subprocess spam on every call)
# ---------------------------------------------------------------------------

_mic_device = None
_speaker_device = None


def get_mic_device(refresh=False):
    global _mic_device
    if _mic_device and not refresh:
        return _mic_device
    result = subprocess.run(['arecord', '-l'], capture_output=True, text=True)
    # Preference order: wireless lav mic -> INMP441 (voiceHAT) -> default
    for keyword, label in [('mfiiap2', 'Wireless mic'),
                           ('googlevoice', 'Google Voice HAT')]:
        for line in result.stdout.splitlines():
            if keyword in line.lower():
                parts = line.split(':')
                if parts[0].startswith('card '):
                    card = int(parts[0].replace('card ', '').strip())
                    print(f"[MIC] {label} found on card {card}")
                    _mic_device = f"plughw:{card},0"
                    return _mic_device
    print("[MIC] Using default plughw:0,0")
    _mic_device = "plughw:0,0"
    return _mic_device


def get_speaker_device(refresh=False):
    global _speaker_device
    if _speaker_device and not refresh:
        return _speaker_device
    result = subprocess.run(['aplay', '-l'], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if 'usb audio' in line.lower():
            parts = line.split(':')
            if parts[0].startswith('card '):
                card = int(parts[0].replace('card ', '').strip())
                print(f"[SPEAKER] USB dongle is card {card}")
                _speaker_device = f"plughw:{card},0"
                return _speaker_device
    print("[SPEAKER] Using default plughw:1,0")
    _speaker_device = "plughw:1,0"
    return _speaker_device


# ---------------------------------------------------------------------------
# Desktop-session (PipeWire) helpers
# ---------------------------------------------------------------------------

def _get_session_user():
    """Return (user, uid) of the logged-in desktop user, or (None, None).
    PipeWire runs in that user's session and owns the USB card once the
    browser/display has used audio."""
    try:
        uids = [d for d in os.listdir('/run/user')
                if d.isdigit() and d != '0']
        if not uids:
            return None, None
        uid = sorted(uids, key=int)[0]
        return pwd.getpwuid(int(uid)).pw_name, uid
    except Exception:
        return None, None


def _play_wav_session(wav_path):
    """Play a wav via PipeWire (mixes with browser audio, never hits
    'device busy'). Works whether this process runs as root (uses
    runuser to enter the desktop user's session) or as the desktop
    user itself (runs the player directly). Returns True on success."""
    running_as_root = (os.geteuid() == 0)
    if running_as_root:
        user, uid = _get_session_user()
        if not user:
            return False
        prefix = ['runuser', '-u', user, '--', 'env',
                  f'XDG_RUNTIME_DIR=/run/user/{uid}']
        env = None
    else:
        prefix = []
        env = dict(os.environ)
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.geteuid()}')
    for player in (['pw-play', wav_path],
                   ['paplay', wav_path],
                   ['aplay', '-D', 'default', wav_path]):
        if not shutil.which(player[0]):
            continue
        p = subprocess.run(prefix + player, env=env,
                           capture_output=True, timeout=60)
        if p.returncode == 0:
            return True
        print(f"[TTS] {player[0]} failed: "
              f"{p.stderr.decode(errors='replace').strip()[-220:]}")
    return False


def _play_wav_direct(wav_path):
    """Direct ALSA playback — only works when PipeWire is NOT holding the
    card (e.g. headless boot, no desktop session). Returns True on success."""
    dev = get_speaker_device()
    p = subprocess.run(['aplay', '-q', '-D', dev, wav_path],
                       capture_output=True, timeout=60)
    if p.returncode == 0:
        return True
    err = p.stderr.decode(errors='replace').strip()[-200:]
    print(f"[TTS] direct aplay on {dev} failed: {err}")
    return False


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

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
                # card numbers may have shuffled (USB replug) — re-detect
                get_mic_device(refresh=True)
                time.sleep(2)   # backoff, never flood on mic failure
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


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

def _synthesize(model, text):
    """Piper -> sox -> wav on disk. Returns wav path or None."""
    gen = (
        f'{PIPER_BIN} --model {model} --output_raw | '
        f'sox -t raw -r 22050 -e signed-integer -b 16 -c 1 - '
        f'-t wav -r 48000 -e signed-integer -b 16 -c 1 {TTS_WAV}'
    )
    g = subprocess.run(gen, shell=True, input=text.encode(),
                       capture_output=True, timeout=60)
    if g.returncode != 0 or not os.path.isfile(TTS_WAV):
        err = g.stderr.decode(errors='replace').strip()[-220:]
        print(f"[TTS] synthesis failed: {err}")
        return None
    os.chmod(TTS_WAV, 0o644)
    return TTS_WAV


def speak(text: str):
    # -- Display caption: show what's being said ------------
    try:
        from display_addon import notify_display
        notify_display("speech_start", text)
    except Exception:
        pass

    is_speaking.set()
    with audio_lock:
        try:
            from settings import load_settings
            s = load_settings()
            voice = s.get('voice', 'female')
        except Exception:
            voice = 'female'
        voice_map = {
            'female': os.path.join(VOICES_DIR, 'en_US-amy-medium.onnx'),
            'male':   os.path.join(VOICES_DIR, 'en_US-ryan-medium.onnx'),
        }
        model = voice_map.get(voice, voice_map['female'])
        try:
            if not os.path.isfile(model):
                print(f"[TTS] FAILED — voice model missing: {model}")
                return
            if not (PIPER_BIN and os.path.isfile(PIPER_BIN)):
                print(f"[TTS] FAILED — piper not found (looked at: {PIPER_BIN})")
                return

            wav = _synthesize(model, text)
            if not wav:
                return

            # PipeWire owns the USB card whenever a desktop session is
            # active, so session playback is the PRIMARY path. Direct
            # ALSA is the fallback for headless boots.
            if _play_wav_session(wav):
                print("[TTS] Played via desktop session")
            elif _play_wav_direct(wav):
                print("[TTS] Played via direct ALSA")
            else:
                print("[TTS] FAILED — all playback paths exhausted")
        except Exception as e:
            print(f"[TTS] Error: {e}")
        finally:
            is_speaking.clear()

    # -- Display caption: clear speaking state --------------
    try:
        from display_addon import notify_display
        notify_display("speech_end")
    except Exception:
        pass
