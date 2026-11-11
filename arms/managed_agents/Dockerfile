# Custom-mode image for arm (c). Only needed if your team can use `agent: custom`.
#   docker build -f arms/managed_agents/Dockerfile -t registry.digitalocean.com/<reg>/dcagent:1 .
#   docker push registry.digitalocean.com/<reg>/dcagent:1
#   then pass the digest-pinned ref:  --image registry.digitalocean.com/<reg>/dcagent@sha256:...
FROM python:3.12-slim
WORKDIR /opt/dcagent
COPY core/ /opt/dcagent/core/
ENV ARM=managed_agents HOST=127.0.0.1 PORT=8080 WORKSPACE=/workspace/agent-ws \
    PYTHONUNBUFFERED=1
CMD ["python3", "-m", "core.server"]
