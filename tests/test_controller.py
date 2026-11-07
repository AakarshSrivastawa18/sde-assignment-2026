"""Controller tests: statistics, cohorts, config diffing/stepping, governance crash recovery, ingest idempotency.

Run: .venv/bin/python -m pytest -q tests/test_controller.py
None of these import `sim`: the controller is tested against fakes of the HTTP contract.
"""
import json
import math

import numpy as np
import pytest

from controller import settings
from controller.cohorts import Cohort, CohortPlan
from controller.context import changed_dims, cohort_for_features, max_shift
from controller.governance import Governance
from controller.ingest import FeatureCodec, Ingestor
from controller.planner import Planner
from controller.stats import poisson_sf, stratified_diff, z_crit
from controller.store import Store


# ---------------------------------------------------------------- statistics
def test_stratified_diff_removes_simpson_bias():
    # Arm and control are identical within each stratum (0.40 and 0.60), but the arm got most of its traffic in
    # the high stratum. Pooling says the arm is much better; the stratified estimate says no difference.
    strata = [(40, 100, 360, 900), (540, 900, 60, 100)]  # (x_arm, n_arm, x_ctl, n_ctl)
    pooled = (40 + 540) / 1000 - (360 + 60) / 1000
    d = stratified_diff(strata)
    assert pooled > 0.15
    assert abs(d.diff) < 1e-9


def test_stratified_diff_matches_two_proportion_z_for_one_stratum():
    d = stratified_diff([(330, 1000, 300, 1000)])
    p = 630 / 2000
    z = 0.03 / math.sqrt(p * (1 - p) * (2 / 1000))
    assert abs(d.z - z) < 1e-9 and abs(d.lift - 0.1) < 1e-9


def test_poisson_sf():
    assert abs(poisson_sf(0, 3.0) - 1.0) < 1e-12
    assert abs(poisson_sf(1, 3.0) - (1 - math.exp(-3))) < 1e-12
    assert poisson_sf(30, 5.0) < 1e-10


def _would_promote(d, m):
    zc = z_crit(m, settings.FAMILY_ALPHA, settings.POCOCK_FACTOR)
    lo = (d.diff - zc * d.se) / d.p_ctl
    return d.n_arm >= settings.MIN_CONNECTED_PER_ARM and d.z >= zc and d.lift >= settings.MIN_LIFT_TO_PROMOTE and lo > 0


def test_false_winner_rate_under_null_is_below_5_percent():
    """A/A: 3 challengers identical to control, 10 daily looks, the promotion rule from experiments.look."""
    rng = np.random.default_rng(7)
    p, sims, false = 0.55, 1500, 0
    share_arm, conn_per_day = 0.10, 1100
    for _ in range(sims):
        cum = {a: [0, 0] for a in range(4)}  # arm 0 = control
        hit = False
        for day in range(10):
            for a in range(4):
                n = int(conn_per_day * (1 - 3 * share_arm if a == 0 else share_arm))
                cum[a][0] += rng.binomial(n, p)
                cum[a][1] += n
            for a in (1, 2, 3):
                d = stratified_diff([(cum[a][0], cum[a][1], cum[0][0], cum[0][1])])
                if _would_promote(d, 3):
                    hit = True
            if hit:
                break
        false += hit
    rate = false / sims
    print(f"A/A false-winner rate: {rate:.3%}")
    assert rate < 0.05


def test_power_for_a_10_percent_lift():
    """Same rule, one arm 10% better at RAMP_SHARE after day 2: most runs should find it within 8 days."""
    rng = np.random.default_rng(11)
    found = 0
    for _ in range(400):
        x = {0: [0, 0], 1: [0, 0]}
        for day in range(8):
            share = settings.EXPLORE_SHARE if day < 2 else settings.RAMP_SHARE
            for a, rate, n in ((0, 0.55, int(1100 * (1 - share))), (1, 0.605, int(1100 * share))):
                x[a][0] += rng.binomial(n, rate)
                x[a][1] += n
            if _would_promote(stratified_diff([(x[1][0], x[1][1], x[0][0], x[0][1])]), 2):
                found += 1
                break
    assert found / 400 > 0.6


# ---------------------------------------------------------------- cohorts and config
def test_cohort_plan_partition_agrees_with_first_match():
    plan = CohortPlan(["age_band", "lang"], {"age_band": ["18-30", "60+"], "lang": ["hi", "ta"]})
    cohorts = plan.config_cohorts()
    assert len(cohorts) == 4
    cfg = {"cohorts": cohorts}
    for f in ({"age_band": "60+", "lang": "ta"}, {"age_band": "18-30", "lang": "hi"}, {"age_band": "31-45", "lang": "hi"}):
        assert cohort_for_features(cfg, f) == plan.cohort_of(f)


def test_cohort_plan_respects_limit():
    plan = CohortPlan(["lang"], {"lang": ["hi", "en", "ta", "te", "kn", "mr", "bn"]})
    assert plan.with_feature("age_band", ["18-30", "31-45", "46-60", "60+"]) is None
    assert Cohort.make({"age_band": ["60+"]}).id == "age_band=60+"


