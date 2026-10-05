#!/usr/bin/env python3
# BirdThing dashboard API: serves recent BirdNET detections + bird photos for the
# Car Thing 800x480 screen. Reads BirdNET-Pi's SQLite DB; proxies/caches Wikipedia photos.
import sqlite3, os, json, urllib.request, urllib.parse, urllib.error, threading, time, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
HTML = "/opt/birdthing/birdthing-dashboard.html"
CACHE = "/opt/birdthing/imgcache"
import socket
import time

# The Pi's IPv6 route is broken (v6 TLS handshakes hang until timeout) while IPv4 works fine,
# and urllib tries IPv6 first -> weather/wiki fetches "randomly" time out. Force IPv4 everywhere.
_real_getaddrinfo = socket.getaddrinfo
def _v4_getaddrinfo(host, port, family=0, *args, **kw):
    return _real_getaddrinfo(host, port, socket.AF_INET, *args, **kw)
socket.getaddrinfo = _v4_getaddrinfo

WCONF = "/opt/birdthing/weather.json"
PORT = 8090
os.makedirs(CACHE, exist_ok=True)
_imglock = threading.Lock()

# WMO weather code -> (emoji icon, short description)
WMO = {0:("☀️","Clear"),1:("\U0001f324️","Mainly clear"),
 2:("⛅","Partly cloudy"),3:("☁️","Overcast"),
 45:("\U0001f32b️","Fog"),48:("\U0001f32b️","Rime fog"),
 51:("\U0001f326️","Light drizzle"),53:("\U0001f326️","Drizzle"),
 55:("\U0001f326️","Heavy drizzle"),56:("\U0001f327️","Freezing drizzle"),
 57:("\U0001f327️","Freezing drizzle"),61:("\U0001f327️","Light rain"),
 63:("\U0001f327️","Rain"),65:("\U0001f327️","Heavy rain"),
 66:("\U0001f327️","Freezing rain"),67:("\U0001f327️","Freezing rain"),
 71:("❄️","Light snow"),73:("❄️","Snow"),75:("❄️","Heavy snow"),
 77:("❄️","Snow grains"),80:("\U0001f326️","Showers"),
 81:("\U0001f327️","Showers"),82:("\U0001f327️","Heavy showers"),
 85:("\U0001f328️","Snow showers"),86:("\U0001f328️","Snow showers"),
 95:("⛈️","Thunderstorm"),96:("⛈️","Thunderstorm"),
 99:("⛈️","Thunderstorm")}

def load_wconf():
    c = {"lat": 40.7128, "lon": -74.0060, "unit": "C", "place": "New York, NY"}
    try:
        c.update(json.load(open(WCONF)))
    except Exception:
        pass
    return c

def save_wconf(c):
    try:
        json.dump(c, open(WCONF, "w"))
    except Exception:
        pass

_wx_last = None   # last successful weather payload (served during transient API outages)

def weather():
    global _wx_last
    c = load_wconf()
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
               "&current=temperature_2m,weather_code,cloud_cover" % (c["lat"], c["lon"]))
        req = urllib.request.Request(url, headers={"User-Agent": "BirdThing/1.0"})
        cur = json.load(urllib.request.urlopen(req, timeout=8))["current"]
        tc = cur["temperature_2m"]; code = int(cur["weather_code"])
        temp = tc if c["unit"] == "C" else tc * 9 / 5 + 32
        icon, desc = WMO.get(code, ("\U0001f321️", "—"))
        _wx_last = {"temp": round(temp), "unit": c["unit"], "icon": icon,
                    "desc": desc, "place": c["place"],
                    "lat": float(c["lat"]), "lon": float(c["lon"]),
                    "cloud": cur.get("cloud_cover"), "at": time.time()}
        return dict(_wx_last)
    except Exception as e:
        if _wx_last and time.time() - _wx_last.get("at", 0) < 7200:   # serve stale up to 2h
            out = dict(_wx_last); out["stale"] = True; return out
        return {"temp": None, "unit": c["unit"], "icon": "\U0001f321️",
                "desc": "—", "place": c["place"],
                "lat": float(c["lat"]), "lon": float(c["lon"]), "err": str(e)}

def geocode(q):
    try:
        url = ("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=5"
               % urllib.parse.quote(q))
        req = urllib.request.Request(url, headers={"User-Agent": "BirdThing/1.0"})
        res = json.load(urllib.request.urlopen(req, timeout=8)).get("results", [])
        out = []
        for r in res:
            place = r["name"]
            if r.get("admin1"): place += ", " + r["admin1"]
            if r.get("country_code"): place += ", " + r["country_code"]
            out.append({"place": place, "lat": r["latitude"], "lon": r["longitude"]})
        return out
    except Exception:
        return []

def _read_level():
    # Current loudness the mic is hearing (written by the receiver). Used for a real-time
    # "hearing a bird" indicator that reacts to sound, not to the (slower) BirdNET ID.
    try:
        return int(open("/tmp/bt_level").read().strip() or 0)
    except Exception:
        return 0


def tz_off_min():
    # Pi local UTC offset in minutes east of UTC (e.g. EDT = -240). The Car Thing
    # has no RTC/NTP and a wrong clock+TZ, so the dashboard renders time from this.
    is_dst = time.localtime().tm_isdst > 0
    secs_west = time.altzone if is_dst else time.timezone
    return -secs_west // 60


