"""Governance client (C9): the only code that changes a workflow's config.

State machine of a proposal (table `proposals`, column `status`):

    approval channel
      submitting ──POST ok──▶ pending ──▶ approved ──▶ applying ──POST ok──▶ applied
          │                      │            │            ├─409 stale──▶ stale   (re-proposed on fresh base)
          │                      │            │            └─410───────▶ lapsed  (re-proposed)
          │                      │            └─(apply window passes)──▶ lapsed
          │                      ├──▶ rejected   (back-off, then re-proposed with fresh data)
          │                      └──▶ expired    (treated as a rejection — Product memo)
          └─4xx──▶ failed

    auto channel
      auto_applying ──PUT ok──▶ applied
            └─403/409/422──▶ failed (reasons recorded)

Crash safety: every transition that precedes an external side effect is COMMITTED first (write-ahead
intent: `submitting`, `applying`, `auto_applying`). On restart `recover()` resolves each intent against the
simulator's own record (approvals list by proposal_id, config history by proposal_id) before doing anything
else, so nothing is sent twice and no approval is lost. proposal_ids are deterministic per (workflow, seq).
"""
from __future__ import annotations

import json
import logging

from . import settings
from .client import SimError
from .simtime import fmt

log = logging.getLogger("controller.governance")

OPEN_APPROVAL = ("submitting", "pending", "approved", "applying")