def _cfg(voice, did=None, cohorts=None, extra=None):
    c = {"cohorts": cohorts or [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": voice,
                                                               "caller_id": did or {"D1": 1.0}}},
         "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    if extra:
        c["allocations"].update(extra)
    return c


def test_changed_dims_and_max_shift():
    a = _cfg({"V1": 1.0})
    b = _cfg({"V1": 0.5, "V2": 0.5}, did={"D1": 0.5, "D2": 0.5})
    assert changed_dims(a, b) == ["voice", "caller_id"]
    assert abs(max_shift(a, b) - 0.5) < 1e-9
    assert changed_dims(a, _cfg({"V1": 1.0000001})) == []


def test_auto_step_never_exceeds_max_shift():
    cur = _cfg({"V1": 1.0})
    target = _cfg({"V1": 0.15, "V4": 0.85})
    step = Planner._step(cur, target, 0.2)
    assert max_shift(cur, step) <= 0.2 + 1e-9
    assert max_shift(cur, step) > 0.19
    # repeated steps converge on the target
    for _ in range(10):
        cur = step
        step = Planner._step(cur, target, 0.2)
    assert max_shift(step, target) < 1e-6


# ---------------------------------------------------------------- governance crash recovery
class FakeSim:
    """Just enough of the HTTP contract to exercise governance."""

    def __init__(self):
        self._apr, self.history, self.calls = {}, [{"version": 1, "proposal_id": None, "approval_id": None}], []
        self.version = 1

    def request_approval(self, body):
        self.calls.append(("request", body["proposal_id"]))
        if any(a["proposal_id"] == body["proposal_id"] for a in self._apr.values()):
            from controller.client import SimError
            raise SimError(409, "duplicate_proposal_id", body["proposal_id"])
        aid = f"apr_{len(self._apr) + 1:05d}"
        self._apr[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"], "workflow_id": body["workflow_id"],
                               "status": "pending", "expires_at": "2027-01-05T10:00:00+05:30",
                               "created_at": "2027-01-04T10:00:00+05:30"}
        return self._apr[aid]

    def approvals(self, workflow_id=None, status=None):
        return list(self._apr.values())

    def approval(self, aid):
        return self._apr[aid]

    def apply(self, wid, aid):
        self.calls.append(("apply", aid))
        self.version += 1
        self._apr[aid]["status"] = "applied"
        self.history.append({"version": self.version, "proposal_id": self._apr[aid]["proposal_id"],
                             "approval_id": aid})
        return {"workflow_id": wid, "version": self.version, "changed_dims": ["voice"]}

    def config_history(self, wid):
        return self.history

    def config(self, wid):
        return {"version": self.version, "config": _cfg({"V1": 1.0}), "actor": "x", "applied_at": None}


class Ctx:
    id = "wf"
    version = 1
    gov = {"max_requests_per_24h": 3, "approver_hours": [10, 19], "approver_latency_minutes": [30, 240],
           "proposal_ttl_hours": 24}

    def refresh(self, client, store):
        self.version = client.config("wf")["version"]

    def preferences(self):
        return {}


def _store(tmp_path):
    s = Store(tmp_path / "c.db")
    s.now = 600
    return s


def _insert(store, pid, status, approval_id=None):
    store.db.execute("INSERT INTO proposals(proposal_id,wf,kind,channel,status,base_version,config,request,items,"
                     "created_minute,updated_minute,approval_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, "wf", "routine", "approval", status, 1, json.dumps(_cfg({"V2": 1.0})),
                      json.dumps({"change_summary": "s", "diff": {"x": 1}, "affected_leads": 1, "dry_run": {"x": 1},
                                  "impact_score": 0.1, "rationale": "r"}), "[]", 600, 600, approval_id))


def test_recover_submitting_that_reached_the_sim_is_not_resent(tmp_path):
    store, sim = _store(tmp_path), FakeSim()
    sim.request_approval({"proposal_id": "p1", "workflow_id": "wf"})  # crash happened after POST, before our commit
    sim.calls.clear()
    _insert(store, "p1", "submitting")
    Governance(store, sim, {"wf": Ctx()}).recover()
    assert sim.calls == []
    assert store.q1("SELECT status FROM proposals WHERE proposal_id='p1'") == "pending"
    assert store.q1("SELECT approval_id FROM proposals WHERE proposal_id='p1'") == "apr_00001"


def test_recover_submitting_that_never_reached_the_sim_is_sent_once(tmp_path):
    store, sim = _store(tmp_path), FakeSim()
    _insert(store, "p2", "submitting")
    Governance(store, sim, {"wf": Ctx()}).recover()
    assert sim.calls == [("request", "p2")]
    assert store.q1("SELECT status FROM proposals WHERE proposal_id='p2'") == "pending"


