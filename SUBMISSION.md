# Outcome-First Agent Loop — Submission

**Name:** Aakarsh Srivastava · **Date:** 2026-11-07 · **Hours actually spent (be honest):** _[fill in before submitting]_ ·
**Language / storage:** Python 3.11+ (stdlib + `httpx`; `numpy` only in tests/scripts) / one SQLite file (WAL)

> Runbook for the engineer on call: [`docs/CONTROLLER.md`](docs/CONTROLLER.md). Every number below is reproducible
> with the scripts named next to it.

---

## 0. TL;DR

- An always-on controller that ingests the event stream exactly once, keeps hour-grain aggregates in SQLite, and runs
  five loops per workflow: **caller-ID health, call-time scheduling, welcome×voice experiments (with decay rollback),
  hallucination incident response, and a KPI/guardrail monitor**. All of them feed one desired config. A governance
  client applies it through `/approvals` (one batched request) or policy-permitted auto-apply (stepped under `max_shift`).
- **public_01**: conclusion rate **+13.4%** (pickup +8.2%, engagement +3.4%), 24 changes, **0 harmful**.
  **public_02**: conclusion **+26.8%** (pickup +23.9%, engagement +0.1%), 31 changes, **0 harmful**. Flagged caller ID
  off within 15 min of the verdict. Supervisor's bad prompt fixed through approval with validation evidence:
  **MTTR 180 min**, 6 credits.
- Robustness: across public_01/02 plus 4 seed variants, **1 harmful change in 6 runs**
  ([reports/robustness.md](reports/robustness.md)). Load: **30–35s controller time per simulated day** at public_03
  volume, 25–31k events/s live, 410 MB RSS ([reports/load_test.md](reports/load_test.md)). Crash test: 8 kills
  (3 injected at the worst moment), **decisions identical to an uninterrupted run** ([reports/crash_test.md](reports/crash_test.md)).
- **Cut:** per-person call time, cross-tenant fairness (C15), a UI (the supervisor view is a CLI), a weekly
  compliance digest, proactive prompt improvement.

## 1. Assumptions

1. `*_max_drop` in `objective.guardrail` is an **absolute** drop in the rate (0.02 = 2 points), not relative.
2. Approver timing: a request sent at night only queues, so we send **only inside approver hours**. We do not use
   the trick of submitting at 06:00 so the latency lands exactly at 10:00. Compliance said approvers dislike queues.
3. When a workflow's max approver latency is longer than its approver day (nw_kyc: 8h latency, 10–18), no send time
   guarantees a same-day decision, so we send at the first slot of the day. The TTL still covers it.
4. One `max_requests_per_24h` slot per workflow is **reserved for incidents/rollbacks**. Routine work uses the rest.
5. Tenant preferences stricter than policy (Kestrel: ≤2 routine requests/day, none before 11:00) are honoured as
   **voluntary** limits. They live in `settings.TENANT_PREFERENCES`, keyed by tenant id. Unknown tenants get the
   policy only.
6. `mid_flight: next_campaign_only` means our agent-facing changes reach only new campaigns. Analysis uses the
   variant on each call, so locked campaigns are fine as evidence.
7. Caller-ID health is relative: a number is judged against its **peer numbers in the same hours**. A workflow with a
   single number in history has no comparison until we explore a second one.
8. The QA `hallucination_flag` has a stable false-positive floor. The **pre-D0 history rate** (upper 95% bound) is each
   workflow's reference.
9. A validation run measures safety only. Engagement of a prompt is known only from real calls on that workflow.
10. History before D0 ran on the business default (all leads, one variant), so it informs **call time, caller-ID
    baselines, KPI and QA baselines**, but nothing about untried welcome/voice options.
11. The controller may read only the HTTP API. The scenario files, `runs/` and `sim/` were read by me as a developer
    to understand the contract. Seed variants of the public scenarios (`devscen/`, git-ignored) were used **only** to
    test robustness to noise. Nothing in `controller/` reads them, and no tunable is keyed to a public workflow id or
    effect.
