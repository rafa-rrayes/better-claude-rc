"""Shared FastAPI dependencies: DB handle, session-cookie auth, Origin check.

Session cookie: __Host-bcrc_sess (HttpOnly, Secure, SameSite=Lax), SHA-256
stored in PG, 90d rolling. No tokens in URLs, ever.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException, Request

from .db import Db

SESSION_COOKIE = "__Host-bcrc_sess"


def get_db(request: Request) -> Db:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="accounts not configured")
    return db


async def current_user(request: Request):
    """The session cookie's user row (id, email, plan) or 401."""
    db = get_db(request)
    token = request.cookies.get(SESSION_COOKIE)
    user = await db.session_user(token) if token else None
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def check_origin(request: Request) -> None:
    """Cross-origin unsafe requests are refused (defense in depth on top of
    SameSite=Lax). Absent Origin (curl, agents) passes — CSRF is a browser
    attack and browsers send Origin on cross-site POSTs."""
    origin = request.headers.get("origin")
    if not origin:
        return
    try:
        o = urlparse(origin)
    except ValueError:
        raise HTTPException(status_code=403, detail="bad origin")
    host = request.headers.get("host", "")

    def norm(netloc: str, scheme: str) -> str:
        if ":" in netloc:
            return netloc
        return f"{netloc}:{443 if scheme == 'https' else 80}"

    if o.scheme not in ("http", "https") or \
            norm(o.netloc, o.scheme) != norm(host, o.scheme):
        raise HTTPException(status_code=403, detail="cross-origin refused")
