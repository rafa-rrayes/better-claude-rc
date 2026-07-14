"""Plan quota table (plan §data model).

The machine cap is enforced in a FOR UPDATE transaction at enroll time
(db.enroll_machine); the live caps (concurrent client connections, term
channels) are enforced in-memory by the relay Router — race-free because a
single relay instance on one event loop is authoritative. Downgrades apply
at connect/open time only; nothing is killed mid-flight.
"""

from __future__ import annotations

CAPS = {
    "free": {"machines": 1, "clients": 1, "term_channels": 2},
    "pro": {"machines": 5, "clients": 5, "term_channels": 8},
}


def _caps(plan: str | None) -> dict:
    return CAPS.get(plan or "free", CAPS["free"])


def machine_cap(plan: str | None) -> int:
    return _caps(plan)["machines"]


def client_cap(plan: str | None) -> int:
    return _caps(plan)["clients"]


def term_channel_cap(plan: str | None) -> int:
    return _caps(plan)["term_channels"]
