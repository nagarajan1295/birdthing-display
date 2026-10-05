#!/usr/bin/env python3
"""Verify the fix for the bug that ate real notifications: a dropped BLE link
must NOT deactivate ordinary notifications (the displays refuse to toast
anything with active=false), but must still cancel a ringing call."""
import sys
sys.path.insert(0, "/opt/ancs")
import ancs_gateway as g

S = g.Store(10)

msg = {"uid": 1, "call": False, "active": True, "ts": 0}
call = {"uid": 2, "call": True, "active": True, "ts": 0}
S.add(msg)
S.add(call)
S.set_link(True, "iPhone")

# the phone walks out of range mid-conversation
S.set_link(False)

ok = True
if msg["active"] is not True:
    print("FAIL: an ordinary notification was deactivated by a dropped link "
          "- this is the bug that made every message invisible")
    ok = False
else:
    print("PASS: ordinary notification survived the disconnect (active=True)")

if call["active"] is not False:
    print("FAIL: a ringing call stayed active after the phone vanished")
    ok = False
else:
    print("PASS: ringing call was cancelled by the disconnect (active=False)")

# The phone clearing a MESSAGE must not retract it from the house displays:
# texting yourself clears it in under a second, and the displays poll every
# 2.5s, so such a notification could otherwise never be shown at all.
S2 = g.Store(10)
m2 = {"uid": 10, "call": False, "active": True, "ts": 0}
c2 = {"uid": 11, "call": True, "active": True, "ts": 0}
S2.add(m2)
S2.add(c2)
S2.remove(10)
S2.remove(11)

if m2["active"] is not True:
    print("FAIL: a message the phone cleared was retracted from the displays "
          "- this is the 'I texted myself and nothing appeared' bug")
    ok = False
else:
    print("PASS: phone-cleared message still displays (removed_ts=%s)"
          % (m2.get("removed_ts") is not None))

if c2["active"] is not False:
    print("FAIL: a call that ended was NOT dismissed")
    ok = False
else:
    print("PASS: ended call is dismissed by the phone-side removal")

sys.exit(0 if ok else 1)
