# Build from the repo root:
#   docker build -f services/ingest_browser/Dockerfile -t ingest-browser .
# Chromium and its system libraries are installed at build time (Playwright), so the running job never
# downloads code; every request it makes at run time goes through the conduct gateway (lc_browser).
# Debian 12 (bookworm): a base Playwright's Chromium supports; pinned so a new default base can't break it.
FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 10001 ingest
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir "playwright==1.60.0" \
 && python -m playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright
COPY config/sources.yaml config/sources.yaml
COPY config/fields.yaml config/fields.yaml
COPY config/cycle.yaml config/cycle.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_ingest packages/lc_ingest
COPY packages/lc_browser packages/lc_browser
COPY services/ingest_browser services/ingest_browser
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_ingest ./packages/lc_browser ./services/ingest_browser

USER ingest
ENV PYTHONUNBUFFERED=1 LC_FIELDS_CONFIG=/app/config/fields.yaml
ENV LC_SOURCES_CONFIG=/app/config/sources.yaml LC_CYCLE_CONFIG=/app/config/cycle.yaml
CMD ["python", "-m", "ingest_browser.job"]
