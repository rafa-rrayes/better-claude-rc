"""M5 e2e: billing — fixture Stripe webhook events drive free -> pro -> free
plan transitions against a real (disposable) Postgres, with
PLATFORM_FAKE_BILLING=1 (no Stripe network, ever).

Covers (milestone definition):
  - checkout.session.completed        -> plan pro
  - replaying the same event id       -> no-op (idempotency ledger)
  - customer.subscription.updated     -> past_due stays pro (dunning)
  - invoice.payment_failed            -> audit only
  - customer.subscription.deleted     -> plan free
  - quota behavior flips with plan: machine cap (enroll), live client cap
    (relay welcome{plan}), agent-side live-session cap (connect.py) — and a
    downgrade kills nothing mid-flight (connect/open time only)
  - real (non-fake) mode: unsigned/garbage-signed events -> 400; a properly
    HMAC-signed event passes stripe.Webhook.construct_event -> processed
  - fake checkout/portal endpoints return redirect URLs

Run: uv run python scripts/e2e_billing.py
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pgtmp  # noqa: E402

HOST, PORT = "127.0.0.1", 8811
BASE = f"http://{HOST}:{PORT}"
RELAY = f"ws://{HOST}:{PORT}"
EMAIL = "billing@e2e.test"
COOKIE_NAME = "__Host-bcrc_sess"
WEBHOOK = "/api/stripe/webhook"

CUS, SUB = "cus_e2e_1", "sub_e2e_1"


def evt(eid: str, etype: str, obj: dict) -> dict:
    # the shape stripe.Webhook.construct_event parses (top-level object=event)
    return {"id": eid, "object": "event", "type": etype,
            "created": int(time.time()), "data": {"object": obj}}


def sig_header(payload: bytes, secret: str) -> str:
    t = int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


async def expect_ws_close(url: str, code: int, headers: dict | None = None,
                          hello: bool = False) -> None:
    import websockets

    from server import proto
    ws = await websockets.connect(url, additional_headers=headers or {},
                                  max_size=proto.FRAME_MAX_WIRE)
    try:
        if hello:
            await ws.send(proto.pack_frame(
                proto.KIND_CTRL, 0,
                json.dumps({"type": "hello", "proto": proto.PROTO}).encode()))
        while True:
            await asyncio.wait_for(ws.recv(), 10)
    except websockets.exceptions.ConnectionClosed as e:
        got = e.rcvd.code if e.rcvd else None
        assert got == code, f"expected close {code}, got {got}"
    finally:
        await ws.close()


class _FakeLive:
    """Quacks enough like a live SessionRunner for the agent-side cap."""

    client = object()

    def snapshot(self) -> dict:
        return {"sid": "e2e-fake-live", "title": "fake", "cwd": "/", "state":
                "working", "detail": "", "project": "/", "model": "haiku",
                "mode": "default", "cost_usd": None, "context_pct": None,
                "claude_session_id": None, "created_at": 0, "last_active": 0,
                "live": True, "pending_permissions": 0, "last_msg": None}


async def main() -> None:
    print("-- provisioning postgres --")
    pg = pgtmp.start()
    try:
        await pgtmp.wait_ready(pg.dsn)
        print(f"postgres ready: {pg.dsn}")
        await run_suite(pg)
    finally:
        pg.stop()
    print("\nBILLING E2E PASS")


async def run_suite(pg) -> None:
    import os
    os.environ["PLATFORM_DATABASE_URL"] = pg.dsn
    os.environ["PLATFORM_BASE_URL"] = BASE
    os.environ["PLATFORM_FAKE_BILLING"] = "1"

    import asyncpg
    import httpx
    import uvicorn

    from app import emailer
    from app.main import app as platform_app
    from relay_client import DeviceSim, FakeClient
    from server import main as srv
    from server import proto
    from server.connect import Uplink
    from server.identity import Devices, EnrollError, Identity, Pairing, enroll
    from server.sessions import SessionManager

    server = uvicorn.Server(uvicorn.Config(
        platform_app, host=HOST, port=PORT, log_level="warning",
        ws_ping_interval=5, ws_ping_timeout=5, timeout_graceful_shutdown=1))
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        if server_task.done():
            server_task.result()
            return
        await asyncio.sleep(0.05)

    http = httpx.AsyncClient(base_url=BASE)
    scratch = Path(tempfile.mkdtemp(prefix="bcrc-e2e-billing-"))

    # ---- S1: signup -> session cookie; starts on the free plan
    r = await http.post("/api/auth/request-link", json={"email": EMAIL})
    assert r.status_code == 200, r.text
    magic = re.search(r"#auth=([A-Za-z0-9_\-]+)",
                      emailer.get_sender().sent[-1]["body"]).group(1)
    r = await http.post("/api/auth/redeem", json={"token": magic})
    assert r.status_code == 200, r.text
    session = re.match(rf"{COOKIE_NAME}=([^;]+)",
                       r.headers["set-cookie"]).group(1)
    cookie_hdr = {"Cookie": f"{COOKIE_NAME}={session}"}
    me = (await http.get("/api/me", headers=cookie_hdr)).json()
    user_id = me["user_id"]
    assert me["plan"] == "free", me
    b = (await http.get("/api/billing", headers=cookie_hdr)).json()
    assert b == {"plan": "free", "status": None, "current_period_end": None,
                 "manageable": False}, b
    print("S1 PASS — signup, plan free, empty billing info")

    # ---- S2: fake checkout / portal mint redirect URLs (no Stripe network)
    r = await http.post("/api/billing/checkout", headers=cookie_hdr)
    assert r.status_code == 200 and "#billing=" in r.json()["url"], r.text
    r = await http.post("/api/billing/portal", headers=cookie_hdr)
    assert r.status_code == 200 and "#billing=" in r.json()["url"], r.text
    r = await http.post("/api/billing/checkout")
    assert r.status_code == 401, "checkout must require a session"
    print("S2 PASS — fake checkout/portal URLs; auth required")

    # ---- S3: checkout.session.completed -> pro
    checkout_evt = evt("evt_e2e_checkout_1", "checkout.session.completed", {
        "id": "cs_e2e_1", "object": "checkout.session",
        "client_reference_id": user_id, "customer": CUS,
        "subscription": SUB, "mode": "subscription"})
    r = await http.post(WEBHOOK, json=checkout_evt)
    assert r.status_code == 200 and r.json()["status"] == "processed", r.text
    me = (await http.get("/api/me", headers=cookie_hdr)).json()
    assert me["plan"] == "pro", me
    b = (await http.get("/api/billing", headers=cookie_hdr)).json()
    assert b["status"] == "active" and b["manageable"], b
    print("S3 PASS — checkout.session.completed -> plan pro")

    # ---- S4: replaying the same event id is a no-op
    r = await http.post(WEBHOOK, json=checkout_evt)
    assert r.status_code == 200 and r.json()["status"] == "duplicate", r.text
    conn = await asyncpg.connect(pg.dsn)
    try:
        n = await conn.fetchval("SELECT count(*) FROM stripe_events"
                                " WHERE id = 'evt_e2e_checkout_1'")
        assert n == 1, f"event ledger rows: {n}"
        n = await conn.fetchval("SELECT count(*) FROM audit_log"
                                " WHERE event = 'billing.plan_changed'")
        assert n == 1, f"plan_changed audits after replay: {n}"
    finally:
        await conn.close()
    print("S4 PASS — replayed event id -> duplicate no-op (ledger row = 1)")

    # ---- S5: subscription.updated past_due stays pro (Stripe dunning)
    r = await http.post(WEBHOOK, json=evt(
        "evt_e2e_sub_upd_1", "customer.subscription.updated", {
            "id": SUB, "object": "subscription", "customer": CUS,
            "status": "past_due", "current_period_end": 1893456000,
            "items": {"data": [{"price": {"id": "price_e2e_pro"}}]}}))
    assert r.status_code == 200 and r.json()["status"] == "processed", r.text
    me = (await http.get("/api/me", headers=cookie_hdr)).json()
    assert me["plan"] == "pro", me
    b = (await http.get("/api/billing", headers=cookie_hdr)).json()
    assert b["status"] == "past_due" and b["current_period_end"], b
    print("S5 PASS — status past_due recorded, plan stays pro")

    # ---- S6: invoice.payment_failed -> audit only
    r = await http.post(WEBHOOK, json=evt(
        "evt_e2e_inv_fail_1", "invoice.payment_failed", {
            "id": "in_e2e_1", "object": "invoice", "customer": CUS,
            "subscription": SUB}))
    assert r.status_code == 200 and r.json()["status"] == "processed", r.text
    me = (await http.get("/api/me", headers=cookie_hdr)).json()
    assert me["plan"] == "pro", me
    print("S6 PASS — invoice.payment_failed audited, plan unchanged")

    # ---- S7: pro quotas — 2 machines enroll; 2 concurrent clients welcome
    async def enroll_machine(ident_path: Path, name: str) -> tuple:
        ident = Identity.load_or_create(ident_path)
        machine_id = await asyncio.to_thread(
            enroll, BASE, (await http.post(
                "/api/machines/enroll-token",
                headers=cookie_hdr)).json()["token"],
            ident, name=name, agent_version="0.1.0", proto=proto.PROTO)
        ident.machine_id = machine_id
        ident.save()
        return ident, machine_id

    ident_a, machine_a = await enroll_machine(scratch / "idA.json", "mac-a")
    _ident_b, _machine_b = await enroll_machine(scratch / "idB.json", "mac-b")
    print(f"  two machines enrolled on pro ({machine_a[:8]}…, "
          f"{_machine_b[:8]}…) — free would have refused the second")
    c1 = FakeClient(RELAY, machine_a, headers=cookie_hdr)
    w1 = await c1.connect()
    assert w1["plan"] == "pro", w1
    c2 = FakeClient(RELAY, machine_a, headers=cookie_hdr)
    w2 = await c2.connect()
    assert w2["plan"] == "pro", w2  # free caps concurrent clients at 1
    print("S7 PASS — machine cap + concurrent-client cap lifted on pro")

    # ---- S8: subscription.deleted -> free; nothing killed mid-flight
    r = await http.post(WEBHOOK, json=evt(
        "evt_e2e_sub_del_1", "customer.subscription.deleted", {
            "id": SUB, "object": "subscription", "customer": CUS,
            "status": "canceled"}))
    assert r.status_code == 200 and r.json()["status"] == "processed", r.text
    me = (await http.get("/api/me", headers=cookie_hdr)).json()
    assert me["plan"] == "free", me
    b = (await http.get("/api/billing", headers=cookie_hdr)).json()
    assert b["plan"] == "free" and b["status"] == "canceled", b
    await asyncio.sleep(0.5)  # downgrades apply at connect/open time only:
    assert c1.close_code is None and c2.close_code is None, \
        "downgrade must not kill live connections"
    # ...but a NEW connection sees the free cap (2 already connected >= 1)
    await expect_ws_close(f"{RELAY}/client/ws?machine={machine_a}",
                          proto.CLOSE_FORBIDDEN, headers=cookie_hdr,
                          hello=True)
    await c1.close()
    await c2.close()
    print("S8 PASS — downgrade to free: live conns kept, new conn refused")

    # ---- S9: machine cap back on free — third machine refused
    tok = (await http.post("/api/machines/enroll-token",
                           headers=cookie_hdr)).json()["token"]
    try:
        await asyncio.to_thread(
            enroll, BASE, tok, Identity.load_or_create(scratch / "idC.json"),
            name="mac-c", agent_version="0.1.0", proto=proto.PROTO)
        raise AssertionError("third machine must be refused on free")
    except EnrollError as e:
        assert "403" in str(e), e
    print("S9 PASS — third machine refused after downgrade (403)")

    # ---- S10: real (non-fake) mode — stripe.Webhook.construct_event
    os.environ["PLATFORM_FAKE_BILLING"] = "0"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_e2e_test"
    body = json.dumps(evt("evt_e2e_signed_1", "invoice.payment_failed", {
        "id": "in_e2e_2", "object": "invoice", "customer": CUS,
        "subscription": SUB})).encode()
    r = await http.post(WEBHOOK, content=body,
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 400, f"unsigned event must be 400: {r.text}"
    r = await http.post(WEBHOOK, content=body, headers={
        "Content-Type": "application/json",
        "Stripe-Signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400, f"garbage signature must be 400: {r.text}"
    r = await http.post(WEBHOOK, content=body, headers={
        "Content-Type": "application/json",
        "Stripe-Signature": sig_header(body, "whsec_e2e_test")})
    assert r.status_code == 200 and r.json()["status"] == "processed", r.text
    os.environ["PLATFORM_FAKE_BILLING"] = "1"
    print("S10 PASS — unsigned/garbage 400; HMAC-signed event verified")

    # ---- S11: agent-side live-session cap flips with welcome{plan}
    manager = SessionManager()
    devices_a = Devices.load(scratch / "devicesA.json")
    pairing_a = Pairing.generate(machine_a, ident_a.public_key)
    qr = pairing_a.qr_payload()
    uplink = Uplink(manager, srv.terms, srv.config, RELAY, machine_a,
                    identity=ident_a, devices=devices_a, pairing=pairing_a,
                    ping_interval=5, ping_timeout=5)
    utask = asyncio.create_task(uplink.run())
    for _ in range(100):
        if uplink.plan is not None:
            break
        await asyncio.sleep(0.1)
    assert uplink.plan == "free", f"agent welcome plan: {uplink.plan}"
    c5 = FakeClient(RELAY, machine_a, headers=cookie_hdr, device=DeviceSim())
    await c5.connect()
    await c5.wait_status(True, timeout=10)
    ctrl = await c5.secure_ctrl(pair_qr=qr)
    manager.runners["e2e-fake-live"] = _FakeLive()  # 1 live session = at cap
    rr = await c5.rpc(ctrl, "sessions.create", {"cwd": "/nowhere/jailed"})
    assert not rr["ok"] and rr["error"]["code"] == "quota_sessions", rr
    del manager.runners["e2e-fake-live"]
    rr = await c5.rpc(ctrl, "sessions.create", {"cwd": "/nowhere/jailed"})
    assert not rr["ok"] and rr["error"]["code"] != "quota_sessions", rr
    print("S11 PASS — free plan caps 1 live session; gate opens below cap")

    # ---- S12: billing audit trail
    conn = await asyncpg.connect(pg.dsn)
    try:
        events = {row["event"] for row in
                  await conn.fetch("SELECT event FROM audit_log")}
        for expected in ("billing.checkout_completed", "billing.plan_changed",
                         "billing.subscription_updated",
                         "billing.payment_failed"):
            assert expected in events, f"missing audit {expected}: {events}"
    finally:
        await conn.close()
    print("S12 PASS — billing audit events recorded")

    # ---- teardown
    await c5.close()
    utask.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await utask
    await http.aclose()
    server.should_exit = True
    await server_task


asyncio.run(main())
