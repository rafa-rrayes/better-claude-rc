"""Stripe webhooks (plan §stripe).

POST /api/stripe/webhook — signature verified with
stripe.Webhook.construct_event; idempotency via INSERT ... ON CONFLICT DO
NOTHING on the stripe_events ledger (the insert and all row updates share one
transaction, so an event is recorded iff it was fully applied). Handled types,
exactly:

  checkout.session.completed        -> record customer/subscription, plan pro
  customer.subscription.updated     -> update status, remap the user's plan
  customer.subscription.deleted     -> same (status is canceled)
  invoice.payment_failed            -> audit only (Stripe dunning owns retries)

Status mapping: active|trialing|past_due -> pro;
canceled|unpaid|incomplete_expired (and anything else) -> free.

PLATFORM_FAKE_BILLING=1 bypasses Stripe entirely for e2e: the signature check
is stubbed ONLY under this flag (fixture events are injected straight into
the same handler path). Never set in production.

Plan changes take effect at connect/open time only — the relay reads the plan
from Postgres on every new agent/client connection; nothing live is killed.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, HTTPException, Request

from . import metrics
from .db import Db
from .deps import get_db

log = logging.getLogger("bcrc.stripe")
router = APIRouter()

# Stripe subscription statuses that entitle the pro plan (plan §stripe):
# past_due stays pro while Stripe dunning retries the card.
PRO_STATUSES = frozenset({"active", "trialing", "past_due"})


def fake_billing() -> bool:
    return os.environ.get("PLATFORM_FAKE_BILLING") == "1"


def plan_for_status(status: str | None) -> str:
    return "pro" if status in PRO_STATUSES else "free"


def _ts(val) -> datetime | None:
    if not isinstance(val, (int, float)):
        return None
    return datetime.fromtimestamp(val, tz=timezone.utc)


# ---------------- row helpers (conn-scoped: share the event transaction) ---

async def _audit(conn, event: str, *, user_id=None, **meta) -> None:
    await conn.execute(
        "INSERT INTO audit_log (user_id, event, meta) VALUES ($1, $2, $3)",
        user_id, event, json.dumps(meta))


async def _set_plan(conn, user_id: uuid.UUID, plan: str) -> None:
    changed = await conn.fetchval(
        "UPDATE users SET plan = $2 WHERE id = $1 AND plan <> $2"
        " RETURNING id", user_id, plan)
    if changed is not None:
        await _audit(conn, "billing.plan_changed", user_id=user_id, plan=plan)
        log.info("user %s plan -> %s", user_id, plan)


async def _upsert_subscription(conn, user_id: uuid.UUID, *,
                               customer_id: str | None,
                               subscription_id: str | None,
                               status: str | None,
                               price_id: str | None = None,
                               current_period_end: datetime | None = None) -> None:
    await conn.execute(
        "INSERT INTO subscriptions (user_id, stripe_customer_id,"
        " stripe_subscription_id, status, price_id, current_period_end)"
        " VALUES ($1, $2, $3, $4, $5, $6)"
        " ON CONFLICT (user_id) DO UPDATE SET"
        "  stripe_customer_id ="
        "   coalesce(EXCLUDED.stripe_customer_id, subscriptions.stripe_customer_id),"
        "  stripe_subscription_id ="
        "   coalesce(EXCLUDED.stripe_subscription_id, subscriptions.stripe_subscription_id),"
        "  status = coalesce(EXCLUDED.status, subscriptions.status),"
        "  price_id = coalesce(EXCLUDED.price_id, subscriptions.price_id),"
        "  current_period_end = coalesce(EXCLUDED.current_period_end,"
        "   subscriptions.current_period_end)",
        user_id, customer_id, subscription_id, status, price_id,
        current_period_end)


async def _user_for_customer(conn, customer_id, subscription_id):
    """Map a Stripe customer/subscription back to our user via the
    subscriptions row that checkout.session.completed created."""
    if customer_id:
        row = await conn.fetchval(
            "SELECT user_id FROM subscriptions WHERE stripe_customer_id = $1",
            customer_id)
        if row is not None:
            return row
    if subscription_id:
        return await conn.fetchval(
            "SELECT user_id FROM subscriptions"
            " WHERE stripe_subscription_id = $1", subscription_id)
    return None


# ---------------- event handlers ----------------

async def _checkout_completed(conn, obj: dict) -> None:
    raw = obj.get("client_reference_id") or \
        (obj.get("metadata") or {}).get("user_id")
    try:
        user_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        await _audit(conn, "billing.unmatched_event",
                     type="checkout.session.completed")
        log.warning("checkout.session.completed without a usable "
                    "client_reference_id — ignored")
        return
    # Checkout only completes on successful payment (or trial start), so the
    # subscription is live; the customer.subscription.* stream keeps the
    # status exact from here on.
    await _upsert_subscription(
        conn, user_id,
        customer_id=obj.get("customer"),
        subscription_id=obj.get("subscription"),
        status="active")
    await _audit(conn, "billing.checkout_completed", user_id=user_id)
    await _set_plan(conn, user_id, plan_for_status("active"))


async def _subscription_changed(conn, obj: dict) -> None:
    """customer.subscription.updated AND .deleted (status: canceled)."""
    status = obj.get("status")
    customer_id = obj.get("customer")
    subscription_id = obj.get("id")
    user_id = await _user_for_customer(conn, customer_id, subscription_id)
    if user_id is None:
        await _audit(conn, "billing.unmatched_event",
                     type="customer.subscription.*", status=status)
        log.warning("subscription event for unknown customer %s — ignored",
                    customer_id)
        return
    items = ((obj.get("items") or {}).get("data") or [{}])
    price_id = ((items[0] or {}).get("price") or {}).get("id")
    await _upsert_subscription(
        conn, user_id, customer_id=customer_id,
        subscription_id=subscription_id, status=status, price_id=price_id,
        current_period_end=_ts(obj.get("current_period_end")))
    await _audit(conn, "billing.subscription_updated", user_id=user_id,
                 status=status)
    await _set_plan(conn, user_id, plan_for_status(status))


async def _payment_failed(conn, obj: dict) -> None:
    """Audit only — Stripe dunning owns retries; past_due keeps pro."""
    user_id = await _user_for_customer(conn, obj.get("customer"),
                                       obj.get("subscription"))
    await _audit(conn, "billing.payment_failed", user_id=user_id)


_HANDLERS = {
    "checkout.session.completed": _checkout_completed,
    "customer.subscription.updated": _subscription_changed,
    "customer.subscription.deleted": _subscription_changed,
    "invoice.payment_failed": _payment_failed,
}


# ---------------- endpoint ----------------

async def apply_event(db: Db, event: dict) -> str:
    """Record + apply one webhook event. Returns 'processed', 'duplicate'
    (same event id replayed — a no-op) or 'ignored' (unhandled type)."""
    event_id = event.get("id")
    etype = event.get("type")
    if not isinstance(event_id, str) or not event_id \
            or not isinstance(etype, str) or not etype:
        raise HTTPException(status_code=400, detail="malformed event")
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        raise HTTPException(status_code=400, detail="malformed event")
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval(
                "INSERT INTO stripe_events (id, type, payload)"
                " VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING"
                " RETURNING id", event_id, etype, json.dumps(event))
            if inserted is None:
                log.info("stripe event %s replayed — no-op", event_id)
                return "duplicate"
            handler = _HANDLERS.get(etype)
            if handler is None:
                return "ignored"
            await handler(conn, obj)
    metrics.STRIPE_EVENTS.labels(etype).inc()
    return "processed"


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    db = get_db(request)
    payload = await request.body()
    if fake_billing():
        # e2e escape hatch: fixture events go straight into the handler
        # path; the signature check is stubbed ONLY under this flag.
        try:
            event = json.loads(payload)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad payload")
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="bad payload")
    else:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            raise HTTPException(status_code=503,
                                detail="billing not configured")
        sig = request.headers.get("stripe-signature", "")
        try:
            # verifies the HMAC + shape; handlers below use the raw dict
            stripe.Webhook.construct_event(payload, sig, secret)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid signature")
        event = json.loads(payload)
    status = await apply_event(db, event)
    return {"received": True, "status": status}
