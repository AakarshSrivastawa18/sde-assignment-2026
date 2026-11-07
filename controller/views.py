"""Read-only views over the controller's own store (never the simulator's logs).

  supervisor_view  C11 — one screen per workflow: KPIs, what's tested, what's winning, what we need from you,
                   what changed today, incidents, caller IDs. Replaces fifty alerts.
  why              C12 — "why did workflow X change at T, who approved it, and what happened next?"
  person           compliance — what a given customer heard on a day, and the decision that made it possible
  health           C14 — the loop's own health: ingest throughput, lag, decision rate, credits, memory, alerts
"""
from __future__ import annotations

import json

from .context import dist_of
from .simtime import MIN_PER_DAY, fmt, parse_when


def _now(st) -> int:
    return st.q1("SELECT MAX(minute) FROM metrics") or st.q1("SELECT MAX(minute) FROM decisions") or 0


def _kpi(st, wf: str, h_from: int, h_to: int) -> dict:
    d, c, e, f = st.q("SELECT COALESCE(SUM(dials),0), COALESCE(SUM(conn),0), COALESCE(SUM(eng),0), "
                      "COALESCE(SUM(flags),0) FROM agg_kpi WHERE wf=? AND h>=? AND h<?", (wf, h_from, h_to))[0]
    return {"dials": d, "pickup": c / d if d else None, "engagement": e / c if c else None,
            "flag_rate": f / c if c else None}


def _pct(x):
    return "—" if x is None else f"{x:.1%}"


