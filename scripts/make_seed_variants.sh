#!/usr/bin/env bash
# Developer-only: seed variants of the public scenarios (same truth, different random draws) used for
# reports/robustness.md, plus the shortened load scenario. The controller never reads these files.
cd "$(dirname "$0")/.."
mkdir -p devscen
for sd in 102 103; do sed "s/^seed: 101/seed: $sd/; s/^name: public_01_dev/name: dev01_s$sd/" scenarios/public_01_dev.yaml > devscen/dev01_s$sd.yaml; done
for sd in 203 204; do sed "s/^seed: 202/seed: $sd/; s/^name: public_02_incidents/name: dev02_s$sd/" scenarios/public_02_incidents.yaml > devscen/dev02_s$sd.yaml; done
sed "s/^days: 5/days: 2/; s/^history_days: 7/history_days: 3/; s/^name: public_03_load/name: load_short/" scenarios/public_03_load.yaml > devscen/load_short.yaml
ls devscen