def test_recover_applying_already_applied_is_not_applied_twice(tmp_path):
    store, sim = _store(tmp_path), FakeSim()
    a = sim.request_approval({"proposal_id": "p3", "workflow_id": "wf"})
    a["status"] = "approved"
    sim.apply("wf", a["approval_id"])  # crash after POST /apply, before our commit
    sim.calls.clear()
    _insert(store, "p3", "applying", a["approval_id"])
    Governance(store, sim, {"wf": Ctx()}).recover()
    assert ("apply", a["approval_id"]) not in sim.calls
    assert store.q1("SELECT status FROM proposals WHERE proposal_id='p3'") == "applied"
    assert store.q1("SELECT applied_version FROM proposals WHERE proposal_id='p3'") == 2


def test_approved_proposal_superseded_by_newer_evidence_is_not_applied(tmp_path):
    store, sim = _store(tmp_path), FakeSim()
    a = sim.request_approval({"proposal_id": "p4", "workflow_id": "wf"})
    _insert(store, "p4", "pending", a["approval_id"])
    store.db.execute("UPDATE proposals SET items=? WHERE proposal_id='p4'", (json.dumps([{"dim": "voice"}]),))
    store.now = 700
    store.decide("wf", "experiment", "winner_rolled_back", "V2 stopped winning", {"dim": "voice"})
    a["status"], a["decided_at"] = "approved", "2027-01-04T11:00:00+05:30"
    Governance(store, sim, {"wf": Ctx()}).poll(700)
    assert store.q1("SELECT status FROM proposals WHERE proposal_id='p4'") == "superseded"
    assert not any(c[0] == "apply" for c in sim.calls)


# ---------------------------------------------------------------- ingest
class FakeEvents:
    def __init__(self, chunks):
        self._chunks = chunks  # list of (meta, text)

    def chunks(self, kind, after):
        return [m for m, _ in self._chunks if m["kind"] == kind and m["chunk"] > after]

    def chunk_text(self, n):
        return self._chunks[n][1]


def _chunk(n, events):
    lines = "\n".join(json.dumps(e) for e in events) + "\n"
    return ({"chunk": n, "kind": "events", "events": len(events), "last_event_id": events[-1]["event_id"],
             "until_minute": 600}, lines)


def _events():
    lead = {"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": "wf", "campaign_id": "c1",
            "lead_id": "wf:l1", "person_id": "wf:p1", "features": {"lang": "hi", "city_tier": "1", "age_band": "60+",
                                                                   "value_band": "low"}, "event_id": 1}
    call = {"type": "call", "ts": "2027-01-04T09:05:00+05:30", "tenant_id": "t", "workflow_id": "wf",
            "campaign_id": "c1", "lead_id": "wf:l1", "person_id": "wf:p1", "attempt_no": 1, "campaign_day_index": 0,
            "config_version": 1, "cohort_id": "default",
            "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
            "connected": True, "engaged": True, "disposition": "concluded_interested", "duration_s": 90, "turns": 8,
            "qa": {"hallucination_flag": False, "flow_adherence": 0.9}, "event_id": 2}
    return lead, call


def test_ingest_is_exactly_once_across_restarts(tmp_path):
    lead, call = _events()
    src = FakeEvents([_chunk(0, [lead, call])])
    store = Store(tmp_path / "i.db")
    Ingestor(store, src, FeatureCodec(store)).ingest_available()
    # a "restart": new objects over the same db and the same (unchanged) chunk list
    store2 = Store(tmp_path / "i.db")
    ing2 = Ingestor(store2, src, FeatureCodec(store2))
    assert ing2.lead_fcode == {"wf:l1": 0}
    ing2.ingest_available()
    assert store2.q("SELECT SUM(dials), SUM(conn), SUM(eng) FROM agg_kpi")[0] == (1, 1, 1)
    assert store2.q1("SELECT done FROM leads WHERE lead_id='wf:l1'") == 1


def test_ingest_crash_mid_chunk_leaves_nothing_behind(tmp_path):
    lead, call = _events()
    bad = dict(call, event_id=3)
    del bad["variant"]  # malformed line after a good one -> exception inside the chunk transaction
    src = FakeEvents([_chunk(0, [lead, call, bad])])
    store = Store(tmp_path / "j.db")
    with pytest.raises(KeyError):
        Ingestor(store, src, FeatureCodec(store)).ingest_available()
    assert store.q1("SELECT COUNT(*) FROM agg_kpi") == 0
    assert store.meta("last_chunk", -1) == -1


# ---------------------------------------------------------------- supervisor edits are never auto-overwritten
def test_supervisor_edited_dim_is_approval_only(tmp_path):
    from controller.planner import auto_dims_allowed

    class PolicyCtx:
        id = "wf"

        def auto_dims(self):
            return {"schedule", "caller_id", "welcome_message", "voice", "cohorts"}

    store = _store(tmp_path)
    assert auto_dims_allowed(PolicyCtx(), store) == {"schedule", "caller_id", "welcome_message", "voice"}
    store.set_kv("wf", "supervisor_touched", {"voice": 7500})
    assert "voice" not in auto_dims_allowed(PolicyCtx(), store)   # long after the 24h pin, still approval-only