def detections(limit=60):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        cur = con.execute(
            "SELECT Date,Time,Com_Name,Sci_Name,Confidence FROM detections "
            "ORDER BY Date DESC, Time DESC LIMIT ?", (limit,))
        rows = [{"date": r[0], "time": r[1], "com": r[2], "sci": r[3],
                 "conf": round(r[4], 2)} for r in cur.fetchall()]
        today = con.execute(
            "SELECT COUNT(*) , COUNT(DISTINCT Com_Name) FROM detections WHERE Date=?",
            (rows[0]["date"],)).fetchone() if rows else (0, 0)
        con.close()
        return {"rows": rows, "today_count": today[0], "today_species": today[1],
                "now": int(time.time() * 1000), "tzoff": tz_off_min(), "level": _read_level()}
    except Exception as e:
        return {"rows": [], "today_count": 0, "today_species": 0, "err": str(e),
                "now": int(time.time() * 1000), "tzoff": tz_off_min(), "level": _read_level()}

def by_date(days=7):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        cur = con.execute(
            "SELECT Date,Com_Name,Sci_Name,COUNT(*) c FROM detections "
            "GROUP BY Date,Com_Name ORDER BY Date DESC, c DESC")
        out = []
        for date, com, sci, c in cur.fetchall():
            day = next((d for d in out if d["date"] == date), None)
            if not day:
                if len(out) >= days:
                    continue
                day = {"date": date, "birds": []}; out.append(day)
            day["birds"].append({"com": com, "sci": sci, "count": c})
        con.close()
        return out
    except Exception as e:
        return []

def stats():
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        today = con.execute("SELECT MAX(Date) FROM detections").fetchone()[0]
        hourly = [0] * 24
        for hr, c in con.execute(
                "SELECT CAST(substr(Time,1,2) AS INT) h,COUNT(*) FROM detections "
                "WHERE Date=? GROUP BY h", (today,)):
            if 0 <= hr < 24:
                hourly[hr] = c
        top = [{"com": r[0], "count": r[1]} for r in con.execute(
            "SELECT Com_Name,COUNT(*) c FROM detections WHERE Date=? "
            "GROUP BY Com_Name ORDER BY c DESC LIMIT 5", (today,))]
        total = con.execute("SELECT COUNT(*) FROM detections WHERE Date=?", (today,)).fetchone()[0]
        species = con.execute("SELECT COUNT(DISTINCT Com_Name) FROM detections WHERE Date=?", (today,)).fetchone()[0]
        alltime = con.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        con.close()
        return {"hourly": hourly, "top": top, "total": total, "species": species, "alltime": alltime}
    except Exception as e:
        return {"hourly": [0]*24, "top": [], "total": 0, "species": 0, "alltime": 0}

XCKEY_FILE = "/opt/birdthing/xenocanto.key"

def _xckey():
    try:
        return open(XCKEY_FILE).read().strip()
    except Exception:
        return os.environ.get("XC_KEY", "").strip()

def song(name):
    # Find a bird-call recording via the Xeno-canto API v3 (needs a free API key, since Oct 2025).
    # Put the key in /opt/birdthing/xenocanto.key (get it at https://xeno-canto.org/account).
    key = _xckey()
    if not key:
        return {"url": "", "err": "no-key"}
    try:
        url = ("https://xeno-canto.org/api/3/recordings?query=" +
               urllib.parse.quote(name) + "&key=" + urllib.parse.quote(key))
        req = urllib.request.Request(url, headers={"User-Agent": "BirdThing/1.0"})
        recs = json.load(urllib.request.urlopen(req, timeout=8)).get("recordings", [])
        for r in recs:
            f = r.get("file")
            if f:
                if f.startswith("//"):
                    f = "https:" + f
                return {"url": f}
        return {"url": ""}
    except Exception as e:
        return {"url": "", "err": str(e)}


BNCONF = "/home/birdpi/BirdNET-Pi/birdnet.conf"

def _set_conf(kv):
    try:
        lines = open(BNCONF).read().splitlines()
        seen = set()
        for i, l in enumerate(lines):
            for k, v in kv.items():
                if l.startswith(k + "="):
                    lines[i] = "%s=%s" % (k, v); seen.add(k)
        for k, v in kv.items():
            if k not in seen:
                lines.append("%s=%s" % (k, v))
        open(BNCONF, "w").write("\n".join(lines) + "\n")
        subprocess.run(["sudo", "systemctl", "restart", "birdnet_analysis"], capture_output=True)
        return True
    except Exception:
        return False

def birdloc(lat, lon, place):
    try:
        kv = {"LATITUDE": "%.4f" % float(lat), "LONGITUDE": "%.4f" % float(lon)}
    except Exception:
        return {"ok": False, "err": "bad coords"}
    ok = _set_conf(kv)
    if place:
        try:
            json.dump({"place": place}, open(BIRDLOC_FILE, "w"))
        except Exception:
            pass
    return {"ok": ok, "lat": kv["LATITUDE"], "lon": kv["LONGITUDE"], "place": place}

def set_sf(thresh):
    try:
        t = max(0.0, min(0.1, float(thresh)))
    except Exception:
        return {"ok": False}
    return {"ok": _set_conf({"SF_THRESH": "%.3f" % t}), "sf": t}

