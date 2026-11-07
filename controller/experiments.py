"""Variant experimentation on welcome_message and voice (C6) with promotion, decay detection and rollback (C7).

Design (SUBMISSION.md §5 has the reasoning and the numbers):
  * Factorial by construction: the dialler samples welcome and voice independently per call, so each dim's
    arms are balanced over the other dim. We test MAIN effects per dim; interactions are not modelled.
  * Metric: engagement = engaged / connected. Only calls that actually happened count — an arm whose
    approval never came simply has no calls and no evidence.
  * Estimator: stratified (day × config_version × prompt) difference vs the segment's control arm.
  * Exploration: an untested arm gets EXPLORE_SHARE; a promising one (P(better) ≥ 0.9) gets RAMP_SHARE.
    Ramping is an allocation choice, not a verdict.
  * Verdict (deterministic): once a day, at a fixed look time. An arm wins if n ≥ MIN_CONNECTED_PER_ARM, its
    lift ≥ MIN_LIFT_TO_PROMOTE and z ≥ z_crit, where z_crit is Bonferroni over the arms compared and
    inflated for ~10 daily looks (Pocock). Losers are dropped (harm) or retired (futility).
  * Segments: a winner can be specific to a feature value (e.g. a voice that works for 60+). Segment tests
    run over every feature value with ≥ SEGMENT_MIN_SHARE of traffic, Bonferroni over all of them. A
    significant segment effect that the global decision doesn't capture splits the dim by that feature —
    which becomes a cohort axis in the shared CohortPlan.
  * Decay: after promotion the old control keeps HOLDBACK. Every 2h we compare winner vs holdback over the
    last 48 abs hours; we roll back if the lift has fallen significantly below the lift we promoted on, or is
    significantly negative. Rollback is cheap and safe, so its evidence bar is deliberately lower.
"""
from __future__ import annotations

import math

from . import settings
from .context import WorkflowCtx, dist_of, norm
from .stats import Diff, stratified_diff, z_crit

DECAY_WINDOW_ABS_H = 48


def _new_seg(control: str, options: list[str]) -> dict:
    return {"control": control, "arms": {o: "active" for o in options if o != control}, "status": "exploring",
            "winner": None, "promoted_h": None, "promo": None}


def init_state(ctx: WorkflowCtx, dim: str) -> dict:
    cur = dist_of(ctx.config, "default", dim)
    control = max(cur, key=lambda o: (cur[o], o))
    return {"partition": None, "segs": {"*": _new_seg(control, ctx.options(dim))}, "pending_plan": False}


# ---------------------------------------------------------------- data
class ArmData:
    """Connected/engaged counts for one dim: rows of (stratum, fcode, arm, n, x)."""

    def __init__(self, store, wf: str, dim: str, h_from: int):
        col = "wm" if dim == "welcome_message" else "vo"
        self.rows = store.q(
            f"SELECT h/24, cv, pv, fcode, {col}, SUM(conn), SUM(eng) FROM agg_eng WHERE wf=? AND h>=? "
            f"GROUP BY h/24, cv, pv, fcode, {col}", (wf, h_from))

    @staticmethod
    def window(store, wf: str, dim: str, h_from: int, h_to: int) -> "ArmData":
        col = "wm" if dim == "welcome_message" else "vo"
        a = ArmData.__new__(ArmData)
        a.rows = store.q(
            f"SELECT h/24, cv, pv, fcode, {col}, SUM(conn), SUM(eng) FROM agg_eng WHERE wf=? AND h>=? AND h<? "
            f"GROUP BY h/24, cv, pv, fcode, {col}", (wf, h_from, h_to))
        return a

    def diff(self, arm: str, ctl: str, fcodes: set[int] | None = None) -> Diff | None:
        acc: dict[tuple, list[int]] = {}
        for d, cv, pv, fc, a, n, x in self.rows:
            if a != arm and a != ctl:
                continue
            if fcodes is not None and fc not in fcodes:
                continue
            s = acc.setdefault((d, cv, pv), [0, 0, 0, 0])
            if a == arm:
                s[0] += x
                s[1] += n
            else:
                s[2] += x
                s[3] += n
        return stratified_diff(acc.values())

    def share(self, fcodes: set[int] | None) -> float:
        tot = sum(r[5] for r in self.rows)
        if fcodes is None:
            return 1.0
        return sum(r[5] for r in self.rows if r[3] in fcodes) / tot if tot else 0.0


