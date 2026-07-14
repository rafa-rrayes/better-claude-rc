"""M3 e2e: accounts + machine registry against a real (disposable) Postgres.

Flow (milestone definition):
  signup -> magic link (captured from the dev-mode email stub) -> session
  cookie -> create enroll token -> agent enrolls + connects with signed
  header -> welcome received -> second machine on free tier refused ->
  duplicate agent connect kicks the old (4409).

Plus the hardening seams: cookie attributes, token single-use burns, client
WS auth (cookie + ownership), live quotas (1 client / 2 term channels on
free), signed-header replay refusal, revoke -> live kill + connect refusal,
magic-link rate limit (3/h/email), platform-served PWA + CSP/HSTS headers,
audit log append-only.

Provisions its own Postgres (Docker, else pg_ctl) and tears it down.
Run: uv run python scripts/e2e_platform_auth.py
"""

import asyncio
import contextlib
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pgtmp  # noqa: E402

HOST, PORT = "127.0.0.1", 8807
BASE = f"http://{HOST}:{PORT}"
RELAY = f"ws://{HOST}:{PORT}"
EMAIL = "rafa@e2e.test"
COOKIE_NAME = "__Host-bcrc_sess"


async def expect_ws_close(url: str, code: int, headers: dict | None = None,
                          hello: bool = False) -> None:
    import websockets

    from server import proto
    ws = await websockets.connect(url, additional_headers=headers or {},
                                  max_size=proto.FRAME_MAX_WIRE)
    try:
        if hello:
            import json as _json
            await ws.send(proto.pack_frame(
                proto.KIND_CTRL, 0,
                _json.dumps({"type": "hello", "proto": proto.PROTO}).encode()))
        while True:
            await asyncio.wait_for(ws.recv(), 10)
    except websockets.exceptions.ConnectionClosed as e:
        got = e.rcvd.code if e.rcvd else None
        assert got == code, f"expected close {code}, got {got}"
    finally:
        await ws.close()


async def agent_hello(url: str, header: str):
    """Raw agent-leg connect: returns (ws, welcome_msg)."""
    import json as _json

    import websockets

    from server import proto
    ws = await websockets.connect(url, additional_headers={
        "X-BCRC-Agent": header}, max_size=proto.FRAME_MAX_WIRE)
    await ws.send(proto.pack_frame(
        proto.KIND_CTRL, 0,
        _json.dumps({"type": "hello", "proto": proto.PROTO}).encode()))
    kind, _ch, payload = proto.unpack_frame(
        await asyncio.wait_for(ws.recv(), 10))
    msg = _json.loads(payload)
    assert kind == proto.KIND_CTRL and msg.get("type") == "welcome", msg
    return ws, msg


async def main() -> None:
    print("-- provisioning postgres --")
    pg = pgtmp.start()
    try:
        await pgtmp.wait_ready(pg.dsn)
        print(f"postgres ready: {pg.dsn}")
        await run_suite(pg)
    finally:
        pg.stop()
    print("\nPLATFORM AUTH E2E PASS")


