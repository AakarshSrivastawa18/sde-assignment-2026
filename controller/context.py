"""Per-workflow view of the world the components share: spec, policy, current config, time helpers."""
from __future__ import annotations

import json

from . import settings
from .cohorts import Cohort, CohortPlan
from .simtime import MIN_PER_DAY, hour_of_day

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
EXPERIMENT_DIMS = ("welcome_message", "voice")


class WorkflowCtx:
    def __init__(self, spec: dict, store, codec):
        self.spec = spec
        self.id = spec["workflow_id"]
        self.tenant = spec["tenant_id"]
        self.store = store
        self.codec = codec
        self.version: int = spec.get("current_version", 1)
        self.config: dict = {}
        self.actor: str = "business_default"

    def refresh(self, client, store) -> bool:
        """Re-read the live config; record any version we have not seen. Returns True if it changed."""
        c = client.config(self.id)
        changed = c["version"] != self.version or not self.config
        self.version, self.config, self.actor = c["version"], c["config"], c["actor"]
        if changed:
            from .simtime import ts_minute
            store.db.execute(
                "INSERT OR IGNORE INTO config_versions(wf,version,actor,changed_dims,proposal_id,approval_id,"
                "applied_minute,config) VALUES(?,?,?,?,?,?,?,?)",
                (self.id, c["version"], c["actor"], None, None, None,
                 ts_minute(c["applied_at"]) if c.get("applied_at") else None, json.dumps(c["config"])))
        return changed

    # ---- policy
    @property
    def gov(self) -> dict:
        return self.spec["governance"]

    @property
    def objective(self) -> dict:
        return self.spec["objective"]

    @property
    def primary(self) -> str:
        return self.objective["primary"]

    @property
    def guardrail(self) -> tuple[str, float]:
        g = self.objective.get("guardrail") or {}
        for k, v in g.items():
            return k.replace("_max_drop", ""), float(v)
        return ("", 0.0)

    def auto_dims(self) -> set[str]:
        a = self.gov.get("auto_apply") or {}
        return set(a.get("allowed_dims") or []) if a.get("enabled") else set()

    def max_shift(self) -> float:
        return float((self.gov.get("auto_apply") or {}).get("max_shift") or 0.0)

    def options(self, dim: str) -> list[str]:
        return list(self.spec["options"][dim])

    @property
    def window(self) -> tuple[int, int]:
        lo, hi = self.spec["dialling_window"]
        return int(lo), int(hi)

    def window_hours(self) -> list[int]:
        lo, hi = self.window
        return list(range(lo, hi))

    @property
    def cadence_days(self) -> int:
        return len(self.spec["cadence"])

    def preferences(self) -> dict:
        return settings.TENANT_PREFERENCES.get(self.tenant, {})

    # ---- approver timing
    def approver_open(self, minute: int) -> bool:
        lo, hi = self.gov["approver_hours"]
        lo = max(lo, self.preferences().get("earliest_hour", lo))
        return lo <= hour_of_day(minute) < hi

    def worst_decision_minute(self, minute: int) -> int:
        """Latest time the approver could decide a request sent now (mirrors documented approver hours)."""
        lo, hi = self.gov["approver_hours"]
        t = minute + int(self.gov["approver_latency_minutes"][1])
        day, mod = divmod(t, MIN_PER_DAY)
        if mod < lo * 60:
            return day * MIN_PER_DAY + lo * 60
        if mod >= hi * 60:
            return (day + 1) * MIN_PER_DAY + lo * 60
        return t

    def same_day_decision(self, minute: int) -> bool:
        """True if even the slowest approver answers today. When the latency can exceed the whole approver day
        (e.g. 8h latency in a 10–18 day) no slot qualifies, so we accept the first slot of the day instead."""
        lo, hi = self.gov["approver_hours"]
        lat = int(self.gov["approver_latency_minutes"][1])
        if lo * 60 + lat >= hi * 60:
            return minute % MIN_PER_DAY < lo * 60 + 60
        return minute + lat < (minute // MIN_PER_DAY) * MIN_PER_DAY + hi * 60

    def decision_before_expiry(self, minute: int) -> bool:
        # the approver still decides when decide_minute == expires_minute
        return self.worst_decision_minute(minute) <= minute + int(float(self.gov["proposal_ttl_hours"]) * 60)

    # ---- cohorts in the live config (may be supervisor-defined, so ids are taken as given)
    def live_cohorts(self) -> list[Cohort]:
        return [Cohort(c["id"], tuple(sorted((f, tuple(sorted(v))) for f, v in c["where"].items())))
                for c in self.config.get("cohorts", [])]

    def plan(self) -> CohortPlan:
        return CohortPlan.from_json(self.store.kv(self.id, "plan"))


def dist_of(cfg: dict, cohort_id: str, dim: str) -> dict:
    a = cfg.get("allocations", {})
    return (a.get(cohort_id) or {}).get(dim) or a["default"][dim]


def cohort_for_features(cfg: dict, features: dict) -> str:
    for c in cfg.get("cohorts", []):
        if all(features.get(f) in v for f, v in c["where"].items()):
            return c["id"]
    return "default"


def hour_table(cfg: dict, cohort_id: str) -> dict | None:
    hw = cfg.get("schedule", {}).get("hour_weights", {})
    if cohort_id in hw:
        return hw[cohort_id]
    return hw.get("default")


def norm(d: dict) -> dict:
    s = sum(v for v in d.values() if v > 0)
    return {k: v / s for k, v in sorted(d.items()) if v > 0} if s > 0 else {}


def dist_close(a: dict, b: dict, tol: float = 1e-4) -> bool:
    a, b = norm(a), norm(b)
    return all(abs(a.get(k, 0) - b.get(k, 0)) <= tol for k in set(a) | set(b))


def tv(a: dict, b: dict) -> float:
    a, b = norm(a), norm(b)
    return 0.5 * sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b))


