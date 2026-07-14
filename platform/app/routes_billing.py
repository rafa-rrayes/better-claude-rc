"""Billing routes (plan §stripe): Stripe owns every card/cancel/invoice UI.

POST /api/billing/checkout — Checkout Session (mode=subscription) for the
upgrade button; POST /api/billing/portal — Customer Portal session for the
manage button; GET /api/billing — current plan/status for the billing page.

The webhook stream (stripe_webhooks.py) is the single writer of plan state;
these routes only mint Stripe-hosted URLs. PLATFORM_FAKE_BILLING=1 returns
placeholder URLs so e2e never touches the network.

Env: STRIPE_SECRET_KEY, STRIPE_PRICE_ID (the pro subscription price),
PLATFORM_BASE_URL (redirect origin).
"""

from __future__ import annotations

import asyncio
import logging
import os

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request

from .deps import check_origin, current_user, get_db
from .limits import limiter
from .stripe_webhooks import fake_billing

log = logging.getLogger("bcrc.billing")
router = APIRouter()


def _base_url(request: Request) -> str:
    return (os.environ.get("PLATFORM_BASE_URL")
            or str(request.base_url)).rstrip("/")


def _stripe_conf() -> tuple[str, str]:
    key = os.environ.get("STRIPE_SECRET_KEY")
    price = os.environ.get("STRIPE_PRICE_ID")
    if not key or not price:
        raise HTTPException(status_code=503, detail="billing not configured")
    return key, price


@router.get("/api/billing")
async def billing_info(request: Request, user=Depends(current_user)):
    db = get_db(request)
    sub = await db.subscription_for(user["id"])
    return {
        "plan": user["plan"],
        "status": sub["status"] if sub else None,
        "current_period_end": sub["current_period_end"].isoformat()
        if sub and sub["current_period_end"] else None,
        "manageable": bool(sub and sub["stripe_customer_id"]),
    }


@router.post("/api/billing/checkout", dependencies=[Depends(check_origin)])
@limiter.limit("10/hour")
async def create_checkout(request: Request, user=Depends(current_user)):
    db = get_db(request)
    base = _base_url(request)
    if fake_billing():
        return {"url": f"{base}/#billing=fake_checkout"}
    key, price = _stripe_conf()
    sub = await db.subscription_for(user["id"])
    customer_id = sub["stripe_customer_id"] if sub else None

    def _create():
        stripe.api_key = key
        params = dict(
            mode="subscription",
            line_items=[{"price": price, "quantity": 1}],
            client_reference_id=str(user["id"]),
            subscription_data={"metadata": {"user_id": str(user["id"])}},
            success_url=f"{base}/#billing=success",
            cancel_url=f"{base}/#billing=cancelled",
        )
        if customer_id:
            params["customer"] = customer_id
        else:
            params["customer_email"] = user["email"]
        return stripe.checkout.Session.create(**params)

    try:
        session = await asyncio.to_thread(_create)
    except stripe.StripeError as e:
        log.warning("checkout session failed: %s", e)
        raise HTTPException(status_code=502, detail="stripe error")
    await db.audit("billing.checkout_created", user_id=user["id"])
    return {"url": session.url}


@router.post("/api/billing/portal", dependencies=[Depends(check_origin)])
@limiter.limit("10/hour")
async def create_portal(request: Request, user=Depends(current_user)):
    db = get_db(request)
    base = _base_url(request)
    if fake_billing():
        return {"url": f"{base}/#billing=fake_portal"}
    key, _price = _stripe_conf()
    sub = await db.subscription_for(user["id"])
    customer_id = sub["stripe_customer_id"] if sub else None
    if not customer_id:
        raise HTTPException(status_code=409,
                            detail="no billing account yet — upgrade first")

    def _create():
        stripe.api_key = key
        return stripe.billing_portal.Session.create(
            customer=customer_id, return_url=f"{base}/")

    try:
        session = await asyncio.to_thread(_create)
    except stripe.StripeError as e:
        log.warning("portal session failed: %s", e)
        raise HTTPException(status_code=502, detail="stripe error")
    await db.audit("billing.portal_opened", user_id=user["id"])
    return {"url": session.url}
