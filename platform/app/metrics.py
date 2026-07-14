"""Prometheus metrics (plan §observability): connected agents/clients,
channels, frames/bytes relayed, closes by code, quota denials."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)

AGENTS_CONNECTED = Gauge(
    "bcrc_agents_connected", "Agent uplinks currently connected")
CLIENTS_CONNECTED = Gauge(
    "bcrc_clients_connected", "Browser client connections currently open")
CHANNELS_OPEN = Gauge(
    "bcrc_channels_open", "Relay channels currently open", ["kind"])
FRAMES_RELAYED = Counter(
    "bcrc_frames_relayed_total", "Opaque frames relayed", ["direction"])
BYTES_RELAYED = Counter(
    "bcrc_bytes_relayed_total", "Opaque frame bytes relayed", ["direction"])
WS_CLOSES = Counter(
    "bcrc_ws_closes_total", "WS closes initiated by the relay",
    ["leg", "code"])
QUOTA_DENIALS = Counter(
    "bcrc_quota_denials_total", "Live quota denials", ["kind"])
STRIPE_EVENTS = Counter(
    "bcrc_stripe_events_total", "Stripe webhook events processed", ["type"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
