"""Desired-state reconciliation: components say what they want, the planner turns it into config changes.

Each component owns one slice of the config and writes its desired state to `kv`:
    schedule  → schedule.hour_weights (by the schedule's split feature)
    caller_id → allocations.default.caller_id
    exp:<dim> → allocations.<cohort>.<dim> (by the experiment's partition feature, if any)
The planner composes them into ONE desired config on top of the live config, so:
  * whatever no component owns (lead_preferred_hour, prompt, a supervisor's own cohorts) is carried over
    unchanged — we never silently overwrite a supervisor edit;
  * a dim a supervisor edited is *pinned* for SUPERVISOR_PIN_HOURS: it is copied from the live config;
  * the cohort list is the shared CohortPlan (schedule feature × experiment partition features).

Then it splits the delta by channel. Dims the policy lets us auto-apply go out via PUT, stepped so no cohort's
distribution moves more than max_shift per step, spaced by AUTO_STEP_COOLDOWN_MIN. Everything else goes into
ONE batched approval request per workflow, timed for the approver's day (governance decides when).
"""
from __future__ import annotations

import copy
import json
import logging

from . import callerid, settings
from .cohorts import Cohort, CohortPlan
from .context import (AGENT_DIMS, EXPERIMENT_DIMS, WorkflowCtx, changed_dims, cohort_for_features, dist_close,
                      dist_of, hour_table, max_shift, norm)
from .experiments import desired as exp_desired
from .experiments import expected_change

log = logging.getLogger("controller.planner")


def wf_values(store, codec, wf: str) -> dict[str, list[str]]:
    codes = [r[0] for r in store.q("SELECT DISTINCT fcode FROM agg_pick WHERE wf=? AND fcode>=0", (wf,))]
    out: dict[str, set] = {}
    for c in codes:
        for k, v in codec.by_code[c].items():
            out.setdefault(k, set()).add(v)
    return {k: sorted(v) for k, v in out.items()}


