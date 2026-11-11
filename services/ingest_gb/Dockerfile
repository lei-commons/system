# Build from the repo root:
#   docker build -f services/ingest_gb/Dockerfile -t ingest-gb .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 ingest
WORKDIR /app
COPY config/sources.yaml config/sources.yaml
COPY config/fields.yaml config/fields.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_ingest packages/lc_ingest
COPY services/ingest_gb services/ingest_gb
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_ingest ./services/ingest_gb

USER ingest
ENV PYTHONUNBUFFERED=1 LC_FIELDS_CONFIG=/app/config/fields.yaml
ENV LC_SOURCES_CONFIG=/app/config/sources.yaml
CMD ["python", "-m", "ingest_gb.job"]
