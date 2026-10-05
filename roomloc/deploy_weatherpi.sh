#!/bin/bash
# Deploy roomloc hub + node on the weather Pi (also the Home Assistant host).
#   IRK=<32 hex chars> PHONE_ADDR=<AA:BB:..> ./deploy_weatherpi.sh
# The IRK is a permanent tracking identifier for the phone -- keep it out of
# this repo. Lift it from a box the phone is already bonded to:
#   sudo grep -A1 IdentityResolvingKey /var/lib/bluetooth/*/*/info
set -e
: "${IRK:?set IRK}"
: "${PHONE_ADDR:?set PHONE_ADDR}"
HUB=http://127.0.0.1:8093

sudo mkdir -p /opt/roomloc
sudo cp /tmp/roomloc_hub.py /tmp/roomloc_node.py /opt/roomloc/
sudo chmod +x /opt/roomloc/*.py

sudo tee /etc/systemd/system/roomloc-hub.service >/dev/null <<EOF
[Unit]
Description=roomloc hub (phone room fingerprint arbiter)
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/roomloc/roomloc_hub.py --port 8093 --nodes weatherpi,birdpi --store /opt/roomloc/fingerprints.json
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/roomloc-node.service >/dev/null <<EOF
[Unit]
Description=roomloc BLE node (weatherpi)
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/bin/python3 /opt/roomloc/roomloc_node.py --irk $IRK --node weatherpi --hub $HUB --identity-addr $PHONE_ADDR --identity-type 1
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now roomloc-hub roomloc-node
sleep 4
systemctl is-active roomloc-hub roomloc-node
