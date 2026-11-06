# Build from the repo root:
#   docker build -f services/console/Dockerfile -t owner-console .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 console
WORKDIR /app
COPY packages/lc_core packages/lc_core
COPY services/console services/console
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./services/console

USER console
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "lc_console.server"]