12. One controller process owns the store (single writer). Scale-out partitions workflows across processes (§8).

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | **Built** | Everything else is only as fresh as this. Chunk = unit of atomicity: aggregates + cursor in one txn | Kafka consumer with offset = cursor; same txn pattern |
| C2 | Lead joins + one cohort object | **Built** | `Cohort`/`CohortPlan` used by config, experiments, schedule and the view | — |
| C3 | Bootstrap from history | **Built** | Call-time tables, caller-ID census, KPI and QA reference rates from pre-D0 | — |
| C4 | Call-time optimisation | **Partial: per cohort** | G-test picks the feature with a distinct hour profile; interaction weights; IPF dry run | Per-person: see §3 Product. Needs returning-person data, a 200k-entry config push per day, governance for it |
| C5 | Caller-ID health | **Built** | Most outcome per line of code: pickup is the primary KPI on nw_kyc and the guardrail elsewhere | Carrier spam-status feed; scheduled re-probe of flagged numbers |
| C6 | Welcome × voice experiments | **Built** | Primary KPI on 2 of 3 workflows | Interaction terms; Bayesian allocation if approvals get cheaper |
| C7 | Promotion, decay detection, rollback | **Built** | The decaying welcome variant is the scenario's trap | CUSUM on winner-vs-holdback instead of 2-hourly windows |
| C8 | Hallucination incident response | **Built** | Regulated clients; MTTR is graded | — |
| C9 | Governance client | **Built** | The boundary is absolute; crash safety lives here | Batch across workflows of one approver |
| C10 | Impact, affected leads, dry runs | **Partial** | Defined and honest (point estimates, method stated) but not calibrated | Track predicted vs realised per change; calibrate impact |
| C11 | Supervisor view | **Partial: CLI** | `python -m controller view`: one screen, not fifty alerts | Read-only HTTP endpoint over the same function |
| C12 | Audit store | **Built** | Mandatory; `why` and `person` answer the compliance questions | — |
| C13 | Crash recovery, demonstrated | **Built** | SRE non-negotiable; proven, not promised | — |
| C14 | Loop observability | **Partial** | `metrics` + `alerts` tables, `health` CLI, page/ticket split | Prometheus exporter; alert routing |
| C15 | Fair allocation across tenants | **Cut** | 3–12 workflows never contend for credits (6 of 200 used) | Per-tenant credit budgets + approver-attention budget |

**Principle:** build what moves the primary KPI **safely**, and make it provably safe before making it clever. Every
non-negotiable in README §5 got a built capability (C9, C12, C13, C5/C8 for populations and credits). Next came the
two biggest outcome levers the evidence showed: caller-ID spreading and call timing for pickup, experiments for
engagement. I cut what has no effect at this scale (C15), and what Product wants but the data can't support yet
(per-person timing).

## 3. Stakeholders

### 01 — Sales (Nikhil)
- **Accepted:** speed where policy allows. Kestrel's welcome/voice/caller-ID/schedule changes go out by auto-apply
  in `max_shift` steps 30 minutes apart, with no approval round-trip. Per-client lift numbers come from `view`.
- **Declined:** a blended score (Product and README §5.3 forbid it). "Winning by lunch, live by evening": a lunchtime
  lead is noise. Our A/A test shows a 3.7% false-winner rate *with* the daily look rule; peeking hourly multiplies
  that. Pushing the persuasive prompt: an LLM may propose, never decide. P4 is a hallucination risk, needs validation
  ≥0.97 (its expected pass rate sits right at that bar), and a demo on another account is not evidence (Product:
  winners don't cross clients).
- **Wrong against policy:** "Kestrel turned on auto-apply for everything". `ks_cancel` allows only
  schedule/caller_id/welcome/voice, **max_shift 0.2**; cohorts and prompts need approval. "Northwind is fine with
  auto-apply on KYC": `nw_kyc` has `auto_apply.enabled: false`, and Northwind's compliance head asks for named human
  approval of anything customer-facing.
- **Reply:** *"Nikhil, we can show a real lift per client: on our test data, completed conversations are up 13–28% with
  zero harmful changes. What I can't do is one blended score (Product's rule) or push lunchtime winners. Most of
  those are noise, and Northwind's compliance head would end the program over one bad change. Kestrel's auto-apply
  covers some dimensions only, in 20% steps; Northwind KYC has auto-apply off. The persuasive script raises
  hallucinations, so it stays out. For the summit I'll give you per-client lift with the audit trail behind it. That
  story sells better in BFSI."*

### 02 — Client Compliance (Meera, Northwind)
- **Accepted:** every welcome/voice/script change on Northwind goes through `/approvals` (their policy allows
  auto only for schedule/caller_id on renewal). Script changes carry `evidence.validation_run_id` and the dry run
  shows the target prompt's real-call history. Requests go out only inside 10–19 / 10–18. "Slow and right" is
  built in: promotions need the effect in each of the last two days, unapplied promotions are re-checked and
  withdrawn, and stale approvals are not applied. The IRDAI question is answered by `controller person --person <id>
  --day <n>`: what the customer heard, the cohort allocation live then, and the approval that made it live.
- **Pushed back:** the weekly summary is not built as a digest. Every operational change is in the audit store and
  `view` today; the digest is a template over the same queries.
- **Wrong against policy:** "operational settings … are covered by the policy we signed" is true for **renewal**
  (`allowed_dims: [schedule, caller_id]`) but **not for KYC**, where auto-apply is disabled. On KYC even caller-ID
  rotation and call timing go to an approver.
- **Reply:** *"Meera, confirmed on all five. Nothing a Northwind customer hears changes without a named approval. Script
  changes carry a validation run and the script's own call history. Requests only reach your approvers 10:00–19:00
  (KYC 10:00–18:00). For IRDAI we can show, per customer and day, the exact opening, voice and script they heard and
  the approval behind it, within minutes. One correction: your KYC policy has auto-apply off entirely, so caller-ID
  and call-timing changes there also come to your approvers. The weekly summary of operational changes on renewal
  will follow; the data is already recorded."*

