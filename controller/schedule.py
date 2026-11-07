"""Call-time optimisation (C4), per cohort. Bootstrapped from pre-D0 history (C3).

What we may change: *who* is dialled in a given hour (schedule.hour_weights), never how many or when the
window is. So the only honest lever is TIMING: within an hour, prefer the cohorts whose pickup is relatively
high at that hour, and save the others for hours that suit them. We deliberately do NOT prioritise cohorts
that simply pick up more overall — that raises the pickup *rate* by starving hard-to-reach people, which is a
cadence decision the business owns.

1. Pick the split feature: for each lead feature f, the G-test of the f × hour-of-day *interaction* (pickup
   modelled as p_v·p_h/p vs. the full table). The feature with the strongest significant interaction becomes
   the schedule's cohort axis. Typical result: age_band (morning-vs-evening people).
2. Weight: w_v(h) = I_v(h)^γ, where I_v(h) is the interaction ratio shrunk toward 1 with SCHED_PRIOR_DIALS
   pseudo-dials, clipped to [W_MIN, W_MAX] so every cohort keeps getting dialled at every hour (that is our
   exploration: the table never zeroes an hour, so we keep learning the profile).
3. Dry run: replay the lookback's dial counts through iterative proportional fitting — each hour keeps its
   capacity, each cohort keeps its daily dial count, mass moves toward weighted cells — and price it with the
   smoothed pickup table. It is an estimate under "same eligible pool" and says so.
"""
from __future__ import annotations

import math

from . import settings
from .context import WorkflowCtx
from .stats import norm_cdf


def _chi2_sf(x: float, df: int) -> float:
    if df <= 0:
        return 1.0
    if x <= 0:
        return 1.0
    z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return 1 - norm_cdf(z)


def _table(store, codec, wf: str, h_from: int, window_hours: list[int]):
    rows = store.q("SELECT fcode, h, SUM(dials), SUM(conn) FROM agg_pick WHERE wf=? AND h>=? AND fcode>=0 "
                   "GROUP BY fcode, h", (wf, h_from))
    cells: dict[tuple[int, int], list[int]] = {}
    for fc, h, n, x in rows:
        hod = h % 24
        if hod not in window_hours:
            continue
        c = cells.setdefault((fc, hod), [0, 0])
        c[0] += n
        c[1] += x
    return cells


def _by_feature(codec, cells, feature: str):
    t: dict[str, dict[int, list[int]]] = {}
    for (fc, hod), (n, x) in cells.items():
        v = codec.by_code[fc].get(feature)
        c = t.setdefault(v, {}).setdefault(hod, [0, 0])
        c[0] += n
        c[1] += x
    return t


def _interaction(t) -> tuple[float, int, dict]:
    N = sum(n for v in t.values() for n, _ in v.values())
    X = sum(x for v in t.values() for _, x in v.values())
    if N == 0 or X == 0:
        return 0.0, 0, {}
    p = X / N
    pv = {v: sum(x for _, x in hs.values()) / max(sum(n for n, _ in hs.values()), 1) for v, hs in t.items()}
    nh, xh = {}, {}
    for hs in t.values():
        for h, (n, x) in hs.items():
            nh[h] = nh.get(h, 0) + n
            xh[h] = xh.get(h, 0) + x
    ph = {h: xh[h] / nh[h] for h in nh if nh[h]}
    G = 0.0
    for v, hs in t.items():
        for h, (n, x) in hs.items():
            e = min(max(n * pv[v] * ph[h] / p, 1e-9), n - 1e-9)
            if x > 0:
                G += 2 * x * math.log(x / e)
            if n - x > 0:
                G += 2 * (n - x) * math.log((n - x) / (n - e))
    df = (len(t) - 1) * (len(nh) - 1)
    return G, df, {"p": p, "pv": pv, "ph": ph}


