"""Caller-ID health (C5): detect a degrading number, rotate off it, explore unused numbers safely.

Measurement is observed/expected pickups. For number d, every abs hour h in the window contributes
E += dials(d,h) × pickup-rate of the *other* numbers in that same hour. Same hour ⇒ same leads pool, same
time-of-day, same schedule — caller_id is randomised per dial, so the only systematic difference left is the
number itself. O/E ≈ 1 is healthy; a spam-flagged number shows O/E ≈ its health factor.

Populations, not anecdotes: no verdict until E ≥ DID_MIN_EXPECTED_PICKUPS, and "bad" needs both a large
effect (O/E ≤ 0.75) and significance (z ≤ -3, variance includes the noise in the peers' rate).
"""
from __future__ import annotations

import math

from . import settings
from .context import WorkflowCtx, dist_of, norm


def measure(store, wf: str, h_from: int, h_to: int) -> dict:
    rows = store.q("SELECT h, did, SUM(dials), SUM(conn) FROM agg_pick WHERE wf=? AND h>=? AND h<? GROUP BY h, did",
                   (wf, h_from, h_to))
    by_h: dict[int, dict[str, tuple[int, int]]] = {}
    for h, did, n, x in rows:
        by_h.setdefault(h, {})[did] = (n, x)
    out = {}
    dids = {d for v in by_h.values() for d in v}
    for d in dids:
        O = E = var_e = 0.0
        n_d = 0
        for h, per in by_h.items():
            if d not in per:
                continue
            n, x = per[d]
            on = sum(v[0] for k, v in per.items() if k != d)
            ox = sum(v[1] for k, v in per.items() if k != d)
            if on < 10:
                continue
            p = ox / on
            O += x
            E += n * p
            var_e += n * n * p * (1 - p) / on
            n_d += n
        if E <= 0:
            continue
        z = (O - E) / math.sqrt(E + var_e)
        out[d] = {"dials": n_d, "observed": int(O), "expected": round(E, 1), "ratio": round(O / E, 3),
                  "z": round(z, 2)}
    return out


def step(ctx: WorkflowCtx, store, now: int) -> dict:
    """Update health verdicts; return {'dist': desired caller_id distribution, 'urgent': bool, 'explain': {...}}."""
    wf = ctx.id
    st = store.kv(wf, "did", None) or {"status": {d: "unknown" for d in ctx.options("caller_id")}, "evidence": {}}
    h_now = now // 60
    cur = dist_of(ctx.config, "default", "caller_id") if ctx.config else {}
    urgent = False
    # 1) fast incident check on numbers carrying traffic
    fast = measure(store, wf, h_now - settings.DID_INCIDENT_WINDOW_H, h_now)
    census = measure(store, wf, h_now - settings.DID_HEALTH_WINDOW_H, h_now)
    for src, d, m in [("fast", d, m) for d, m in fast.items()] + [("census", d, m) for d, m in census.items()]:
        old = st["status"].get(d, "unknown")
        if old == "bad" or m["expected"] < settings.DID_MIN_EXPECTED_PICKUPS:
            continue
        if m["ratio"] <= settings.DID_BAD_RATIO and m["z"] <= settings.DID_BAD_Z:
            st["status"][d] = "bad"
            st["evidence"][d] = {**m, "at": now}
            share = cur.get(d, 0)
            urgent = urgent or share > 0
            store.decide(wf, "caller_id", "did_degraded",
                         f"{d} picks up at {m['ratio']:.0%} of peer numbers in the same hours "
                         f"(O={m['observed']} vs E={m['expected']}, z={m['z']}); carrying {share:.0%} of dials",
                         {"did": d, "measure": m, "share": share})
            if share > 0:
                store.alert("page" if share >= 0.25 else "ticket", wf, f"did_degraded_{d}",
                            f"{wf}: caller ID {d} degraded (O/E {m['ratio']:.2f}, z {m['z']}); "
                            f"controller is rotating it out — check the number's spam status with the carrier")
        elif old == "unknown" and src == "census" and m["ratio"] >= settings.DID_HEALTHY_RATIO:
            st["status"][d] = "healthy"
            st["evidence"][d] = {**m, "at": now}
            store.decide(wf, "caller_id", "did_healthy", f"{d} measured healthy (O/E {m['ratio']:.2f}, "
                         f"E={m['expected']})", {"did": d, "measure": m})
    store.set_kv(wf, "did", st)
    # 2) desired distribution: healthy numbers share evenly; ≤2 unknowns explored at a small share
    healthy = [d for d in ctx.options("caller_id") if st["status"].get(d) == "healthy"]
    unknown = [d for d in ctx.options("caller_id") if st["status"].get(d) == "unknown"]
    if not healthy:
        # nothing measured yet (e.g. a single number in history): keep live numbers that are not bad,
        # and explore unknowns next to them
        healthy = [d for d in cur if st["status"].get(d) != "bad"]
        unknown = [d for d in unknown if d not in healthy]
    if not healthy:
        return {"dist": cur, "urgent": urgent, "status": st["status"]}
    explore = unknown[: settings.DID_MAX_UNKNOWN_AT_ONCE]
    dist = {d: settings.DID_EXPLORE_SHARE for d in explore}
    rest = 1.0 - sum(dist.values())
    for d in healthy:
        dist[d] = rest / len(healthy)
    return {"dist": norm(dist), "urgent": urgent, "status": st["status"],
            "evidence": {d: st["evidence"].get(d) for d in dist}}


def expected_pickup_change(cur: dict, new: dict, status_evidence: dict) -> float:
    """Relative pickup change from moving share between numbers, using measured O/E (1.0 if unmeasured)."""
    def r(d):
        e = status_evidence.get(d) or {}
        return float(e.get("ratio", 1.0))
    a = sum(w * r(d) for d, w in norm(cur).items())
    b = sum(w * r(d) for d, w in norm(new).items())
    return b / a - 1 if a > 0 else 0.0
