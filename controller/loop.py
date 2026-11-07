"""The always-on loop. One iteration = one simulated tick:

    ingest new chunks ─▶ react to config_changed (supervisor edits) ─▶ poll approvals (apply what's approved)
      ─▶ per workflow: caller-ID health · hallucination incidents · schedule (daily) · experiments
         (daily look + 2-hourly safety checks) · planner (auto-apply / batched approval)
      ─▶ metrics ─▶ POST /sim/advance

Restart semantics: everything the loop knows is in SQLite. On start it re-reads workflows and live configs,
resolves write-ahead governance intents, and continues from the ingest cursor at whatever minute the world
is at. Decisions are deterministic functions of stored state, so a restarted controller makes the same
choices an uninterrupted one would have.
"""
from __future__ import annotations

import json
import logging
import resource
import time

from . import callerid, experiments, schedule, settings
from .client import SimClient
from .context import EXPERIMENT_DIMS, WorkflowCtx
from .governance import Governance
from .incidents import IncidentResponder
from .ingest import FeatureCodec, Ingestor
from .planner import Planner
from .simtime import MIN_PER_DAY, fmt, hour_of_day, ts_minute
from .store import Store

log = logging.getLogger("controller.loop")


class Controller:
    def __init__(self, sim_url: str, db_path: str):
        self.client = SimClient(sim_url)
        self.store = Store(db_path)
        self.codec = FeatureCodec(self.store)
        self.ingestor = Ingestor(self.store, self.client, self.codec)
        self.ctxs: dict[str, WorkflowCtx] = {}
        self.gov = Governance(self.store, self.client, self.ctxs)
        self.incidents = IncidentResponder(self.store, self.client, self.gov, self.ctxs)
        self.planner = Planner(self.store, self.client, self.gov, self.ctxs, self.codec)
        self._pending_cfg_events: list[dict] = []
        self._tick_events = 0
        self._tick_ingest_s = 0.0

    # ------------------------------------------------------------------ startup
    def start(self):
        st = self.client.state()
        self.store.now = st["sim_minute"]
        scen = self.store.meta("scenario")
        if scen is not None and scen != st["scenario"]:
            raise SystemExit(f"state db belongs to scenario {scen!r}, simulator runs {st['scenario']!r}")
        with self.store.tx():
            self.store.set_meta("scenario", st["scenario"])
            if self.store.meta("credits_total") is None:
                self.store.set_meta("credits_total", st["credits_remaining"])
            for spec in self.client.workflows():
                self.store.db.execute("INSERT INTO workflows(wf,tenant,spec) VALUES(?,?,?) ON CONFLICT(wf) DO UPDATE "
                                      "SET spec=excluded.spec", (spec["workflow_id"], spec["tenant_id"], json.dumps(spec)))
                ctx = WorkflowCtx(spec, self.store, self.codec)
                ctx.refresh(self.client, self.store)
                self.ctxs[ctx.id] = ctx
        restarted = self.store.meta("bootstrapped", False)
        if restarted:
            self.store.decide(None, "loop", "restart", f"controller restarted at {fmt(self.store.now)}; "
                              f"ingest cursor chunk {self.ingestor.cursor}")
            log.info("restart at %s, cursor %s — reconciling governance intents", fmt(self.store.now),
                     self.ingestor.cursor)
            self.gov.recover()
        r = self.ingestor.ingest_available(self._on_sim_event)
        log.info("ingested %d events in %.1fs (%d chunks)", r["events"], r["seconds"], r["chunks"])
        self._handle_config_events()
        if not restarted:
            self._bootstrap()

    def _bootstrap(self):
        """C3: everything history can tell us before D0 — reference rates, call-time tables, caller-ID census."""
        now = self.store.now
        with self.store.tx():
            for wf, ctx in self.ctxs.items():
                from .incidents import reference_rate
                ref = reference_rate(self.store, wf)
                kpi = self.store.q("SELECT SUM(dials), SUM(conn), SUM(eng) FROM agg_kpi WHERE wf=? AND h<0", (wf,))[0]
                base = {"pickup_rate": kpi[1] / kpi[0] if kpi[0] else None,
                        "engagement_rate": kpi[2] / kpi[1] if kpi[1] else None, "halluc_flag_rate": ref,
                        "dials": kpi[0]}
                self.store.set_kv(wf, "baseline", base)
                self.store.decide(wf, "bootstrap", "history_baseline",
                                  f"history: pickup {base['pickup_rate'] or 0:.3f}, engagement "
                                  f"{base['engagement_rate'] or 0:.3f}, QA flag rate {ref:.3%} over {kpi[0]} dials",
                                  base)
                schedule.step(ctx, self.store, self.codec, now, force=True)
                for dim in EXPERIMENT_DIMS:
                    if len(ctx.options(dim)) > 1 and self.store.kv(wf, f"exp:{dim}") is None:
                        st = experiments.init_state(ctx, dim)
                        st["start_h"] = max(now // 60, 0)
                        self.store.set_kv(wf, f"exp:{dim}", st)
                        self.store.decide(wf, "experiment", "experiment_started",
                                          f"{dim}: control {st['segs']['*']['control']}, challengers "
                                          f"{sorted(st['segs']['*']['arms'])} at {settings.EXPLORE_SHARE:.0%} each",
                                          {"dim": dim, "state": st})
            self.store.set_meta("bootstrapped", True)

    # ------------------------------------------------------------------ events
    def _on_sim_event(self, ev: dict):
        if ev["type"] == "config_changed":
            self._pending_cfg_events.append(ev)

    def _handle_config_events(self):
        evs, self._pending_cfg_events = self._pending_cfg_events, []
        for ev in evs:
            wf = ev["workflow_id"]
            ctx = self.ctxs.get(wf)
            if not ctx:
                continue
            m = ts_minute(ev["ts"])
            with self.store.tx():
                self.store.db.execute(
                    "INSERT INTO config_versions(wf,version,actor,changed_dims,proposal_id,approval_id,applied_minute,"
                    "config) VALUES(?,?,?,?,?,?,?,NULL) ON CONFLICT(wf,version) DO UPDATE SET actor=excluded.actor, "
                    "changed_dims=excluded.changed_dims, proposal_id=excluded.proposal_id, "
                    "approval_id=excluded.approval_id, applied_minute=excluded.applied_minute",
                    (wf, ev["version"], ev["actor"], json.dumps(ev["changed_dims"]), ev.get("proposal_id"),
                     ev.get("approval_id"), m))
                if ev["version"] >= ctx.version:
                    ctx.refresh(self.client, self.store)
                    self.store.db.execute("UPDATE config_versions SET config=? WHERE wf=? AND version=? AND config IS NULL",
                                          (json.dumps(ctx.config), wf, ctx.version))
                if ev["actor"] == "supervisor":
                    self._on_supervisor_edit(ctx, ev, m)

    def _on_supervisor_edit(self, ctx: WorkflowCtx, ev: dict, minute: int):
        pins = self.store.kv(ctx.id, "pins") or {}
        until = minute + settings.SUPERVISOR_PIN_HOURS * 60
        for d in ev["changed_dims"]:
            pins[d] = max(pins.get(d, 0), until)
        self.store.set_kv(ctx.id, "pins", pins)
        self.store.decide(ctx.id, "governance", "supervisor_edit",
                          f"supervisor changed {ev['changed_dims']} (v{ev['version']}); those dims are pinned until "
                          f"{fmt(until)} — routine changes will not touch them, and open proposals built on the old "
                          f"version will be re-proposed on the new one", {"event": ev, "pins": pins})
        for d in ev["changed_dims"]:
            if d in EXPERIMENT_DIMS:
                experiments.on_supervisor_edit(ctx, self.store, d)

    # ------------------------------------------------------------------ one tick
    def tick(self, now: int):
        self.store.now = now
        for wf, ctx in self.ctxs.items():
            try:
                self._tick_wf(ctx, now)
            except Exception:  # one workflow's bug must not stop the platform's loop
                log.exception("workflow %s failed at %s", wf, fmt(now))
                with self.store.tx():
                    self.store.alert("page", wf, "controller_exception",
                                     f"{wf}: controller step raised at {fmt(now)} — see controller log")
        if now % MIN_PER_DAY == 0:
            self.ingestor.prune(now // MIN_PER_DAY)

    def _tick_wf(self, ctx: WorkflowCtx, now: int):
        wf = ctx.id
        urgent = False
        with self.store.tx():
            d = callerid.step(ctx, self.store, now)
            self.store.set_kv(wf, "did_desired", d)
            urgent |= d["urgent"]
            schedule.step(ctx, self.store, self.codec, now)
        self.incidents.detect(wf, now)
        self.incidents.step(wf, now)
        hod, mod = hour_of_day(now), now % 60
        lo, hi = ctx.window
        look_h = max(ctx.gov["approver_hours"][0], ctx.preferences().get("earliest_hour", 0)) - 1
        with self.store.tx():
            for dim in EXPERIMENT_DIMS:
                if self.store.kv(wf, f"exp:{dim}") is None:
                    continue
                if now >= 0 and mod == 0 and hod == look_h:
                    r = experiments.look(ctx, self.store, self.codec, dim, now)
                    urgent |= r["urgent"]
                elif now >= 0 and mod == 0 and lo + 2 <= hod <= hi and hod % 2 == 0:
                    r = experiments.look(ctx, self.store, self.codec, dim, now, intraday=True)
                    urgent |= r["urgent"]
        self.planner.act(ctx, now, urgent=urgent)

    # ------------------------------------------------------------------ run
    def run(self, kill_at_minute: int | None = None):
        self.start()
        last_metrics = None
        while True:
            t0 = time.perf_counter()
            st = self.client.state()
            now = st["sim_minute"]
            self.store.now = now
            r = self.ingestor.ingest_available(self._on_sim_event)
            self._tick_events += r["events"]
            self._tick_ingest_s += r["seconds"]
            self._handle_config_events()
            self.gov.poll(now)
            t1 = time.perf_counter()
            if not st["finished"]:
                self.tick(now)
            t2 = time.perf_counter()
            if last_metrics is None or now - last_metrics >= 60 or st["finished"]:
                self._metrics(now, st, t2 - t1)
                last_metrics = now
            if st["finished"]:
                log.info("scenario finished at %s", fmt(now))
                with self.store.tx():
                    self.store.decide(None, "loop", "finished", f"scenario finished at {fmt(now)}")
                return
            if kill_at_minute is not None and now >= kill_at_minute:
                log.warning("simulated crash requested at %s", fmt(now))
                import os
                os._exit(137)
            self.client.advance(settings.TICK_MINUTES)
            if now % MIN_PER_DAY == 0:
                log.info("%s  events=%d  ingest=%.1fs  loop=%.2fs", fmt(now), self._tick_events,
                         self._tick_ingest_s, time.perf_counter() - t0)

    def _metrics(self, now: int, st: dict, decide_s: float):
        backlog = st["chunks"] - 1 - self.ingestor.cursor
        lag = 0 if backlog <= 0 else now - self.store.meta("ingested_until_minute", now)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1024 * 1024) if rss > 10**7 else rss / 1024  # macOS bytes, Linux KiB
        decisions = self.store.q1("SELECT COUNT(*) FROM decisions WHERE minute>? AND minute<=?", (now - 60, now)) or 0
        open_p = self.store.q1("SELECT COUNT(*) FROM proposals WHERE status IN ('submitting','pending','approved',"
                               "'applying','auto_applying')") or 0
        eps = self._tick_events / self._tick_ingest_s if self._tick_ingest_s > 0 else 0
        with self.store.tx():
            self.store.db.execute(
                "INSERT OR REPLACE INTO metrics VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now, time.time(), self._tick_events, round(eps, 1), round(self._tick_ingest_s, 3), round(decide_s, 3),
                 lag, st["credits_remaining"], decisions, open_p, round(rss_mb, 1)))
            total = self.store.meta("credits_total") or 0
            if total and st["credits_remaining"] < 0.2 * total:
                self.store.alert("page", None, "credits_low", f"credits at {st['credits_remaining']} of {total}: "
                                 f"incident response will stop at the floor — top up or raise the cap")
            if lag > 60:
                self.store.alert("page", None, "ingest_lag", f"ingest is {lag} sim-minutes ({backlog} chunks) behind")
        self._tick_events = 0
        self._tick_ingest_s = 0.0
