"""Blind relay: routes opaque frames between agents (dialing out) and
browser clients (plan §relay).

WSS /agent/ws              one uplink per machine (kick-old on duplicate).
WSS /client/ws?machine=id  one per phone tab; connecting = opening a pipe
                           to that machine.

The relay parses ONLY CTRL frames (hello/welcome, open/opened, close,
machine_status, error). HS and DATA frames are forwarded byte-for-byte by a
dict lookup + put_nowait into bounded per-link send queues — the relay never
sees session ids, prompts, outputs, or keys (in M4 DATA becomes ciphertext;
nothing here changes because nothing here reads payloads).

Backpressure (plan §backpressure): client queue 512 / agent queue 1024
frames; overflow closes the slow CLIENT with 4408 — never the agent link.
Recovery is always safe: reconnect + attach{since_seq} replay.

Heartbeats are protocol-level WS pings, configured where uvicorn is started
(ws_ping_interval=20, ws_ping_timeout=25 → dead legs reaped within ~45s).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server import proto

log = logging.getLogger("bcrc.relay")
router = APIRouter()

CLIENT_QUEUE_MAX = 512
AGENT_QUEUE_MAX = 1024

# Frame tap for e2e blindness checks (M4 greps this for plaintext markers):
# when BCRC_RELAY_TAP is set, every relayed frame's raw bytes are appended.
_TAP_PATH = os.environ.get("BCRC_RELAY_TAP")


def _tap(direction: str, frame: bytes) -> None:
    if _TAP_PATH:
        with open(_TAP_PATH, "ab") as f:
            f.write(direction.encode() + b" " + frame + b"\n")


def ctrl_frame(msg: dict) -> bytes:
    return proto.pack_frame(proto.KIND_CTRL, 0, json.dumps(msg).encode())


# ---------------- auth seams (M3 replaces both) ----------------

async def authenticate_agent(ws: WebSocket) -> str | None:
    """M2 STUB — permissive. M3 replaces with signed-header verification:
    X-BCRC-Agent: <agent_id>.<ts>.<Ed25519 sig over method|path|ts>
    (±60s window + replay cache) checked against the machine registry.
    Returns the machine id, or None after closing the socket."""
    raw = ws.headers.get("x-bcrc-agent", "")
    machine_id = raw.split(".", 1)[0].strip()
    if not machine_id:
        await ws.close(code=proto.CLOSE_UNAUTHORIZED)
        return None
    return machine_id


async def authenticate_client(ws: WebSocket, machine_id: str) -> str | None:
    """M2 STUB — permissive. M3 replaces with __Host- session cookie +
    Origin validation at upgrade, and enforces agent.user_id == device.user_id.
    Returns the user id, or None after closing the socket."""
    return "anon"


def plan_for(machine_id: str) -> str:
    """M3 seam: look up the owning user's subscription plan."""
    return "free"


# ---------------- links + router ----------------

class Link:
    """One WS leg: bounded send queue drained by a sender task."""

    def __init__(self, ws: WebSocket, qsize: int):
        self.ws = ws
        self.q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=qsize)
        self.dead = False
        self._sender: asyncio.Task | None = None

    def start(self) -> None:
        self._sender = asyncio.get_running_loop().create_task(self._send_loop())

    async def _send_loop(self) -> None:
        try:
            while True:
                frame = await self.q.get()
                await self.ws.send_bytes(frame)
        except Exception:
            self.dead = True

    def try_put(self, frame: bytes) -> bool:
        if self.dead:
            return False
        try:
            self.q.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False

    def put_ctrl(self, msg: dict) -> bool:
        return self.try_put(ctrl_frame(msg))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.dead = True
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender
            self._sender = None
        with contextlib.suppress(Exception):
            await self.ws.close(code=code, reason=reason)


class AgentLink(Link):
    def __init__(self, ws: WebSocket, machine: str):
        super().__init__(ws, AGENT_QUEUE_MAX)
        self.machine = machine


class ClientLink(Link):
    def __init__(self, ws: WebSocket, machine: str, conn_id: int):
        super().__init__(ws, CLIENT_QUEUE_MAX)
        self.machine = machine
        self.conn_id = conn_id
        self.channels: set[int] = set()


class Router:
    """In-memory routing table. Process-local by design (plan: no Redis) —
    a single relay instance is authoritative for live quota counters."""

    def __init__(self) -> None:
        self.agents: dict[str, AgentLink] = {}
        self.clients: dict[str, set[ClientLink]] = {}
        self.channel_owner: dict[tuple[str, int], ClientLink] = {}
        self._next_ch: dict[str, int] = {}
        self._conn_ids = itertools.count(1)

    def alloc_channel(self, machine: str) -> int:
        n = self._next_ch.get(machine, 0) + 1
        self._next_ch[machine] = n
        return n


