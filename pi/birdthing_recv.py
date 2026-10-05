#!/usr/bin/env python3
# BirdThing Pi receiver: TCP 9000 -> gentle 250 Hz high-pass -> aplay -> ALSA loopback -> BirdNET.
# The high-pass removes sub-bass rumble (wind, traffic, AC hum) that masks faint birds, WITHOUT
# touching bird frequencies (birds are ~300 Hz and up), so unlike aggressive band/gate filtering it
# doesn't cost detections. Runs on the BirdNET venv python (numpy+scipy). On ANY error it falls back
# to plain passthrough so the pipeline can never break. Single writer to aplay.stdin (no deadlock).
import socket, subprocess, time, os, threading

try:
    import clapdetect            # double-clap -> Home Assistant light toggle
except Exception as _e:          # never let the clap add-on break the pipeline
    clapdetect = None
    print("clapdetect unavailable:", _e, flush=True)


def _rawmic_listener():
    # Car Thing mic-health beacon (UDP 9001, JSON with the RAW pre-AGC peak) -> /tmp/bt_rawmic.
    # The pipeline watchdog reads it to tell "quiet room" from "stuck mic" (file mtime = freshness).
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        u.bind(("0.0.0.0", 9001))
        while True:
            d, _ = u.recvfrom(256)
            try:
                with open("/tmp/bt_rawmic", "wb") as f:
                    f.write(d)
            except Exception:
                pass
    except Exception as e:
        print("rawmic listener disabled:", e, flush=True)


threading.Thread(target=_rawmic_listener, daemon=True).start()

PORT = 9000
_lastlvl = 0.0   # throttle for writing the current loudness to /tmp/bt_level
LOOPDEV = "hw:Loopback,0,0"
RATE = 48000
# HP_HZ is set by the dashboard's mic-tuning "Rumble filter" toggle (systemd env drop-in).
# 0 (or unset<=0) = raw passthrough; otherwise the high-pass cutoff in Hz.
HP = float(os.environ.get("HP_HZ", "250"))
ORDER = 4

OK = HP > 0          # apply the high-pass; if off we still publish the loudness level below
HAVE_NP = False
try:
    import numpy as np
    from scipy.signal import butter, sosfilt, sosfilt_zi
    HAVE_NP = True
    if OK:
        SOS = butter(ORDER, HP, btype="highpass", fs=RATE, output="sos")
        S = {"zi": sosfilt_zi(SOS), "buf": b""}
        print("high-pass %d Hz active (rumble removal)" % HP, flush=True)
    else:
        print("rumble filter off (raw passthrough)", flush=True)
except Exception as e:
    print("filter disabled (passthrough):", e, flush=True)
    OK = False


def _publish_level(data):
    # write the current peak loudness for the dashboard's live "Listening…" indicator
    global _lastlvl
    if not HAVE_NP:
        return
    t = time.time()
    if t - _lastlvl <= 0.4:
        return
    _lastlvl = t
    try:
        a = np.frombuffer(data[:len(data) // 4 * 4], dtype="<i2")
        if a.size:
            open("/tmp/bt_level", "w").write(str(int(np.abs(a).max())))
    except Exception:
        pass


def _feed_clap(data):
    # tap the raw stream for the double-clap detector; must never raise
    if clapdetect is None or not HAVE_NP:
        return
    try:
        a = np.frombuffer(data[:len(data) // 4 * 4], dtype="<i2")
        if a.size:
            mono = a.reshape(-1, 2)[:, 0]  # one channel is enough for onset detection
            clapdetect.feed(mono)
    except Exception:
        pass


def process(data):
    _feed_clap(data)
    if not OK:
        _publish_level(data)               # rumble filter off: still report loudness
        return data
    try:
        raw = S["buf"] + data
        n = len(raw) // 4                  # 4 bytes per stereo S16_LE frame
        u = n * 4
        S["buf"] = raw[u:]
        if n == 0:
            return b""
        a = np.frombuffer(raw[:u], dtype="<i2").reshape(-1, 2).astype(np.float32)
        mono = (a[:, 0] + a[:, 1]) * 0.5
        y, S["zi"] = sosfilt(SOS, mono, zi=S["zi"])
        y = np.clip(y, -32768.0, 32767.0).astype("<i2")
        global _lastlvl
        t = time.time()
        if t - _lastlvl > 0.4:                 # publish current loudness for the live indicator
            _lastlvl = t
            try:
                open("/tmp/bt_level", "w").write(str(int(np.abs(y).max())))
            except Exception:
                pass
        st = np.empty((y.shape[0], 2), dtype="<i2")
        st[:, 0] = y
        st[:, 1] = y
        return st.tobytes()
    except Exception as e:
        print("filter error, passthrough:", e, flush=True)
        return data


def start_aplay():
    return subprocess.Popen(
        ["aplay", "-D", LOOPDEV, "-f", "S16_LE", "-r", "48000", "-c", "2", "-q"],
        stdin=subprocess.PIPE)


aplay = start_aplay()

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(1)
print("birdthing_recv listening on %d" % PORT, flush=True)

while True:
    conn, addr = srv.accept()
    print("client connected:", addr, flush=True)
    if OK:
        S["buf"] = b""
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            if aplay.poll() is not None:
                aplay = start_aplay()
            out = process(data)
            if out:
                aplay.stdin.write(out)
                aplay.stdin.flush()
    except Exception as e:
        print("client error:", e, flush=True)
    finally:
        try: conn.close()
        except Exception: pass
        print("client disconnected", flush=True)
