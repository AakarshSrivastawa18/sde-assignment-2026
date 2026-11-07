"""KPI drift + guardrail monitor (detection for 'pickup or engagement drifting').

Once a day before dialling, each workflow's primary and guardrail KPIs over the last 48 abs hours are compared
with the pre-D0 history baseline. Population test only: a two-proportion z on tens of thousands of dials.

  * Guardrail breach (drop > max_drop, absolute, and z ≤ -3): ticket + our call-time schedule is suspended for
    72h. The schedule is the one change we make that alters WHO picks up (a mix effect), so it is the one that
    can move the other KPI without any per-person harm; experiments carry their own concurrent controls and
    caller-ID changes are judged against peer numbers, so they need no extra backstop here.
  * Primary drift (drop > 2× max_drop or 5% relative, z ≤ -3): ticket only — it is usually the world (a bad
    number, a supervisor's prompt), and the specific detectors own the response.

Assumption (SUBMISSION.md §1): `*_max_drop` is an absolute drop in the rate (0.02 = 2 points).
"""
from __future__ import annotations

import math

SUSPEND_HOURS = 72


def _rate(store, wf: str, h_from: int, h_to: int, kpi: str) -> tuple[float, int]:
    dials, conn, eng = store.q("SELECT COALESCE(SUM(dials),0), COALESCE(SUM(conn),0), COALESCE(SUM(eng),0) "
                               "FROM agg_kpi WHERE wf=? AND h>=? AND h<?", (wf, h_from, h_to))[0]
    if kpi == "pickup_rate":
        return (conn / dials if dials else 0.0), dials
    return (eng / conn if conn else 0.0), conn


def check(ctx, store, now: int):
    if now % 1440 != 8 * 60 or now < 2 * 1440:
        return
    wf = ctx.id
    base = store.kv(wf, "baseline") or {}
    h = now // 60
    gname, gmax = ctx.guardrail
    for kpi, role in ((ctx.primary, "primary"), (gname, "guardrail")):
        if not kpi or base.get(kpi) is None:
            continue
        b = base[kpi]
        nb = base.get("dials") if kpi == "pickup_rate" else (base.get("dials") or 0) * base.get("pickup_rate", 0.3)
        r, n = _rate(store, wf, h - 48, h, kpi)
        if n < 500 or not nb:
            continue
        se = math.sqrt(b * (1 - b) / n + b * (1 - b) / nb)
        z = (r - b) / se if se else 0.0
        drop = b - r
        detail = {"kpi": kpi, "role": role, "baseline": round(b, 4), "last_48h": round(r, 4), "n": n,
                  "z": round(z, 2), "max_drop": gmax}
        if role == "guardrail" and drop > gmax and z <= -3:
            sched = store.kv(wf, "schedule") or {}
            store.alert("ticket", wf, "guardrail_breach",
                        f"{wf}: guardrail {kpi} {r:.3f} vs baseline {b:.3f} (drop {drop:.3f} > {gmax}); "
                        f"call-time schedule suspended {SUSPEND_HOURS}h")
            if sched.get("feature"):
                store.set_kv(wf, "schedule_suspended_until", now + SUSPEND_HOURS * 60)
            store.decide(wf, "guardrail", "guardrail_breach",
                         f"{kpi} fell {drop:.3f} below its history baseline (limit {gmax}, z={z:.1f}); "
                         f"backing out our call-time weights for {SUSPEND_HOURS}h", detail)
        elif role == "primary" and (drop > 2 * max(gmax, 0.01) or drop > 0.05 * b) and z <= -3:
            store.alert("ticket", wf, "primary_drift", f"{wf}: primary {kpi} {r:.3f} vs baseline {b:.3f} (z={z:.1f})")
            store.decide(wf, "guardrail", "primary_drift",
                         f"primary {kpi} is {r:.3f} vs history {b:.3f} (z={z:.1f}) over the last 48h", detail)
