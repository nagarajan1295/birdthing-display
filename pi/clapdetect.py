#!/usr/bin/env python3
# Clap -> Home Assistant toggle, fed mono int16 samples from birdthing_recv.
#
# Detects the sharp broadband impulse of a hand clap (loud, very short, quiet
# before it). A DOUBLE clap -- two claps DBL_MIN..DBL_MAX seconds apart -- fires
# an HA `<domain>.toggle` on every configured entity via REST in a BACKGROUND
# thread, so `switch.toggle` / `light.toggle` / `media_player.toggle` flips them
# on/off exactly like "clap twice on, clap twice off". Runs inside the recv
# process alongside BirdNET; it only READS the mono samples that already flow
# through, so it never touches the bird audio path.
#
# TIME OF DAY PICKS THE TARGET: `entities` is the evening set (the sunset window
# below), `day_entities` the daytime one -- so the same double clap can toggle
# the living-room lights after sunset and the TV in the morning. See
# _target_entities().
#
# DETECTION IS RATIO-BASED so it survives the Car Thing mic's auto-gain: a clap
# is scored against the *rolling background level*, not a fixed absolute (a fixed
# threshold silently stops working when the room gets louder and AGC drops the
# gain). Two dials in clap.json, both live-editable from the dashboard:
#   sensitivity 1..10  -> how far above background a clap must jump (+ a floor)
#   boost       1..8   -> gain applied to the detection signal (for a quiet/far mic)
#
# TWO DETECTION MODES, live-switchable via clap.json "mode" (dashboard toggle):
#   "amplitude" -- any onset that's loud enough and isn't an instantaneous
#     hardware-glitch pop fires INSTANTLY. Reacts to any loud noise shaped like
#     a sharp rise-then-partial-decay, claps included -- the naive/fast option.
#   "smart" (default) -- the SAME fast onset detection, but a real double-clap
#     candidate is confirmed by YAMNet (below) before the toggle fires. More
#     accurate, costs roughly a second of extra latency per pair while ML
#     confirms.
#
# YAMNET CONFIRMATION (mode="smart"): Google's YAMNet -- a pretrained, open-
# source (Apache-2.0), AudioSet-trained sound-event classifier used worldwide
# in real shipped products and research (see yamnet_clap.py for model details).
# History worth recording here: an earlier version of this file tried to do
# clap-vs-voice discrimination itself with a hand-rolled spectral-flatness +
# decay-duration heuristic. Tested against REAL recorded audio (not synthetic
# approximations), it correctly rejected real coughs/laughs -- but ALSO
# incorrectly rejected real hand claps (a real clap's reverberant decay commonly
# runs longer, and its early spectrum looks different, than the clean synthetic
# model the heuristic was tuned against). That's a false-negative bug users
# would experience as "my claps don't work." YAMNet, tested against the same
# real audio, cleanly separated clap (0.89-0.94 clap-family score) from voice
# (0.33 voice-family score, ~0.02 clap-family) with a wide margin -- so it now
# owns the actual accept/reject decision in smart mode; the heuristic was
# removed rather than layered underneath a step that could veto it.
#
# Once the onset detector below provisionally accepts a double clap, a
# background thread buffers ~1s of audio starting at the first clap (from the
# rolling `_ring`) and asks YAMNet whether "Clapping"-family classes actually
# dominate over "Speech/voice"-family classes before the HA toggle fires. Fails
# OPEN (fires anyway) if the model is missing or inference errors, so a
# software problem in this layer can never silently stop the light from
# responding to a real double clap.
#
# DEFENSIVE: every public entry point swallows its own errors. Nothing in here
# may ever raise into birdthing_recv and break the pipeline.
import json, os, time, threading, urllib.request, datetime
from collections import deque

CFG_PATH  = os.environ.get("CLAP_CFG", "/opt/birdthing/clap.json")
LOG_PATH  = os.environ.get("CLAP_LOG", "/tmp/bt_clap.log")
STAT_PATH = os.environ.get("CLAP_STAT", "/tmp/bt_clap_stat.json")

