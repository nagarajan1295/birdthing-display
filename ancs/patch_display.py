#!/usr/bin/env python3
"""
Inject the ANCS notification toast into one of the house displays.

Idempotent: re-running replaces the previously injected block rather than
stacking copies. Always writes a timestamped .bak first.

  patch_display.py <html-file> <widget-file> <ancs-url> [key=value ...]

Recognised keys (all optional):
  theme=light      host page is light but has no body.light (bedroom kiosk)
  style=full       whole-screen notification instead of a top banner
  fs=<css-length>  base font size; pass the host's own scale so the
                   notification matches that screen's typography, e.g.
                   'calc(17px * var(--ts,1))'
  hold_ms / call_hold_ms / poll_ms
"""
import json
import os
import shutil
import sys
import time

BEGIN = "<!-- ANCS-NOTIFY-BEGIN -->"
END = "<!-- ANCS-NOTIFY-END -->"


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    html_path, widget_path, ancs_url = sys.argv[1], sys.argv[2], sys.argv[3]

    # device: which screen this is, so the central per-display on/off switch
    # (set from the BirdThing settings panel) can address it.
    keys = {"device": "ANCS_DEVICE",
            "theme": "ANCS_THEME", "style": "ANCS_STYLE", "fs": "ANCS_FS",
            "hold_ms": "ANCS_HOLD_MS", "call_hold_ms": "ANCS_CALL_HOLD_MS",
            "poll_ms": "ANCS_POLL_MS",
            # phone-link status glyph placement:
            #   status_slot   = CSS selector of a status row to sit inside
            #   status_anchor = CSS selector of an existing status icon to sit
            #                   just left of (so they never overlap)
            #   status        = 'tr'|'tl'|'br'|'bl'|'off' corner fallback
            "status_slot": "ANCS_STATUS_SLOT",
            "status_anchor": "ANCS_STATUS_ANCHOR",
            "status": "ANCS_STATUS"}
    opts = {}
    for arg in sys.argv[4:]:
        if "=" not in arg:
            print("ERROR: expected key=value, got %r" % arg)
            return 2
        k, v = arg.split("=", 1)
        if k not in keys:
            print("ERROR: unknown key %r (known: %s)"
                  % (k, ", ".join(sorted(keys))))
            return 2
        opts[keys[k]] = v

    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    with open(widget_path, encoding="utf-8") as fh:
        widget = fh.read()

    cfg = "window.ANCS_URL=%s;" % json.dumps(ancs_url)
    for name, val in sorted(opts.items()):
        cfg += "window.%s=%s;" % (
            name, val if val.isdigit() else json.dumps(val))
    block = "%s\n<script>%s</script>\n%s\n%s\n" % (
        BEGIN, cfg, widget.strip(), END)

    if BEGIN in html and END in html:
        start = html.index(BEGIN)
        stop = html.index(END) + len(END)
        # keep whatever trailing newline the old block had
        new = html[:start] + block.rstrip("\n") + html[stop:]
        action = "replaced"
    else:
        idx = html.rfind("</body>")
        if idx == -1:
            print("ERROR: no </body> in %s" % html_path)
            return 1
        new = html[:idx] + block + html[idx:]
        action = "inserted"

    if new == html:
        print("no change needed: %s" % html_path)
        return 0

    bak = "%s.bak.%d" % (html_path, int(time.time()))
    shutil.copy2(html_path, bak)
    tmp = html_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    shutil.copymode(html_path, tmp)
    os.replace(tmp, html_path)
    print("%s toast in %s (url=%s), backup %s"
          % (action, html_path, ancs_url, os.path.basename(bak)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
