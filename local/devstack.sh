#!/usr/bin/env bash
# Local stand-ins for every cloud dependency, for tests.
#   local/devstack.sh start [state_dir]   # mock inference, fake Functions,
#                                          # fake Harness Runtime, 2 droplet agents
#   local/devstack.sh stop
#   source <(local/devstack.sh env)       # export client env vars
set -u
cd "$(dirname "$0")/.."
SD="${2:-/tmp/dcstack}"
PIDS="$SD/pids"

case "${1:-}" in
start)
  mkdir -p "$SD/ws-a1" "$SD/ws-a2"
  : > "$PIDS"
  python3 local/mock_inference.py --port 9100 --latency "${MOCK_LATENCY:-0.05}" > "$SD/mock.log" 2>&1 & echo $! >> "$PIDS"
  python3 local/fake_functions.py --port 9200 --state-dir "$SD/fn" \
      --keepalive "${FN_KEEPALIVE:-3}" --blocking-window "${FN_WINDOW:-1}" > "$SD/fn.log" 2>&1 & echo $! >> "$PIDS"
  python3 local/fake_harness.py --port 9300 > "$SD/hr.log" 2>&1 & echo $! >> "$PIDS"
  for i in 1 2; do
    ARM=droplet HOST=127.0.0.1 PORT=$((8080 + i)) AGENT_ID="a$i" WORKSPACE="$SD/ws-a$i" \
    AGENT_TOKEN=tok MODEL_BASE_URL=http://127.0.0.1:9100/v1 MODEL_ID=mock MODEL_ACCESS_KEY=k \
      python3 -m core.server > "$SD/a$i.log" 2>&1 & echo $! >> "$PIDS"
  done
  sleep 1
  echo "devstack up in $SD"
  ;;
stop)
  [ -f "$PIDS" ] && xargs kill < "$PIDS" 2>/dev/null || true
  # sandbox daemons: pidfiles under fake sandbox HOMEs, plus Linux /proc fallback
  for pf in /tmp/fake-sbx-*/.fake_pids; do
    [ -f "$pf" ] || continue
    while read -r pid; do
      [ -n "$pid" ] || continue
      kill -CONT "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
    done < "$pf"
  done
  if [ -d /proc ]; then
    for p in /proc/[0-9]*; do
      grep -qa FAKE_SANDBOX= "$p/environ" 2>/dev/null && kill -CONT "${p#/proc/}" 2>/dev/null && kill "${p#/proc/}" 2>/dev/null
    done
  fi
  rm -f "$PIDS"
  echo "devstack stopped"
  ;;
env)
  cat <<EOF
export DO_API_BASE=http://127.0.0.1:9300 DIGITALOCEAN_TOKEN=local
export MODEL_BASE_URL=http://127.0.0.1:9100/v1 MODEL_ID=mock MODEL_ACCESS_KEY=k
export FN_KEY_ID=local FN_KEY_SECRET=local AGENT_TOKEN=tok
EOF
  ;;
*) echo "usage: $0 start|stop|env [state_dir]"; exit 1 ;;
esac
