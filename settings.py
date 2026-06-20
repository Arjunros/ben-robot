import json, os

SETTINGS_FILE = "settings.json"

VOICES = {
    "female": "/home/ben/pi_assistant/voices/en_US-amy-medium.onnx",
    "male":   "/home/ben/pi_assistant/voices/en_US-ryan-medium.onnx",
    "child":  "/home/ben/pi_assistant/voices/en_US-lessac-low.onnx",
}

DEFAULT = {
    "language":      "en",
    "voice":         "female",
    "speed":         "150",
    "ai_model":      "gpt-5.4-nano",
    "robot_name":    "Pi Assistant",
    "welcome_speech": "System ready"
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return DEFAULT.copy()

def save_settings(data):
    s = load_settings()
    s.update(data)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)
    return s

def get_voice_model():
    s = load_settings()
    voice = s.get("voice", "female")
    return VOICES.get(voice, VOICES["female"])
