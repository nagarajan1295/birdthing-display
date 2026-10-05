#!/usr/bin/env python3
"""Read RSSI of a live LE connection via a raw HCI socket.

`hcitool rssi` only resolves BR/EDR connections by address, so it fails on an
LE-only link. Going straight to HCI_Read_RSSI with the connection handle works
for either transport.
"""
import re
import socket
import struct
import subprocess
import sys
import time

AF_BLUETOOTH, BTPROTO_HCI, HCI_FILTER = 31, 1, 2
OGF_STATUS, OCF_READ_RSSI = 0x05, 0x0005


def handles_for(addr):
    """Connection handles for addr, from `hcitool con`."""
    out = subprocess.run(["hcitool", "con"], capture_output=True, text=True).stdout
    return [int(m.group(1)) for line in out.splitlines()
            if addr.upper() in line.upper()
            for m in [re.search(r"handle (\d+)", line)] if m]


def read_rssi(sock, handle):
    opcode = (OGF_STATUS << 10) | OCF_READ_RSSI
    sock.send(struct.pack("<BHBH", 0x01, opcode, 2, handle))
    deadline = time.time() + 1.0
    while time.time() < deadline:
        pkt = sock.recv(258)
        # 0x04 = HCI event, 0x0E = Command Complete
        if len(pkt) >= 10 and pkt[0] == 0x04 and pkt[1] == 0x0E:
            status, h, rssi = struct.unpack("<BHb", pkt[6:10])
            if status == 0 and h == handle:
                return rssi
    return None


def open_hci(dev=0):
    s = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    s.bind((dev,))  # the filter is per-bound-device; setting it first gets EINVAL
    # typemask=all, event mask=all, opcode=0
    s.setsockopt(socket.SOL_HCI, HCI_FILTER,
                 struct.pack("<IIIH", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0))
    return s


if __name__ == "__main__":
    addr = sys.argv[1]
    hs = handles_for(addr)
    print(f"handles for {addr}: {hs}")
    if not hs:
        sys.exit("no live connection")
    s = open_hci()
    for _ in range(6):
        print("rssi:", read_rssi(s, hs[0]))
        time.sleep(1)
