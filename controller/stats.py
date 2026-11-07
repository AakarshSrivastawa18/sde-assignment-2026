"""Deterministic statistics. No randomness anywhere: the same data always gives the same verdict.

The core estimator is a *stratified* difference of proportions. Arms are randomised per dial, so within one
stratum (a day × config version × prompt) arm and control see the same population and the same prompt. Across
strata the allocation shares change (we ramp arms) and baseline engagement drifts (prompt changes, campaign
mix); pooling naively would be Simpson-biased. Weighting each stratum by n_a·n_c/(n_a+n_c) (the
Cochran–Mantel–Haenszel weight) removes that bias and is the minimum-variance weight under homogeneity.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

_N = NormalDist()


def norm_cdf(z: float) -> float:
    return _N.cdf(z)


def norm_ppf(p: float) -> float:
    return _N.inv_cdf(min(max(p, 1e-12), 1 - 1e-12))


@dataclass
class Diff:
    diff: float          # stratified p_arm - p_control
    se: float            # unpooled standard error (for CIs)
    se0: float           # pooled-under-null standard error (for tests)
    n_arm: int
    n_ctl: int
    p_ctl: float         # control rate over the same strata

    @property
    def z(self) -> float:
        return self.diff / self.se0 if self.se0 > 0 else 0.0

    @property
    def lift(self) -> float:
        return self.diff / self.p_ctl if self.p_ctl > 0 else 0.0

    def lift_ci(self, z: float) -> tuple[float, float]:
        if self.p_ctl <= 0:
            return (0.0, 0.0)
        return ((self.diff - z * self.se) / self.p_ctl, (self.diff + z * self.se) / self.p_ctl)

    def as_dict(self) -> dict:
        lo, hi = self.lift_ci(1.96)
        return {"lift": round(self.lift, 4), "lift_ci95": [round(lo, 4), round(hi, 4)], "z": round(self.z, 3),
                "n_arm": self.n_arm, "n_ctl": self.n_ctl, "p_ctl": round(self.p_ctl, 4),
                "diff": round(self.diff, 5)}


def stratified_diff(strata) -> Diff | None:
    """strata: iterable of (x_arm, n_arm, x_ctl, n_ctl). Strata missing either side are skipped."""
    sw = swd = v = v0 = 0.0
    na = nc = 0
    xc_w = nc_w = 0.0
    for xa, n_a, xc, n_c in strata:
        if n_a <= 0 or n_c <= 0:
            continue
        w = n_a * n_c / (n_a + n_c)
        pa, pc = xa / n_a, xc / n_c
        pp = (xa + xc) / (n_a + n_c)
        sw += w
        swd += w * (pa - pc)
        v += w * w * (pa * (1 - pa) / n_a + pc * (1 - pc) / n_c)
        v0 += w * w * pp * (1 - pp) * (1 / n_a + 1 / n_c)
        na += n_a
        nc += n_c
        xc_w += w * pc
        nc_w += w
    if sw == 0:
        return None
    return Diff(diff=swd / sw, se=math.sqrt(v) / sw, se0=math.sqrt(v0) / sw, n_arm=na, n_ctl=nc,
                p_ctl=xc_w / nc_w)


def z_crit(m: int, alpha: float, pocock: float) -> float:
    """One-sided critical z for m simultaneous comparisons (Bonferroni) under repeated looks (Pocock-ish)."""
    return norm_ppf(1 - alpha / (max(m, 1) * pocock))


def poisson_sf(k: int, mu: float) -> float:
    """P(X >= k) for X ~ Poisson(mu). Exact for the small counts we test."""
    if k <= 0:
        return 1.0
    if mu <= 0:
        return 0.0
    term = math.exp(-mu)
    cdf = term
    for i in range(1, k):
        term *= mu / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def poisson_cdf(k: int, mu: float) -> float:
    """P(X <= k)."""
    return 1.0 - poisson_sf(k + 1, mu)


def oe_z(observed: float, expected: float) -> float:
    """z for observed vs expected count under Poisson (Anscombe-free, fine for expected ≥ 30)."""
    if expected <= 0:
        return 0.0
    return (observed - expected) / math.sqrt(expected)
