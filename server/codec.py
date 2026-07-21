"""PayloadCodec — seals/opens DATA frame payloads.

NullCodec is a passthrough (local loopback tests). E2ECodec is the real
end-to-end layer (plan §per-connection handshake + frame encryption):

* One codec per client connection. The per-connection handshake (identity.py
  / crypto.js) yields two 32-byte crypto_kx keys — one per direction.
* Each direction of each CHANNEL runs its own
  crypto_secretstream_xchacha20poly1305 stream whose key is derived from the
  direction key with keyed BLAKE2b over the channel id. Deriving per channel
  (instead of one stream per direction for the whole connection) keeps the
  blind relay's per-channel routing races — it legitimately drops in-flight
  frames of a channel that just closed — from desyncing every other channel:
  a lost frame can only ever kill the channel it belonged to. The security
  properties are unchanged: fresh keys per connection (forward secrecy),
  AEAD with the 6-byte envelope header as associated data (tampering with
  routing breaks authentication), and secretstream's built-in replay /
  reorder / truncation rejection per channel.
* The first sealed frame of a (channel, direction) carries the 24-byte
  secretstream header prefixed to the ciphertext; both ends know determinis-
  tically when to expect it. A tampered header just makes the first open
  fail — fail closed.
* Rekey: after 2^16 messages or 1 GiB on a stream the sender pushes with
  TAG_REKEY; libsodium ratchets both ends automatically.

Any open/seal on an unestablished codec, any authentication failure, and any
plaintext DATA frame (which cannot authenticate) raises CodecError — the
owner closes the channel (ctrl channel = the whole client connection).
"""

from __future__ import annotations

import hashlib

from nacl import bindings as nb
from nacl.exceptions import CryptoError

from . import proto

SECRETSTREAM_HEADER = nb.crypto_secretstream_xchacha20poly1305_HEADERBYTES
SECRETSTREAM_ABYTES = nb.crypto_secretstream_xchacha20poly1305_ABYTES
_TAG_MESSAGE = nb.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
_TAG_REKEY = nb.crypto_secretstream_xchacha20poly1305_TAG_REKEY

REKEY_MSGS = 2 ** 16      # plan: rekey every 2^16 msgs …
REKEY_BYTES = 2 ** 30     # … or 1 GiB, whichever first

CHANNEL_KEY_CTX = b"BCRC1|ch|"


class CodecError(Exception):
    """Payload failed to open (tampered/replayed/plaintext/corrupt) — close
    the channel."""


class PayloadCodec:
    def seal(self, header: bytes, plaintext: bytes) -> bytes:
        """Encrypt+authenticate plaintext bound to the envelope header."""
        raise NotImplementedError

    def open(self, header: bytes, payload: bytes) -> bytes:
        """Reverse of seal. Raises CodecError on authentication failure."""
        raise NotImplementedError

    def drop_channel(self, ch: int) -> None:
        """Forget stream state for a closed channel (memory hygiene)."""


class NullCodec(PayloadCodec):
    """Loopback tests only: payloads travel as-is. Never used on the relay
    path — connect mode and RelayTransport are E2E-only."""

    def seal(self, header: bytes, plaintext: bytes) -> bytes:
        return plaintext

    def open(self, header: bytes, payload: bytes) -> bytes:
        return payload


def channel_key(direction_key: bytes, ch: int) -> bytes:
    """Per-channel stream key: keyed BLAKE2b(direction key, channel id).
    Mirrored byte-for-byte in web/crypto.js (crypto_generichash)."""
    return hashlib.blake2b(
        CHANNEL_KEY_CTX + ch.to_bytes(4, "big"),
        key=direction_key, digest_size=32).digest()


class _PushStream:
    __slots__ = ("state", "pending_header", "msgs", "bytes")

    def __init__(self, key: bytes):
        self.state = nb.crypto_secretstream_xchacha20poly1305_state()
        self.pending_header = \
            nb.crypto_secretstream_xchacha20poly1305_init_push(self.state, key)
        self.msgs = 0
        self.bytes = 0


class E2ECodec(PayloadCodec):
    """One per client connection; keys arrive via set_keys() when the
    handshake completes. Symmetric for both roles — each side passes its own
    (tx, rx) crypto_kx session keys."""

    def __init__(self) -> None:
        self._tx_key: bytes | None = None
        self._rx_key: bytes | None = None
        self._push: dict[int, _PushStream] = {}
        self._pull: dict[int, object] = {}

    @property
    def established(self) -> bool:
        return self._tx_key is not None

    def set_keys(self, tx: bytes, rx: bytes) -> None:
        if len(tx) != 32 or len(rx) != 32:
            raise ValueError("session keys must be 32 bytes")
        self._tx_key = tx
        self._rx_key = rx

    @staticmethod
    def _channel_of(header: bytes) -> int:
        if len(header) != proto.HEADER_LEN:
            raise CodecError("bad envelope header")
        return int.from_bytes(header[2:6], "big")

    def seal(self, header: bytes, plaintext: bytes) -> bytes:
        if self._tx_key is None:
            raise CodecError("codec not established")
        ch = self._channel_of(header)
        st = self._push.get(ch)
        if st is None:
            st = self._push[ch] = _PushStream(channel_key(self._tx_key, ch))
        tag = _TAG_MESSAGE
        if st.msgs + 1 >= REKEY_MSGS or st.bytes + len(plaintext) >= REKEY_BYTES:
            tag = _TAG_REKEY  # ratchets both ends; receiver needs no logic
            st.msgs = 0
            st.bytes = 0
        else:
            st.msgs += 1
            st.bytes += len(plaintext)
        ct = nb.crypto_secretstream_xchacha20poly1305_push(
            st.state, plaintext, ad=bytes(header), tag=tag)
        if st.pending_header is not None:
            ct = st.pending_header + ct
            st.pending_header = None
        return ct

    def open(self, header: bytes, payload: bytes) -> bytes:
        if self._rx_key is None:
            raise CodecError("codec not established")
        ch = self._channel_of(header)
        state = self._pull.get(ch)
        if state is None:
            if len(payload) < SECRETSTREAM_HEADER + SECRETSTREAM_ABYTES:
                raise CodecError("short first frame")
            state = nb.crypto_secretstream_xchacha20poly1305_state()
            try:
                nb.crypto_secretstream_xchacha20poly1305_init_pull(
                    state, payload[:SECRETSTREAM_HEADER],
                    channel_key(self._rx_key, ch))
            except (CryptoError, ValueError) as e:
                raise CodecError(f"bad stream header: {e}")
            payload = payload[SECRETSTREAM_HEADER:]
            self._pull[ch] = state
        try:
            plaintext, _tag = nb.crypto_secretstream_xchacha20poly1305_pull(
                state, bytes(payload), ad=bytes(header))
        except (CryptoError, ValueError, RuntimeError) as e:
            raise CodecError(f"open failed: {e}")
        return plaintext

    def drop_channel(self, ch: int) -> None:
        self._push.pop(ch, None)
        self._pull.pop(ch, None)
