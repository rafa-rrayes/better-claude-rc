"""Fake mux "browser" client for the relay e2e scripts.

Speaks the client leg of the relay wire format: CTRL hello/welcome/open/
opened/close/machine_status on channel 0, DATA frames sealed through a
PayloadCodec with the [1B ptype][body] plaintext layout, and (with a
DeviceSim) the real M4 E2E layer: QR pairing + per-connection handshake
over KIND_HS frames + per-channel secretstream via server.codec.E2ECodec —
the same crypto web/crypto.js runs in the browser, so the relay path is
exercised exactly as production ciphertext.
"""

from __future__ import annotations

import asyncio
import base64
import json

import websockets
from nacl import bindings as nb
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from server import identity as idm
from server import proto
from server.codec import E2ECodec, NullCodec


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64d(value: str) -> bytes:
    return base64.b64decode(value)


class PairError(Exception):
    pass


class DeviceSim:
    """The phone role: an Ed25519 device key, QR pairing, the hs1/hs2
    handshake and the resulting E2ECodec (fresh one per connection)."""

    def __init__(self):
        self.sign_key = SigningKey.generate()
        self.pinned_agent_pk: bytes | None = None
        self.qr: dict | None = None
        self.codec = E2ECodec()  # replaced on every hs1
        self._eph = None  # (pk, sk, nonce) while a handshake is in flight

    @property
    def device_pk(self) -> bytes:
        return bytes(self.sign_key.verify_key)

    @property
    def device_pk_b64(self) -> str:
        return _b64(self.device_pk)

    # -------- pairing (QR string from the agent terminal) --------

    def load_qr(self, payload: str) -> None:
        parts = payload.strip().split(".")
        if len(parts) != 4 or parts[0] != idm.QR_PREFIX:
            raise PairError(f"bad QR payload: {payload[:32]}…")
        self.qr = {"agent_id": parts[1], "agent_pk": _b64d(parts[2]),
                   "secret": _b64d(parts[3])}

    def pair1(self, name: str = "e2e-device") -> dict:
        assert self.qr, "load_qr first"
        mac1 = idm.pair_mac1(self.qr["secret"], self.qr["agent_id"],
                             self.qr["agent_pk"], self.device_pk)
        self._mac1 = mac1
        return {"type": "pair1", "v": idm.HS_VERSION,
                "device_pk": self.device_pk_b64, "name": name,
                "mac1": _b64(mac1)}

    def handle_pair2(self, msg: dict) -> None:
        agent_pk = _b64d(msg["agent_pk"])
        if agent_pk != self.qr["agent_pk"]:
            raise PairError("pair2 agent_pk does not match the QR")
        try:
            VerifyKey(agent_pk).verify(
                idm.pair2_transcript(self.qr["agent_id"], agent_pk,
                                     self.device_pk, self._mac1),
                _b64d(msg["sig"]))
        except BadSignatureError:
            raise PairError("pair2 signature invalid")
        self.pinned_agent_pk = agent_pk  # PIN — never accepted again unsigned

    # -------- per-connection handshake --------

    def hs1(self) -> dict:
        import os
        eph_pk, eph_sk = nb.crypto_kx_keypair()
        nonce = os.urandom(16)
        self._eph = (eph_pk, eph_sk, nonce)
        self.codec = E2ECodec()  # fresh streams per connection
        sig = self.sign_key.sign(
            idm.hs1_transcript(self.device_pk, eph_pk, nonce)).signature
        return {"type": "hs1", "v": idm.HS_VERSION,
                "device_pk": self.device_pk_b64, "eph_pk": _b64(eph_pk),
                "nonce": _b64(nonce), "sig": _b64(sig)}

    def handle_hs2(self, msg: dict) -> None:
        assert self.pinned_agent_pk, "not paired"
        assert self._eph, "no handshake in flight"
        eph_pk, eph_sk, nonce = self._eph
        agent_eph = _b64d(msg["eph_pk"])
        VerifyKey(self.pinned_agent_pk).verify(
            idm.hs2_transcript(self.device_pk, eph_pk, nonce, agent_eph),
            _b64d(msg["sig"]))  # BadSignatureError propagates = test fails
        rx, tx = nb.crypto_kx_client_session_keys(eph_pk, eph_sk, agent_eph)
        self.codec.set_keys(tx=tx, rx=rx)
        self._eph = None

    def handle_hs(self, msg: dict) -> None:
        """Called inline from the read loop so keys are always installed
        before the next DATA frame is decoded."""
        t = msg.get("type")
        if t == "pair2":
            self.handle_pair2(msg)
        elif t == "hs2":
            self.handle_hs2(msg)
        # hs_err: surfaced to the test via the hs queue


