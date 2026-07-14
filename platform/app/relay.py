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

Auth (M3, plan §credentials):
  agent leg  — X-BCRC-Agent: <agent_id>.<ts>.<Ed25519 sig over method|path|ts>
               verified against the machine registry pubkey; ±60s window +
               replay cache. Revoked machines are refused (4401).
  client leg — __Host-bcrc_sess cookie + Origin check at upgrade; the relay
               enforces machine.user_id == session.user_id (4403).
  Without a configured database (local dev / relay-only e2e) both legs fall
  back to M2's permissive behavior — loudly logged, never in production.

Quotas (plan §data model): live caps enforced here in-memory — concurrent
client connections per machine and term channels per machine, by the owning
user's plan (delivered to both legs via CTRL welcome{plan}). The machine cap
itself is enforced at enroll time in Postgres.

Backpressure (plan §backpressure): client queue 512 / agent queue 1024
frames; overflow closes the slow CLIENT with 4408 — never the agent link.
Recovery is always safe: reconnect + attach{since_seq} replay.

Heartbeats are protocol-level WS pings, configured where uvicorn is started
(ws_ping_interval=20, ws_ping_timeout=25 → dead legs reaped within ~45s).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import itertools
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from server import proto

from . import metrics, quotas
from .db import Db
from .deps import SESSION_COOKIE
from .limits import SlidingWindow

log = logging.getLogger("bcrc.relay")
router = APIRouter()

CLIENT_QUEUE_MAX = 512
AGENT_QUEUE_MAX = 1024

SIG_WINDOW_S = 60          # signed-header freshness (±)
WS_CONNECT_LIMIT = 10      # client WS connects per IP per minute

# Set by app startup when PLATFORM_DATABASE_URL is configured; None keeps
# the permissive dev behavior for DB-less runs (local relay e2e).
DB: Db | None = None

_ws_connect_window = SlidingWindow(WS_CONNECT_LIMIT, 60.0)
_seen_sigs: dict[str, float] = {}  # replay cache: sig -> wall-clock expiry

# Frame tap for e2e blindness checks (M4 greps this for plaintext markers):
# when BCRC_RELAY_TAP is set, every relayed frame's raw bytes are appended.
_TAP_PATH = os.environ.get("BCRC_RELAY_TAP")


def _tap(direction: str, frame: bytes) -> None:
    metrics.FRAMES_RELAYED.labels(direction).inc()
    metrics.BYTES_RELAYED.labels(direction).inc(len(frame))
    if _TAP_PATH:
        with open(_TAP_PATH, "ab") as f:
            f.write(direction.encode() + b" " + frame + b"\n")


def ctrl_frame(msg: dict) -> bytes:
    return proto.pack_frame(proto.KIND_CTRL, 0, json.dumps(msg).encode())


async def _refuse(ws: WebSocket, leg: str, code: int, reason: str = "",
                  error: str | None = None) -> None:
    metrics.WS_CLOSES.labels(leg, str(code)).inc()
    if error is not None:
        with contextlib.suppress(Exception):
            await ws.send_bytes(ctrl_frame({"type": "error", "code": error}))
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=reason)


# ---------------- auth ----------------

def _verify_agent_header(raw: str) -> tuple[str, int, bytes] | None:
    """Parse <agent_id>.<ts>.<b64url sig>; enforce freshness + replay."""
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    agent_id, ts_s, sig_b64 = parts
    try:
        ts = int(ts_s)
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except (ValueError, binascii.Error):
        return None
    now = time.time()
    if abs(now - ts) > SIG_WINDOW_S:
        return None
    for k, exp in list(_seen_sigs.items()):  # prune + replay check
        if exp < now:
            _seen_sigs.pop(k, None)
    if sig_b64 in _seen_sigs:
        return None
    return agent_id, ts, sig


