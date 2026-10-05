#!/usr/bin/env python3
"""Fail if the private home location leaks into this PUBLIC repo.

The dashboard and API ship with a default location. On the real device that
default is the author's home town; in the repo it must stay the generic New
York placeholder. Syncing deployed files straight into the repo has silently
undone that scrub before (commit 918ee80), so this runs as a pre-commit hook:

    python3 tools/check_location_scrub.py          # report, exit 1 if leaking
    python3 tools/check_location_scrub.py --fix    # rewrite to the placeholder

Install the hook (from the repo root):

    cp tools/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
"""
import re, sys, pathlib

# (regex to find, replacement) -- the placeholder is New York City.
RULES = [
    (re.compile(r"Potsdam,\s*NY"), "New York, NY"),
    (re.compile(r"Potsdam\s*NY"), "New York"),
    (re.compile(r"\bPotsdam\b"), "New York"),
    (re.compile(r"44\.6\d{2,}"), "40.7128"),
    (re.compile(r"-74\.9\d{2,}"), "-74.0060"),
]
TARGETS = [
    "pi/birdthing_api.py",
    "dashboard/birdthing-dashboard.html",
    "bridgething/apps/weatherthing/public/manifest.json",
    "bridgething/apps/weatherthing/src/App.tsx",
]

root = pathlib.Path(__file__).resolve().parent.parent
fix = "--fix" in sys.argv
leaks = []

for rel in TARGETS:
    p = root / rel
    if not p.exists():
        continue
    text = original = p.read_text(encoding="utf-8")
    for rx, repl in RULES:
        text = rx.sub(repl, text)
    if text != original:
        for i, line in enumerate(original.splitlines(), 1):
            if any(rx.search(line) for rx, _ in RULES):
                leaks.append("%s:%d" % (rel, i))
        if fix:
            p.write_text(text, encoding="utf-8")

if not leaks:
    print("location scrub OK — no private location in the tracked files")
    sys.exit(0)
if fix:
    print("scrubbed the private location from:\n  " + "\n  ".join(leaks))
    sys.exit(0)
print("PRIVATE LOCATION LEAK — this repo is public:\n  " + "\n  ".join(leaks))
print("\nRun:  python3 tools/check_location_scrub.py --fix")
sys.exit(1)
