# ANCS — iPhone notifications on the house displays

Puts iPhone notifications — **including incoming calls and message
previews** — on the BirdThing dashboard, the WeatherThing Car Thing and the
bedroom weather kiosk. No app, no cloud, no extra hardware.

## How it works

Apple publishes **ANCS** (Apple Notification Center Service), the BLE service
fitness bands use. The gateway host advertises itself as a BLE peripheral
*soliciting* ANCS; once the iPhone pairs and grants notification access, the
host becomes a GATT client against the phone and receives every notification
the phone shows: app id, title (the caller/sender), the message body, and a
distinct `IncomingCall` category.

```
iPhone --BLE/ANCS--> Mac mini (gateway :8099) --HTTP--> the three displays
```

One radio serves all three screens: the gateway holds notifications in memory
and every display reads the same JSON.

## Host: the BirdThing Pi (moved to the Mac mini and back, 2026-08-13)

It briefly ran on the Mac mini. **That was a mistake, for two reasons worth
recording** — the section below explains why the mini looked attractive:

1. **A desktop advertises Class of Device `0x00010c` = Computer/Laptop, and iOS
   will not offer "Share System Notifications" to a device it classifies as a
   COMPUTER.** It treats it as a peer machine rather than an accessory, so the
   toggle never appears and ANCS can never be granted. The Pi presents
   `0x400000` (Miscellaneous device, Telephony service class), which is
   accessory-shaped. `ancs-prep.sh` now asserts that value on every boot.
   **Check `hciconfig -a hci0 | grep Class` first whenever this moves hosts.**
2. **Location beats antenna quality.** The mini has the better radio — a
   dedicated USB BCM20702 on the Mac's own antenna — but it is in the wrong
   room. BLE is ~10 m; the Pi is where the user actually sits.

The mini's install is left in place but disabled, so it is one
`systemctl enable --now ancs-prep ancs-gateway` away if it is ever wanted.

## Why the mini looked like a good idea (2026-08-13)

It first ran on the **BirdThing Pi** and was unreliable there: the phone never
reconnected on its own, and connecting it by hand produced no notifications.
It now runs on the **Mac mini** (the Sentry host, `192.168.1.72`).

Why the mini is the better host:

- its Broadcom **BCM20702** (Apple `05ac:8289`, HCI 4.0) is a **dedicated USB
  radio on the Mac's own antenna**, not the Pi's SoC radio sharing a PCB
  antenna with WiFi
- its Bluetooth was **completely unused — zero bonds**, so there is nothing to
  collide with
- **the address clash disappears entirely.** The Pi's BD address
  `DC:A6:32:62:53:01` is the one the bedroom Pi *spoofs* to serve the
  WeatherThing over BT PAN, so running classic Bluetooth on the Pi meant two
  boxes answering paging on the same address — "mitigated, not eliminated".
  The mini is `D4:DC:CD:F3:9B:89`, unrelated to anything.

Bluetooth on the BirdThing Pi is now disabled again (`ancs-gateway`,
`ancs-prep` and `bluetooth.service` all disabled, the iPhone bond removed),
which restores the state the WeatherThing wants. The bedroom Pi's adapter is
deliberately **not** touched — it is a serdev-attached BCM4345C0 with a long
history of wedging, and the WeatherThing depends on it.

### Keep the phone from treating the mini as an audio device

The mini is a desktop, so it advertised A2DP, AVRCP and Hands-Free. An iPhone
paired to a host offering **Hands-Free will route call audio to it** — into a
headless kiosk in another room. Both halves have to be turned off, and they
live in different places:

- A2DP/AVRCP come from **bluetoothd plugins** →
  `bluetoothd --noplugin=a2dp,avrcp,sap` (systemd drop-in)
- HFP/HSP are registered by **WirePlumber** via `org.bluez.ProfileManager1`, so
  `--noplugin` cannot reach them → `monitor.bluez = disabled` in
  `~/.config/wireplumber/wireplumber.conf.d/50-disable-bluez.conf`

## Dual mode is required

ANCS is pure BLE, so LE-only *ought* to work. **It does not.** iOS will not
list a pure-BLE peripheral in Settings → Bluetooth however correct its
solicitation is — verified on air: AD type `0x15` carrying the real ANCS UUID,
100 ms interval, connectable, named, and the iPhone showed nothing. Classic
Bluetooth must be available for the phone to discover and keep the accessory.
Once connected, the ANCS link itself rides LE (`hcitool con` shows
`LE … AUTH ENCRYPT`), and pairing over classic derives the LE keys via CTKD.

## THE RECONNECT BUG — why it used to be "hit or miss"

This is the one that made the whole thing feel broken, and the cause was in
the gateway, not the radio.

