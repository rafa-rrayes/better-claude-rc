"""Relay chaos e2e: reliability invariants through relay restarts and agent
freezes (plan §reliability), with the REAL E2E layer (M4): the device pairs
via the QR code the agent subprocess printed to its terminal, then every
reconnect runs a fresh handshake before sealed traffic resumes.

  1. Kill + restart the relay MID-TURN: the agent reconnects (full-jitter
     backoff), the client reconnects (fresh handshake), and
     attach{since_seq} replay yields a gap-free seq sequence — no event
     lost, the turn's result arrives.
  2. SIGSTOP the agent: relay pings go unanswered → link reaped →
     machine_status{online:false} flows to the client. SIGCONT → agent
     reconnects → machine_status{online:true}; channels reopen (fresh
     handshake again) and attach{since_seq} still replays clean.

The agent runs `server.cli connect` as a real subprocess (so it can be
SIGSTOPped); the relay runs in-process with 1s/2s WS keepalive so reaping is
fast. Costs one haiku turn. Run: uv run python scripts/e2e_relay_reconnect.py
"""

import asyncio
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.main import app as platform_app  # noqa: E402
from relay_client import DeviceSim, FakeClient  # noqa: E402

HOST, RPORT, APORT = "127.0.0.1", 8809, 8810
RELAY = f"ws://{HOST}:{RPORT}"
MACHINE = "reconn-mach"
CWD = str(ROOT / "data" / "e2e-playground")
Path(CWD).mkdir(parents=True, exist_ok=True)

PROMPT = ("Count from 1 to 30 in English words, one word per line. "
          "No other text.")


async def start_relay():
    server = uvicorn.Server(uvicorn.Config(
        platform_app, host=HOST, port=RPORT, log_level="warning",
        ws_ping_interval=1.0, ws_ping_timeout=2.0,
        timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    return server, task


async def stop_relay(server, task):
    server.should_exit = True
    try:
        await asyncio.wait_for(task, 10)
    except asyncio.TimeoutError:
        server.force_exit = True
        await task


def start_agent(data_dir: str) -> subprocess.Popen:
    env = {**os.environ,
           "BCRC_DATA_DIR": data_dir,
           "BCRC_MACHINE_ID": MACHINE,
           "BCRC_PING_INTERVAL": "1",
           "BCRC_PING_TIMEOUT": "2"}
    logf = open(Path(data_dir) / "agent.log", "wb")
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "server.cli", "connect", "--no-qr",
         "--relay", RELAY, "--port", str(APORT)],
        cwd=str(ROOT), env=env,
        stdout=logf, stderr=subprocess.STDOUT)


async def read_pairing_code(data_dir: str, timeout: float = 30.0) -> str:
    """First run prints the pairing code (BCRC1.…) to the agent terminal —
    exactly what a user would scan/paste. Fish it out of the log."""
    log_path = Path(data_dir) / "agent.log"
    deadline = asyncio.get_running_loop().time() + timeout
    pat = re.compile(r"(BCRC1\.[^.\s]+\.[^.\s]+\.[^.\s]+)")
    while asyncio.get_running_loop().time() < deadline:
        if log_path.exists():
            match = pat.search(log_path.read_text(errors="replace"))
            if match:
                return match.group(1)
        await asyncio.sleep(0.2)
    raise AssertionError("agent never printed a pairing code")


async def open_ctrl(c: FakeClient, pair_qr: str | None = None) -> int:
    """Open the ctrl channel + fresh E2E handshake (pairing on first use)."""
    ctrl = await c.open("ctrl")
    await c.handshake(ctrl, pair_qr=pair_qr)
    await c.wait_type(ctrl, "hello")
    await c.wait_type(ctrl, "sessions")
    return ctrl


def note_evs(collected: dict, evs) -> None:
    """Record raw ev dicts (the attached replay payload)."""
    for ev in evs:
        if ev.get("seq") is not None:
            collected[ev["seq"]] = ev.get("kind")


def note_msgs(collected: dict, msgs, sid: str) -> None:
    """Record wrapped {"type":"event","session_id",ev} live messages."""
    for msg in msgs:
        if msg.get("type") == "event" and msg.get("session_id") == sid:
            note_evs(collected, [msg["ev"]])