ROUTER = Router()


def _drop_client(link: ClientLink, code: int, reason: str) -> None:
    """Close a client link (slow consumer / violation). Mapping cleanup runs
    in its endpoint's finally once the receive loop observes the close."""
    if link.dead:
        return
    link.dead = True
    asyncio.get_running_loop().create_task(link.close(code, reason))


def _send_to_client(link: ClientLink, frame: bytes) -> None:
    if not link.try_put(frame):
        _drop_client(link, proto.CLOSE_SLOW_CLIENT, "slow client")


def _broadcast_status(machine: str, online: bool) -> None:
    for link in list(ROUTER.clients.get(machine, ())):
        _send_to_client(link, ctrl_frame({"type": "machine_status",
                                          "online": online}))


def _close_machine_channels(machine: str, reason: str) -> None:
    """Agent link went away — every channel on it is dead. Notify owners;
    client WSs stay open (frontend reopens channels on online:true)."""
    for (m_id, ch), owner in list(ROUTER.channel_owner.items()):
        if m_id != machine:
            continue
        ROUTER.channel_owner.pop((m_id, ch), None)
        owner.channels.discard(ch)
        _send_to_client(owner, ctrl_frame({"type": "close", "ch": ch,
                                           "reason": reason}))


# ---------------- frame receive helpers ----------------

async def _recv_frame(ws: WebSocket) -> bytes | None:
    """Next binary frame, or None once the leg is gone. Enforces the wire
    frame cap (kill violators, 1009) and binary-only framing (1003)."""
    while True:
        try:
            msg = await ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if msg["type"] == "websocket.disconnect":
            return None
        data = msg.get("bytes")
        if data is None:
            await ws.close(code=1003, reason="binary frames only")
            return None
        if len(data) > proto.FRAME_MAX_WIRE:
            await ws.close(code=1009, reason="frame too large")
            return None
        return data


async def _handshake(ws: WebSocket) -> bool:
    """First frame must be CTRL hello{proto} with a supported proto
    (current + previous; otherwise 4426 upgrade required)."""
    frame = await _recv_frame(ws)
    if frame is None:
        return False
    try:
        kind, _ch, payload = proto.unpack_frame(frame)
        msg = json.loads(payload)
    except (proto.FrameError, ValueError):
        await ws.close(code=1008, reason="bad hello")
        return False
    if kind != proto.KIND_CTRL or msg.get("type") != "hello":
        await ws.close(code=1008, reason="expected hello")
        return False
    if msg.get("proto") not in (proto.PROTO, proto.PROTO - 1):
        with contextlib.suppress(Exception):
            await ws.send_bytes(ctrl_frame({"type": "error",
                                            "code": "upgrade_required"}))
        await ws.close(code=proto.CLOSE_UPGRADE_REQUIRED)
        return False
    return True


# ---------------- agent leg ----------------

@router.websocket("/agent/ws")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    machine = await authenticate_agent(ws)
    if machine is None:
        return
    if not await _handshake(ws):
        return

    link = AgentLink(ws, machine)
    old = ROUTER.agents.get(machine)
    if old is not None:
        # Duplicate agent connect → kick old, accept new (4409). Zombie
        # socket after laptop sleep is the common case; a genuinely
        # duplicated agent flaps loudly (30s min backoff after 4409).
        log.info("agent %s: kicking duplicate uplink", machine)
        _close_machine_channels(machine, "agent_replaced")
        await old.close(proto.CLOSE_DUPLICATE_AGENT, "duplicate agent")
    ROUTER.agents[machine] = link
    link.start()
    link.put_ctrl({"type": "welcome", "proto": proto.PROTO,
                   "plan": plan_for(machine)})
    _broadcast_status(machine, True)
    log.info("agent %s online", machine)

    try:
        while True:
            frame = await _recv_frame(ws)
            if frame is None:
                break
            if not _from_agent(link, frame):
                break
    finally:
        await link.close(1012)
        if ROUTER.agents.get(machine) is link:
            del ROUTER.agents[machine]
            _close_machine_channels(machine, "agent_gone")
            _broadcast_status(machine, False)
            log.info("agent %s offline", machine)


