"""Channel multiplexer with bounded per-channel outbound queues.

One MuxConn per connected client (agent side). Senders enqueue payloads on a
channel; a single pump task drains channels round-robin, seals each payload
with the codec and hands the framed bytes to `send_bytes` (e.g. ws.send).

Drop policy (plan §backpressure — the agent sees event types, so it can be
smarter than the blind relay):

* transient payloads (seq:null deltas/status) are dropped oldest-first once
  TRANSIENT_QUEUE_MAX of them are queued on a channel — losing them is safe,
  they carry no replayable state;
* seq-bearing payloads are NEVER dropped — if a channel's queue still exceeds
  CHANNEL_QUEUE_MAX the client is hopeless and the channel is closed. The
  invariant that makes this safe: any disconnect is fully repaired by
  attach{since_seq} replay from events.jsonl.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from typing import Awaitable, Callable

from . import proto
from .codec import NullCodec, PayloadCodec


class ChannelClosed(Exception):
    """Send attempted on a closed (or just-overflowed) channel."""


class _Channel:
    __slots__ = ("id", "items", "transients", "closed", "reason")

    def __init__(self, ch_id: int):
        self.id = ch_id
        self.items: deque[tuple[bool, bytes]] = deque()  # (transient, plaintext)
        self.transients = 0
        self.closed = False
        self.reason = ""


class MuxConn:
    def __init__(
        self,
        send_bytes: Callable[[bytes], Awaitable[None]],
        codec: PayloadCodec | None = None,
        on_channel_close: Callable[[int, str], None] | None = None,
    ):
        self._send_bytes = send_bytes
        self.codec = codec or NullCodec()
        self.on_channel_close = on_channel_close
        self._channels: dict[int, _Channel] = {}
        self._wake = asyncio.Event()
        self._pump_task: asyncio.Task | None = None
        self.closed = False

    # ---------------- outbound ----------------

    def send_json(self, ch_id: int, obj: dict, transient: bool = False) -> None:
        body = json.dumps(obj, default=str).encode()
        self._put(ch_id, bytes([proto.PTYPE_JSON]) + body, transient)

    def send_raw(self, ch_id: int, data: bytes) -> None:
        self._put(ch_id, bytes([proto.PTYPE_RAW]) + data, False)

    def _put(self, ch_id: int, plaintext: bytes, transient: bool) -> None:
        if len(plaintext) > proto.FRAME_MAX_PLAINTEXT:
            raise ValueError(f"payload exceeds {proto.FRAME_MAX_PLAINTEXT}B cap")
        ch = self._channels.get(ch_id)
        if ch is None:
            ch = self._channels[ch_id] = _Channel(ch_id)
        if ch.closed or self.closed:
            raise ChannelClosed(ch_id)
        if transient:
            if ch.transients >= proto.TRANSIENT_QUEUE_MAX:
                self._drop_oldest_transient(ch)
            ch.items.append((True, plaintext))
            ch.transients += 1
        else:
            if len(ch.items) >= proto.CHANNEL_QUEUE_MAX:
                self.close_channel(ch_id, "overflow")
                raise ChannelClosed(ch_id)
            ch.items.append((False, plaintext))
        self._wake.set()

    @staticmethod
    def _drop_oldest_transient(ch: _Channel) -> None:
        for i, (is_transient, _) in enumerate(ch.items):
            if is_transient:
                del ch.items[i]
                ch.transients -= 1
                return

    # ---------------- pump ----------------

    def start(self) -> None:
        if self._pump_task is None:
            self._pump_task = asyncio.get_running_loop().create_task(self._pump())

    async def _pump(self) -> None:
        try:
            while not self.closed:
                sent = False
                for ch in list(self._channels.values()):
                    if ch.closed or not ch.items:
                        continue
                    is_transient, plaintext = ch.items.popleft()
                    if is_transient:
                        ch.transients -= 1
                    header = proto.pack_header(proto.KIND_DATA, ch.id)
                    payload = self.codec.seal(header, plaintext)
                    await self._send_bytes(header + payload)
                    sent = True
                if not sent:
                    self._wake.clear()
                    if any(ch.items and not ch.closed
                           for ch in self._channels.values()):
                        continue  # raced with a put
                    await self._wake.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transport failure (e.g. uplink died mid-send): the conn is
            # unusable — mark closed so senders get ChannelClosed and
            # close() doesn't re-raise. The owner's recv side tears down.
            self.closed = True

    def pending(self, ch_id: int) -> int:
        ch = self._channels.get(ch_id)
        return len(ch.items) if ch else 0

    # ---------------- lifecycle ----------------

    def channel_open(self, ch_id: int) -> bool:
        ch = self._channels.get(ch_id)
        return ch is not None and not ch.closed

    def close_channel(self, ch_id: int, reason: str = "") -> None:
        ch = self._channels.get(ch_id)
        if ch is None or ch.closed:
            return
        ch.closed = True
        ch.reason = reason
        ch.items.clear()
        ch.transients = 0
        if self.on_channel_close is not None:
            self.on_channel_close(ch_id, reason)

    async def close(self) -> None:
        self.closed = True
        self._wake.set()
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None

    # ---------------- inbound ----------------

    def decode(self, frame: bytes) -> tuple[int, int, int | None, bytes]:
        """-> (kind, channel, ptype|None, body).

        CTRL/HS frames return (kind, ch, None, payload) — plaintext JSON.
        DATA frames are opened with the codec (header bytes as AD) and split
        into ptype + body. Raises FrameError/CodecError on garbage.
        """
        kind, ch_id, payload = proto.unpack_frame(frame)
        if kind != proto.KIND_DATA:
            return kind, ch_id, None, payload
        plaintext = self.codec.open(frame[:proto.HEADER_LEN], payload)
        if not plaintext:
            raise proto.FrameError("empty DATA payload")
        if len(plaintext) > proto.FRAME_MAX_PLAINTEXT + 1:
            raise proto.FrameError("DATA payload exceeds plaintext cap")
        return kind, ch_id, plaintext[0], plaintext[1:]
