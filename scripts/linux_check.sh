#!/usr/bin/env bash
# Runs INSIDE a Linux container (python:3.10-slim) on a `git archive` of the repo. Driven by:
#   git archive HEAD | docker run --rm -i --cpus 4 --memory 7g -v "$PWD/reports/linux:/out" python:3.10-slim \
#     bash -c 'mkdir /work && cd /work && tar -x && bash scripts/linux_check.sh grader'
# Modes: grader   — deps installed system-wide (as for running the sim), public_01 + public_02 end to end,
#                   graded, then a SIGKILL/restart test verified against the simulator
#        fallback — a python3 with NO venv module and NO deps: run.sh must still start on its own
set -uo pipefail
MODE="${1:-grader}"
OUT=/out; mkdir -p "$OUT"
log() { echo "$*" | tee -a "$OUT/$MODE.txt"; }
wait_up() { python3 - "$1" <<'PY'
import sys, time, urllib.request
for _ in range(600):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2); sys.exit(0)
    except Exception:
        time.sleep(0.5)
sys.exit(1)
PY
}
: > "$OUT/$MODE.txt"
log "## $MODE — $(uname -srm), $(python3 --version), nproc=$(nproc), mem=$(awk '/MemTotal/{print int($2/1024)" MB"}' /proc/meminfo)"
if [ "$MODE" = grader ]; then
  pip install -q --root-user-action=ignore -r requirements.txt 2>&1 | grep -v notice
  for sc in public_01_dev public_02_incidents; do
    python3 -m sim baseline --scenario scenarios/$sc.yaml --run-dir runs/base_$sc > /dev/null
    python3 -m sim serve --scenario scenarios/$sc.yaml --run-dir runs/mine_$sc --port 8700 > /dev/null 2>&1 &
    SIM=$!; wait_up 8700
    T0=$(date +%s); CONTROLLER_DB=state/$sc.db ./run.sh http://127.0.0.1:8700 2> "$OUT/controller_$sc.log"; RC=$?
    log "$sc: run.sh exit $RC in $(( $(date +%s) - T0 ))s; exceptions in log: $(grep -ciE 'traceback|exception' "$OUT/controller_$sc.log")"
    kill $SIM; wait $SIM 2>/dev/null
    python3 -m sim grade --run runs/mine_$sc --baseline runs/base_$sc --out "$OUT/$sc.md" > /dev/null
    if diff -q <(sed -n '5,200p' "$OUT/$sc.md") <(sed -n '5,200p' reports/$sc.md) > /dev/null; then
      log "$sc: grade report IDENTICAL to the committed reports/$sc.md (macOS)"
    else
      log "$sc: grade report differs from macOS — see $sc.md"; sed -n 5,10p "$OUT/$sc.md" | tee -a "$OUT/$MODE.txt"
    fi
    rm -rf runs/mine_$sc runs/base_$sc
  done
  # crash test: SIGKILL at random moments, restart on the same db, verify against the sim + the clean run above
  python3 -m sim serve --scenario scenarios/public_02_incidents.yaml --run-dir runs/crash --port 8701 > /dev/null 2>&1 &
  SIM=$!; wait_up 8701
  for i in 1 2 3 4; do
    CONTROLLER_DB=state/crash.db ./run.sh http://127.0.0.1:8701 2>> "$OUT/controller_crash.log" &
    C=$!; D=$(python3 -c "import random; print(round(random.uniform(3, 10), 1))"); sleep "$D"
    if kill -0 $C 2>/dev/null; then kill -9 $C; wait $C 2>/dev/null; log "crash: SIGKILL $i after ${D}s"; fi
  done
  CONTROLLER_DB=state/crash.db ./run.sh http://127.0.0.1:8701 2>> "$OUT/controller_crash.log"; log "crash: final run exit $?"
  python3 scripts/verify_run.py --url http://127.0.0.1:8701 --db state/crash.db --reference-db state/public_02_incidents.db \
    > "$OUT/crash_verify.md"
  grep -E "applied twice|missing from our store|disagrees|exactly once|identical|different|RESULT" "$OUT/crash_verify.md" | tee -a "$OUT/$MODE.txt"
  kill $SIM
else
  # the sim gets its own environment; the system python3 has neither the deps nor the venv module
  python3 -m venv /simenv && /simenv/bin/pip install -q -r requirements.txt 2>&1 | grep -v notice
  rm -rf "$(python3 -c 'import ensurepip, os; print(os.path.dirname(ensurepip.__file__))')"
  python3 -m venv /tmp/probe > /dev/null 2>&1 && log "venv still works?!" || log "system python3: venv module broken (as on Ubuntu without python3-venv)"
  python3 -c "import httpx" 2>/dev/null && log "httpx present?!" || log "system python3: no httpx"
  /simenv/bin/python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/fb --port 8702 > /dev/null 2>&1 &
  SIM=$!; wait_up 8702
  CONTROLLER_DB=state/fb.db ./run.sh http://127.0.0.1:8702 2> "$OUT/controller_fallback.log"; RC=$?
  log "fallback: run.sh exit $RC; finished: $(grep -c 'scenario finished' "$OUT/controller_fallback.log"); .venv created: $([ -d .venv ] && echo yes || echo no); deps installed with --user: $(python3 -c 'import httpx, site; print(httpx.__file__.startswith(site.getuserbase()))' 2>/dev/null)"
  kill $SIM
fi
