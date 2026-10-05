#!/usr/bin/env python3
"""Prove the ANCS subscribe chain survives BlueZ's one-ATT-at-a-time rule.

The real failure this reproduces: BlueZ processes a single ATT operation per
connection, so a StartNotify issued while another is still outstanding returns
`org.bluez.Error.InProgress`. Firing both ANCS subscribes back-to-back made the
second one fail every single time, and retrying on a timer collided again -
observed live as 7 consecutive InProgress failures on both characteristics,
after which the gateway stopped attaching at all and no notification could
arrive.

The fake below enforces exactly that rule: any StartNotify that overlaps
another one errors with InProgress. A correct implementation must serialise.
"""
import sys

sys.path.insert(0, "/opt/ancs")
sys.argv = ["test"]
import ancs_gateway as g
from gi.repository import GLib


class InProgress(Exception):
    def __str__(self):
        return "org.bluez.Error.InProgress: In Progress"


BUSY = {"n": 0}
ATTEMPTS = {"total": 0, "collisions": 0}


class FakeChar:
    def __init__(self, name):
        self.name = name
        self.notifying = False

    # stands in for both org.bluez.GattCharacteristic1 and Properties
    def Get(self, iface, prop):
        return self.notifying

    def StartNotify(self, reply_handler=None, error_handler=None):
        ATTEMPTS["total"] += 1
        if BUSY["n"] > 0:                     # another ATT op is outstanding
            ATTEMPTS["collisions"] += 1
            GLib.idle_add(lambda: (error_handler(InProgress()), False)[1])
            return
        BUSY["n"] += 1

        def done():
            self.notifying = True
            BUSY["n"] -= 1
            reply_handler()
            return False

        GLib.timeout_add(120, done)           # the ATT round trip


class FakeBus:
    def __init__(self, chars):
        self.chars = chars

    def get_object(self, _name, path):
        return self.chars[path]


NS, DS = "/chrc/ns", "/chrc/ds"
chars = {NS: FakeChar("notification_source"), DS: FakeChar("data_source")}
g.dbus.Interface = lambda obj, iface: obj          # bypass real D-Bus plumbing

client = g.AncsClient(FakeBus(chars), "/org/bluez/hci0")
chrcs = {g.NOTIFICATION_SOURCE: NS, g.DATA_SOURCE: DS}

loop = GLib.MainLoop()
client._subscribe_chain(chrcs)
GLib.timeout_add(6000, lambda: (loop.quit(), False)[1])
loop.run()

ns_ok = chars[NS].notifying
ds_ok = chars[DS].notifying
diag = g.DIAG.get("notifying", {})

print("attempts=%d collisions=%d" % (ATTEMPTS["total"], ATTEMPTS["collisions"]))
print("notification_source notifying=%s  data_source notifying=%s"
      % (ns_ok, ds_ok))
print("DIAG notifying=%s" % (diag,))

ok = True
if not (ns_ok and ds_ok):
    print("FAIL: did not end up subscribed to both characteristics")
    ok = False
else:
    print("PASS: both characteristics subscribed despite the InProgress rule")

if ATTEMPTS["collisions"] > 0:
    print("FAIL: %d StartNotify calls overlapped - they are not serialised, "
          "which is the bug itself" % ATTEMPTS["collisions"])
    ok = False
else:
    print("PASS: no overlapping StartNotify calls (properly serialised)")

if diag.get("notification_source") is not True or diag.get("data_source") is not True:
    print("FAIL: /api/status would misreport the subscription state")
    ok = False
else:
    print("PASS: /api/status reports the true subscription state")

# --- CONTROL -------------------------------------------------------------
# A test that passes against a fake which cannot reproduce the bug proves
# nothing. Replay the OLD back-to-back behaviour and confirm the fake really
# does punish it, so the PASS above is meaningful.
print("\n--- control: the old back-to-back behaviour ---")
BUSY["n"] = 0
ATTEMPTS["total"] = ATTEMPTS["collisions"] = 0
chars2 = {NS: FakeChar("notification_source"), DS: FakeChar("data_source")}
loop2 = GLib.MainLoop()
for p in (DS, NS):                       # both at once, no chaining
    chars2[p].StartNotify(reply_handler=lambda: None,
                          error_handler=lambda e: None)
GLib.timeout_add(2000, lambda: (loop2.quit(), False)[1])
loop2.run()
print("control: attempts=%d collisions=%d  ns=%s ds=%s"
      % (ATTEMPTS["total"], ATTEMPTS["collisions"],
         chars2[NS].notifying, chars2[DS].notifying))
if ATTEMPTS["collisions"] == 0:
    print("FAIL(control): the fake never collides, so the main test is "
          "vacuous - it would pass even with the bug present")
    ok = False
else:
    print("PASS(control): old behaviour collides and leaves a characteristic "
          "unsubscribed - exactly the live failure, so the fake is valid")

sys.exit(0 if ok else 1)
