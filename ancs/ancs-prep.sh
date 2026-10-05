#!/bin/bash
# Prepare hci0 on the BirdThing Pi for ANCS, and assert it on every boot.
#
# The gateway briefly ran on the Mac mini and was moved back: the mini sits in
# the wrong room (BLE is ~10m) and, being a desktop, advertised itself as a
# COMPUTER - which stops iOS offering notification sharing at all. This Pi is
# where the user actually is.
#
# WHY DUAL MODE, not LE-only:
#   ANCS itself is pure BLE, but iOS will not list a pure-BLE peripheral in
#   Settings > Bluetooth (verified on air on the Pi: correct AD type 0x15, real
#   ANCS UUID, 100ms interval, connectable, named - the iPhone showed nothing).
#   Classic has to be available for the phone to DISCOVER the accessory. Once
#   connected, the ANCS link itself rides LE, and pairing over classic derives
#   the LE keys via CTKD.
#
# btmgmt GOTCHA: it silently ignores its command when it has no tty (exits 0
# doing nothing; under systemd it blocks until the unit times out). Every
# btmgmt call here therefore goes through `script -qec` to get a pty.
set -u
ADAPTER="${ADAPTER:-hci0}"
log() { echo "[ancs-prep] $*"; }
mgmt() { script -qec "btmgmt --index $ADAPTER $*" /dev/null >/dev/null 2>&1; }

for rf in /sys/class/rfkill/rfkill*; do
    [ -e "$rf/type" ] || continue
    [ "$(cat "$rf/type")" = "bluetooth" ] || continue
    if [ "$(cat "$rf/soft")" != "0" ]; then
        echo 0 > "$rf/soft" 2>/dev/null && log "unblocked $(basename "$rf")"
    fi
done

for _ in $(seq 1 30); do
    hciconfig "$ADAPTER" >/dev/null 2>&1 && break
    sleep 1
done

# =====================================================================
# THE ADDRESS CLASH - ELIMINATED, not mitigated
# =====================================================================
# This Pi's factory BD address is DC:A6:32:62:53:01, and the BEDROOM Pi
# SPOOFS that exact address to serve the WeatherThing Car Thing over BT PAN
# (the CT's firmware is hard-locked to it, so the bedroom Pi cannot give it
# up without reflashing the CT).
#
# Two devices answering on one address is not a theoretical problem - it
# broke notifications in a way that looked like everything else:
#   - in the bedroom the phone reached the OTHER Pi, which offers NAP, so
#     iOS showed the connection as an ETHERNET/network device
#   - that Pi is called "raspberrypi", so the name "BirdThing" would
#     randomly disappear from the phone's Bluetooth list
#   - that Pi runs no ANCS gateway, so NO notification could ever arrive
#
# Fix: this Pi takes a DIFFERENT address. Its Bluetooth serves only ANCS
# (the BirdThing Car Thing is on USB, birdthing-btnap stays disabled), so
# nothing else here depends on the factory address.
#
# NOTE: changing the adapter address moves bluez's bond storage to a new
# /var/lib/bluetooth/<addr> directory, so the phone must be re-paired ONCE.
WANT_ADDR="${WANT_ADDR:-DC:A6:32:62:53:B1}"
cur_addr="$(hciconfig "$ADAPTER" 2>/dev/null | sed -n 's/.*BD Address: \([0-9A-F:]*\).*/\1/p' | head -1)"
if [ -n "$WANT_ADDR" ] && [ "$cur_addr" != "$WANT_ADDR" ]; then
    log "BD address $cur_addr -> $WANT_ADDR (clash with the bedroom Pi's spoof)"
    mgmt power off
    mgmt public-addr "$WANT_ADDR"
    mgmt power on
    sleep 2
    # this BCM part needs an HCI reset before a written static address
    # actually becomes the ACTIVE BD address - btmgmt power on alone is not
    # enough (same lesson as the bedroom Pi's spoof script)
    for i in $(seq 1 8); do
        cur_addr="$(hciconfig "$ADAPTER" 2>/dev/null | sed -n 's/.*BD Address: \([0-9A-F:]*\).*/\1/p' | head -1)"
        [ "$cur_addr" = "$WANT_ADDR" ] && break
        log "address not applied yet (try $i) - hci reset"
        hciconfig "$ADAPTER" down  2>/dev/null
        hciconfig "$ADAPTER" reset 2>/dev/null
        hciconfig "$ADAPTER" up    2>/dev/null
        sleep 2
    done
