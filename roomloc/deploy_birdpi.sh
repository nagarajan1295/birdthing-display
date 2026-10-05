#!/bin/bash
# Deploy the roomloc BLE node on the BirdThing Pi (living room).
#   IRK=<32 hex chars> PHONE_ADDR=<AA:BB:..> ./deploy_birdpi.sh   (see deploy_weatherpi.sh)
set -e
: "${IRK:?set IRK}"
: "${PHONE_ADDR:?set PHONE_ADDR}"
HUB=http://192.168.1.29:8093

sudo mkdir -p /opt/roomloc
sudo cp /tmp/roomloc_node.py /opt/roomloc/
sudo chmod +x /opt/roomloc/roomloc_node.py

sudo tee /etc/systemd/system/roomloc-node.service >/dev/null <<EOF
[Unit]
Description=roomloc BLE node (birdpi)
After=bluetooth.service
Requires=bluetooth.service

[Service]
ExecStart=/usr/bin/python3 /opt/roomloc/roomloc_node.py --irk $IRK --node birdpi --hub $HUB --identity-addr $PHONE_ADDR --identity-type 1
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now roomloc-node
sleep 4
systemctl is-active roomloc-node