def compute(ctx: WorkflowCtx, store, codec, now: int) -> dict:
    wf = ctx.id
    hours = ctx.window_hours()
    h_from = now // 60 - settings.SCHED_LOOKBACK_DAYS * 24
    cells = _table(store, codec, wf, h_from, hours)
    feats = sorted({k for f in codec.by_code.values() for k in f})
    tests = {}
    best = None
    for f in feats:
        t = _by_feature(codec, cells, f)
        if len(t) < 2:
            continue
        G, df, fit = _interaction(t)
        pval = _chi2_sf(G, df)
        tests[f] = {"G": round(G, 1), "df": df, "p": pval}
        if pval < settings.SCHED_FEATURE_P and (best is None or G - df > tests[best]["G"] - tests[best]["df"]):
            best = f
    if best is None:
        return {"feature": None, "tests": tests, "reason": "no feature has a significant hour-profile interaction"}
    t = _by_feature(codec, cells, best)
    _, _, fit = _interaction(t)
    p, pv, ph = fit["p"], fit["pv"], fit["ph"]
    k = settings.SCHED_PRIOR_DIALS
    weights, smooth = {}, {}
    for v, hs in t.items():
        weights[v], smooth[v] = {}, {}
        for h in hours:
            n, x = hs.get(h, (0, 0))
            prior = pv[v] * ph.get(h, p) / p
            ps = (x + k * prior) / (n + k)          # smoothed pickup of (v, h)
            smooth[v][h] = ps
            ratio = ps / prior if prior > 0 else 1.0  # interaction ratio, shrunk toward 1
            w = min(max(ratio ** settings.SCHED_GAMMA, settings.SCHED_W_MIN), settings.SCHED_W_MAX)
            weights[v][str(h)] = round(w, 3)
    dry = dry_run(t, weights, smooth, hours)
    return {"feature": best, "values": sorted(t), "weights": weights, "tests": tests, "dry_run": dry,
            "gain": dry["expected_rel_change"], "computed_minute": now}


def dry_run(t, weights, smooth, hours) -> dict:
    vals = sorted(t)
    n0 = {(v, h): float(t[v].get(h, (0, 0))[0]) for v in vals for h in hours}
    row = {v: sum(n0[(v, h)] for h in hours) for v in vals}
    col = {h: sum(n0[(v, h)] for v in vals) for h in hours}
    M = {(v, h): n0[(v, h)] * weights[v][str(h)] for v in vals for h in hours}
    for _ in range(50):
        for v in vals:
            s = sum(M[(v, h)] for h in hours)
            if s > 0:
                for h in hours:
                    M[(v, h)] *= row[v] / s
        for h in hours:
            s = sum(M[(v, h)] for v in vals)
            if s > 0:
                for v in vals:
                    M[(v, h)] *= col[h] / s
    N = sum(n0.values())
    cur = sum(n0[c] * smooth[c[0]][c[1]] for c in n0) / N if N else 0
    new = sum(M[c] * smooth[c[0]][c[1]] for c in M) / N if N else 0
    return {"method": "IPF replay of lookback dials: hour capacity and cohort daily volume held fixed",
            "dials_replayed": int(N), "pickup_current": round(cur, 4), "pickup_expected": round(new, 4),
            "expected_rel_change": round(new / cur - 1, 4) if cur else 0.0,
            "caveat": "assumes the eligible pool per hour looks like the lookback; ignores cadence depletion"}


def step(ctx: WorkflowCtx, store, codec, now: int, force: bool = False) -> dict | None:
    """Recompute once a day (07:00, before dialling) or when forced; keep the state only if material."""
    wf = ctx.id
    st = store.kv(wf, "schedule")
    due = force or st is None or (now % 1440 == 7 * 60 and now // 1440 > st.get("computed_minute", -10**9) // 1440)
    if not due:
        return st
    new = compute(ctx, store, codec, now)
    if new["feature"] is None or new["gain"] < settings.SCHED_MIN_GAIN:
        if st is None:
            store.set_kv(wf, "schedule", {"feature": None, "computed_minute": now, "tests": new.get("tests"),
                                          "gain": new.get("gain")})
            store.decide(wf, "schedule", "no_schedule",
                         f"no call-time change: {new.get('reason') or 'expected gain below threshold'}",
                         {"tests": new.get("tests"), "dry_run": new.get("dry_run")})
        return store.kv(wf, "schedule")
    material = st is None or st.get("feature") != new["feature"] or any(
        abs(math.log(new["weights"][v][h] / st["weights"].get(v, {}).get(h, 1.0)))
        > settings.SCHED_MATERIAL_LOG_CHANGE for v in new["weights"] for h in new["weights"][v])
    if material:
        store.set_kv(wf, "schedule", new)
        store.decide(wf, "schedule", "schedule_computed",
                     f"call-time by {new['feature']}: expected pickup {new['dry_run']['pickup_current']:.3f} → "
                     f"{new['dry_run']['pickup_expected']:.3f} ({new['gain']:+.1%})",
                     {"feature": new["feature"], "tests": new["tests"], "dry_run": new["dry_run"],
                      "weights": new["weights"]})
    else:
        st["computed_minute"] = now
        store.set_kv(wf, "schedule", st)
    return store.kv(wf, "schedule")