# --- fixed detector constants ---
RATE       = 48000
FRAME      = 512      # ~10.7 ms analysis frame
BG_ALPHA   = 0.04     # EMA rate for the slow background level
REFRACTORY = 0.12     # ignore new onsets this long after one (a clap's decay/echo)
DBL_MIN    = 0.09     # two edges closer than this = same clap, not a pair
STAT_EVERY = 0.4      # how often to publish the live tuning stats
DECAY_MIN_FRAC = 0.20  # candidate onset must retain >=20% of its peak one frame later
                       # (real acoustic decay) or it's rejected as a hardware glitch pop
RING_MAX_SEC   = 2.0   # how much recent raw mono audio stays buffered for YAMNet
ML_WAIT_TIMEOUT = 2.5  # give up waiting for enough post-clap audio after this long
                       # (mic stall etc.) and fail OPEN rather than hang forever

_np = None
try:
    import numpy as _np
except Exception:
    _np = None

yamnet_clap = None
try:
    import yamnet_clap
except Exception as _e:
    yamnet_clap = None

# runtime state
_buf = b""            # leftover bytes < one FRAME
_bg = 1000.0          # background peak level (EMA), on the boosted signal
_hist = [0.0, 0.0, 0.0]   # last 3 boosted frame peaks (for the "quiet before" onset gate)
_last_edge = 0.0      # time of the last accepted clap edge
_last_double = 0.0    # time of the last accepted double clap
_muted_until = 0.0    # cooldown end after a toggle
_singles = 0          # counters (for the dashboard)
_doubles = 0
_pk_hold = 0.0        # loudest raw peak since the last stat publish
_stat_t = 0.0
_pending = None       # candidate onset awaiting next-frame decay confirmation:
                       # (peak_time, peak_pk, dbl_max, cooldown) or None
_glitches = 0          # onsets rejected for lacking acoustic decay (diagnostics)
_ring = deque()        # rolling (timestamp, raw_frame) buffer feeding YAMNet windows
_ml_confirmed = 0       # smart-mode doubles the ML layer agreed were real claps
_ml_rejected = 0        # smart-mode doubles the ML layer said were NOT a clap
_ml_unavailable = 0     # ML confirmation attempted but couldn't run (fail-open count)
# mic-health: rolling max of the RAW peak over ~2x30s windows. A healthy room always
# spikes past 30 within a minute (fridge, steps, voices); a stuck-quiet PDM never does.
_mw_start = 0.0
_mw_cur = 0.0
_mw_prev = 30000.0    # assume healthy until a full window proves otherwise
_cfg = None
_cfg_mtime = 0.0


def _log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _sens_params(s):
    # sensitivity 1 (strict) .. 10 (twitchy) -> (min_peak_floor, ratio_above_bg)
    s = max(1.0, min(10.0, float(s)))
    frac = (s - 1) / 9.0
    min_peak = 14000 - frac * 10500      # 14000 .. 3500
    ratio    = 7.0 - frac * 4.8          # 7.0 .. 2.2
    return min_peak, ratio


def _load_cfg():
    # reload clap.json on change so tuning/enable can be edited without a restart
    global _cfg, _cfg_mtime
    try:
        m = os.path.getmtime(CFG_PATH)
    except Exception:
        _cfg = None
        return None
    if _cfg is not None and m == _cfg_mtime:
        return _cfg
    try:
        with open(CFG_PATH) as f:
            c = json.load(f)
        _cfg, _cfg_mtime = c, m
        _log("config: entities=%s day_entities=%s enabled=%s sens=%s boost=%s mode=%s "
             "after_sunset=%s pre_min=%s"
             % (_entities(c), _entities(c, "day_entities"), c.get("enabled"),
                c.get("sensitivity", 6), c.get("boost", 1.0), c.get("mode", "smart"),
                c.get("after_sunset", False), c.get("pre_sunset_min", 0)))
        return c
    except Exception as e:
        _log("config error: %s" % e)
        _cfg = None
        return None


def _entities(c, key="entities"):
    e = c.get(key)
    if isinstance(e, list) and e:
        return [x for x in e if x and "." in x]
    if key == "entities" and c.get("entity"):
        return [c["entity"]]
    return []


_sun = {"t": 0.0, "night": None, "set": None}   # cached HA sun.sun (state + next sunset)

