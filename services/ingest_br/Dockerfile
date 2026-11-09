# Build from the repo root:
#   docker build -f services/ingest_br/Dockerfile -t ingest-br .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 ingest
WORKDIR /app
COPY config/fields.yaml config/fields.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_ingest packages/lc_ingest
COPY services/ingest_br services/ingest_br
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_ingest ./services/ingest_br

USER ingest
ENV PYTHONUNBUFFERED=1 LC_FIELDS_CONFIG=/app/config/fields.yaml
CMD ["python", "-m", "ingest_br.job"]
