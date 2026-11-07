"""Size the hallucination CUSUM (k, h): false-incident rate per 10-day run vs detection delay.

    .venv/bin/python scripts/cusum_sizing.py

A run has ~120 dialling hours. Reference flag rate 1.5% (QA false positives + baseline hallucinations).
n = connected calls per hour: 70 (small KYC), 110 (dev cancel line), 1300 (load-scenario workflow).
"""
import math

import numpy as np

rng = np.random.default_rng(1)


def first_alarm(rate, ref, n, hours, k, h):
    S = 0.0
    for t in range(hours):
        S = max(0.0, S + rng.poisson(n * rate) * math.log(k) - n * ref * (k - 1))
        if S >= h:
            return t + 1
    return None


ref = 0.015
print("k    h   FP/run(n=70,110,1300)        median delay hours: 2x(n=70,110,1300) | 3.6x(...)")
for k in (2.0, 2.5, 3.0):
    for h in (6, 7, 8, 9, 10):
        fp = [np.mean([first_alarm(ref, ref, n, 120, k, h) is not None for _ in range(2000)]) for n in (70, 110, 1300)]
        d = []
        for mult in (2.0, 3.6):
            for n in (70, 110, 1300):
                ts = [first_alarm(ref * mult, ref, n, 120, k, h) for _ in range(400)]
                d.append(float(np.median([t for t in ts if t] or [999])))
        print(f"{k:<4} {h:<3} {[round(x, 4) for x in fp]}  {d[:3]} | {d[3:]}")
