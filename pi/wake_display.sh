#!/bin/bash
# Force the Car Thing's screen back on every morning, even if the 'm' button
# was used to blank it (/tmp/display_off) overnight -- so it's on by the time
# the user wakes up regardless of whether they turned it off themselves.
#
# 2026-09-23: /tmp/display_off is created by birdknob.service, which runs as
# root (User=root in its unit file) -- so the flag file is root-owned, and a
# plain `rm` as the superbird user fails with "Operation not permitted"
# against root-owned files in /tmp's sticky-bit directory, even though
# superbird is in the sudo group. This silently broke every wake for days
# (found via /var/log/birdthing-wake-display.log showing "Operation not
# permitted" x3 every morning, "FAILED after 3 attempts", screen still off).
# Fix: use sudo (superbird has passwordless sudo on the CT, verified).
LOG=/var/log/birdthing-wake-display.log
CT_USER=superbird
CT_HOST=192.168.7.2
CT_PW=superbird

for attempt in 1 2 3; do
  if sshpass -p "$CT_PW" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=8 "$CT_USER@$CT_HOST" 'sudo -n rm -f /tmp/display_off' 2>>"$LOG"; then
    echo "$(date '+%F %T') woke the display (attempt $attempt)" >> "$LOG"
    exit 0
  fi
  sleep 20
done
echo "$(date '+%F %T') FAILED to reach the Car Thing after 3 attempts" >> "$LOG"
exit 1
