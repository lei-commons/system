# Build from the repo root:
#   docker build -f services/ingest_bulk/Dockerfile -t ingest-bulk .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 ingest
WORKDIR /app
COPY config/sources.yaml config/sources.yaml
COPY config/fields.yaml config/fields.yaml
# A source on trial measures its status words against the status map (lc_ingest.quality, docs/source-trials.md).
COPY config/status.yaml config/status.yaml
COPY config/labels.yaml config/labels.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_ingest packages/lc_ingest
COPY services/ingest_bulk services/ingest_bulk
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_ingest ./services/ingest_bulk

USER ingest
ENV PYTHONUNBUFFERED=1 LC_FIELDS_CONFIG=/app/config/fields.yaml
ENV LC_SOURCES_CONFIG=/app/config/sources.yaml LC_STATUS_CONFIG=/app/config/status.yaml LC_LABELS_CONFIG=/app/config/labels.yaml
CMD ["python", "-m", "ingest_bulk.job"]
