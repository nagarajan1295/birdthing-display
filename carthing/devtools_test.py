#!/usr/bin/env python3
# Dispatch ArrowDown twice, then Enter, then screenshot -> verify list nav + facts panel.
import socket, base64, json, os, struct, time, urllib.request, sys
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
while b"\r\n\r\n" not in resp: resp += s.recv(4096)
_id = [0]
def frame(obj):
    data = json.dumps(obj).encode(); hdr = bytearray([0x81]); ln = len(data); mask = os.urandom(4)
    if ln < 126: hdr.append(0x80 | ln)
    elif ln < 65536: hdr.append(0x80 | 126); hdr += struct.pack(">H", ln)
    else: hdr.append(0x80 | 127); hdr += struct.pack(">Q", ln)
    hdr += mask
    s.send(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
def recvall(n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b)); b += c
    return b
def recv_msg():
    b0, b1 = recvall(2); ln = b1 & 0x7f
    if ln == 126: ln = struct.unpack(">H", recvall(2))[0]
    elif ln == 127: ln = struct.unpack(">Q", recvall(8))[0]
    return recvall(ln)
def keypress(name, vk):
    for typ in ("keyDown", "keyUp"):
        _id[0] += 1
        frame({"id": _id[0], "method": "Input.dispatchKeyEvent",
               "params": {"type": typ, "key": name, "code": name,
                          "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}})
        time.sleep(0.05)

action = sys.argv[1] if len(sys.argv) > 1 else "nav"
if action == "nav":
    keypress("ArrowDown", 40); time.sleep(0.2); keypress("ArrowDown", 40); time.sleep(0.3)
elif action == "facts":
    keypress("Enter", 13); time.sleep(1.5)   # open facts (waits for wiki fetch)
time.sleep(0.4)
_id[0] += 1
frame({"id": _id[0], "method": "Page.captureScreenshot", "params": {"format": "png"}})
for _ in range(60):
    m = json.loads(recv_msg())
    if m.get("id") == _id[0]:
        open("/tmp/shot.png", "wb").write(base64.b64decode(m["result"]["data"])); break
print("ok")
