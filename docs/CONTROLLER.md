# Controller runbook

For the engineer who owns or gets paged for the outcome loop. Design rationale lives in `SUBMISSION.md`;
this page is how to run it, read it, debug it and change it.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# the contract: start against a running simulator, run until the scenario finishes
./run.sh http://127.0.0.1:8700                      # state in $CONTROLLER_DB (default state/controller.db)

# one scenario end to end (baseline + sim + controller + grade) → reports/<scenario>.md
scripts/run_scenario.sh scenarios/public_01_dev.yaml 8701
scripts/eval_all.sh scenarios/public_01_dev.yaml scenarios/public_02_incidents.yaml   # parallel, one line each
scripts/crash_test.sh scenarios/public_02_incidents.yaml 5 8790                      # → reports/crash_test.md
scripts/load_test.sh                                                                  # → reports/load_test.md
.venv/bin/python -m pytest -q tests                                                   # 21 tests, ~3 s
```

Re-running `run.sh` with the same db **resumes** (crash-safe). A db that belongs to another scenario is refused; use
a fresh `CONTROLLER_DB` per scenario. `PYTHONHASHSEED` is pinned in `run.sh` and configs are sent in canonical key
order, so runs are reproducible.

## Look at it

```bash
python -m controller view   --db state/X.db [--workflow ks_cancel]   # supervisor screen: KPIs, tests, asks, changes today
python -m controller why    --db state/X.db --workflow ks_cancel --at "D3 18:00"   # why the config is what it is
python -m controller person --db state/X.db --person ks_cancel:p0001234 --day 3    # what a customer heard, and why
python -m controller health --db state/X.db                          # ingest rate, lag, decide time, credits, alerts
sqlite3 state/X.db "SELECT minute, wf, component, kind, summary FROM decisions ORDER BY id DESC LIMIT 30"
```
Minutes are sim minutes since D0 00:00 (`D3 14:00` = 3×1440 + 840 = 5160). Every command takes `--json`.

## Code map

| file | owns |
|---|---|
| `loop.py` | the tick: ingest → config events → poll approvals → per-workflow components → planner → metrics → advance; startup/recovery |
| `ingest.py` | chunk → aggregates in one txn with the cursor; lead→feature join; pruning |
| `store.py` | SQLite schema, `tx()`, `decide()` (audit row), `alert()` |
| `governance.py` | the only code that changes configs: proposal state machine, timing/budgets, auto-apply, `recover()`, `superseded_by()` |
| `planner.py` | composes component desired-states into one config; splits auto vs approval; impact/affected/dry run |
| `callerid.py` | O/E caller-ID health, verdicts, desired caller distribution |
| `schedule.py` | call-time feature choice (G-test), hour weights, IPF dry run |
| `experiments.py` | welcome/voice arms: looks, ramp/drop/promote, segment split, decay rollback, supervisor re-base |
| `incidents.py` | hallucination CUSUM, confirmation, fix choice, validation, proposal, verification |
| `guardrail.py` | daily KPI drift + guardrail breach (backs out schedule) |
| `cohorts.py` | `Cohort` / `CohortPlan`: the one shared cohort object |
| `stats.py` | stratified diff, critical z, Poisson tails — no randomness |
| `views.py` | the read-only commands above |
| `settings.py` | **every threshold, with the reason for its value** |

## State (one SQLite file)

`meta` cursors (`last_chunk`, `cfg_events_handled_upto`, `bootstrapped`, `credits_total`) · `agg_eng` (connected/engaged
by hour × feature-code × config version × welcome × voice × prompt) · `agg_pick` (dials/connects by hour × feature-code ×
caller ID) · `agg_kpi` (hour × version × prompt) · `leads` (live join) · `calls` (connected calls, for `person`) ·
`sim_events` · `config_versions` · `proposals` · `kv` (component state: `exp:<dim>`, `did`, `schedule`, `pins`, `plan`,
`urgent`, …) · `decisions` (append-only audit) · `incidents` · `alerts` · `metrics`.

## Alerts and what to do

| alert | severity | do |
|---|---|---|
| `did_degraded_<D>` (fair share) | page | the number is likely spam-flagged; the controller has rotated it out. Ask the carrier, replace the number in the workflow's options |
| `did_degraded_<D>` (overload) | ticket | nothing; it is rested 48h and re-probed |
| `hallucination_no_fix` | page | no validated prompt within the credit cap. A human picks a prompt; check `incidents` detail for what failed |
| `credits_low` | page | incident response stops at the 10% floor. Top up or raise `INCIDENT_CREDIT_CAP` deliberately |
| `ingest_lag` | page | the controller is behind the event stream; check `health`, the sim/bus, disk |
| `controller_exception` | page | one workflow's step raised; others continue. Log has the traceback; the step retries next tick |
| `hallucination_incident` / `_unconfirmed` | ticket | informational / check the QA detector on sample calls |
| `guardrail_breach` | ticket | our schedule is already backed out for 72h; look at `why` for the last schedule change |
| `primary_drift`, `fix_engagement_regression` | ticket | review with the client; usually the world (number, prompt), not us |

## Debugging a decision

1. `why --workflow W --at T` → the version, the proposal, the evidence trail, what happened after.
2. `SELECT detail FROM decisions WHERE id=…` → full stats (n, z, lift CI, z_crit) for that decision.
3. `SELECT * FROM proposals WHERE wf=W ORDER BY created_minute` → the governance path (statuses, reasons, errors).
4. Component state now: `SELECT v FROM kv WHERE wf=W AND k='exp:voice'`.

## Extending

- **New lever (e.g. per-person timing):** add a component that writes its desired slice to `kv`, teach
  `Planner.build` to place it in the config, and add an `_items` branch for its summary/impact/dry run.
  Governance, batching, crash safety and audit come for free.
- **New threshold:** add it to `settings.py` with a reason. If it changes a statistical rule, extend
  `tests/test_controller.py` (the A/A false-winner test is the guard).
- **New tenant preference:** `settings.TENANT_PREFERENCES[tenant_id]` (`max_routine_requests_per_day`, `earliest_hour`).
- **Postgres:** `store.py` is the only file that knows it's SQLite (`ON CONFLICT … DO UPDATE` and `WITHOUT ROWID`
  are the only dialect bits).