class FakeClient:
    def __init__(self, relay_url: str, machine: str, codec=None,
                 headers: dict | None = None, device: DeviceSim | None = None):
        self.url = relay_url.rstrip("/") + f"/client/ws?machine={machine}"
        self.device = device
        self.codec = device.codec if device else (codec or NullCodec())
        self.headers = headers or {}
        self.ws = None
        self.close_code: int | None = None
        self._ctrl_buf: list[dict] = []
        self._ctrl_q: asyncio.Queue = asyncio.Queue()
        self._hs_q: asyncio.Queue = asyncio.Queue()
        self._chan_q: dict[int, asyncio.Queue] = {}
        self._reader: asyncio.Task | None = None
        self._rid = 0

    # ---------------- lifecycle ----------------

    async def connect(self, timeout: float = 15.0) -> dict:
        """Dial (retrying while the relay is down), send hello, await
        welcome. Returns the welcome message."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                self.ws = await websockets.connect(
                    self.url, max_size=proto.FRAME_MAX_WIRE,
                    additional_headers=self.headers)
                break
            except OSError:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.2)
        self._reader = asyncio.create_task(self._read_loop())
        await self.send_ctrl({"type": "hello", "proto": proto.PROTO})
        return await self.next_ctrl("welcome", timeout=10)

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, 5)
            except Exception:
                self._reader.cancel()

    async def _read_loop(self) -> None:
        from server.codec import CodecError
        try:
            async for raw in self.ws:
                frame = raw if isinstance(raw, bytes) else raw.encode()
                kind, ch, payload = proto.unpack_frame(frame)
                if kind == proto.KIND_CTRL:
                    self._ctrl_q.put_nowait(json.loads(payload))
                elif kind == proto.KIND_HS:
                    msg = json.loads(payload)
                    if self.device is not None:
                        # inline: keys must exist before the next DATA frame
                        self.device.handle_hs(msg)
                        self.codec = self.device.codec
                    self._hs_q.put_nowait((ch, msg))
                else:
                    try:
                        plaintext = self.codec.open(frame[:proto.HEADER_LEN],
                                                    payload)
                    except CodecError:
                        continue  # stale channel after a re-handshake
                    q = self._chan_q.setdefault(ch, asyncio.Queue())
                    q.put_nowait((plaintext[0], plaintext[1:]))
        except websockets.exceptions.ConnectionClosed as e:
            self.close_code = e.rcvd.code if e.rcvd else None
        finally:
            self._ctrl_q.put_nowait(None)
            self._hs_q.put_nowait(None)
            for q in self._chan_q.values():
                q.put_nowait(None)

    # ---------------- send ----------------

    async def send_ctrl(self, msg: dict) -> None:
        await self.ws.send(proto.pack_frame(
            proto.KIND_CTRL, 0, json.dumps(msg).encode()))

    async def send_hs(self, ch: int, msg: dict) -> None:
        await self.ws.send(proto.pack_frame(
            proto.KIND_HS, ch, json.dumps(msg).encode()))

    async def send_json(self, ch: int, obj: dict) -> None:
        header = proto.pack_header(proto.KIND_DATA, ch)
        plaintext = bytes([proto.PTYPE_JSON]) + json.dumps(obj).encode()
        await self.ws.send(header + self.codec.seal(header, plaintext))

    # ---------------- E2E: pairing + handshake ----------------

    async def next_hs(self, timeout: float = 15.0) -> dict:
        item = await asyncio.wait_for(self._hs_q.get(), timeout)
        if item is None:
            raise ConnectionError(f"ws closed (code={self.close_code})")
        return item[1]

    async def handshake(self, ch: int, pair_qr: str | None = None,
                        timeout: float = 15.0) -> None:
        """Run pairing (if a QR payload is given and we're not yet paired)
        then the per-connection handshake on the ctrl channel `ch`."""
        assert self.device is not None, "handshake needs a DeviceSim"
        dev = self.device
        if pair_qr is not None and dev.pinned_agent_pk is None:
            dev.load_qr(pair_qr)
            await self.send_hs(ch, dev.pair1())
            reply = await self.next_hs(timeout)
            if reply.get("type") != "pair2":
                raise PairError(f"pairing refused: {reply}")
        await self.send_hs(ch, dev.hs1())
        self.codec = dev.codec
        reply = await self.next_hs(timeout)
        if reply.get("type") != "hs2":
            raise ConnectionError(f"handshake refused: {reply}")
        assert dev.codec.established

    async def secure_ctrl(self, pair_qr: str | None = None,
                          timeout: float = 15.0) -> int:
        """open("ctrl") + pairing/handshake + wait for hello/sessions —
        everything a real phone does before the app is usable."""
        ch = await self.open("ctrl")
        await self.handshake(ch, pair_qr, timeout)
        await self.wait_type(ch, "hello", timeout)
        await self.wait_type(ch, "sessions", timeout)
        return ch

    # ---------------- receive ----------------

    async def next_ctrl(self, type_: str | None = None,
                        timeout: float = 15.0) -> dict:
        for i, m in enumerate(self._ctrl_buf):
            if type_ is None or m.get("type") == type_:
                return self._ctrl_buf.pop(i)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            m = await asyncio.wait_for(self._ctrl_q.get(),
                                       max(0.01, deadline - loop.time()))
            if m is None:
                raise ConnectionError(f"ws closed (code={self.close_code})")
            if type_ is None or m.get("type") == type_:
                return m
            self._ctrl_buf.append(m)

    async def wait_status(self, online: bool, timeout: float = 30.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            m = await self.next_ctrl("machine_status",
                                     max(0.01, deadline - loop.time()))
            if m.get("online") == online:
                return

    async def open(self, kind: str, timeout: float = 10.0) -> int:
        await self.send_ctrl({"type": "open", "kind": kind})
        opened = await self.next_ctrl("opened", timeout)
        assert opened.get("kind") == kind, opened
        ch = opened["ch"]
        self._chan_q.setdefault(ch, asyncio.Queue())
        return ch

    async def next_data(self, ch: int,
                        timeout: float = 30.0) -> tuple[int, bytes]:
        q = self._chan_q.setdefault(ch, asyncio.Queue())
        item = await asyncio.wait_for(q.get(), timeout)
        if item is None:
            raise ConnectionError(f"ws closed (code={self.close_code})")
        return item

    async def next_json(self, ch: int, timeout: float = 30.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            ptype, body = await self.next_data(
                ch, max(0.01, deadline - loop.time()))
            if ptype == proto.PTYPE_JSON:
                return json.loads(body)

    async def wait_type(self, ch: int, type_: str, timeout: float = 30.0,
                        sink: list | None = None) -> dict:
        """Next JSON message of the given type; others go to sink (if any)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self.next_json(ch, max(0.01, deadline - loop.time()))
            if msg.get("type") == type_:
                return msg
            if sink is not None:
                sink.append(msg)

    async def rpc(self, ch: int, method: str, params: dict | None = None,
                  timeout: float = 30.0, sink: list | None = None) -> dict:
        self._rid += 1
        rid = self._rid
        await self.send_json(ch, {"type": "rpc", "id": rid, "method": method,
                                  "params": params or {}})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self.next_json(ch, max(0.01, deadline - loop.time()))
            if msg.get("type") == "rpc_result" and msg.get("id") == rid:
                return msg
            if sink is not None:
                sink.append(msg)
