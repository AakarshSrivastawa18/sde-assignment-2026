#!/usr/bin/env bash
# Starts the controller and runs until the scenario finishes. No Docker; needs Python ≥3.10.
#   ./run.sh http://127.0.0.1:8700
# Interpreter, first match wins (the system Python is never modified):
#   1. ./.venv if present   2. $PYTHON / python3 if it already has the requirements (e.g. the env running the sim)
#   3. a new ./.venv with requirements.txt   4. pip --user install into python3 (boxes without the venv module)
# State lives in $CONTROLLER_DB (default state/controller.db). Re-running with the same db RESUMES (crash-safe);
# a db that belongs to another scenario is refused, so a fresh scenario needs a fresh db path.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
SYS="${PYTHON:-python3}"
if [ -x .venv/bin/python ] && .venv/bin/python -c "import httpx" 2>/dev/null; then
  PY=.venv/bin/python
elif "$SYS" -c "import httpx" 2>/dev/null; then
  PY="$SYS"
elif "$SYS" -m venv .venv 2>/dev/null && .venv/bin/python -m pip install -q -r requirements.txt; then
  PY=.venv/bin/python
else
  rm -rf .venv
  "$SYS" -m pip install -q --user -r requirements.txt
  PY="$SYS"
fi
export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"  # reproducible set ordering (configs are canonicalised too)
export CONTROLLER_DB="${CONTROLLER_DB:-state/controller.db}"
exec "$PY" -m controller run --url "$SIM_URL" --db "$CONTROLLER_DB"
