#!/usr/bin/env python3
# BirdThing knob bridge: the Car Thing rotary (/dev/input/event1) has no kbd handler, so
# Chromium never sees it. This reads the rotary and dispatches Arrow keys to the page via the
# DevTools protocol (port 9222), so the dashboard's keyboard handler scrolls the bird list.
import socket, base64, json, os, struct, time, urllib.request, threading

DEV_KNOB = "/dev/input/event1"
DEV_BUTTONS = "/dev/input/event0"
DISPLAY_FLAG = "/tmp/display_off"
BTN_M = 50                        # 'm' top button keycode -> toggle display
HOST, PORT = "127.0.0.1", 9222
EVENT_FORMAT = "llHHI"            # sec, usec, type, code, value
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

def control_server():
    # Tiny HTTP control the dashboard (running in the CT's own Chromium) calls for brightness.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import urllib.parse as up
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            q = up.parse_qs(up.urlparse(self.path).query)
            if self.path.startswith("/bright"):
                lvl = q.get("level", ["70"])[0]
                if lvl.isdigit():
                    open("/tmp/bt_bright", "w").write(str(max(0, min(100, int(lvl)))))
            elif self.path.startswith("/display"):
                on = q.get("on", ["1"])[0]
                if on == "0": open("/tmp/display_off", "w").close()
                else:
                    try: os.remove("/tmp/display_off")
                    except OSError: pass
            elif self.path.startswith("/reboot"):
                os.system("sudo systemctl reboot")
            elif self.path.startswith("/poweroff"):
                os.system("sudo systemctl poweroff")
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers(); self.wfile.write(b"ok")
        def do_OPTIONS(self):                       # Chromium Private Network Access preflight
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
    try:
        HTTPServer(("127.0.0.1", 8091), H).serve_forever()
    except Exception:
        pass

def button_watch():
    # The 'm' (last top) button toggles the screen on/off via the flag the backlight loop reads.
    while True:
        try:
            with open(DEV_BUTTONS, "rb") as f:
                while True:
                    d = f.read(EVENT_SIZE)
                    if not d or len(d) < EVENT_SIZE:
                        continue
                    _, _, etype, code, value = struct.unpack(EVENT_FORMAT, d)
                    # 'm' is now owned by the dashboard JS (short=screen off/wake,
                    # long=power menu via the /display, /reboot, /poweroff endpoints).
                    _ = (etype, code, value)
        except Exception:
            time.sleep(2)

threading.Thread(target=button_watch, daemon=True).start()
threading.Thread(target=control_server, daemon=True).start()

class DevTools:
    def __init__(self):
        self.s = None
    def connect(self):
        pages = json.load(urllib.request.urlopen("http://%s:%d/json" % (HOST, PORT), timeout=5))
        page = [p for p in pages if p.get("type") == "page"][0]
        path = page["webSocketDebuggerUrl"].split("%d" % PORT, 1)[1]
        s = socket.create_connection((HOST, PORT), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        s.send(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                % (path, HOST, PORT, key)).encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        self.s = s; self._id = 0
    def _frame(self, data):
        data = data.encode(); hdr = bytearray([0x81]); ln = len(data); mask = os.urandom(4)
        if ln < 126: hdr.append(0x80 | ln)
        elif ln < 65536: hdr.append(0x80 | 126); hdr += struct.pack(">H", ln)
        else: hdr.append(0x80 | 127); hdr += struct.pack(">Q", ln)
        hdr += mask
        self.s.send(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    def key(self, keyname, vk):
        self._id += 1
        for typ in ("keyDown", "keyUp"):
            self._frame(json.dumps({"id": self._id, "method": "Input.dispatchKeyEvent",
                "params": {"type": typ, "key": keyname, "code": keyname,
                           "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}}))

dt = DevTools()
def ensure():
    while True:
        try:
            dt.connect(); return
        except Exception:
            time.sleep(3)

ensure()
while True:
    try:
        with open(DEV_KNOB, "rb") as f:
            while True:
                data = f.read(EVENT_SIZE)
                if not data or len(data) < EVENT_SIZE:
                    continue
                _, _, etype, code, value = struct.unpack(EVENT_FORMAT, data)
                if code != 6:           # rotary turn events use code 6
                    continue
                try:
                    if value == 1:
                        dt.key("ArrowDown", 40)
                    elif value != 0:    # wrapped -1 == turn left
                        dt.key("ArrowUp", 38)
                except Exception:
                    ensure()
    except Exception:
        time.sleep(2)
