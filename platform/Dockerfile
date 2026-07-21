# bcrc-platform — hosted relay + accounts.
# Build context is the REPO ROOT (uv workspace):
#   docker build -f platform/Dockerfile .
# Dokploy: buildType dockerfile, dockerfile platform/Dockerfile, context .

FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src

# Dependency layer: lockfile + workspace manifests only, so source edits
# don't bust the (slow) third-party install.
COPY pyproject.toml uv.lock ./
COPY platform/pyproject.toml platform/
RUN uv sync --frozen --no-dev --no-install-workspace --package bcrc-platform

# Workspace members installed as real wheels (--no-editable) so
# importlib.resources finds server/web inside site-packages (app.main).
COPY README.md LICENSE ./
COPY server/ server/
COPY platform/ platform/
RUN uv sync --frozen --no-dev --no-editable --package bcrc-platform

# The agent wheel the platform self-hosts at /dist/ for the curl-pipe
# installer (GET /i/<token>) — scoped --package, NEVER a bare `uv build`
# (that would also build the private bcrc-platform wheel).
RUN uv build --package better-claude-rc --wheel --out-dir /dist


FROM python:3.14-slim
RUN useradd --create-home --uid 1000 bcrc
COPY --from=build /src/.venv /src/.venv
COPY --from=build /dist /app/dist
ENV PATH="/src/.venv/bin:$PATH" \
    PLATFORM_ENV=production \
    PLATFORM_DIST_DIR=/app/dist \
    PYTHONUNBUFFERED=1
USER bcrc
EXPOSE 8500
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", \
       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8500/healthz', timeout=4)"]

# ONE worker, always: the relay Router and live quota counters are
# process-local by design. Never add --workers >1 — scale the box instead.
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8500", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "25", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--timeout-graceful-shutdown", "10"]
