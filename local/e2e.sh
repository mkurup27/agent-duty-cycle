#!/usr/bin/env bash
# End-to-end rehearsal of experiments 1, 4 and 5 against local stand-ins.
# Half a simulated day per main schedule, compressed ~1440x (about a minute).
#   bash local/e2e.sh [state_dir]
set -euo pipefail
cd "$(dirname "$0")/.."
SD="${1:-/tmp/dcstack}"
rm -rf "$SD"; mkdir -p "$SD/sched"
trap 'bash local/devstack.sh stop "$SD" >/dev/null' EXIT
bash local/devstack.sh start "$SD"
source <(bash local/devstack.sh env)

python3 arms/managed_agents/bootstrap.py up --names c-2pct,c-30pct,c-state \
  --pause-seconds 0.2 > "$SD/bootstrap.log"
grep -E '"process_survived_pause"|"probe_ok"' "$SD/bootstrap.log"

for arm in a b c; do for p in 2pct 30pct; do
  python3 -m driver.schedule main --profile $p --days 0.5 --target $arm-$p \
    --out "$SD/sched/$arm-$p.jsonl" 2>>"$SD/sched/meta.log"
done; done
for arm in b c; do
  python3 -m driver.schedule state --sizes 10240,1048576,10485760 --wakes 5 \
    --gap 60 --seed-gap 60 --target $arm-state --out "$SD/sched/$arm-state.jsonl" \
    2>>"$SD/sched/meta.log"
done

python3 -m driver.run --targets local/targets.local.json --run-dir "$SD/run" \
  --speed 1440 --work-scale 0.0007 --delay-start 1 "$SD"/sched/*.jsonl
python3 -m analysis.analyze "$SD/run" --idle-timeout 900
