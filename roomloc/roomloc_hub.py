#!/usr/bin/env python3
"""roomloc hub — collects RSSI vectors from the nodes and decides which room
the phone is in, by fingerprint matching rather than absolute distance.

Fingerprinting is what makes this work with no anchor in the target rooms: we
never ask "how far is the phone from the kitchen", only "does the current
(livingroom, bedroom, ...) RSSI vector look more like the one recorded while
standing in the kitchen, or the one recorded in the bathroom".

Exposes:
  POST /report        node -> hub RSSI reports
  GET  /state         current vector + decided room  (Home Assistant polls this)
  GET  /              calibration UI (open it on the phone, walk, tap)
  POST /calibrate     {room, seconds} capture a fingerprint
  GET  /fingerprints  stored fingerprints
  POST /forget        {room} drop a room's fingerprints

Stock deps only: python3 stdlib.
"""
import argparse
import collections
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ABSENT = -105.0  # RSSI stand-in for "this node cannot hear the phone"

STATE_LOCK = threading.Lock()
NODES = {}          # node -> {rssi, age, ts}
FINGERPRINTS = {}   # room -> [vector, ...]
DECISION = {"room": None, "confidence": 0.0, "changed_at": 0.0, "distances": {}}
VOTES = collections.deque(maxlen=300)  # (timestamp, room|None), one per second
CFG = {}


# --------------------------------------------------------------------------- io
def load_fingerprints(path):
    global FINGERPRINTS
    if os.path.exists(path):
        with open(path) as f:
            FINGERPRINTS = json.load(f)


