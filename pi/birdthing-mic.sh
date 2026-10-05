#!/bin/bash
# BirdThing mic streamer: Car Thing PDM mic -> ALSA loopback (so BirdNET records it).
# Re-establishes usb0, pushes the recorder to the Car Thing, streams stereo S16LE 48k.
# Backs off slowly on failure so a broken stream can't hammer/wedge the Car Thing sshd.
set -u
CT_IP=192.168.7.2
CT_PASS=superbird
MICSTREAM=/opt/birdthing/micstream.py
LOOPDEV="hw:Loopback,0,0"
SSHOPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"

ensure_usb0() {
  ip link set usb0 up 2>/dev/null
  ip -4 addr show usb0 | grep -q 192.168.7 || ip addr add 192.168.7.1/24 dev usb0 2>/dev/null
}

while true; do
  ensure_usb0
  if ! ping -c1 -W2 "$CT_IP" >/dev/null 2>&1; then
    echo "[birdthing-mic] Car Thing unreachable, retry in 15s"; sleep 15; continue
  fi
  echo "[birdthing-mic] starting stream $(date)"
  START=$(date +%s)
  # On the Car Thing: free any stuck capture holder, write the recorder, run it.
  # fuser -k on the capture node clears an orphaned python WITHOUT matching our own shell.
  cat "$MICSTREAM" | sshpass -p "$CT_PASS" ssh $SSHOPTS superbird@"$CT_IP" \
      "fuser -k /dev/snd/pcm0c 2>/dev/null; sleep 1; cat > /tmp/micstream.py && exec python3 /tmp/micstream.py" 2>/dev/null \
    | aplay -D "$LOOPDEV" -f S16_LE -r 48000 -c 2 -q 2>/dev/null &
  APIPE=$!
  # Watchdog: if aplay stops consuming bytes for ~12s, the CT stream stalled (half-open
  # ssh). Kill the chain so the loop reconnects.
  ( AP=""
    for _ in $(seq 1 200); do AP=$(pgrep -x aplay | head -1); [ -n "$AP" ] && break; sleep 0.2; done
    LAST=-1
    while kill -0 "$APIPE" 2>/dev/null; do
      sleep 12
      CUR=$(awk '/^rchar/{print $2}' /proc/"$AP"/io 2>/dev/null)
      [ -z "$CUR" ] && break
      if [ "$CUR" = "$LAST" ]; then
        echo "[birdthing-mic] watchdog: aplay starved, killing stream"
        pkill -P "$APIPE" 2>/dev/null; kill "$APIPE" 2>/dev/null; break
      fi
      LAST="$CUR"
    done ) &
  WD=$!
  wait "$APIPE"
  RC=$?
  kill "$WD" 2>/dev/null
  ELAPSED=$(( $(date +%s) - START ))
  # If the stream died almost immediately, back off hard so we don't hammer sshd.
  if [ "$ELAPSED" -lt 10 ]; then
    echo "[birdthing-mic] stream failed fast (rc=$RC, ${ELAPSED}s) -> back off 30s"; sleep 30
  else
    echo "[birdthing-mic] stream ended (rc=$RC after ${ELAPSED}s), restart in 8s"; sleep 8
  fi
done
