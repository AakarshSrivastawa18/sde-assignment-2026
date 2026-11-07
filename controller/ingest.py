"""Event ingestion (C1): chunk-at-a-time, exactly-once, restartable.

Contract with the event source: chunks are numbered globally and increase; a chunk's content never changes.
So the cursor is a single integer, `last_chunk`, and each chunk is folded into the aggregates inside ONE
SQLite transaction that also moves the cursor. A crash before COMMIT leaves the chunk un-ingested and it is
re-read on restart; a crash after COMMIT never re-applies it. That is the whole idempotency story — no
per-event dedup table is needed because the unit of atomicity is the chunk.

The lead→feature join (C2) happens here: `lead` events carry features, `call` events carry only lead_id.
Live leads are held in memory (lead_id → fcode) and persisted in `leads`, so a restart rebuilds the map.
Leads are pruned once their cadence can no longer dial them.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

from . import settings
from .simtime import MIN_PER_DAY, ts_minute
from .store import Store

log = logging.getLogger("controller.ingest")


class FeatureCodec:
    """Feature dict <-> small int. One shared code space; cohorts and segments are sets of codes."""

    def __init__(self, store: Store):
        self.store = store
        self.by_key: dict[str, int] = {}
        self.by_code: dict[int, dict] = {}
        for code, key, fj in store.q("SELECT fcode, fkey, fjson FROM features"):
            self.by_key[key] = code
            self.by_code[code] = json.loads(fj)

    def code(self, features: dict) -> int:
        key = "|".join(f"{k}={features[k]}" for k in sorted(features))
        c = self.by_key.get(key)
        if c is None:
            c = len(self.by_key)
            self.by_key[key] = c
            self.by_code[c] = dict(features)
            self.store.db.execute("INSERT INTO features(fcode,fkey,fjson) VALUES(?,?,?)",
                                  (c, key, json.dumps(dict(sorted(features.items())))))
        return c

    def values(self) -> dict[str, list[str]]:
        out: dict[str, set] = defaultdict(set)
        for f in self.by_code.values():
            for k, v in f.items():
                out[k].add(v)
        return {k: sorted(v) for k, v in out.items()}

    def codes_where(self, where: dict) -> set[int]:
        return {c for c, f in self.by_code.items() if all(f.get(k) in vals for k, vals in where.items())}


class Ingestor:
    def __init__(self, store: Store, client, codec: FeatureCodec):
        self.store, self.client, self.codec = store, client, codec
        self.lead_fcode: dict[str, int] = {}
        self.cadence_days: dict[str, int] = {}
        self.unjoined_calls = 0
        # reload live leads (crash recovery)
        for lid, fc in store.q("SELECT lead_id, fcode FROM leads"):
            self.lead_fcode[lid] = fc

    @property
    def cursor(self) -> int:
        return self.store.meta("last_chunk", -1)

    def pending(self) -> list[dict]:
        cur = self.cursor
        out = self.client.chunks("history", cur) + self.client.chunks("events", cur)
        return sorted(out, key=lambda c: c["chunk"])

    def ingest_available(self, on_sim_event=None) -> dict:
        """Ingest every chunk after the cursor. Returns {'events', 'seconds', 'chunks', 'last_minute'}."""
        t0 = time.perf_counter()
        n_events = 0
        chunks = self.pending()
        last_minute = None
        for ch in chunks:
            if ch["chunk"] <= self.cursor:
                continue
            text = self.client.chunk_text(ch["chunk"])
            n, lm, sim_events = self._ingest_chunk(ch, text)
            n_events += n
            last_minute = lm if lm is not None else last_minute
            if on_sim_event:
                for ev in sim_events:
                    on_sim_event(ev)
        return {"events": n_events, "seconds": time.perf_counter() - t0, "chunks": len(chunks),
                "last_minute": last_minute}

    def _ingest_chunk(self, ch: dict, text: str):
        eng = defaultdict(lambda: [0, 0])
        pick = defaultdict(lambda: [0, 0])
        kpi = defaultdict(lambda: [0, 0, 0, 0])
        leads_rows, done, calls_rows, sim_rows, sim_events = [], [], [], [], []
        lead_fcode = self.lead_fcode
        codec_code = self.codec.code
        n = 0
        last_minute = None
        loads = json.loads
        with self.store.tx() as db:
            for line in text.splitlines():
                if not line:
                    continue
                ev = loads(line)
                n += 1
                t = ev["type"]
                if t == "call":
                    m = ts_minute(ev["ts"])
                    last_minute = m
                    wf = ev["workflow_id"]
                    h = m // 60
                    lid = ev["lead_id"]
                    fc = lead_fcode.get(lid, -1)
                    if fc < 0:
                        self.unjoined_calls += 1
                    var = ev["variant"]
                    did, pv, cv = var["caller_id"], var["prompt_version"], ev["config_version"]
                    c = ev["connected"]
                    p = pick[(wf, h, fc, did)]
                    p[0] += 1
                    k = kpi[(wf, h, cv, pv)]
                    k[0] += 1
                    if c:
                        p[1] += 1
                        e = ev["engaged"]
                        flag = 1 if (ev.get("qa") or {}).get("hallucination_flag") else 0
                        g = eng[(wf, h, fc, cv, var["welcome_message"], var["voice"], pv)]
                        g[0] += 1
                        k[1] += 1
                        k[3] += flag
                        if e:
                            g[1] += 1
                            k[2] += 1
                            done.append((lid,))
                        if settings.STORE_CONNECTED_CALLS:
                            calls_rows.append((ev["event_id"], wf, m, ev["person_id"], lid, cv, ev["cohort_id"],
                                               var["welcome_message"], var["voice"], did, pv, int(e), flag))
                elif t == "lead":
                    fc = codec_code(ev["features"])
                    lid = ev["lead_id"]
                    lead_fcode[lid] = fc
                    leads_rows.append((lid, ev["workflow_id"], ev["person_id"], ts_minute(ev["ts"]) // MIN_PER_DAY, fc))
                else:
                    m = ts_minute(ev["ts"])
                    last_minute = m
                    sim_rows.append((ev["event_id"], t, ev.get("workflow_id"), m, line))
                    sim_events.append(ev)
            db.executemany(
                "INSERT INTO agg_eng VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "conn=conn+excluded.conn, eng=eng+excluded.eng",
                [(*key, v[0], v[1]) for key, v in eng.items()])
            db.executemany(
                "INSERT INTO agg_pick VALUES(?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials, conn=conn+excluded.conn",
                [(*key, v[0], v[1]) for key, v in pick.items()])
            db.executemany(
                "INSERT INTO agg_kpi VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials, conn=conn+excluded.conn, eng=eng+excluded.eng, "
                "flags=flags+excluded.flags",
                [(*key, *v) for key, v in kpi.items()])
            db.executemany("INSERT OR IGNORE INTO leads(lead_id,wf,person_id,day,fcode) VALUES(?,?,?,?,?)",
                           leads_rows)
            db.executemany("UPDATE leads SET done=1 WHERE lead_id=?", done)
            db.executemany("INSERT OR IGNORE INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", calls_rows)
            db.executemany("INSERT OR IGNORE INTO sim_events VALUES(?,?,?,?,?)", sim_rows)
            self.store.set_meta("last_chunk", ch["chunk"])
            self.store.set_meta("last_event_id", ch["last_event_id"])
            self.store.set_meta("ingested_until_minute", ch["until_minute"])
        return n, last_minute, sim_events

    def prune(self, today: int):
        """Drop leads that no cadence can dial any more (memory bound for the join)."""
        rows = self.store.q("SELECT wf, spec FROM workflows")
        with self.store.tx() as db:
            for wf, spec in rows:
                keep_days = len(json.loads(spec)["cadence"]) + settings.LEAD_RETENTION_EXTRA_DAYS
                old = [r[0] for r in db.execute("SELECT lead_id FROM leads WHERE wf=? AND day < ?",
                                                (wf, today - keep_days)).fetchall()]
                if old:
                    db.execute("DELETE FROM leads WHERE wf=? AND day < ?", (wf, today - keep_days))
                    for lid in old:
                        self.lead_fcode.pop(lid, None)
