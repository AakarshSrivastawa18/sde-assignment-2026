# Linux check

`scripts/linux_check.sh` inside `python:3.10-slim` (Debian), `docker run --cpus 4 --memory 7g`, on a `git archive` of the
committed tree. Host: Docker Desktop on Apple Silicon. Run twice: **linux/aarch64** (native) and **linux/x86_64** (`--platform linux/amd64`, emulated — hence ~2× slower).
`nproc`/`MemTotal` show the Docker VM (8 CPUs, 7.75 GB); the container itself was capped by cgroups at 4 CPUs / 7 GB.
On both architectures the grade reports are byte-identical to the macOS ones in `reports/` (raw outputs in `reports/linux/` and `reports/linux_amd64/`).

## aarch64 — grader-style (requirements installed system-wide, as for running the simulator)

```
## grader — Linux 7.0.14-linuxkit aarch64, Python 3.10.22, nproc=8, mem=7934 MB
public_01_dev: run.sh exit 0 in 19s; exceptions in log: 0
public_01_dev: grade report IDENTICAL to the committed reports/public_01_dev.md (macOS)
public_02_incidents: run.sh exit 0 in 24s; exceptions in log: 0
public_02_incidents: grade report IDENTICAL to the committed reports/public_02_incidents.md (macOS)
crash: SIGKILL 1 after 5.6s
crash: SIGKILL 2 after 4.7s
crash: SIGKILL 3 after 4.5s
crash: SIGKILL 4 after 4.1s
crash: final run exit 0
- changes applied twice: **0** 
- approvals known to the simulator but missing from our store: **0** 
- approvals whose status disagrees: **0** 
- call events in the simulator's chunks: 245008; dials in our aggregates: 245008 → **exactly once**
- controller-made config versions: crashed run 31, uninterrupted run 31 → **identical (version, actor, dims, minute)**
**RESULT: PASS**
```

## aarch64 — run.sh fallback (python3 with no venv module and no dependencies)

```
## fallback — Linux 7.0.14-linuxkit aarch64, Python 3.10.22, nproc=8, mem=7934 MB
system python3: venv module broken (as on Ubuntu without python3-venv)
system python3: no httpx
fallback: run.sh exit 0; finished: 1; .venv created: no; deps installed with --user: True
```

## x86_64 — grader-style

```
## grader — Linux 7.0.14-linuxkit x86_64, Python 3.10.22, nproc=8, mem=7934 MB
public_01_dev: run.sh exit 0 in 40s; exceptions in log: 0
public_01_dev: grade report IDENTICAL to the committed reports/public_01_dev.md (macOS)
public_02_incidents: run.sh exit 0 in 48s; exceptions in log: 0
public_02_incidents: grade report IDENTICAL to the committed reports/public_02_incidents.md (macOS)
crash: SIGKILL 1 after 5.8s
crash: SIGKILL 2 after 4.1s
crash: SIGKILL 3 after 6.4s
crash: SIGKILL 4 after 5.9s
crash: final run exit 0
- changes applied twice: **0** 
- approvals known to the simulator but missing from our store: **0** 
- approvals whose status disagrees: **0** 
- call events in the simulator's chunks: 245008; dials in our aggregates: 245008 → **exactly once**
- controller-made config versions: crashed run 31, uninterrupted run 31 → **identical (version, actor, dims, minute)**
**RESULT: PASS**
```

## x86_64 — run.sh fallback

```
## fallback — Linux 7.0.14-linuxkit x86_64, Python 3.10.22, nproc=8, mem=7934 MB
system python3: venv module broken (as on Ubuntu without python3-venv)
system python3: no httpx
fallback: run.sh exit 0; finished: 1; .venv created: no; deps installed with --user: True
```