def seg_codes(codec, partition: str | None, seg: str) -> set[int] | None:
    if partition is None or seg == "*":
        return None
    return {c for c, f in codec.by_code.items() if f.get(partition) == seg}


# ---------------------------------------------------------------- allocation
def seg_dist(seg: dict) -> dict:
    if seg["status"] == "promoted":
        return norm({seg["winner"]: 1 - settings.HOLDBACK, seg["control"]: settings.HOLDBACK})
    if seg["status"] == "done":
        return {seg["control"]: 1.0}
    shares = {a: (settings.RAMP_SHARE if s == "ramped" else settings.EXPLORE_SHARE)
              for a, s in seg["arms"].items() if s in ("active", "ramped")}
    tot = sum(shares.values())
    if tot > settings.MAX_CHALLENGER_TOTAL:
        shares = {a: v * settings.MAX_CHALLENGER_TOTAL / tot for a, v in shares.items()}
    shares[seg["control"]] = 1 - sum(shares.values())
    return norm(shares)


def desired(state: dict) -> dict:
    """{'partition': f|None, 'dists': {segment value or '*': {option: share}}}"""
    return {"partition": state["partition"], "dists": {k: seg_dist(s) for k, s in state["segs"].items()}}


# ---------------------------------------------------------------- the daily look
def look(ctx: WorkflowCtx, store, codec, dim: str, now: int, intraday: bool = False) -> dict:
    """Run tests, update arm statuses, promote/drop/rollback. Returns {'changed': bool, 'urgent': bool}."""
    wf = ctx.id
    st = store.kv(wf, f"exp:{dim}") or init_state(ctx, dim)
    if st.get("last_look_minute") == now:
        # idempotency: a look and its state changes commit together; re-running the same tick after a crash must
        # not take a second step on the same data (it once turned a fresh segment split into a promotion)
        return {"changed": False, "urgent": False}
    st["last_look_minute"] = now
    start_h = st.setdefault("start_h", now // 60)
    data = ArmData(store, wf, dim, start_h)
    # a decaying arm hides behind its good early days in cumulative data: also judge the recent window
    recent = ArmData(store, wf, dim, max(start_h, now // 60 - DECAY_WINDOW_ABS_H))
    # promotion stability: the effect must still be there over the last day (holding costs a day; promoting a
    # fading variant harms everyone it reaches)
    days = [ArmData.window(store, wf, dim, max(start_h, now // 60 - 24 * (i + 1)), now // 60 - 24 * i)
            for i in range(2)]
    changed = urgent = False
    for key, seg in st["segs"].items():
        codes = seg_codes(codec, st["partition"], key)
        label = f"{dim}[{key if key != '*' else 'all'}]"
        if seg["status"] == "exploring":
            live = [a for a, s in seg["arms"].items() if s in ("active", "ramped")]
            zc = z_crit(len(live), settings.FAMILY_ALPHA, settings.POCOCK_FACTOR)
            res = {a: data.diff(a, seg["control"], codes) for a in live}
            winners = []
            harm_z = settings.SEVERE_HARM_Z if intraday else settings.HARM_DROP_Z
            for a, d in res.items():
                if d is None:
                    continue
                dr = recent.diff(a, seg["control"], codes)
                for which, x in (("cumulative", d), ("last 48h", dr)):
                    if x is not None and x.n_arm >= settings.HARM_MIN_N and x.z <= harm_z:
                        break
                else:
                    x = None
                if x is not None:
                    seg["arms"][a] = "dropped"
                    seg.setdefault("drop_reason", {})[a] = "harm"
                    changed = True
                    urgent = urgent or intraday
                    store.decide(wf, "experiment", "arm_dropped_harm",
                                 f"{label}: {a} is worse than {seg['control']} ({which}: lift {x.lift:+.1%}, "
                                 f"z={x.z:.2f}, n={x.n_arm}); stop exposing customers to it",
                                 {"dim": dim, "seg": key, "arm": a, "window": which, "stats": x.as_dict(),
                                  "cumulative": d.as_dict()})
                    continue
                if intraday:
                    continue
                if seg["arms"][a] == "ramped" and dr is not None and dr.n_arm >= settings.HARM_MIN_N and \
                        dr.lift < settings.MIN_LIFT_TO_PROMOTE / 2:
                    # symmetric to ramping: extra traffic only while the effect is visible in recent data
                    seg["arms"][a] = "active"
                    changed = True
                    store.decide(wf, "experiment", "arm_deramped",
                                 f"{label}: {a} no longer shows its effect (last 48h lift {dr.lift:+.1%}, n={dr.n_arm}; "
                                 f"cumulative {d.lift:+.1%}) — back to {settings.EXPLORE_SHARE:.0%}",
                                 {"dim": dim, "seg": key, "arm": a, "recent": dr.as_dict(), "cumulative": d.as_dict()})
                    continue
                if d.n_arm >= settings.FUTILITY_MIN_N and d.lift_ci(1.645)[1] < settings.MIN_LIFT_TO_PROMOTE:
                    seg["arms"][a] = "dropped"
                    changed = True
                    store.decide(wf, "experiment", "arm_dropped_futile",
                                 f"{label}: {a} cannot plausibly beat {seg['control']} by "
                                 f"{settings.MIN_LIFT_TO_PROMOTE:.0%} (95% upper bound "
                                 f"{d.lift_ci(1.645)[1]:+.1%}, n={d.n_arm})",
                                 {"dim": dim, "seg": key, "arm": a, "stats": d.as_dict()})
                    continue
                lo = (d.diff - zc * d.se) / d.p_ctl if d.p_ctl else 0
                if d.n_arm >= settings.MIN_CONNECTED_PER_ARM and d.z >= zc and d.lift >= settings.MIN_LIFT_TO_PROMOTE \
                        and lo > 0:
                    winners.append((lo, a, d))
                elif seg["arms"][a] == "active" and d.n_arm >= settings.RAMP_MIN_N and d.z >= settings.RAMP_Z \
                        and d.lift >= settings.MIN_LIFT_TO_PROMOTE and _still_there(days[0], a, seg["control"], codes):
                    seg["arms"][a] = "ramped"
                    changed = True
                    store.decide(wf, "experiment", "arm_ramped",
                                 f"{label}: {a} looks promising (lift {d.lift:+.1%}, z={d.z:.2f}, n={d.n_arm}); "
                                 f"more traffic to reach a verdict sooner — not a winner yet",
                                 {"dim": dim, "seg": key, "arm": a, "stats": d.as_dict()})
            held = []
            for w in sorted(winners, reverse=True):
                lo, a, d = w
                daily = [x.diff(a, seg["control"], codes) for x in days]
                # each of the last two days must show the effect, and the last day must keep at least half of the
                # cumulative lift (a weak trend test: novelty effects fade within days)
                floors = [max(settings.MIN_LIFT_TO_PROMOTE / 2, d.lift / 2), settings.MIN_LIFT_TO_PROMOTE / 2]
                weak = [i for i, x in enumerate(daily) if x is not None and x.n_arm >= settings.STABILITY_MIN_N
                        and x.lift < floors[i]]
                if weak:
                    held.append(w)
                    store.decide(wf, "experiment", "promotion_held",
                                 f"{label}: {a} passes the cumulative test (lift {d.lift:+.1%}) but its lift was "
                                 + ", ".join(f"{daily[i].lift:+.1%} {'in the last 24h' if i == 0 else '24–48h ago'}"
                                             for i in weak) + " — a fading (novelty) effect is not promoted",
                                 {"dim": dim, "seg": key, "arm": a, "cumulative": d.as_dict(),
                                  "daily": [x.as_dict() if x else None for x in daily]})
            winners = [w for w in winners if w not in held]
            if winners:
                lo, a, d = max(winners)
                seg.update(status="promoted", winner=a, promoted_h=now // 60,
                           promo={"diff": d.diff, "se": d.se, "lift": d.lift})
                changed = True
                store.decide(wf, "experiment", "winner_promoted",
                             f"{label}: {a} beats {seg['control']} — lift {d.lift:+.1%} (lower bound {lo:+.1%}), "
                             f"z={d.z:.2f} ≥ {zc:.2f} (Bonferroni over {len(live)} arms, ~10 looks), "
                             f"n={d.n_arm} vs {d.n_ctl}; promote with {settings.HOLDBACK:.0%} holdback on "
                             f"{seg['control']}", {"dim": dim, "seg": key, "arm": a, "stats": d.as_dict(),
                                                   "z_crit": zc, "all_arms": {k: v.as_dict() for k, v in res.items()
                                                                              if v}})
            elif not intraday and not [a for a, s in seg["arms"].items() if s in ("active", "ramped")]:
                seg["status"] = "done"
                changed = True
                store.decide(wf, "experiment", "experiment_concluded",
                             f"{label}: no arm beat {seg['control']}; keep {seg['control']}", {"dim": dim, "seg": key})
        elif seg["status"] == "promoted" and not _live(ctx, st, key, seg, dim):
            # decided but not yet applied (waiting for approval): the evidence must still hold when it ships
            daily = [x.diff(seg["winner"], seg["control"], codes) for x in days]
            weak = [x for x in daily if x is not None and x.n_arm >= settings.STABILITY_MIN_N
                    and x.lift < settings.MIN_LIFT_TO_PROMOTE / 2]
            if weak:
                old = seg["winner"]
                seg["arms"][old] = "ramped"
                seg.update(status="exploring", winner=None, promoted_h=None, promo=None)
                changed = True
                store.decide(wf, "experiment", "promotion_withdrawn",
                             f"{label}: {old} was promoted but is not live yet, and its daily lift is now "
                             + ", ".join(f"{x.lift:+.1%}" for x in weak) + " — withdrawn before it ships",
                             {"dim": dim, "seg": key, "arm": old, "daily": [x.as_dict() if x else None for x in daily]})
        elif seg["status"] == "promoted":
            r = decay_check(store, wf, dim, seg, codes, now)
            if r is not None:
                seg["arms"][seg["winner"]] = "rolled_back"
                old = seg["winner"]
                seg.update(status="exploring", winner=None, promoted_h=None, promo=None)
                if not [a for a, s in seg["arms"].items() if s in ("active", "ramped")]:
                    seg["status"] = "done"
                changed = urgent = True
                store.decide(wf, "experiment", "winner_rolled_back",
                             f"{label}: {old} stopped winning — {r['why']}; back to {seg['control']}",
                             {"dim": dim, "seg": key, "arm": old, **r})
    if not intraday and st["partition"] is None:
        changed |= _maybe_split(ctx, store, codec, dim, st, data, recent, now)
    store.set_kv(wf, f"exp:{dim}", st)
    return {"changed": changed, "urgent": urgent}


def _still_there(day: ArmData, arm: str, ctl: str, codes) -> bool:
    """More exposure only if the effect is visible in the last 24h too (or there is too little data to say)."""
    x = day.diff(arm, ctl, codes)
    return x is None or x.n_arm < settings.STABILITY_MIN_N or x.lift >= settings.MIN_LIFT_TO_PROMOTE / 2


def _live(ctx: WorkflowCtx, st: dict, key: str, seg: dict, dim: str) -> bool:
    """Is this segment's promotion actually in the live config (winner carries the majority)?"""
    cohort = "default"
    if st["partition"] and key != "*":
        cohort = next((c.id for c in ctx.live_cohorts() if c.value(st["partition"]) == key), None)
        if cohort is None:
            return False
    return dist_of(ctx.config, cohort, dim).get(seg["winner"], 0) >= 0.5


def decay_check(store, wf: str, dim: str, seg: dict, codes, now: int) -> dict | None:
    h_now = now // 60
    if h_now - seg["promoted_h"] < settings.DECAY_MIN_AGE_HOURS:
        return None
    window = ArmData(store, wf, dim, max(seg["promoted_h"], h_now - DECAY_WINDOW_ABS_H))
    d = window.diff(seg["winner"], seg["control"], codes)
    if d is None or d.n_ctl < settings.DECAY_MIN_CTL:
        return None
    p = seg["promo"]
    z_drop = (p["diff"] - d.diff) / math.sqrt(p["se"] ** 2 + d.se ** 2) if d.se > 0 else 0
    if d.diff < 0 and d.z <= settings.DECAY_NEG_Z:
        return {"why": f"lift is now {d.lift:+.1%} (z={d.z:.2f})", "window": d.as_dict(), "promo": p}
    if z_drop >= settings.DECAY_DIFF_Z and d.lift < 0.01:
        return {"why": f"lift fell from {p['lift']:+.1%} at promotion to {d.lift:+.1%} (drop z={z_drop:.2f})",
                "window": d.as_dict(), "promo": p}
    return None


def _maybe_split(ctx, store, codec, dim: str, st: dict, data: ArmData, recent: ArmData, now: int) -> bool:
    seg0 = st["segs"]["*"]
    if seg0["status"] == "done" and not any(s != "dropped" for s in seg0["arms"].values()):
        pass  # concluded globally; a segment effect can still exist — keep looking
    incumbent = seg0["winner"] if seg0["status"] == "promoted" else seg0["control"]
    feats = sorted({k for f in codec.by_code.values() for k in f})
    cells = []
    for f in feats:
        for v in sorted({fv[f] for fv in codec.by_code.values() if f in fv}):
            codes = {c for c, fv in codec.by_code.items() if fv.get(f) == v}
            share = data.share(codes)
            if share < settings.SEGMENT_MIN_SHARE:
                continue
            for a in ctx.options(dim):
                # never revive an arm we rolled back (decay) or dropped for harm via a segment
                if a == incumbent or seg0["arms"].get(a) == "rolled_back" or \
                        (seg0.get("drop_reason") or {}).get(a) == "harm":
                    continue
                dr = recent.diff(a, incumbent, codes)
                if dr is not None and dr.n_arm >= settings.STABILITY_MIN_N and dr.lift < settings.MIN_LIFT_TO_PROMOTE / 2:
                    continue
                d = data.diff(a, incumbent, codes)
                if d is not None:
                    cells.append((f, v, a, share, d))
    if not cells:
        return False
    m = len(cells)
    zc = z_crit(m, settings.FAMILY_ALPHA, settings.POCOCK_FACTOR)
    sig = [(f, v, a, s, d) for f, v, a, s, d in cells
           if d.n_arm >= settings.MIN_CONNECTED_PER_ARM * 2 // 3 and d.z >= zc and d.lift >= settings.SEGMENT_MIN_LIFT]
    if not sig:
        return False
    gain: dict[str, float] = {}
    for f, v, a, s, d in sig:
        gain[f] = gain.get(f, 0) + s * d.lift
    plan = ctx.plan()
    for f in sorted(gain, key=lambda k: -gain[k]):
        vals = sorted({fv[f] for fv in codec.by_code.values() if f in fv})
        if plan.with_feature(f, vals) is None:
            continue
        segs = {}
        for v in vals:
            s = {**seg0, "arms": dict(seg0["arms"]), "promo": dict(seg0["promo"]) if seg0["promo"] else None}
            best = max(((d.diff - zc * d.se) / d.p_ctl, a, d) for ff, vv, a, _, d in sig if ff == f and vv == v) \
                if any(ff == f and vv == v for ff, vv, *_ in sig) else None
            if best:
                # a segment finding earns the segment more traffic, not an instant promotion: the winner must
                # still pass the segment's own daily look (incl. the two-day stability check)
                lo, a, d = best
                s.update(status="exploring", winner=None, promoted_h=None, promo=None, control=incumbent)
                s["arms"] = {o: st_ for o, st_ in s["arms"].items() if o != incumbent}
                if seg0["control"] != incumbent:
                    s["arms"].setdefault(seg0["control"], "active")
                s["arms"][a] = "ramped"
            segs[v] = s
        st["partition"], st["segs"], st["pending_plan"] = f, segs, True
        store.decide(ctx.id, "experiment", "segment_split",
                     f"{dim}: effect differs by {f} — " + "; ".join(
                         f"{a} in {f}={v}: lift {d.lift:+.1%} (z={d.z:.2f}≥{zc:.2f}, n={d.n_arm})"
                         for ff, v, a, _, d in sig if ff == f) + f"; split {dim} by {f} (new cohorts) and ramp those "
                     f"arms in their segment — promotion still needs the segment's own look",
                     {"dim": dim, "feature": f, "z_crit": zc, "tests": m,
                      "significant": [{"value": v, "arm": a, **d.as_dict()} for ff, v, a, _, d in sig if ff == f]})
        return True
    return False


def on_supervisor_edit(ctx: WorkflowCtx, store, dim: str):
    """A supervisor changed this dim: their choice becomes the control everywhere; promotions are cleared."""
    st = store.kv(ctx.id, f"exp:{dim}")
    if not st:
        return
    for key, seg in st["segs"].items():
        cohort = "default"
        if st["partition"] and key != "*":
            for c in ctx.live_cohorts():
                if c.value(st["partition"]) == key:
                    cohort = c.id
                    break
        cur = dist_of(ctx.config, cohort, dim)
        control = max(cur, key=lambda o: (cur[o], o))
        arms = {o: ("dropped" if seg["arms"].get(o) in ("dropped", "rolled_back") else "active")
                for o in ctx.options(dim) if o != control}
        seg.update(control=control, arms=arms, status="exploring", winner=None, promoted_h=None, promo=None)
    store.set_kv(ctx.id, f"exp:{dim}", st)
    store.decide(ctx.id, "experiment", "rebased_on_supervisor",
                 f"{dim}: supervisor edit adopted as the new control; experiment paused for "
                 f"{settings.SUPERVISOR_PIN_HOURS}h, then resumes against it", {"dim": dim, "state": st})


def expected_change(store, codec, ctx: WorkflowCtx, dim: str, cur_cfg: dict, new_dists: dict, partition) -> dict:
    """Point estimate of the engagement change of moving to new_dists, from measured arm lifts (0 if unmeasured)."""
    st = store.kv(ctx.id, f"exp:{dim}")
    if not st:
        return {"rel_change": 0.0}
    data = ArmData(store, ctx.id, dim, st.get("start_h", 0))
    total = 0.0
    for key, nd in new_dists.items():
        seg = st["segs"].get(key) or st["segs"].get("*")
        codes = seg_codes(codec, partition, key)
        share = data.share(codes)
        cohort = "default"
        if partition and key != "*":
            for c in ctx.live_cohorts():
                if c.value(partition) == key:
                    cohort = c.id
                    break
        cd = dist_of(cur_cfg, cohort, dim) if cur_cfg else {}
        ctl = seg["control"]
        for a in set(nd) | set(cd):
            if a == ctl:
                continue
            d = data.diff(a, ctl, codes)
            lift = d.lift if d else 0.0
            total += share * (nd.get(a, 0) - cd.get(a, 0)) * lift
    return {"rel_change": round(total, 4)}
