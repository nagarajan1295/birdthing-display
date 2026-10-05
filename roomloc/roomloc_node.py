#!/usr/bin/env python3
"""roomloc node — one per box with a Bluetooth radio.

Watches BLE adverts, resolves the iPhone's rotating private address with its
IRK, and reports smoothed RSSI to the roomloc hub. Event-driven off BlueZ's
PropertiesChanged so every advert gives a fresh, timestamped sample (polling
GetManagedObjects cannot tell a new advert from a cached one).

Stock deps only: python3-dbus, python3-gi, python3-cryptography, python3-requests.
"""
import argparse
import ctypes
import socket
import struct
import threading
import time

import dbus
import dbus.mainloop.glib
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from gi.repository import GLib

BLUEZ = "org.bluez"
DEV_IFACE = "org.bluez.Device1"

AF_BLUETOOTH, BTPROTO_HCI = 31, 1
HCI_DEV_NONE, HCI_CHANNEL_CONTROL = 0xFFFF, 3
MGMT_OP_GET_CONN_INFO = 0x0031
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_STATUS_BUSY = 0x0D


class _SockaddrHCI(ctypes.Structure):
    _fields_ = [("hci_family", ctypes.c_ushort),
                ("hci_dev", ctypes.c_ushort),
                ("hci_channel", ctypes.c_ushort)]


