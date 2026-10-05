#!/usr/bin/env python3
"""
Make pull-to-refresh actually usable on the Car Thing.

Two defects, both in the gesture conditions rather than the handler:

  1. The pull only counted if the finger landed on something with NO
     scrollable ancestor. Real pull-to-refresh triggers whenever the
     scrollable area is already AT THE TOP - otherwise landing anywhere on a
     scrollable region silently scrolls by a few pixels and eats the gesture.

  2. The threshold was a 130px drag. On a 480px-tall panel that is 27% of the
     screen height. Dropped to 70px.

Idempotent, writes a timestamped .bak.  usage: fix_ptr.py <html-file>
"""
import os
import shutil
import sys
import time

OLD_MOVE = """    if(el){el.scrollTop=st-dy;}
    else if(dy>0&&!curOverlay()){pull=dy;
      ptr.style.opacity=Math.min(1,dy/130);ptr.style.transform='translate(-50%,'+Math.min(56,dy/2.4)+'px)';}"""

NEW_MOVE = """    // pull wins over scrolling when the scroller is already at its top,
    // which is what makes the gesture reachable anywhere on the screen
    if(el&&!(dy>0&&st<=0&&el.scrollTop<=0)){el.scrollTop=st-dy;}
    else if(dy>0&&!curOverlay()){pull=dy;
      ptr.style.opacity=Math.min(1,dy/PTR_T);ptr.style.transform='translate(-50%,'+Math.min(56,dy/2.4)+'px)';}"""

OLD_END = "  function end(){if(pull>130)location.reload();"
NEW_END = "  function end(){if(pull>PTR_T)location.reload();"

OLD_VARS = "  var down=false,sy=0,el=null,pull=0,moved=0,st=0;"
NEW_VARS = "  var down=false,sy=0,el=null,pull=0,moved=0,st=0;\n  var PTR_T=70;   // drag px needed to refresh (was 130 = 27% of a 480px screen)"


def main():
    path = sys.argv[1]
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    if "PTR_T" in src:
        print("already patched: %s" % path)
        return 0

    missing = [n for n, s in (("vars", OLD_VARS), ("move", OLD_MOVE),
                              ("end", OLD_END)) if s not in src]
    if missing:
        print("ERROR: could not find %s block(s) in %s" % (missing, path))
        return 1

    src = src.replace(OLD_VARS, NEW_VARS, 1)
    src = src.replace(OLD_MOVE, NEW_MOVE, 1)
    src = src.replace(OLD_END, NEW_END, 1)

    bak = "%s.bak.%d" % (path, int(time.time()))
    shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(src)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    print("patched pull-to-refresh in %s (backup %s)" % (path, os.path.basename(bak)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
