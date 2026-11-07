"""Hallucination incident response (C8): detect → pick a fix → validate → propose with evidence → verify.

Detection (populations only): over the last HALLUC_WINDOW_H hours, and separately since the current prompt
went live, the number of QA hallucination flags on connected calls is compared with the workflow's reference
flag rate (pre-D0 history). We open an incident only if ≥ HALLUC_MIN_CONNECTED connected calls,
≥ HALLUC_MIN_FLAGS flags, a Poisson tail p < HALLUC_P and the rate is ≥ HALLUC_RATIO × reference. The QA
detector itself has false positives and negatives; the reference rate absorbs its FP floor.

Choosing a fix, cheapest evidence first ("the best compute is no compute"):
  1. A prompt this workflow has already run with a normal flag rate (usually the one the incident replaced).
     We know its engagement from real calls, so it cannot be a "nobody hallucinated, nobody stayed" fix.
     Cost: one validation run (3 credits), required by governance.
  2. Only if no known-good prompt exists: ask the LLM for candidates (3 credits) and validate them one at a
     time. The LLM proposes; the validation pass rate (and, after apply, real calls) decide.
The change is always a prompt change, so it always goes to a named human through /approvals — even when it
reverts a supervisor's own edit. That is not "undoing it behind their back": the request carries the evidence.

Verification: after the fix is live, ≥ HALLUC_MIN_CONNECTED connected calls on it must show a flag rate
≤ HALLUC_RESOLVED_RATIO × reference; engagement is checked too. Otherwise the prompt is marked bad and we
try the next candidate.
"""
from __future__ import annotations

import json
import logging

from . import settings
from .client import SimError
from .stats import poisson_sf, stratified_diff

log = logging.getLogger("controller.incidents")


def reference_rate(store, wf: str) -> float:
    r = store.kv(wf, "halluc_ref")
    if r is not None:
        return r
    conn, flags = store.q("SELECT COALESCE(SUM(conn),0), COALESCE(SUM(flags),0) FROM agg_kpi WHERE wf=? AND h<0",
                          (wf,))[0]
    if conn < 500:
        conn, flags = store.q("SELECT COALESCE(SUM(conn),0), COALESCE(SUM(flags),0) FROM agg_kpi WHERE h<0")[0]
    rate = max(flags / conn if conn else 0.015, 0.002)
    store.set_kv(wf, "halluc_ref", rate)
    return rate


def window_stats(store, wf: str, pv: str, h_from: int, h_to: int | None = None) -> tuple[int, int, int]:
    sql = "SELECT COALESCE(SUM(conn),0), COALESCE(SUM(flags),0), COALESCE(SUM(eng),0) FROM agg_kpi " \
          "WHERE wf=? AND pv=? AND h>=?"
    args = [wf, pv, h_from]
    if h_to is not None:
        sql += " AND h<?"
        args.append(h_to)
    return tuple(store.q(sql, args)[0])


def spike(conn: int, flags: int, ref: float) -> tuple[bool, float]:
    if conn < settings.HALLUC_MIN_CONNECTED or flags < settings.HALLUC_MIN_FLAGS:
        return False, 1.0
    p = poisson_sf(flags, conn * ref)
    return (p < settings.HALLUC_P and flags / conn >= settings.HALLUC_RATIO * ref), p


def prompt_since_minute(store, wf: str, pv: str) -> int | None:
    """When did the current prompt go live (last version whose prompt differs before it)?"""
    rows = store.q("SELECT version, applied_minute, config FROM config_versions WHERE wf=? ORDER BY version", (wf,))
    since = None
    prev = None
    for _, m, cfg in rows:
        p = json.loads(cfg)["prompt_version"]
        if p != prev:
            since = m
        prev = p
    return since if prev == pv else None


def known_good_prompts(store, wf: str, current: str, ref: float) -> list[tuple[str, dict]]:
    """Prompts this workflow has run on ≥300 connected calls without a flag spike, most recent first."""
    rows = store.q("SELECT pv, SUM(conn), SUM(flags), SUM(eng), MAX(h) FROM agg_kpi WHERE wf=? GROUP BY pv", (wf,))
    out = []
    for pv, conn, flags, eng, last_h in rows:
        if pv == current or conn < 300:
            continue
        bad, _ = spike(conn, flags, ref)
        if not bad and flags / conn <= settings.HALLUC_RESOLVED_RATIO * ref:
            out.append((last_h, pv, {"connected": conn, "flag_rate": round(flags / conn, 4),
                                     "engagement": round(eng / conn, 4)}))
    return [(pv, s) for _, pv, s in sorted(out, reverse=True)]


