"""Email sending behind an interface. Dev mode (the default, and the only
mode tests may use) LOGS the message instead of sending anything — the e2e
suite captures magic links from here. A real transactional provider
(Resend/Postmark over httpx) plugs in at deploy time (M6) without touching
callers.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("bcrc.email")


class DevEmailSender:
    """Logs the email; keeps the last few for in-process test capture."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})
        del self.sent[:-20]
        log.info("[dev email] to=%s subject=%r\n%s", to, subject, body)


_SENDER: DevEmailSender | None = None


def get_sender():
    global _SENDER
    if _SENDER is None:
        mode = os.environ.get("PLATFORM_EMAIL_MODE", "dev")
        if mode != "dev":
            raise RuntimeError(
                f"email mode {mode!r} not implemented yet (M6 ships a real "
                "provider); unset PLATFORM_EMAIL_MODE for the dev logger")
        _SENDER = DevEmailSender()
    return _SENDER
