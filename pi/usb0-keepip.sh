#!/bin/bash
# Keep the Pi's usb0 IP present so Car Thing RNDIS link flaps never break the connection.
while true; do
  ip link set usb0 up 2>/dev/null
  ip -4 addr show usb0 2>/dev/null | grep -q "192.168.7.1" || ip addr add 192.168.7.1/24 dev usb0 2>/dev/null
  sleep 5
done
