# Build from the repo root:
#   docker build -f services/conduct_proxy/Dockerfile -t conduct-gateway .
# The egress allow-list is baked in, so changing it needs a reviewed release.
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 gateway
WORKDIR /app
COPY packages/lc_core packages/lc_core
COPY services/conduct_proxy services/conduct_proxy
COPY config/egress-allowlist.yaml config/egress-allowlist.yaml
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./services/conduct_proxy

USER gateway
ENV LC_EGRESS_CONFIG=/app/config/egress-allowlist.yaml PYTHONUNBUFFERED=1
CMD ["python", "-m", "conduct_proxy.server"]
