"""The one shared cohort object (C2).

A `Cohort` is a named conjunction of feature-value lists — exactly the simulator's `where` clause — with an id
derived from its definition. The same object is used by:
  * the config we propose (`cohorts` + per-cohort allocations and hour tables),
  * detection and experiment segments (a segment is "all cohorts whose <feature> is in <values>"),
  * the supervisor view (KPIs per cohort id).

A `CohortPlan` is a partition: up to two *split features*; cohorts are the cross product of their values.
Because the cohorts are disjoint and exhaustive over observed values, first-match order in the simulator
cannot change which cohort a lead lands in, so our own `cohort_of` is guaranteed to agree with the dialler.
Unseen feature values fall to `default`, which always carries the workflow-level allocation.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

MAX_COHORTS = 20


@dataclass(frozen=True)
class Cohort:
    id: str
    where: tuple  # ((feature, (values...)), ...) sorted — hashable

    @staticmethod
    def make(where: dict) -> "Cohort":
        items = tuple(sorted((f, tuple(sorted(v))) for f, v in where.items()))
        cid = ";".join(f"{f}={'+'.join(v)}" for f, v in items) or "default"
        return Cohort(cid, items)

    def where_dict(self) -> dict:
        return {f: list(v) for f, v in self.where}

    def value(self, feature: str) -> str | None:
        for f, v in self.where:
            if f == feature:
                return v[0] if len(v) == 1 else "+".join(v)
        return None

    def matches(self, features: dict) -> bool:
        return all(features.get(f) in v for f, v in self.where)


@dataclass
class CohortPlan:
    split_features: list[str] = field(default_factory=list)
    values: dict[str, list[str]] = field(default_factory=dict)  # feature -> observed values (one cohort each)

    def to_json(self) -> dict:
        return {"split_features": self.split_features, "values": self.values}

    @staticmethod
    def from_json(d: dict | None) -> "CohortPlan":
        if not d:
            return CohortPlan()
        return CohortPlan(list(d.get("split_features", [])), {k: list(v) for k, v in d.get("values", {}).items()})

    def cohorts(self) -> list[Cohort]:
        if not self.split_features:
            return []
        axes = [[(f, v) for v in self.values[f]] for f in self.split_features]
        out = [Cohort.make({f: [v] for f, v in combo}) for combo in itertools.product(*axes)]
        assert len(out) <= MAX_COHORTS, "plan exceeds the simulator's cohort limit"
        return out

    def size(self) -> int:
        s = 1
        for f in self.split_features:
            s *= len(self.values[f])
        return s if self.split_features else 0

    def cohort_of(self, features: dict) -> str:
        if not self.split_features:
            return "default"
        where = {}
        for f in self.split_features:
            v = features.get(f)
            if v not in self.values[f]:
                return "default"
            where[f] = [v]
        return Cohort.make(where).id

    def with_feature(self, feature: str, values: list[str]) -> "CohortPlan | None":
        """Add a split feature if the product still fits in MAX_COHORTS."""
        if feature in self.split_features:
            return self
        p = CohortPlan(self.split_features + [feature], {**self.values, feature: sorted(values)})
        return p if p.size() <= MAX_COHORTS else None

    def config_cohorts(self) -> list[dict]:
        return [{"id": c.id, "where": c.where_dict()} for c in self.cohorts()]
