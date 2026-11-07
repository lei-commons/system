# Build from the repo root:
#   docker build -f services/site/Dockerfile -t lei-commons-site .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 site
WORKDIR /app
COPY packages/lc_core packages/lc_core
COPY services/site services/site
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./services/site

USER site
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "lc_site.server"]
