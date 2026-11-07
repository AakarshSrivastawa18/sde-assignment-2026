"""Verify a (possibly crash-interrupted) controller run against the simulator's own records.

Checks:
  1. the scenario finished;
  2. no proposal/approval was applied twice (sim config history);
  3. no approval was lost: every approval the sim knows is in our store, with a consistent status, and nothing
     approved was left unapplied unless our store says why (superseded / lapsed / stale = apply hit a newer
     base version, e.g. a supervisor edit, and was re-proposed — the sim keeps such approvals as "approved");
  4. ingestion is exactly-once: our aggregate dial count == call events in the sim's chunks;
  5. optional: the controller-made config history equals an uninterrupted reference run's.
"""
import argparse
import json
import sqlite3
from collections import Counter

import httpx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--reference-db")
    ap.add_argument("--kills")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.url, timeout=300)
    db = sqlite3.connect(a.db)
    out, ok = ["# Crash-recovery test", ""], True
    if a.kills:
        out += ["## Kills", "", "```", open(a.kills).read().strip(), "```", ""]
    st = c.get("/sim/state").json()
    out += ["## Invariants", "", f"- scenario `{st['scenario']}` finished: **{st['finished']}** at {st['ts']}"]
    ok &= st["finished"]
    restarts = db.execute("SELECT COUNT(*) FROM decisions WHERE kind='restart'").fetchone()[0]
    recovered = db.execute("SELECT COUNT(*) FROM decisions WHERE summary LIKE '%reconciled%'").fetchone()[0]
    out.append(f"- controller restarts recorded in our audit log: {restarts}; governance intents reconciled on restart: {recovered}")
    dup = []
    hist = {}
    for w in c.get("/workflows").json():
        h = c.get(f"/workflows/{w['workflow_id']}/config/history").json()
        hist[w["workflow_id"]] = h
        for key in ("proposal_id", "approval_id"):
            cnt = Counter(v[key] for v in h if v[key])
            dup += [(w["workflow_id"], key, k, n) for k, n in cnt.items() if n > 1]
    out.append(f"- changes applied twice: **{len(dup)}** {dup or ''}")
    ok &= not dup
    ours = {r[0]: r[1:] for r in db.execute("SELECT approval_id, status, proposal_id FROM proposals WHERE approval_id IS NOT NULL")}
    lost, mismatch = [], []
    for ap_ in c.get("/approvals").json():
        mine = ours.get(ap_["approval_id"])
        if not mine:
            lost.append(ap_["approval_id"])
            continue
        s_sim, s_ours = ap_["status"], mine[0]
        consistent = s_sim == s_ours or (s_sim == "approved" and s_ours in ("superseded", "lapsed", "stale")) or \
            (s_sim == "lapsed" and s_ours == "lapsed")
        if not consistent:
            mismatch.append((ap_["approval_id"], s_sim, s_ours))
    out.append(f"- approvals known to the simulator but missing from our store: **{len(lost)}** {lost or ''}")
    out.append(f"- approvals whose status disagrees: **{len(mismatch)}** {mismatch or ''}")
    ok &= not lost and not mismatch
    n_calls = 0
    for ch in c.get("/events/chunks", params={"kind": "history", "after": -1}).json() + \
            c.get("/events/chunks", params={"kind": "events", "after": -1}).json():
        n_calls += c.get(f"/events/chunks/{ch['chunk']}").text.count('"type":"call"')
    ours_dials = db.execute("SELECT SUM(dials) FROM agg_kpi").fetchone()[0]
    out.append(f"- call events in the simulator's chunks: {n_calls}; dials in our aggregates: {ours_dials} → "
               f"**{'exactly once' if n_calls == ours_dials else 'MISMATCH'}**")
    ok &= n_calls == ours_dials
    if a.reference_db:
        try:
            ref = sqlite3.connect(a.reference_db)
            q = ("SELECT wf, version, actor, changed_dims, applied_minute FROM config_versions WHERE actor LIKE 'controller%' "
                 "ORDER BY wf, version")
            mine_h, ref_h = db.execute(q).fetchall(), ref.execute(q).fetchall()
            same = mine_h == ref_h
            out += ["", "## Same decisions as an uninterrupted run?", "",
                    f"- controller-made config versions: crashed run {len(mine_h)}, uninterrupted run {len(ref_h)} → "
                    f"**{'identical (version, actor, dims, minute)' if same else 'different'}**"]
            if not same:
                diff = [x for x in mine_h if x not in ref_h][:5]
                out.append(f"- first differences (crashed run): {diff}")
        except sqlite3.Error as e:
            out.append(f"- reference comparison skipped: {e}")
    out += ["", f"**RESULT: {'PASS' if ok else 'FAIL'}**"]
    print("\n".join(out))


if __name__ == "__main__":
    main()
