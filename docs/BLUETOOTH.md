# BirdThing v2.0 — Bluetooth (in progress)

Goal: replace the USB cable between the Car Thing and the Pi with a **Bluetooth PAN** link, so the
Car Thing only needs power.

## Why it isn't just a config change
The bishopdynamics Debian kiosk image (BirdThing v1.0) **cannot do Bluetooth**: its device tree
only exposes the BT chip's *wake* GPIOs, not a power‑enable line, so the Broadcom combo chip never
powers on (every HCI command times out). We confirmed the **hardware works** on a stock‑OS Car
Thing — so the fix is a **device‑tree / boot‑image change**, i.e. a reflash.

## Plan: flash a spare to nixos‑superbird (no build needed)
[nixos‑superbird](https://github.com/JoeyEamigh/nixos-superbird) is general‑purpose NixOS for the
Car Thing whose kernel + config have **Bluetooth + PAN and PDM audio working**. A **prebuilt**
image exists, so no multi‑hour build:

1. **Back up the spare's stock firmware first** (so you can always revert):
   ```
   python superbird_tool.py --dump_device .\stock_backup   # ~110 min
   ```
2. **Flash** the prebuilt chrome‑kiosk installer with
   [superbird‑tool](https://github.com/thinglabsoss/superbird-tool):
   - Hold preset buttons **1 + 4** while plugging USB → burn mode (device `1b8e:c003`).
   - On Windows, bind **libusb‑win32** to it with **Zadig** (one time).
   - `python superbird_tool.py --restore_device <unzipped installer>` (~11 min; `--slow_burn`
     if it stalls).
3. **Boot + SSH** (`ssh root@172.16.42.2`, no password). NixOS runs a DHCP server on `172.16.42.1`.
4. **Port BirdThing**: in a `nixos-superbird-template` flake, set `superbird.gui.kiosk_url` to the
   dashboard, `superbird.bluetooth.enable = true`, and add a systemd service for `birdmic_ct.py`
   (the mic capture). Develop live over SSH, then rebuild the image for reproducibility (WSL2 +
   Nix + qemu binfmt; needs ~8 GB RAM / 40–60 GB disk).

## Pi side is ready
`pi/bt-nap.sh` brings up the Pi as a **BT‑PAN access point** (bridge `br0` 192.168.44.1/24 +
dnsmasq + BlueZ NAP + discoverable). Once a Car Thing pairs and PAN‑connects, repoint
`birdmic_ct.py`'s `PI_HOST` and the kiosk URL to `192.168.44.1`.

> Keep the USB version working as the fallback until Bluetooth is proven on the new image.
