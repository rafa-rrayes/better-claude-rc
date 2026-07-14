"""Wire protocol constants + the 6-byte binary envelope.

One envelope on both relay legs (plan §wire format):

    byte 0    ver  = 0x01
    byte 1    kind = 0x00 CTRL | 0x01 HS | 0x02 DATA
    bytes 2-5 channel_id uint32 BE
    bytes 6-  payload

CTRL is plaintext JSON (the only thing the relay interprets). HS carries
handshake JSON, relayed opaque. DATA carries codec-sealed payloads whose
decrypted form is [1B ptype][body]; the 6-byte header is the AEAD associated
data, so tampering with routing breaks authentication (enforced by E2ECodec
in M4 — NullCodec ignores it).
"""

from __future__ import annotations

import struct

PROTO = 1          # CTRL hello{proto}; relay supports current+previous

ENV_VER = 0x01
KIND_CTRL = 0x00
KIND_HS = 0x01
KIND_DATA = 0x02
KINDS = frozenset((KIND_CTRL, KIND_HS, KIND_DATA))

HEADER_LEN = 6
_HEADER = struct.Struct(">BBI")

# decrypted DATA payload: [1B ptype][body]
PTYPE_RAW = 0x00   # raw bytes (PTY traffic)
PTYPE_JSON = 0x01  # JSON control/event messages

# 256 KiB plaintext frame cap (PTY reads are already <=64 KiB; scrollback
# replay is chunked). The relay enforces the cap and kills violators.
FRAME_MAX_PLAINTEXT = 256 * 1024

# Agent-side per-channel outbound queue policy (plan §backpressure):
# transient (seq:null) events drop oldest-first past TRANSIENT_QUEUE_MAX;
# seq events are never dropped — past CHANNEL_QUEUE_MAX the channel closes
# (any disconnect is fully repaired by attach{since_seq} replay).
TRANSIENT_QUEUE_MAX = 128
CHANNEL_QUEUE_MAX = 512

# WS close codes (agent/relay vocabulary; used from M2 on)
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_SLOW_CLIENT = 4408
CLOSE_DUPLICATE_AGENT = 4409
CLOSE_UPGRADE_REQUIRED = 4426


class FrameError(ValueError):
    """Malformed envelope — the peer is broken or malicious."""


def pack_header(kind: int, channel: int) -> bytes:
    return _HEADER.pack(ENV_VER, kind, channel)


def pack_frame(kind: int, channel: int, payload: bytes) -> bytes:
    return pack_header(kind, channel) + payload


def unpack_frame(frame: bytes | bytearray | memoryview) -> tuple[int, int, bytes]:
    """-> (kind, channel, payload). Raises FrameError on a bad envelope."""
    frame = bytes(frame)
    if len(frame) < HEADER_LEN:
        raise FrameError("short frame")
    ver, kind, channel = _HEADER.unpack_from(frame)
    if ver != ENV_VER:
        raise FrameError(f"unsupported envelope version {ver}")
    if kind not in KINDS:
        raise FrameError(f"unknown frame kind {kind}")
    return kind, channel, frame[HEADER_LEN:]