The old version paged bonded phones itself every 30 s with
`Device1.Connect()`. For a **dual-mode bond** — which is what pairing through
iOS Settings always produces, via CTKD — BlueZ routes `Connect()` over
**BR/EDR**, and the iPhone exposes no classic profile this host wants. So
every attempt failed with:

```
org.bluez.Error.Failed: br-connection-profile-unavailable
```

**9 600+ consecutive failures were logged on the Pi** (one every 30 s for
~80 hours, `linked:false` throughout). BlueZ does **not** fall back to LE
after that error, so the transport ANCS actually needs was never tried. An
earlier version of this README called that error "harmless" — it was not.

The fix is to use the model every real ANCS accessory uses: **keep a
connectable LE advertisement soliciting ANCS on the air and let iOS reconnect
to it.** `ensure_advertising()` re-registers the advertisement whenever BlueZ
releases it (adapter power cycle, daemon `Release()`, controller reset) —
if the advert lapses the phone has nothing to reconnect *to*, which looks
exactly like "sometimes it works". The direct `Connect()` is kept only as a
rate-limited (5 min) fallback so it cannot flood the log or keep the
controller busy paging a phone that is out of range.

### Other traps worth knowing

**`btmgmt` silently ignores its command when it has no tty** — it exits 0
having done nothing, and under systemd it blocks until the unit times out.
Every `btmgmt` call must go through a pty: `script -qec "btmgmt …" /dev/null`.

**Power-cycling `bredr` clears `ssp`.** Without Secure Simple Pairing the
adapter offers only legacy PIN pairing and iOS will not pair with it at all —
while still looking healthy in `hciconfig`. Always check that `current
settings` contains **both** `br/edr` and `ssp`.

**`connectable` and `bondable` are not implied by anything.** `connectable` is
page scan: without it iOS *cannot initiate a reconnection* and you must connect
by hand every time. The Mac mini booted with **neither** set (`current
settings: powered ssp br/edr le secure-conn`), so pairing would have failed
outright. `ancs-prep.sh` asserts both on every boot.

**`btmon` labels AD type `0x15` (solicitation) and `0x06`/`0x07` (plain service
UUID lists) identically** as "128-bit Service UUIDs", so a capture cannot tell
you whether the solicitation really went out. `decode_solicit.py` reads the
actual type byte out of a `btmon -w` capture:

```bash
sudo btmon -w /tmp/adv.btsnoop &   # then restart the gateway
sudo python3 decode_solicit.py /tmp/adv.btsnoop
# offset 868: len=0x11 type=0x15  128-bit SERVICE SOLICITATION  <-- correct
```

## Files

| file | what it is |
| --- | --- |
| `ancs_gateway.py` | the gateway: BlueZ D-Bus advertising + agent, ANCS GATT client, JSON API on `:8099` |
| `ancs-prep.sh` | clears any rfkill soft-block, waits for `hci0`, asserts `br/edr ssp connectable bondable le` every boot |
| `decode_solicit.py` | reads the real AD type byte out of a `btmon` capture (see above) |
| `ancs-prep.service` / `ancs-gateway.service` | systemd units (both enabled) |
| `notify_widget.html` | the toast — self-contained CSS+JS, injected into each display |
| `patch_display.py` | idempotently injects the toast into a display's HTML |
| `patch_api_route.py` | idempotently adds a same-origin `/api/notify` proxy to a Pi's API server |

## Deploy

```bash
sudo mkdir -p /opt/ancs && sudo cp ancs_gateway.py ancs-prep.sh /opt/ancs/
sudo cp ancs-prep.service ancs-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bluetooth ancs-prep ancs-gateway
```

`/etc/bluetooth/main.conf` needs, under `[General]`:

```ini
ControllerMode = dual
JustWorksRepairing = always
Experimental = true
```

Needs `python3-dbus` and `python3-gi`. `Experimental = true` is what makes
BlueZ honour the advertising-interval properties — without it BlueZ advertises
every 1280 ms, slow enough that an iOS Settings scan can take a very long time
to notice the host. `JustWorksRepairing = always` is what lets a phone that
tapped *Forget This Device* pair again.

On a desktop host, also disable the Bluetooth audio profiles (see above) so
the phone cannot route call audio to it.

## Pairing

iPhone → Settings → Bluetooth → **BirdThing Hub** under *Other Devices* →
pair, then allow *Share System Notifications* on the prompt (or enable it
later under the ⓘ). Range is ~10 m, so the phone only feeds the gateway while
it is near the mini.

The host stays discoverable whenever **no phone is linked**, so you can always
re-pair from the phone alone. To force a window anyway:

```bash
curl "http://192.168.1.250:8099/api/pair?mins=20"
```

### Automatic reconnection — what is and is not possible

**The phone is the initiator, not the Pi.** For ANCS the accessory is the BLE
*peripheral*: it advertises, and iOS (the *central*) connects to it. So the Pi
cannot "page" the iPhone the way a Bluetooth speaker gets reconnected — the
iPhone exposes no classic profile to connect to, which is what
`br-connection-profile-unavailable` was telling us for 9 600 attempts.