async def authenticate_agent(ws: WebSocket) -> tuple[str, str] | None:
    """-> (machine_id, plan) or None after closing the socket."""
    raw = ws.headers.get("x-bcrc-agent", "")
    if DB is None:
        log.warning("agent auth: no database configured — permissive dev mode")
        machine_id = raw.split(".", 1)[0].strip()
        if not machine_id:
            await _refuse(ws, "agent", proto.CLOSE_UNAUTHORIZED)
            return None
        return machine_id, "free"

    parsed = _verify_agent_header(raw)
    if parsed is None:
        await _refuse(ws, "agent", proto.CLOSE_UNAUTHORIZED, "bad auth")
        return None
    agent_id, ts, sig = parsed
    try:
        mid = uuid.UUID(agent_id)
    except ValueError:
        await _refuse(ws, "agent", proto.CLOSE_UNAUTHORIZED, "bad auth")
        return None
    row = await DB.machine_auth(mid)
    if row is None:  # unknown or revoked
        await _refuse(ws, "agent", proto.CLOSE_UNAUTHORIZED, "unknown machine")
        return None
    try:
        VerifyKey(bytes(row["ed25519_pk"])).verify(
            f"GET|/agent/ws|{ts}".encode(), sig)
    except BadSignatureError:
        await _refuse(ws, "agent", proto.CLOSE_UNAUTHORIZED, "bad signature")
        return None
    _seen_sigs[raw.split(".")[2]] = time.time() + 2 * SIG_WINDOW_S
    return str(row["id"]), row["plan"]


def _ws_origin_ok(ws: WebSocket) -> bool:
    """Browser WS upgrades must be same-origin (the PWA is served by this
    host). Absent Origin (non-browser client) passes — CSWSH is a browser
    attack and browsers always send Origin on upgrades."""
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    from urllib.parse import urlparse
    try:
        o = urlparse(origin)
    except ValueError:
        return False
    if o.scheme not in ("http", "https") or not o.netloc:
        return False
    host = ws.headers.get("host", "")

    def norm(netloc: str, scheme: str) -> str:
        if ":" in netloc:
            return netloc
        return f"{netloc}:{443 if scheme == 'https' else 80}"

    return norm(o.netloc, o.scheme) == norm(host, o.scheme)


async def authenticate_client(ws: WebSocket,
                              machine_id: str) -> tuple[str, str] | None:
    """-> (user_id, plan) or None after closing the socket. Enforces
    session cookie + Origin at upgrade and machine ownership
    (agent.user_id == device user_id, plan §threat model)."""
    if DB is None:
        log.warning("client auth: no database configured — permissive dev mode")
        return "anon", "free"
    ip = ws.client.host if ws.client else "?"
    if not _ws_connect_window.allow(ip):
        metrics.QUOTA_DENIALS.labels("ws_connect").inc()
        await _refuse(ws, "client", 1013, "rate limited")  # try again later
        return None
    if not _ws_origin_ok(ws):
        await _refuse(ws, "client", proto.CLOSE_FORBIDDEN, "bad origin")
        return None
    token = ws.cookies.get(SESSION_COOKIE)
    user = await DB.session_user(token) if token else None
    if user is None:
        await _refuse(ws, "client", proto.CLOSE_UNAUTHORIZED)
        return None
    try:
        mid = uuid.UUID(machine_id)
    except ValueError:
        await _refuse(ws, "client", proto.CLOSE_FORBIDDEN, "no such machine")
        return None
    machine = await DB.machine_auth(mid)
    if machine is None or machine["user_id"] != user["id"]:
        await _refuse(ws, "client", proto.CLOSE_FORBIDDEN, "no such machine")
        return None
    return str(user["id"]), user["plan"]


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
    def __init__(self, ws: WebSocket, machine: str, conn_id: int, plan: str):
        super().__init__(ws, CLIENT_QUEUE_MAX)
        self.machine = machine
        self.conn_id = conn_id
        self.plan = plan
        self.channels: set[int] = set()


