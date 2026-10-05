#!/usr/bin/env python3
"""Probe: can this box see the iPhone's BLE adverts and resolve its rotating
private address with the IRK we lifted from BlueZ's bond store?

Prints every resolved hit with RSSI. Run for N seconds, then summarise.
Zero non-stock deps: python3-dbus + python3-cryptography.
"""
import sys, time, collections
import dbus
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

IRK_HEX = sys.argv[1]  # never default this: an IRK identifies a phone for life
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 60
LABEL = sys.argv[3] if len(sys.argv) > 3 else "node"

BLUEZ = "org.bluez"


def ah(key: bytes, r: bytes) -> bytes:
    """Bluetooth Core random address hash function ah(k, r) -> 24-bit."""
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (enc.update(b"\x00" * 13 + r) + enc.finalize())[-3:]


def resolves(irk: bytes, addr: str) -> bool:
    """True if addr (AA:BB:..) is a resolvable private address for this IRK."""
    mac = bytes.fromhex(addr.replace(":", ""))
    if len(mac) != 6 or (mac[0] & 0xC0) != 0x40:  # not an RPA
        return False
    return ah(irk, mac[:3]) == mac[3:]


def main():
    # BlueZ stores the IRK big-endian in the bond file; the crypto wants it
    # little-endian. Try both so a byte-order mistake can't silently kill this.
    raw = bytes.fromhex(IRK_HEX)
    candidates = {"as-is": raw, "reversed": raw[::-1]}

    bus = dbus.SystemBus()
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")

    # find an adapter
    objs = om.GetManagedObjects()
    adapter_path = next(p for p, i in objs.items() if "org.bluez.Adapter1" in i)
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), "org.bluez.Adapter1")

    try:
        adapter.SetDiscoveryFilter({"Transport": "le", "DuplicateData": dbus.Boolean(True)})
    except dbus.DBusException as e:
        print(f"[warn] SetDiscoveryFilter: {e}")
    try:
        adapter.StartDiscovery()
    except dbus.DBusException as e:
        print(f"[warn] StartDiscovery: {e} (another client may already be scanning)")

    hits = collections.defaultdict(list)
    seen_rpas = set()
    total_devices = set()
    deadline = time.time() + DURATION
    print(f"[{LABEL}] scanning on {adapter_path} for {DURATION}s ...", flush=True)

    while time.time() < deadline:
        for path, ifaces in om.GetManagedObjects().items():
            d = ifaces.get("org.bluez.Device1")
            if not d:
                continue
            addr = str(d.get("Address", ""))
            total_devices.add(addr)
            if str(d.get("AddressType", "")) != "random":
                continue
            for name, irk in candidates.items():
                if resolves(irk, addr):
                    rssi = d.get("RSSI")
                    if rssi is None:
                        continue
                    rssi = int(rssi)
                    if (addr, rssi) not in seen_rpas:
                        seen_rpas.add((addr, rssi))
                        hits[name].append(rssi)
                        print(f"[{LABEL}] HIT irk={name} rpa={addr} rssi={rssi}", flush=True)
        time.sleep(2)

    try:
        adapter.StopDiscovery()
    except dbus.DBusException:
        pass

    print(f"\n[{LABEL}] === summary ===")
    print(f"[{LABEL}] BLE devices seen at all: {len(total_devices)}")
    for name, vals in hits.items():
        if vals:
            print(f"[{LABEL}] irk-order={name}: {len(vals)} samples, "
                  f"rssi min={min(vals)} max={max(vals)} mean={sum(vals)/len(vals):.1f}")
    if not hits:
        print(f"[{LABEL}] NO RESOLVED HITS — iPhone not advertising, out of range, "
              f"or IRK is stale (re-pair changes it).")


if __name__ == "__main__":
    main()