class Planner:
    def __init__(self, store, client, gov, ctxs, codec):
        self.store, self.client, self.gov, self.ctxs, self.codec = store, client, gov, ctxs, codec

    # ------------------------------------------------------------------ composition
    def pins(self, wf: str, now: int) -> set[str]:
        return {d for d, until in (self.store.kv(wf, "pins") or {}).items() if until > now}

    def plan(self, ctx: WorkflowCtx) -> CohortPlan:
        sched = self.store.kv(ctx.id, "schedule") or {}
        feats = []
        if sched.get("feature"):
            feats.append(sched["feature"])
        for dim in EXPERIMENT_DIMS:
            st = self.store.kv(ctx.id, f"exp:{dim}")
            if st and st.get("partition") and st["partition"] not in feats:
                feats.append(st["partition"])
        vals = wf_values(self.store, self.codec, ctx.id)
        plan = CohortPlan()
        for f in feats:
            nxt = plan.with_feature(f, vals.get(f, []))
            if nxt is not None:
                plan = nxt
        return plan

    def build(self, ctx: WorkflowCtx, now: int, cohorts_override: list[dict] | None = None,
              ignore_pins: bool = False) -> dict:
        cur = ctx.config
        pins = set() if ignore_pins else self.pins(ctx.id, now)
        if cohorts_override is not None:
            cohorts = copy.deepcopy(cohorts_override)
        elif pins:
            cohorts = copy.deepcopy(cur.get("cohorts", []))
        else:
            plan = self.plan(ctx)
            self.store.set_kv(ctx.id, "plan", plan.to_json())
            cohorts = plan.config_cohorts()
        cobjs = [Cohort(c["id"], tuple(sorted((f, tuple(sorted(v))) for f, v in c["where"].items()))) for c in cohorts]
        cfg = {"cohorts": cohorts, "allocations": {"default": {}}, "prompt_version": cur["prompt_version"],
               "schedule": {"hour_weights": {},
                            "lead_preferred_hour": copy.deepcopy(cur.get("schedule", {}).get("lead_preferred_hour", {}))}}
        # agent dims
        for dim in EXPERIMENT_DIMS:
            st = self.store.kv(ctx.id, f"exp:{dim}")
            if not st or dim in pins:
                cfg["allocations"]["default"][dim] = dist_of(cur, "default", dim)
                for c in cobjs:
                    explicit = (cur.get("allocations", {}).get(c.id) or {}).get(dim)
                    if explicit:
                        cfg["allocations"].setdefault(c.id, {})[dim] = explicit
                continue
            des = exp_desired(st)
            part, dists = des["partition"], des["dists"]
            if part is None:
                default = dists["*"]
            else:
                ctl = {}
                for s in st["segs"].values():
                    ctl[s["control"]] = ctl.get(s["control"], 0) + 1
                default = {max(ctl, key=lambda o: (ctl[o], o)): 1.0}
            cfg["allocations"]["default"][dim] = default
            if part is not None:
                for c in cobjs:
                    v = c.value(part)
                    if v in dists and not dist_close(dists[v], default):
                        cfg["allocations"].setdefault(c.id, {})[dim] = dists[v]
        did = self.store.kv(ctx.id, "did_desired")
        if did and "caller_id" not in pins:
            cfg["allocations"]["default"]["caller_id"] = did["dist"]
        else:
            cfg["allocations"]["default"]["caller_id"] = dist_of(cur, "default", "caller_id")
            for c in cobjs:
                explicit = (cur.get("allocations", {}).get(c.id) or {}).get("caller_id")
                if explicit:
                    cfg["allocations"].setdefault(c.id, {})["caller_id"] = explicit
        # schedule
        sched = self.store.kv(ctx.id, "schedule") or {}
        if (self.store.kv(ctx.id, "schedule_suspended_until") or -10**9) > now:
            sched = {}  # guardrail breach: our call-time weights are backed out
        if "schedule" in pins or not sched.get("feature"):
            live_ids = {c.id for c in cobjs} | {"default"}
            cfg["schedule"]["hour_weights"] = {k: v for k, v in cur.get("schedule", {}).get("hour_weights", {}).items()
                                               if k in live_ids}
        else:
            f, W = sched["feature"], sched["weights"]
            hw = {}
            for c in cobjs:
                v = c.value(f)
                if v in W:
                    hw[c.id] = dict(W[v])
            if hw:
                hw["default"] = {str(h): 1.0 for h in ctx.window_hours()}
            cfg["schedule"]["hour_weights"] = hw
        return cfg

    # ------------------------------------------------------------------ act
    def act(self, ctx: WorkflowCtx, now: int, urgent: bool = False):
        wf = ctx.id
        cur = ctx.config
        target = self.build(ctx, now)
        dims = changed_dims(cur, target)
        if not dims:
            return
        auto_ok = ctx.auto_dims() & {"schedule", "caller_id", "welcome_message", "voice"}
        approval_dims = [d for d in dims if d not in auto_ok]
        open_apr = self.gov.open_approval(wf)
        if approval_dims and not open_apr:
            kind = "incident" if urgent else "routine"
            ok, why = self.gov.can_submit(wf, now, kind)
            if ok:
                self._submit(ctx, now, kind, target, dims)
                return
            self._note_wait(wf, f"approval needed for {approval_dims}: {why}")
        # auto part: same cohorts as live, only auto-permitted dims moved
        if open_apr and not urgent:
            return
        auto_target = self._auto_target(ctx, now, auto_ok)
        adims = changed_dims(cur, auto_target)
        if not adims:
            return
        last = self.store.kv(wf, "last_auto_minute", -10**9)
        if not urgent and now - last < settings.AUTO_STEP_COOLDOWN_MIN:
            return
        step = self._step(cur, auto_target, ctx.max_shift())
        sdims = changed_dims(cur, step)
        if not sdims:
            return
        items = self._items(ctx, cur, step, sdims, now)
        rationale = ". ".join(i["summary"] for i in items) or "policy-permitted operational change"
        evidence = {"items": [i.get("evidence") for i in items if i.get("evidence")]}
        with self.store.tx():
            self.store.set_kv(wf, "last_auto_minute", now)
        self.gov.auto_apply(wf, now, "incident" if urgent else "routine", step, rationale[:2000], evidence, items)

    def _auto_target(self, ctx, now, auto_ok: set[str]) -> dict:
        cur = ctx.config
        full = self.build(ctx, now, cohorts_override=cur.get("cohorts", []))
        t = copy.deepcopy(cur)
        keys = set(cur.get("allocations", {})) | set(full.get("allocations", {}))
        for dim in AGENT_DIMS:
            if dim not in auto_ok:
                continue
            for k in keys:
                fd = (full["allocations"].get(k) or {}).get(dim)
                if k == "default":
                    t["allocations"]["default"][dim] = fd
                elif fd is not None:
                    t["allocations"].setdefault(k, {})[dim] = fd
                elif (t["allocations"].get(k) or {}).get(dim):
                    t["allocations"][k].pop(dim)
        if "schedule" in auto_ok:
            t["schedule"] = full["schedule"]
        return t

    @staticmethod
    def _step(cur: dict, target: dict, max_sh: float) -> dict:
        ms = max_shift(cur, target)
        if ms <= max_sh + 1e-9 or ms == 0:
            return target
        t = (max_sh * 0.995) / ms
        step = copy.deepcopy(target)
        keys = {"default"} | set(cur.get("allocations", {})) | set(target.get("allocations", {}))
        for k in keys:
            for dim in AGENT_DIMS:
                a, b = norm(dist_of(cur, k, dim)), norm(dist_of(target, k, dim))
                if a == b:
                    continue
                mix = {o: a.get(o, 0) + t * (b.get(o, 0) - a.get(o, 0)) for o in set(a) | set(b)}
                step["allocations"].setdefault(k, {})[dim] = {o: round(w, 6) for o, w in mix.items() if w > 1e-6}
        return step

    # ------------------------------------------------------------------ request building
    def _note_wait(self, wf: str, why: str):
        if self.store.kv(wf, "last_wait") != why:
            with self.store.tx():
                self.store.set_kv(wf, "last_wait", why)
                self.store.decide(wf, "planner", "waiting", why)

    def _items(self, ctx, cur: dict, new: dict, dims: list[str], now: int) -> list[dict]:
        items = []
        wf = ctx.id
        if "cohorts" in dims:
            plan = self.store.kv(wf, "plan") or {}
            items.append({"dim": "cohorts", "summary": f"cohorts by {' × '.join(plan.get('split_features', [])) or 'none'} "
                                                       f"({len(new['cohorts'])} cohorts)",
                          "expected": {}})
        if "schedule" in dims:
            s = self.store.kv(wf, "schedule") or {}
            dr = s.get("dry_run") or {}
            items.append({"dim": "schedule", "summary": f"call-time weights by {s.get('feature')}: expected pickup "
                                                        f"{dr.get('expected_rel_change', 0):+.1%} (dry run)",
                          "expected": {"pickup_rate": dr.get("expected_rel_change", 0.0)}, "dry_run": dr,
                          "evidence": {"schedule_tests": s.get("tests")}})
        if "caller_id" in dims:
            st = self.store.kv(wf, "did") or {}
            a, b = dist_of(cur, "default", "caller_id"), dist_of(new, "default", "caller_id")
            ev = {d: e for d, e in (st.get("evidence") or {}).items() if st.get("status", {}).get(d) != "unknown"}
            ch = callerid.expected_pickup_change(a, b, ev)
            items.append({"dim": "caller_id", "summary": "caller IDs " + _fmt_dist(a) + " → " + _fmt_dist(b) +
                          f" (status {st.get('status')}; expected pickup {ch:+.1%})",
                          "expected": {"pickup_rate": ch}, "evidence": {"did_status": st.get("status"),
                                                                        "did_evidence": st.get("evidence")}})
        for dim in EXPERIMENT_DIMS:
            if dim not in dims:
                continue
            st = self.store.kv(wf, f"exp:{dim}")
            des = exp_desired(st) if st else {"partition": None, "dists": {"*": dist_of(new, "default", dim)}}
            ex = expected_change(self.store, self.codec, ctx, dim, cur, des["dists"], des["partition"]) if st else \
                {"rel_change": 0.0}
            last = self.store.q("SELECT summary FROM decisions WHERE wf=? AND component='experiment' AND "
                                "detail LIKE ? ORDER BY id DESC LIMIT 3", (wf, f'%"dim": "{dim}"%'))
            why = "; ".join(r[0] for r in last) or "start exploring untested options at a small share"
            segs = ", ".join(f"{k}: {_fmt_dist(v)}" for k, v in des["dists"].items())
            items.append({"dim": dim, "summary": f"{dim} → {segs}. Why: {why}",
                          "expected": {"engagement_rate": ex["rel_change"]},
                          "evidence": {"experiment": st}})
        return items

    def _affected(self, ctx, cur: dict, new: dict, dims: list[str], now: int) -> int:
        rows = self.store.q("SELECT fcode, COUNT(*) FROM leads WHERE wf=? AND done=0 AND day>=? GROUP BY fcode",
                            (ctx.id, now // 1440 - ctx.cadence_days + 1))
        n = 0
        for fc, cnt in rows:
            f = self.codec.by_code.get(fc, {})
            a, b = cohort_for_features(cur, f), cohort_for_features(new, f)
            diff = cur.get("prompt_version") != new.get("prompt_version")
            for dim in AGENT_DIMS:
                if not dist_close(dist_of(cur, a, dim), dist_of(new, b, dim)):
                    diff = True
            ta, tb = hour_table(cur, a), hour_table(new, b)
            if (ta or tb) and json.dumps(ta, sort_keys=True) != json.dumps(tb, sort_keys=True):
                diff = True
            n += cnt if diff else 0
        return int(n)

    def _submit(self, ctx, now: int, kind: str, target: dict, dims: list[str]):
        cur = ctx.config
        items = self._items(ctx, cur, target, dims, now)
        prim = ctx.primary
        exp_primary = sum(i["expected"].get(prim, 0.0) for i in items)
        other = "pickup_rate" if prim == "engagement_rate" else "engagement_rate"
        exp_other = sum(i["expected"].get(other, 0.0) for i in items)
        impact = min(1.0, max(0.05, 10 * max(exp_primary, 0) + 2 * max(exp_other, 0)))
        if kind == "incident":
            impact = max(impact, 0.8)
        if kind == "routine" and exp_primary < -0.005:
            self._note_wait(ctx.id, f"not proposing {dims}: our own estimate of {prim} is {exp_primary:+.1%}")
            return
        affected = self._affected(ctx, cur, target, dims, now)
        pop = self.store.q("SELECT fcode, SUM(conn) FROM agg_eng WHERE wf=? AND h>=? GROUP BY fcode",
                           (ctx.id, now // 60 - 7 * 24))
        cohort_mix: dict[str, int] = {}
        for fc, c in pop:
            k = cohort_for_features(target, self.codec.by_code.get(fc, {}))
            cohort_mix[k] = cohort_mix.get(k, 0) + c
        tot = sum(cohort_mix.values()) or 1
        dry = {"method": "replay of the last 7 days of connected calls through the proposed cohorts, priced with "
                         "this workflow's measured arm lifts / pickup tables (point estimates; unmeasured options = 0)",
               "cohort_share_last_7d": {k: round(v / tot, 3) for k, v in sorted(cohort_mix.items())},
               "expected_rel_change": {prim: round(exp_primary, 4), other: round(exp_other, 4)},
               "items": {i["dim"]: i.get("dry_run") or i["expected"] for i in items}}
        summary = f"{len(items)} change(s): " + "; ".join(i["dim"] for i in items) + \
                  f" — expected {prim.replace('_rate', '')} {exp_primary:+.1%}"
        if self.pins(ctx.id, now):
            summary += f" (supervisor-edited dims left untouched: {sorted(self.pins(ctx.id, now))})"
        request = {"change_summary": summary,
                   "diff": {d: {"from": _summ(cur, d), "to": _summ(target, d)} for d in dims},
                   "affected_leads": affected, "dry_run": dry, "impact_score": round(impact, 3),
                   "rationale": " | ".join(i["summary"] for i in items)[:4000],
                   "evidence": {"items": [i.get("evidence") for i in items if i.get("evidence")]}}
        self.gov.submit(ctx.id, now, kind, target, request, [{k: v for k, v in i.items() if k != "evidence"}
                                                              for i in items])


def _fmt_dist(d: dict) -> str:
    return "{" + ", ".join(f"{k}:{v:.0%}" for k, v in sorted(norm(d).items())) + "}"


def _summ(cfg: dict, dim: str):
    if dim == "cohorts":
        return [c["id"] for c in cfg.get("cohorts", [])]
    if dim == "schedule":
        return {"hour_weight_tables": sorted(cfg.get("schedule", {}).get("hour_weights", {}))}
    if dim == "prompt_version":
        return cfg.get("prompt_version")
    out = {"default": _fmt_dist(dist_of(cfg, "default", dim))}
    for k, v in cfg.get("allocations", {}).items():
        if k != "default" and dim in v:
            out[k] = _fmt_dist(v[dim])
    return out
