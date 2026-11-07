"""Summarise a load-test run from the controller's own metrics table and the run log."""
import argparse
import os
import re
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--log")
    ap.add_argument("--wall", type=int)
    ap.add_argument("--scenario")
    ap.add_argument("--run-dir")
    a = ap.parse_args()
    db = sqlite3.connect(a.db)
    log = open(a.log).read()
    hist = re.search(r"ingested (\d+) events in ([\d.]+)s \((\d+) chunks\)", log)
    days = re.findall(r"D(\d+) 00:00\s+events=(\d+)\s+ingest=([\d.]+)s\s+loop=([\d.]+)s", log)
    rss = re.search(r"(\d+)\s+maximum resident set size", log) or re.search(r"Maximum resident set size \(kbytes\): (\d+)", log)
    rss_mb = (int(rss.group(1)) / 1024 / 1024 if "maximum resident" in rss.group(0) else int(rss.group(1)) / 1024) if rss else None
    m = db.execute("SELECT COUNT(*), SUM(events), SUM(ingest_s), SUM(decide_s), MAX(decide_s), MAX(lag_min) FROM metrics "
                   "WHERE minute>=0").fetchone()
    per_day = db.execute("SELECT minute/1440, SUM(events), SUM(ingest_s), SUM(decide_s) FROM metrics WHERE minute>=0 "
                         "GROUP BY minute/1440 HAVING SUM(events)>0 ORDER BY 1").fetchall()
    n_wf = db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
    calls = db.execute("SELECT SUM(dials) FROM agg_kpi WHERE h>=0").fetchone()[0]
    dec = db.execute("SELECT COUNT(*) FROM decisions WHERE minute>=0").fetchone()[0]
    applied = db.execute("SELECT COUNT(*) FROM proposals WHERE status='applied'").fetchone()[0]
    size = sum(os.path.getsize(a.db + s) for s in ("", "-wal") if os.path.exists(a.db + s)) / 1e6
    L = ["# Load test", "",
         f"Scenario `{a.scenario}` — {n_wf} workflows. Measured on this machine (Apple Silicon laptop, 8 cores), "
         f"controller and simulator on the same host.", "",
         "| measure | value |", "|---|---|"]
    if hist:
        ev, s = int(hist.group(1)), float(hist.group(2))
        L.append(f"| history backfill (pre-D0) | {ev:,} events in {s:.0f}s → **{ev / s:,.0f} events/s** |")
    L += [f"| live days | {len(per_day)} |",
          f"| live dials ingested | {calls:,} |",
          f"| live events ingested | {m[1]:,} in {m[2]:.0f}s → **{m[1] / m[2]:,.0f} events/s** |",
          f"| controller time per simulated day (ingest + decide) | **{(m[2] + m[3]) / max(len(per_day), 1):.0f}s** |",
          f"| decide time, worst tick | {m[4]:.2f}s |",
          f"| decision lag (sim minutes between the world and our ingested state, worst) | {m[5]} |",
          f"| wall-clock for the whole run incl. simulator stepping | {a.wall}s |",
          f"| peak controller RSS | {rss_mb:.0f} MB |" if rss_mb else "| peak RSS | n/a |",
          f"| state db size | {size:.0f} MB |",
          f"| audit decisions written / changes applied | {dec} / {applied} |", "",
          "Per simulated day (from the controller's own `metrics` table):", "",
          "| day | events | ingest s | decide s |", "|---|---|---|---|"]
    L += [f"| D{d} | {e:,} | {i:.0f} | {t:.0f} |" for d, e, i, t in per_day]

    print("\n".join(L))


if __name__ == "__main__":
    main()