class Governance:
    def __init__(self, store, client, ctxs: dict):
        self.store, self.client, self.ctxs = store, client, ctxs

    # ------------------------------------------------------------------ queries
    def open_approval(self, wf: str) -> dict | None:
        r = self.store.q("SELECT proposal_id FROM proposals WHERE wf=? AND channel='approval' AND status IN "
                         "(?,?,?,?) ORDER BY created_minute", (wf, *OPEN_APPROVAL))
        return self.get(r[0][0]) if r else None

    def open_approvals(self, wf: str) -> list[dict]:
        r = self.store.q("SELECT proposal_id FROM proposals WHERE wf=? AND channel='approval' AND status IN "
                         "(?,?,?,?) ORDER BY created_minute", (wf, *OPEN_APPROVAL))
        return [self.get(x[0]) for x in r]

    def get(self, pid: str) -> dict:
        cols = [d[1] for d in self.store.q("PRAGMA table_info(proposals)")]
        row = self.store.q("SELECT * FROM proposals WHERE proposal_id=?", (pid,))[0]
        p = dict(zip(cols, row))
        for k in ("config", "request", "items", "reasons"):
            p[k] = json.loads(p[k]) if p[k] else None
        return p

    def requests_last_24h(self, wf: str, now: int) -> int:
        return self.store.q1("SELECT COUNT(*) FROM proposals WHERE wf=? AND channel='approval' AND "
                             "approval_id IS NOT NULL AND submitted_minute > ?", (wf, now - 1440)) or 0

    def last_closed(self, wf: str, kind: str | None = None) -> dict | None:
        sql = "SELECT proposal_id FROM proposals WHERE wf=? AND channel='approval' AND status NOT IN (?,?,?,?)"
        args = [wf, *OPEN_APPROVAL]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        r = self.store.q(sql + " ORDER BY updated_minute DESC LIMIT 1", args)
        return self.get(r[0][0]) if r else None

    def routine_capacity(self, wf: str, now: int) -> int:
        ctx = self.ctxs[wf]
        cap = int(ctx.gov["max_requests_per_24h"]) - settings.INCIDENT_RESERVED_REQUESTS
        pref = ctx.preferences().get("max_routine_requests_per_day")
        if pref is not None:
            cap = min(cap, int(pref))
        return max(cap, 1) - self.requests_last_24h(wf, now)

    def incident_capacity(self, wf: str, now: int) -> int:
        return int(self.ctxs[wf].gov["max_requests_per_24h"]) - self.requests_last_24h(wf, now)

    def can_submit(self, wf: str, now: int, kind: str) -> tuple[bool, str]:
        ctx = self.ctxs[wf]
        if not ctx.approver_open(now):
            return False, "outside approver hours (we do not queue requests overnight)"
        if not ctx.decision_before_expiry(now):
            return False, "worst-case approver latency would outlive the proposal TTL"
        if kind == "routine":
            if not ctx.same_day_decision(now):
                return False, "too late in the approver day for a same-day decision"
            if self.routine_capacity(wf, now) <= 0:
                return False, "routine request budget for 24h used (one slot reserved for incidents)"
            last = self.last_closed(wf, "routine")
            if last and last["status"] in ("rejected", "expired") and \
                    now - (last["decided_minute"] or last["updated_minute"]) < settings.REJECT_BACKOFF_MIN:
                return False, "backing off after a rejection"
        elif self.incident_capacity(wf, now) <= 0:
            return False, "approver request limit reached for 24h"
        return True, ""

    # ------------------------------------------------------------------ id
    def _next_pid(self, wf: str, now: int, tag: str) -> str:
        seq = (self.store.kv(wf, "proposal_seq", 0) or 0) + 1
        self.store.set_kv(wf, "proposal_seq", seq)
        return f"{wf}.{tag}.{seq:04d}.m{now}"

    # ------------------------------------------------------------------ submit (approval channel)
    def submit(self, wf: str, now: int, kind: str, config: dict, request: dict, items: list) -> dict | None:
        ctx = self.ctxs[wf]
        with self.store.tx():
            pid = self._next_pid(wf, now, "apr")
            self.store.db.execute(
                "INSERT INTO proposals(proposal_id,wf,kind,channel,status,base_version,config,request,items,"
                "created_minute,updated_minute) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pid, wf, kind, "approval", "submitting", ctx.version, json.dumps(config), json.dumps(request),
                 json.dumps(items), now, now))
            self.store.decide(wf, "governance", "proposal_created", request["change_summary"],
                              {"kind": kind, "base_version": ctx.version, "items": items,
                               "impact_score": request["impact_score"], "affected_leads": request["affected_leads"]},
                              proposal_id=pid)
        return self._send(pid)

    def _send(self, pid: str) -> dict | None:
        p = self.get(pid)
        body = {"proposal_id": pid, "workflow_id": p["wf"], "base_version": p["base_version"],
                "proposed_config": p["config"], **p["request"]}
        try:
            r = self.client.request_approval(body)
        except SimError as e:
            if e.code == "duplicate_proposal_id":
                return self._resolve_submitting(p)
            with self.store.tx():
                self._set(pid, status="failed", last_error=f"{e.status} {e.code}: {e.detail}")
                self.store.decide(p["wf"], "governance", "proposal_failed",
                                  f"submit refused: {e.code}", {"detail": e.detail}, proposal_id=pid)
            return None
        with self.store.tx():
            self._set(pid, status="pending", approval_id=r["approval_id"], submitted_minute=self.store.now)
            self.store.decide(p["wf"], "governance", "proposal_submitted",
                              f"{r['approval_id']} pending, expires {r['expires_at']}",
                              {"approval": r}, proposal_id=pid)
        return self.get(pid)

    def _resolve_submitting(self, p: dict) -> dict | None:
        found = [a for a in self.client.approvals(workflow_id=p["wf"]) if a["proposal_id"] == p["proposal_id"]]
        if not found:
            return None
        a = found[0]
        with self.store.tx():
            sub = self.store.now if p["submitted_minute"] is None else p["submitted_minute"]
            self._set(p["proposal_id"], status="pending", approval_id=a["approval_id"], submitted_minute=sub)
            self.store.decide(p["wf"], "governance", "recovered", f"submit reconciled → {a['approval_id']}",
                              {"approval": a}, proposal_id=p["proposal_id"])
        return self._on_approval(self.get(p["proposal_id"]), a)

    # ------------------------------------------------------------------ poll / apply
    def poll(self, now: int):
        rows = self.store.q("SELECT proposal_id FROM proposals WHERE channel='approval' AND status IN "
                            "('pending','approved','applying')")
        for (pid,) in rows:
            p = self.get(pid)
            try:
                a = self.client.approval(p["approval_id"])
            except SimError as e:
                log.warning("poll %s: %s", pid, e)
                continue
            self._on_approval(p, a)

    def _on_approval(self, p: dict, a: dict) -> dict:
        pid, wf = p["proposal_id"], p["wf"]
        st = a["status"]
        decided = a.get("decided_at")
        from .simtime import ts_minute
        dmin = ts_minute(decided) if decided else None
        if st == "pending":
            return p
        if st in ("rejected", "expired"):
            with self.store.tx():
                self._set(pid, status=st, decided_minute=dmin if dmin is not None else self.store.now,
                          reasons=json.dumps(a.get("reasons") or []))
                self.store.decide(wf, "governance", f"proposal_{st}",
                                  f"{a['approval_id']} {st}: {'; '.join(a.get('reasons') or []) or 'no decision in TTL'}",
                                  {"approval": a}, proposal_id=pid)
            return self.get(pid)
        if st == "lapsed":
            with self.store.tx():
                self._set(pid, status="lapsed", reasons=json.dumps(a.get("reasons") or []))
                self.store.decide(wf, "governance", "proposal_lapsed", f"{a['approval_id']} lapsed", {"approval": a},
                                  proposal_id=pid)
            return self.get(pid)
        if st == "applied":
            ver = next((v["version"] for v in self.client.config_history(wf) if v.get("approval_id") == a["approval_id"]),
                       None)
            with self.store.tx():
                self._set(pid, status="applied", applied_version=ver, decided_minute=dmin)
                self.store.decide(wf, "governance", "applied", f"{a['approval_id']} applied as v{ver} (reconciled)",
                                  {"approval": a}, proposal_id=pid)
            return self.get(pid)
        if st == "approved":
            if p["status"] != "applying":
                with self.store.tx():
                    self._set(pid, status="approved", decided_minute=dmin)
                    self.store.decide(wf, "governance", "proposal_approved",
                                      f"{a['approval_id']} approved at {fmt(dmin)}; apply by {a.get('apply_by')}",
                                      {"approval": a}, proposal_id=pid)
            return self._apply(self.get(pid))
        return p

    def _apply(self, p: dict) -> dict:
        pid, wf = p["proposal_id"], p["wf"]
        with self.store.tx():
            self._set(pid, status="applying")
        try:
            r = self.client.apply(wf, p["approval_id"])
        except SimError as e:
            if e.code == "stale_base_version":
                st, why = "stale", "config changed since proposal (supervisor edit or another change); re-proposing"
            elif e.status == 410:
                st, why = "lapsed", "approved change not applied inside the apply window"
            elif e.code == "not_approved":
                a = self.client.approval(p["approval_id"])
                with self.store.tx():
                    self._set(pid, status="approved" if a["status"] == "approved" else p["status"])
                return self._on_approval(self.get(pid), a)
            else:
                st, why = "failed", f"{e.code}: {e.detail}"
            with self.store.tx():
                self._set(pid, status=st, last_error=f"{e.status} {e.code}")
                self.store.decide(wf, "governance", f"apply_{st}", why, {"error": str(e)}, proposal_id=pid)
            return self.get(pid)
        with self.store.tx():
            self._set(pid, status="applied", applied_version=r["version"])
            self.store.decide(wf, "governance", "applied",
                              f"{p['approval_id']} applied as v{r['version']} ({', '.join(r['changed_dims'])})",
                              {"result": r}, proposal_id=pid)
        self.ctxs[wf].refresh(self.client, self.store)
        return self.get(pid)

    # ------------------------------------------------------------------ auto channel
    def auto_apply(self, wf: str, now: int, kind: str, config: dict, rationale: str, evidence: dict,
                   items: list) -> dict | None:
        ctx = self.ctxs[wf]
        with self.store.tx():
            pid = self._next_pid(wf, now, "auto")
            req = {"rationale": rationale, "evidence": evidence, "change_summary": rationale.split(". ")[0]}
            self.store.db.execute(
                "INSERT INTO proposals(proposal_id,wf,kind,channel,status,base_version,config,request,items,"
                "created_minute,updated_minute) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (pid, wf, kind, "auto", "auto_applying", ctx.version, json.dumps(config), json.dumps(req),
                 json.dumps(items), now, now))
            self.store.decide(wf, "governance", "auto_apply_intent", req["change_summary"],
                              {"items": items, "base_version": ctx.version}, proposal_id=pid)
        return self._send_auto(pid)

    def _send_auto(self, pid: str) -> dict | None:
        p = self.get(pid)
        body = {"proposal_id": pid, "base_version": p["base_version"], "proposed_config": p["config"],
                "rationale": p["request"]["rationale"], "evidence": p["request"].get("evidence") or {}}
        try:
            r = self.client.auto_apply(p["wf"], body)
        except SimError as e:
            if e.code == "duplicate_proposal_id":
                return self._resolve_auto(p)
            with self.store.tx():
                self._set(pid, status="failed", last_error=f"{e.status} {e.code}: {e.detail}")
                self.store.decide(p["wf"], "governance", "auto_apply_failed", f"{e.code}",
                                  {"detail": e.detail}, proposal_id=pid)
            return None
        with self.store.tx():
            self._set(pid, status="applied", applied_version=r["version"], submitted_minute=self.store.now)
            self.store.decide(p["wf"], "governance", "applied",
                              f"auto-applied as v{r['version']} ({', '.join(r['changed_dims'])})", {"result": r},
                              proposal_id=pid)
        self.ctxs[p["wf"]].refresh(self.client, self.store)
        return self.get(pid)

    def _resolve_auto(self, p: dict) -> dict | None:
        hist = self.client.config_history(p["wf"])
        v = next((h for h in hist if h.get("proposal_id") == p["proposal_id"]), None)
        with self.store.tx():
            if v:
                self._set(p["proposal_id"], status="applied", applied_version=v["version"])
                self.store.decide(p["wf"], "governance", "recovered", f"auto-apply reconciled as v{v['version']}",
                                  {"version": v}, proposal_id=p["proposal_id"])
            else:  # id was burnt by a refused attempt
                self._set(p["proposal_id"], status="failed", last_error="refused before crash")
        return self.get(p["proposal_id"])

    # ------------------------------------------------------------------ crash recovery
    def recover(self):
        """Resolve every write-ahead intent against the simulator before the loop does anything else."""
        for (pid,) in self.store.q("SELECT proposal_id FROM proposals WHERE status='submitting'"):
            p = self.get(pid)
            if self._resolve_submitting(p) is None:
                log.info("recover: %s never reached the simulator; sending now", pid)
                self._send(pid)
        for (pid,) in self.store.q("SELECT proposal_id FROM proposals WHERE status='auto_applying'"):
            p = self.get(pid)
            hist = self.client.config_history(p["wf"])
            if any(h.get("proposal_id") == pid for h in hist):
                self._resolve_auto(p)
            else:
                log.info("recover: auto %s never applied; sending now", pid)
                self._send_auto(pid)
        for (pid,) in self.store.q("SELECT proposal_id FROM proposals WHERE status IN ('applying','approved')"):
            p = self.get(pid)
            self._on_approval(p, self.client.approval(p["approval_id"]))

    def _set(self, pid: str, **cols):
        cols["updated_minute"] = self.store.now
        sets = ", ".join(f"{k}=?" for k in cols)
        self.store.db.execute(f"UPDATE proposals SET {sets} WHERE proposal_id=?", (*cols.values(), pid))