def _dist(d: dict) -> str:
    return ", ".join(f"{k} {v:.0%}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


def _workflows(st, wf):
    rows = st.q("SELECT wf, tenant, spec FROM workflows" + (" WHERE wf=?" if wf else "") + " ORDER BY wf",
                (wf,) if wf else ())
    return [(w, t, json.loads(s)) for w, t, s in rows]


# ---------------------------------------------------------------- C11
def supervisor_view(st, wf: str | None = None, as_json: bool = False) -> str:
    now = _now(st)
    day0 = (now // MIN_PER_DAY) * MIN_PER_DAY
    out = []
    data = {}
    for w, tenant, spec in _workflows(st, wf):
        base = st.kv(w, "baseline") or {}
        cv = st.q("SELECT version, actor, config FROM config_versions WHERE wf=? AND config IS NOT NULL "
                  "ORDER BY version DESC LIMIT 1", (w,))
        version, actor, cfg = (cv[0][0], cv[0][1], json.loads(cv[0][2])) if cv else (None, None, None)
        today, last24 = _kpi(st, w, day0 // 60, now // 60 + 1), _kpi(st, w, now // 60 - 24, now // 60 + 1)
        g = spec["objective"].get("guardrail", {})
        rec = {"tenant": tenant, "objective": spec["objective"], "version": version, "actor": actor,
               "kpis": {"baseline": base, "today": today, "last_24h": last24}}
        L = [f"══ {w}  ({tenant}) — primary {spec['objective']['primary']}, guardrail {g}",
             f"   config v{version} by {actor} · now {fmt(now)}",
             f"   KPIs        pickup  engagement  QA-flags   (history baseline: pickup {_pct(base.get('pickup_rate'))}, "
             f"engagement {_pct(base.get('engagement_rate'))})",
             f"     today     {_pct(today['pickup']):>6}  {_pct(today['engagement']):>10}  {_pct(today['flag_rate']):>8}"
             f"   ({today['dials']} dials)",
             f"     last 24h  {_pct(last24['pickup']):>6}  {_pct(last24['engagement']):>10}  {_pct(last24['flag_rate']):>8}"]
        # tested / winning
        L.append("   BEING TESTED / WINNING")
        exps = {}
        for dim in ("welcome_message", "voice"):
            s = st.kv(w, f"exp:{dim}")
            if not s:
                continue
            exps[dim] = s
            for key, seg in s["segs"].items():
                where = "everyone" if key == "*" else f"{s['partition']}={key}"
                if seg["status"] == "promoted":
                    L.append(f"     ✔ {dim} for {where}: {seg['winner']} WINNING (promoted, lift "
                             f"{seg['promo']['lift']:+.1%}; {seg['control']} kept as 15% holdback to watch for decay)")
                elif seg["status"] == "exploring":
                    arms = ", ".join(f"{a}({s_})" for a, s_ in seg["arms"].items())
                    L.append(f"     … {dim} for {where}: control {seg['control']}; arms {arms}")
                else:
                    L.append(f"     · {dim} for {where}: concluded, keeping {seg['control']}")
        sched = st.kv(w, "schedule") or {}
        if sched.get("feature"):
            L.append(f"     ⏱ call-time weights by {sched['feature']} (dry-run pickup "
                     f"{sched['dry_run']['expected_rel_change']:+.1%})")
        did = st.kv(w, "did") or {}
        if did:
            live = dist_of(cfg, "default", "caller_id") if cfg else {}
            L.append("     ☎ caller IDs: " + ", ".join(f"{d} {did['status'].get(d)}{' ' + format(live.get(d, 0), '.0%') if live.get(d) else ''}"
                                               for d in sorted(did["status"])))
        rec["experiments"], rec["schedule_feature"], rec["caller_ids"] = exps, sched.get("feature"), did.get("status")
        # asks
        asks = st.q("SELECT proposal_id, status, approval_id, json_extract(request,'$.change_summary'), "
                    "json_extract(request,'$.impact_score'), json_extract(request,'$.affected_leads') FROM proposals "
                    "WHERE wf=? AND channel='approval' AND status IN ('submitting','pending','approved','applying') "
                    "ORDER BY json_extract(request,'$.impact_score') DESC", (w,))
        L.append("   WHAT WE NEED FROM YOU" + ("" if asks else ": nothing"))
        for pid, status, aid, summ, imp, aff in asks:
            L.append(f"     ▶ {aid or pid} [{status}] impact {imp} · {aff} leads · {summ}")
        rec["asks"] = [dict(zip(("proposal_id", "status", "approval_id", "summary", "impact", "affected"), a)) for a in asks]
        # changes today
        ch = st.q("SELECT version, actor, changed_dims, applied_minute, proposal_id FROM config_versions WHERE wf=? AND "
                  "applied_minute>=? ORDER BY version", (w, day0))
        L.append("   CHANGED TODAY" + ("" if ch else ": nothing"))
        for v, a, dims, m, pid in ch:
            summ = st.q1("SELECT json_extract(request,'$.change_summary') FROM proposals WHERE proposal_id=?", (pid,)) \
                if pid else "supervisor edit"
            L.append(f"     {fmt(m)} v{v} {a} {json.loads(dims or '[]')}: {(summ or '')[:140]}")
        rec["changed_today"] = [dict(zip(("version", "actor", "dims", "minute", "proposal_id"), c)) for c in ch]
        inc = st.q("SELECT id, status, opened_minute, json_extract(detail,'$.prompt') FROM incidents WHERE wf=? AND "
                   "(status NOT IN ('resolved','unconfirmed') OR closed_minute>=?)", (w, day0))
        for iid, status, om, pv in inc:
            L.append(f"   ⚠ INCIDENT {iid}: hallucination on {pv}, {status} (opened {fmt(om)})")
        rec["incidents"] = inc
        pins = {d: u for d, u in (st.kv(w, "pins") or {}).items() if u > now}
        if pins:
            L.append("   supervisor-edited dims we are not touching: " + ", ".join(f"{d} until {fmt(u)}" for d, u in pins.items()))
        out.append("\n".join(L))
        data[w] = rec
    return json.dumps(data, indent=2, default=str) if as_json else "\n\n".join(out)


# ---------------------------------------------------------------- C12
def why(st, wf: str, at: str | None, as_json: bool = False) -> str:
    if not wf:
        return "--workflow is required"
    t = parse_when(at) if at else _now(st)
    row = st.q("SELECT version, actor, changed_dims, proposal_id, approval_id, applied_minute FROM config_versions "
               "WHERE wf=? AND (applied_minute IS NULL OR applied_minute<=?) ORDER BY version DESC LIMIT 1", (wf, t))
    if not row:
        return f"no config version known for {wf} at {fmt(t)}"
    v, actor, dims, pid, aid, m = row[0]
    res = {"workflow": wf, "asked_at": fmt(t), "live_version": v, "actor": actor, "changed_dims": json.loads(dims or "[]"),
           "applied_at": fmt(m)}
    L = [f"{wf} at {fmt(t)}: config v{v}, applied {fmt(m)} by {actor} — changed {res['changed_dims']}"]
    if actor == "supervisor":
        L.append("  A client supervisor edited the config directly (bypasses governance). Our response:")
        for mm, kind, summ in st.q("SELECT minute, kind, summary FROM decisions WHERE wf=? AND minute>=? AND minute<=? "
                                   "AND kind IN ('supervisor_edit','rebased_on_supervisor','hallucination_detected')",
                                   (wf, m, m + 360)):
            L.append(f"    {fmt(mm)} {kind}: {summ}")
    elif pid:
        p = st.q("SELECT kind, channel, status, base_version, created_minute, submitted_minute, decided_minute, "
                 "request, items FROM proposals WHERE proposal_id=?", (pid,))
        if p:
            kind, channel, status, base, cm, sm, dm, req, items = p[0]
            req = json.loads(req)
            res["proposal"] = {"id": pid, "kind": kind, "channel": channel, "status": status, "approval_id": aid,
                               "summary": req.get("change_summary"), "rationale": req.get("rationale"),
                               "impact_score": req.get("impact_score"), "affected_leads": req.get("affected_leads"),
                               "dry_run": req.get("dry_run"), "evidence": req.get("evidence")}
            who = (f"approved by the workflow's client approver on {aid} (decided {fmt(dm)})" if channel == "approval"
                   else "auto-applied under the workflow's signed auto-apply policy (no human approval required)")
            L += [f"  WHY: {req.get('change_summary')}",
                  f"  RATIONALE: {req.get('rationale')}",
                  f"  WHO: {who}; proposed {fmt(cm)} on base v{base}",
                  f"  IMPACT {req.get('impact_score')} · affected leads {req.get('affected_leads')}"]
            if req.get("dry_run"):
                L.append(f"  DRY RUN: {json.dumps(req['dry_run'], default=str)[:600]}")
            trail = st.q("SELECT minute, component, kind, summary FROM decisions WHERE wf=? AND minute<=? AND minute>=? "
                         "AND component IN ('experiment','caller_id','schedule','incident','guardrail') ORDER BY id",
                         (wf, cm, cm - 2 * MIN_PER_DAY))
            if trail:
                L.append("  EVIDENCE TRAIL (48h before the proposal):")
                L += [f"    {fmt(mm)} [{c}] {k}: {s}" for mm, c, k, s in trail]
            res["evidence_trail"] = trail
    # what happened next
    before, after = _kpi(st, wf, m // 60 - 24, m // 60), _kpi(st, wf, m // 60, m // 60 + 24)
    L.append(f"  NEXT 24h vs PRIOR 24h: pickup {_pct(before['pickup'])} → {_pct(after['pickup'])}, engagement "
             f"{_pct(before['engagement'])} → {_pct(after['engagement'])}, QA flags {_pct(before['flag_rate'])} → "
             f"{_pct(after['flag_rate'])}  (before/after, not a controlled comparison)")
    nxt = st.q("SELECT minute, component, kind, summary FROM decisions WHERE wf=? AND minute>? AND minute<=? AND kind IN "
               "('winner_rolled_back','arm_dropped_harm','promotion_withdrawn','incident_resolved','fix_failed_verification',"
               "'did_degraded','guardrail_breach','supervisor_edit','proposal_superseded') ORDER BY id LIMIT 10",
               (wf, m, m + 3 * MIN_PER_DAY))
    nv = st.q("SELECT version, actor, applied_minute FROM config_versions WHERE wf=? AND version>? ORDER BY version LIMIT 1",
              (wf, v))
    if nv:
        L.append(f"  NEXT CHANGE: v{nv[0][0]} by {nv[0][1]} at {fmt(nv[0][2])}")
    if nxt:
        L.append("  FOLLOW-UPS:")
        L += [f"    {fmt(mm)} [{c}] {k}: {s}" for mm, c, k, s in nxt]
    res.update(before=before, after=after, follow_ups=nxt, next_version=nv[0] if nv else None)
    return json.dumps(res, indent=2, default=str) if as_json else "\n".join(L)


# ---------------------------------------------------------------- compliance: one customer
def person(st, pid: str, day: int | None, as_json: bool = False) -> str:
    if not pid:
        return "--person is required"
    sql = "SELECT minute, wf, lead_id, cv, cohort, wm, vo, did, pv, engaged, flag FROM calls WHERE person_id=?"
    args = [pid]
    if day is not None:
        sql += " AND minute>=? AND minute<?"
        args += [day * MIN_PER_DAY, (day + 1) * MIN_PER_DAY]
    rows = st.q(sql + " ORDER BY minute", args)
    if not rows:
        return f"no connected calls recorded for {pid}" + (f" on D{day}" if day is not None else "")
    L, out = [], []
    for m, wf, lid, cv, cohort, wm, vo, did, pv, eng, flag in rows:
        cfgrow = st.q("SELECT actor, proposal_id, approval_id, applied_minute, config FROM config_versions WHERE wf=? AND "
                      "version=?", (wf, cv))
        why_ = "business default"
        alloc = {}
        if cfgrow:
            actor, ppid, aid, am, cfg = cfgrow[0]
            if cfg:
                c = json.loads(cfg)
                alloc = {d: dist_of(c, cohort, d) for d in ("welcome_message", "voice")}
            if ppid:
                summ = st.q1("SELECT json_extract(request,'$.change_summary') FROM proposals WHERE proposal_id=?", (ppid,))
                why_ = f"v{cv} by {actor} ({aid or 'auto-apply'}, {fmt(am)}): {summ}"
            elif actor == "supervisor":
                why_ = f"v{cv} by a client supervisor at {fmt(am)}"
        L.append(f"{fmt(m)} {wf} lead {lid} cohort {cohort}: heard welcome {wm}, voice {vo}, prompt {pv}, from {did}; "
                 f"{'stayed to a conclusion' if eng else 'hung up early'}{' [QA flag]' if flag else ''}\n"
                 f"    live allocation for that cohort: welcome {_dist(alloc.get('welcome_message', {}))}; voice "
                 f"{_dist(alloc.get('voice', {}))} (sampled per call)\n    why that config was live: {why_}")
        out.append({"minute": m, "wf": wf, "variant": [wm, vo, pv, did], "config_version": cv, "why": why_})
    return json.dumps(out, indent=2) if as_json else "\n".join(L)


# ---------------------------------------------------------------- C14
def health(st, as_json: bool = False) -> str:
    m = st.q("SELECT minute, events, events_per_s, ingest_s, decide_s, lag_min, credits, decisions, open_proposals, rss_mb "
             "FROM metrics ORDER BY minute")
    if not m:
        return "no metrics yet"
    total_ev = sum(r[1] for r in m)
    total_ingest = sum(r[3] for r in m)
    last = m[-1]
    alerts = st.q("SELECT minute, severity, wf, code, msg FROM alerts ORDER BY id DESC LIMIT 20")
    res = {"now": fmt(last[0]), "events_ingested": total_ev, "ingest_seconds": round(total_ingest, 1),
           "ingest_events_per_s": round(total_ev / total_ingest, 0) if total_ingest else None,
           "max_decide_s_per_tick": max(r[4] for r in m), "max_lag_min": max(r[5] for r in m),
           "credits_remaining": last[6], "decisions_last_hour": last[7], "open_proposals": last[8],
           "peak_rss_mb": max(r[9] for r in m),
           "decisions_per_hour_avg": round(sum(r[7] for r in m) / max(len(m), 1), 1)}
    L = [f"loop health at {res['now']}",
         f"  ingest: {total_ev} events in {total_ingest:.1f}s ({res['ingest_events_per_s']}/s); max lag "
         f"{res['max_lag_min']} sim-min",
         f"  decide: max {res['max_decide_s_per_tick']:.2f}s per tick; {res['decisions_per_hour_avg']} decisions/hour avg",
         f"  credits remaining {last[6]}; open proposals {last[8]}; peak RSS {res['peak_rss_mb']} MB",
         "  alerts (newest first):"]
    L += [f"    {fmt(a[0])} {a[1].upper():6} {a[2] or '-'} {a[3]}: {a[4]}" for a in alerts] or ["    none"]
    res["alerts"] = alerts
    return json.dumps(res, indent=2, default=str) if as_json else "\n".join(L)
