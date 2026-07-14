"""Machine registry APIs: enroll-token minting (dashboard), agent enrollment
over HTTPS, list/rename/revoke.

Enrollment (plan §pairing step 1): the dashboard mints `bcrc_enr_` + 32B
(10 min, single use, SHA-256 stored); the agent's first
`connect <token>` POSTs its Ed25519 PUBLIC key here — the token burn and the
plan's machine cap run in one FOR UPDATE transaction (db.enroll_machine).
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import db as dbmod
from . import quotas, relay
from .db import QuotaExceeded
from .deps import check_origin, current_user, get_db
from .limits import limiter

log = logging.getLogger("bcrc.machines")
router = APIRouter()


def _mid(machine_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(machine_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such machine")


@router.post("/api/machines/enroll-token",
             dependencies=[Depends(check_origin)])
@limiter.limit("10/hour")
async def create_enroll_token(request: Request, user=Depends(current_user)):
    db = get_db(request)
    token = await db.create_token(user["id"], "enroll", dbmod.ENROLL_TTL_S,
                                  prefix=dbmod.ENROLL_PREFIX)
    await db.audit("machine.enroll_token_created", user_id=user["id"])
    return {"token": token, "expires_in": dbmod.ENROLL_TTL_S,
            "command": f"uvx better-claude-rc connect {token}"}


class EnrollBody(BaseModel):
    token: str
    name: str = Field(default="", max_length=120)
    ed25519_pk: str  # base64 (raw 32-byte Ed25519 public key)
    agent_version: str | None = Field(default=None, max_length=40)
    proto: int | None = None


@router.post("/api/enroll")
@limiter.limit("10/hour")
async def enroll(request: Request, body: EnrollBody):
    """Called by the agent (no session): burns the enroll token and
    registers the machine's public key."""
    db = get_db(request)
    try:
        pk = base64.b64decode(body.ed25519_pk, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="bad public key")
    if len(pk) != 32:
        raise HTTPException(status_code=400, detail="bad public key")
    try:
        row = await db.enroll_machine(
            body.token, name=body.name.strip()[:120], ed25519_pk=pk,
            agent_version=body.agent_version, proto=body.proto,
            machine_cap=quotas.machine_cap)
    except QuotaExceeded:
        raise HTTPException(
            status_code=403,
            detail="machine limit reached for your plan — upgrade or revoke "
                   "a machine from the dashboard")
    except Exception as e:  # duplicate pubkey etc.
        if "ed25519_pk" in str(e):
            raise HTTPException(status_code=409,
                                detail="this machine is already enrolled")
        raise
    if row is None:
        raise HTTPException(status_code=401,
                            detail="invalid or expired enroll token")
    await db.audit("machine.enrolled", user_id=row["user_id"],
                   machine_id=row["id"])
    return {"machine_id": str(row["id"]), "name": row["name"]}


@router.get("/api/machines")
async def list_machines(request: Request, user=Depends(current_user)):
    db = get_db(request)
    rows = await db.machines_for(user["id"])
    return {"machines": [{
        "id": str(r["id"]),
        "name": r["name"],
        "online": str(r["id"]) in relay.ROUTER.agents,
        "agent_version": r["agent_version"],
        "created_at": r["created_at"].isoformat(),
        "last_seen_at": r["last_seen_at"].isoformat()
        if r["last_seen_at"] else None,
    } for r in rows]}


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.post("/api/machines/{machine_id}/rename",
             dependencies=[Depends(check_origin)])
async def rename_machine(request: Request, machine_id: str, body: RenameBody,
                         user=Depends(current_user)):
    db = get_db(request)
    mid = _mid(machine_id)
    if not await db.rename_machine(user["id"], mid, body.name.strip()):
        raise HTTPException(status_code=404, detail="no such machine")
    await db.audit("machine.renamed", user_id=user["id"], machine_id=mid)
    return {"ok": True}


@router.post("/api/machines/{machine_id}/revoke",
             dependencies=[Depends(check_origin)])
async def revoke_machine(request: Request, machine_id: str,
                         user=Depends(current_user)):
    db = get_db(request)
    mid = _mid(machine_id)
    if not await db.revoke_machine(user["id"], mid):
        raise HTTPException(status_code=404, detail="no such machine")
    await db.audit("machine.revoked", user_id=user["id"], machine_id=mid)
    # dashboard revoke -> relay kills the live connection (plan §credentials)
    await relay.kick_machine(str(mid))
    return {"ok": True}