class ConnRSSI:
    """RSSI of a live link, via the BlueZ management socket.

    An iPhone that is BLE-*connected* to this box (our ANCS gateway holds such a
    link) stops sending the adverts the IRK path relies on -- that node would
    otherwise read nothing at all. The mgmt API reads RSSI straight off the
    connection instead, and does it for LE, which `hcitool rssi` cannot.
    """

    def __init__(self, addr, addr_type=1, index=0):
        self.addr = bytes.fromhex(addr.replace(":", ""))[::-1]  # mgmt wants LE order
        self.addr_type = addr_type  # 0=BR/EDR 1=LE public 2=LE random
        self.index = index
        self.sock = None

    def _connect(self):
        s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
        sa = _SockaddrHCI(AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.bind(s.fileno(), ctypes.byref(sa), ctypes.sizeof(sa)) != 0:
            s.close()
            raise OSError(ctypes.get_errno(), "mgmt bind failed")
        s.settimeout(1.5)
        self.sock = s

    def _drain(self):
        """Discard queued unsolicited events.

        The mgmt socket carries every event for every controller, and a busy
        adapter fills the buffer between polls. Without this the backlog eats
        the whole read window and our own reply is never reached.
        """
        self.sock.setblocking(False)
        try:
            while True:
                if not self.sock.recv(512):
                    break
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self.sock.settimeout(1.5)

    def read(self):
        """dBm, or None if not connected / busy / unavailable."""
        try:
            if self.sock is None:
                self._connect()
            self._drain()
            pkt = struct.pack("<HHH", MGMT_OP_GET_CONN_INFO, self.index, 7)
            self.sock.send(pkt + self.addr + bytes([self.addr_type]))
            deadline = time.time() + 1.5
            while time.time() < deadline:
                p = self.sock.recv(512)
                if len(p) < 9:
                    continue
                ev = struct.unpack("<H", p[:2])[0]
                if ev != MGMT_EV_CMD_COMPLETE:
                    continue  # unsolicited mgmt event, not our reply
                if struct.unpack("<H", p[6:8])[0] != MGMT_OP_GET_CONN_INFO:
                    continue
                status = p[8]
                if status != 0 or len(p) < 19:
                    return None  # 0x0d busy / 0x0e not connected -- retry next tick
                return struct.unpack("<b", p[16:17])[0]
        except TimeoutError:
            return None  # a slow reply is not a broken socket; keep it open
        except (OSError, struct.error):
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock = None
        return None


def ah(key: bytes, r: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (enc.update(b"\x00" * 13 + r) + enc.finalize())[-3:]


class Tracker:
    def __init__(self, irk_hex, node, hub, report_interval, stale_after, alpha):
        # BlueZ writes the IRK big-endian in its bond file; the crypto wants the
        # reverse. Verified empirically against this phone -- do not "fix".
        self.irk = bytes.fromhex(irk_hex)[::-1]
        self.node = node
        self.hub = hub.rstrip("/")
        self.report_interval = report_interval
        self.stale_after = stale_after
        self.alpha = alpha

        self.lock = threading.Lock()
        self.ema = None
        self.last_seen = 0.0
        self.raw = None
        self.hits = 0
        self.source = None
        self.resolved_cache = {}  # rpa -> bool, RPAs rotate ~15min so this stays small

    # --- address resolution -------------------------------------------------
    def is_phone(self, addr: str) -> bool:
        cached = self.resolved_cache.get(addr)
        if cached is not None:
            return cached
        try:
            mac = bytes.fromhex(addr.replace(":", ""))
        except ValueError:
            return False
        ok = len(mac) == 6 and (mac[0] & 0xC0) == 0x40 and ah(self.irk, mac[:3]) == mac[3:]
        if len(self.resolved_cache) > 512:
            self.resolved_cache.clear()
        self.resolved_cache[addr] = ok
        return ok

    def sample(self, addr: str, rssi: int):
        if not self.is_phone(addr):
            return
        self.add(rssi, "advert")

    def add(self, rssi: int, source: str):
        with self.lock:
            now = time.time()
            # Restart the average on a gap (stale room's signal must not bleed
            # into the new one) or on a source switch (advert and connection RSSI
            # sit at different offsets, so blending them corrupts fingerprints).
            stale = self.ema is None or (now - self.last_seen) > self.stale_after
            if stale or source != self.source:
                self.ema = float(rssi)
            else:
                self.ema = self.alpha * rssi + (1 - self.alpha) * self.ema
            self.raw = rssi
            self.source = source
            self.last_seen = now
            self.hits += 1

    def conn_loop(self, conn: "ConnRSSI", interval: float):
        """Poll the live link. Adverts win when both are available -- a connected
        phone barely advertises, so in practice only one path is ever active."""
        while True:
            rssi = conn.read()
            if rssi is not None and -127 < rssi < 0:
                with self.lock:
                    fresh_advert = (self.source == "advert"
                                    and time.time() - self.last_seen < 20)
                if not fresh_advert:
                    self.add(rssi, "conn")
            time.sleep(interval)

    # --- dbus plumbing ------------------------------------------------------
    def on_props(self, interface, changed, invalidated, path=None):
        if interface != DEV_IFACE or "RSSI" not in changed:
            return
        addr = path.rsplit("/", 1)[-1].replace("dev_", "").replace("_", ":")
        self.sample(addr, int(changed["RSSI"]))

    def on_added(self, path, interfaces):
        dev = interfaces.get(DEV_IFACE)
        if dev and "RSSI" in dev:
            self.sample(str(dev.get("Address", "")), int(dev["RSSI"]))

    # --- reporting ----------------------------------------------------------
    def report_loop(self):
        sess = requests.Session()
        while True:
            time.sleep(self.report_interval)
            with self.lock:
                age = time.time() - self.last_seen if self.last_seen else None
                fresh = age is not None and age <= self.stale_after
                payload = {
                    "node": self.node,
                    "rssi": round(self.ema, 1) if fresh and self.ema is not None else None,
                    "raw": self.raw if fresh else None,
                    "age": round(age, 1) if age is not None else None,
                    "hits": self.hits,
                    "src": self.source if fresh else None,
                }
            try:
                sess.post(f"{self.hub}/report", json=payload, timeout=4)
            except requests.RequestException:
                pass  # hub restarts are routine; keep scanning regardless


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--irk", required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--hub", required=True)
    p.add_argument("--report-interval", type=float, default=3.0)
    p.add_argument("--stale-after", type=float, default=45.0)
    p.add_argument("--alpha", type=float, default=0.4)
    p.add_argument("--identity-addr", default=None,
                   help="phone's identity BD_ADDR; enables live-link RSSI reads")
    p.add_argument("--identity-type", type=int, default=1,
                   help="0=BR/EDR 1=LE public 2=LE random")
    # Roughly a third of mgmt reads come back BUSY, so poll faster than the
    # sample rate you actually want.
    p.add_argument("--conn-interval", type=float, default=2.0)
    args = p.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")
    adapter_path = next(
        p_ for p_, i in om.GetManagedObjects().items() if "org.bluez.Adapter1" in i
    )
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), "org.bluez.Adapter1")

    t = Tracker(args.irk, args.node, args.hub, args.report_interval,
                args.stale_after, args.alpha)

    bus.add_signal_receiver(t.on_props, dbus_interface="org.freedesktop.DBus.Properties",
                            signal_name="PropertiesChanged", path_keyword="path")
    bus.add_signal_receiver(t.on_added, dbus_interface="org.freedesktop.DBus.ObjectManager",
                            signal_name="InterfacesAdded")

    def start_discovery():
        try:
            adapter.SetDiscoveryFilter({"Transport": "le",
                                        "DuplicateData": dbus.Boolean(True)})
        except dbus.DBusException:
            pass
        try:
            adapter.StartDiscovery()
        except dbus.DBusException as e:
            if "InProgress" not in str(e):
                print(f"StartDiscovery failed: {e}", flush=True)
        return True

    start_discovery()
    # BlueZ silently drops discovery on adapter resets; re-arming is idempotent.
    GLib.timeout_add_seconds(120, start_discovery)

    # Purge BlueZ's device cache periodically. Retired RPAs otherwise pile up in
    # the object tree forever and slow every signal dispatch down.
    def prune():
        try:
            for path_, ifaces in om.GetManagedObjects().items():
                d = ifaces.get(DEV_IFACE)
                if d and not d.get("Connected") and not d.get("Paired"):
                    try:
                        adapter.RemoveDevice(path_)
                    except dbus.DBusException:
                        pass
        except dbus.DBusException:
            pass
        return True

    GLib.timeout_add_seconds(900, prune)

    if args.identity_addr:
        conn = ConnRSSI(args.identity_addr, args.identity_type)
        threading.Thread(target=t.conn_loop, args=(conn, args.conn_interval),
                         daemon=True).start()
        print(f"live-link RSSI enabled for {args.identity_addr}", flush=True)

    threading.Thread(target=t.report_loop, daemon=True).start()
    print(f"roomloc node '{args.node}' scanning on {adapter_path} -> {args.hub}", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
