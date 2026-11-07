#!/usr/bin/env bash
# Developer evaluation: run the controller on every scenario given, in parallel, and print one summary line each.
#   scripts/eval_all.sh scenarios/public_01_dev.yaml scenarios/public_02_incidents.yaml devscen/*.yaml
cd "$(dirname "$0")/.."
port=8720
for sc in "$@"; do
  port=$((port+1))
  ( scripts/run_scenario.sh "$sc" "$port" > /dev/null 2>&1 ) &
done
wait
for sc in "$@"; do
  n=$(basename "$sc" .yaml)
  .venv/bin/python - "$n" <<'PY'
import json, sys, subprocess
n = sys.argv[1]
g = json.loads(subprocess.check_output([".venv/bin/python", "-m", "sim", "grade", "--run", f"runs/mine_{n}",
                                        "--baseline", f"runs/base_{n}", "--json"]))
d = g["delta_vs_baseline"]
v = g["controller_changes"]["verdicts"]
inc = [i.get("mttr_minutes") for i in g["incidents"] if "mttr_minutes" in i]
per = " ".join(f"{w}:c{d[w]['conclusion_rate']*100:+.0f}/p{d[w]['pickup_rate']*100:+.0f}/e{d[w]['engagement_rate']*100:+.1f}"
               for w in d if w != "ALL")
print(f"{n:22s} ALL c{d['ALL']['conclusion_rate']*100:+.1f}% p{d['ALL']['pickup_rate']*100:+.1f}% "
      f"e{d['ALL']['engagement_rate']*100:+.1f}% | harmful={v.get('harmful',0)} benef={v.get('beneficial',0)} "
      f"neutral={v.get('neutral',0)} mttr={inc} credits={g['credits_used']:.0f} | {per}")
PY
done