def geoip():
    try:
        r = json.load(urllib.request.urlopen(
            "http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country", timeout=8))
        if r.get("status") != "success":
            return {"err": "lookup failed"}
        place = ", ".join(x for x in [r.get("city"), r.get("regionName"), r.get("country")] if x)
        return {"lat": r.get("lat"), "lon": r.get("lon"), "place": place}
    except Exception as e:
        return {"err": str(e)}

def birdstatus():
    st = {"lat": None, "lon": None, "sf": None, "place": ""}
    try:
        for line in open(BNCONF):
            if line.startswith("LATITUDE="): st["lat"] = float(line.split("=", 1)[1])
            elif line.startswith("LONGITUDE="): st["lon"] = float(line.split("=", 1)[1])
            elif line.startswith("SF_THRESH="): st["sf"] = float(line.split("=", 1)[1])
    except Exception:
        pass
    try:
        st["place"] = json.load(open(BIRDLOC_FILE)).get("place", "")
    except Exception:
        pass
    return st


# ---- Microphone & bird-ID tuning ------------------------------------------------------------
# gain -> MAX_GAIN and gate -> NOISE_FLOOR live in birdmic_ct.py ON THE CAR THING (reached over the
# USB link); hp -> the receiver's high-pass (systemd env on the Pi); conf -> BirdNET CONFIDENCE.
MICTUNE = "/opt/birdthing/mictune.json"

def _ct_ssh(cmd, timeout=30):
    # same channel the watchdog uses to poke the Car Thing's birdmic service
    return subprocess.run(
        ["sshpass", "-p", "superbird", "ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
         "superbird@192.168.7.2", cmd],
        capture_output=True, timeout=timeout)

def _mictune_load():
    c = {"gain": 120, "gate": 25, "hp": 1, "conf": 0.80}
    try:
        c.update(json.load(open(MICTUNE)))
    except Exception:
        pass
    # keep CONFIDENCE in sync with whatever birdnet.conf actually has (the geo-filter row edits it too)
    try:
        for line in open(BNCONF):
            if line.startswith("CONFIDENCE="):
                c["conf"] = float(line.split("=", 1)[1]); break
    except Exception:
        pass
    return c

def mictune_set(q):
    c = _mictune_load()
    for k, cast, lo, hi in (("gain", int, 20, 300), ("gate", int, 0, 200),
                            ("hp", int, 0, 1), ("conf", float, 0.5, 0.95)):
        if k in q:
            try:
                c[k] = min(hi, max(lo, cast(q[k][0])))
            except Exception:
                pass
    try:
        json.dump(c, open(MICTUNE, "w"))
    except Exception:
        pass
    applied = {"mic": False, "recv": False, "birdnet": False}
    # 1. BirdNET confidence (Pi birdnet.conf; restarts birdnet_analysis)
    try:
        applied["birdnet"] = _set_conf({"CONFIDENCE": "%.2f" % c["conf"]})
    except Exception:
        pass
    # 2. rumble high-pass on the receiver (env override + restart; ~2 s audio gap)
    try:
        hz = "250" if c["hp"] else "0"
        subprocess.run(["sudo", "bash", "-c",
                        "mkdir -p /etc/systemd/system/birdthing-recv.service.d && "
                        "printf '[Service]\\nEnvironment=HP_HZ=%s\\n' > /etc/systemd/system/birdthing-recv.service.d/tune.conf && "
                        "systemctl daemon-reload && systemctl restart birdthing-recv" % hz],
                       timeout=25, capture_output=True)
        applied["recv"] = True
    except Exception:
        pass
    # 3. gain (MAX_GAIN) + gate (NOISE_FLOOR) on the Car Thing mic node, then restart its birdmic
    try:
        remote = ("sudo sed -i 's/^MAX_GAIN = .*/MAX_GAIN = %d/' /opt/birdthing/birdmic_ct.py ; "
                  "sudo sed -i 's/^NOISE_FLOOR = .*/NOISE_FLOOR = %d/' /opt/birdthing/birdmic_ct.py ; "
                  "sudo systemctl restart birdmic" % (int(c["gain"]), int(c["gate"])))
        r = _ct_ssh(remote, timeout=30)
        applied["mic"] = r.returncode == 0
    except Exception:
        pass
    c["applied"] = applied
    return c

BIRDLOC_FILE = "/opt/birdthing/birdloc.json"

def _nm_unesc(s):
    return s.replace("\\:", ":").replace("\\\\", "\\")

def wifi_status():
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if line.startswith("yes:"):
                return {"ssid": _nm_unesc(line[4:])}
        return {"ssid": ""}
    except Exception as e:
        return {"ssid": "", "err": str(e)}

def wifi_scan():
    try:
        # Trigger a rescan, then WAIT for the full multi-channel scan to finish before listing.
        # (--rescan yes / immediate list return only the connected AP because the scan, while
        # associated, hasn't swept all channels yet. rescan + ~5s settle finds every nearby network.)
        subprocess.run(["nmcli", "dev", "wifi", "rescan"], capture_output=True, timeout=15)
        time.sleep(5)
        out = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                             capture_output=True, text=True, timeout=15).stdout
        best = {}
        for line in out.splitlines():
            p = line.replace("\\:", "\x00").split(":")
            if len(p) < 2:
                continue
            ssid = p[0].replace("\x00", ":")
            if not ssid:
                continue
            try:
                sig = int(p[1])
            except Exception:
                sig = 0
            sec = len(p) > 2 and p[2] not in ("", "--")
            if ssid not in best or sig > best[ssid]["signal"]:
                best[ssid] = {"ssid": ssid, "signal": sig, "secure": sec}
        return sorted(best.values(), key=lambda x: -x["signal"])[:30]
    except Exception as e:
        return []

