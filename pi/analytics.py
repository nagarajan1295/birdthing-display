#!/usr/bin/env python3
# BirdThing nightly analytics. Summarizes the day's detections + a confidence-based quality proxy
# (how clean/precise the IDs were), writes a markdown report into the repo's docs/analytics/, and
# commits + pushes to GitHub (token optional: put a fine-grained PAT in /opt/birdthing/gh_token).
import sqlite3, json, os, subprocess, datetime, statistics

DB = "/home/birdpi/BirdNET-Pi/scripts/birds.db"
REPO = "/opt/birdthing/repo"
OUTDIR = os.path.join(REPO, "docs", "analytics")
TOKEN_FILE = "/opt/birdthing/gh_token"
GH_REPO = "github.com/nagarajan1295/BirdThing.git"


def day_stats(con, d):
    rows = con.execute(
        "SELECT Com_Name,Confidence,Time FROM detections WHERE Date=?", (d,)).fetchall()
    if not rows:
        return None
    confs = [r[1] for r in rows]
    species = {}
    for com, cf, _ in rows:
        species.setdefault(com, []).append(cf)
    hourly = [0] * 24
    for _, _, t in rows:
        try:
            hourly[int(t[:2])] += 1
        except Exception:
            pass
    hi = sum(1 for c in confs if c >= 0.85)
    bord = sum(1 for c in confs if 0.70 <= c < 0.80)
    top = sorted(([k, len(v), round(statistics.mean(v), 3)] for k, v in species.items()),
                 key=lambda x: -x[1])[:10]
    return {"date": d, "detections": len(rows), "species": len(species),
            "conf_mean": round(statistics.mean(confs), 3),
            "conf_median": round(statistics.median(confs), 3),
            "high_conf_pct": round(100 * hi / len(confs), 1),
            "borderline_pct": round(100 * bord / len(confs), 1),
            "hourly": hourly, "top": top}


def bar(n, mx, width=24):
    return "█" * int(round(width * n / mx)) if mx else ""


def render(t, y, alltime):
    L = []
    L.append("# BirdThing analytics — %s\n" % t["date"])
    L.append("**Today:** %d detections across %d species.  " % (t["detections"], t["species"]))
    L.append("Mean confidence **%.2f** (median %.2f).\n" % (t["conf_mean"], t["conf_median"]))
    L.append("- High-confidence (≥ 0.85): **%.1f%%** — clean, reliable IDs" % t["high_conf_pct"])
    L.append("- Borderline (0.70–0.80): **%.1f%%** — watch these for false IDs" % t["borderline_pct"])
    if y:
        dd = t["detections"] - y["detections"]
        L.append("- vs yesterday: %+d detections, %+d species, confidence %+.2f"
                 % (dd, t["species"] - y["species"], t["conf_mean"] - y["conf_mean"]))
    L.append("\n## Top species today\n")
    L.append("| Bird | Count | Avg confidence |")
    L.append("|---|---:|---:|")
    for com, c, cf in t["top"]:
        L.append("| %s | %d | %.2f |" % (com, c, cf))
    L.append("\n## Hourly activity\n```")
    mx = max(t["hourly"]) or 1
    for h in range(24):
        if t["hourly"][h]:
            L.append("%02d:00 %4d %s" % (h, t["hourly"][h], bar(t["hourly"][h], mx)))
    L.append("```")
    peak = t["hourly"].index(max(t["hourly"]))
    L.append("\n**Best listening hour:** ~%02d:00.  " % peak)
    L.append("**Quality read:** %.0f%% of IDs were high-confidence; "
             "%.0f%% borderline (candidate false positives). "
             "A rising borderline %% usually means more ambient noise — consider re-enabling the "
             "spectral gate or raising CONFIDENCE.\n" % (t["high_conf_pct"], t["borderline_pct"]))
    L.append("> All-time: %d detections, %d species.\n" % (alltime[0], alltime[1]))
    L.append("*Generated automatically by analytics.py.*")
    return "\n".join(L)


def main():
    today = datetime.date.today().isoformat()
    yest = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    t = day_stats(con, today)
    y = day_stats(con, yest)
    alltime = con.execute("SELECT COUNT(*),COUNT(DISTINCT Com_Name) FROM detections").fetchone()
    con.close()
    if not t:
        print("no detections today; nothing to report")
        return
    os.makedirs(os.path.join(OUTDIR, "history"), exist_ok=True)
    md = render(t, y, alltime)
    open(os.path.join(OUTDIR, "latest.md"), "w", encoding="utf-8").write(md)
    open(os.path.join(OUTDIR, "history", today + ".md"), "w", encoding="utf-8").write(md)
    json.dump({"today": t, "yesterday": y, "alltime": list(alltime)},
              open(os.path.join(OUTDIR, "data.json"), "w"), indent=2)
    env = {**os.environ, "GIT_AUTHOR_NAME": "BirdThing", "GIT_AUTHOR_EMAIL": "birdthing@local",
           "GIT_COMMITTER_NAME": "BirdThing", "GIT_COMMITTER_EMAIL": "birdthing@local"}
    subprocess.run(["git", "-C", REPO, "add", "docs/analytics"], env=env)
    r = subprocess.run(["git", "-C", REPO, "commit", "-m", "analytics: report for " + today], env=env)
    if r.returncode != 0:
        print("nothing to commit"); return
    token = ""
    if os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip()
    if token:
        url = "https://%s@%s" % (token, GH_REPO)
        p = subprocess.run(["git", "-C", REPO, "push", url, "HEAD:master"],
                           capture_output=True, text=True)
        print("push:", "ok" if p.returncode == 0 else p.stderr[-200:])
    else:
        print("committed locally (no /opt/birdthing/gh_token, so not pushed)")


if __name__ == "__main__":
    main()
