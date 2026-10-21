# rAIden Console: the web control plane for a PostgreSQL fleet.
#
#   docker build -t raiden-console .
#   docker run -p 8080:8080 -e RAIDEN_DEMO=1 raiden-console          # simulated fleet, nothing real is contacted
#   docker compose up -d                                             # with a PostgreSQL state store (docker-compose.yml)
#
# The image runs as an unprivileged user and keeps its state in /data (SQLite by default, or RAIDEN_CONSOLE_DB).

# ---- the web UI -------------------------------------------------------------------------------------------------
FROM node:22-alpine AS web
# behind a corporate proxy or air-gapped: --build-arg NPM_REGISTRY=https://your-mirror/npm/
ARG NPM_REGISTRY=
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi && npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- the console ------------------------------------------------------------------------------------------------
FROM python:3.12-slim AS console
# a PyPI mirror: --build-arg PIP_INDEX_URL=https://your-mirror/simple/
ARG PIP_INDEX_URL=
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# the dependencies exactly as pinned and hashed in requirements/console.lock (`make lock`), in their own cached layer
COPY requirements/console.lock requirements/
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} --require-hashes -r requirements/console.lock
COPY pyproject.toml README.md LICENSE ./
COPY raiden/ raiden/
COPY --from=web /web/dist/ raiden/console/static/
RUN pip install ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} --no-deps . && pip check \
 && useradd --uid 10001 --no-create-home --home-dir /data --shell /usr/sbin/nologin raiden \
 && mkdir -p /data && chown raiden:raiden /data

LABEL org.opencontainers.image.title="rAIden Console" \
      org.opencontainers.image.description="The AI on-call DBA for PostgreSQL fleets: monitoring, incidents with per-stage autonomy, routines, chat" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.licenses="MIT"
USER raiden
ENV RAIDEN_CONSOLE_DATA=/data RAIDEN_CONSOLE_HOST=0.0.0.0 RAIDEN_CONSOLE_PORT=8080
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["raiden", "console"]
