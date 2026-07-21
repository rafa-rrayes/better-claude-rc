"""RPC-over-WebSocket e2e: every method in server/rpc.py driven through a
real /ws connection (uvicorn on localhost), plus the M1 hardening negatives:
cookie-only WS auth, Origin allowlist, no ?token= anywhere, cwd jail.

No Claude turns are run (sessions.create is exercised without send).
Run: uv run python scripts/e2e_rpc.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
import websockets  # noqa: E402

from server import main as srv  # noqa: E402
from server.config import Config  # noqa: E402
from server.rpc import METHODS  # noqa: E402

HOST, PORT = "127.0.0.1", 8798
BASE = f"http://{HOST}:{PORT}"
WS_URL = f"ws://{HOST}:{PORT}/ws"

CWD = str(Path(__file__).resolve().parent.parent / "data" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)

cfg = Config.load()
_rid = 0
driven: set[str] = set()


async def rpc(ws, method, params=None):
    global _rid
    _rid += 1
    rid = _rid
    driven.add(method)
    await ws.send(json.dumps(
        {"type": "rpc", "id": rid, "method": method, "params": params or {}}))
    while True:  # event pushes may interleave with rpc results
        msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if msg.get("type") == "rpc_result" and msg.get("id") == rid:
            return msg


async def expect_close(code, why, url=WS_URL, **kwargs):
    try:
        async with websockets.connect(url, **kwargs) as ws:
            await asyncio.wait_for(ws.recv(), 10)
        raise AssertionError(f"{why}: connection survived, expected close {code}")
    except websockets.ConnectionClosed as e:
        got = e.rcvd.code if e.rcvd else None
        assert got == code, f"{why}: expected close {code}, got {got}"
    print(f"  auth negative ok — {why} -> {code}")


async def main():
    server = uvicorn.Server(uvicorn.Config(
        srv.app, host=HOST, port=PORT, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    home = str(Path(cfg.browse_root).resolve())
    good = {"Cookie": f"bcrc_token={cfg.token}", "Origin": f"http://{HOST}:{PORT}"}

    async with websockets.connect(WS_URL, additional_headers=good) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello", hello
        first = json.loads(await ws.recv())
        assert first["type"] == "sessions", first
        print(f"connected — hello v{hello['version']}")

        # ---- state
        r = await rpc(ws, "state")
        assert r["ok"] and r["result"]["ok"] and "defaults" in r["result"], r
        print(f"state ok — {len(r['result']['sessions'])} sessions")

        # ---- projects.recent
        r = await rpc(ws, "projects.recent")
        assert r["ok"] and isinstance(r["result"]["projects"], list), r
        print(f"projects.recent ok — {len(r['result']['projects'])} projects")

        # ---- browse + root confinement
        r = await rpc(ws, "browse")
        assert r["ok"] and r["result"]["path"] == home, r
        r = await rpc(ws, "browse", {"path": "/etc"})
        assert r["ok"] and r["result"]["path"] == home, "browse escaped root!"
        print("browse ok — root confinement holds")

        # ---- library
        r = await rpc(ws, "library")
        assert r["ok"] and "skills" in r["result"], r
        print(f"library ok — {len(r['result']['skills'])} skills")

        # ---- file.read + jail
        probe = Path(CWD) / "rpc-probe.txt"
        probe.write_text("hello-rpc")
        r = await rpc(ws, "file.read", {"path": str(probe)})
        assert r["ok"] and r["result"]["content"] == "hello-rpc", r
        r = await rpc(ws, "file.read", {"path": "/etc/hosts"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", r
        r = await rpc(ws, "file.read", {"path": str(Path(CWD) / "nope.txt")})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        print("file.read ok — content + jail + not_found")

        # ---- projects.sessions / projects.preview (real transcripts)
        r = await rpc(ws, "projects.sessions", {"cwd": CWD})
        assert r["ok"] and isinstance(r["result"]["sessions"], list), r
        past = r["result"]["sessions"]
        sid_prev = past[0]["session_id"] if past else "no-such-session"
        r = await rpc(ws, "projects.preview", {"session_id": sid_prev, "cwd": CWD})
        assert r["ok"] and isinstance(r["result"]["messages"], list), r
        print(f"projects.sessions/preview ok — {len(past)} past, "
              f"{len(r['result']['messages'])} preview messages")

        # ---- sessions.create / stop / delete (no model turn)
        r = await rpc(ws, "sessions.create",
                      {"cwd": CWD, "model": "haiku", "title": "rpc-e2e"})
        assert r["ok"] and r["result"]["sid"], r
        sid = r["result"]["sid"]
        r = await rpc(ws, "sessions.stop", {"sid": sid})
        assert r["ok"], r
        r = await rpc(ws, "sessions.delete", {"sid": sid})
        assert r["ok"] and r["result"]["ok"], r
        print(f"sessions.create/stop/delete ok — sid {sid}")

        # ---- devices.list / devices.revoke / revoke_device (M4 registry)
        import tempfile

        from server import identity as idm
        from server.identity import Devices
        devdir = Path(tempfile.mkdtemp(prefix="bcrc-rpc-devs-"))
        registry = Devices.load(devdir / "devices.json")
        fake_pk = "A" * 43 + "="  # b64 of 32 bytes, shape only
        registry.add(fake_pk, "rpc-e2e-phone")
        idm.set_active_devices(registry)
        r = await rpc(ws, "devices.list")
        assert r["ok"] and any(d["device_pk"] == fake_pk
                               for d in r["result"]["devices"]), r
        r = await rpc(ws, "devices.revoke", {"device_pk": fake_pk})
        assert r["ok"] and not registry.is_paired(fake_pk), r
        r = await rpc(ws, "revoke_device", {"device_pk": fake_pk})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        print("devices.list/revoke ok — list, revoke, alias not_found")

        # ---- error paths
        r = await rpc(ws, "sessions.create", {"cwd": "/etc"})
        assert not r["ok"] and r["error"]["code"] == "forbidden", \
            f"cwd jail failed: {r}"
        r = await rpc(ws, "sessions.create", {"cwd": ""})
        assert not r["ok"] and r["error"]["code"] == "bad_request", r
        r = await rpc(ws, "sessions.stop", {"sid": "zzz"})
        assert not r["ok"] and r["error"]["code"] == "not_found", r
        r = await rpc(ws, "does.not.exist")
        assert not r["ok"] and r["error"]["code"] == "unknown_method", r
        driven.discard("does.not.exist")
        print("error paths ok — cwd jail, bad_request, not_found, unknown_method")

    missing = set(METHODS) - driven
    assert not missing, f"rpc methods not driven: {missing}"
    print(f"all {len(METHODS)} rpc methods driven over /ws")

    # ---- auth negatives (cookie-only + Origin allowlist, no query tokens)
    await expect_close(4401, "no cookie")
    await expect_close(4401, "wrong cookie",
                       additional_headers={"Cookie": "bcrc_token=wrong"})
    await expect_close(4401, "?token= query auth removed",
                       url=f"{WS_URL}?token={cfg.token}")
    await expect_close(4403, "cross-origin browser upgrade", additional_headers={
        "Cookie": f"bcrc_token={cfg.token}", "Origin": "https://evil.example"})

    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE}/api/state", params={"token": cfg.token})
        assert r.status_code == 401, "REST ?token= auth must be gone"
        r = await client.get(f"{BASE}/api/env")
        assert r.status_code == 200 and r.json() == {"mode": "local"}, r.text
    print("REST negatives ok — ?token= dead, /api/env open")

    server.should_exit = True
    await task
    print("\nRPC E2E PASS")


asyncio.run(main())
