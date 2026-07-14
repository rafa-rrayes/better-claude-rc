"""Platform app: accounts + machine registry (M3) + blind relay (M2).

Configuration (env):
  PLATFORM_DATABASE_URL   Postgres DSN. Unset -> accounts disabled and the
                          relay runs in permissive DEV mode (loudly logged).
  PLATFORM_BASE_URL       public origin used in magic links (default: the
                          request's own origin).
  PLATFORM_AUTO_MIGRATE   "0" to skip startup migrations (default: apply).

Serve with WS keepalive so dead legs are reaped (plan §heartbeats):
    uvicorn app.main:app --ws-ping-interval 20 --ws-ping-timeout 25
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from . import metrics, relay
from .db import Db
from .limits import limiter
from .routes_auth import router as auth_router
from .routes_machines import router as machines_router

log = logging.getLogger("bcrc.platform")

# The platform serves the identical PWA from the installed agent package —
# one frontend copy, no drift (plan §repo layout).
WEB_DIR = Path(str(files("server").joinpath("web")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("PLATFORM_DATABASE_URL") \
        or os.environ.get("DATABASE_URL")
    if dsn:
        migrate = os.environ.get("PLATFORM_AUTO_MIGRATE", "1") != "0"
        app.state.db = await Db.connect(dsn, migrate=migrate)
        relay.DB = app.state.db
        log.info("database connected%s", " (migrated)" if migrate else "")
    else:
        app.state.db = None
        relay.DB = None
        log.warning("PLATFORM_DATABASE_URL not set — accounts disabled, "
                    "relay in permissive dev mode")
    try:
        yield
    finally:
        if app.state.db is not None:
            await app.state.db.close()
            relay.DB = None


app = FastAPI(title="bcrc-platform", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    host = request.headers.get("host", "")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        f"connect-src 'self' wss://{host} ws://{host}; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    resp.headers.setdefault("Strict-Transport-Security",
                            "max-age=63072000; includeSubDomains")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


app.include_router(auth_router)
app.include_router(machines_router)
app.include_router(relay.router)


# ---------------- health + observability ----------------

@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
async def readyz(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"ok": True, "db": False}
    try:
        await db.ping()
    except Exception:
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return {"ok": True, "db": True}


@app.get("/metrics")
async def prometheus_metrics():
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/api/env")
async def api_env():
    """Boot-time transport selection for the PWA."""
    return {"mode": "platform"}


# ---------------- static frontend (the agent package's PWA) ----------------

_RESERVED = ("/api/", "/agent/", "/client/", "/healthz", "/readyz", "/metrics")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith(_RESERVED):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