def wifi_connect(ssid, psk):
    # Create the profile EXPLICITLY with the security type set, then bring it up. `nmcli dev wifi
    # connect ... password ...` fails with "key-mgmt property is missing" when a profile for the SSID
    # already exists, so we make our own named profile instead.
    try:
        con = "bt-" + ssid
        subprocess.run(["sudo", "nmcli", "con", "delete", con], capture_output=True)
        if psk:
            add = ["sudo", "nmcli", "con", "add", "type", "wifi", "con-name", con, "ssid", ssid,
                   "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
        else:
            add = ["sudo", "nmcli", "con", "add", "type", "wifi", "con-name", con, "ssid", ssid]
        a = subprocess.run(add, capture_output=True, text=True, timeout=20)
        if a.returncode != 0:
            return {"ok": False, "msg": (a.stderr or a.stdout).strip()[:160]}
        r = subprocess.run(["sudo", "nmcli", "con", "up", con], capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            subprocess.run(["sudo", "nmcli", "con", "delete", con], capture_output=True)
        return {"ok": r.returncode == 0, "msg": (r.stdout or r.stderr).strip()[:160]}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def _day_stats(date, rows):
    # rows: list of (Com_Name, Confidence, Time) for a single Date.
    confs = [r[1] for r in rows]
    sp = {}
    for com, cf, _ in rows:
        sp.setdefault(com, []).append(cf)
    hourly = [0] * 24
    for _, _, t in rows:
        try:
            hourly[int(t[:2])] += 1
        except Exception:
            pass
    hi = sum(1 for c in confs if c >= 0.85)
    bord = sum(1 for c in confs if 0.70 <= c < 0.80)
    top = sorted(([k, len(v), round(sum(v) / len(v), 2)] for k, v in sp.items()),
                 key=lambda x: -x[1])[:8]
    return {"date": date, "detections": len(rows), "species": len(sp),
            "conf_mean": round(sum(confs) / len(confs), 2),
            "high_pct": round(100 * hi / len(confs)),
            "bord_pct": round(100 * bord / len(confs)),
            "hourly": hourly, "peak": hourly.index(max(hourly)), "top": top}

def analytics(days=7):
    # Per-day analytics for the on-screen report: today first, then previous days stacked
    # below. Each day: volume, species, a confidence-based quality proxy (high-confidence vs
    # borderline = likely-false), and the hourly activity chart.
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        dates = [r[0] for r in con.execute(
            "SELECT DISTINCT Date FROM detections ORDER BY Date DESC LIMIT ?", (days,))]
        buckets = {d: [] for d in dates}
        if dates:
            qs = ",".join("?" * len(dates))
            for d, com, cf, t in con.execute(
                    "SELECT Date,Com_Name,Confidence,Time FROM detections WHERE Date IN (%s)" % qs,
                    dates):
                if d in buckets:
                    buckets[d].append((com, cf, t))
        con.close()
        out = [_day_stats(d, buckets[d]) for d in dates if buckets[d]]
        return {"days": out}
    except Exception as e:
        return {"days": [], "err": str(e)}


def play_pi(name):
    # Play the bird's call on the PI's default audio sink (e.g. a paired Bluetooth speaker).
    s = song(name)
    if not s.get("url"):
        return {"ok": False, "err": s.get("err", "no-recording")}
    try:
        subprocess.run(["pkill", "-f", "mpg123"], capture_output=True)
        subprocess.Popen(["mpg123", "-q", s["url"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "err": "mpg123-not-installed"}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def stop_pi():
    try:
        subprocess.run(["pkill", "-f", "mpg123"], capture_output=True)
    except Exception:
        pass
    return {"ok": True}


# ---- Spotify (Web API remote: shows what's playing on the user's phone/Alexa + controls it) ----
# Audio plays on the user's own Spotify Connect device; the display is a remote only.
# Creds in spotify.json next to this file: {"client_id": "...", "refresh_token": "..."}
# (Authorization Code + PKCE — no client secret stored).
SP_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify.json")
_sp = {"access": None, "exp": 0, "playing": False}

def _sp_conf():
    try:
        return json.load(open(SP_CONF))
    except Exception:
        return None

def _sp_token():
    if _sp["access"] and time.time() < _sp["exp"] - 60:
        return _sp["access"]
    c = _sp_conf()
    if not c or not c.get("refresh_token") or not c.get("client_id"):
        return None
    data = urllib.parse.urlencode({"grant_type": "refresh_token",
        "refresh_token": c["refresh_token"], "client_id": c["client_id"]}).encode()
    try:
        req = urllib.request.Request("https://accounts.spotify.com/api/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        tok = json.load(urllib.request.urlopen(req, timeout=8))
    except Exception:
        return None
    _sp["access"] = tok.get("access_token")
    _sp["exp"] = time.time() + tok.get("expires_in", 3600)
    if tok.get("refresh_token") and tok["refresh_token"] != c["refresh_token"]:
        c["refresh_token"] = tok["refresh_token"]      # Spotify rotates refresh tokens under PKCE
        try: json.dump(c, open(SP_CONF, "w"))
        except Exception: pass
    return _sp["access"]

def _sp_api(method, path, timeout=8):
    t = _sp_token()
    if not t:
        return None, 401
    req = urllib.request.Request("https://api.spotify.com/v1" + path, method=method,
        headers={"Authorization": "Bearer " + t})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read()
        return (json.loads(body) if body else None), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0

def spotify_status():
    if not _sp_conf():
        return {"available": False, "err": "not-configured"}
    data, code = _sp_api("GET", "/me/player")
    if code == 401:
        return {"available": False, "err": "auth"}
    if code == 204 or not data:                 # nothing playing / no active device
        return {"available": True, "playing": False}
    item = data.get("item") or {}
    imgs = (item.get("album") or {}).get("images") or []
    dev = data.get("device") or {}
    _sp["playing"] = bool(data.get("is_playing"))
    return {"available": True, "playing": _sp["playing"],
            "title": item.get("name"),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])) or None,
            "album": (item.get("album") or {}).get("name"),
            "art": imgs[0]["url"] if imgs else None,
            "dur_ms": item.get("duration_ms") or 0,
            "pos_ms": data.get("progress_ms") or 0,
            "volume": dev.get("volume_percent"),
            "device": dev.get("name")}

def spotify_cmd(c):
    if c == "playpause":
        c = "pause" if _sp["playing"] else "play"
    routes = {"play": ("PUT", "/me/player/play"), "pause": ("PUT", "/me/player/pause"),
              "next": ("POST", "/me/player/next"), "prev": ("POST", "/me/player/previous"),
              "previous": ("POST", "/me/player/previous")}
    if c not in routes:
        return {"ok": False, "err": "bad-cmd"}
    m, p = routes[c]
    _, code = _sp_api(m, p)
    if c in ("play", "pause"):
        _sp["playing"] = (c == "play")
    return {"ok": code in (200, 202, 204), "code": code}

def spotify_vol(v):
    try: v = max(0, min(100, int(v)))
    except Exception: return {"ok": False, "err": "bad-vol"}
    _, code = _sp_api("PUT", "/me/player/volume?volume_percent=%d" % v)
    return {"ok": code in (200, 202, 204), "volume": v, "code": code}


def fetch_info(name):
    safe = "".join(c for c in name if c.isalnum() or c in " -").strip()
    path = os.path.join(CACHE, safe + ".json")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return json.load(open(path))
        except Exception:
            pass
    try:
        api = ("https://en.wikipedia.org/api/rest_v1/page/summary/" +
               urllib.parse.quote(name.replace(" ", "_")))
        req = urllib.request.Request(api, headers={"User-Agent": "BirdThing/1.0"})
        meta = json.load(urllib.request.urlopen(req, timeout=8))
        out = {"extract": meta.get("extract", ""),
               "title": meta.get("title", name)}
        with open(path, "w") as f:
            json.dump(out, f)
        return out
    except Exception as e:
        return {"extract": "", "title": name, "err": str(e)}

def _is_img(data):
    # a real JPEG or PNG, big enough to be a photo (not an error page / partial download)
    return bool(data) and len(data) > 1024 and (
        data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n")

def _valid_cached(path):
    try:
        if os.path.getsize(path) < 1024:
            return False
        with open(path, "rb") as f:
            return _is_img(f.read(16) + b" " * 1024)  # magic check only
    except Exception:
        return False

def fetch_image(name):
    safe = "".join(c for c in name if c.isalnum() or c in " -").strip()
    path = os.path.join(CACHE, safe + ".jpg")
    if _valid_cached(path):
        return path
    with _imglock:
        if _valid_cached(path):
            return path
        try:
            api = ("https://en.wikipedia.org/api/rest_v1/page/summary/" +
                   urllib.parse.quote(name.replace(" ", "_")))
            req = urllib.request.Request(api, headers={"User-Agent": "BirdThing/1.0"})
            meta = json.load(urllib.request.urlopen(req, timeout=8))
            url = meta.get("thumbnail", {}).get("source") or \
                  meta.get("originalimage", {}).get("source")
            if url:
                req2 = urllib.request.Request(url, headers={"User-Agent": "BirdThing/1.0"})
                data = urllib.request.urlopen(req2, timeout=8).read()
                if _is_img(data):                 # only cache a genuine image -> broken fetches retry
                    with open(path, "wb") as f:
                        f.write(data)
                    return path
        except Exception:
            pass
    return None

# ---- Clap control (double-clap -> Home Assistant toggle) ------------------------------------
# Config lives in /opt/birdthing/clap.json (also read live by clapdetect.py in the receiver);
# clapdetect publishes live tuning stats to /tmp/bt_clap_stat.json. These endpoints let the
# dashboard's Clap page read status, adjust sensitivity/boost/entities, and test-toggle. This is
# entirely separate from BirdNET — nothing here touches bird detection.
CLAPCONF = "/opt/birdthing/clap.json"
CLAPSTAT = "/tmp/bt_clap_stat.json"
_clap_sw = {"t": 0.0, "list": []}

def _clap_load():
    try:
        return json.load(open(CLAPCONF))
    except Exception:
        return {}

def _clap_save(c):
    try:
        json.dump(c, open(CLAPCONF, "w"), indent=2)
        os.chmod(CLAPCONF, 0o600)
        return True
    except Exception:
        return False

def _clap_entities(c, key="entities"):
    e = c.get(key)
    if isinstance(e, list) and e:
        return [x for x in e if x and "." in x]
    if key == "entities" and c.get("entity"):
        return [c["entity"]]
    return []

def _ha_switches(c):
    # togglable HA entities (switch.* / light.* / media_player.* / remote.*), cached ~20 s;
    # _led indicators sorted last. media_player/remote are here so a TV can be a clap target.
    # NOTE for Apple TV: pick the remote.* entity, not media_player.* -- media_player.turn_off
    # is accepted but does nothing on it, while remote.toggle really powers it on/off (tested).
    now = time.time()
    if now - _clap_sw["t"] < 20 and _clap_sw["list"]:
        return _clap_sw["list"]
    out = []
    if c.get("ha_url") and c.get("ha_token"):
        try:
            req = urllib.request.Request(c["ha_url"].rstrip("/") + "/api/states",
                                         headers={"Authorization": "Bearer " + c["ha_token"]})
            for s in json.load(urllib.request.urlopen(req, timeout=6)):
                eid = s["entity_id"]
                if eid.split(".")[0] in ("switch", "light", "media_player", "remote"):
                    out.append({"id": eid,
                                "name": s.get("attributes", {}).get("friendly_name", eid),
                                "state": s.get("state"), "led": eid.endswith("_led")})
            out.sort(key=lambda x: (x["led"], x["id"]))
            _clap_sw.update(t=now, list=out)
        except Exception:
            pass
    return out

_clap_sun = {"t": 0.0, "night": None, "set": None, "rise": None}

def _ha_sun(c):
    # current sun state + next actual sunset/sunrise (next_setting/next_rising = the
    # horizon crossing; NOT next_dusk/next_dawn which are ~25 min later). Cached 60s.
    now = time.time()
    if now - _clap_sun["t"] < 60 and _clap_sun["night"] is not None:
        return _clap_sun
    if c.get("ha_url") and c.get("ha_token"):
        try:
            req = urllib.request.Request(c["ha_url"].rstrip("/") + "/api/states/sun.sun",
                                         headers={"Authorization": "Bearer " + c["ha_token"]})
            s = json.load(urllib.request.urlopen(req, timeout=5))
            a = s.get("attributes", {})
            _clap_sun.update(t=now, night=(s.get("state") == "below_horizon"),
                             set=a.get("next_setting"), rise=a.get("next_rising"))
        except Exception:
            pass
    return _clap_sun

def clap_status():
    c = _clap_load()
    try:
        stat = json.load(open(CLAPSTAT))
    except Exception:
        stat = {}
    sun = _ha_sun(c)
    if stat.get("t"):
        stat["age"] = round(time.time() - stat["t"], 1)   # staleness: >5s = detector not running
    return {"enabled": bool(c.get("enabled")),
            "entities": _clap_entities(c),
            "day_entities": _clap_entities(c, "day_entities"),
            "mode": c.get("mode", "smart"),
            "sensitivity": c.get("sensitivity", 6),
            "boost": c.get("boost", 1.0),
            "dbl_max": c.get("dbl_max", c.get("tune", {}).get("dbl_max", 0.6)),
            "cooldown": c.get("cooldown", c.get("tune", {}).get("cooldown", 1.2)),
            "after_sunset": bool(c.get("after_sunset")),
            "pre_sunset_min": int(c.get("pre_sunset_min", 0) or 0),
            "sun_night": sun.get("night"), "sun_set": sun.get("set"), "sun_rise": sun.get("rise"),
            "has_token": bool(c.get("ha_token")),
            "switches": _ha_switches(c), "stat": stat}

def clap_set(q):
    c = _clap_load()
    def val(k, cast, lo, hi):
        if k in q:
            try:
                return min(hi, max(lo, cast(q[k][0])))
            except Exception:
                return None
        return None
    v = val("enabled", int, 0, 1);        c["enabled"] = bool(v) if v is not None else c.get("enabled", True)
    if "mode" in q and q["mode"][0] in ("smart", "amplitude"):
        c["mode"] = q["mode"][0]
    else:
        c["mode"] = c.get("mode", "smart")
    v = val("sensitivity", int, 1, 10);   c["sensitivity"] = v if v is not None else c.get("sensitivity", 6)
    v = val("boost", float, 1.0, 8.0);    c["boost"] = round(v, 1) if v is not None else c.get("boost", 1.0)
    v = val("dbl_max", float, 0.3, 1.2);  c["dbl_max"] = v if v is not None else c.get("dbl_max", 0.6)
    v = val("cooldown", float, 0.3, 4.0); c["cooldown"] = v if v is not None else c.get("cooldown", 1.2)
    v = val("after_sunset", int, 0, 1);   c["after_sunset"] = bool(v) if v is not None else c.get("after_sunset", False)
    v = val("pre_sunset_min", int, 0, 120); c["pre_sunset_min"] = v if v is not None else c.get("pre_sunset_min", 0)
    if "entities" in q:
        c["entities"] = [e for e in q["entities"][0].split(",") if e and "." in e]
        c.pop("entity", None)
    if "day_entities" in q:      # daytime targets (empty string clears them)
        c["day_entities"] = [e for e in q["day_entities"][0].split(",") if e and "." in e]
    c.pop("tune", None)     # sensitivity/boost supersede the old raw tune block
    _clap_save(c)
    return clap_status()

def clap_test(q=None):
    # ?which=day tests the daytime targets; default tests the evening ones
    c = _clap_load()
    which = (q or {}).get("which", [""])[0]
    ents = _clap_entities(c, "day_entities" if which == "day" else "entities")
    if not ents or not c.get("ha_token"):
        return {"ok": False, "err": "no entity or token"}
    ok = True
    for e in ents:
        try:
            body = json.dumps({"entity_id": e}).encode()
            req = urllib.request.Request(
                c["ha_url"].rstrip("/") + "/api/services/%s/toggle" % e.split(".")[0],
                data=body, method="POST",
                headers={"Authorization": "Bearer " + c["ha_token"],
                         "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            ok = False
    return {"ok": ok, "toggled": ents}


# ---- Room presence sensitivity (Settings sliders -> Home Assistant helpers) -----------------
# The two sliders ARE Home Assistant helpers (input_number.bedroom_sensitivity /
# living_room_sensitivity, 1-10). HA automations turn the level into the ESP32 motion threshold
# / living-room zone threshold, so this is only a thin read/write proxy: the HA app slider and
# the BirdThing page are always the same value. Only these two fixed entities can be written,
# and only integer levels 1-10 -- nothing from the query string selects an entity.
_PRESENS = {"bedroom": "input_number.bedroom_sensitivity",
            "living_room": "input_number.living_room_sensitivity"}

def _ha_json(c, path, payload=None, timeout=6):
    hdr = {"Authorization": "Bearer " + c["ha_token"]}
    url = c["ha_url"].rstrip("/") + path
    if payload is None:
        req = urllib.request.Request(url, headers=hdr)
    else:
        hdr["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=hdr)
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def presens_status():
    c = _clap_load()
    if not (c.get("ha_url") and c.get("ha_token")):
        return {"ok": False, "err": "no Home Assistant token"}
    try:
        st = lambda e: _ha_json(c, "/api/states/" + e)
        return {"ok": True,
                "bedroom": int(float(st(_PRESENS["bedroom"])["state"])),
                "living_room": int(float(st(_PRESENS["living_room"])["state"])),
                "bedroom_threshold": st("number.espectre_bedroom_threshold")["state"],
                "living_room_threshold": st("input_number.living_room_zone_min")["state"]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:120]}

def presens_set(q):
    c = _clap_load()
    if not (c.get("ha_url") and c.get("ha_token")):
        return {"ok": False, "err": "no Home Assistant token"}
    for key, ent in _PRESENS.items():
        if key not in q:
            continue
        try:
            v = min(10, max(1, int(round(float(q[key][0])))))
        except Exception:
            continue
        try:
            _ha_json(c, "/api/services/input_number/set_value", {"entity_id": ent, "value": v})
        except Exception as e:
            return {"ok": False, "err": str(e)[:120]}
    time.sleep(1.2)   # give the HA automations a moment to push the threshold so the read-back shows it
    return presens_status()


# --- ANCS iPhone notifications ---
# Same-origin proxy for the ANCS gateway (BirdThing Pi :8099) so the Car
# Thing's browser can read iPhone notifications; it has no route to that host
# or port itself. Short cache so a 2.5s UI poll can't stampede the gateway.
_ancs_cache = {"t": 0.0, "d": {"ok": False, "linked": False, "items": []}}
ANCS_URL = "http://127.0.0.1:8099/api/notifications"
ANCS_SET_URL = "http://127.0.0.1:8099/api/settings"
ANCS_DISMISS_URL = "http://127.0.0.1:8099/api/dismiss"


def notify_clear(qs=""):
    """Write-through 'Clear' from this screen's notification centre - see
    Store.dismiss_from_display() on the gateway. qs empty = clear everything
    currently shown; qs='uid=<n>' clears just that one row."""
    url = ANCS_DISMISS_URL + ("?" + qs if qs else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        _ancs_cache["t"] = 0.0      # force the next poll to see the clear
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def notify_set(qs=""):
    """Write-through to the gateway's central display settings.

    The dashboard runs on the Car Thing, which can only reach this Pi's :8090 -
    not the gateway's :8099 - so the settings write has to be proxied the same
    way the notification feed already is. Settings live in the gateway because
    ALL THREE screens poll it; putting them anywhere else would mean the
    BirdThing settings screen could not control the other two.
    """
    url = ANCS_SET_URL + ("/set?" + qs if qs else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        _ancs_cache["t"] = 0.0      # force the next poll to see the new settings
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def notify():
    now = time.time()
    if now - _ancs_cache["t"] < 1.0:
        return _ancs_cache["d"]
    try:
        with urllib.request.urlopen(ANCS_URL, timeout=3) as r:
            _ancs_cache["d"] = json.loads(r.read().decode())
    except Exception as e:
        _ancs_cache["d"] = {"ok": False, "linked": False, "items": [],
                            "error": str(e)[:120]}
    _ancs_cache["t"] = now
    return _ancs_cache["d"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            try:
                with open(HTML, "rb") as f: body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
        elif self.path.startswith("/api/notifyset"):
            # ?hold=15&show_body=0&dev_kiosk=0 ... ; no query = just read back
            q = self.path.split("?", 1)
            self._send(200, "application/json", json.dumps(notify_set(q[1] if len(q) > 1 else "")).encode())
        elif self.path.startswith("/api/notifyclear"):
            # ?uid=<n> clears one row; no query clears everything shown
            q = self.path.split("?", 1)
            self._send(200, "application/json", json.dumps(notify_clear(q[1] if len(q) > 1 else "")).encode())
        elif self.path.startswith("/api/notify"):
            self._send(200, "application/json", json.dumps(notify()).encode())
        elif self.path.startswith("/api/detections"):
            self._send(200, "application/json", json.dumps(detections()).encode())
        elif self.path.startswith("/api/bydate"):
            self._send(200, "application/json", json.dumps(by_date()).encode())
        elif self.path.startswith("/api/stats"):
            self._send(200, "application/json", json.dumps(stats()).encode())
        elif self.path.startswith("/api/song"):
            q = urllib.parse.urlparse(self.path).query
            name = urllib.parse.parse_qs(q).get("name", [""])[0]
            self._send(200, "application/json", json.dumps(song(name)).encode())
        elif self.path.startswith("/api/geoip"):
            self._send(200, "application/json", json.dumps(geoip()).encode())
        elif self.path.startswith("/api/birdstatus"):
            self._send(200, "application/json", json.dumps(birdstatus()).encode())
        elif self.path.startswith("/api/birdloc"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(birdloc(
                q.get("lat", ["0"])[0], q.get("lon", ["0"])[0], q.get("place", [""])[0])).encode())
        elif self.path.startswith("/api/sf"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(set_sf(q.get("t", ["0.03"])[0])).encode())
        elif self.path.startswith("/api/presens/set"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(presens_set(q)).encode())
        elif self.path.startswith("/api/presens"):
            self._send(200, "application/json", json.dumps(presens_status()).encode())
        elif self.path.startswith("/api/clap/set"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(clap_set(q)).encode())
        elif self.path.startswith("/api/clap/test"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(clap_test(q)).encode())
        elif self.path.startswith("/api/clap"):
            self._send(200, "application/json", json.dumps(clap_status()).encode())
        elif self.path.startswith("/api/mictune/set"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(mictune_set(q)).encode())
        elif self.path.startswith("/api/mictune"):
            self._send(200, "application/json", json.dumps(_mictune_load()).encode())
        elif self.path.startswith("/api/wifi/scan"):
            self._send(200, "application/json", json.dumps(wifi_scan()).encode())
        elif self.path.startswith("/api/wifi/connect"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(
                wifi_connect(q.get("ssid", [""])[0], q.get("psk", [""])[0])).encode())
        elif self.path.startswith("/api/wifi"):
            self._send(200, "application/json", json.dumps(wifi_status()).encode())
        elif self.path.startswith("/api/analytics"):
            self._send(200, "application/json", json.dumps(analytics()).encode())
        elif self.path.startswith("/api/play_pi"):
            q = urllib.parse.urlparse(self.path).query
            name = urllib.parse.parse_qs(q).get("name", [""])[0]
            self._send(200, "application/json", json.dumps(play_pi(name)).encode())
        elif self.path.startswith("/api/stop_pi"):
            self._send(200, "application/json", json.dumps(stop_pi()).encode())
        elif self.path.startswith("/api/spotify/cmd"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(spotify_cmd(q.get("c", [""])[0])).encode())
        elif self.path.startswith("/api/spotify/vol"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json", json.dumps(spotify_vol(q.get("v", ["50"])[0])).encode())
        elif self.path.startswith("/api/spotify"):
            self._send(200, "application/json", json.dumps(spotify_status()).encode())
        elif self.path.startswith("/api/info"):
            q = urllib.parse.urlparse(self.path).query
            name = urllib.parse.parse_qs(q).get("name", [""])[0]
            self._send(200, "application/json", json.dumps(fetch_info(name)).encode())
        elif self.path.startswith("/api/weather/unit"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            c = load_wconf(); c["unit"] = "F" if q.get("u", ["C"])[0].upper() == "F" else "C"
            save_wconf(c)
            self._send(200, "application/json", json.dumps(weather()).encode())
        elif self.path.startswith("/api/weather/loc"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            c = load_wconf()
            try:
                c["lat"] = float(q["lat"][0]); c["lon"] = float(q["lon"][0])
                c["place"] = q.get("place", [c["place"]])[0]; save_wconf(c)
            except Exception:
                pass
            self._send(200, "application/json", json.dumps(weather()).encode())
        elif self.path.startswith("/api/weather"):
            self._send(200, "application/json", json.dumps(weather()).encode())
        elif self.path.startswith("/api/geocode"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(200, "application/json",
                       json.dumps(geocode(q.get("q", [""])[0])).encode())
        elif self.path.startswith("/assets/"):
            fn = os.path.basename(urllib.parse.urlparse(self.path).path)
            fp = os.path.join("/opt/birdthing/assets", fn)
            if os.path.exists(fp) and "/" not in fn.replace("..", ""):
                ct = "font/woff2" if fn.endswith(".woff2") else "application/octet-stream"
                with open(fp, "rb") as f:
                    self.send_response(200); self.send_header("Content-Type", ct)
                    self.send_header("Cache-Control", "max-age=86400")
                    body = f.read(); self.send_header("Content-Length", str(len(body)))
                    self.end_headers(); self.wfile.write(body)
            else:
                self._send(404, "text/plain", b"no asset")
        elif self.path.startswith("/api/image"):
            q = urllib.parse.urlparse(self.path).query
            name = urllib.parse.parse_qs(q).get("name", [""])[0]
            p = fetch_image(name) if name else None
            if p:
                with open(p, "rb") as f: self._send(200, "image/jpeg", f.read())
            else:
                self._send(404, "text/plain", b"no image")
        else:
            self._send(404, "text/plain", b"not found")

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
