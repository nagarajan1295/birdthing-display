#!/usr/bin/env python3
"""
Add a same-origin /api/notify proxy to one of the house API servers.

Both the Car Thing (BirdThing) and the WeatherThing Car Thing can only reach
their own Pi - the BirdThing CT over USB, the WeatherThing CT over the BT PAN
link. Neither can hit the ANCS gateway's port directly, so each Pi's existing
API server proxies it on the origin the page was loaded from.

Idempotent. Writes a timestamped .bak first.

  patch_api_route.py <api-file> <gateway-url> <route-anchor>

<route-anchor> is an existing `elif self.path.startswith("...")` line that the
new route is inserted directly above.
"""
import os
import re
import shutil
import sys
import time

MARK = "# --- ANCS iPhone notifications ---"

HELPER = '''
# --- ANCS iPhone notifications ---
# Same-origin proxy for the ANCS gateway (BirdThing Pi :8099) so the Car
# Thing's browser can read iPhone notifications; it has no route to that host
# or port itself. Short cache so a 2.5s UI poll can't stampede the gateway.
_ancs_cache = {"t": 0.0, "d": {"ok": False, "linked": False, "items": []}}
ANCS_URL = "%(url)s"


def notify():
    now = time.time()
    if now - _ancs_cache["t"] < 1.0:
        return _ancs_cache["d"]
    try:
        with urllib.request.urlopen(ANCS_URL, timeout=3) as r:
            _ancs_cache["d"] = json.loads(r.read().decode())
    except Exception as e:
        _ancs_cache["d"] = {"ok": False, "linked": False, "items": [],
                            "error": str(e)[:120]}
    _ancs_cache["t"] = now
    return _ancs_cache["d"]

'''


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    api_path, url, anchor = sys.argv[1], sys.argv[2], sys.argv[3]

    with open(api_path, encoding="utf-8") as fh:
        src = fh.read()

    if MARK in src:
        print("already patched: %s" % api_path)
        return 0

    # 1) helper, inserted just above the request-handler class
    m = re.search(r"^class \w+\(BaseHTTPRequestHandler\):", src, re.M)
    if not m:
        print("ERROR: no BaseHTTPRequestHandler subclass in %s" % api_path)
        return 1
    src = src[:m.start()] + (HELPER % {"url": url}).lstrip("\n") + "\n" + src[m.start():]

    # 2) route, inserted above the anchor route. The two servers dispatch
    # differently - birdthing_api.py tests `self.path`, weather_api.py binds
    # `p = self.path` first - so match whichever this file uses and mirror it.
    m = re.search(r"^(\s*)elif (self\.path|p)\.startswith\(['\"]%s['\"]\)"
                  % re.escape(anchor), src, re.M)
    if not m:
        print("ERROR: anchor route %r not found in %s" % (anchor, api_path))
        return 1
    indent, subject = m.group(1), m.group(2)

    # prefer the file's own JSON helper when it has one
    if re.search(r"^\s*def _json\(self", src, re.M):
        body = "%s    self._json(notify())\n" % indent
    else:
        body = ('%s    self._send(200, "application/json", '
                'json.dumps(notify()).encode())\n' % indent)
    route = '%selif %s.startswith("/api/notify"):\n%s' % (indent, subject, body)
    src = src[:m.start()] + route + src[m.start():]

    bak = "%s.bak.%d" % (api_path, int(time.time()))
    shutil.copy2(api_path, bak)
    tmp = api_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(src)
    shutil.copymode(api_path, tmp)
    os.replace(tmp, api_path)
    print("patched %s -> /api/notify proxies %s (backup %s)"
          % (api_path, url, os.path.basename(bak)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
