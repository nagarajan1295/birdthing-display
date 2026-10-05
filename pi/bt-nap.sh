#!/bin/bash
# BirdThing Pi Bluetooth PAN access point (NAP). Run when ready to pair a Car Thing over BT.
# Brings up hci0, a br0 bridge (192.168.44.1/24) with dnsmasq, registers a BlueZ NAP server,
# and makes the Pi discoverable/pairable with a Just-Works agent. Does NOT touch eth0/usb0.
set -e
# Unblock the BT radio (no rfkill binary on this image; use sysfs)
for r in /sys/class/rfkill/*; do
  [ "$(cat $r/type 2>/dev/null)" = "bluetooth" ] && echo 0 > "$r/soft" 2>/dev/null || true
done
hciconfig hci0 up
# Bridge + DHCP for PAN clients
brctl addbr br0 2>/dev/null || true
ip addr add 192.168.44.1/24 dev br0 2>/dev/null || true
ip link set br0 up
pkill -f "dnsmasq.*br0" 2>/dev/null || true
/usr/sbin/dnsmasq --interface=br0 --bind-interfaces --except-interface=lo \
  --dhcp-range=192.168.44.10,192.168.44.50,255.255.255.0,1h \
  --dhcp-option=3,192.168.44.1 --pid-file=/run/btnap-dnsmasq.pid
# Discoverable + Just-Works agent
bluetoothctl --timeout 4 <<BT
power on
discoverable on
pairable on
agent NoInputNoOutput
default-agent
BT
# NAP server: incoming PAN connections get enslaved to br0 -> DHCP from dnsmasq
pkill -f "bt-network -s" 2>/dev/null || true
setsid bt-network -s nap br0 >/tmp/btnap.log 2>&1 </dev/null &
sleep 1
echo "NAP up. Pi BT: $(hciconfig hci0 | grep -o 'BD Address: [0-9A-F:]*')  br0: 192.168.44.1"
echo "To pair from a Car Thing (BlueZ): scan, pair DC:A6:32:62:53:01, trust, then PAN-connect"
echo "  (BlueZ 5.49 client w/o bt-network: D-Bus org.bluez.Network1.Connect uuid 'nap')."
