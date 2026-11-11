# Build from the repo root:
#   docker build -f services/site/Dockerfile -t lei-commons-site .
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 site
WORKDIR /app
COPY config/sources.yaml config/sources.yaml
COPY packages/lc_core packages/lc_core
COPY packages/lc_directory packages/lc_directory
COPY services/site services/site
RUN pip install --no-cache-dir "./packages/lc_core[gcp]" ./packages/lc_directory ./services/site
# The full-text search extension is fetched at build time and loaded from here,
# so the running service never downloads code.
ENV LC_DUCKDB_EXTENSIONS=/opt/duckdb-extensions
RUN python -c "import duckdb; c = duckdb.connect(); c.execute(\"SET extension_directory='/opt/duckdb-extensions'\"); c.execute('INSTALL fts')" \
 && chmod -R a+rX /opt/duckdb-extensions

USER site
ENV PYTHONUNBUFFERED=1 LC_SOURCES_CONFIG=/app/config/sources.yaml
CMD ["python", "-m", "lc_site.server"]
