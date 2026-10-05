#!/usr/bin/env python3
# Optional helper: add a "Clap Lights" page to Home Assistant that embeds the
# BirdThing clap-control page (Settings -> Clap) as a full-screen iframe, so you can
# tune claps from the HA app/sidebar. Run it ON the BirdThing Pi (it reuses the HA
# long-lived token already in /opt/birdthing/clap.json). Creates a storage dashboard
# via the HA WebSocket API -- no HA restart, no configuration.yaml edit.
#
#   Edit HA_HOST and IFRAME_URL below, then:  python3 ha_clap_dashboard.py
import json, os, socket, base64, struct

HA_HOST = "YOUR_HA_IP"          # the LAN IP of the machine running Home Assistant
HA_PORT = 8123
URL_PATH = "clap-lights"        # sidebar path (lowercase, must contain a hyphen)
IFRAME_URL = "http://YOUR_BIRDTHING_PI_IP:8090/#clap"   # the BirdThing dashboard, deep-linked to the Clap page
TOKEN = json.load(open("/opt/birdthing/clap.json"))["ha_token"]

def ws_connect():
    s = socket.create_connection((HA_HOST, HA_PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /api/websocket HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
               % (HA_HOST, HA_PORT, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(1024)
    if b"101" not in buf.split(b"\r\n")[0]:
        raise RuntimeError("ws upgrade failed")
    return s

def ws_send(s, obj):
    p = json.dumps(obj).encode(); mask = os.urandom(4); n = len(p)
    hdr = b"\x81"
    if n < 126: hdr += struct.pack("!B", 0x80 | n)
    elif n < 65536: hdr += struct.pack("!B", 0x80 | 126) + struct.pack("!H", n)
    else: hdr += struct.pack("!B", 0x80 | 127) + struct.pack("!Q", n)
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(p)))

def ws_recv(s):
    def rd(n):
        d = b""
        while len(d) < n:
            c = s.recv(n - len(d))
            if not c: raise RuntimeError("closed")
            d += c
        return d
    b0, b1 = rd(2); ln = b1 & 0x7f
    if ln == 126: ln = struct.unpack("!H", rd(2))[0]
    elif ln == 127: ln = struct.unpack("!Q", rd(8))[0]
    return json.loads(rd(ln).decode())

def cmd(s, mid, obj):
    ws_send(s, dict(obj, id=mid))
    while True:
        m = ws_recv(s)
        if m.get("id") == mid and m.get("type") == "result":
            return m

ws = ws_connect()
ws_recv(ws)                                        # auth_required
ws_send(ws, {"type": "auth", "access_token": TOKEN})
if ws_recv(ws).get("type") != "auth_ok":
    raise SystemExit("HA auth failed")
print("auth ok")

exists = any(d.get("url_path") == URL_PATH
             for d in (cmd(ws, 1, {"type": "lovelace/dashboards/list"}).get("result") or []))
if not exists:
    print("create:", cmd(ws, 2, {"type": "lovelace/dashboards/create", "url_path": URL_PATH,
        "title": "Clap Lights", "icon": "mdi:gesture-double-tap",
        "show_in_sidebar": True, "require_admin": False, "mode": "storage"}).get("success"))
config = {"title": "Clap Lights", "views": [{"title": "Clap Lights", "path": "clap", "panel": True,
          "cards": [{"type": "iframe", "url": IFRAME_URL, "aspect_ratio": "100%"}]}]}
print("config saved:", cmd(ws, 3, {"type": "lovelace/config/save",
      "url_path": URL_PATH, "config": config}).get("success"))
print("done -> HA sidebar 'Clap Lights'")
