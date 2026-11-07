#!/usr/bin/env bash
# Load test (README §3, stakeholders/03): ingest + decide at public_03_load volume; reports wall-clock per
# simulated day, ingest throughput, decision lag, peak memory.
#   scripts/load_test.sh [scenario] [port]       → reports/load_test.md
# Default scenario is devscen/load_short.yaml — public_03_load with history_days 3 and days 2 instead of 7 and 5
# (identical workflows/volumes per day; the shorter run fits the disk of the machine it was measured on).
set -uo pipefail
cd "$(dirname "$0")/.."
SC="${1:-devscen/load_short.yaml}"; PORT="${2:-8799}"; NAME="$(basename "$SC" .yaml)"; PY=.venv/bin/python
mkdir -p logs reports state devscen
if [ "$SC" = "devscen/load_short.yaml" ] && [ ! -f "$SC" ]; then
  sed "s/^days: 5/days: 2/; s/^history_days: 7/history_days: 3/; s/^name: public_03_load/name: load_short/" \
    scenarios/public_03_load.yaml > "$SC"
fi
rm -f "state/$NAME.db" "state/$NAME.db-wal" "state/$NAME.db-shm"
T0=$(date +%s)
"$PY" -m sim serve --scenario "$SC" --run-dir "runs/load_$NAME" --port "$PORT" --force > "logs/sim_$NAME.log" 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 2; done
echo "simulator ready after $(( $(date +%s) - T0 ))s (history generation)"
START=$(date +%s)
/usr/bin/time -l env CONTROLLER_DB="state/$NAME.db" LOG_LEVEL=INFO ./run.sh "http://127.0.0.1:$PORT" \
  2> "logs/controller_$NAME.log" || /usr/bin/time -v env CONTROLLER_DB="state/$NAME.db" ./run.sh "http://127.0.0.1:$PORT" 2>> "logs/controller_$NAME.log"
WALL=$(( $(date +%s) - START ))
"$PY" scripts/load_report.py --db "state/$NAME.db" --log "logs/controller_$NAME.log" --wall "$WALL" \
  --scenario "$SC" --run-dir "runs/load_$NAME" > reports/load_test.md
cat reports/load_test.md
