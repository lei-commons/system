# Build from the repo root:
#   docker build -f services/web_claims/Dockerfile -t web-claims .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 crawler
WORKDIR /app
COPY config/web_claims.yaml config/web_claims.yaml
COPY config/models.yaml config/models.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_ingest packages/lc_ingest
COPY services/web_claims services/web_claims
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_ingest "./services/web_claims[models]"

USER crawler
ENV PYTHONUNBUFFERED=1 LC_WEB_CLAIMS_CONFIG=/app/config/web_claims.yaml LC_MODELS_CONFIG=/app/config/models.yaml
CMD ["python", "-m", "web_claims.job"]