def _sun_state(c):
    # HA sun.sun: is it currently below the horizon, and when is the next actual
    # sunset (next_setting, the horizon crossing -- NOT next_dusk which is ~25 min
    # later). Uses the location HA is configured with; cached 2 min.
    now = time.time()
    if _sun["night"] is not None and now - _sun["t"] < 120:
        return _sun
    try:
        req = urllib.request.Request(
            c["ha_url"].rstrip("/") + "/api/states/sun.sun",
            headers={"Authorization": "Bearer " + c["ha_token"]})
        s = json.load(urllib.request.urlopen(req, timeout=4))
        a = s.get("attributes", {})
        _sun.update(t=now, night=(s.get("state") == "below_horizon"),
                    set=a.get("next_setting"))
    except Exception as e:
        _log("sun.sun check failed (%s) -> allowing" % e)
        if _sun["night"] is None:            # never got a reading -> fail OPEN
            return {"t": now, "night": True, "set": None}
    return _sun

def _sunset_allows(c):
    # True if clap toggling is allowed under the sunset gate. Active window =
    # [sunset - pre_sunset_min, sunrise]. At night -> active (until sunrise, when
    # sun.sun flips to above_horizon). By day -> only within pre_sunset_min before
    # the next sunset. Fails OPEN if the sun state is unknown.
    s = _sun_state(c)
    if s["night"]:
        return True
    pre = float(c.get("pre_sunset_min", 0) or 0)
    if pre > 0 and s.get("set"):
        try:
            secs = (datetime.datetime.fromisoformat(s["set"])
                    - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            if 0 <= secs <= pre * 60:
                return True
        except Exception:
            pass
    return False


def _target_entities(c):
    # Which entities a double clap should toggle RIGHT NOW.
    #
    # "Evening" = inside the sunset window [sunset - pre_sunset_min, sunrise];
    # "daytime" = everything else (sunrise until that window opens). If
    # `day_entities` is configured, daytime claps toggle THAT set instead of
    # being ignored -- e.g. lights own the evening while the TV owns the day.
    # With no `day_entities` the behaviour is exactly as before: one set, only
    # gated to the evening window when `after_sunset` is on.
    # Returns (entities, when) -- an empty list means "ignore this clap".
    day = _entities(c, "day_entities")
    if not day:
        if c.get("after_sunset") and not _sunset_allows(c):
            return [], "outside the after-sunset window"
        return _entities(c), "evening"
    if _sunset_allows(c):
        return _entities(c), "evening"
    return day, "daytime"


def _fire_toggle():
    # POST /api/services/<domain>/toggle for each entity that applies right now
    c = _load_cfg()
    if not c or not c.get("enabled") or not c.get("ha_token"):
        _log("double-clap (no HA config / disabled -> not toggling)")
        return
    ents, when = _target_entities(c)
    if not ents:
        _log("double-clap ignored — %s" % when)
        return
    _log("double-clap -> %s target%s: %s" % (when, "" if len(ents) == 1 else "s", ", ".join(ents)))
    for e in ents:
        domain = e.split(".")[0]
        url = c["ha_url"].rstrip("/") + "/api/services/%s/toggle" % domain
        body = json.dumps({"entity_id": e}).encode()
        for attempt in (1, 2):           # one retry: a momentary HA hiccup mustn't eat a clap
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Authorization": "Bearer " + c["ha_token"],
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=4) as r:
                    _log("toggled %s -> HTTP %s" % (e, r.status))
                break
            except Exception as ex:
                _log("toggle %s for %s: %s" % ("retrying" if attempt == 1 else "FAILED", e, ex))
                if attempt == 1:
                    time.sleep(0.8)


def _tune(c):
    # returns (min_peak, ratio, boost, dbl_max, cooldown), honouring optional
    # advanced overrides (peak_abs / ratio) in clap.json
    boost = max(1.0, min(8.0, float(c.get("boost", 1.0))))
    min_peak, ratio = _sens_params(c.get("sensitivity", 6))
    t = c.get("tune", {})
    if "peak_abs" in c or "peak_abs" in t:
        min_peak = float(c.get("peak_abs", t.get("peak_abs")))
    if "ratio" in c or "ratio" in t:
        ratio = float(c.get("ratio", t.get("ratio")))
    dbl_max  = float(c.get("dbl_max",  t.get("dbl_max", 0.6)))
    cooldown = float(c.get("cooldown", t.get("cooldown", 1.2)))
    return min_peak, ratio, boost, dbl_max, cooldown


