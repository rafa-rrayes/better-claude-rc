"""Platform app: accounts + machine registry (M3) + blind relay (M2).

Configuration (env):
  PLATFORM_DATABASE_URL   Postgres DSN. Unset -> accounts disabled and the
                          relay runs in permissive DEV mode (loudly logged).
  PLATFORM_BASE_URL       public origin used in magic links (default: the
                          request's own origin).
  PLATFORM_AUTO_MIGRATE   "0" to skip startup migrations (default: apply).
  PLATFORM_ENV            "production" (baked into the Docker image) refuses
                          to boot without DB/base-URL/real email configured.
  PLATFORM_METRICS_TOKEN  when set, /metrics requires it as a Bearer token.

Serve with WS keepalive so dead legs are reaped (plan §heartbeats):
    uvicorn app.main:app --ws-ping-interval 20 --ws-ping-timeout 25
"""

from __future__ import annotations

import logging
import os
import secrets
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
from .deps import SESSION_COOKIE
from .emailer import get_sender
from .limits import limiter
from .routes_auth import router as auth_router
from .routes_billing import router as billing_router
from .routes_machines import router as machines_router
from .stripe_webhooks import router as stripe_router

log = logging.getLogger("bcrc.platform")

# uvicorn configures only its own loggers; without a root handler, INFO
# records from bcrc.* (including the dev-mode magic-link emails) are
# silently dropped. No-op if the embedding process configured logging.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")

# The platform serves the identical PWA from the installed agent package —
# one frontend copy, no drift (plan §repo layout).
WEB_DIR = Path(str(files("server").joinpath("web")))
LANDING_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ.get("PLATFORM_DATABASE_URL") \
        or os.environ.get("DATABASE_URL")
    if os.environ.get("PLATFORM_ENV") == "production":
        # Refuse to boot into the permissive dev relay or the log-only email
        # stub — a misconfigured prod deploy must fail loudly, not silently.
        missing = [name for name in ("PLATFORM_BASE_URL",)
                   if not os.environ.get(name)]
        if not dsn:
            missing.append("PLATFORM_DATABASE_URL")
        if missing:
            raise RuntimeError(
                f"PLATFORM_ENV=production but unset: {', '.join(missing)}")
        if os.environ.get("PLATFORM_EMAIL_MODE", "dev") == "dev":
            raise RuntimeError(
                "PLATFORM_ENV=production requires a real PLATFORM_EMAIL_MODE"
                " (dev only logs magic links)")
        get_sender()  # missing RESEND_API_KEY fails boot, not first login
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
        # Close live WS legs (1012 try-again) BEFORE the DB goes away so
        # endpoint finally blocks can still touch machines/audit tables.
        await relay.ROUTER.drain()
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
        # 'wasm-unsafe-eval' lets the vendored libsodium.js compile its
        # (inlined, same-origin) WebAssembly; no remote code is allowed.
        "script-src 'self' 'wasm-unsafe-eval'; "
        "img-src 'self' data:; "
        f"connect-src 'self' wss://{host} ws://{host}; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    resp.headers.setdefault("Strict-Transport-Security",
                            "max-age=63072000; includeSubDomains")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    if not request.url.path.startswith(("/api/", "/agent/", "/client/")):
        # Cloudflare fronts this origin: force edge revalidation so a deploy
        # never serves a stale app.js (ETag 304s keep it cheap).
        resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


app.include_router(auth_router)
app.include_router(machines_router)
app.include_router(billing_router)
app.include_router(stripe_router)
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
async def prometheus_metrics(request: Request):
    want = os.environ.get("PLATFORM_METRICS_TOKEN")
    if want:
        got = request.headers.get("authorization", "")
        if not secrets.compare_digest(got, f"Bearer {want}"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


@app.get("/api/env")
async def api_env():
    """Boot-time transport selection for the PWA."""
    return {"mode": "platform"}


# ---------------- static frontend (landing + the agent package's PWA) -------

_RESERVED = ("/api/", "/agent/", "/client/", "/landing/", "/i/", "/dist/",
             "/healthz", "/readyz", "/metrics")


@app.get("/")
async def index(request: Request):
    """Hosted platform: anonymous visitors get the landing page, a valid
    session cookie gets the PWA. DB-less (local/e2e relay) runs keep
    serving the PWA directly — there is nothing to sign up for."""
    db = getattr(request.app.state, "db", None)
    if db is not None:
        token = request.cookies.get(SESSION_COOKIE)
        user = await db.session_user(token) if token else None
        if user is None:
            return FileResponse(LANDING_DIR / "landing.html")
    return FileResponse(WEB_DIR / "index.html")


@app.get("/app")
async def pwa():
    """The PWA unconditionally — magic links land on /app#auth=… so
    redemption never detours through the landing page."""
    return FileResponse(WEB_DIR / "index.html")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith(_RESERVED):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html")


app.mount("/landing", StaticFiles(directory=str(LANDING_DIR)), name="landing")
app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
