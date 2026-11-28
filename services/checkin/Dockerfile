# Build from the repo root:
#   docker build -f services/checkin/Dockerfile -t checkin .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 checkin
WORKDIR /app
COPY config/sources.yaml config/sources.yaml
COPY config/cycle.yaml config/cycle.yaml
COPY packages/lc_core packages/lc_core
COPY services/checkin services/checkin
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./services/checkin

USER checkin
ENV PYTHONUNBUFFERED=1
ENV LC_SOURCES_CONFIG=/app/config/sources.yaml LC_CYCLE_CONFIG=/app/config/cycle.yaml
ENTRYPOINT ["python", "-m", "checkin.job"]
