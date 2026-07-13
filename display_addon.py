"""
display_addon.py — touch-display support for Ben Pro Max.

Adds three routes to the existing Flask server:

  GET  /display          -> serves display.html (the kiosk touch UI)
  GET  /display/events   -> Server-Sent Events stream (live speech, notices)
  POST /display/say      -> push an event to the display
                            {"type":"speech_start","text":"Hello!"}
                            {"type":"speech_end"}
                            {"type":"notice","text":"WiFi updated"}

Wire-up (2 lines in your server file, after `app = Flask(__name__)`):

    from display_addon import init_display, notify_display
    init_display(app)

Then make your TTS report what it says. In audio_utils.speak():

    from display_addon import notify_display
    def speak(text):
        notify_display("speech_start", text)
        ...play the audio (blocking)...
        notify_display("speech_end")

notify_display() works from the same process OR any other process on the
Jetson (it falls back to an HTTP POST to localhost:5000), so it's safe to
call from your voice-assistant script even if it runs separately.
"""

import json
import os
import queue
import threading

# Folder containing this file — display.html, three.min.js, logo.png and
# the videos/ library live next to it. Works on any machine (Jetson, Pi).
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Wi-Fi client interface (internet). The AP interface is never touched.
#   Jetson: internal card  = "wlP1p1s0"
#   Raspberry Pi: usually  = "wlan0"     <-- change this per robot
WIFI_CLIENT_IFACE = "wlan0"
_subscribers = []
_lock = threading.Lock()


# ── broadcast core ─────────────────────────────────────────
def _broadcast(event: dict):
    dead = []
    with _lock:
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def notify_display(event_type: str, text: str = ""):
    """Send an event to the touch display.
    Tries in-process first; falls back to HTTP so separate
    processes (voice loop, etc.) can use the same call."""
    event = {"type": event_type, "text": text}
    if _subscribers or _registered:
        _broadcast(event)
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:5000/display/say",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # display not up yet — never block speech on this


