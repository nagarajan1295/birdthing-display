#!/usr/bin/env python3
# Evaluate a JS expression on the page (argv[1]) then screenshot to /tmp/shot.png.
import socket, base64, json, os, struct, time, urllib.request, sys
HOST, PORT = "127.0.0.1", 9222
pages = json.load(urllib.request.urlopen("http://%s:%d/json" % (HOST, PORT)))
page = [p for p in pages if p.get("type") == "page"][0]
path = page["webSocketDebuggerUrl"].split("%d" % PORT, 1)[1]
s = socket.create_connection((HOST, PORT))
k = base64.b64encode(os.urandom(16)).decode()
s.send(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, HOST, PORT, k)).encode())
r = b""
while b"\r\n\r\n" not in r: r += s.recv(4096)
_id = [0]
def frame(o):
    d = json.dumps(o).encode(); h = bytearray([0x81]); n = len(d); m = os.urandom(4)
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    h += m; s.send(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(d)))
def rd(n):
    b = b""
    while len(b) < n: b += s.recv(n - len(b))
    return b
def msg():
    b0, b1 = rd(2); n = b1 & 0x7f
    if n == 126: n = struct.unpack(">H", rd(2))[0]
    elif n == 127: n = struct.unpack(">Q", rd(8))[0]
    return rd(n)
_id[0] += 1
frame({"id": _id[0], "method": "Runtime.evaluate", "params": {"expression": sys.argv[1]}})
time.sleep(1.2)
_id[0] += 1
frame({"id": _id[0], "method": "Page.captureScreenshot", "params": {"format": "png"}})
for _ in range(80):
    m = json.loads(msg())
    if m.get("id") == _id[0]:
        open("/tmp/shot.png", "wb").write(base64.b64decode(m["result"]["data"])); break
print("ok")
