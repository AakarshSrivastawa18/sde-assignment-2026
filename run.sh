#!/usr/bin/env bash
# Starts the controller and runs until the scenario finishes. No Docker; needs Python ≥3.11.
#   ./run.sh http://127.0.0.1:8700
# First run creates .venv and installs requirements.txt (system Python is never modified).
# State lives in $CONTROLLER_DB (default state/controller.db). Re-running with the same db RESUMES (crash-safe);
# a db that belongs to another scenario is refused, so a fresh scenario needs a fresh db path.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  "${PYTHON:-python3}" -m venv .venv
  .venv/bin/python -m pip install -q -r requirements.txt
fi
PY=.venv/bin/python
"$PY" -c "import httpx" 2>/dev/null || "$PY" -m pip install -q -r requirements.txt
export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"  # reproducible set ordering (configs are canonicalised too)
export CONTROLLER_DB="${CONTROLLER_DB:-state/controller.db}"
exec "$PY" -m controller run --url "$SIM_URL" --db "$CONTROLLER_DB"
