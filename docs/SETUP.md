# BirdThing setup (v1.0, USB)

This guide builds BirdThing on a Raspberry Pi + a Spotify Car Thing connected by USB.

## 0. Prerequisites
- Raspberry Pi (4B recommended) on your LAN, with SSH and passwordless `sudo`.
- A Spotify Car Thing flashed with the
  [bishopdynamics Debian kiosk](https://github.com/bishopdynamics/superbird-debian-kiosk),
  connected to the Pi with a USB‑A↔USB‑C cable. It appears to the Pi as `192.168.7.2`
  (user/pass `superbird`/`superbird`).

## 1. BirdNET‑Pi on the Pi
Install [BirdNET‑Pi](https://github.com/Nachtzuster/BirdNET-Pi) and set your latitude/longitude in
`/etc/birdnet/birdnet.conf`. Confirm the web UI loads and `birdnet_analysis` / `birdnet_recording`
run.

## 2. ALSA loopback (the "virtual cable")
Load `snd-aloop` at boot so audio written to `hw:Loopback,0,0` comes out at `hw:Loopback,1,0`:
```bash
echo snd-aloop | sudo tee /etc/modules-load.d/birdthing-aloop.conf
sudo modprobe snd-aloop
```
Point BirdNET at the capture end and use stereo to match:
```bash
sudo sed -i 's/^REC_CARD=.*/REC_CARD=hw:Loopback,1,0/; s/^CHANNELS=.*/CHANNELS=2/' /etc/birdnet/birdnet.conf
```
**Gotcha:** always use the explicit 3‑part device names `hw:Loopback,0,0` ↔ `hw:Loopback,1,0`.
The `hw:CARD=Loopback,DEV=0` form leaves the subdevice unpinned and the loopback passes silence.

Optional, lower latency (birds appear in ~4s instead of ~15s):
```bash
sudo sed -i 's/^RECORDING_LENGTH=.*/RECORDING_LENGTH=3/' /etc/birdnet/birdnet.conf
```
Optional, only show real birds (drop Engine/Gun/Siren/Dog/Human/etc.):
```bash
# write those labels (from scripts/labels.txt) into scripts/exclude_species_list.txt
```

## 3. BirdThing Pi services
Copy the `pi/` and `dashboard/` files to `/opt/birdthing/` on the Pi and install the services:
```bash
sudo mkdir -p /opt/birdthing/assets
sudo cp pi/*.py pi/*.sh /opt/birdthing/
sudo cp dashboard/birdthing-dashboard.html /opt/birdthing/
sudo cp dashboard/assets/*.woff2 /opt/birdthing/assets/
sudo cp pi/*.service /etc/systemd/system/
sudo chmod +x /opt/birdthing/*.sh
sudo systemctl daemon-reload
sudo systemctl enable --now birdthing-usb0 birdthing-recv birdthing-api
sudo systemctl restart birdnet_recording   # so it records from the loopback
```
- `birdthing-recv`  — listens on TCP 9000, feeds Car Thing audio into `aplay → hw:Loopback,0,0`.
- `birdthing-api`   — serves the dashboard + detections/photos/weather on `:8090`.
- `birdthing-usb0`  — keeps `192.168.7.1/24` on `usb0` across link flaps.

## 4. Car Thing daemons + kiosk
Copy the `carthing/` files to `/opt/birdthing/` on the Car Thing (via the Pi):
```bash
# from the Pi:
sshpass -p superbird scp carthing/birdmic_ct.py carthing/birdknob_ct.py \
  carthing/setup_backlight.sh superbird@192.168.7.2:/tmp/
sshpass -p superbird ssh superbird@192.168.7.2 'sudo mkdir -p /opt/birdthing && \
  sudo cp /tmp/birdmic_ct.py /tmp/birdknob_ct.py /opt/birdthing/ && \
  sudo cp /tmp/setup_backlight.sh /scripts/setup_backlight.sh'
```
Install the systemd units (`carthing/*.service`) for `birdmic` (mic streamer) and `birdknob`
(knob/button/brightness bridge), enable them, and restart `backlight.service`.

Point the kiosk at the dashboard — edit `/scripts/chromium_settings.sh` on the Car Thing:
```
URL="http://192.168.7.1:8090/"
```
then `sudo systemctl restart chromium.service`.

## 5. Done
Birds at the window now appear on the Car Thing screen within seconds. Use the **knob** to scroll,
**press** for facts, and the **top buttons**: 1 = bird info, 2 = all birds, 3 = live, 4 = settings.

> Notes on this Car Thing image: its package manager is damaged (don't `apt install`); its `/run`
> tmpfs mis‑reports size so `systemctl daemon-reload` is refused — load new units by rebooting.
> RAM is ~500 MB, so keep the kiosk page light (this dashboard is).