### 03 — Platform/SRE (Kabir)
- **Accepted:** the kill/restart test (`scripts/crash_test.sh`): 5 random SIGKILLs plus 3 injected kills right after
  a successful submit/apply/auto-apply, before we record it. 0 double-applies, 0 lost approvals, exactly-once
  ingest, decisions identical to an uninterrupted run. Idempotency: deterministic proposal ids, write-ahead intents,
  chunk-atomic ingest, once-per-minute looks. Alerts have two severities: **page** only for a degraded caller ID at a
  fair share, a hallucination with no fix, credits <20%, ingest lag >60 sim-min, or a controller exception. Everything
  else is a **ticket**, and each message says what to look at. Loop health without logs: `controller health`.
- **No new stateful infra:** SQLite is a library and a file. Nobody operates it at 2 a.m. Backup is a file copy.
  In production the same schema moves to the Postgres you already run (§8).
- **Numbers:** one simulated day of public_03 volume costs **30–35s of controller time** (about 60s wall including the
  simulator), against your 10-minute budget. Where it breaks at 10×: §8.
- **Reply:** *"Kabir, the kill test is in the repo: scripts/crash_test.sh kills the controller 8 times, including right
  after the simulator accepted a change we hadn't recorded yet. Result: no double applies, no lost approvals, the same
  decisions as a clean run. No new infra; state is one SQLite file, Postgres in prod. One load-scale day takes ~35s
  of controller time (~60s wall), so ~10× headroom on your 10-minute budget. `controller health` shows lag, decisions
  per hour and credits. Pages are only for things that need a human now."*

