#!/usr/bin/env python3
# BirdThing Car Thing mic daemon. Captures the PDM mic, pushes gained stereo S16LE 48k
# to the Pi over ONE persistent TCP connection. Run by systemd on the Car Thing.
# Self-healing: if the PDM capture stalls (readi would block forever), snd_pcm_wait times
# out and we reopen the device (re-applying PDMIN source-select + PDM Train trigger).
import ctypes, socket, signal, sys, time, array

PI_HOST = "192.168.7.1"
PI_PORT = 9000
BEACON_PORT = 9001   # UDP mic-health beacon -> Pi: {"t":..,"raw":..,"gain":..} every ~2s.
                     # The RAW (pre-AGC) peak is the mic-health ground truth: a healthy PDM
                     # always reads self-noise > NOISE_FLOOR even in a silent room, so the
                     # Pi watchdog can tell "quiet room" from "stuck mic" without SSH.
WAIT_MS = 2000  # if no audio for this long, the PDM stalled -> reopen
# Auto gain: scale each chunk so its peak approaches TARGET_PEAK, smoothed to avoid pumping.
# A noise gate keeps near-silence from being blown up into false detections.
TARGET_PEAK = 9000
MAX_GAIN = 120
MIN_GAIN = 1
NOISE_FLOOR = 25   # raw peak below this is treated as silence (gain held low; user-tunable slider)
STUCK_FLOOR = 20   # FIXED stuck-quiet threshold, deliberately decoupled from NOISE_FLOOR: when the
                   # user raised the gate slider to 110, the reopen-watchdog inherited it and
                   # reopened the PDM every 20s during perfectly healthy low-noise periods -> the
                   # "mic flapping" churn. Hardware-stuck is raw < ~20 regardless of taste in gating.
QUIET_RECOVER_SEC = 20  # if raw stays below STUCK_FLOOR this long the PDM has gone "stuck-quiet"
                        # (still streaming but ~silent) -> reopen to recover. A healthy mic always
                        # reads some self-noise (>25), so genuine quiet won't false-trigger.
_gain = [8.0]
_quiet_since = [None]
_usock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_braw = [0]          # max raw peak since the last beacon
_bt = [0.0]          # last beacon time

a = ctypes.CDLL("libasound.so.2")
a.snd_pcm_readi.restype = ctypes.c_long
a.snd_pcm_wait.restype = ctypes.c_int
pcm = ctypes.c_void_p()
_run = [True]

def set_ctl(numid, v):
    ctl = ctypes.c_void_p()
    if a.snd_ctl_open(ctypes.byref(ctl), b"hw:0", 0) < 0:
        return
    eid = ctypes.c_void_p(); a.snd_ctl_elem_id_malloc(ctypes.byref(eid))
    a.snd_ctl_elem_id_set_numid(eid, numid); a.snd_ctl_elem_id_set_interface(eid, 2)
    val = ctypes.c_void_p(); a.snd_ctl_elem_value_malloc(ctypes.byref(val))
    a.snd_ctl_elem_value_set_id(val, eid); a.snd_ctl_elem_value_set_enumerated(val, 0, v)
    a.snd_ctl_elem_write(ctl, val)
    a.snd_ctl_close(ctl)

def close_pcm():
    try: a.snd_pcm_drop(pcm); a.snd_pcm_close(pcm)
    except Exception: pass

def open_pcm():
    global pcm
    pcm = ctypes.c_void_p()
    set_ctl(15, 4)  # Audio In Source = PDMIN
    set_ctl(6, 1)   # PDM Train trigger -> starts the PDM datapath
    if a.snd_pcm_open(ctypes.byref(pcm), b"hw:0,0", 1, 0) < 0:
        return False
    if a.snd_pcm_set_params(pcm, 2, 3, 1, 48000, 1, 500000) < 0:
        return False
    a.snd_pcm_start(pcm)
    return True

def cleanup(*_):
    _run[0] = False
    close_pcm()
    sys.exit(0)

signal.signal(signal.SIGTERM, cleanup)
signal.signal(signal.SIGINT, cleanup)

while not open_pcm():
    close_pcm(); time.sleep(2)

N = 2048
b = ctypes.create_string_buffer(N*2)
sock = None
while _run[0]:
    if sock is None:
        try:
            sock = socket.create_connection((PI_HOST, PI_PORT), timeout=10)
        except Exception:
            time.sleep(2); continue
    # Wait for audio with a timeout; 0 == stall -> reopen the PDM device.
    w = a.snd_pcm_wait(pcm, WAIT_MS)
    if w == 0:
        close_pcm()
        while _run[0] and not open_pcm():
            close_pcm(); time.sleep(1)
        continue
    if w < 0:
        a.snd_pcm_recover(pcm, w, 1); continue
    n = a.snd_pcm_readi(pcm, b, N)
    if n < 0:
        a.snd_pcm_recover(pcm, n, 1); continue
    if n == 0:
        continue
    mono = array.array('h'); mono.frombytes(b.raw[:n*2])
    peak = max(abs(max(mono)), abs(min(mono))) if n else 0
    # watchdog for the "stuck-quiet" PDM state: snd_pcm_wait keeps returning data so the normal
    # stall-reopen never fires, but the data is ~silent. After a sustained quiet run, reopen.
    nowt = time.time()
    # mic-health beacon (fire-and-forget UDP; must never affect the audio path)
    if peak > _braw[0]:
        _braw[0] = peak
    if nowt - _bt[0] >= 2.0:
        try:
            _usock.sendto(b'{"t":%d,"raw":%d,"gain":%d}'
                          % (int(nowt), int(_braw[0]), int(_gain[0])), (PI_HOST, BEACON_PORT))
        except Exception:
            pass
        _bt[0] = nowt
        _braw[0] = 0
    if peak < STUCK_FLOOR:
        if _quiet_since[0] is None:
            _quiet_since[0] = nowt
        elif nowt - _quiet_since[0] > QUIET_RECOVER_SEC:
            _quiet_since[0] = None
            close_pcm()
            while _run[0] and not open_pcm():
                close_pcm(); time.sleep(1)
            _gain[0] = 8.0
            continue
    else:
        _quiet_since[0] = None
    if peak < NOISE_FLOOR:
        desired = MIN_GAIN            # silence: don't amplify the noise floor
    else:
        desired = TARGET_PEAK / peak
        if desired > MAX_GAIN: desired = MAX_GAIN
        elif desired < MIN_GAIN: desired = MIN_GAIN
    # smooth: rise quickly, fall slowly so a loud bird isn't clipped but quiet stays boosted
    g = _gain[0]
    g = (0.5*g + 0.5*desired) if desired < g else (0.8*g + 0.2*desired)
    _gain[0] = g
    st = array.array('h', bytes(4*n))
    for i in range(n):
        v = int(mono[i] * g)
        if v > 32767: v = 32767
        elif v < -32768: v = -32768
        st[2*i] = v; st[2*i+1] = v
    try:
        sock.sendall(st.tobytes())
    except Exception:
        try: sock.close()
        except Exception: pass
        sock = None; time.sleep(1)
