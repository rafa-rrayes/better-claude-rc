"""Read-only smoke test against a LIVE platform deployment.

Verifies the public surface without creating any state: health endpoints,
metrics, landing page vs PWA split, security headers, auth refusals, and
both relay WS legs refusing unauthenticated connects. With --email it also
sends one real magic link (the only write; rate-limited server-side).

Run:
    uv run python scripts/smoke_platform.py https://bcrc.rrayes.com.br \
        [--email you@example.com] [--metrics-token TOKEN]

Exit code 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import websockets  # noqa: E402

from server import proto  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


async def expect_ws_close(url: str, code: int,
                          headers: dict | None = None) -> tuple[bool, str]:
    """Connect and wait for the server to close; True if it closed with
    `code`. Auth happens before the app-level hello, so none is sent."""
    try:
        ws = await websockets.connect(url, additional_headers=headers or {},
                                      max_size=proto.FRAME_MAX_WIRE,
                                      open_timeout=15)
    except Exception as e:
        return False, f"connect failed: {e!r}"
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 15)
    except websockets.exceptions.ConnectionClosed as e:
        got = e.rcvd.code if e.rcvd else None
        return got == code, f"close code {got} (want {code})"
    except asyncio.TimeoutError:
        return False, "no close within 15s"
    finally:
        await ws.close()


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", help="e.g. https://bcrc.rrayes.com.br")
    ap.add_argument("--email", help="send one real magic link to this address")
    ap.add_argument("--metrics-token", help="PLATFORM_METRICS_TOKEN if the "
                    "deployment gates /metrics")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    print(f"smoke: {base}\n")

    async with httpx.AsyncClient(base_url=base, timeout=20,
                                 follow_redirects=False) as http:
        # -- health
        r = await http.get("/healthz")
        check("healthz", r.status_code == 200 and r.json().get("ok") is True,
              f"{r.status_code}")
        r = await http.get("/readyz")
        check("readyz (db up)", r.status_code == 200
              and r.json() == {"ok": True, "db": True}, r.text[:80])

        # -- metrics (gated or open)
        headers = {}
        if args.metrics_token:
            r = await http.get("/metrics")
            check("metrics gated without token", r.status_code == 401,
                  f"{r.status_code}")
            headers = {"Authorization": f"Bearer {args.metrics_token}"}
        r = await http.get("/metrics", headers=headers)
        check("metrics series", r.status_code == 200
              and "bcrc_agents_connected" in r.text
              and "bcrc_emails_sent_total" in r.text, f"{r.status_code}")

        # -- landing page at /, PWA at /app
        r = await http.get("/")
        check("/ is landing (anonymous)", r.status_code == 200
              and "Claude RC" in r.text and 'id="signup"' in r.text,
              f"{r.status_code}")
        csp = r.headers.get("content-security-policy", "")
        check("security headers", "default-src 'self'" in csp
              and "max-age" in r.headers.get("strict-transport-security", "")
              and r.headers.get("x-content-type-options") == "nosniff"
              and "no-cache" in r.headers.get("cache-control", ""),
              "CSP/HSTS/nosniff/no-cache")
        r = await http.get("/app")
        check("/app is PWA", r.status_code == 200 and "app.js" in r.text
              and 'id="signup"' not in r.text, f"{r.status_code}")
        r = await http.get("/api/env")
        check("api/env platform mode", r.status_code == 200
              and r.json() == {"mode": "platform"}, r.text[:80])

        # -- auth surface
        r = await http.get("/api/me")
        check("api/me unauthenticated 401", r.status_code == 401,
              f"{r.status_code}")
        r = await http.post("/api/auth/request-link",
                            json={"email": "not-an-email"})
        check("bad email 400", r.status_code == 400, f"{r.status_code}")
        if args.email:
            r = await http.post("/api/auth/request-link",
                                json={"email": args.email})
            check(f"magic link sent to {args.email}", r.status_code == 200,
                  f"{r.status_code} {r.text[:80]}")

    # -- relay WS legs refuse unauthenticated connects
    machine = uuid.uuid4()
    ok, detail = await expect_ws_close(
        f"{ws_base}/client/ws?machine={machine}", proto.CLOSE_UNAUTHORIZED)
    check("client WS without cookie 4401", ok, detail)
    ok, detail = await expect_ws_close(
        f"{ws_base}/agent/ws", proto.CLOSE_UNAUTHORIZED,
        headers={"X-BCRC-Agent": f"{machine}.0.garbage"})
    check("agent WS with garbage header 4401", ok, detail)

    # -- summary
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("PLATFORM SMOKE PASS")
    return 0


sys.exit(asyncio.run(main()))