What makes reconnection automatic in practice, all now enforced:

- a **connectable** advertisement soliciting ANCS, always on the air at
  100–150 ms, re-registered by a watchdog whenever BlueZ releases it
- **`connectable` (page scan) asserted on every boot** — losing it is the
  single most common cause of "I have to connect it by hand every time", and it
  is not implied by `br/edr` or `ssp`
- an **LE bond** (`bond_le.le == true`), without which iOS has nothing to
  reconnect *to* over the transport ANCS needs
- `Trusted=true` on the device, so no authorisation prompt blocks it

One genuine iOS behaviour to know: if you tap **Disconnect** in Settings →
Bluetooth, iOS deliberately will not reconnect to that accessory until you tap
it again. Out-of-range → back-in-range does reconnect on its own.

**If you tap "Forget This Device" on the phone, clear the host's side too** —
otherwise the bond is one-sided and pairing walls up in a
`LinkKeyRequest → NegativeReply → disconnect` loop (the same failure that cost
days on the Car Thing):

```bash
sudo bluetoothctl remove <phone-mac>
```

## The displays

Each display polls a URL every 2.5 s. The two Car Things have no route to the
gateway's host — the BirdThing CT reaches only its own Pi over USB, the
WeatherThing CT only its Pi over the BT PAN link — so each Pi's existing API
server proxies the gateway on the origin the page was loaded from.

| display | endpoint |
| --- | --- |
| BirdThing dashboard (birdpi `:8090`) | `/api/notify` → `127.0.0.1:8099` |
| WeatherThing (bedroom Pi `:8090`) | `/api/notify` → `192.168.1.250:8099` |
| bedroom kiosk (bedroom Pi `:8080`) | `/api/notify` → `192.168.1.250:8099` |

**Every display goes through a same-origin proxy — none of them fetches the
gateway directly any more.** The kiosk used to, and it was silently broken: the
cross-origin request was blocked by the browser and the widget swallows every
fetch error (`.catch(function(){})`), so there was no toast and no clue. If a
display ever shows nothing, check its proxy with `curl` before suspecting
Bluetooth.

Chromium also serves `index.html` from its own HTTP cache across kiosk
restarts, so a patched page can keep running the old one; the kiosk server now
sends `Cache-Control: no-store` for it.

The first poll after a page load only seeds: a reload never replays the
backlog. Incoming calls stay on screen until the phone says the call ended;
everything else clears after 9 s. Notification text is rendered with
`textContent`, never `innerHTML`.

## API

- `GET /api/notifications` — recent notifications + `linked` state
- `GET /healthz`
- `GET /api/status` — **start here when something is wrong.** Reports whether
  the advertisement is actually registered, what is bonded (with RSSI), the
  last reconnect error, whether a phone is connected *without* ANCS attached,
  `notifying` per ANCS characteristic, `class_of_device` (flagged if it reads
  as a COMPUTER), `bond_le`, and a plain-English `verdict`

### `bond_le` — the check that catches a silently useless pairing

Pairing happens over classic; the LE keys are supposed to be derived from it by
**CTKD**. When that does not happen the bond is BR/EDR only, the phone shows as
paired *and connected*, and ANCS can never work, because ANCS rides LE. A good
bond looks like:

```json
{"technologies": "BR/EDR;LE", "le": true,
 "long_term_key": true, "identity_resolving_key": true}
```

`"le": false` means forget the device on the phone, `bluetoothctl remove` it
here, and pair again. A one-sided bond walls up exactly like the Car Thing saga.
- `GET /api/pair?mins=20` — re-open classic discoverability to (re-)pair a phone
- `GET /api/test?cat=IncomingCall&app=Phone&title=X&message=Y` — inject a
  synthetic notification to test the display chain without a real call
- `GET /api/test/clear`

## Config — `/etc/ancs-gateway.json` (optional)

```json
{"redact_body": false, "expire_sec": 240, "log_bodies": false}
```

Set `redact_body` to `true` to send sender only and never the message text —
worth considering, since these are always-on screens visible to anyone in the
room. Message bodies stay out of the journal unless `log_bodies` is set.

## Limits

- **Range.** BLE is ~10 m. Away from the mini, nothing arrives. This is the
  one thing moving hosts does not fix — it relocates the coverage, it does not
  extend it. Whichever room the mini is in is where the phone has to be.
- **Unencrypted on the LAN.** The gateway serves message text over plain HTTP
  to anything on the network.
- **The mini's IP is a DHCP lease** baked into three files — see above.
- iOS re-grants ANCS on reconnect; if notifications stop, check
  *Show Notifications* is still on under the ⓘ next to **BirdThing Hub**.
