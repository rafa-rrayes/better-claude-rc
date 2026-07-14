"""Postgres layer (asyncpg): users, hashed credentials, machine registry,
append-only audit log, plain-SQL migrations.

Every token the platform issues is stored as its SHA-256 only (plan
§credentials) — a stolen database yields emails, pubkeys and hashes, zero
impersonation capability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from pathlib import Path

import asyncpg

log = logging.getLogger("bcrc.db")

MAGIC_LINK_TTL_S = 15 * 60          # 15 min, single use
ENROLL_TTL_S = 10 * 60              # 10 min, single use
SESSION_TTL_S = 90 * 24 * 3600      # 90 days, rolling

ENROLL_PREFIX = "bcrc_enr_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def _migrations_dir() -> Path:
    here = Path(__file__).resolve().parent
    for cand in (here / "migrations", here.parent / "migrations"):
        if cand.is_dir():
            return cand
    raise FileNotFoundError("migrations directory not found")


async def apply_migrations(conn: asyncpg.Connection) -> list[str]:
    """Apply platform/migrations/*.sql in filename order, once each."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
    applied = []
    for path in sorted(_migrations_dir().glob("*.sql")):
        row = await conn.fetchrow(
            "SELECT 1 FROM schema_migrations WHERE name = $1", path.name)
        if row:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
        applied.append(path.name)
        log.info("applied migration %s", path.name)
    return applied


class QuotaExceeded(Exception):
    """Machine cap reached for the user's plan."""


class Db:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, migrate: bool = True) -> "Db":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        if migrate:
            async with pool.acquire() as conn:
                await apply_migrations(conn)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> bool:
        return await self.pool.fetchval("SELECT 1") == 1

    # ---------------- users ----------------

    async def upsert_user(self, email: str) -> asyncpg.Record:
        return await self.pool.fetchrow(
            "INSERT INTO users (email) VALUES ($1)"
            " ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email"
            " RETURNING id, email, plan, created_at, last_login_at", email)

    async def user(self, user_id: uuid.UUID) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT id, email, plan, created_at, last_login_at"
            " FROM users WHERE id = $1", user_id)

    # ---------------- tokens ----------------

    async def create_token(self, user_id: uuid.UUID, purpose: str,
                           ttl_s: int, *, prefix: str = "") -> str:
        """Mint a token, store only its hash. Returns the plaintext once."""
        token = new_token(prefix)
        await self.pool.execute(
            "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at)"
            " VALUES ($1, $2, $3, now() + make_interval(secs => $4))",
            user_id, purpose, hash_token(token), float(ttl_s))
        return token

    async def redeem_token(self, token: str, purpose: str) -> uuid.UUID | None:
        """Burn a single-use token (magic_link / enroll). None if invalid,
        expired, or already used — the UPDATE is the atomic burn."""
        return await self.pool.fetchval(
            "UPDATE auth_tokens SET used_at = now()"
            " WHERE token_hash = $1 AND purpose = $2"
            "   AND used_at IS NULL AND expires_at > now()"
            " RETURNING user_id", hash_token(token), purpose)

    # ---------------- sessions ----------------

    async def create_session(self, user_id: uuid.UUID) -> str:
        token = await self.create_token(user_id, "session", SESSION_TTL_S)
        await self.pool.execute(
            "UPDATE users SET last_login_at = now() WHERE id = $1", user_id)
        return token

    async def session_user(self, token: str) -> asyncpg.Record | None:
        """Validate a session cookie; 90d rolling (each use extends expiry)."""
        return await self.pool.fetchrow(
            "UPDATE auth_tokens t"
            " SET expires_at = now() + make_interval(secs => $2)"
            " FROM users u"
            " WHERE t.token_hash = $1 AND t.purpose = 'session'"
            "   AND t.used_at IS NULL AND t.expires_at > now()"
            "   AND u.id = t.user_id"
            " RETURNING u.id, u.email, u.plan, u.created_at",
            hash_token(token), float(SESSION_TTL_S))

    async def revoke_session(self, token: str) -> None:
        await self.pool.execute(
            "UPDATE auth_tokens SET used_at = now()"
            " WHERE token_hash = $1 AND purpose = 'session' AND used_at IS NULL",
            hash_token(token))

    # ---------------- machines ----------------

    async def enroll_machine(self, token: str, *, name: str, ed25519_pk: bytes,
                             agent_version: str | None, proto: int | None,
                             machine_cap) -> asyncpg.Record | None:
        """Burn the enroll token and insert the machine, enforcing the plan's
        machine cap inside one FOR UPDATE transaction (plan §data model).
        Returns the machine row, None for a bad token, raises QuotaExceeded
        at the cap. machine_cap: plan -> int."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                user_id = await conn.fetchval(
                    "UPDATE auth_tokens SET used_at = now()"
                    " WHERE token_hash = $1 AND purpose = 'enroll'"
                    "   AND used_at IS NULL AND expires_at > now()"
                    " RETURNING user_id", hash_token(token))
                if user_id is None:
                    return None
                # Serialize concurrent enrolls for this user on the user row.
                plan = await conn.fetchval(
                    "SELECT plan FROM users WHERE id = $1 FOR UPDATE", user_id)
                count = await conn.fetchval(
                    "SELECT count(*) FROM machines"
                    " WHERE user_id = $1 AND revoked_at IS NULL", user_id)
                if count >= machine_cap(plan):
                    raise QuotaExceeded(f"machine cap reached for plan {plan}")
                return await conn.fetchrow(
                    "INSERT INTO machines"
                    " (user_id, name, ed25519_pk, agent_version, proto)"
                    " VALUES ($1, $2, $3, $4, $5)"
                    " RETURNING id, user_id, name, created_at",
                    user_id, name, ed25519_pk, agent_version, proto)

    async def machine_auth(self, machine_id: uuid.UUID) -> asyncpg.Record | None:
        """Machine + owning user's plan, for relay auth. None if revoked."""
        return await self.pool.fetchrow(
            "SELECT m.id, m.user_id, m.ed25519_pk, u.plan"
            " FROM machines m JOIN users u ON u.id = m.user_id"
            " WHERE m.id = $1 AND m.revoked_at IS NULL", machine_id)

    async def machines_for(self, user_id: uuid.UUID) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, name, agent_version, created_at, last_seen_at, revoked_at"
            " FROM machines WHERE user_id = $1 AND revoked_at IS NULL"
            " ORDER BY created_at", user_id)

    async def rename_machine(self, user_id: uuid.UUID, machine_id: uuid.UUID,
                             name: str) -> bool:
        tag = await self.pool.execute(
            "UPDATE machines SET name = $3"
            " WHERE id = $2 AND user_id = $1 AND revoked_at IS NULL",
            user_id, machine_id, name)
        return tag.endswith("1")

    async def revoke_machine(self, user_id: uuid.UUID,
                             machine_id: uuid.UUID) -> bool:
        tag = await self.pool.execute(
            "UPDATE machines SET revoked_at = now()"
            " WHERE id = $2 AND user_id = $1 AND revoked_at IS NULL",
            user_id, machine_id)
        return tag.endswith("1")

    async def touch_machine(self, machine_id: uuid.UUID) -> None:
        await self.pool.execute(
            "UPDATE machines SET last_seen_at = now() WHERE id = $1", machine_id)

    async def set_agent_meta(self, machine_id: uuid.UUID,
                             agent_version: str | None, proto: int | None) -> None:
        await self.pool.execute(
            "UPDATE machines SET agent_version = coalesce($2, agent_version),"
            " proto = coalesce($3, proto), last_seen_at = now() WHERE id = $1",
            machine_id, agent_version, proto)

    # ---------------- billing ----------------

    async def subscription_for(self, user_id: uuid.UUID) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT user_id, stripe_customer_id, stripe_subscription_id,"
            " status, price_id, current_period_end"
            " FROM subscriptions WHERE user_id = $1", user_id)

    # ---------------- audit (append-only, metadata only) ----------------

    async def audit(self, event: str, *, user_id: uuid.UUID | None = None,
                    machine_id: uuid.UUID | None = None, **meta) -> None:
        try:
            await self.pool.execute(
                "INSERT INTO audit_log (user_id, machine_id, event, meta)"
                " VALUES ($1, $2, $3, $4)",
                user_id, machine_id, event, json.dumps(meta))
        except Exception:  # audit must never take the request down
            log.exception("audit write failed: %s", event)
