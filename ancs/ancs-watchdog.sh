#!/bin/bash
# ANCS watchdog - escalating self-repair.
#
# WHY THIS EXISTS: every previous "fix" verified the link worked ONCE. The
# user's actual complaint was that it works for a few minutes and then never
# recovers on its own after they leave and come back. Nothing was watching.
#
# It also keeps a health record (/var/log/ancs-health.log) so link uptime can
# be judged over hours instead of over a lucky 60-second window.
set -u
API=http://127.0.0.1:8099
STATE=/var/lib/ancs-watchdog
HEALTH=/var/log/ancs-health.log
mkdir -p "$STATE"

now=$(date +%s)
log() { echo "$(date '+%F %T') $*" >> /var/log/ancs-watchdog.log; }

status=$(curl -s --max-time 8 "$API/api/status" 2>/dev/null)
if [ -z "$status" ]; then
    log "gateway API not answering - restarting ancs-gateway"
    systemctl restart ancs-gateway
    echo "$now" > "$STATE/last_action"
    exit 0
fi

linked=$(echo "$status"  | grep -o '"linked": [a-z]*'        | awk '{print $2}')
bonded=$(echo "$status"  | grep -o '"path": "[^"]*"' | head -1)
advert=$(echo "$status"  | grep -o '"advertising": [a-z]*'   | awk '{print $2}')
nsrc=$(echo "$status"    | grep -o '"notification_source": [a-z]*' | awk '{print $2}')
rssi=$(echo "$status"    | grep -o '"rssi": -\?[0-9]*'       | awk '{print $2}')

# one line per run: the record that shows whether this actually holds up
echo "$(date '+%F %T') linked=$linked notifying=$nsrc advertising=$advert rssi=${rssi:-na}" >> "$HEALTH"
# keep the health log bounded
tail -n 20000 "$HEALTH" > "$HEALTH.tmp" 2>/dev/null && mv "$HEALTH.tmp" "$HEALTH"

last_action_silent=$(cat "$STATE/last_silent" 2>/dev/null || echo 0)

# nothing bonded => nothing to repair, the phone simply has not been paired
[ -z "$bonded" ] && { rm -f "$STATE/down_since"; exit 0; }

if [ "$linked" = "true" ] && [ "$nsrc" = "true" ]; then
    rm -f "$STATE/down_since" "$STATE/escalation"

    # SILENT-LINK: healthy-looking but delivering nothing.
    # This is the failure that kept coming back - BlueZ reports Notifying=true
    # while holding no notify session, so the CCCD was never written and the
    # phone sends nothing, forever. The gateway now forces a re-subscribe on
    # every attach so it should not recur, but it recurred often enough to be
    # worth a backstop the watchdog can actually see. A restart re-attaches and
    # re-subscribes in ~10s.
    #
    # 90 minutes is deliberately long: a genuinely quiet stretch (overnight) is
    # normal and must not cause hourly churn.
    events=$(echo "$status" | grep -o '"events_since_link": [0-9]*' | awk '{print $2}')
    linked_at=$(echo "$status" | grep -o '"linked_at": [0-9.]*' | awk '{print $2}' | cut -d. -f1)
    if [ -n "$events" ] && [ "$events" = "0" ] && [ -n "$linked_at" ] && [ "$linked_at" != "0" ]; then
        age=$(( now - linked_at ))
        if [ "$age" -gt 5400 ] && [ $(( now - last_action_silent )) -gt 5400 ]; then
            log "linked ${age}s with ZERO events - forcing a re-subscribe (restart)"
            systemctl restart ancs-gateway
            echo "$now" > "$STATE/last_silent"
        fi
    fi
    exit 0
fi

# --- not healthy: track how long, and escalate slowly ----------------------
if [ ! -f "$STATE/down_since" ]; then
    echo "$now" > "$STATE/down_since"
    exit 0                      # a brief gap is normal; do not react instantly
fi
down_since=$(cat "$STATE/down_since")
down=$(( now - down_since ))
esc=$(cat "$STATE/escalation" 2>/dev/null || echo 0)
last_action=$(cat "$STATE/last_action" 2>/dev/null || echo 0)
last_action_silent=$(cat "$STATE/last_silent" 2>/dev/null || echo 0)

# never act more than once every 4 minutes
[ $(( now - last_action )) -lt 240 ] && exit 0

# The phone being out of range is NORMAL and must not trigger repairs - that
# would bounce the radio all day while the user is at work. Only escalate once
# the outage is long enough to be suspicious.
if [ "$down" -lt 600 ]; then exit 0; fi

case "$esc" in
  0)
    log "down ${down}s - re-asserting the adapter (prep) and advertisement"
    /opt/ancs/ancs-prep.sh >> /var/log/ancs-watchdog.log 2>&1
    echo 1 > "$STATE/escalation" ;;
  1)
    log "down ${down}s - restarting ancs-gateway"
    systemctl restart ancs-gateway
    echo 2 > "$STATE/escalation" ;;
  2)
    log "down ${down}s - bouncing bluetoothd, then prep + gateway"
    systemctl restart bluetooth
    sleep 5
    /opt/ancs/ancs-prep.sh >> /var/log/ancs-watchdog.log 2>&1
    systemctl restart ancs-gateway
    echo 3 > "$STATE/escalation" ;;
  *)
    # Deliberately NO reboot: this Pi runs BirdNET and the dashboard, and a
    # reboot is worse than an outage. Log loudly and stop escalating.
    log "down ${down}s - still down after every repair step; leaving it alone"
    ;;
esac
echo "$now" > "$STATE/last_action"
