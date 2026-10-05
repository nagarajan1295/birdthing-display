# roomloc — follow-me Bluetooth audio for an iPhone, with no new hardware

Walk from the bathroom to the kitchen and the music follows you: the iPhone's
Bluetooth stream hands off from the bathroom Echo to the kitchen Echo on its own.

Everything runs on boxes that were already in the house. Nothing is installed on
the phone, and no app has to be open.

## Why this is possible at all

iOS gives you no way to script a Bluetooth connection, so the phone cannot be
the thing that switches. **The Echo is.** An Echo can be told to connect to a
phone it is already paired with, and iOS routes audio to a Bluetooth sink the
moment that sink connects. So the whole system is server-side:

```
iPhone BLE adverts / live link
        |
        v
  roomloc nodes (one per Pi)   -- resolve the phone, measure RSSI
        |  POST
        v
  roomloc hub                  -- fingerprint match -> which room
        |  REST sensor
        v
  Home Assistant               -- sensor.phone_room
        |  media_player.select_source (Alexa Media Player)
        v
  Echo connects over Bluetooth -- iOS follows the new sink
```

## The two hard problems, and what solves them

**1. iPhones hide from BLE scanners.** They advertise under a private address
that rotates every ~15 minutes, so you cannot just watch for a MAC. But any box
the phone has *bonded* with holds its Identity Resolving Key, and that key
reverses the rotation. Lift the IRK once and the phone is trackable forever:

```bash
sudo grep -B12 -A1 IdentityResolvingKey /var/lib/bluetooth/*/*/info
```

BlueZ stores it big-endian; the crypto wants it reversed. `roomloc_node.py`
handles that — do not "fix" the reversal.

**2. A connected phone stops advertising.** If a box holds a BLE link to the
phone (an ANCS notification gateway, say), that box sees *zero* adverts and
reads nothing — exactly the node you most want a reading from. The fix is to
read RSSI off the live link instead, via the BlueZ management socket
(`MGMT_OP_GET_CONN_INFO`). `hcitool rssi` cannot do this: it resolves handles on
BR/EDR only and fails on an LE-only link with "No such file or directory".
Nodes run both paths and prefer whichever is live.

## Fingerprints, not distances

There is no anchor in the kitchen or the bathroom, and there does not need to be.
The hub never asks "how far away is the kitchen". It asks "does the current
RSSI vector across all nodes look more like the one recorded while standing in
the kitchen, or the one recorded in the bathroom". Two anchors on opposite sides
of the house separate two rooms cleanly, because what matters is the *difference*
between nodes, not any absolute number.

Calibrate by walking: open `http://<hub>:8093/` on the phone, stand in a room,
type its name, tap Capture. Do each room two or three times, including an
`elsewhere` catch-all so the system can tell you are in neither.

A room change commits by **rolling vote**: each second's reading either names a
room (if it beats the runner-up by `--margin` dB) or abstains, and a room is
committed once it holds `--vote-share` of the last `--dwell` seconds. Do not
replace this with a dwell timer that resets when the instantaneous winner
changes — RSSI flips winners constantly, such a timer never accumulates, and the
decision freezes on whatever it saw first.

**Check the calibration before trusting it.** `GET /quality` reports each room's
centroid, how far apart the rooms sit, and any capture that landed closer to a
different room's centre. Those misfits matter more than they look: rooms are
scored by their *nearest* fingerprint, so one stray capture parked in the
kitchen's signal space claims every kitchen reading for the bathroom. `POST
/prune` drops them. Separation below ~8 dB means the rooms are not reliably
distinguishable — recapture, or add an anchor.

Hold the phone the same way you normally carry it when capturing. A body between
phone and Pi is worth ~20 dB, which is larger than the gap between many rooms.

## Layout

| file | where it runs |
|---|---|
| `roomloc_node.py` | every box with a Bluetooth radio |
| `roomloc_hub.py` | one box (here: alongside Home Assistant), port 8093 |
| `ble_probe.py` | one-shot "can this box see the phone at all" check |
| `connrssi_test.py` | one-shot live-link RSSI check |
| `ha_rest_sensor.yaml` | goes in HA's `rest:` block |
| `ha_automations.yaml` | the Echo handoff; fill in the `<<...>>` entity ids |

Stock deps only: `python3-dbus`, `python3-gi`, `python3-cryptography`,
`python3-requests`. No pip, no MQTT, no broker.

## Setup

```bash
IRK=<32 hex> PHONE_ADDR=<AA:BB:CC:DD:EE:FF> ./deploy_weatherpi.sh
IRK=<32 hex> PHONE_ADDR=<AA:BB:CC:DD:EE:FF> ./deploy_birdpi.sh
```

The IRK is a permanent tracking identifier for the phone. Keep it in the
systemd unit on the box, never in this repo.

Then, in Home Assistant: add Alexa Media Player (HACS) with the Amazon account
the Echoes are on, pair the phone to each Echo once by hand, read each Echo's
`source_list` to get the phone's Bluetooth name, and fill in
`ha_automations.yaml`.

## Gotchas

- **iOS auto-switching must stay on.** Settings > General > AirPlay & Continuity
  has a toggle that stops audio moving to newly connected devices. If that is
  enabled, nothing here can route audio and the handoff silently does nothing.
- **Connect the new Echo before disconnecting the old one.** With no Bluetooth
  sink at all, iOS falls back to the phone speaker and will not auto-resume.
- **Bind before setsockopt on an HCI socket**, and note that `HCI_FILTER` is
  rejected outright on some kernels — which is why the mgmt socket path exists.
