#!/usr/bin/env bash
# Crash-recovery test (C13): SIGKILL the controller at random wall-clock moments (mid-ingest, mid-decision,
# mid-HTTP), restart it on the same state db, repeat, let it finish, then verify invariants against the
# simulator's own records and compare the config history with an uninterrupted run of the same scenario.
#
#   scripts/crash_test.sh [scenario] [kills] [port]
# Output: reports/crash_test.md (and logs/crash_*.log)
set -uo pipefail
cd "$(dirname "$0")/.."
SC="${1:-scenarios/public_02_incidents.yaml}"; KILLS="${2:-5}"; PORT="${3:-8790}"
NAME="crash_$(basename "$SC" .yaml)"; PY=.venv/bin/python
mkdir -p logs reports state
rm -f "state/$NAME.db"*
"$PY" -m sim serve --scenario "$SC" --run-dir "runs/$NAME" --port "$PORT" --force > "logs/sim_$NAME.log" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 0.5; done
: > "logs/$NAME.log"
echo "# kill log" > "logs/${NAME}_kills.txt"
for i in $(seq 1 "$KILLS"); do
  CONTROLLER_DB="state/$NAME.db" ./run.sh "http://127.0.0.1:$PORT" >> "logs/$NAME.log" 2>&1 &
  C=$!
  DELAY=$("$PY" -c "import random; print(round(random.uniform(2.0, 9.0), 2))")
  sleep "$DELAY"
  if kill -0 "$C" 2>/dev/null; then
    MIN=$(curl -s "http://127.0.0.1:$PORT/sim/state" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['ts'])")
    kill -9 "$C"; wait "$C" 2>/dev/null
    echo "kill $i: SIGKILL after ${DELAY}s wall, world at $MIN" | tee -a "logs/${NAME}_kills.txt"
  else
    echo "kill $i: controller already finished" | tee -a "logs/${NAME}_kills.txt"
  fi
done
# targeted kills at the worst moments: right after the simulator accepted a request, before we recorded it
for POINT in auto submit apply; do
  rm -f "state/.crash_fired_$POINT"
  CONTROLLER_CRASH_AFTER=$POINT CONTROLLER_CRASH_MARKER="state/.crash_fired_$POINT" CONTROLLER_DB="state/$NAME.db" \
    ./run.sh "http://127.0.0.1:$PORT" >> "logs/$NAME.log" 2>&1
  CODE=$?
  if [ -f "state/.crash_fired_$POINT" ]; then
    MIN=$(curl -s "http://127.0.0.1:$PORT/sim/state" | "$PY" -c "import sys,json; print(json.load(sys.stdin)['ts'])")
    echo "injected crash right after a successful '$POINT' call (exit $CODE), world at $MIN" | tee -a "logs/${NAME}_kills.txt"
  else
    echo "no '$POINT' call happened before the scenario ended (exit $CODE)" | tee -a "logs/${NAME}_kills.txt"
  fi
done
CONTROLLER_DB="state/$NAME.db" ./run.sh "http://127.0.0.1:$PORT" >> "logs/$NAME.log" 2>&1
echo "final run exit code: $?" | tee -a "logs/${NAME}_kills.txt"
"$PY" scripts/verify_run.py --url "http://127.0.0.1:$PORT" --db "state/$NAME.db" \
  --reference-db "state/$(basename "$SC" .yaml).db" --kills "logs/${NAME}_kills.txt" > reports/crash_test.md
cat reports/crash_test.md
