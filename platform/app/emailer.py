"""Email sending behind an interface. Dev mode (the default, and the only
mode tests may use) LOGS the message instead of sending anything — the e2e
suite captures magic links from here. PLATFORM_EMAIL_MODE=resend sends real
mail through Resend's HTTP API (M6); callers never change.
"""

from __future__ import annotations

import logging
import os

import httpx

from . import metrics

log = logging.getLogger("bcrc.email")


class EmailError(Exception):
    """Send failed. Callers decide whether to surface it — the auth route
    swallows it so responses stay generic (no email enumeration)."""


class DevEmailSender:
    """Logs the email; keeps the last few for in-process test capture."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        del self.sent[:-20]
        log.info("[dev email] to=%s subject=%r\n%s", to, subject, body)


class ResendEmailSender:
    """POST https://api.resend.com/emails, one call per message."""

    def __init__(self, api_key: str, sender: str) -> None:
        self.sender = sender
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=10.0)

    async def send(self, to: str, subject: str, body: str) -> None:
        try:
            r = await self._client.post(
                "https://api.resend.com/emails",
                json={"from": self.sender, "to": [to],
                      "subject": subject, "text": body})
        except httpx.HTTPError as e:
            metrics.EMAILS_SENT.labels("error").inc()
            log.error("resend send to %s failed: %r", to, e)
            raise EmailError(str(e)) from e
        if r.status_code >= 400:
            metrics.EMAILS_SENT.labels("error").inc()
            log.error("resend refused (%s): %s", r.status_code, r.text[:500])
            raise EmailError(f"resend returned {r.status_code}")
        metrics.EMAILS_SENT.labels("sent").inc()


_SENDER: DevEmailSender | ResendEmailSender | None = None


def get_sender():
    global _SENDER
    if _SENDER is None:
        mode = os.environ.get("PLATFORM_EMAIL_MODE", "dev")
        if mode == "dev":
            _SENDER = DevEmailSender()
        elif mode == "resend":
            api_key = os.environ.get("RESEND_API_KEY")
            sender = os.environ.get("PLATFORM_EMAIL_FROM")
            if not api_key or not sender:
                raise RuntimeError(
                    "PLATFORM_EMAIL_MODE=resend requires RESEND_API_KEY "
                    "and PLATFORM_EMAIL_FROM")
            _SENDER = ResendEmailSender(api_key, sender)
        else:
            raise RuntimeError(
                f"unknown PLATFORM_EMAIL_MODE {mode!r} (dev | resend)")
    return _SENDER
