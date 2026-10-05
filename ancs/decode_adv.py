#!/usr/bin/env python3
"""Pull the raw LE advertising data out of a btsnoop capture and name the AD
types. The one that matters is 0x15 (128-bit service SOLICITATION) - that is
what makes iOS offer the notification-access prompt. 0x06/0x07 (service class
UUIDs) look similar in btmon output but do NOT trigger it."""
import struct
import sys

AD_TYPES = {
    0x01: "Flags", 0x02: "16-bit UUIDs (partial)", 0x03: "16-bit UUIDs",
    0x06: "128-bit UUIDs (partial)", 0x07: "128-bit UUIDs (complete)",
    0x08: "Short name", 0x09: "Complete name", 0x0A: "TX power",
    0x14: "16-bit SOLICITATION", 0x15: "128-bit SOLICITATION",
    0x16: "Service data", 0xFF: "Manufacturer data",
}


def parse_ad(blob):
    out, i = [], 0
    while i < len(blob):
        ln = blob[i]
        if ln == 0:
            break
        t = blob[i + 1]
        val = blob[i + 2:i + 1 + ln]
        name = AD_TYPES.get(t, "type 0x%02x" % t)
        if t == 0x15 and len(val) == 16:
            uuid = "-".join([val[::-1].hex()[s:e] for s, e in
                             ((0, 8), (8, 12), (12, 16), (16, 20), (20, 32))])
            out.append("  0x%02x %-24s %s   <-- SOLICITATION" % (t, name, uuid))
        elif t in (0x06, 0x07) and len(val) == 16:
            uuid = "-".join([val[::-1].hex()[s:e] for s, e in
                             ((0, 8), (8, 12), (12, 16), (16, 20), (20, 32))])
            out.append("  0x%02x %-24s %s   <-- NOT solicitation" % (t, name, uuid))
        elif t in (0x08, 0x09):
            out.append("  0x%02x %-24s %r" % (t, name, val.decode("utf-8", "replace")))
        else:
            out.append("  0x%02x %-24s %s" % (t, name, val.hex()))
        i += 1 + ln
    return out


def main(path):
    with open(path, "rb") as fh:
        data = fh.read()
    pos = 16                        # btsnoop file header
    seen = 0
    while pos + 24 <= len(data):
        _olen, ilen, flags, _drops, _ts = struct.unpack(">IIIIq", data[pos:pos + 24])
        pkt = data[pos + 24:pos + 24 + ilen]
        pos += 24 + ilen
        # btmon -w writes datalink 2001 (HCI monitor): the record's flags field
        # carries (adapter_index << 16 | monitor_opcode) and the payload is the
        # bare HCI packet with NO H4 type byte. 0x0002 = command packet.
        if (flags & 0xFFFF) != 0x0002 or len(pkt) < 3:
            continue
        opcode = struct.unpack("<H", pkt[0:2])[0]
        if opcode not in (0x2008, 0x2009, 0x2037, 0x2038):
            continue                # adv data / scan rsp (legacy + extended)
        label = {0x2008: "LE Set Advertising Data",
                 0x2009: "LE Set Scan Response Data",
                 0x2037: "LE Set Extended Advertising Data",
                 0x2038: "LE Set Extended Scan Response Data"}[opcode]
        body = pkt[3:]              # skip opcode(2) + param length(1)
        if opcode in (0x2037, 0x2038):
            body = body[4:]         # handle, operation, frag pref, data len
        ln = body[0] if body else 0
        blob = body[1:1 + ln]
        if not blob:
            continue
        seen += 1
        print("%s (%d bytes):" % (label, ln))
        for line in parse_ad(blob):
            print(line)
        print()
    if not seen:
        print("no advertising-data commands found in capture")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/adv.btsnoop")
