# Build from the repo root:
#   docker build -f services/build_snapshot/Dockerfile -t build-snapshot .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 publisher
WORKDIR /app
COPY config/sources.yaml config/sources.yaml
COPY config/publishing.yaml config/publishing.yaml
COPY config/dedi.yaml config/dedi.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_dedi packages/lc_dedi
COPY packages/lc_publish packages/lc_publish
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_dedi "./packages/lc_publish[dedi]"

USER publisher
ENV PYTHONUNBUFFERED=1
ENV LC_SOURCES_CONFIG=/app/config/sources.yaml LC_PUBLISHING_CONFIG=/app/config/publishing.yaml LC_DEDI_CONFIG=/app/config/dedi.yaml
CMD ["python", "-m", "lc_publish.job"]
