# Build from the repo root:
#   docker build -f agents/harness/Dockerfile -t lc-sandbox .
FROM python:3.12-slim

WORKDIR /app
COPY packages/lc_core packages/lc_core
COPY services/conduct_proxy services/conduct_proxy
COPY agents/harness agents/harness
COPY config/sandbox-egress.yaml config/sandbox-egress.yaml
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./services/conduct_proxy "./agents/harness[vertex,test]"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp LC_SANDBOX_EGRESS=/app/config/sandbox-egress.yaml
USER 10001
