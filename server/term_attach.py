"""Transport-agnostic True View attach — the body of the old ws_terminal
endpoint, shared by the local WebSocket route (main.py) and relay term
channels (connect.py).

The transport hides how bytes reach the client:

    send_bytes(data)  raw PTY output (local: binary WS frame; relay: DATA
                      frame with ptype=RAW, chunked under the frame cap)
    send_json(obj)    control messages {"type": "exit"|"pong"} (local: text
                      WS frame; relay: DATA frame with ptype=JSON)
    recv_json()       next client control dict ({input,resize,ping,kill}),
                      or None once the client is gone
    close()           tear down the client leg (WS close / channel close)

The sid is a plain argument: locally it comes from the URL path, over the
relay it arrives *inside* the channel via the term_attach message — the
relay never learns session ids.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from .sessions import BUSY_STATES, SessionManager
from .terminal import TerminalError, TerminalManager, claude_argv


class TermTransport(Protocol):
    async def send_bytes(self, data: bytes) -> None: ...
    async def send_json(self, obj: dict) -> None: ...
    async def recv_json(self) -> dict | None: ...
    async def close(self) -> None: ...


async def attach_terminal(m: SessionManager, terms: TerminalManager,
                          sid: str, transport: TermTransport) -> None:
    """Attach one client to the session's PTY terminal, spawning
    `claude --resume <claude_id>` on first attach. Returns when the client
    leg is gone (transport closed / recv_json() -> None)."""

    async def bail(reason: str) -> None:
        await transport.send_json({"type": "exit", "reason": reason})
        await transport.close()

    runner = m.get(sid)
    if runner is None:
        await bail("No such session")
        return
    if not runner.claude_ids:
        await bail("No Claude session yet — send a message first")
        return
    if runner.client is not None and runner.state in BUSY_STATES:
        await bail("Session is busy in the app — wait for the turn to finish")
        return
    if runner.client is not None:
        await runner.detach_for_terminal()
    term = terms.get(sid)
    if term is None:
        try:
            argv = claude_argv(runner.claude_ids[-1], runner.model, runner.mode)
            term = terms.open(sid, argv, runner.cwd)
        except (TerminalError, OSError) as e:
            await bail(f"Could not start terminal: {e}")
            return

    q: asyncio.Queue = asyncio.Queue()
    replay = term.attach(q)

    async def pump() -> None:
        if replay:
            await transport.send_bytes(replay)
        while True:
            item = await q.get()
            if isinstance(item, dict):
                await transport.send_json(item)
                if item.get("type") == "exit":
                    await transport.close()
                    return
            else:
                await transport.send_bytes(item)

    sender = asyncio.create_task(pump())
    try:
        while True:
            data = await transport.recv_json()
            if data is None:
                break
            dtype = data.get("type")
            if dtype == "input":
                term.write(str(data.get("data", "")).encode())
            elif dtype == "resize":
                term.resize(int(data.get("cols") or 0), int(data.get("rows") or 0))
            elif dtype == "ping":
                q.put_nowait({"type": "pong"})
            elif dtype == "kill":
                terms.close(sid, "Terminal ended")
    finally:
        term.detach(q)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