class IncidentResponder:
    def __init__(self, store, client, governance, ctxs):
        self.store, self.client, self.gov, self.ctxs = store, client, governance, ctxs

    def open_incident(self, wf: str) -> dict | None:
        r = self.store.q("SELECT id, status, detail, opened_minute FROM incidents WHERE wf=? AND kind='hallucination' "
                         "AND status NOT IN ('resolved','abandoned') ORDER BY opened_minute DESC LIMIT 1", (wf,))
        if not r:
            return None
        return {"id": r[0][0], "status": r[0][1], "detail": json.loads(r[0][2]), "opened_minute": r[0][3]}

    def _save(self, inc: dict, status: str | None = None, closed: int | None = None):
        if status:
            inc["status"] = status
        self.store.db.execute("UPDATE incidents SET status=?, detail=?, closed_minute=COALESCE(?, closed_minute) "
                              "WHERE id=?", (inc["status"], json.dumps(inc["detail"]), closed, inc["id"]))

    # ------------------------------------------------------------------ detection
    def detect(self, wf: str, now: int):
        ctx = self.ctxs[wf]
        if self.open_incident(wf):
            return
        pv = ctx.config["prompt_version"]
        ref = reference_rate(self.store, wf)
        h = now // 60
        windows = {"recent": h - settings.HALLUC_WINDOW_H}
        since = prompt_since_minute(self.store, wf, pv)
        if since is not None and since >= 0:
            windows["since_prompt_live"] = since // 60
        for name, h_from in windows.items():
            conn, flags, _ = window_stats(self.store, wf, pv, h_from)
            hit, p = spike(conn, flags, ref)
            if hit:
                iid = f"{wf}.halluc.m{now}"
                detail = {"prompt": pv, "window": name, "connected": conn, "flags": flags,
                          "flag_rate": round(flags / conn, 4), "reference_rate": round(ref, 4), "p_value": p,
                          "tried": [], "validations": [], "bad_prompts": [pv], "credits_spent": 0,
                          "proposal_id": None, "candidate": None}
                with self.store.tx():
                    self.store.db.execute("INSERT INTO incidents(id,wf,kind,status,opened_minute,detail) "
                                          "VALUES(?,?,?,?,?,?)", (iid, wf, "hallucination", "open", now,
                                                                  json.dumps(detail)))
                    self.store.decide(wf, "incident", "hallucination_detected",
                                      f"prompt {pv}: {flags}/{conn} connected calls flagged ({flags / conn:.1%}) vs "
                                      f"reference {ref:.1%} (p={p:.1e}, window={name})", detail)
                    self.store.alert("ticket", wf, "hallucination_incident",
                                     f"{wf}: hallucination flags {flags / conn:.1%} vs {ref:.1%} on prompt {pv}; "
                                     f"controller is preparing a validated fix for approval")
                return

    # ------------------------------------------------------------------ response
    def step(self, wf: str, now: int):
        inc = self.open_incident(wf)
        if not inc:
            return
        ctx = self.ctxs[wf]
        d = inc["detail"]
        ref = reference_rate(self.store, wf)
        cur = ctx.config["prompt_version"]
        st = inc["status"]
        if st in ("open", "proposing", "awaiting") and cur not in d["bad_prompts"]:
            # someone (a supervisor, or our applied fix) moved off the bad prompt: verify instead
            with self.store.tx():
                d["fix_live_minute"] = now
                d["candidate"] = d.get("candidate") or cur
                self._save(inc, "verifying")
                self.store.decide(wf, "incident", "fix_live", f"prompt is now {cur}; verifying on live calls",
                                  {"incident": inc["id"]})
            return
        if st == "open":
            self._choose_fix(inc, ctx, now, ref)
        elif st == "proposing":
            self._propose(inc, ctx, now)
        elif st == "awaiting":
            p = self.gov.get(d["proposal_id"])
            if p["status"] in ("rejected", "expired", "lapsed", "stale", "failed"):
                with self.store.tx():
                    self._save(inc, "proposing")
                    self.store.decide(wf, "incident", "fix_reproposal",
                                      f"fix proposal {p['status']}; re-proposing with fresh data",
                                      {"incident": inc["id"], "proposal": p["proposal_id"]})
        elif st == "verifying":
            self._verify(inc, ctx, now, ref)

    def _credits(self) -> float:
        return float(self.client.state()["credits_remaining"])

    def _can_spend(self, inc: dict, cost: float, essential: bool = False) -> bool:
        total = self.store.meta("credits_total") or 0
        left = self._credits()
        if left < cost:
            return False
        if inc["detail"]["credits_spent"] + cost > settings.INCIDENT_CREDIT_CAP:
            return False
        if not essential and left - cost < settings.CREDIT_FLOOR_SHARE * total:
            return False
        return True

    def _validate(self, inc: dict, ctx, pv: str, essential: bool) -> dict | None:
        n = settings.VALIDATION_N
        cost = -(-n // 100)
        if not self._can_spend(inc, cost, essential):
            return None
        try:
            r = self.client.validate(ctx.id, pv, n)
        except SimError as e:
            self.store.decide(ctx.id, "incident", "validation_failed", f"validation of {pv} refused: {e.code}",
                              {"incident": inc["id"]})
            return None
        inc["detail"]["credits_spent"] += cost
        inc["detail"]["validations"].append(r)
        need = float(ctx.gov.get("min_validation_pass_rate", 0.97))
        self.store.decide(ctx.id, "incident", "validation_run",
                          f"{pv}: pass rate {r['pass_rate']:.3f} on n={n} (approver bar {need:.2f})",
                          {"incident": inc["id"], "run": r})
        return r

    def _choose_fix(self, inc: dict, ctx, now: int, ref: float):
        d = inc["detail"]
        need = float(ctx.gov.get("min_validation_pass_rate", 0.97)) + settings.VALIDATION_MARGIN
        tried = set(d["tried"]) | set(d["bad_prompts"])
        cands = [(pv, s, "known_good") for pv, s in known_good_prompts(self.store, ctx.id, ctx.config["prompt_version"], ref)
                 if pv not in tried]
        with self.store.tx():
            if not cands and not d.get("llm_asked"):
                if self._can_spend(inc, 3):
                    try:
                        r = self.client.suggest_fixes(ctx.id, f"hallucination flag rate {d['flag_rate']:.1%} vs "
                                                              f"{d['reference_rate']:.1%} on prompt {d['prompt']}")
                        d["credits_spent"] += 3
                        d["llm_asked"] = True
                        d["llm_suggestions"] = r["suggestions"]
                        self.store.decide(ctx.id, "incident", "llm_suggested",
                                          "LLM proposed: " + ", ".join(f"{s['prompt_version']} ({s['summary']})"
                                                                       for s in r["suggestions"]),
                                          {"incident": inc["id"], "suggestions": r["suggestions"]})
                    except SimError as e:
                        self.store.decide(ctx.id, "incident", "llm_failed", f"suggest-fixes refused: {e.code}",
                                          {"incident": inc["id"]})
            if not cands:
                cands = [(s["prompt_version"], {"summary": s["summary"]}, "llm")
                         for s in d.get("llm_suggestions", []) if s["prompt_version"] not in tried]
            for pv, info, src in cands:
                d["tried"].append(pv)
                r = self._validate(inc, ctx, pv, essential=(src == "known_good"))
                if r is None:
                    break
                if r["pass_rate"] >= need:
                    d["candidate"] = pv
                    d["candidate_info"] = {"source": src, **info}
                    d["validation_run_id"] = r["validation_run_id"]
                    self._save(inc, "proposing")
                    self.store.decide(ctx.id, "incident", "fix_chosen",
                                      f"fix: {d['prompt']} → {pv} ({src}; validation {r['pass_rate']:.3f})",
                                      {"incident": inc["id"], "candidate": pv, "info": info, "run": r})
                    break
                d["bad_prompts"].append(pv)
            else:
                if not d.get("exhausted_logged"):
                    d["exhausted_logged"] = True
                    self.store.decide(ctx.id, "incident", "no_fix", "no candidate passed validation within the "
                                      "credit cap; escalating to a human", {"incident": inc["id"]})
                    self.store.alert("page", ctx.id, "hallucination_no_fix",
                                     f"{ctx.id}: hallucination incident {inc['id']} has no validated fix; "
                                     f"credits/candidates exhausted — needs a human prompt decision")
            self._save(inc)
        if inc["status"] == "proposing":
            self._propose(inc, ctx, now)

    def _propose(self, inc: dict, ctx, now: int):
        d = inc["detail"]
        ok, why = self.gov.can_submit(ctx.id, now, "incident")
        if not ok:
            if d.get("last_wait_reason") != why:
                with self.store.tx():
                    d["last_wait_reason"] = why
                    self._save(inc)
                    self.store.decide(ctx.id, "incident", "fix_waiting", f"fix ready, not sent: {why}",
                                      {"incident": inc["id"]})
            return
        cfg = json.loads(json.dumps(ctx.config))
        cfg["prompt_version"] = d["candidate"]
        cand = d["candidate"]
        kg = d.get("candidate_info", {})
        affected = self.store.q1("SELECT COUNT(*) FROM leads WHERE wf=? AND done=0 AND day>=?",
                                 (ctx.id, now // 1440 - ctx.cadence_days + 1)) or 0
        val = next(v for v in d["validations"] if v["validation_run_id"] == d["validation_run_id"])
        request = {
            "change_summary": f"Safety fix: switch prompt {d['prompt']} → {cand} (hallucination flags "
                              f"{d['flag_rate']:.1%} vs normal {d['reference_rate']:.1%})",
            "diff": {"prompt_version": {"from": ctx.config["prompt_version"], "to": cand}},
            "affected_leads": int(affected),
            "dry_run": {"method": "validation run + this workflow's own call history for the target prompt",
                        "validation": {k: val[k] for k in ("validation_run_id", "n", "pass_rate", "failures")},
                        "target_prompt_history": kg if kg.get("source") == "known_good" else "never run here",
                        "incident_window": {k: d[k] for k in ("connected", "flags", "flag_rate", "reference_rate")}},
            "impact_score": 0.95,
            "rationale": f"{d['flags']} of {d['connected']} connected calls on {d['prompt']} were flagged for "
                         f"hallucination (p={d['p_value']:.1e} vs this workflow's reference rate). {cand} "
                         f"passed validation at {val['pass_rate']:.3f} (bar "
                         f"{ctx.gov.get('min_validation_pass_rate', 0.97)}). "
                         + (f"It ran here on {kg['connected']} connected calls with flag rate {kg['flag_rate']:.1%} "
                            f"and engagement {kg['engagement']:.1%}." if kg.get("source") == "known_good" else
                            "It has not run on this workflow; engagement will be verified after apply."),
            "evidence": {"validation_run_id": d["validation_run_id"], "incident_id": inc["id"]},
        }
        p = self.gov.submit(ctx.id, now, "incident", cfg, request,
                            [{"component": "incident", "incident": inc["id"], "dim": "prompt_version"}])
        with self.store.tx():
            if p:
                d["proposal_id"] = p["proposal_id"]
                self._save(inc, "awaiting")
            else:
                self.store.decide(ctx.id, "incident", "fix_submit_failed", "submission failed; will retry",
                                  {"incident": inc["id"]})

    def _verify(self, inc: dict, ctx, now: int, ref: float):
        d = inc["detail"]
        pv = ctx.config["prompt_version"]
        conn, flags, eng = window_stats(self.store, ctx.id, pv, d["fix_live_minute"] // 60 + 1)
        if conn < settings.HALLUC_MIN_CONNECTED:
            return
        rate = flags / conn
        bad, p = spike(conn, flags, ref)
        with self.store.tx():
            if bad or rate > settings.HALLUC_RESOLVED_RATIO * ref:
                d["bad_prompts"].append(pv)
                self._save(inc, "open")
                self.store.decide(ctx.id, "incident", "fix_failed_verification",
                                  f"{pv} still flags {rate:.1%} on {conn} calls; trying the next candidate",
                                  {"incident": inc["id"], "connected": conn, "flags": flags})
                return
            # engagement check against the last two days before the incident on the old prompt
            opened_h = inc["opened_minute"] // 60
            pre = self.store.q("SELECT COALESCE(SUM(conn),0), COALESCE(SUM(eng),0) FROM agg_kpi WHERE wf=? AND "
                               "h>=? AND h<? AND pv<>?", (ctx.id, opened_h - 48, opened_h, d["prompt"]))[0]
            note = {}
            if pre[0] >= 200:
                dd = stratified_diff([(eng, conn, pre[1], pre[0])])
                note = {"engagement_vs_pre_incident": dd.as_dict()}
                if dd.lift < -0.05 and dd.z < -2.33:
                    self.store.alert("ticket", ctx.id, "fix_engagement_regression",
                                     f"{ctx.id}: hallucination fixed with {pv} but engagement is {dd.lift:+.1%} vs "
                                     f"before the incident — review the prompt with the client")
            d["verified"] = {"connected": conn, "flags": flags, "flag_rate": round(rate, 4), **note}
            self._save(inc, "resolved", closed=now)
            self.store.decide(ctx.id, "incident", "incident_resolved",
                              f"{pv}: flag rate {rate:.1%} on {conn} calls (reference {ref:.1%}); incident closed",
                              {"incident": inc["id"], **d["verified"]})
