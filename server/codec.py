"""PayloadCodec — seals/opens DATA frame payloads.

NullCodec (this milestone) is a passthrough so the whole mux path runs in
plaintext locally. E2ECodec (M4) wraps crypto_secretstream with the 6-byte
envelope header as associated data; because every DATA payload already flows
through this interface, turning on end-to-end encryption swaps one object.
"""

from __future__ import annotations


class CodecError(Exception):
    """Payload failed to open (tampered/corrupt) — close the channel."""


class PayloadCodec:
    def seal(self, header: bytes, plaintext: bytes) -> bytes:
        """Encrypt+authenticate plaintext bound to the envelope header."""
        raise NotImplementedError

    def open(self, header: bytes, payload: bytes) -> bytes:
        """Reverse of seal. Raises CodecError on authentication failure."""
        raise NotImplementedError


class NullCodec(PayloadCodec):
    """Local mode / pre-M4 relay mode: payloads travel as-is."""

    def seal(self, header: bytes, plaintext: bytes) -> bytes:
        return plaintext

    def open(self, header: bytes, payload: bytes) -> bytes:
        return payload
