#!/usr/bin/env bash
# Arm (a): install the always-on agent as a systemd service on a Droplet.
# Run ON the Droplet as root, from a copy of the repo:
#   scp -r agent-duty-cycle root@DROPLET:/root/
#   ssh root@DROPLET 'cd /root/agent-duty-cycle && \
#     NAME=a-main-2pct PORT=8080 DRIVER_IP=1.2.3.4 MODEL_ID=... MODEL_ACCESS_KEY=... \
#     AGENT_TOKEN=$(openssl rand -hex 16) bash arms/droplet/setup.sh'
# One Droplet per agent (so each arm's bill is one line item).
set -euo pipefail
: "${NAME:?}" "${PORT:=8080}" "${DRIVER_IP:?}" "${MODEL_ID:?}" "${MODEL_ACCESS_KEY:?}" "${AGENT_TOKEN:?}"
cd "$(dirname "$0")/../.."

id dcagent >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/dcagent dcagent
install -d -o dcagent /opt/dcagent /var/lib/dcagent/ws-"$NAME"
cp -r core /opt/dcagent/
# The service runs as dcagent: make the code readable whatever modes the
# copy arrived with (B-003: a zip with 0600 files broke this).
chmod -R a+rX /opt/dcagent/core

install -d -m 700 /etc/dcagent
cat > /etc/dcagent/"$NAME".env <<EOF
ARM=droplet
HOST=0.0.0.0
PORT=$PORT
AGENT_ID=$NAME
WORKSPACE=/var/lib/dcagent/ws-$NAME
AGENT_TOKEN=$AGENT_TOKEN
MODEL_BASE_URL=${MODEL_BASE_URL:-https://inference.do-ai.run/v1}
MODEL_ID=$MODEL_ID
MODEL_ACCESS_KEY=$MODEL_ACCESS_KEY
PYTHONUNBUFFERED=1
EOF
chmod 600 /etc/dcagent/"$NAME".env

cat > /etc/systemd/system/dcagent@.service <<'EOF'
[Unit]
Description=duty-cycle experiment agent (%i)
After=network-online.target
[Service]
User=dcagent
WorkingDirectory=/opt/dcagent
EnvironmentFile=/etc/dcagent/%i.env
ExecStart=/usr/bin/python3 -m core.server
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now dcagent@"$NAME"

# Only the driver may reach the agent port.
if command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null
  ufw allow from "$DRIVER_IP" to any port "$PORT" proto tcp >/dev/null
  ufw --force enable >/dev/null
fi
sleep 1
curl -s "http://127.0.0.1:$PORT/health" && echo
echo "agent $NAME listening on :$PORT (token in /etc/dcagent/$NAME.env)"