### 04 — Client Ops Supervisor (Anita, Kestrel)
- **Accepted:** your edits are facts. A supervisor `config_changed` pins the edited dims for 24h; after that they
  change only through an approval (never auto-applied, even where your policy would allow it), and the request says
  it touches a dim you edited. Experiments re-base on your choice as the new control. Requests: ≤2 routine per day,
  never before 11:00 (stricter than your policy's 4/day from 10:00), batched into one request. One screen:
  `controller view --workflow ks_cancel` shows what's tested, what's winning, what we need from you, what changed
  today. The "compliance script word for word" failure: our first version did exactly that in testing (a false
  incident shipped the "Strict" prompt). Now we validate the live prompt before acting, prefer a prompt with a
  known-good engagement record, and check engagement after any fix.
- **Pushed back:** (1) **Safety beats the pin.** If your prompt edit raises hallucinations (public_02: your P2
  pushed QA flags from 1.7% to 5.4%), we propose a revert to the last known-good prompt *through your approver*,
  with the evidence. That is not behind your back, but it does undo your change if approved. Such requests may use
  a third, reserved slot. (2) **"Tell me before anything changes on a running campaign."** Your policy permits
  auto-apply on welcome/voice/caller-ID/schedule mid-flight. Those changes appear in "changed today", but we have no
  channel to notify you *before*. That's a product hook, not built.
- **Reply:** *"Anita, agreed: we never overwrite your edits silently. When you change something we leave it alone for 24
  hours and adopt it as the new baseline. At most two routine requests a day, after 11:00, everything batched into
  one. Your screen is one command today, a page next. One exception: if a script change raises hallucinations, we'll
  send your approver a revert with the evidence, which may be a third request that day. We'll never ship a fix that
  kills engagement without checking. Small operational changes your policy auto-approves show in 'changed today';
  advance notice needs a notification hook we haven't built yet."*

### 05 — Product (Tara)
- **Accepted:** separate KPIs everywhere, primary + guardrail per workflow. One cohort object (`cohorts.py`).
  Proposals always carry impact score, diff, affected-lead count and dry run. **Impact score** = clamp(10 × expected
  relative change in the primary KPI + 2 × expected change in the other, 0.05..1); safety fixes ≥0.8. So 0.1 ≈ a 1%
  primary move, and the queue sorts by expected effect. Expired = rejection: the planner rebuilds from fresh data
  with a new proposal id, never resubmits. Only calls that happened count. Winners stay inside their workflow.
- **Min cohort size / window, decided:** a variant needs **≥300 connected calls per arm** to win, segments need
  **≥8% of traffic**, verdicts are daily, the critical z is Bonferroni over arms × Pocock for ~10 looks, and the lift
  must hold in each of the last two days. **Cost:** A/A false-winner rate **3.7%** (test). Power: a 10% lift on a
  ~1,100-connected/day workflow is found within 8 days in **97%** of simulated runs, median day 4, before the
  stability gate and approval latency (test asserts >60%). Segment-specific effects in <15% of traffic usually
  stay undetected in a 10-day scenario.
- **Pushed back:** your three P0s. Caller-ID health is built. The supervisor view is a CLI. **Per-person call time
  is cut:** a returning person has ≤8 past dials (~2 pickups); the signal is mostly the cohort's archetype, which
  cohort timing captures. The lever (`lead_preferred_hour`, ×6/×0.25) is blunt enough to starve hours. It also needs
  a 50k–200k-entry config change per day through governance (an approval per day on KYC).
- **Reply:** *"Tara, held to all your definitions. Minimum: 300 connected calls per variant, daily verdicts, the lift
  must hold two days running. That gives 3.7% false winners; a 10% lift is usually found in ~4 days, plus approval time. Impact score is now
  'expected % move in the primary KPI ×10', so the queue order means something. Of the three P0s: caller-ID health
  shipped, the supervisor view is a command (page next), and per-person call time is out. The per-person signal is
  too thin, cohort timing captures most of it, and it means a giant config approval every day."*

### 06 — CTO (Aryamann)
- **Accepted:** explainable over clever (every change has a `why` trail). Harm-averse rules everywhere, each one
  traceable to a harmful decision we observed and fixed (§12). Credits only where they change a decision: public_01
  used **0**, public_02 **6**. A known-good revert needs no LLM call; a 3-credit validation of the live prompt blocks
  false incidents. Scale: §8. Choosing: §2.
- **Claims we found untrue:** listed under 01 (Kestrel "everything", Northwind KYC auto-apply) and 02 (KYC
  operational latitude). Plus one stakeholder assumption: Product's "per-person call time is the headline" assumes
  per-person signal this data doesn't have.
- **Reply:** *"Five capabilities done properly plus the boundary, audit and crash safety. Cut: per-person timing,
  tenant fairness, a UI. The memos' auto-apply claims were wrong against the policies, and we follow the policies.
  On every scenario we ran: 0–1 harmful changes per run, conclusions up 9–29%, an incident fixed in ~3h for 6
  credits. What breaks first at 10× is single-writer SQLite plus the one-process loop; the fix is partitioning by
  workflow (§8). The doc for whoever's on call is docs/CONTROLLER.md."*

## 4. Architecture

```
        simulator (HTTP)                                   controller process (one writer)
 ┌───────────────────────┐   GET chunks (streamed)   ┌────────────────────────────────────────────────────────────┐
 │ /events/chunks/{n}    │ ────────────────────────▶ │ Ingestor ── one txn per chunk ──▶ agg_eng / agg_pick /       │
 │ /sim/advance          │ ◀── POST advance 15min ── │   lead→feature join (memory+leads)  agg_kpi / calls / leads │
 │ /workflows/{id}/config│ ◀── GET on version change │   config_changed → sim_events ──▶ cursor-driven handler      │
 │ /approvals ...        │                           │                                       (pins, re-base)        │
 │ /validation /llm      │                           │ per workflow, per tick:                                       │
 └───────────────────────┘                           │   callerid.step   schedule.step(07:00)  guardrail.check(08:00)│
           ▲   ▲                                     │   incidents.detect/step   experiments.look(daily / 2-hourly)  │
           │   │ POST /approvals, POST apply,        │        │ each writes its desired slice + audit rows to SQLite │
           │   └─ PUT config (auto)                  │        ▼                                                      │
           │                                         │   Planner.build ─ desired config on top of the live config    │
           └──────────── Governance ◀────────────────│   Planner.act  ─ approval part (one batched request, timed)   │
              write-ahead intents: submitting/       │                └ auto part (stepped ≤ max_shift, cooldown)    │
              applying/auto_applying, recover()      │   metrics + alerts                                            │
                                                     └────────────────────────────────────────────────────────────┘
 state: state/<scenario>.db (WAL) — meta(cursors), aggregates, leads, calls, config_versions, proposals, kv
        (component state), decisions (audit), incidents, alerts, metrics
```

**Restart:** `start()` reloads workflows and live configs, then `Governance.recover()` resolves every write-ahead
intent against the simulator's own record (`GET /approvals` by proposal_id, `GET .../config/history` by proposal_id)
*before* anything else. Next it continues ingesting from `meta.last_chunk`, handles `config_changed` from
`meta.cfg_events_handled_upto`, and resumes the loop at whatever minute the world is at. Component state is in `kv`,
so nothing is learned twice or forgotten.

## 5. The learning loop

- **Cohorts.** One `CohortPlan`: up to two split features, cohorts = cross product (≤20). The schedule picks the first
  feature from history (strongest feature × hour-of-day interaction; age_band on all public workflows, found from
  data). An experiment adds a second only when a segment effect is significant after correction. Disjoint and
  exhaustive cohorts mean first-match order cannot disagree with our own mapping. Segments must carry ≥8% of
  connected calls. Cohorts multiply only when evidence demands it, which avoids fragmenting traffic.
- **Experiment design.** The dialler samples welcome and voice independently per call, so the design is factorial
  for free. We test **main effects** per dim, balanced over the other dim; interactions are not modelled. Allocation
  is staged, not a bandit: an untested arm gets **10%** (a 10%-worse arm costs ≤1% engagement), a promising one
  (z ≥ 1.28, still visible in the last 24h) **25%**, control keeps ≥45%. Arms are dropped on harm (z ≤ −2.33,
  cumulative *or* last 48h), on futility (95% upper bound < +2%), or de-ramped when their effect fades. Approval
  workflows move one batched step per day. Kestrel auto-applies the same steps under max_shift 0.2.
- **Deciding a winner.** A deterministic frequentist test. The estimator is a stratified difference
  (day × config_version × prompt, CMH weights), so allocation changes and prompt drift cannot Simpson-bias it (test).
  Promote if n ≥ 300 per arm, lift ≥ 2%, z ≥ z_crit, and the lower bound > 0. z_crit = Φ⁻¹(1 − 0.05/(m × 4)):
  Bonferroni over the m arms compared, ×4 for ~10 daily looks (Pocock). The **stability gate**: the lift must hold
  in each of the last two days, and the last day must keep ≥ half the cumulative lift. False-winner rate measured by
  A/A simulation: **3.7%**. Segment tests are Bonferroni over every (feature value × arm) cell. A segment win ramps
  the arm in that segment; it does not promote it.
- **Decay and rollback.** After promotion the old control keeps a **15% holdback**. Every 2h we compare winner vs
  holdback over the last 48h: roll back if the lift fell vs the lift we promoted on (z ≥ 2.58 and lift now <1%), or
  is significantly negative (z ≤ −2.33). We need ≥300 holdback calls first, because one day of holdback produced a
  false rollback in testing. A promotion still waiting for approval is re-checked and **withdrawn** if it fades. An
  approval whose dim was rolled back since is **superseded**, not applied. public_01: W3 was never promoted
  (correctly; its effect was gone by the time evidence would have been enough). public_02: a decayed ramp left within
  ~3 days.
- **Call time and caller ID.** History gives a (feature value × hour) pickup table. Weights are interaction ratios
  (shrunk with 200 pseudo-dials, squared, clipped to [0.15, 6]), so every cohort keeps some traffic in every hour:
  that is the exploration. We change *who* is called in an hour, never *how many*, and never favour cohorts that
  simply pick up more. That would raise pickup rate by starving hard-to-reach people, which is a cadence decision
  owned by the business. Caller ID: an O/E ratio vs peer numbers in the same hours. ≥30 expected pickups before any
  verdict. Bad = O/E ≤0.75 and z ≤ −3. Healthy numbers share evenly; up to 2 unknown numbers are explored at 4%. Bad
  verdicts are split into *overload* (the number carried ≥1.5× its peers' share: rested 48h, re-probed on fresh calls
  only) and *flagged* (out; page the carrier check).
- **Guardrail.** Welcome/voice cannot move pickup (they are heard after pickup), so their guardrail is their own
  concurrent control. Schedule changes alter *who* picks up, so can shift the engagement mix. The daily guardrail
  monitor compares 48h KPIs with the history baseline, and a breach (drop > max_drop, z ≤ −3) suspends our
  call-time weights for 72h. The planner also refuses any routine proposal whose own estimate of the primary KPI is
  negative.

## 6. Governance

```
approval:  submitting ─▶ pending ─▶ approved ─▶ applying ─▶ applied
              │            │           ├─ dim rolled back since ─▶ superseded  (not applied; re-proposed fresh)
              │            │           └─ apply 409 stale ───────▶ stale       (supervisor edit; re-proposed on new base)
              │            │              apply 410 ─────────────▶ lapsed      (re-proposed)
              │            ├─▶ rejected (2h back-off, re-proposed with fresh data)
              │            └─▶ expired  (= rejection, per Product)
              └─ 4xx ─▶ failed
auto:      auto_applying ─▶ applied | failed (403 reasons recorded)
```

- **Batching and timing.** At most one open request per workflow, carrying every pending change. It is sent inside
  approver hours, only when the worst-case latency still yields a decision before TTL (and same-day for routine
  work), within `max_requests_per_24h − 1` (one slot reserved for incidents) and within tenant preferences. Approved
  changes are applied on the next 15-min tick, well inside the 2h apply window: **0 lapsed** in every run.
- **Auto-apply.** Used for whatever the policy permits (dims ⊆ `allowed_dims`), stepped so every step's TV shift ≤
  `max_shift`, 30 min apart (immediately for safety). We never auto-apply cohorts or prompts, even where a policy
  might allow it. While a request is pending, routine auto-applies wait so they don't stale it.
- **Impact, affected leads, dry run.** Impact as in §3 Product. Affected leads = active, not-yet-concluded leads
  still within cadence whose effective experience (cohort's welcome/voice/caller distribution, hour table, prompt)
  differs between live and proposed configs. Dry run = replay of the last 7 days of connected calls through the
  proposed cohorts, priced with measured arm lifts and pickup tables (point estimates; unmeasured options count 0).
  The schedule part is an IPF replay holding hour capacity and cohort volume fixed. Honest but uncalibrated.
- **Supervisor edits mid-experiment.** Edited dims are pinned 24h, and are approval-only for the rest of the run
  (never auto-applied over a supervisor, even where policy permits auto-apply). The experiment re-bases (the supervisor's choice
  becomes the control; harm-dropped arms stay dropped). Open approvals on the old version fail on apply (`stale`) and
  are rebuilt on the new base. public_02: the supervisor set voice V2 at D5 15:00. The voice experiment re-based,
  resumed after 24h, and later dropped V4/V1 against V2 on evidence.

## 7. Incidents

**Detection:** a Poisson CUSUM on QA flags per connected call, hourly, since the current prompt went live:
k = 2.5 × reference, h = 8. A prompt live <48h *without* clean history here is "fresh": h = 6, and h = 3 if its
up-front validation (3 credits) already failed the client's own approval bar. Sized by simulation
(`scripts/cusum_sizing.py`): **≈0.3–0.5% false incidents per workflow per 10-day run**, median delay ≈3h for a 3.6×
spike. **Confirmation:** before any change, the live prompt is validated (or the prevalidation reused). If it
validates clean, a prompt switch can't fix the spike: we ticket and change nothing. **Fix choice:** a prompt this
workflow ran cleanly (known engagement) first, LLM suggestions only if none exists. Unknown prompts are validated on
n=800 with a +0.01 margin. **Credits:** cap 30 per incident, never below 10% of the budget except for a known-good
revert. **Verify:** ≥300 calls on the fix; resolved if flags aren't significantly above reference, failed if they
are; engagement checked against pre-incident calls.

**Timeline, public_02 (from our audit store):**
```
D2 12:00 caller_id  D2 picks up at 54% of peers (O=65 vs E=121, z=-4.38), 33% of dials → flagged
D2 12:00 governance auto-applied v8: {D1 43%, D2 13%, D3 43%}   (max_shift 0.2 step)
D2 12:15 governance auto-applied v9: {D1 50%, D3 50%}           → flagged number at 0% from D3 on
D3 11:45 supervisor sets prompt P2 (v12); prompt dim pinned 24h
D3 11:45 prevalidation: P2 has no clean history → validation 0.937 < 0.97 bar → monitoring tightened
D3 13:00 hallucination_detected: 9/168 flagged (5.4% vs 1.7%), CUSUM 4.0 ≥ 3.0
D3 13:00 confirmed (P2 fails validation); P1 = known-good (20,448 calls, flags 1.5%) → validation 0.990
D3 13:00 approval apr_00006 submitted (incident slot) with validation_run_id + P1's call history
D3 14:14 approved → D3 14:30 applied v13 (P2 → P1)
D3 18:30 resolved: P1 flags 0.9% on 327 calls
```
MTTR (grader) **180 min**: 75 min detection, 74 min approver latency, the rest tick granularity. Credits: 6. No
LLM call was needed.

## 8. Scale

Measured with `scripts/load_test.sh` on public_03 workflows/volumes (12 workflows × 4,500 dials/h). Run with 3
history days + 2 live days instead of 7 + 5, because the full run (~4.5 GB of simulator output) did not fit on the
disk of the machine used. Per-day figures are unaffected.

| | |
|---|---|
| live ingest | **25–31k events/s** across runs (history backfill ~115k/s: bigger chunks) |
| controller time per simulated day | **30–35s** across runs (ingest 25–33s, decide 2–3s) → wall incl. simulator ≈52–60s/day |
| decision lag | 0 sim-min: every tick ingests to the head before deciding |
| worst decide tick | 0.42s for 12 workflows |
| peak RSS / state | 410 MB / 249 MB per 2 days (slim call log ≈ 200k rows/day) |
| hidden-truth verdicts | 50 changes: 32 beneficial, 18 neutral, 0 harmful |

**What breaks first at 10×** (8.5M events/day, ~120 workflows): (1) **single-writer ingest**: 25–33s/day becomes
~5 min/day; still inside a 12h window but no longer 15-min fresh during peaks (~600k events/h). (2) The **decide
loop is linear in workflows** (~35 ms each per tick): 4s per tick at 120. (3) **SQLite file size** (~1.2 GB/day with
the call log). (4) The in-memory lead join (~8M live leads ≈ 1.5 GB). **Partitioning:** shard by `workflow_id`. Every
aggregate, experiment, proposal and audit row is workflow-keyed, and nothing crosses workflows except C15 budgets.
N controller processes each own a workflow range with its own cursor (in prod: a Kafka consumer group keyed by
workflow; Postgres schema per shard or a `wf` partition key). Approver batching per tenant needs one small
coordinator. Lead joins move to a keyed state store (Postgres/Redis) instead of process memory; the slim call log
moves to cheap partitioned storage.

## 9. Reliability

- **Method:** `scripts/crash_test.sh` on public_02. 5 SIGKILLs at random wall times, then 3 deterministic kills via
  `CONTROLLER_CRASH_AFTER=auto|submit|apply`: the process dies right after the simulator accepted the call, before we
  recorded it. Then `scripts/verify_run.py` checks against the simulator.
- **Result:** 8 restarts, 3 intents reconciled, **0 double-applies, 0 lost approvals, 0 status mismatches,
  245,008 call events = 245,008 aggregated dials**, and the controller-made config history is **identical** to an
  uninterrupted run (version, actor, dims, minute) ([reports/crash_test.md](reports/crash_test.md)).
- **Linux:** the same checks inside `python:3.10-slim` capped at 4 CPUs / 7 GB, on both linux/x86_64 and linux/aarch64:
  public_01/02 grade reports byte-identical to macOS, crash test PASS with identical decisions, and `run.sh` starts
  on a python3 with no venv module and no dependencies ([reports/linux_check.md](reports/linux_check.md)).
- **Bugs this test found and fixed:** unhandled `config_changed` events in memory; urgency kept in memory; a re-run
  look taking a second step on the same data; non-canonical config key order making runs irreproducible (the
  dialler samples options in listed order).
- **What can be lost:** a validation/LLM call in flight when killed is re-run (cost 3–8 credits; the API has no way
  to look up past validation runs). Metrics for the interrupted hour.
- **Idempotency keys:** `proposal_id = <wf>.<apr|auto>.<seq>.m<minute>`, seq persisted with the intent. Chunk number
  for ingest. Event id for config events. `last_look_minute` per experiment.
- **4xx/5xx:** transport errors and 5xx retry with capped backoff. 4xx are answers: `stale_base_version` → stale →
  re-propose; `duplicate_proposal_id` → reconcile; `402` → stop spending, page; `403 auto_apply_not_permitted` →
  recorded with reasons; `410` → lapsed. An exception in one workflow's step is caught, paged, and does not stop the
  others.

## 10. Audit & observability

```
$ python -m controller why --db state/public_02_incidents.db --workflow ks_cancel --at "D3 18:00"
ks_cancel at D3 18:00: config v13, applied D3 14:30 by controller_approved — changed ['prompt_version']
  WHY: Safety fix: switch prompt P2 → P1 (hallucination flags 5.4% vs normal 1.7%)
  RATIONALE: 9 of 168 connected calls on P2 were flagged for hallucination (p=2.6e-03 vs this workflow's reference
  rate). P1 passed validation at 0.990 (bar 0.97). It ran here on 20448 connected calls with flag rate 1.5% and
  engagement 50.0%.
  WHO: approved by the workflow's client approver on apr_00006 (decided D3 14:14); proposed D3 13:00 on base v12
  IMPACT 0.95 · affected leads 3582
  DRY RUN: {"method": "validation run + this workflow's own call history for the target prompt", ...}
  EVIDENCE TRAIL (48h before the proposal):
    D2 12:00 [caller_id] did_degraded: D2 picks up at 54% of peer numbers in the same hours (...) — treated as flagged
    D3 11:45 [incident] prevalidation: new prompt P2 has no clean history here: validation pass rate 0.937 (...)
    D3 13:00 [incident] hallucination_detected: prompt P2: 9/168 connected calls flagged (5.4%) vs reference 1.7% (...)
    D3 13:00 [incident] incident_confirmed: P2 fails validation (0.937 < 0.975): confirmed, choosing a fix
    D3 13:00 [incident] fix_chosen: fix: P2 → P1 (known_good; validation 0.990)
  NEXT 24h vs PRIOR 24h: pickup 25.6% → 25.9%, engagement 49.6% → 49.9%, QA flags 2.6% → 2.1%
  NEXT CHANGE: v14 by controller_auto at D4 10:00
  FOLLOW-UPS:
    D3 18:30 [incident] incident_resolved: P1: flag rate 0.9% on 327 calls (reference 1.7%); incident closed
```
Plain SQL works too: `SELECT * FROM decisions WHERE wf='ks_cancel' AND minute BETWEEN 4320 AND 5760 ORDER BY id`.
`controller person --person <id> --day <n>` answers "why did this customer hear this", and `controller view` is the
supervisor screen.

**Page:** caller ID degraded at ≥25% share and not explained by overload; hallucination incident with no validated
fix; credits <20%; ingest lag >60 sim-min; controller exception in a workflow step.
**Ticket (next business day):** hallucination incident opened / unconfirmed (QA detector check); overload verdict;
guardrail breach (schedule already backed out); primary KPI drift; fix with engagement regression.
**Never alert:** experiment verdicts, ramps, routine proposals, approvals/rejections. Those belong to the supervisor
view and the audit log.

## 11. Results

**public_01** ([reports/public_01_dev.md](reports/public_01_dev.md)):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.394 | +15.3% | 0.260 | +6.3% | 0.526 | +6.9% | 34% | 73 (57) |
| nw_kyc | 0.433 | +18.0% | 0.281 | +16.3% | 0.596 | -0.2% | 7% | 28 (21) |
| nw_renewal | 0.452 | +8.6% | 0.281 | +6.4% | 0.548 | +1.2% | 8% | 58 (54) |
| ALL | 0.421 | +13.4% | 0.271 | +8.2% | 0.548 | +3.4% | 18% | 159 (132) |

24 changes: 10 beneficial, 14 neutral, **0 harmful**; 0 credits; 9 approval requests (8 approved, 1 judgement
rejection), 0 lapsed, 0 conflicts.

**public_02** ([reports/public_02_incidents.md](reports/public_02_incidents.md)):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.371 | +57.7% | 0.252 | +39.1% | 0.502 | +7.3% | 38% | 87 (348) |
| nw_kyc | 0.428 | +14.6% | 0.278 | +18.6% | 0.585 | -3.9% | -4% | 33 (29) |
| nw_renewal | 0.479 | +10.6% | 0.295 | +12.5% | 0.542 | -2.3% | 2% | 60 (54) |
| ALL | 0.419 | +26.8% | 0.272 | +23.9% | 0.533 | +0.1% | 20% | 180 (431) |

31 changes: 11 beneficial, 20 neutral, **0 harmful**; MTTR 180 min; flagged caller ID at 0% from the day after it
was flagged (8.6% of that day's dials, mostly before the verdict); 6 credits, 0 LLM calls; 15 approval requests (10
approved, 5 judgement-call rejections re-proposed later), 0 lapsed, 0 apply conflicts.

**Interpretation.**
- *Where we helped:* pickup everywhere. Spreading caller IDs off overloaded numbers is the largest lever (+16–20%
  pickup on nw_kyc, its primary KPI); call timing adds ~1.5–2%. Engagement on ks_cancel via V4 for younger
  segments and W2. ks_cancel in public_02 is mostly the incident response: baseline ran the flagged number and the
  hallucinating prompt for the rest of the run.
- *Where we weren't good enough:* **engagement on nw_renewal / nw_kyc in public_02 is slightly down** (−2.3%, −3.9%).
  Sim truth shows the expected engagement of the people we connected matches the default config's. So this is
  mostly realised noise plus a decayed W3 at a ramp share for ~3 days, not a mix shift from scheduling. But nw_kyc's
  −3.9% (≈ −0.024 absolute) would breach its 0.02 guardrail if the grader compares realised rates. Our guardrail
  monitor did not fire because it requires z ≤ −3 against history. "Engagement gap captured" is low (≈1–8%) on
  Northwind: the effects there are segment-specific (e.g. V2 for Tamil, ~20% of traffic) or decaying, and a 10-day
  run at safe exploration shares rarely gets to significance.
- *Harmful:* none on public_01/02 or the load run. 1 in the 6-run robustness set: the welcome variant whose effect
  fades two days after first exposure got promoted on 2+ days of clean evidence and decayed within ~30h. The decay
  check rolled it back the next day.
- *What I'd change next:* a CUSUM on winner-vs-holdback (faster decay detection); a guardrail test against a
  concurrent schedule holdback instead of history; adaptive exploration toward segments whose pilot data looks
  heterogeneous.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling / bandit allocation | Faster convergence | Each allocation change is a governance event (approvals cost a request; Northwind needs a human). Staged ramps with daily verdicts are auditable and batchable |
| Per-look p-value for hallucinations | Simple | Raised a false incident on a clean scenario and shipped the "Strict" prompt (harmful). CUSUM + confirmation instead |
| Cumulative-only promotion test | Most power | Promoted a decayed novelty effect (harmful). Two-day stability gate |
| Apply whatever is approved | Simple lifecycle | Shipped a variant our own data had rolled back an hour earlier. `superseded` status |
| 2 × 8% caller-ID exploration | Faster verdicts | Harmful on 4 renewal workflows at load scale when one number was flagged. 2 × 4% |
| In-memory cubes + pickle snapshots | Fast analytics | Two recovery paths, unqueryable audit. SQLite aggregates with one txn per chunk |
| Postgres/Redis now | Production parity | SRE: no new infra; the schema ports. SQLite for the assignment |
| Per-person `lead_preferred_hour` | Product P0 | Thin signal, ×6/×0.25 starves hours, daily mega-config approvals. Cohort timing |
| Prioritising high-pickup cohorts | Raises pickup rate | Starves hard-to-reach leads; that is cadence, which the business owns. Timing only |
| LLM-first incident fixes | "Smart" | The known-good revert has real engagement evidence and costs no LLM call |

## 13. Known weaknesses

1. **Fast-decaying variants:** an effect that fades within ~2 days of first exposure can still be promoted (1 harmful
   in 6 runs) or sit at a ramp share for 1–3 days before evidence turns.
2. **Engagement capture is low** on segment-specific effects in small workflows; 10 days at ≤25% shares rarely reach
   significance for a 20%-of-traffic segment.
3. **Guardrail vs realised noise:** the guardrail check compares with history, not a concurrent control. It is slow
   to fire and blind to sub-z=3 drifts (nw_kyc −3.9% in public_02).
4. **Incident MTTR ≈ 2.5–5h** is bounded by Poisson evidence and approver latency; we don't send outside approver
   hours, even for incidents.
5. **Impact scores are uncalibrated** point estimates; affected-lead counts ignore tomorrow's batch.
6. **Validation/LLM calls are not idempotent across a crash** (no lookup API): a kill mid-call can cost a few credits.
7. **Load test ran a shortened public_03** (2 live days) for disk reasons; full run not measured.
8. Caller-ID verdicts are relative; if all numbers degrade together (carrier-wide), only the KPI drift ticket notices.

## 14. If this went to production at Formi next month

1. **Week 1:** move the store to Postgres (same schema, workflow partition key); replace chunk polling with the event
   bus consumer (offset = cursor, same per-batch txn); ship `health` as Prometheus metrics; page routing.
2. **Week 1:** shadow mode on 2–3 real workflows: every proposal built and logged, nothing sent. Compare predicted
   vs realised effects to calibrate impact scores and the dry run.
3. **Week 2:** supervisor view as a read-only page and the Northwind weekly operational digest (same queries).
   Notification hook for Kestrel's "tell me before".
4. **Week 2:** concurrent holdback for schedule changes (proper guardrail test) and a CUSUM decay detector.
5. **Week 2:** go live on one tenant with auto-apply limited to caller ID; widen per tenant after a week of clean
   audit. Partition plan (§8) before the 50th workflow.
