#!/bin/bash
# Run as the desktop user (wireplumber is a USER service), not root.
set -u
echo "=== wireplumber version"
wireplumber --version 2>&1 | head -2
echo "=== is it running?"
systemctl --user is-active wireplumber pipewire 2>&1

CONF_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
mkdir -p "$CONF_DIR"
cat > "$CONF_DIR/50-disable-bluez.conf" <<'EOF'
# This machine's Bluetooth exists ONLY to receive iPhone notifications over
# ANCS (a GATT/LE service). It must never become the phone's audio route:
# an iPhone paired to a host advertising Hands-Free will happily send CALL
# AUDIO to it, and this host is a headless kiosk in another room.
#
# bluetoothd's --noplugin=a2dp,avrcp kills A2DP/AVRCP, but HFP/HSP are
# registered by WirePlumber via org.bluez.ProfileManager1, so they have to be
# turned off here instead.
#
# Reverse by deleting this file and: systemctl --user restart wireplumber
wireplumber.profiles = {
  main = {
    monitor.bluez = disabled
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
echo "wrote $CONF_DIR/50-disable-bluez.conf"
systemctl --user restart wireplumber 2>&1 || echo "(wireplumber restart failed)"
sleep 3
echo "=== remaining adapter UUIDs"
bluetoothctl show | grep -E 'UUID'