def table_close(a: dict | None, b: dict | None, tol: float = 1e-4) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    keys = set(a) | set(b)
    return all(abs(float(a.get(k, 0)) - float(b.get(k, 0))) <= tol for k in keys)


def changed_dims(old: dict, new: dict) -> list[str]:
    """Same semantics as the documented `changed_dims` (cohorts, agent dims per cohort, prompt, schedule)."""
    ch = []
    oc = [(c["id"], json.dumps(c["where"], sort_keys=True)) for c in old.get("cohorts", [])]
    nc = [(c["id"], json.dumps(c["where"], sort_keys=True)) for c in new.get("cohorts", [])]
    if oc != nc:
        ch.append("cohorts")
    keys = set(old.get("allocations", {})) | set(new.get("allocations", {}))
    for dim in AGENT_DIMS:
        if any(not dist_close(dist_of(old, k, dim), dist_of(new, k, dim)) for k in keys):
            ch.append(dim)
    if old.get("prompt_version") != new.get("prompt_version"):
        ch.append("prompt_version")
    os_, ns = old.get("schedule", {}), new.get("schedule", {})
    ohw, nhw = os_.get("hour_weights", {}) or {}, ns.get("hour_weights", {}) or {}
    if set(ohw) != set(nhw) or any(not table_close(ohw[k], nhw[k]) for k in ohw) or \
            (os_.get("lead_preferred_hour") or {}) != (ns.get("lead_preferred_hour") or {}):
        ch.append("schedule")
    order = ["cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule"]
    return [d for d in order if d in ch]


def max_shift(old: dict, new: dict) -> float:
    keys = {"default"} | {c["id"] for c in old.get("cohorts", [])} | {c["id"] for c in new.get("cohorts", [])}
    worst = 0.0
    for k in keys:
        for dim in AGENT_DIMS:
            worst = max(worst, tv(dist_of(old, k, dim), dist_of(new, k, dim)))
    return worst