def _from_agent(link: AgentLink, frame: bytes) -> bool:
    """Route one frame from the agent. Returns False to drop the link."""
    try:
        kind, ch, payload = proto.unpack_frame(frame)
    except proto.FrameError:
        asyncio.get_running_loop().create_task(
            link.close(1008, "bad frame"))
        return False
    if kind == proto.KIND_CTRL:
        try:
            msg = json.loads(payload)
        except ValueError:
            return True
        if msg.get("type") == "close":
            ch = int(msg.get("ch") or 0)
            owner = ROUTER.channel_owner.pop((link.machine, ch), None)
            if owner is not None:
                owner.channels.discard(ch)
                _send_to_client(owner, frame)
        return True
    # HS / DATA — forwarded opaque to the channel owner
    owner = ROUTER.channel_owner.get((link.machine, ch))
    if owner is None:
        return True  # racing a channel close — drop
    _tap("a>c", frame)
    _send_to_client(owner, frame)
    return True


# ---------------- client leg ----------------

@router.websocket("/client/ws")
async def client_ws(ws: WebSocket):
    await ws.accept()
    machine = ws.query_params.get("machine", "").strip()
    if not machine:
        await ws.close(code=proto.CLOSE_FORBIDDEN, reason="machine required")
        return
    user = await authenticate_client(ws, machine)
    if user is None:
        return
    if not await _handshake(ws):
        return

    link = ClientLink(ws, machine, next(ROUTER._conn_ids))
    ROUTER.clients.setdefault(machine, set()).add(link)
    link.start()
    link.put_ctrl({"type": "welcome", "proto": proto.PROTO,
                   "plan": plan_for(machine)})
    link.put_ctrl({"type": "machine_status",
                   "online": machine in ROUTER.agents})

    try:
        while True:
            frame = await _recv_frame(ws)
            if frame is None:
                break
            if not _from_client(link, frame):
                break
    finally:
        await link.close(1012)
        ROUTER.clients.get(machine, set()).discard(link)
        agent = ROUTER.agents.get(machine)
        for ch in list(link.channels):
            ROUTER.channel_owner.pop((machine, ch), None)
            if agent is not None:  # best effort — agent reaps via close
                agent.try_put(ctrl_frame({"type": "close", "ch": ch,
                                          "reason": "client_gone"}))


def _from_client(link: ClientLink, frame: bytes) -> bool:
    """Route one frame from a client. Returns False to drop the link."""
    try:
        kind, ch, payload = proto.unpack_frame(frame)
    except proto.FrameError:
        _drop_client(link, 1008, "bad frame")
        return False
    machine = link.machine
    if kind == proto.KIND_CTRL:
        try:
            msg = json.loads(payload)
        except ValueError:
            return True
        mtype = msg.get("type")
        if mtype == "open":
            # open{kind} exists so the blind relay can count channels for
            # quota enforcement (M3: live caps by plan).
            kind_ = msg.get("kind")
            if kind_ not in ("ctrl", "term"):
                link.put_ctrl({"type": "error", "code": "bad_open"})
                return True
            agent = ROUTER.agents.get(machine)
            if agent is None:
                link.put_ctrl({"type": "error", "code": "machine_offline"})
                return True
            new_ch = ROUTER.alloc_channel(machine)
            ROUTER.channel_owner[(machine, new_ch)] = link
            link.channels.add(new_ch)
            if not agent.try_put(ctrl_frame({"type": "open", "kind": kind_,
                                             "ch": new_ch,
                                             "conn": link.conn_id})):
                # Agent queue full: this client's traffic can't be delivered
                # — close the slow client, never the agent link.
                ROUTER.channel_owner.pop((machine, new_ch), None)
                link.channels.discard(new_ch)
                _drop_client(link, proto.CLOSE_SLOW_CLIENT, "agent backlog")
                return False
            link.put_ctrl({"type": "opened", "ch": new_ch, "kind": kind_})
        elif mtype == "close":
            ch = int(msg.get("ch") or 0)
            if ROUTER.channel_owner.get((machine, ch)) is link:
                ROUTER.channel_owner.pop((machine, ch), None)
                link.channels.discard(ch)
                agent = ROUTER.agents.get(machine)
                if agent is not None:
                    agent.try_put(frame)
        return True
    # HS / DATA — must ride a channel this client owns
    owner = ROUTER.channel_owner.get((machine, ch))
    if owner is None:
        return True  # racing a close — drop
    if owner is not link:
        _drop_client(link, proto.CLOSE_FORBIDDEN, "channel not owned")
        return False
    agent = ROUTER.agents.get(machine)
    if agent is None:
        return True  # agent just died; client gets machine_status offline
    _tap("c>a", frame)
    if not agent.try_put(frame):
        _drop_client(link, proto.CLOSE_SLOW_CLIENT, "agent backlog")
        return False
    return True