async def run_suite(pg) -> None:
    import os
    os.environ["PLATFORM_DATABASE_URL"] = pg.dsn
    os.environ["PLATFORM_BASE_URL"] = BASE

    import asyncpg
    import httpx
    import uvicorn
    import websockets

    from app import emailer
    from app import relay as relay_mod
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
    scratch = Path(tempfile.mkdtemp(prefix="bcrc-e2e-ident-"))

    # ---- S0: health + served PWA + headers + env
    r = await http.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"], r.text
    r = await http.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": True}, r.text
    r = await http.get("/api/env")
    assert r.json() == {"mode": "platform"}, r.text
    r = await http.get("/")
    assert r.status_code == 200 and "Claude RC" in r.text
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
    assert "max-age" in r.headers.get("strict-transport-security", "")
    r = await http.get("/transport.js")
    assert r.status_code == 200 and "RelayTransport" in r.text
    r = await http.get("/metrics")
    assert r.status_code == 200 and "bcrc_agents_connected" in r.text
    print("S0 PASS — health, PWA from agent package, CSP/HSTS, metrics")

    # ---- S1: signup -> magic link (dev email stub) -> session cookie
    r = await http.post("/api/auth/request-link", json={"email": EMAIL})
    assert r.status_code == 200, r.text
    body = emailer.get_sender().sent[-1]["body"]
    m = re.search(r"#auth=([A-Za-z0-9_\-]+)", body)
    assert m, f"no fragment token in email body: {body!r}"
    assert "?auth" not in body and "token=" not in body  # fragment only
    magic = m.group(1)

    r = await http.post("/api/auth/redeem", json={"token": magic})
    assert r.status_code == 200, r.text
    set_cookie = r.headers.get("set-cookie", "")
    assert set_cookie.startswith(f"{COOKIE_NAME}=")
    low = set_cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=lax" in low, \
        set_cookie
    session = re.match(rf"{COOKIE_NAME}=([^;]+)", set_cookie).group(1)
    cookie_hdr = {"Cookie": f"{COOKIE_NAME}={session}"}

    r = await http.get("/api/me", headers=cookie_hdr)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["email"] == EMAIL and me["plan"] == "free", me

    r = await http.post("/api/auth/redeem", json={"token": magic})
    assert r.status_code == 401, "magic link must be single-use"
    r = await http.get("/api/me")
    assert r.status_code == 401, "no cookie must mean 401"
    print(f"S1 PASS — signup -> magic link -> {COOKIE_NAME} cookie -> /api/me")

    # ---- S2: enroll token -> agent enrolls over HTTP (Ed25519 pubkey)
    r = await http.post("/api/machines/enroll-token", headers=cookie_hdr)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token"].startswith("bcrc_enr_"), tok
    assert "connect bcrc_enr_" in tok["command"], tok

    ident1 = Identity.load_or_create(scratch / "id1.json")
    assert (scratch / "id1.json").stat().st_mode & 0o777 == 0o600
    machine_id = await asyncio.to_thread(
        enroll, BASE, tok["token"], ident1,
        name="e2e-mac", agent_version="0.1.0", proto=proto.PROTO)
    ident1.machine_id = machine_id
    ident1.save()

    try:  # the token burned atomically with the insert
        await asyncio.to_thread(
            enroll, BASE, tok["token"], Identity.load_or_create(
                scratch / "idreuse.json"),
            name="reuse", agent_version="0.1.0", proto=proto.PROTO)
        raise AssertionError("reused enroll token must be refused")
    except EnrollError as e:
        assert "401" in str(e), e

    r = await http.get("/api/machines", headers=cookie_hdr)
    machines = r.json()["machines"]
    assert len(machines) == 1 and machines[0]["id"] == machine_id
    assert machines[0]["online"] is False
    print(f"S2 PASS — enroll token burned, machine {machine_id[:8]}… registered")

    # ---- S3: agent connects with the signed header -> welcome{plan}
    manager = SessionManager()
    devices1 = Devices.load(scratch / "devices1.json")
    pairing1 = Pairing.generate(machine_id, ident1.public_key)
    qr = pairing1.qr_payload()
    uplink = Uplink(manager, srv.terms, srv.config, RELAY, machine_id,
                    identity=ident1, devices=devices1, pairing=pairing1,
                    ping_interval=5, ping_timeout=5)
    utask = asyncio.create_task(uplink.run())
    for _ in range(100):
        if uplink.plan is not None:
            break
        await asyncio.sleep(0.1)
    assert uplink.plan == "free", f"no welcome (plan={uplink.plan})"
    r = await http.get("/api/machines", headers=cookie_hdr)
    assert r.json()["machines"][0]["online"] is True
    print("S3 PASS — signed header accepted, welcome{plan=free}, online")

    # ---- S4: client WS auth — cookie + ownership; rename API
    c = FakeClient(RELAY, machine_id, headers=cookie_hdr,
                   device=DeviceSim())
    w = await c.connect()
    assert w.get("plan") == "free", w
    await c.wait_status(True, timeout=10)
    ctrl = await c.open("ctrl")
    await c.handshake(ctrl, pair_qr=qr)  # M4: pair + E2E handshake
    hello = await c.wait_type(ctrl, "hello")
    assert hello.get("version"), hello
    rr = await c.rpc(ctrl, "state")
    assert rr["ok"] and rr["result"]["ok"], rr

    await expect_ws_close(f"{RELAY}/client/ws?machine={machine_id}",
                          proto.CLOSE_UNAUTHORIZED)  # no cookie
    r = await http.post(f"/api/machines/{machine_id}/rename",
                        headers=cookie_hdr, json={"name": "MacBook e2e"})
    assert r.status_code == 200, r.text
    r = await http.get("/api/machines", headers=cookie_hdr)
    assert r.json()["machines"][0]["name"] == "MacBook e2e"
    print("S4 PASS — cookie WS auth + rpc through relay; cookie-less 4401")

    # ---- S5: free-tier live quotas — 1 client connection, 2 term channels
    await expect_ws_close(f"{RELAY}/client/ws?machine={machine_id}",
                          proto.CLOSE_FORBIDDEN, headers=cookie_hdr,
                          hello=True)  # second concurrent client
    t1 = await c.open("term")
    t2 = await c.open("term")
    await c.send_ctrl({"type": "open", "kind": "term"})
    err = await c.next_ctrl("error", timeout=10)
    assert err.get("code") == "quota_term", err
    for ch in (t1, t2):
        await c.send_ctrl({"type": "close", "ch": ch})
    print("S5 PASS — 2nd client refused 4403; 3rd term channel quota_term")

    # ---- S6: second machine on the free tier refused (FOR UPDATE cap)
    r = await http.post("/api/machines/enroll-token", headers=cookie_hdr)
    tok2 = r.json()["token"]
    ident2 = Identity.load_or_create(scratch / "id2.json")
    try:
        await asyncio.to_thread(
            enroll, BASE, tok2, ident2,
            name="second", agent_version="0.1.0", proto=proto.PROTO)
        raise AssertionError("second free-tier machine must be refused")
    except EnrollError as e:
        assert "403" in str(e), e
    print("S6 PASS — second machine refused on free tier (403)")

    # ---- S7: duplicate agent connect kicks the old uplink (4409)
    # Ed25519 is deterministic: a header minted in the same wall-clock
    # second as the uplink's dial is byte-identical and the replay cache
    # (rightly) refuses it. Cross a second boundary for a fresh ts.
    await asyncio.sleep(1.1)
    dup_header = ident1.auth_header()
    raw2, w2 = await agent_hello(f"{RELAY}/agent/ws", dup_header)
    assert w2["plan"] == "free", w2
    closed = await c.next_ctrl("close", timeout=10)
    assert closed.get("reason") == "agent_replaced", closed
    assert machine_id in relay_mod.ROUTER.agents
    assert not utask.done(), "kicked uplink must keep retrying (30s backoff)"
    print("S7 PASS — duplicate agent connect kicked the old uplink (4409)")

    # ---- S8: signed-header replay + garbage signature refused
    await expect_ws_close(f"{RELAY}/agent/ws", proto.CLOSE_UNAUTHORIZED,
                          headers={"X-BCRC-Agent": dup_header})  # replay
    forged = ident1.auth_header().rsplit(".", 1)[0] + "." + "A" * 86
    await expect_ws_close(f"{RELAY}/agent/ws", proto.CLOSE_UNAUTHORIZED,
                          headers={"X-BCRC-Agent": forged})
    print("S8 PASS — header replay refused; forged signature refused")

    # ---- S9: revoke -> live agent killed + reconnect refused
    utask.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await utask
    # raw2 is the live agent; revoking must kill it (dashboard -> relay kill)
    r = await http.post(f"/api/machines/{machine_id}/revoke",
                        headers=cookie_hdr)
    assert r.status_code == 200, r.text
    try:
        while True:
            await asyncio.wait_for(raw2.recv(), 10)
    except websockets.exceptions.ConnectionClosed as e:
        assert e.rcvd and e.rcvd.code == proto.CLOSE_UNAUTHORIZED, e.rcvd
    await asyncio.sleep(1.1)  # fresh ts: prove refusal is the revoke
    await expect_ws_close(f"{RELAY}/agent/ws", proto.CLOSE_UNAUTHORIZED,
                          headers={"X-BCRC-Agent": ident1.auth_header()})
    r = await http.get("/api/machines", headers=cookie_hdr)
    assert r.json()["machines"] == [], r.text
    print("S9 PASS — revoke killed the live uplink; reconnect refused 4401")

    # ---- S10: magic-link rate limit 3/h/email
    other = "burst@e2e.test"
    for i in range(3):
        r = await http.post("/api/auth/request-link", json={"email": other})
        assert r.status_code == 200, f"req {i}: {r.text}"
    r = await http.post("/api/auth/request-link", json={"email": other})
    assert r.status_code == 429, r.text
    print("S10 PASS — 4th magic link for one email -> 429")

    # ---- S11: audit trail exists and is append-only
    conn = await asyncpg.connect(pg.dsn)
    try:
        events = {r["event"] for r in
                  await conn.fetch("SELECT event FROM audit_log")}
        for expected in ("auth.magic_link_requested", "auth.login",
                         "machine.enroll_token_created", "machine.enrolled",
                         "machine.renamed", "machine.revoked",
                         "agent.connected"):
            assert expected in events, f"missing audit event {expected}: {events}"
        try:
            await conn.execute("UPDATE audit_log SET event = 'tampered'")
            raise AssertionError("audit_log must refuse UPDATE")
        except asyncpg.exceptions.RaiseError:
            pass
    finally:
        await conn.close()
    print("S11 PASS — audit events recorded; table is append-only")

    # ---- teardown
    await c.close()
    await http.aclose()
    server.should_exit = True
    await server_task


asyncio.run(main())
