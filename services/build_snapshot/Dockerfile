# Build from the repo root:
#   docker build -f services/build_snapshot/Dockerfile -t build-snapshot .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 publisher
WORKDIR /app
COPY packages/lc_core packages/lc_core
COPY packages/lc_publish packages/lc_publish
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_publish

USER publisher
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "lc_publish.job"]
