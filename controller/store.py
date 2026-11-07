"""The controller's only state: one SQLite file (WAL).

Three groups of tables:
  * ingestion   — cursor, lead→feature join, hour-grain aggregates, slim connected-call log
  * decisions   — config versions seen, proposals (the governance state machine), component state,
                  incidents, and an append-only `decisions` audit log that every component writes to
  * operations  — metrics and alerts about the loop itself

Why SQLite: it is a library, not a server — no new stateful infrastructure (stakeholders/03). The schema is
plain SQL and moves to the Postgres the platform already runs; see SUBMISSION.md §8 for the partitioning.
Every mutation that must be atomic with the ingest cursor or with a governance transition happens inside
one `with store.tx():` block.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS workflows (wf TEXT PRIMARY KEY, tenant TEXT, spec TEXT);
CREATE TABLE IF NOT EXISTS features (fcode INTEGER PRIMARY KEY, fkey TEXT UNIQUE, fjson TEXT);
CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY, wf TEXT, person_id TEXT, day INTEGER, fcode INTEGER, done INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS leads_wf_day ON leads(wf, day);
-- engagement facts: connected calls only
CREATE TABLE IF NOT EXISTS agg_eng (
  wf TEXT, h INTEGER, fcode INTEGER, cv INTEGER, wm TEXT, vo TEXT, pv TEXT, conn INTEGER, eng INTEGER,
  PRIMARY KEY (wf, h, fcode, cv, wm, vo, pv)) WITHOUT ROWID;
-- pickup facts: every dial
CREATE TABLE IF NOT EXISTS agg_pick (
  wf TEXT, h INTEGER, fcode INTEGER, did TEXT, dials INTEGER, conn INTEGER,
  PRIMARY KEY (wf, h, fcode, did)) WITHOUT ROWID;
-- workflow KPIs + QA
CREATE TABLE IF NOT EXISTS agg_kpi (
  wf TEXT, h INTEGER, cv INTEGER, pv TEXT, dials INTEGER, conn INTEGER, eng INTEGER, flags INTEGER,
  PRIMARY KEY (wf, h, cv, pv)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS calls (
  event_id INTEGER PRIMARY KEY, wf TEXT, minute INTEGER, person_id TEXT, lead_id TEXT, cv INTEGER,
  cohort TEXT, wm TEXT, vo TEXT, did TEXT, pv TEXT, engaged INTEGER, flag INTEGER);
CREATE INDEX IF NOT EXISTS calls_person ON calls(person_id, minute);
CREATE TABLE IF NOT EXISTS sim_events (
  event_id INTEGER PRIMARY KEY, type TEXT, wf TEXT, minute INTEGER, body TEXT);

CREATE TABLE IF NOT EXISTS config_versions (
  wf TEXT, version INTEGER, actor TEXT, changed_dims TEXT, proposal_id TEXT, approval_id TEXT,
  applied_minute INTEGER, config TEXT, PRIMARY KEY (wf, version));
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY, wf TEXT, kind TEXT, channel TEXT, status TEXT, base_version INTEGER,
  config TEXT, request TEXT, items TEXT, created_minute INTEGER, submitted_minute INTEGER,
  approval_id TEXT, decided_minute INTEGER, applied_version INTEGER, reasons TEXT, last_error TEXT,
  updated_minute INTEGER);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals(wf, status);
CREATE TABLE IF NOT EXISTS kv (wf TEXT, k TEXT, v TEXT, PRIMARY KEY (wf, k));
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, wf TEXT, minute INTEGER, component TEXT, kind TEXT,
  summary TEXT, detail TEXT, proposal_id TEXT, wall_ts REAL);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(wf, minute);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, wf TEXT, kind TEXT, status TEXT, opened_minute INTEGER, closed_minute INTEGER,
  detail TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, severity TEXT, wf TEXT, code TEXT, msg TEXT,
  wall_ts REAL);
CREATE TABLE IF NOT EXISTS metrics (
  minute INTEGER PRIMARY KEY, wall_ts REAL, events INTEGER, events_per_s REAL, ingest_s REAL,
  decide_s REAL, lag_min INTEGER, credits REAL, decisions INTEGER, open_proposals INTEGER, rss_mb REAL);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # a killed *process* never loses a committed txn in WAL
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-200000")
        self.db.executescript(SCHEMA)
        self._in_tx = False
        self.now = 0  # current sim minute; set by the loop, stamped on audit rows

    @contextmanager
    def tx(self):
        if self._in_tx:  # nested: join the outer transaction
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")
        finally:
            self._in_tx = False

    # ---- small helpers
    def q(self, sql: str, args=()) -> list[tuple]:
        return self.db.execute(sql, args).fetchall()

    def q1(self, sql: str, args=()):
        r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    def meta(self, k: str, default=None):
        v = self.q1("SELECT v FROM meta WHERE k=?", (k,))
        return default if v is None else json.loads(v)

    def set_meta(self, k: str, v):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v)))

    def kv(self, wf: str, k: str, default=None):
        v = self.q1("SELECT v FROM kv WHERE wf=? AND k=?", (wf, k))
        return default if v is None else json.loads(v)

    def set_kv(self, wf: str, k: str, v):
        self.db.execute("INSERT INTO kv(wf,k,v) VALUES(?,?,?) ON CONFLICT(wf,k) DO UPDATE SET v=excluded.v",
                        (wf, k, json.dumps(v, sort_keys=True)))

    def decide(self, wf: str | None, component: str, kind: str, summary: str, detail: dict | None = None,
               proposal_id: str | None = None):
        """Append one row to the audit log. Every observation that leads to (or blocks) a change lands here."""
        self.db.execute(
            "INSERT INTO decisions(wf,minute,component,kind,summary,detail,proposal_id,wall_ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (wf, self.now, component, kind, summary, json.dumps(detail or {}, default=str, sort_keys=True),
             proposal_id, time.time()))

    def alert(self, severity: str, wf: str | None, code: str, msg: str):
        """severity: 'page' (act now) or 'ticket' (next business day). De-duplicated per (wf, code, day)."""
        day = self.now // 1440
        dup = self.q1("SELECT 1 FROM alerts WHERE wf IS ? AND code=? AND minute/1440=?", (wf, code, day))
        if not dup:
            self.db.execute("INSERT INTO alerts(minute,severity,wf,code,msg,wall_ts) VALUES(?,?,?,?,?,?)",
                            (self.now, severity, wf, code, msg, time.time()))
