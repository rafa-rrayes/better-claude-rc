"""Relay e2e: in-process platform relay + agent in connect mode + a fake mux
"browser" client (plaintext NullCodec — M4 swaps in E2ECodec).

Everything flows phone-style THROUGH the relay:
  1. rpc (state, sessions.create) over a ctrl channel
  2. attach → send → streaming events → result (seqs contiguous)
  3. Bash permission request → approve → tool_result
  4. term channel: term_attach{sid} inside the channel → ANSI output →
     typed input echoes back → kill → exit + channel close
  5. frame cap: oversized frame kills the violator with 1009
  6. duplicate agent connect: old uplink kicked with 4409, new accepted

Costs a few haiku turns. Run: uv run python scripts/e2e_relay.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
import websockets  # noqa: E402

from app import relay as relay_mod  # noqa: E402
from app.main import app as platform_app  # noqa: E402
from relay_client import FakeClient  # noqa: E402
from server import main as srv  # noqa: E402
from server import proto  # noqa: E402
from server.connect import Uplink  # noqa: E402
from server.sessions import SessionManager  # noqa: E402

HOST, RPORT = "127.0.0.1", 8806
RELAY = f"ws://{HOST}:{RPORT}"
MACHINE = "e2e-mach"
CWD = str(Path(__file__).resolve().parent.parent / "data" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)

# The permission scenario must prompt even if the user's global settings
# allowlist Bash: a project-local ask rule takes precedence over allow rules,
# so `echo …` deterministically asks inside the playground.
_local = Path(CWD) / ".claude"
_local.mkdir(exist_ok=True)
(_local / "settings.local.json").write_text(json.dumps(
    {"permissions": {"ask": ["Bash(echo:*)"]}}))


async def start_relay():
    server = uvicorn.Server(uvicorn.Config(
        platform_app, host=HOST, port=RPORT, log_level="warning",
        ws_ping_interval=5, ws_ping_timeout=5, timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def run_turn(c, ctrl, sid, text):
    """Send a prompt; collect events until result. Returns collected state."""
    await c.send_json(ctrl, {"type": "send", "session_id": sid, "text": text})
    saw = {"deltas": 0, "text": None, "result": None, "tool_out": "",
           "perm": False, "seqs": []}
    while True:
        msg = await c.next_json(ctrl, timeout=120)
        if msg.get("type") != "event" or msg.get("session_id") != sid:
            continue
        ev = msg["ev"]
        if ev.get("seq") is not None:
            saw["seqs"].append(ev["seq"])
        k = ev.get("kind")
        if k == "delta":
            saw["deltas"] += 1
        elif k == "text":
            saw["text"] = ev["text"]
        elif k == "tool_result":
            saw["tool_out"] += ev.get("text") or ""
        elif k == "permission":
            saw["perm"] = True
            await c.send_json(ctrl, {
                "type": "permission", "session_id": sid,
                "request_id": ev["request_id"], "behavior": "allow"})
        elif k == "notice" and ev.get("level") == "error":
            print("ERROR NOTICE:", ev["text"])
        elif k == "result":
            saw["result"] = ev
            return saw


async def main():
    relay_server, relay_task = await start_relay()
    m = SessionManager()
    uplink = Uplink(m, srv.terms, srv.config, RELAY, MACHINE,
                    ping_interval=5, ping_timeout=5)
    utask = asyncio.create_task(uplink.run())

    # ---- connect the fake browser, wait for the machine to come online
    c = FakeClient(RELAY, MACHINE)
    w = await c.connect()
    assert w.get("proto") == proto.PROTO and "plan" in w, w
    await c.wait_status(True, timeout=20)
    print(f"machine online through relay (plan={w['plan']})")

    # ---- ctrl channel: agent pushes hello + sessions, rpc works
    ctrl = await c.open("ctrl")
    hello = await c.wait_type(ctrl, "hello")
    assert hello.get("version"), hello
    first = await c.wait_type(ctrl, "sessions")
    assert isinstance(first.get("sessions"), list), first
    r = await c.rpc(ctrl, "state")
    assert r["ok"] and r["result"]["ok"], r
    print(f"ctrl channel ok — hello v{hello['version']}, rpc state ok")

    # ---- create + attach
    r = await c.rpc(ctrl, "sessions.create",
                    {"cwd": CWD, "model": "haiku", "title": "relay-e2e"})
    assert r["ok"], r
    sid = r["result"]["sid"]
    await c.send_json(ctrl, {"type": "attach", "session_id": sid,
                             "since_seq": 0})
    att = await c.wait_type(ctrl, "attached")
    assert att["session_id"] == sid, att
    print(f"attached — sid {sid}")

    # ---- scenario 1: plain text turn streams through the relay
    saw = await run_turn(c, ctrl, sid,
                         "Reply with exactly the single word: pong")
    assert saw["text"] and "pong" in saw["text"].lower(), saw["text"]
    assert saw["deltas"] > 0, "no streaming deltas through the relay"
    assert saw["result"]["is_error"] is False
    seqs = saw["seqs"]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), \
        f"seq gap in live stream: {seqs}"
    print(f"S1 PASS — text={saw['text']!r} deltas={saw['deltas']} "
          f"seqs {seqs[0]}..{seqs[-1]} contiguous")

    # ---- scenario 2: permission approve through the relay
    saw = await run_turn(
        c, ctrl, sid,
        "Use the Bash tool to run exactly: echo relay-rc-77\n"
        "Then tell me it worked.")
    assert saw["perm"], "no permission request arrived"
    assert "relay-rc-77" in saw["tool_out"], saw["tool_out"]
    print(f"S2 PASS — permission approved, tool_out={saw['tool_out'].strip()!r}")

    # ---- scenario 3: True View terminal over a term channel
    term = await c.open("term")
    await c.send_json(term, {"type": "term_attach", "sid": sid})
    buf = bytearray()
    while not (len(buf) > 800 and b"\x1b[" in buf):
        ptype, body = await c.next_data(term, timeout=45)
        if ptype == proto.PTYPE_RAW:
            buf.extend(body)
    print(f"  TUI drew {len(buf)} bytes through the relay")
    await c.send_json(term, {"type": "input", "data": "relayecho"})
    while b"relayecho" not in buf:
        ptype, body = await c.next_data(term, timeout=20)
        if ptype == proto.PTYPE_RAW:
            buf.extend(body)
    print("  typed input echoed back through the relay")
    await c.send_json(term, {"type": "kill"})
    exited = False
    while True:
        try:
            ptype, body = await c.next_data(term, timeout=15)
        except ConnectionError:
            break
        if ptype == proto.PTYPE_JSON and \
                json.loads(body).get("type") == "exit":
            exited = True
            break
    assert exited, "no exit control message after kill"
    closed = await c.next_ctrl("close", timeout=10)
    assert closed.get("ch") == term, closed
    print("S3 PASS — term echo + kill + channel close")

    # ---- cleanup the session (before we start kicking agents around)
    r = await c.rpc(ctrl, "sessions.delete", {"sid": sid})
    assert r["ok"], r

    # ---- scenario 4: frame cap kills the violator (not the agent)
    v = FakeClient(RELAY, MACHINE)
    await v.connect()
    vch = await v.open("ctrl")
    await v.ws.send(proto.pack_header(proto.KIND_DATA, vch)
                    + b"x" * (proto.FRAME_MAX_WIRE + 1))
    for _ in range(100):
        if v.close_code is not None:
            break
        await asyncio.sleep(0.1)
    assert v.close_code == 1009, f"expected 1009, got {v.close_code}"
    assert MACHINE in relay_mod.ROUTER.agents, "agent link was harmed"
    print("S4 PASS — oversized frame → violator closed 1009, agent intact")

    # ---- scenario 5: duplicate agent connect → kick old (4409), accept new
    raw2 = await websockets.connect(
        f"{RELAY}/agent/ws", additional_headers={"X-BCRC-Agent": MACHINE},
        max_size=proto.FRAME_MAX_WIRE)
    await raw2.send(proto.pack_frame(
        proto.KIND_CTRL, 0,
        json.dumps({"type": "hello", "proto": proto.PROTO}).encode()))
    kind, _ch, payload = proto.unpack_frame(await asyncio.wait_for(
        raw2.recv(), 10))
    welcome2 = json.loads(payload)
    assert kind == proto.KIND_CTRL and welcome2["type"] == "welcome", welcome2
    # the client's ctrl channel rode the old uplink → relay closes it
    closed = await c.next_ctrl("close", timeout=10)
    assert closed.get("reason") == "agent_replaced", closed
    assert relay_mod.ROUTER.agents[MACHINE].ws is not None
    assert not utask.done(), "kicked uplink must keep retrying (30s backoff)"
    await raw2.close()
    await c.wait_status(False, timeout=10)  # replacement agent left
    print("S5 PASS — duplicate agent kicked old uplink (4409), new accepted")

    # ---- teardown
    await c.close()
    await v.close()
    utask.cancel()
    try:
        await utask
    except asyncio.CancelledError:
        pass
    relay_server.should_exit = True
    await relay_task
    print("\nRELAY E2E PASS")


asyncio.run(main())
