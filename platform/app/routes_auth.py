"""Magic-link auth (plan §credentials).

request-link -> 32B urlsafe token, SHA-256 stored, 15 min single-use; the
link carries the token in the URL FRAGMENT (#auth=...) so it never reaches
server logs or Referer headers. redeem -> __Host-bcrc_sess session cookie
(HttpOnly, Secure, SameSite=Lax), SHA-256 in PG, 90d rolling.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from . import db as dbmod
from .deps import SESSION_COOKIE, check_origin, current_user, get_db
from .emailer import get_sender
from .limits import SlidingWindow, limiter

log = logging.getLogger("bcrc.auth")
router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# magic links: 3/h per email (plus slowapi's per-IP guard on the route)
_email_window = SlidingWindow(max_events=3, window_s=3600)


def _base_url(request: Request) -> str:
    return (os.environ.get("PLATFORM_BASE_URL")
            or str(request.base_url)).rstrip("/")


def _set_session_cookie(response: Response, token: str) -> None:
    # __Host- prefix: Secure, Path=/, no Domain — bound to this exact origin.
    response.set_cookie(
        SESSION_COOKIE, token, max_age=dbmod.SESSION_TTL_S,
        httponly=True, secure=True, samesite="lax", path="/")


class RequestLink(BaseModel):
    email: str


class Redeem(BaseModel):
    token: str


@router.post("/api/auth/request-link", dependencies=[Depends(check_origin)])
@limiter.limit("20/hour")
async def request_link(request: Request, body: RequestLink):
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise HTTPException(status_code=400, detail="invalid email")
    if not _email_window.allow(email):
        raise HTTPException(status_code=429, detail="too many login links; "
                            "try again later")
    db = get_db(request)
    user = await db.upsert_user(email)
    token = await db.create_token(user["id"], "magic_link",
                                  dbmod.MAGIC_LINK_TTL_S)
    link = f"{_base_url(request)}/#auth={token}"
    await get_sender().send(
        email, "Sign in to Better Claude RC",
        f"Tap to sign in (valid 15 minutes, single use):\n\n{link}\n")
    await db.audit("auth.magic_link_requested", user_id=user["id"])
    return {"ok": True}


@router.post("/api/auth/redeem", dependencies=[Depends(check_origin)])
@limiter.limit("30/hour")
async def redeem(request: Request, body: Redeem, response: Response):
    db = get_db(request)
    user_id = await db.redeem_token(body.token, "magic_link")
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid or expired link")
    session = await db.create_session(user_id)
    _set_session_cookie(response, session)
    await db.audit("auth.login", user_id=user_id)
    return {"ok": True}


@router.post("/api/auth/logout", dependencies=[Depends(check_origin)])
async def logout(request: Request, response: Response):
    db = getattr(request.app.state, "db", None)
    token = request.cookies.get(SESSION_COOKIE)
    if db is not None and token:
        await db.revoke_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True,
                           httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/api/me")
async def me(user=Depends(current_user)):
    return {"user_id": str(user["id"]), "email": user["email"],
            "plan": user["plan"]}