def save_fingerprints(path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(FINGERPRINTS, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------- matching
def current_vector(node_names):
    now = time.time()
    vec = {}
    for n in node_names:
        d = NODES.get(n)
        if d and d["rssi"] is not None and (now - d["ts"]) < CFG["stale_after"]:
            vec[n] = float(d["rssi"])
        else:
            vec[n] = ABSENT
    return vec


def distance(vec, fp, node_names):
    return sum((vec[n] - fp.get(n, ABSENT)) ** 2 for n in node_names) ** 0.5


def decide(node_names):
    """Nearest-fingerprint room, committed by rolling vote.

    An earlier version used a dwell timer that reset whenever the instantaneous
    winner changed. RSSI is noisy enough that the winner flips constantly, so the
    timer never accumulated and the decision froze on whatever it had first --
    it sat on "bathroom" for three minutes while every reading said kitchen.
    A vote over the last `dwell` seconds tolerates that flapping instead.
    """
    vec = current_vector(node_names)
    now = time.time()
    if all(v == ABSENT for v in vec.values()):
        VOTES.append((now, None))
        return vec, DECISION["room"], {}, 0.0

    scored = []
    for room, samples in FINGERPRINTS.items():
        if not samples:
            continue
        scored.append((min(distance(vec, s, node_names) for s in samples), room))
    if not scored:
        return vec, None, {}, 0.0
    scored.sort()
    dists = {r: round(d, 1) for d, r in scored}

    best_d, best_room = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else best_d + 999
    # Only vote when this reading actually prefers one room; an ambiguous
    # reading abstains rather than dragging the tally around.
    VOTES.append((now, best_room if (runner_up - best_d) >= CFG["margin"] else None))

    window = [r for ts, r in VOTES if now - ts <= CFG["dwell"]]
    cast = [r for r in window if r]
    confidence = 0.0
    if cast and len(window) >= CFG["dwell"] * 0.5:
        winner = max(set(cast), key=cast.count)
        share = cast.count(winner) / len(window)
        confidence = round(share, 2)
        if share >= CFG["vote_share"] and winner != DECISION["room"]:
            DECISION["room"] = winner
            DECISION["changed_at"] = now
    return vec, DECISION["room"], dists, confidence


def fingerprint_quality(node_names):
    """Flag captures that sit closer to another room's centre than their own.

    One mislabelled capture is enough to break everything: rooms are scored by
    their *nearest* fingerprint, so a stray print parked in the kitchen's signal
    space will claim every kitchen reading for the bathroom.
    """
    centroids = {}
    for room, samples in FINGERPRINTS.items():
        if samples:
            centroids[room] = {n: sum(s.get(n, ABSENT) for s in samples) / len(samples)
                               for n in node_names}
    report = {"rooms": {}, "separation": {}, "misfits": []}
    for room, samples in FINGERPRINTS.items():
        entries = []
        for i, s in enumerate(samples):
            own = distance(s, centroids[room], node_names)
            others = {r: distance(s, c, node_names)
                      for r, c in centroids.items() if r != room}
            nearest = min(others, key=others.get) if others else None
            bad = nearest is not None and others[nearest] < own
            entries.append({"index": i, "fingerprint": s, "to_own_centre": round(own, 1),
                            "nearest_other": nearest,
                            "to_nearest_other": round(others[nearest], 1) if nearest else None,
                            "misfit": bad})
            if bad:
                report["misfits"].append({"room": room, "index": i, "fingerprint": s,
                                          "closer_to": nearest})
        report["rooms"][room] = {"count": len(samples),
                                 "centroid": {k: round(v, 1) for k, v in centroids[room].items()},
                                 "captures": entries}
    rooms = sorted(centroids)
    for i, a in enumerate(rooms):
        for b in rooms[i + 1:]:
            report["separation"][f"{a} vs {b}"] = round(
                distance(centroids[a], centroids[b], node_names), 1)
    return report


def decision_loop(node_names):
    while True:
        with STATE_LOCK:
            vec, room, dists, conf = decide(node_names)
            DECISION["distances"] = dists
            DECISION["confidence"] = round(conf, 2)
            DECISION["vector"] = vec
        time.sleep(1.0)


# -------------------------------------------------------------------------- http
PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>roomloc calibration</title>
<style>body{font:16px system-ui;margin:0;padding:16px;background:#111;color:#eee}
h1{font-size:19px;margin:0 0 4px}p.sub{color:#888;margin:0 0 16px;font-size:13px}
table{width:100%;border-collapse:collapse;margin-bottom:16px}
td{padding:6px 4px;border-bottom:1px solid #262626;font-variant-numeric:tabular-nums}
td.v{text-align:right;color:#7dd3fc}
button{font:600 16px system-ui;padding:14px;width:100%;margin-bottom:8px;border:0;
border-radius:10px;background:#1f6feb;color:#fff}button.g{background:#262626;color:#bbb}
#room{font-size:26px;font-weight:700;margin:8px 0}#msg{color:#4ade80;min-height:20px;font-size:14px}
input{font:16px system-ui;padding:12px;width:100%;box-sizing:border-box;margin-bottom:8px;
border-radius:10px;border:1px solid #333;background:#1a1a1a;color:#eee}</style>
<h1>roomloc</h1><p class=sub>Stand still in a room, name it, tap Capture. Do each room 2-3 times.</p>
<div id=room>-</div><table id=t></table>
<input id=name placeholder="room name e.g. kitchen">
<button onclick=cap()>Capture 12s fingerprint</button>
<button class=g onclick=forget()>Forget this room</button>
<div id=msg></div>
<script>
async function tick(){
 const s=await (await fetch('/state')).json();
 document.getElementById('room').textContent=(s.room||'unknown')+' ('+s.confidence+')';
 document.getElementById('t').innerHTML=Object.entries(s.vector||{}).map(([k,v])=>
  `<tr><td>${k}</td><td class=v>${v<=-105?'--':v.toFixed(1)}</td></tr>`).join('')
  +Object.entries(s.distances||{}).map(([k,v])=>
  `<tr><td style=color:#777>fit: ${k}</td><td class=v style=color:#777>${v}</td></tr>`).join('');
}
async function cap(){const r=document.getElementById('name').value.trim();if(!r)return;
 document.getElementById('msg').textContent='capturing '+r+'... hold still';
 const x=await (await fetch('/calibrate',{method:'POST',body:JSON.stringify({room:r,seconds:12})})).json();
 document.getElementById('msg').textContent=x.ok?('saved '+r+' ('+x.count+' fingerprints)'):('error: '+x.error);}
async function forget(){const r=document.getElementById('name').value.trim();if(!r)return;
 await fetch('/forget',{method:'POST',body:JSON.stringify({room:r})});
 document.getElementById('msg').textContent='forgot '+r;}
setInterval(tick,1000);tick();
</script>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/state":
            with STATE_LOCK:
                out = {
                    "room": DECISION["room"],
                    "confidence": DECISION["confidence"],
                    "vector": DECISION.get("vector", {}),
                    "distances": DECISION["distances"],
                    "changed_at": DECISION["changed_at"],
                    "seconds_in_room": round(time.time() - DECISION["changed_at"], 1)
                    if DECISION["changed_at"] else None,
                    "nodes": {k: {"rssi": v["rssi"], "src": v.get("src"),
                                  "age": round(time.time() - v["ts"], 1)}
                              for k, v in NODES.items()},
                    "rooms_known": sorted(FINGERPRINTS),
                }
            return self._send(200, json.dumps(out))
        if self.path == "/fingerprints":
            return self._send(200, json.dumps(FINGERPRINTS, indent=1))
        if self.path == "/quality":
            with STATE_LOCK:
                return self._send(200, json.dumps(fingerprint_quality(CFG["nodes"]), indent=1))
        self._send(404, "{}")

    def do_POST(self):
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._send(400, json.dumps({"error": "bad json"}))

        if self.path == "/report":
            node = body.get("node")
            if not node:
                return self._send(400, json.dumps({"error": "no node"}))
            with STATE_LOCK:
                NODES[node] = {"rssi": body.get("rssi"), "age": body.get("age"),
                               "src": body.get("src"), "ts": time.time()}
            return self._send(200, json.dumps({"ok": True}))

        if self.path == "/calibrate":
            room = (body.get("room") or "").strip().lower()
            secs = float(body.get("seconds") or 12)
            if not room:
                return self._send(400, json.dumps({"ok": False, "error": "no room"}))
            samples = []
            end = time.time() + secs
            while time.time() < end:
                with STATE_LOCK:
                    v = current_vector(CFG["nodes"])
                if not all(x == ABSENT for x in v.values()):
                    samples.append(v)
                time.sleep(1.5)
            if not samples:
                return self._send(200, json.dumps({"ok": False, "error": "phone not seen"}))
            # Average the run into one fingerprint; append so a room can hold
            # several (near the door, at the sink, ...) without them blurring.
            avg = {n: round(sum(s[n] for s in samples) / len(samples), 1)
                   for n in CFG["nodes"]}
            with STATE_LOCK:
                FINGERPRINTS.setdefault(room, []).append(avg)
                save_fingerprints(CFG["store"])
                count = len(FINGERPRINTS[room])
            return self._send(200, json.dumps({"ok": True, "room": room,
                                               "fingerprint": avg, "count": count}))

        if self.path == "/prune":
            # Drop captures that sit in another room's signal space.
            with STATE_LOCK:
                bad = fingerprint_quality(CFG["nodes"])["misfits"]
                for m in sorted(bad, key=lambda m: -m["index"]):
                    del FINGERPRINTS[m["room"]][m["index"]]
                save_fingerprints(CFG["store"])
                VOTES.clear()
                DECISION["room"] = None
            return self._send(200, json.dumps({"ok": True, "dropped": bad}))

        if self.path == "/forget":
            room = (body.get("room") or "").strip().lower()
            with STATE_LOCK:
                FINGERPRINTS.pop(room, None)
                save_fingerprints(CFG["store"])
                if DECISION["room"] == room:
                    DECISION["room"] = None
            return self._send(200, json.dumps({"ok": True}))

        self._send(404, "{}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8093)
    p.add_argument("--nodes", required=True, help="comma-separated node names")
    p.add_argument("--store", default="/opt/roomloc/fingerprints.json")
    p.add_argument("--dwell", type=float, default=8.0,
                   help="seconds a new room must win before we commit")
    p.add_argument("--margin", type=float, default=6.0,
                   help="dB the best room must beat the runner-up by")
    p.add_argument("--stale-after", type=float, default=45.0)
    p.add_argument("--vote-share", type=float, default=0.6,
                   help="fraction of the dwell window that must agree to commit")
    a = p.parse_args()

    CFG.update(nodes=[n.strip() for n in a.nodes.split(",") if n.strip()],
               store=a.store, dwell=a.dwell, margin=a.margin,
               stale_after=a.stale_after, vote_share=a.vote_share)
    os.makedirs(os.path.dirname(a.store), exist_ok=True)
    load_fingerprints(a.store)

    threading.Thread(target=decision_loop, args=(CFG["nodes"],), daemon=True).start()
    print(f"roomloc hub on :{a.port} nodes={CFG['nodes']} rooms={sorted(FINGERPRINTS)}",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
