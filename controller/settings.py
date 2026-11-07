"""Every tunable the controller uses, in one place, with the reason for its value.

Changing a number here changes behaviour on every workflow. Each constant says what it trades off;
SUBMISSION.md §5 explains the derivations.
"""

# ---- loop -------------------------------------------------------------------------------------
TICK_MINUTES = 15                 # advance one sim tick per loop: decisions are at most 15 sim-min stale
SNAPSHOT_METRICS_EVERY_TICKS = 4  # one metrics row per simulated hour

# ---- ingestion --------------------------------------------------------------------------------
LEAD_RETENTION_EXTRA_DAYS = 1     # keep leads for len(cadence)+1 days after upload, then prune
STORE_CONNECTED_CALLS = True      # slim per-call log of connected calls (what a customer *heard*), for
                                  # the compliance question "why did customer X hear Y on day D"

# ---- statistics -------------------------------------------------------------------------------
FAMILY_ALPHA = 0.05               # one-sided family-wise error per (workflow, dim, segment-family) look
POCOCK_FACTOR = 4.0               # ~10 daily looks: Pocock nominal alpha ≈ alpha/4 per look
MIN_CONNECTED_PER_ARM = 300       # min connected calls on an arm before it can win (normal approx + power)
MIN_LIFT_TO_PROMOTE = 0.02        # relative lift; smaller wins are not worth a change request
HARM_DROP_Z = -2.33               # drop an arm whose lift is below 0 at p<0.01 (one-sided), n ≥ HARM_MIN_N
HARM_MIN_N = 100
SEVERE_HARM_Z = -3.0              # intraday (non-look) drops need stronger evidence
FUTILITY_MIN_N = 600              # stop an arm whose 95% upper bound on lift is < MIN_LIFT_TO_PROMOTE
RAMP_Z = 1.28                     # P(better) ≥ 0.9 → give the arm more traffic (allocation, not a verdict)
RAMP_MIN_N = 200
SEGMENT_MIN_SHARE = 0.08          # segments smaller than this share of connected calls are not tested
SEGMENT_MIN_LIFT = 0.05           # a segment-level win must be at least this big to justify a new cohort

# ---- allocation -------------------------------------------------------------------------------
EXPLORE_SHARE = 0.10              # first exposure of an untested arm: a 10%-worse arm costs ≤1% engagement
RAMP_SHARE = 0.25                 # a promising arm
MAX_CHALLENGER_TOTAL = 0.55       # control always keeps ≥45% while exploring
HOLDBACK = 0.15                   # after promotion the old control keeps 15%: needed to detect decay
DECAY_WINDOW_HOURS = 24           # dialling hours (≈2 days) in the rolling decay window
DECAY_MIN_AGE_HOURS = 6           # don't judge a promotion in its first 6 dialling hours
DECAY_DIFF_Z = 2.58               # lift fell vs lift-at-promotion, p<0.005 (checked every 2h: overlapping looks)
DECAY_NEG_Z = -2.33               # or lift is now negative, p<0.01
DECAY_MIN_CTL = 300               # holdback calls in the window before judging: 1 day of a 15% holdback is ~200
                                  # calls and produced a false rollback of a real winner in testing
STABILITY_MIN_N = 50              # per daily window, for the "effect still present in each of the last 2 days" check

# ---- caller IDs -------------------------------------------------------------------------------
DID_EXPLORE_SHARE = 0.08          # untested number gets 8%: a spam-flagged one costs ≤~1.5% pickup
DID_MAX_UNKNOWN_AT_ONCE = 2
DID_MIN_EXPECTED_PICKUPS = 30     # population, not anecdote: ≥30 expected connects before judging
DID_BAD_RATIO = 0.75              # observed/expected pickup ≤ 0.75 ...
DID_BAD_Z = -3.0                  # ... and significant at p≈0.001
DID_HEALTHY_RATIO = 0.85
DID_REPROBE_HOURS = 48           # an *overloaded* number is rested this long, then explored again
DID_INCIDENT_WINDOW_H = 4         # absolute hours for the fast incident check
DID_HEALTH_WINDOW_H = 48          # absolute hours for the health census
AUTO_STEP_COOLDOWN_MIN = 30       # between consecutive auto-apply steps on one workflow

# ---- schedule (call time) ---------------------------------------------------------------------
SCHED_LOOKBACK_DAYS = 14
SCHED_PRIOR_DIALS = 200           # shrink each (segment, hour) pickup toward the workflow hour profile
SCHED_GAMMA = 2.0                 # weight = relative pickup ^ gamma (sharpening)
SCHED_W_MIN, SCHED_W_MAX = 0.15, 6.0
SCHED_MIN_GAIN = 0.01             # relative pickup gain the dry run must show to propose a schedule
SCHED_MATERIAL_LOG_CHANGE = 0.25  # re-push a schedule only when some weight moves by > e^0.25
SCHED_FEATURE_P = 1e-4            # interaction (segment × hour) must be this significant to split

# ---- hallucination incidents ------------------------------------------------------------------
# Poisson CUSUM on QA flags per connected call, over hourly aggregates since the current prompt went live.
# k=2.5, h=8 chosen by simulation (scripts/cusum_sizing.py): ≈0.3–0.5% false incidents per workflow per
# 10-day run at 70–1300 connected/hour, median delay ≈3h for a 3.6× spike, ≈11–18h for a 2× creep.
HALLUC_CUSUM_K = 2.5
HALLUC_CUSUM_H = 8.0
HALLUC_CUSUM_H_FRESH = 6.0        # a prompt live < 48h is untrusted: alarm sooner (~1h faster on a 3.6× spike).
                                  # The extra false alarms (~4%/run) cost 3 credits each: confirmation catches them.
HALLUC_FRESH_HOURS = 48
HALLUC_MIN_CONNECTED = 150
VERIFY_MIN_CONNECTED = 300        # post-fix evidence before we call it resolved
VERIFY_MAX_CONNECTED = 1000       # ... and the most we wait before deciding on the point estimate
VERIFY_OK_P = 0.01                # resolved: flags not significantly above reference (Poisson tail > 1%)
VERIFY_FAIL_P = 1e-3              # failed: significantly above (and ≥1.5× reference)
HALLUC_RESOLVED_RATIO = 1.5
VALIDATION_N_KNOWN = 300          # a prompt with clean real-call history: 3 credits, governance needs the run
VALIDATION_N_UNKNOWN = 800        # never run here: 8 credits; n=300 cannot tell a 1% from a 2.75% failure rate
VALIDATION_MARGIN_KNOWN = 0.005
VALIDATION_MARGIN_UNKNOWN = 0.01
INCIDENT_CREDIT_CAP = 30          # per incident: 1 LLM call + a known-good check + up to 3 unknown validations
CREDIT_FLOOR_SHARE = 0.10         # never spend the last 10% of credits except to validate a known-good revert

# ---- governance -------------------------------------------------------------------------------
INCIDENT_RESERVED_REQUESTS = 1    # of max_requests_per_24h, kept back for incidents/rollbacks
REJECT_BACKOFF_MIN = 120
STALE_REPROPOSE_MIN = 15
SUPERVISOR_PIN_HOURS = 24         # a dim a supervisor edited is not touched by routine changes for 24h
# Voluntary, stricter-than-policy preferences gathered from client memos (stakeholders/04). Keyed by
# tenant id; unknown tenants get the policy only.
TENANT_PREFERENCES = {
    "kestrel_finance": {"max_routine_requests_per_day": 2, "earliest_hour": 11},
}
