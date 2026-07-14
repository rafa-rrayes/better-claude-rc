"""Fake mux "browser" client for the relay e2e scripts.

Speaks the client leg of the relay wire format: CTRL hello/welcome/open/
opened/close/machine_status on channel 0, DATA frames sealed through a
PayloadCodec (NullCodec until M4) with the [1B ptype][body] plaintext layout.
"""

from __future__ import annotations

import asyncio
import json

import websockets

from server import proto
from server.codec import NullCodec


class FakeClient:
    def __init__(self, relay_url: str, machine: str, codec=None,
                 headers: dict | None = None):
        self.url = relay_url.rstrip("/") + f"/client/ws?machine={machine}"
        self.codec = codec or NullCodec()
        self.headers = headers or {}
        self.ws = None
        self.close_code: int | None = None
        self._ctrl_buf: list[dict] = []
        self._ctrl_q: asyncio.Queue = asyncio.Queue()
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
        try:
            async for raw in self.ws:
                frame = raw if isinstance(raw, bytes) else raw.encode()
                kind, ch, payload = proto.unpack_frame(frame)
                if kind == proto.KIND_CTRL:
                    self._ctrl_q.put_nowait(json.loads(payload))
                else:
                    plaintext = self.codec.open(frame[:proto.HEADER_LEN],
                                                payload)
                    q = self._chan_q.setdefault(ch, asyncio.Queue())
                    q.put_nowait((plaintext[0], plaintext[1:]))
        except websockets.exceptions.ConnectionClosed as e:
            self.close_code = e.rcvd.code if e.rcvd else None
        finally:
            self._ctrl_q.put_nowait(None)
            for q in self._chan_q.values():
                q.put_nowait(None)

    # ---------------- send ----------------

    async def send_ctrl(self, msg: dict) -> None:
        await self.ws.send(proto.pack_frame(
            proto.KIND_CTRL, 0, json.dumps(msg).encode()))

    async def send_json(self, ch: int, obj: dict) -> None:
        header = proto.pack_header(proto.KIND_DATA, ch)
        plaintext = bytes([proto.PTYPE_JSON]) + json.dumps(obj).encode()
        await self.ws.send(header + self.codec.seal(header, plaintext))

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
