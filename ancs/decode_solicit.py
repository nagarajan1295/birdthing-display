#!/usr/bin/env python3
"""Find the ANCS UUID in a btsnoop capture and print the AD TYPE byte in front
of it. btmon renders 0x15 (128-bit solicitation) and 0x06/0x07 (plain 128-bit
service UUID lists) with the identical label '128-bit Service UUIDs', so the
only way to know the solicitation really went out is to read the type byte."""
import sys

ANCS = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
raw = bytes.fromhex(ANCS.replace("-", ""))[::-1]   # adv carries it little-endian

blob = open(sys.argv[1], "rb").read()
hits = 0
start = 0
while True:
    i = blob.find(raw, start)
    if i < 0:
        break
    start = i + 1
    hits += 1
    ad_type = blob[i - 1]
    ad_len = blob[i - 2]
    names = {0x06: "INCOMPLETE 128-bit service UUID list (WRONG for ANCS)",
             0x07: "COMPLETE 128-bit service UUID list (WRONG for ANCS)",
             0x15: "128-bit SERVICE SOLICITATION  <-- correct for ANCS"}
    print("offset %d: len=0x%02x type=0x%02x  %s"
          % (i, ad_len, ad_type, names.get(ad_type, "unexpected")))

print("ANCS UUID occurrences: %d" % hits)
sys.exit(0 if hits else 1)