fi
log "BD address: $(hciconfig "$ADAPTER" 2>/dev/null | sed -n 's/.*BD Address: \([0-9A-F:]*\).*/\1/p' | head -1)"

settings() { script -qec "btmgmt --index $ADAPTER info" /dev/null 2>/dev/null \
             | grep -m1 'current settings:'; }

# Assert BR/EDR + SSP + CONNECTABLE + BONDABLE.
#   ssp:         power-cycling bredr CLEARS it, and without it the adapter
#                offers only legacy PIN pairing, which iOS refuses outright.
#   connectable: this is page scan. Without it the iPhone cannot initiate a
#                reconnection at all - the single most common cause of "I have
#                to connect it by hand every time". It is NOT implied by
#                br/edr or ssp and has been silently dropped before.
#   bondable:    the mini boots without it (verified: current settings were
#                just "powered ssp br/edr le secure-conn"), so pairing would
#                fail before this script existed.
# Honour ControllerMode: when main.conf asks for LE-only this script must NOT
# turn BR/EDR back on. It did, silently, which made an LE-only test meaningless
# (current settings still read "br/edr" while main.conf said le).
#
# Classic is only ever needed to PAIR - iOS will not list a pure-BLE peripheral
# in Settings. Once the bond carries LE keys, classic is pure liability: iOS
# grabs the classic link, treats the device as connected, and never opens the
# LE link that ANCS requires.
# AUTO-MODE: classic is needed ONLY to pair. Once a bond exists that carries LE
# keys, classic is pure liability - iOS grabs the classic link, treats the phone
# as connected, and never opens the LE link ANCS requires. That single behaviour
# caused this outage three separate times.
#
# So: bond with LE keys -> LE ONLY.  No such bond -> DUAL, so pairing works.
# This flips itself, which means losing the bond can never lock anyone out.
BOND_LE=0
for inf in /var/lib/bluetooth/*/*/info; do
    [ -f "$inf" ] || continue
    if grep -q 'SupportedTechnologies=.*LE' "$inf" 2>/dev/null        && grep -q 'PeripheralLongTermKey\|LongTermKey' "$inf" 2>/dev/null; then
        BOND_LE=1
        break
    fi
done

WANT_MODE=dual
[ "$BOND_LE" = "1" ] && WANT_MODE=le
if ! grep -qE "^\s*ControllerMode\s*=\s*$WANT_MODE\s*$" /etc/bluetooth/main.conf 2>/dev/null; then
    log "no LE bond yet - switching to DUAL so the phone can pair" 
    [ "$WANT_MODE" = "le" ] && log "LE bond present - switching to LE ONLY (classic steals the ANCS link)"
    sed -i "s/^\s*ControllerMode\s*=.*/ControllerMode = $WANT_MODE/" /etc/bluetooth/main.conf
    systemctl restart bluetooth
    sleep 5
fi

LE_ONLY=0
if [ "$WANT_MODE" = "le" ]; then
    LE_ONLY=1
    log "ControllerMode=le - classic will be disabled (pairing needs dual; flip main.conf back to re-pair)"
    # btmgmt bredr-off is RACY on a freshly-started controller (the tty/timing
    # gotcha) and bluetoothd on this BlueZ build does NOT reliably honour
    # ControllerMode=le from main.conf on its own - verified live: br/edr came
    # back on 5h after a clean boot, which is exactly what "notifications
    # stopped working two weeks ago and never recovered" looked like. So retry
    # until `current settings` actually drops br/edr.
    for i in $(seq 1 8); do
        case "$(settings)" in
            *br/edr*) ;;
            *) break ;;
        esac
        [ "$i" -gt 1 ] && log "br/edr still on (try $i) - retrying bredr off"
        mgmt power off
        mgmt bredr off
        mgmt le on
        mgmt power on
        sleep 2
    done
    case "$(settings)" in
        *br/edr*) log "WARNING: could not disable br/edr - iOS may grab the classic link and ANCS will never attach" ;;
        *) log "br/edr disabled - LE only" ;;
    esac