def _mic_quiet(now, raw_pk):
    # rolling ~60s view of the raw peak; True = the PDM mic looks stuck-quiet
    global _mw_start, _mw_cur, _mw_prev
    if _mw_start == 0.0:
        _mw_start = now
    if raw_pk > _mw_cur:
        _mw_cur = raw_pk
    if now - _mw_start >= 30.0:          # roll the 30s window
        _mw_prev, _mw_cur, _mw_start = _mw_cur, 0.0, now
    return max(_mw_cur, _mw_prev) < 30


def _publish_stat(now, bg, sens, boost, min_peak, ratio, enabled, mic_quiet, mode):
    global _stat_t, _pk_hold
    if now - _stat_t < STAT_EVERY:
        return
    _stat_t = now
    try:
        st = {"t": now, "enabled": bool(enabled), "mic_quiet": bool(mic_quiet),
              "bg": round(bg / max(boost, 1e-6)), "peak": round(_pk_hold),
              "sens": sens, "boost": boost,
              "min_peak": round(min_peak / max(boost, 1e-6)), "ratio": round(ratio, 2),
              "singles": _singles, "doubles": _doubles, "glitches": _glitches,
              "mode": mode, "ml_available": bool(yamnet_clap and yamnet_clap.AVAILABLE),
              "ml_confirmed": _ml_confirmed, "ml_rejected": _ml_rejected,
              "ml_unavailable": _ml_unavailable,
              "last_single_ago": round(now - _last_edge, 1) if _last_edge else None,
              "last_double_ago": round(now - _last_double, 1) if _last_double else None}
        tmp = STAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STAT_PATH)
    except Exception:
        pass
    _pk_hold = 0.0


def _on_edge(now, dbl_max, cooldown, mode):
    # an accepted clap edge at time `now`; decide single vs double
    global _last_edge, _last_double, _muted_until, _singles, _doubles
    _singles += 1
    gap = now - _last_edge
    prev_edge = _last_edge          # the first clap's onset time, for the ML window
    _last_edge = now
    if DBL_MIN <= gap <= dbl_max:
        _muted_until = now + cooldown
        _last_edge = 0.0            # consume the pair so a 3rd clap starts fresh
        _last_double = now
        _doubles += 1
        _log("DOUBLE CLAP (gap=%.0fms)" % (gap * 1000))
        if mode == "smart" and yamnet_clap is not None and yamnet_clap.AVAILABLE:
            threading.Thread(target=_ml_confirm_and_fire, args=(prev_edge,), daemon=True).start()
        else:
            threading.Thread(target=_fire_toggle, daemon=True).start()


def _ring_extract(t0, t1):
    # best-effort concatenation of buffered raw frames covering [t0, t1]
    chunks = [fr for (t, fr) in _ring if t0 - FRAME / RATE <= t <= t1 + FRAME / RATE]
    if not chunks:
        return None
    return _np.concatenate(chunks)


def _ml_confirm_and_fire(window_start):
    # Runs off the audio thread. Waits for enough real audio to have streamed in
    # past the clap (YAMNet needs a fixed ~0.975s window), then asks it whether
    # this was really a clap before the light actually toggles. Fails OPEN (fires
    # anyway) on any problem -- an ML bug must never look like "clap stopped
    # working" to the user.
    global _ml_confirmed, _ml_rejected, _ml_unavailable
    try:
        need_end = window_start + yamnet_clap.WINDOW_SAMPLES / float(yamnet_clap.SAMPLE_RATE)
        deadline = time.time() + ML_WAIT_TIMEOUT
        while time.time() < deadline:
            if _ring and _ring[-1][0] >= need_end:
                break
            time.sleep(0.05)
        raw = _ring_extract(window_start, need_end)
        if raw is None or raw.size < RATE * 0.1:
            raise RuntimeError("not enough buffered audio")
        is_clap, cs, vs, top = yamnet_clap.classify(raw, RATE)
        if is_clap:
            _ml_confirmed += 1
            _log("ML confirmed clap (clap=%.2f voice=%.2f top=%s)" % (cs, vs, top))
            _fire_toggle()
        else:
            _ml_rejected += 1
            _log("ML REJECTED double-clap as non-clap (clap=%.2f voice=%.2f top=%s) -- not toggling"
                 % (cs, vs, top))
    except Exception as e:
        _ml_unavailable += 1
        _log("ML confirmation failed (%s) -- firing anyway (fail-open)" % e)
        _fire_toggle()


