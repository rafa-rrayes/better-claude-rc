"""Rate limiting: slowapi for per-IP HTTP limits, plus a tiny sliding
window used for the per-email magic-link cap and per-IP WS connects
(slowapi can't key on request bodies or WS upgrades)."""

from __future__ import annotations

import time
from collections import deque

from slowapi import Limiter


def client_ip(request) -> str:
    """Real client IP for rate-limit keys. In production we sit behind
    Cloudflare + Traefik: CF-Connecting-IP is set by the edge and cannot be
    spoofed by the client (leftmost X-Forwarded-For can). Falls back to the
    socket peer for direct/local connections. Works for both HTTP Requests
    and WebSocket upgrades (both expose .headers and .client)."""
    ip = request.headers.get("cf-connecting-ip")
    if ip:
        return ip.strip()
    return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(key_func=client_ip, headers_enabled=True)


class SlidingWindow:
    def __init__(self, max_events: int, window_s: float):
        self.max_events = max_events
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        if len(self._hits) > 10_000:  # bound memory on churny keys
            for k in [k for k, v in self._hits.items()
                      if not v or v[-1] <= now - self.window_s]:
                del self._hits[k]
        return True
