#!/usr/bin/env bash
# Starts the controller and runs until the scenario finishes. No Docker; needs Python ≥3.11 + requirements.txt.
#   ./run.sh http://127.0.0.1:8700
# State lives in $CONTROLLER_DB (default state/controller.db). Re-running with the same db RESUMES (crash-safe);
# a db that belongs to another scenario is refused, so a fresh scenario needs a fresh db path.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; fi
"$PY" -c "import httpx" 2>/dev/null || "$PY" -m pip install -q -r requirements.txt
export CONTROLLER_DB="${CONTROLLER_DB:-state/controller.db}"
exec "$PY" -m controller run --url "$SIM_URL" --db "$CONTROLLER_DB"
