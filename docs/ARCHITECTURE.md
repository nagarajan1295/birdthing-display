# BirdThing architecture

## Audio path
1. **Car Thing PDM mic.** The mic produces nothing until two ALSA controls are set: numid 15
   "Audio In Source" → `PDMIN`, and numid 6 "PDM Train" (a self‑clearing trigger). There's no
   `arecord`/`amixer` on the device, so `carthing/birdmic_ct.py` sets them via the ALSA *control*
   API (ctypes) and captures `hw:0,0`.
2. **Auto‑gain + stereo.** The mic is quiet and varies with distance, so the daemon applies a
   smoothed auto‑gain (targets a healthy peak, with a noise gate) and duplicates mono → stereo to
   match BirdNET's `arecord -c2`.
3. **TCP push.** The daemon opens **one** TCP connection to the Pi (`192.168.7.1:9000`) and streams
   S16LE/48k/stereo. (Earlier designs that had the Pi SSH into the Car Thing repeatedly wedged its
   sshd — push from the Car Thing avoids that entirely.) The daemon self‑heals: if PDM capture
   stalls, `snd_pcm_wait` times out and it reopens the device.
4. **Pi receiver** (`pi/birdthing_recv.py`) reads the socket and writes to a persistent
   `aplay -D hw:Loopback,0,0`. Single writer only — an earlier two‑writer design deadlocked.
5. **ALSA loopback** mirrors `hw:Loopback,0,0` → `hw:Loopback,1,0`, which **BirdNET‑Pi** records
   and analyzes, writing detections to its SQLite DB.

## Display path
- `pi/birdthing_api.py` (stdlib HTTP server, `:8090`) serves:
  - `/` the dashboard, `/assets/*` the bundled Inter font.
  - `/api/detections` (recent, with today counts), `/api/bydate` (per‑day per‑bird counts),
    `/api/stats` (hourly activity + top species), `/api/info` (Wikipedia facts),
    `/api/image` (cached Wikipedia photo), `/api/weather` + `/api/geocode` (Open‑Meteo).
- `dashboard/birdthing-dashboard.html` is an 800×480 single‑page app: hero + scrollable list,
  facts panel, bird gallery, live view, settings, and a tappable weather widget.

## Car Thing inputs (`carthing/birdknob_ct.py`)
- Top buttons + knob‑press + back reach Chromium as **keyboard keys** (the device exposes them via
  a kbd handler) → the dashboard's keydown handler uses them (1/2/3/4 = views, Enter = facts).
- The **rotary knob** has no kbd handler, so the daemon reads `/dev/input/event1` and dispatches
  Arrow keys to the page through the **Chrome DevTools protocol** (`:9222`).
- The **'m' button** toggles the screen (writes `/tmp/display_off`).
- A tiny HTTP control server (`127.0.0.1:8091`) lets the Settings page set brightness.

## Brightness (`carthing/setup_backlight.sh`)
Reads the **tmd2772 ambient light sensor** and drives `/sys/class/backlight/aml-bl/brightness`
(auto), or a fixed Low/Mid/High from `/tmp/bt_bright`, or 0 when `/tmp/display_off` exists.

## Resilience
- `birdthing-usb0` keeps the Pi's `usb0` IP across RNDIS flaps.
- The mic daemon self‑heals PDM stalls; the receiver respawns `aplay` if it dies; everything is a
  systemd service that auto‑restarts and survives reboots.