fi

REQUIRED_FLAGS="br/edr ssp connectable bondable"
[ "$LE_ONLY" = "1" ] && REQUIRED_FLAGS="connectable bondable le"

need=""
for flag in $REQUIRED_FLAGS; do
    case "$(settings)" in
        *"$flag"*) ;;
        *) need="$need $flag" ;;
    esac
done

if [ -n "$need" ]; then
    log "asserting:$need"
    mgmt power off
    [ "$LE_ONLY" = "1" ] || mgmt bredr on
    mgmt ssp on
    mgmt connectable on
    mgmt bondable on
    mgmt le on
    mgmt power on
    sleep 2
fi

# CLASS OF DEVICE - this decides whether iOS offers "Share System Notifications"
#
# A desktop host advertises itself as Computer/Laptop (the mini booted as
# 0x00010c). iOS does not offer notification sharing to a device it classifies
# as a COMPUTER - it treats it as a peer machine rather than an accessory, so
# the toggle simply never appears under the (i) and ANCS can never be granted.
# The BirdThing Pi, which worked, presented 0x400000 = Miscellaneous device
# with the Telephony service class. Match that exactly.
#
# main.conf's `Class` only carries the device-class half; the service-class bits
# have to be written over HCI, and bluetoothd resets them on restart - so assert
# the full 24-bit value here on every boot.
WANT_CLASS="${WANT_CLASS:-0x400000}"
cur_class="$(hciconfig -a "$ADAPTER" 2>/dev/null | sed -n 's/.*Class: \(0x[0-9a-fA-F]*\).*/\1/p' | head -1)"
if [ "$cur_class" != "$WANT_CLASS" ]; then
    log "class of device $cur_class -> $WANT_CLASS"
    hciconfig "$ADAPTER" class "$WANT_CLASS" 2>/dev/null
    sleep 1
fi
log "class: $(hciconfig -a "$ADAPTER" 2>/dev/null | grep -m1 'Class:')"

log "$(settings)"
s="$(settings)"
for flag in $REQUIRED_FLAGS; do
    case "$s" in
        *"$flag"*) ;;
        *) log "WARNING: '$flag' missing - iPhone pairing/auto-reconnect will fail" ;;
    esac
done

# =====================================================================
# LE CONNECTION PARAMETERS - why the link kept dying after 2-3 seconds
# =====================================================================
# This Pi ends up as the CENTRAL for the ANCS link, so IT chooses the
# connection parameters - and BlueZ's defaults are far too aggressive for a
# link across a room:
#
#   supervision_timeout = 42  ->  420 ms
#
# At a 48.75 ms connection interval that means missing about NINE connection
# events kills the link. Captured on air as a stream of
#   Disconnect Complete ... Reason: Connection Failed to be Established (0x3e)
# every 2-3 seconds whenever the phone was at normal room distance
# (RSSI -84 to -87), which is why notifications "worked for a few minutes"
# and then never again.
#
# 500 = 5000 ms lets the link ride out five seconds of bad RF instead of
# four hundred milliseconds. debugfs does NOT persist, so assert it on boot.
LE_SUPERVISION_TIMEOUT="${LE_SUPERVISION_TIMEOUT:-500}"
DBG=/sys/kernel/debug/bluetooth/$ADAPTER
if [ -w "$DBG/supervision_timeout" ]; then
    echo "$LE_SUPERVISION_TIMEOUT" > "$DBG/supervision_timeout" 2>/dev/null
    echo 24 > "$DBG/conn_min_interval" 2>/dev/null
    echo 40 > "$DBG/conn_max_interval" 2>/dev/null
    echo 0  > "$DBG/conn_latency"      2>/dev/null
    log "LE supervision timeout: $(cat "$DBG/supervision_timeout")0 ms"
else
    log "WARNING: $DBG/supervision_timeout not writable - the LE link will use"
    log "         BlueZ's 420ms default and will drop on any weak signal"
fi

log "done"
