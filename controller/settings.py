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
RAMP_Z = 0.84                     # P(better) ≥ 0.8 → give the arm more traffic (allocation, not a verdict)
SEGMENT_MIN_SHARE = 0.08          # segments smaller than this share of connected calls are not tested
SEGMENT_MIN_LIFT = 0.05           # a segment-level win must be at least this big to justify a new cohort

# ---- allocation -------------------------------------------------------------------------------
EXPLORE_SHARE = 0.10              # first exposure of an untested arm: a 10%-worse arm costs ≤1% engagement
RAMP_SHARE = 0.25                 # a promising arm
MAX_CHALLENGER_TOTAL = 0.55       # control always keeps ≥45% while exploring
HOLDBACK = 0.15                   # after promotion the old control keeps 15%: needed to detect decay
DECAY_WINDOW_HOURS = 24           # dialling hours (≈2 days) in the rolling decay window
DECAY_MIN_AGE_HOURS = 6           # don't judge a promotion in its first 6 dialling hours
DECAY_DIFF_Z = 2.33               # lift fell vs lift-at-promotion, p<0.01
DECAY_NEG_Z = -1.64               # or lift is now negative, p<0.05 (rollback is cheap and safe)

# ---- caller IDs -------------------------------------------------------------------------------
DID_EXPLORE_SHARE = 0.08          # untested number gets 8%: a spam-flagged one costs ≤~1.5% pickup
DID_MAX_UNKNOWN_AT_ONCE = 2
DID_MIN_EXPECTED_PICKUPS = 30     # population, not anecdote: ≥30 expected connects before judging
DID_BAD_RATIO = 0.75              # observed/expected pickup ≤ 0.75 ...
DID_BAD_Z = -3.0                  # ... and significant at p≈0.001
DID_HEALTHY_RATIO = 0.85
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
HALLUC_WINDOW_H = 3
HALLUC_MIN_CONNECTED = 150
HALLUC_MIN_FLAGS = 8
HALLUC_P = 1e-4                   # Poisson tail vs the workflow's reference flag rate
HALLUC_RATIO = 2.0
HALLUC_RESOLVED_RATIO = 1.5       # post-fix flag rate must be ≤1.5× reference, over ≥ HALLUC_MIN_CONNECTED
VALIDATION_N = 300                # 3 credits; P(false fail) for a 1%-fail prompt at a 0.97 bar < 0.1%
VALIDATION_MARGIN = 0.005
INCIDENT_CREDIT_CAP = 18          # per incident: 1 LLM call + up to 5 validations
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