class Router:
    """In-memory routing table. Process-local by design (plan: no Redis) —
    a single relay instance is authoritative for live quota counters."""

    def __init__(self) -> None:
        self.agents: dict[str, AgentLink] = {}
        self.clients: dict[str, set[ClientLink]] = {}
        self.channel_owner: dict[tuple[str, int], ClientLink] = {}
        self.channel_kind: dict[tuple[str, int], str] = {}
        self._next_ch: dict[str, int] = {}
        self._conn_ids = itertools.count(1)

    def alloc_channel(self, machine: str) -> int:
        n = self._next_ch.get(machine, 0) + 1
        self._next_ch[machine] = n
        return n

    def register_channel(self, machine: str, ch: int, kind: str,
                         owner: ClientLink) -> None:
        self.channel_owner[(machine, ch)] = owner
        self.channel_kind[(machine, ch)] = kind
        owner.channels.add(ch)
        metrics.CHANNELS_OPEN.labels(kind).inc()

    def unregister_channel(self, machine: str, ch: int) -> ClientLink | None:
        owner = self.channel_owner.pop((machine, ch), None)
        kind = self.channel_kind.pop((machine, ch), None)
        if owner is not None:
            owner.channels.discard(ch)
        if kind is not None:
            metrics.CHANNELS_OPEN.labels(kind).dec()
        return owner

    def term_channels(self, machine: str) -> int:
        return sum(1 for (m, _ch), kind in self.channel_kind.items()
                   if m == machine and kind == "term")


ROUTER = Router()


async def kick_machine(machine_id: str,
                       code: int = proto.CLOSE_UNAUTHORIZED) -> bool:
    """Dashboard revoke -> kill the live agent uplink. The agent_ws finally
    block handles channel teardown + machine_status broadcast."""
    link = ROUTER.agents.get(machine_id)
    if link is None:
        return False
    metrics.WS_CLOSES.labels("agent", str(code)).inc()
    await link.close(code, "revoked")
    return True


def _drop_client(link: ClientLink, code: int, reason: str) -> None:
    """Close a client link (slow consumer / violation). Mapping cleanup runs
    in its endpoint's finally once the receive loop observes the close."""
    if link.dead:
        return
    link.dead = True
    metrics.WS_CLOSES.labels("client", str(code)).inc()
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
    for (m_id, ch) in list(ROUTER.channel_owner):
        if m_id != machine:
            continue
        owner = ROUTER.unregister_channel(machine, ch)
        if owner is not None:
            _send_to_client(owner, ctrl_frame({"type": "close", "ch": ch,
                                               "reason": reason}))


# ---------------- frame receive helpers ----------------

async def _recv_frame(ws: WebSocket, leg: str) -> bytes | None:
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
            await _refuse(ws, leg, 1003, "binary frames only")
            return None
        if len(data) > proto.FRAME_MAX_WIRE:
            await _refuse(ws, leg, 1009, "frame too large")
            return None
        return data


async def _handshake(ws: WebSocket, leg: str) -> bool:
    """First frame must be CTRL hello{proto} with a supported proto
    (current + previous; otherwise 4426 upgrade required)."""
    frame = await _recv_frame(ws, leg)
    if frame is None:
        return False
    try:
        kind, _ch, payload = proto.unpack_frame(frame)
        msg = json.loads(payload)
    except (proto.FrameError, ValueError):
        await _refuse(ws, leg, 1008, "bad hello")
        return False
    if kind != proto.KIND_CTRL or msg.get("type") != "hello":
        await _refuse(ws, leg, 1008, "expected hello")
        return False
    if msg.get("proto") not in (proto.PROTO, proto.PROTO - 1):
        await _refuse(ws, leg, proto.CLOSE_UPGRADE_REQUIRED,
                      error="upgrade_required")
        return False
    return True


# ---------------- agent leg ----------------

@router.websocket("/agent/ws")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    auth = await authenticate_agent(ws)
    if auth is None:
        return
    machine, plan = auth
    if not await _handshake(ws, "agent"):
        return

    link = AgentLink(ws, machine)
    old = ROUTER.agents.get(machine)
    if old is not None:
        # Duplicate agent connect → kick old, accept new (4409). Zombie
        # socket after laptop sleep is the common case; a genuinely
        # duplicated agent flaps loudly (30s min backoff after 4409).
        log.info("agent %s: kicking duplicate uplink", machine)
        metrics.WS_CLOSES.labels("agent",
                                 str(proto.CLOSE_DUPLICATE_AGENT)).inc()
        _close_machine_channels(machine, "agent_replaced")
        await old.close(proto.CLOSE_DUPLICATE_AGENT, "duplicate agent")
    ROUTER.agents[machine] = link
    metrics.AGENTS_CONNECTED.inc()
    link.start()
    link.put_ctrl({"type": "welcome", "proto": proto.PROTO, "plan": plan})
    _broadcast_status(machine, True)
    log.info("agent %s online", machine)
    if DB is not None:
        await DB.touch_machine(uuid.UUID(machine))
        await DB.audit("agent.connected", machine_id=uuid.UUID(machine))

    try:
        while True:
            frame = await _recv_frame(ws, "agent")
            if frame is None:
                break
            if not _from_agent(link, frame):
                break
    finally:
        await link.close(1012)
        metrics.AGENTS_CONNECTED.dec()
        if ROUTER.agents.get(machine) is link:
            del ROUTER.agents[machine]
            _close_machine_channels(machine, "agent_gone")
            _broadcast_status(machine, False)
            log.info("agent %s offline", machine)
        if DB is not None:
            with contextlib.suppress(Exception):
                await DB.touch_machine(uuid.UUID(machine))


