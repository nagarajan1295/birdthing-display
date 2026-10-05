#!/usr/bin/env python3
# Capture a screenshot of the Chromium page via the DevTools protocol (port 9222).
# Pure stdlib WebSocket client. Saves PNG to /tmp/shot.png on the Car Thing.
import socket, base64, json, os, struct, urllib.request

HOST, PORT = "127.0.0.1", 9222

pages = json.load(urllib.request.urlopen("http://%s:%d/json" % (HOST, PORT)))
page = [p for p in pages if p.get("type") == "page"][0]
path = page["webSocketDebuggerUrl"].split("%d" % PORT, 1)[1]

s = socket.create_connection((HOST, PORT))
key = base64.b64encode(os.urandom(16)).decode()
s.send(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
        % (path, HOST, PORT, key)).encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += s.recv(4096)

def send(obj):
    data = json.dumps(obj).encode()
    hdr = bytearray([0x81])
    ln = len(data); mask = os.urandom(4)
    if ln < 126: hdr.append(0x80 | ln)
    elif ln < 65536: hdr.append(0x80 | 126); hdr += struct.pack(">H", ln)
    else: hdr.append(0x80 | 127); hdr += struct.pack(">Q", ln)
    hdr += mask
    s.send(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def recvall(n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c: raise IOError("closed")
        buf += c
    return buf

def recv_msg():
    b0, b1 = recvall(2)
    ln = b1 & 0x7f
    if ln == 126: ln = struct.unpack(">H", recvall(2))[0]
    elif ln == 127: ln = struct.unpack(">Q", recvall(8))[0]
    return recvall(ln)

send({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}})
data = None
for _ in range(50):
    msg = json.loads(recv_msg())
    if msg.get("id") == 1:
        data = msg["result"]["data"]; break
with open("/tmp/shot.png", "wb") as f:
    f.write(base64.b64decode(data))
print("saved /tmp/shot.png", os.path.getsize("/tmp/shot.png"), "bytes")
