# Load test

Scenario `devscen/load_short.yaml` — 12 workflows. Measured on this machine (Apple Silicon laptop, 8 cores), controller and simulator on the same host.

| measure | value |
|---|---|
| history backfill (pre-D0) | 2,177,483 events in 18s → **118,341 events/s** |
| live days | 2 |
| live dials ingested | 1,296,000 |
| live events ingested | 1,680,093 in 66s → **25,361 events/s** |
| controller time per simulated day (ingest + decide) | **35s** |
| decide time, worst tick | 0.40s |
| decision lag (sim minutes between the world and our ingested state, worst) | 0 |
| wall-clock for the whole run incl. simulator stepping | 119s |
| peak controller RSS | 410 MB |
| state db size | 249 MB |
| audit decisions written / changes applied | 282 / 50 |

Per simulated day (from the controller's own `metrics` table):

| day | events | ingest s | decide s |
|---|---|---|---|
| D0 | 840,057 | 26 | 2 |
| D1 | 840,036 | 40 | 3 |

Hidden-truth verdicts of the 50 changes applied in this run (grader's rule, computed from the sim's truth file because a baseline run did not fit on disk): beneficial 32, neutral 18