def _from_agent(link: AgentLink, frame: bytes) -> bool:
    """Route one frame from the agent. Returns False to drop the link."""
    try:
        kind, ch, payload = proto.unpack_frame(frame)
    except proto.FrameError:
        metrics.WS_CLOSES.labels("agent", "1008").inc()
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
            owner = ROUTER.unregister_channel(link.machine, ch)
            if owner is not None:
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
        await _refuse(ws, "client", proto.CLOSE_FORBIDDEN, "machine required")
        return
    auth = await authenticate_client(ws, machine)
    if auth is None:
        return
    _user_id, plan = auth
    if not await _handshake(ws, "client"):
        return

    # Live client-connection quota (plan §data model). The count check and
    # the register run back-to-back on one event loop — race-free.
    if DB is not None:
        alive = sum(1 for c in ROUTER.clients.get(machine, ()) if not c.dead)
        if alive >= quotas.client_cap(plan):
            metrics.QUOTA_DENIALS.labels("clients").inc()
            await _refuse(ws, "client", proto.CLOSE_FORBIDDEN,
                          "connection limit", error="quota_clients")
            return

    link = ClientLink(ws, machine, next(ROUTER._conn_ids), plan)
    ROUTER.clients.setdefault(machine, set()).add(link)
    metrics.CLIENTS_CONNECTED.inc()
    link.start()
    link.put_ctrl({"type": "welcome", "proto": proto.PROTO, "plan": plan})
    link.put_ctrl({"type": "machine_status",
                   "online": machine in ROUTER.agents})

    try:
        while True:
            frame = await _recv_frame(ws, "client")
            if frame is None:
                break
            if not _from_client(link, frame):
                break
    finally:
        await link.close(1012)
        metrics.CLIENTS_CONNECTED.dec()
        ROUTER.clients.get(machine, set()).discard(link)
        agent = ROUTER.agents.get(machine)
        for ch in list(link.channels):
            ROUTER.unregister_channel(machine, ch)
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
            # quota enforcement without reading payloads.
            kind_ = msg.get("kind")
            if kind_ not in ("ctrl", "term"):
                link.put_ctrl({"type": "error", "code": "bad_open"})
                return True
            agent = ROUTER.agents.get(machine)
            if agent is None:
                link.put_ctrl({"type": "error", "code": "machine_offline"})
                return True
            if DB is not None and kind_ == "term" and \
                    ROUTER.term_channels(machine) >= \
                    quotas.term_channel_cap(link.plan):
                metrics.QUOTA_DENIALS.labels("term").inc()
                link.put_ctrl({"type": "error", "code": "quota_term"})
                return True
            new_ch = ROUTER.alloc_channel(machine)
            ROUTER.register_channel(machine, new_ch, kind_, link)
            if not agent.try_put(ctrl_frame({"type": "open", "kind": kind_,
                                             "ch": new_ch,
                                             "conn": link.conn_id})):
                # Agent queue full: this client's traffic can't be delivered
                # — close the slow client, never the agent link.
                ROUTER.unregister_channel(machine, new_ch)
                _drop_client(link, proto.CLOSE_SLOW_CLIENT, "agent backlog")
                return False
            link.put_ctrl({"type": "opened", "ch": new_ch, "kind": kind_})
        elif mtype == "close":
            ch = int(msg.get("ch") or 0)
            if ROUTER.channel_owner.get((machine, ch)) is link:
                ROUTER.unregister_channel(machine, ch)
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
