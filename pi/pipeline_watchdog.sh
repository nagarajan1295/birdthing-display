#!/bin/bash
# BirdThing pipeline self-repair. The audio chain (CT PDM mic -> TCP -> Pi recv -> ALSA loopback ->
# BirdNET) occasionally stalls "silently": services stay active but the mic goes dead, so BirdNET
# hears silence and stops IDing.
#
# v2: the old check (loopback peak < 30 => stuck) could NOT tell a stuck mic from a genuinely
# quiet room at night, so it hard-reset the whole chain every ~4 min all night (log showed 65
# resets), killing audio for seconds each time — the "clap sometimes doesn't work" cause. Now the
# Car Thing beacons its RAW pre-AGC mic peak over UDP (birdmic -> recv -> /tmp/bt_rawmic): a
# healthy PDM always reads self-noise > 25 even in silence, so:
#   loopback quiet + raw >= 25          -> quiet room, mic fine: DO NOT reset
#   loopback quiet + raw < 25           -> mic stuck-quiet: reset (rate-limited)
#   loopback quiet + no fresh beacon    -> birdmic/CT dead or pre-beacon build: reset (rate-limited)
# Rate limit: a reset that didn't fix things won't be retried for 15 min (no more reset storms).
PY=/home/birdpi/BirdNET-Pi/birdnet/bin/python3
LOG=/tmp/birdthing-watchdog.log
STATEF=/tmp/birdthing-wd-low
LASTRESET=/tmp/birdthing-wd-lastreset

peak=$("$PY" - <<'EOF'
import glob, os, wave, numpy as np
fs = sorted(glob.glob('/home/birdpi/BirdSongs/StreamData/*.wav'), key=os.path.getmtime)[-3:]
mx = 0
for f in fs:
    try:
        w = wave.open(f); d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if d.size:
            mx = max(mx, int(np.abs(d).max()))
    except Exception:
        pass
print(mx)
EOF
)
peak=${peak:-0}

# raw mic health from the CT beacon: "<age_seconds> <raw_peak>" (9999 -1 if unavailable)
rawinfo=$("$PY" - <<'EOF'
import json, os, time
try:
    st = os.stat('/tmp/bt_rawmic')
    d = json.load(open('/tmp/bt_rawmic'))
    print("%d %d" % (int(time.time() - st.st_mtime), int(d.get("raw", -1))))
except Exception:
    print("9999 -1")
EOF
)
rawage=${rawinfo%% *}
raw=${rawinfo##* }
ts=$(date '+%F %T')

do_reset() {
    now=$(date +%s)
    last=$(cat "$LASTRESET" 2>/dev/null || echo 0)
    if [ $((now - last)) -lt 900 ]; then
        echo "$ts $1 — but rate-limited (last reset $(( (now-last)/60 ))m ago), skipping" >> "$LOG"
        return
    fi
    echo "$ts $1 -> hard-resetting audio chain" >> "$LOG"
    sshpass -p superbird ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
        superbird@192.168.7.2 'sudo systemctl stop birdmic; sleep 2; sudo systemctl start birdmic' 2>/dev/null
    sudo systemctl restart birdthing-recv
    sudo systemctl restart birdnet_analysis
    echo "$now" > "$LASTRESET"
    echo 0 > "$STATEF"
    echo "$ts hard-reset done" >> "$LOG"
}

if [ "$peak" -ge 30 ]; then
    echo 0 > "$STATEF"
    echo "$ts ok (peak=$peak raw=$raw)" >> "$LOG"
else
    c=$(( $(cat "$STATEF" 2>/dev/null || echo 0) + 1 ))
    echo "$c" > "$STATEF"
    # debounce: act only after TWO consecutive silent checks so a normal restart blip passes
    if [ "$c" -ge 2 ]; then
        if [ "$rawage" -le 90 ] && [ "$raw" -ge 25 ]; then
            # mic hardware is alive; the room is just quiet — nothing to fix
            echo 0 > "$STATEF"
            echo "$ts quiet room (peak=$peak raw=$raw) — mic healthy, no reset" >> "$LOG"
        elif [ "$rawage" -le 90 ]; then
            do_reset "STALL: mic stuck-quiet (peak=$peak raw=$raw)"
        else
            do_reset "STALL: no mic beacon ${rawage}s (peak=$peak)"
        fi
    else
        echo "$ts low (peak=$peak raw=$raw age=$rawage x$c) - waiting one more cycle" >> "$LOG"
    fi
fi
# keep the log small
tail -n 200 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
