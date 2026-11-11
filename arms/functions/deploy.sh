#!/usr/bin/env bash
# Vendor core + the handler source into every action dir, then deploy.
#   ./deploy.sh            # build + deploy
#   ./deploy.sh --build    # build only (inspect packages/agent/*)
set -euo pipefail
cd "$(dirname "$0")"
SRC=packages/agent/handle
ACTIONS="handle_2pct handle_30pct state probe_g1m probe_g15m probe_g1h probe_g6h probe_g24h"

[ -f .env ] || { echo "missing .env (copy .env.example)"; exit 1; }

for a in $ACTIONS; do
  d=packages/agent/$a
  rm -rf "$d"
  mkdir -p "$d"
  cp "$SRC"/__main__.py "$SRC"/state_store.py "$SRC"/db.py "$SRC"/s3mini.py \
     "$SRC"/requirements.txt "$SRC"/build.sh "$d"/
  mkdir -p "$d/core"
  cp ../../core/__init__.py ../../core/agent.py ../../core/model.py \
     ../../core/state.py ../../core/timing.py "$d/core/"
  chmod +x "$d/build.sh"
done

# The template dir itself must not deploy as an action.
mv "$SRC" ./.handle_src
trap 'mv ./.handle_src "$SRC"' EXIT

if [ "${1:-}" = "--build" ]; then
  ls -R packages/agent | head -50
  exit 0
fi
doctl serverless deploy . --remote-build