async def main():
    data_dir = tempfile.mkdtemp(prefix="bcrc-reconn-")
    relay_server, relay_task = await start_relay()
    agent = start_agent(data_dir)
    collected: dict[int, str] = {}  # seq -> kind
    saw_result = False

    try:
        # ---- phase 0: pair the device with the code the agent printed
        qr = await read_pairing_code(data_dir)
        dev = DeviceSim()
        print(f"pairing code captured from agent terminal ({qr[:24]}…)")

        # ---- phase 1: connect, create a session, start a turn
        c = FakeClient(RELAY, MACHINE, device=dev)
        await c.connect()
        await c.wait_status(True, timeout=30)
        print("agent subprocess online through relay")
        ctrl = await open_ctrl(c, pair_qr=qr)
        print("device paired + E2E handshake established")
        r = await c.rpc(ctrl, "sessions.create",
                        {"cwd": CWD, "model": "haiku", "title": "reconn-e2e"})
        assert r["ok"], r
        sid = r["result"]["sid"]
        sink: list = []
        await c.send_json(ctrl, {"type": "attach", "session_id": sid,
                                 "since_seq": 0})
        att = await c.wait_type(ctrl, "attached", sink=sink)
        note_evs(collected, att.get("events") or [])
        note_msgs(collected, sink, sid)
        await c.send_json(ctrl, {"type": "send", "session_id": sid,
                                 "text": PROMPT})
        # wait until the turn is visibly streaming (first delta)
        while True:
            msg = await c.next_json(ctrl, timeout=60)
            note_msgs(collected, [msg], sid)
            if msg.get("type") == "event" and msg.get("session_id") == sid \
                    and msg["ev"].get("kind") in ("delta", "text"):
                break
        print(f"turn streaming (seqs so far: {sorted(collected)})")

        # ---- kill the relay MID-TURN, then bring a new one up
        await stop_relay(relay_server, relay_task)
        print("relay killed mid-turn")
        await asyncio.sleep(1.0)
        relay_server, relay_task = await start_relay()
        print("relay restarted")

        # ---- client + agent both reconnect; attach{since_seq} replays
        # (same paired device, fresh per-connection handshake)
        c2 = FakeClient(RELAY, MACHINE, device=dev)
        await c2.connect(timeout=30)
        await c2.wait_status(True, timeout=60)
        print("agent re-connected to the new relay")
        ctrl2 = await open_ctrl(c2)
        since = max(collected, default=0)
        sink2: list = []
        await c2.send_json(ctrl2, {"type": "attach", "session_id": sid,
                                   "since_seq": since})
        att2 = await c2.wait_type(ctrl2, "attached", sink=sink2)
        note_evs(collected, att2.get("events") or [])
        note_msgs(collected, sink2, sid)
        saw_result = any(k == "result" for k in collected.values())
        deadline = asyncio.get_running_loop().time() + 120
        while not saw_result:
            msg = await c2.next_json(
                ctrl2, timeout=deadline - asyncio.get_running_loop().time())
            note_msgs(collected, [msg], sid)
            if msg.get("type") == "event" and msg.get("session_id") == sid \
                    and msg["ev"].get("kind") == "result":
                saw_result = True

        seqs = sorted(collected)
        assert seqs and seqs[0] == 1, f"replay must start at seq 1: {seqs[:5]}"
        gaps = [s for a, b in zip(seqs, seqs[1:]) if b - a > 1
                for s in range(a + 1, b)]
        assert not gaps, f"seq gaps across relay restart: {gaps}"
        assert saw_result, "turn result never arrived"
        print(f"PHASE 1 PASS — {len(seqs)} seq events 1..{seqs[-1]}, "
              "gap-free across relay restart; result delivered")

        # ---- phase 2: SIGSTOP the agent → offline; SIGCONT → online
        agent.send_signal(signal.SIGSTOP)
        await c2.wait_status(False, timeout=30)
        print("PHASE 2a PASS — SIGSTOP → machine_status offline")
        agent.send_signal(signal.SIGCONT)
        await c2.wait_status(True, timeout=60)
        print("PHASE 2b PASS — SIGCONT → machine_status online")

        # channels died with the old agent link — reopen and re-attach
        ctrl3 = await open_ctrl(c2)
        await c2.send_json(ctrl3, {"type": "attach", "session_id": sid,
                                   "since_seq": max(collected)})
        att3 = await c2.wait_type(ctrl3, "attached")
        extra = [e["seq"] for e in (att3.get("events") or [])
                 if e.get("seq") is not None]
        assert all(s > max(collected) for s in extra), extra
        print("re-attach after freeze ok — no replay gaps")

        r = await c2.rpc(ctrl3, "sessions.delete", {"sid": sid})
        assert r["ok"], r
        await c2.close()
        await c.close()
        print("\nRELAY RECONNECT E2E PASS")
    finally:
        agent.send_signal(signal.SIGCONT)  # in case we failed while stopped
        agent.terminate()
        try:
            agent.wait(10)
        except subprocess.TimeoutExpired:
            agent.kill()
        await stop_relay(relay_server, relay_task)


asyncio.run(main())
