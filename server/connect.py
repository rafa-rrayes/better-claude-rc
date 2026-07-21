"""Connect mode: the agent dials OUT to the platform relay (no inbound
ports) and bridges relay channels to the existing session/terminal engine.

One WS uplink carries all browser clients, multiplexed by channel. The relay
tags each open{} with an opaque per-client connection id, and the agent
keeps one MuxConn + one identity.AgentSession per client: every DATA payload
flows through that session's E2ECodec (crypto_secretstream, keys from the
per-connection handshake). Ciphertext is mandatory — a client that skips the
handshake or sends plaintext gets CodecError → hard close. KIND_HS frames
(pairing + session handshake JSON) ride the client's ctrl channel and are
the only client bytes the agent reads before the codec is established.

Channel kinds:
  ctrl — carries the existing /ws JSON vocabulary unchanged (attach/send/
         permission/rpc/…), dispatched through main.handle_ws. The agent
         pushes hello + sessions once the handshake establishes, mirroring
         the local WS.
  term — carries today's /ws/term traffic: ptype RAW = PTY output,
         ptype JSON = {input,resize,ping,kill,exit}. The sid arrives inside
         the ciphertext via {"type":"term_attach","sid":…} — the relay never
         learns session ids.

Reliability (plan §reliability): full-jitter exponential backoff (cap 60s,
retry forever); 30s minimum after a 4409 kick; stop with re-enroll
instructions on 4401/4403; stop with an upgrade hint on 4426.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random

import websockets

from . import VERSION, identity as identity_mod, proto
from . import main as main_mod
from .codec import CodecError
from .config import Config
from .mux import ChannelClosed, MuxConn
from .sessions import SessionManager
from .term_attach import attach_terminal
from .terminal import TerminalManager

log = logging.getLogger("bcrc.connect")

HELLO_TIMEOUT = 15.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
KICK_MIN_BACKOFF = 30.0  # after a 4409 duplicate-agent kick

# Live Claude sessions per plan (plan §data model: free tier runs 1). The
# plan rides CTRL welcome{plan}; agent-enforced, soft by design — applied at
# create time only, nothing is killed on downgrade (the machine cap is the
# real lever). Local serve mode has no Uplink, so it is never capped.
SESSION_CAPS = {"free": 1, "pro": 10}

_FATAL_UNAUTHORIZED = (proto.CLOSE_UNAUTHORIZED, proto.CLOSE_FORBIDDEN)


def agent_auth_header(machine_id: str, identity=None) -> str:
    """Signed header X-BCRC-Agent: <agent_id>.<ts>.<Ed25519 sig over
    method|path|ts> (server/identity.py). An identity without a machine id
    (never enrolled — DB-less dev relay, loopback e2e) degrades to the bare
    machine id, which a production relay refuses."""
    if identity is not None and identity.machine_id:
        return identity.auth_header("GET", "/agent/ws")
    return machine_id


# ---------------- per-client state ----------------

_TRANSIENT_TYPES = ("sessions", "pong")


class ChannelConn:
    """Quacks like main.WSConn for SessionManager broadcasts + handle_ws,
    but pushes into a mux channel (through the client's codec)."""

    def __init__(self, client: "ClientState", ch: int):
        self.client = client
        self.ch = ch
        self.subs: set[str] = set()

    def push(self, payload: dict) -> None:
        try:
            self._push(payload)
        except ChannelClosed:
            pass  # channel teardown deregisters us from manager.conns

    def _push(self, payload: dict) -> None:
        t = payload.get("type")
        transient = t in _TRANSIENT_TYPES or (
            t == "event" and (payload.get("ev") or {}).get("seq") is None)
        mux = self.client.mux
        try:
            mux.send_json(self.ch, payload, transient=transient)
        except ValueError:  # exceeds the frame cap — degrade per type
            if t == "attached":
                # Replay too big for one frame: send the attached head, then
                # stream the replayed events individually (same semantics —
                # the client appends event pushes after attached).
                events = payload.get("events") or []
                mux.send_json(self.ch, {**payload, "events": []})
                sid = payload.get("session_id")
                for ev in events:
                    with contextlib.suppress(ValueError):
                        mux.send_json(self.ch, {"type": "event",
                                                "session_id": sid, "ev": ev})
            elif t == "rpc_result":
                mux.send_json(self.ch, {
                    "type": "rpc_result", "id": payload.get("id"),
                    "ok": False,
                    "error": {"code": "too_large",
                              "message": "result exceeds frame cap"}})
            # anything else oversized: drop (nothing replayable is)


class TermChannel:
    def __init__(self, client: "ClientState", ch: int):
        self.client = client
        self.ch = ch
        self.inbox: asyncio.Queue[dict | None] = asyncio.Queue()
        self.attached = False
        self.task: asyncio.Task | None = None


class _RelayTermTransport:
    """TermTransport over a relay term channel (see term_attach.py)."""

    CHUNK = 64 * 1024  # PTY reads are <=64 KiB; scrollback replay is bigger

    def __init__(self, uplink: "Uplink", client: "ClientState",
                 chan: TermChannel):
        self.uplink = uplink
        self.client = client
        self.chan = chan

    async def send_bytes(self, data: bytes) -> None:
        try:
            for i in range(0, len(data), self.CHUNK):
                self.client.mux.send_raw(self.chan.ch, data[i:i + self.CHUNK])
        except ChannelClosed:
            pass

    async def send_json(self, obj: dict) -> None:
        with contextlib.suppress(ChannelClosed):
            self.client.mux.send_json(self.chan.ch, obj)

    async def recv_json(self) -> dict | None:
        return await self.chan.inbox.get()

    async def close(self) -> None:
        # Let queued frames (the exit control message) egress before
        # teardown clears the channel queue.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        mux = self.client.mux
        while mux.pending(self.chan.ch) and loop.time() < deadline \
                and not mux.closed:
            await asyncio.sleep(0.01)
        await self.uplink.teardown_channel(self.chan.ch, "closed",
                                           notify_relay=True)


class ClientState:
    """One relay-side browser connection: its E2E session (codec), MuxConn
    and channels. Nothing is pushed to the ctrl channel until the session
    is established — the codec cannot seal before the handshake."""

    def __init__(self, conn_id, mux: MuxConn, session):
        self.conn_id = conn_id
        self.mux = mux
        self.session = session
        self.channels: set[int] = set()
        self.ctrl_ch: int | None = None
        self.pending_ctrl: "ChannelConn | None" = None  # awaiting handshake


# ---------------- the uplink ----------------

class Uplink:
    def __init__(self, manager: SessionManager, terms: TerminalManager,
                 config: Config, relay_url: str, machine_id: str, *,
                 identity, devices, pairing=None,
                 ping_interval: float = 20.0, ping_timeout: float = 25.0,
                 backoff_base: float = BACKOFF_BASE,
                 backoff_cap: float = BACKOFF_CAP):
        self.manager = manager
        self.terms = terms
        self.config = config
        self.relay_url = relay_url.rstrip("/")
        self.machine_id = machine_id
        self.identity = identity
        self.devices = devices
        self.pairing = pairing
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.plan: str | None = None
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._welcomed = False
        self.clients: dict[object, ClientState] = {}
        self.channels: dict[int, tuple[ClientState, ChannelConn | TermChannel]] = {}
        # devices.revoke (rpc revoke_device) also cuts live connections
        identity_mod.set_active_devices(devices)
        identity_mod.REVOKE_LISTENERS.append(self._on_device_revoked)

    # -------- reconnect loop --------

    async def run(self) -> None:
        attempt = 0
        while True:
            self._welcomed = False
            code = None
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                log.info("uplink closed (code=%s)", code)
            except Exception as e:
                log.info("uplink error: %r", e)
            if code in _FATAL_UNAUTHORIZED:
                print("[bcrc] The relay refused this agent (unauthorized/"
                      "revoked).\n       Re-enroll: in the dashboard choose "
                      "Add a machine and run the install command it shows.")
                return
            if code == proto.CLOSE_UPGRADE_REQUIRED:
                print("[bcrc] The relay requires a newer agent — re-run the "
                      "install command from your dashboard to upgrade.")
                return
            if self._welcomed:
                attempt = 0
            attempt += 1
            delay = random.uniform(
                0, min(self.backoff_cap, self.backoff_base * (2 ** attempt)))
            if code == proto.CLOSE_DUPLICATE_AGENT:
                delay = max(delay, KICK_MIN_BACKOFF)
                print("[bcrc] Another agent connected for this machine — "
                      f"retrying in {delay:.0f}s")
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        url = self.relay_url + "/agent/ws"
        headers = {"X-BCRC-Agent": agent_auth_header(self.machine_id,
                                                     self.identity)}
        async with websockets.connect(
                url, additional_headers=headers,
                ping_interval=self.ping_interval,
                ping_timeout=self.ping_timeout,
                max_size=proto.FRAME_MAX_WIRE) as ws:
            self._ws = ws
            try:
                await ws.send(proto.pack_frame(
                    proto.KIND_CTRL, 0,
                    json.dumps({"type": "hello",
                                "proto": proto.PROTO}).encode()))
                raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT)
                kind, _ch, payload = proto.unpack_frame(self._as_bytes(raw))
                msg = json.loads(payload)
                if kind != proto.KIND_CTRL or msg.get("type") != "welcome":
                    raise proto.FrameError("expected welcome")
                self.plan = msg.get("plan")
                self._welcomed = True
                log.info("relay connected: %s (plan=%s)", url, self.plan)
                print(f"[bcrc] connected to relay (plan {self.plan})")
                while True:
                    await self._dispatch(self._as_bytes(await ws.recv()))
            finally:
                self._ws = None
                await self._teardown_all()

    @staticmethod
    def _as_bytes(raw) -> bytes:
        if isinstance(raw, bytes):
            return raw
        return raw.encode() if isinstance(raw, str) else bytes(raw)

    # -------- outbound --------

    async def _ws_send(self, data: bytes) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("uplink down")
        async with self._send_lock:
            await ws.send(data)

    def _send_ctrl_soon(self, msg: dict) -> None:
        frame = proto.pack_frame(proto.KIND_CTRL, 0, json.dumps(msg).encode())

        async def _go():
            with contextlib.suppress(Exception):
                await self._ws_send(frame)
        asyncio.get_running_loop().create_task(_go())

    # -------- inbound dispatch --------

    async def _dispatch(self, frame: bytes) -> None:
        kind, ch, payload = proto.unpack_frame(frame)  # bad relay frame → reconnect
        if kind == proto.KIND_CTRL:
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            mtype = msg.get("type")
            if mtype == "open":
                self._open_channel(msg)
            elif mtype == "close":
                await self.teardown_channel(int(msg.get("ch") or 0),
                                            str(msg.get("reason") or ""),
                                            notify_relay=False)
            elif mtype == "error":
                log.warning("relay error: %s", msg)
            return
        if kind == proto.KIND_HS:
            await self._handle_hs(ch, payload)
            return
        entry = self.channels.get(ch)
        if entry is None:
            return  # racing a close
        client, chan = entry
        try:
            _k, _c, ptype, body = client.mux.decode(frame)
        except (proto.FrameError, CodecError):
            # Tampered/replayed/plaintext frame: secretstream can never
            # recover this channel's stream — hard-close the channel (the
            # ctrl channel cascades to the whole client connection).
            await self.teardown_channel(ch, "bad frame", notify_relay=True)
            return
        if isinstance(chan, ChannelConn):
            if ptype != proto.PTYPE_JSON:
                return
            try:
                msg = json.loads(body)
            except ValueError:
                return
            refusal = self._quota_refusal(msg)
            if refusal is not None:
                chan.push(refusal)
                return
            try:
                await main_mod.handle_ws(self.manager, chan, msg)
            except Exception as e:
                chan.push({"type": "error", "message": str(e),
                           "ref": msg.get("type")})
        else:
            if ptype != proto.PTYPE_JSON:
                return  # client→agent term traffic is JSON today
            try:
                data = json.loads(body)
            except ValueError:
                return
            if not chan.attached:
                if data.get("type") == "term_attach" and \
                        isinstance(data.get("sid"), str):
                    chan.attached = True
                    chan.task = asyncio.get_running_loop().create_task(
                        self._run_term(client, chan, data["sid"]))
            else:
                chan.inbox.put_nowait(data)

    def _quota_refusal(self, msg: dict) -> dict | None:
        """Plan enforcement (SESSION_CAPS): refuse sessions.create when the
        plan's live-session cap is already met. Returns the rpc_result to
        push, or None to let the request through."""
        if msg.get("type") != "rpc" or msg.get("method") != "sessions.create":
            return None
        cap = SESSION_CAPS.get(self.plan or "")
        if cap is None:
            return None
        live = sum(1 for r in self.manager.runners.values()
                   if r.client is not None)
        if live < cap:
            return None
        return {"type": "rpc_result", "id": msg.get("id"), "ok": False,
                "error": {"code": "quota_sessions",
                          "message": f"your plan runs {cap} live session"
                                     f"{'s' if cap != 1 else ''} — stop one "
                                     "or upgrade"}}

    # -------- E2E handshake (KIND_HS on the client's ctrl channel) --------

    async def _handle_hs(self, ch: int, payload: bytes) -> None:
        entry = self.channels.get(ch)
        if entry is None:
            return  # racing a close
        client, chan = entry
        if ch != client.ctrl_ch or not isinstance(chan, ChannelConn):
            await self._hs_refuse(client, ch, "protocol")
            return
        try:
            msg = json.loads(payload)
            if not isinstance(msg, dict):
                raise ValueError
        except ValueError:
            await self._hs_refuse(client, ch, "protocol")
            return
        try:
            reply = client.session.handle_hs(msg)
        except identity_mod.HandshakeError as e:
            await self._hs_refuse(client, ch, e.code)
            return
        if reply is not None:
            # hs2 must hit the wire before any sealed frame so the device
            # has keys when the first DATA arrives — send it inline, not
            # through the mux pump.
            with contextlib.suppress(Exception):
                await self._ws_send(proto.pack_frame(
                    proto.KIND_HS, ch, json.dumps(reply).encode()))
        if client.session.established and client.pending_ctrl is not None:
            self._activate_ctrl(client, client.pending_ctrl)
            client.pending_ctrl = None

    async def _hs_refuse(self, client: ClientState, ch: int,
                         code: str) -> None:
        log.info("handshake refused (%s) for client %s", code,
                 client.conn_id)
        with contextlib.suppress(Exception):
            await self._ws_send(proto.pack_frame(
                proto.KIND_HS, ch,
                json.dumps({"type": "hs_err", "code": code}).encode()))
        ctrl = client.ctrl_ch if client.ctrl_ch is not None else ch
        await self.teardown_channel(ctrl, "handshake refused",
                                    notify_relay=True)

    def _on_device_revoked(self, device_pk_b64: str) -> None:
        """identity.Devices.revoke() listener: cut live connections of the
        revoked device (second revocation layer, plan §threat model)."""
        for client in list(self.clients.values()):
            if client.session.device_pk_b64 == device_pk_b64 \
                    and client.ctrl_ch is not None:
                asyncio.get_running_loop().create_task(
                    self.teardown_channel(client.ctrl_ch, "device revoked",
                                          notify_relay=True))

    # -------- channel lifecycle --------

    def _activate_ctrl(self, client: ClientState, chan: "ChannelConn") -> None:
        """Handshake done: only now may the ctrl channel carry (sealed)
        broadcasts. Mirrors what the local WS pushes on connect."""
        self.manager.conns.add(chan)
        chan.push({"type": "hello", "version": VERSION})
        chan.push({"type": "sessions", "sessions": self.manager.snapshots()})

    def _open_channel(self, msg: dict) -> None:
        ch = int(msg.get("ch") or 0)
        kind_ = msg.get("kind")
        conn_id = msg.get("conn")
        if ch <= 0 or ch in self.channels or kind_ not in ("ctrl", "term"):
            self._send_ctrl_soon({"type": "close", "ch": ch,
                                  "reason": "bad open"})
            return
        client = self.clients.get(conn_id)
        if client is None:
            session = identity_mod.AgentSession(self.identity, self.devices,
                                                self.pairing)
            mux = MuxConn(
                self._ws_send, codec=session.codec,
                on_channel_close=lambda c, why: self._on_mux_close(c, why))
            mux.start()
            client = ClientState(conn_id, mux, session)
            self.clients[conn_id] = client
        if kind_ == "ctrl":
            chan: ChannelConn | TermChannel = ChannelConn(client, ch)
            client.ctrl_ch = ch
            self.channels[ch] = (client, chan)
            client.channels.add(ch)
            if client.session.established:
                self._activate_ctrl(client, chan)
            else:
                client.pending_ctrl = chan  # after the handshake
        else:
            chan = TermChannel(client, ch)
            self.channels[ch] = (client, chan)
            client.channels.add(ch)

    def _on_mux_close(self, ch: int, reason: str) -> None:
        """A mux channel closed itself (seq-queue overflow): notify the
        relay and tear down our side. Scheduled — called from sync code."""
        if ch in self.channels:
            asyncio.get_running_loop().create_task(
                self.teardown_channel(ch, reason or "overflow",
                                      notify_relay=True))

    async def teardown_channel(self, ch: int, reason: str, *,
                               notify_relay: bool) -> None:
        entry = self.channels.pop(ch, None)
        if entry is None:
            return
        client, chan = entry
        client.channels.discard(ch)
        if isinstance(chan, ChannelConn):
            self.manager.conns.discard(chan)
            if client.pending_ctrl is chan:
                client.pending_ctrl = None
        else:
            chan.inbox.put_nowait(None)  # unblocks attach_terminal
        client.mux.close_channel(ch, reason)
        client.session.codec.drop_channel(ch)
        if notify_relay and self._ws is not None:
            self._send_ctrl_soon({"type": "close", "ch": ch,
                                  "reason": reason})
        if ch == client.ctrl_ch:
            # The ctrl channel is the client connection's spine (M4: the
            # handshake rides it) — losing it tears down the whole client.
            for other in list(client.channels):
                await self.teardown_channel(other, "client closed",
                                            notify_relay=notify_relay)
        if not client.channels:
            self.clients.pop(client.conn_id, None)
            await client.mux.close()

    async def _run_term(self, client: ClientState, chan: TermChannel,
                        sid: str) -> None:
        transport = _RelayTermTransport(self, client, chan)
        try:
            await attach_terminal(self.manager, self.terms, sid, transport)
        except Exception:
            log.exception("term channel %d failed", chan.ch)
            await self.teardown_channel(chan.ch, "error", notify_relay=True)

    async def _teardown_all(self) -> None:
        for ch in list(self.channels):
            await self.teardown_channel(ch, "uplink lost",
                                        notify_relay=False)
        for client in list(self.clients.values()):
            await client.mux.close()
        self.clients.clear()