def load_json_list_safe(path):
    """Read a JSON list file; [] on any problem."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ── voice hook: reminders ──────────────────────────────────
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
            "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
            "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
            "twenty": 20, "twenty five": 25, "thirty": 30, "forty": 40,
            "forty five": 45, "fifty": 50, "sixty": 60, "seventy": 70,
            "eighty": 80, "ninety": 90, "a": 1, "an": 1}


def try_reminder_command(text: str):
    """If the text sets or cancels a reminder/alarm/timer, do it and
    return a confirmation sentence for Ben to speak. Returns None if the
    text is not a reminder command.

    Understands e.g.:
      "remind me in 10 minutes to check the oven"
      "remind me to take medicine in half an hour"
      "remind me at 5:30 to call amma"
      "set a timer for 2 minutes"
      "cancel my reminders"

    Usage in the voice loop (before video/Q&A checks):

        resp = try_reminder_command(user_text)
        if resp:
            speak(resp)
    """
    import re
    import time as _t
    import urllib.request
    import urllib.parse
    low = (text or "").lower().strip()
    if not any(k in low for k in ("remind", "reminder", "alarm", "timer")):
        return None

    def _call(path):
        try:
            urllib.request.urlopen("http://127.0.0.1:5000" + path, timeout=8)
            return True
        except Exception as e:
            print(f"[REMIND] request failed: {e}")
            return False

    # cancel
    if re.search(r"\b(cancel|stop|delete|clear)\b.*\b(reminder|alarm|timer)", low):
        _call("/display/reminder_cancel?id=all")
        return "Okay, I've cancelled your reminders."

    # duration: "10 minutes", "two hours", "half an hour", "90 seconds"
    def parse_duration(s):
        m = re.search(r"\b(?:(\d+(?:\.\d+)?)|(" + "|".join(_WORDNUM) +
                      r")|half an?)\s*(second|sec|minute|min|hour|hr)s?\b", s)
        if not m:
            return None, None
        if m.group(1):
            n = float(m.group(1))
        elif m.group(2):
            n = float(_WORDNUM[m.group(2)])
        else:
            n = 0.5
        unit = m.group(3)
        mins = n / 60 if unit.startswith("sec") else n * 60 if unit.startswith(("hour", "hr")) else n
        return mins, m.group(0)

    # clock time: "at 5:30", "at 5 30 pm", "at 7 am", "at 530" (dot stripped)
    def parse_at(s):
        m = re.search(r"\bat\s+(\d{1,2})[:. ](\d{2})\s*(am|pm)?\b", s)
        if m:
            h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        else:
            m = re.search(r"\bat\s+(\d{3,4})\s*(am|pm)?\b", s)
            if m:  # "530" -> 5:30, "1730" -> 17:30
                num = m.group(1)
                h, mi, ap = int(num[:-2]), int(num[-2:]), m.group(2)
            else:
                m = re.search(r"\bat\s+(\d{1,2})\s*(am|pm)?\b", s)
                if not m:
                    return None, None
                h, mi, ap = int(m.group(1)), 0, m.group(2)
        h %= 24
        if mi > 59:
            return None, None
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        now = _t.localtime()
        target = _t.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                            h, mi, 0, 0, 0, -1))
        if target <= _t.time() and not ap and h < 12:
            target += 12 * 3600            # "at 5" said in the evening = 5 pm
        if target <= _t.time():
            target += 24 * 3600            # otherwise tomorrow
        return target, m.group(0)

    mins, dphrase = parse_duration(low)
    at, aphrase = (None, None) if mins else parse_at(low)
    if mins is None and at is None:
        return None

    # the task = what's left after removing command words and the time phrase
    task = low
    for junk in filter(None, [dphrase, aphrase]):
        task = task.replace(junk, " ")
    task = re.sub(r"\b(please|hey|ben|remind me|set (a |an )?"
                  r"(reminder|alarm|timer)( for| in)?|reminder|alarm|timer"
                  r"|\bto\b|\bin\b|\bfor\b|\bat\b)\b", " ", task)
    task = re.sub(r"\s+", " ", task).strip(" .,!?")
    task = task or "time's up"

    if mins is not None:
        ok = _call("/display/reminder_add?minutes=" + str(mins)
                   + "&text=" + urllib.parse.quote(task))
        if mins < 5 and mins % 1:
            nice = f"{int(mins * 60)} seconds"
        elif mins < 1:
            nice = f"{int(mins * 60)} seconds"
        elif mins < 60:
            nice = f"{int(mins)} minute{'s' if mins >= 2 else ''}"
        else:
            nice = f"{mins / 60:g} hour{'s' if mins >= 120 else ''}"
        if not ok:
            return "Sorry, I couldn't set that reminder."
        if task == "time's up":
            return f"Okay — timer set for {nice}."
        return f"Okay — I'll remind you in {nice} to {task}."
    else:
        ok = _call("/display/reminder_add?at=" + str(at)
                   + "&text=" + urllib.parse.quote(task))
        tstr = _t.strftime("%I:%M %p", _t.localtime(at)).lstrip("0")
        return (f"Okay — I'll remind you at {tstr} to {task}." if ok
                else "Sorry, I couldn't set that reminder.")


def notify_face(name: str, greeting: str = ""):
    """Show a face-greeting card on the display (photo + name + greeting).
    Call from face recognition right before speaking the greeting:

        from display_addon import notify_face
        notify_face(name, f"Hello {name}, {greeting}")
        speak(f"Hello {name}, {greeting}")
    """
    event = {"type": "face_greet", "name": name or "", "text": greeting or ""}
    if _subscribers or _registered:
        _broadcast(event)
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:5000/display/say",
            data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass


_registered = False


# ── voice hook: call this with the user's transcribed text ─
def try_video_command(text: str):
    """If the text is a video request ('play a video about lions',
    'show me a video of the taj mahal', 'lion video play pannu', ...),
    trigger online playback on the display and return the query string.
    Returns None if the text is not a video request, so your voice loop
    can fall through to Q&A / chat as usual.

    Works from any process — it calls the local server over HTTP.

    Usage in your voice assistant, right after speech-to-text:

        from display_addon import try_video_command
        q = try_video_command(user_text)
        if q:
            speak(f"Playing a video about {q} on my screen")
        else:
            ... normal answer flow ...
    """
    import re
    t = (text or "").lower().strip()
    if "video" not in t and "youtube" not in t:
        return None
    patterns = [
        r"(?:play|show|put on|open)\s+(?:me\s+)?(?:a\s+|the\s+|some\s+)?"
        r"(?:youtube\s+)?videos?\s+(?:of|about|on|for)\s+(.+)",
        r"(?:play|show|put on|open)\s+(?:me\s+)?(.+?)\s+videos?(?:\s+.*)?$",
        r"(.+?)\s+videos?\s+(?:play|show)",
    ]
    query = None
    for p in patterns:
        m = re.search(p, t)
        if m:
            query = m.group(1).strip(" .,!?")
            break
    if not query:
        return None
    try:
        import urllib.request
        import urllib.parse
        url = ("http://127.0.0.1:5000/display/play_online?query="
               + urllib.parse.quote(query))
        urllib.request.urlopen(url, timeout=25)
    except Exception as e:
        print(f"[ONLINE] Voice video request failed: {e}")
    return query


# ── flask wiring ───────────────────────────────────────────
def init_display(app):
    global _registered
    from flask import Response, request, send_from_directory

    @app.route("/display")
    def display_page():
        return send_from_directory(_BASE_DIR, "display.html")

    @app.route("/display/three.min.js")
    def display_three():
        return send_from_directory(_BASE_DIR, "three.min.js")

    @app.route("/display/logo.png")
    def display_logo():
        # optional — drop your Technovation logo.png next to display.html
        return send_from_directory(_BASE_DIR, "logo.png")

    @app.route("/display/events")
    def display_events():
        def stream():
            q = queue.Queue(maxsize=32)
            with _lock:
                _subscribers.append(q)
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        event = q.get(timeout=25)
                        yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                with _lock:
                    if q in _subscribers:
                        _subscribers.remove(q)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── video library ──────────────────────────────────
    import os
    import re
    videos_dir = os.path.join(_BASE_DIR, "videos")
    os.makedirs(videos_dir, exist_ok=True)
    allowed_ext = {".mp4", ".webm", ".ogv", ".ogg", ".m4v", ".mov", ".mkv"}

    def _safe_name(name):
        name = os.path.basename(name or "")
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip()
        root, ext = os.path.splitext(name)
        if not root or ext.lower() not in allowed_ext:
            return None
        return f"{root}{ext.lower()}"

    @app.route("/display/upload_video", methods=["POST"])
    def display_upload_video():
        f = request.files.get("video")
        if not f:
            return {"status": "error", "message": "No file received"}, 400
        name = _safe_name(f.filename)
        if not name:
            return {"status": "error",
                    "message": "Use MP4, WebM, OGV, M4V, MOV or MKV"}, 400
        path = os.path.join(videos_dir, name)
        f.save(path)
        print(f"[MEDIA] Uploaded {name} "
              f"({os.path.getsize(path) // 1024} KB)")
        _broadcast({"type": "notice", "text": f"Video added: {name}"})
        return {"status": "ok", "name": name}, 200

    @app.route("/display/videos")
    def display_videos():
        out = []
        for n in sorted(os.listdir(videos_dir)):
            p = os.path.join(videos_dir, n)
            if os.path.isfile(p) and os.path.splitext(n)[1].lower() in allowed_ext:
                out.append({"name": n, "size": os.path.getsize(p)})
        return json.dumps(out), 200, {"Content-Type": "application/json"}

    @app.route("/display/video/<path:name>")
    def display_video(name):
        name = _safe_name(name)
        if not name:
            return {"status": "error", "message": "Bad name"}, 400
        # conditional=True enables HTTP Range → seeking works in the player
        return send_from_directory(videos_dir, name, conditional=True)

    @app.route("/display/video_delete")
    def display_video_delete():
        name = _safe_name(request.args.get("name"))
        path = os.path.join(videos_dir, name) if name else None
        if path and os.path.isfile(path):
            os.remove(path)
            print(f"[MEDIA] Deleted {name}")
        return {"status": "ok"}, 200

    @app.route("/display/play")
    def display_play():
        name = _safe_name(request.args.get("name"))
        if not name or not os.path.isfile(os.path.join(videos_dir, name)):
            return {"status": "error", "message": "Video not found"}, 404
        _broadcast({"type": "play_video", "text": name})
        print(f"[MEDIA] Playing {name}")
        return {"status": "ok"}, 200

    @app.route("/display/stop_play")
    def display_stop_play():
        _broadcast({"type": "stop_video"})
        return {"status": "ok"}, 200

    # ── online videos (YouTube via yt-dlp, no API key) ─────
    def _yt_search(query, n=6):
        """Search YouTube. Returns list of {id,title,duration,channel}."""
        import subprocess as sp
        try:
            r = sp.run(
                ["yt-dlp", f"ytsearch{n}:{query}",
                 "--flat-playlist", "--dump-json", "--no-warnings"],
                capture_output=True, text=True, timeout=20)
        except FileNotFoundError:
            return None  # yt-dlp not installed
        except Exception as e:
            print(f"[ONLINE] Search error: {e}")
            return []
        out = []
        for line in r.stdout.splitlines():
            try:
                j = json.loads(line)
                out.append({
                    "id": j.get("id", ""),
                    "title": j.get("title", ""),
                    "duration": j.get("duration") or 0,
                    "channel": j.get("channel") or j.get("uploader") or "",
                })
            except Exception:
                pass
        return out

    @app.route("/display/search_online")
    def display_search_online():
        q = (request.args.get("query") or "").strip()
        if not q:
            return {"status": "error", "message": "Empty query"}, 400
        res = _yt_search(q)
        if res is None:
            return {"status": "error",
                    "message": "yt-dlp not installed — run: pip3 install yt-dlp"}, 503
        return json.dumps(res), 200, {"Content-Type": "application/json"}

    @app.route("/display/play_online")
    def display_play_online():
        """Play by ?id=VIDEOID or search-and-play-top-result by ?query=..."""
        vid   = (request.args.get("id") or "").strip()
        title = (request.args.get("title") or "").strip()
        if not vid:
            q = (request.args.get("query") or "").strip()
            if not q:
                return {"status": "error", "message": "Need id or query"}, 400
            res = _yt_search(q, n=1)
            if res is None:
                return {"status": "error",
                        "message": "yt-dlp not installed — run: pip3 install yt-dlp"}, 503
            if not res:
                _broadcast({"type": "notice", "text": f"No video found for: {q}"})
                return {"status": "error", "message": "No results"}, 404
            vid, title = res[0]["id"], res[0]["title"]
        _broadcast({"type": "play_youtube", "id": vid, "text": title})
        print(f"[ONLINE] Playing {vid} — {title}")
        return {"status": "ok", "id": vid, "title": title}, 200

    # ── Wi-Fi client mode: scan / connect / status ──────────
    # (different from your /settings AP endpoint — this joins an
    #  existing network so Ben gets internet)
    import subprocess as _sp

    def _nmcli(args, timeout=25):
        """Run nmcli. Tries passwordless sudo (-n = never prompt, fail
        fast) and falls back to plain nmcli, which usually works
        unprivileged for scanning. Returns the best CompletedProcess."""
        first = None
        try:
            first = _sp.run(["sudo", "-n", "nmcli"] + args,
                            capture_output=True, text=True, timeout=timeout)
            if first.returncode == 0:
                return first
        except Exception:
            first = None
        second = _sp.run(["nmcli"] + args,
                         capture_output=True, text=True, timeout=timeout)
        if second.returncode == 0 or first is None:
            return second
        return first

    @app.route("/display/wifi_scan")
    def display_wifi_scan():
        try:
            r = _nmcli(["-t", "-f", "SSID,SIGNAL,SECURITY",
                        "dev", "wifi", "list", "ifname", WIFI_CLIENT_IFACE,
                        "--rescan", "yes"], timeout=25)
        except Exception as e:
            return {"status": "error", "message": str(e)}, 500
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "nmcli failed").strip()[:160]
            return {"status": "error", "message": err}, 500
        nets = {}
        for line in r.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            sec  = parts[-1]
            sig  = parts[-2]
            ssid = ":".join(parts[:-2]).replace("\\:", ":").strip()
            if not ssid:
                continue
            try:
                s = int(sig)
            except ValueError:
                s = 0
            if ssid not in nets or s > nets[ssid]["signal"]:
                nets[ssid] = {"ssid": ssid, "signal": s,
                              "secured": bool(sec and sec != "--")}
        out = sorted(nets.values(), key=lambda n: -n["signal"])
        return json.dumps(out), 200, {"Content-Type": "application/json"}

    def _wifi_forget(ssid):
        """Delete a saved connection profile by name — but never an
        AP-mode profile (that would kill the robot's own hotspot)."""
        try:
            mode = _nmcli(["-t", "-f", "802-11-wireless.mode",
                           "connection", "show", ssid], timeout=10)
            if "ap" in (mode.stdout or "").lower():
                print(f"[WIFI] Not deleting '{ssid}' — it's the AP profile")
                return False
            r = _nmcli(["connection", "delete", ssid], timeout=10)
            ok = r.returncode == 0
            print(f"[WIFI] Forgot stale profile '{ssid}': {ok}")
            return ok
        except Exception as e:
            print(f"[WIFI] Forget failed: {e}")
            return False

    @app.route("/display/wifi_connect")
    def display_wifi_connect():
        ssid = (request.args.get("ssid") or "").strip()
        pwd  = request.args.get("password") or ""
        if not ssid:
            return {"status": "error", "message": "No network name"}, 400
        args = ["dev", "wifi", "connect", ssid,
                "ifname", WIFI_CLIENT_IFACE]
        if pwd:
            args += ["password", pwd]
        try:
            r = _nmcli(args, timeout=50)
            blob = (r.stdout + r.stderr).lower()
            # stale/corrupt saved profile → forget it and retry once
            if r.returncode != 0 and ("key-mgmt" in blob
                                      or "property is missing" in blob):
                print(f"[WIFI] Stale profile for '{ssid}' — healing")
                if _wifi_forget(ssid):
                    r = _nmcli(args, timeout=50)
                    blob = (r.stdout + r.stderr).lower()
        except Exception as e:
            return {"status": "error", "message": str(e)}, 500
        if r.returncode == 0 and "successfully" in blob:
            print(f"[WIFI] Connected to '{ssid}'")
            _broadcast({"type": "notice", "text": f"Connected to {ssid}"})
            return {"status": "ok", "ssid": ssid}, 200
        # nmcli sometimes reports failure/timeout while NetworkManager
        # finishes connecting in the background — verify before failing
        import time as _wt
        for _ in range(4):
            _wt.sleep(3)
            try:
                st = _nmcli(["-t", "-f", "ACTIVE,SSID", "dev", "wifi",
                             "ifname", WIFI_CLIENT_IFACE], timeout=8)
                for line in st.stdout.splitlines():
                    if (line.startswith("yes:")
                            and line[4:].replace("\\:", ":") == ssid):
                        print(f"[WIFI] Connected to '{ssid}' (late)")
                        _broadcast({"type": "notice",
                                    "text": f"Connected to {ssid}"})
                        return {"status": "ok", "ssid": ssid}, 200
            except Exception:
                pass
        msg = "Wrong password" if ("secrets" in blob or "password" in blob) \
              else (r.stderr.strip().splitlines() or ["Connection failed"])[-1][:120]
        print(f"[WIFI] Connect failed: {msg}")
        return {"status": "error", "message": msg}, 400

    @app.route("/display/net_status")
    def display_net_status():
        ssid = ""
        try:
            r = _nmcli(["-t", "-f", "ACTIVE,SSID",
                        "dev", "wifi", "ifname", WIFI_CLIENT_IFACE], timeout=8)
            for line in r.stdout.splitlines():
                if line.startswith("yes:"):
                    ssid = line[4:].replace("\\:", ":")
                    break
        except Exception:
            pass
        online = False
        try:
            import urllib.request
            req = urllib.request.urlopen(
                "http://connectivitycheck.gstatic.com/generate_204", timeout=4)
            online = (req.status == 204)
        except Exception:
            online = False
        return {"online": online, "ssid": ssid}, 200

    # ── weather for the idle screen (auto location, no API key) ──
    # Optional override: save {"weather_city": "Mumbai"} in settings to pin
    # the city; empty/absent = automatic from the internet connection.
    _wx_cache = {"t": 0.0, "data": None, "key": ""}

    @app.route("/display/weather")
    def display_weather():
        import time as _time
        import urllib.request as _ur
        import urllib.parse as _up
        city_override = ""
        try:
            from settings import load_settings
            city_override = str(load_settings().get("weather_city") or "").strip()
        except Exception:
            pass
        if (_wx_cache["data"] and _wx_cache["key"] == city_override
                and _time.time() - _wx_cache["t"] < 900):
            return _wx_cache["data"], 200
        try:
            if city_override:
                g = json.loads(_ur.urlopen(
                    "https://geocoding-api.open-meteo.com/v1/search?name="
                    + _up.quote(city_override) + "&count=1",
                    timeout=8).read())
                hits = g.get("results") or []
                if not hits:
                    return {"error": f"City not found: {city_override}"}, 404
                lat, lon = hits[0]["latitude"], hits[0]["longitude"]
                city = hits[0].get("name", city_override)
            else:
                loc = json.loads(_ur.urlopen(
                    "http://ip-api.com/json/?fields=status,city,lat,lon",
                    timeout=6).read())
                if loc.get("status") != "success":
                    raise Exception("geolocation failed")
                lat, lon = loc["lat"], loc["lon"]
                city = loc.get("city", "")
            w = json.loads(_ur.urlopen(
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&current=temperature_2m,relative_humidity_2m,weather_code",
                timeout=8).read())
            cur = w.get("current", {})
            data = {
                "city": city,
                "temp": round(cur.get("temperature_2m", 0)),
                "humidity": cur.get("relative_humidity_2m", 0),
                "code": cur.get("weather_code", 0),
            }
            _wx_cache["t"] = _time.time()
            _wx_cache["data"] = data
            _wx_cache["key"] = city_override
            return data, 200
        except Exception as e:
            return {"error": str(e)}, 503

    # ── reminders / alarms ──────────────────────────────────
    import time as _rtime
    REMINDERS_FILE = os.path.join(_BASE_DIR, "reminders.json")
    _rem = {}          # id -> {"id","text","at"}
    _rem_lock = threading.Lock()

    def _rem_save():
        try:
            with open(REMINDERS_FILE, "w") as f:
                json.dump(list(_rem.values()), f, indent=2)
        except Exception as e:
            print(f"[REMIND] Save error: {e}")

    def _rem_fire(rid):
        with _rem_lock:
            rem = _rem.pop(rid, None)
            if rem:
                _rem_save()
        if not rem:
            return  # was cancelled
        print(f"[REMIND] FIRING: {rem['text']}")
        _broadcast({"type": "reminder", "text": rem["text"]})

        def talk():
            try:
                from audio_utils import speak
                speak(f"Reminder! {rem['text']}")
            except Exception as e:
                print(f"[REMIND] Speak failed: {e}")
        threading.Thread(target=talk, daemon=True).start()

    def _rem_schedule(rem):
        delay = max(1.0, rem["at"] - _rtime.time())
        t = threading.Timer(delay, _rem_fire, args=[rem["id"]])
        t.daemon = True
        t.start()

    # reload reminders that survived a restart
    for _r in load_json_list_safe(REMINDERS_FILE):
        if _r.get("at", 0) > _rtime.time():
            _rem[_r["id"]] = _r
            _rem_schedule(_r)
    if _rem:
        print(f"[REMIND] Restored {len(_rem)} pending reminder(s)")

    @app.route("/display/reminder_add")
    def display_reminder_add():
        text = (request.args.get("text") or "").strip() or "Time's up"
        at   = request.args.get("at")       # epoch seconds, OR:
        mins = request.args.get("minutes")  # minutes from now
        try:
            when = float(at) if at else _rtime.time() + float(mins) * 60
        except (TypeError, ValueError):
            return {"status": "error", "message": "Need at= or minutes="}, 400
        if when <= _rtime.time():
            return {"status": "error", "message": "Time is in the past"}, 400
        rem = {"id": str(int(_rtime.time() * 1000)), "text": text,
               "at": when}
        with _rem_lock:
            _rem[rem["id"]] = rem
            _rem_save()
        _rem_schedule(rem)
        print(f"[REMIND] Set: '{text}' in {int(when - _rtime.time())}s")
        return {"status": "ok", "id": rem["id"], "at": when}, 200

    @app.route("/display/reminders")
    def display_reminders():
        now = _rtime.time()
        with _rem_lock:
            out = sorted(
                ({"id": r["id"], "text": r["text"], "at": r["at"],
                  "in_s": int(r["at"] - now)} for r in _rem.values()),
                key=lambda r: r["at"])
        return json.dumps(out), 200, {"Content-Type": "application/json"}

    @app.route("/display/reminder_cancel")
    def display_reminder_cancel():
        rid = request.args.get("id")
        with _rem_lock:
            if rid == "all":
                n = len(_rem)
                _rem.clear()
            else:
                n = 1 if _rem.pop(rid, None) else 0
            _rem_save()
        print(f"[REMIND] Cancelled {n}")
        return {"status": "ok", "cancelled": n}, 200

    @app.route("/display/say", methods=["POST"])
    def display_say():
        data = request.get_json(silent=True) or {}
        event = {
            "type": data.get("type", "notice"),
            "text": data.get("text", ""),
        }
        if data.get("name"):
            event["name"] = str(data["name"])
        _broadcast(event)
        return {"status": "ok"}, 200

    # ── face photo lookup for the greeting card ─────────────
    @app.route("/display/face_photo")
    def display_face_photo():
        import base64 as _b64
        from flask import send_file, Response
        name = (request.args.get("name") or "").strip().lower()
        if not name:
            return {"status": "error"}, 404

        candidates = []
        # 1) JSON stores: vision_data.json (Pi) / faces_db.json (Jetson)
        for store in ("vision_data.json", "faces_db.json"):
            path = os.path.join(_BASE_DIR, store)
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:
                continue
            entries = (data.items() if isinstance(data, dict)
                       else enumerate(data) if isinstance(data, list) else [])
            for key, entry in entries:
                if not isinstance(entry, dict):
                    continue
                # the person's name may live in any of these fields
                labels = [str(entry.get(f) or "").strip().lower()
                          for f in ("name", "label", "person", "speech")]
                labels.append(str(key).strip().lower())
                if not any(lb == name or (lb and name in lb.split())
                           for lb in labels):
                    continue
                for field in ("image", "photo", "img", "face", "path", "file"):
                    v = entry.get(field)
                    if isinstance(v, str) and v:
                        candidates.append(v)
        # 2) image files named after the person
        for d in ("faces", "known_faces", "vision", "images", "."):
            folder = os.path.join(_BASE_DIR, d)
            try:
                for fn in os.listdir(folder):
                    root, ext = os.path.splitext(fn)
                    if (root.strip().lower() == name
                            and ext.lower() in (".jpg", ".jpeg", ".png", ".webp")):
                        candidates.append(os.path.join(folder, fn))
            except Exception:
                pass

        for c in candidates:
            # base64 / data-URI stored image
            if c.startswith("data:image") or (len(c) > 500 and "/" not in c[:80]):
                try:
                    mt = "image/jpeg"
                    if c.startswith("data:"):
                        mt = c[5:c.index(";")] or mt
                    b64 = c.split(",", 1)[1] if "," in c else c
                    return Response(_b64.b64decode(b64), mimetype=mt)
                except Exception:
                    continue
            p = c if os.path.isabs(c) else os.path.join(_BASE_DIR, c)
            if os.path.isfile(p):
                return send_file(p)
        return {"status": "error", "message": "no photo"}, 404

    _registered = True
    print("[DISPLAY] Routes ready at /display")
