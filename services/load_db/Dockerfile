# Build from the repo root:
#   docker build -f services/load_db/Dockerfile -t load-db .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 loader
WORKDIR /app
COPY packages/lc_core packages/lc_core
COPY packages/lc_directory packages/lc_directory
COPY services/load_db services/load_db
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_directory ./services/load_db

USER loader
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "load_db.job"]