def feed(mono):
    """Feed a 1-D int16 numpy array of mono samples. Never raises."""
    global _buf, _bg, _pk_hold, _pending, _glitches
    if _np is None:
        return
    try:
        c = _load_cfg() or {}
        min_peak, ratio, boost, dbl_max, cooldown = _tune(c)
        enabled = bool(c.get("enabled"))
        sens = c.get("sensitivity", 6)
        mode = c.get("mode", "smart")
        if mode not in ("smart", "amplitude"):
            mode = "smart"
        if mono is None or mono.size == 0:
            return
        raw = _buf + mono.astype("<i2").tobytes()
        n = len(raw) // (FRAME * 2)
        _buf = raw[n * FRAME * 2:]
        if n == 0:
            return
        frames = _np.frombuffer(raw[:n * FRAME * 2], dtype="<i2").reshape(n, FRAME)
        now = time.time()
        for fr in frames:
            raw_pk = float(_np.abs(fr).max())
            pk = raw_pk * boost
            now += FRAME / RATE
            if raw_pk > _pk_hold:
                _pk_hold = raw_pk
            mq = _mic_quiet(now, raw_pk)

            # keep a rolling buffer of raw audio so a smart-mode double-clap can be
            # handed a real window of surrounding sound for YAMNet to judge
            _ring.append((now, fr.copy()))
            while _ring and _ring[0][0] < now - RING_MAX_SEC:
                _ring.popleft()

            # DECAY CONFIRMATION (both modes): a real clap is an acoustic event with
            # physical persistence -- the room's reverb means the NEXT ~10.7ms frame
            # is still meaningfully loud. A hardware glitch (the PDM mic is the same
            # board design as the ALS that failed at the silicon level -- see
            # memory) is an instantaneous electrical pop: one sample-frame spike
            # that snaps straight back to the noise floor with nothing after it.
            # This is a HARDWARE workaround, not a clap-vs-voice discriminator, so
            # it applies in both modes; actual clap-vs-voice discrimination in
            # smart mode is YAMNet's job (see _on_edge), not this check's.
            if _pending is not None:
                p_time, p_pk, p_dbl_max, p_cooldown = _pending
                _pending = None
                if pk >= DECAY_MIN_FRAC * p_pk:
                    _on_edge(p_time, p_dbl_max, p_cooldown, mode)
                else:
                    _glitches += 1
                    _log("glitch rejected (peak=%.0f next=%.0f, no decay)" % (p_pk, pk))

            # onset = a sharp RISE above the RECENT quietest point. Using the min of the
            # last 3 frames (~32ms) instead of just the previous frame means a clap whose
            # attack straddles a frame boundary still registers (the frame right before
            # may hold half the rise), while sustained loud noise (music/TV) — where all
            # recent frames are loud — still fails the test.
            quiet_before = min(_hist) < 0.5 * pk
            is_candidate = (enabled and now >= _muted_until and quiet_before
                            and pk >= min_peak and pk >= ratio * _bg
                            and (now - _last_edge) >= REFRACTORY)
            if is_candidate:
                _pending = (now, pk, dbl_max, cooldown)
            else:
                # only quiet frames update the background (claps mustn't inflate it)
                _bg = (1 - BG_ALPHA) * _bg + BG_ALPHA * min(pk, _bg * 3 + 200)
            _hist[0], _hist[1], _hist[2] = _hist[1], _hist[2], pk
            _publish_stat(now, _bg, sens, boost, min_peak, ratio, enabled, mq, mode)
    except Exception as e:
        _log("feed error: %s" % e)


# so the module can be smoke-tested on a WAV without the pipeline
if __name__ == "__main__":
    import sys, wave
    if _np is None:
        print("numpy required"); sys.exit(1)
    w = wave.open(sys.argv[1], "rb")
    ch = w.getnchannels()
    data = _np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch == 2:
        data = data.reshape(-1, 2).mean(axis=1).astype("<i2")
    feed(data)
    print("done; see", LOG_PATH)
