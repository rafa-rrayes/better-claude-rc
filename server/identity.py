"""Agent identity, device registry, pairing and the per-connection E2E
handshake (plan §security architecture).

Identity: an Ed25519 keypair generated ON the machine. The seed lives in
data/identity.json (0600) and never leaves; the platform stores the PUBLIC
key only, so a stolen platform DB yields zero impersonation capability.

Devices: data/devices.json (0600) — the Ed25519 public keys of paired
phones. Pairing (once per device x machine) uses a 32-byte single-use
secret that exists only in agent RAM and travels out-of-band via the
terminal QR (`BCRC1.<agent_id>.<b64 agent_pk>.<b64 pairing_secret>`); the
relay never sees it, so it can neither register a rogue device nor make the
phone pin a rogue agent key.

Per-connection handshake (HS frames on the client's ctrl channel, relayed
opaque): ephemeral X25519 via crypto_kx, authenticated by both long-term
Ed25519 keys — the device key signs hs1, the agent key signs hs2 and is
pinned by the device. Yields the two direction keys that drive
codec.E2ECodec's secretstream layer. Fixed v:1, no negotiation.

Wire vocabulary (JSON inside KIND_HS frames):
  device -> agent   pair1{v, device_pk, name, mac1}     (pairing only)
  agent  -> device  pair2{v, agent_pk, sig}
  device -> agent   hs1{v, device_pk, eph_pk, nonce, sig}
  agent  -> device  hs2{v, eph_pk, sig}
  agent  -> device  hs_err{code}                        (then hard close)
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import secrets
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

from nacl import bindings as nb
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .codec import E2ECodec

log = logging.getLogger("bcrc.identity")

HS_VERSION = 1
PAIRING_TTL = 600.0        # 10 minutes, single use
QR_PREFIX = "BCRC1"

# Transcript domain-separation contexts. Mirrored byte-for-byte in
# web/crypto.js and scripts/relay_client.py — do not change casually.
PAIR1_CTX = b"BCRC1|pair1|"
PAIR2_CTX = b"BCRC1|pair2|"
HS1_CTX = b"BCRC1|hs1|"
HS2_CTX = b"BCRC1|hs2|"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64d(value, size: int | None = None) -> bytes:
    """Strict-ish b64 decode for handshake fields; raises ValueError."""
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("bad b64 field")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        raise ValueError("bad b64 field")
    if size is not None and len(raw) != size:
        raise ValueError("bad field size")
    return raw


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# -------- private-file hygiene (0600 files, 0700 data dir) --------

def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        log.warning("%s permissions were %o — fixing to 0700", path, mode)
        path.chmod(0o700)


def _check_file_perms(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        log.warning("%s permissions were %o — fixing to 0600", path, mode)
        path.chmod(0o600)


def _write_private(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    path.chmod(0o600)  # in case the file pre-existed


class Identity:
    def __init__(self, path: Path, signing_key: SigningKey,
                 machine_id: str | None = None):
        self.path = path
        self.signing_key = signing_key
        self.machine_id = machine_id

    # -------- persistence (0600, checked on every load) --------

    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        if path.exists():
            _check_file_perms(path)
            raw = json.loads(path.read_text())
            key = SigningKey(base64.b64decode(raw["seed"]))
            return cls(path, key, raw.get("machine_id") or None)
        ident = cls(path, SigningKey.generate())
        ident.save()
        log.info("generated new agent identity at %s", path)
        return ident

    def save(self) -> None:
        _write_private(self.path, json.dumps({
            "seed": _b64(bytes(self.signing_key)),
            "machine_id": self.machine_id,
        }, indent=2))

    # -------- keys + signed header --------

    @property
    def public_key(self) -> bytes:
        return bytes(self.signing_key.verify_key)

    @property
    def public_key_b64(self) -> str:
        return _b64(bytes(self.signing_key.verify_key))

    def auth_header(self, method: str = "GET",
                    path: str = "/agent/ws") -> str:
        """X-BCRC-Agent value; a fresh ts per dial (relay window ±60s)."""
        if not self.machine_id:
            raise RuntimeError("identity has no machine id (not enrolled)")
        ts = int(time.time())
        sig = self.signing_key.sign(f"{method}|{path}|{ts}".encode()).signature
        return f"{self.machine_id}.{ts}.{_b64url_nopad(sig)}"


# -------- enrollment over HTTPS (burns the bcrc_enr_ token) --------

class EnrollError(Exception):
    pass


def platform_http_url(relay_url: str) -> str:
    """wss://host -> https://host (the enroll API rides plain HTTPS)."""
    u = relay_url.strip().rstrip("/")
    for ws_scheme, http_scheme in (("wss://", "https://"),
                                   ("ws://", "http://")):
        if u.startswith(ws_scheme):
            return http_scheme + u[len(ws_scheme):]
    return u


def enroll(platform_url: str, token: str, identity: Identity, *,
           name: str, agent_version: str, proto: int) -> str:
    """POST the machine's public key; returns the platform machine id."""
    body = json.dumps({
        "token": token,
        "name": name,
        "ed25519_pk": identity.public_key_b64,
        "agent_version": agent_version,
        "proto": proto,
    }).encode()
    req = urllib.request.Request(
        platform_url.rstrip("/") + "/api/enroll", data=body,
        headers={"Content-Type": "application/json",
                 # urllib's default Python-urllib/x.y UA is on Cloudflare's
                 # Browser Integrity Check ban list (error 1010).
                 "User-Agent": f"better-claude-rc/{agent_version}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("detail", "")
        except Exception:
            detail = ""
        raise EnrollError(f"enrollment refused ({e.code}): "
                          f"{detail or e.reason}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EnrollError(f"could not reach the platform: {e}")
    machine_id = data.get("machine_id")
    if not machine_id:
        raise EnrollError(f"bad enroll response: {data}")
    return str(machine_id)


# ======================================================================
# Devices — the paired phones' Ed25519 public keys (data/devices.json)
# ======================================================================

class Devices:
    def __init__(self, path: Path, entries: list[dict]):
        self.path = path
        self.entries = entries

    @classmethod
    def load(cls, path: Path) -> "Devices":
        if path.exists():
            _check_file_perms(path)
            try:
                raw = json.loads(path.read_text())
            except ValueError:
                raw = []
            entries = [e for e in raw if isinstance(e, dict)
                       and isinstance(e.get("device_pk"), str)]
        else:
            entries = []
        return cls(path, entries)

    def save(self) -> None:
        _write_private(self.path, json.dumps(self.entries, indent=2))

    def __len__(self) -> int:
        return len(self.entries)

    def is_paired(self, device_pk_b64: str) -> bool:
        return any(e["device_pk"] == device_pk_b64 for e in self.entries)

    def add(self, device_pk_b64: str, name: str) -> None:
        if not self.is_paired(device_pk_b64):
            self.entries.append({
                "device_pk": device_pk_b64,
                "name": name[:64],
                "paired_at": int(time.time()),
            })
            self.save()
        log.info("device paired: %s (%s)", device_pk_b64[:12], name[:64])

    def revoke(self, device_pk_b64: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries
                        if e["device_pk"] != device_pk_b64]
        if len(self.entries) == before:
            return False
        self.save()
        log.info("device revoked: %s", device_pk_b64[:12])
        for cb in list(REVOKE_LISTENERS):
            try:
                cb(device_pk_b64)
            except Exception:
                log.exception("revoke listener failed")
        return True

    def snapshot(self) -> list[dict]:
        return [dict(e) for e in self.entries]


# The Devices instance the running process pairs/authenticates against.
# Set by connect mode at startup (Uplink) so the rpc handlers (devices.list
# / revoke_device) and the handshake share one registry.
ACTIVE_DEVICES: Devices | None = None
# Called with the device_pk (b64) on revoke — connect.Uplink registers a
# listener that tears down that device's live connections.
REVOKE_LISTENERS: list = []


def set_active_devices(devices: Devices) -> None:
    global ACTIVE_DEVICES
    ACTIVE_DEVICES = devices


def active_devices() -> Devices:
    global ACTIVE_DEVICES
    if ACTIVE_DEVICES is None:
        from .config import DATA_DIR
        ACTIVE_DEVICES = Devices.load(DATA_DIR / "devices.json")
    return ACTIVE_DEVICES


# ======================================================================
# Pairing — 32B single-use secret, 10 minutes, agent RAM only
# ======================================================================

def pair_mac1(secret: bytes, agent_id: str, agent_pk: bytes,
              device_pk: bytes) -> bytes:
    """Keyed BLAKE2b over the pairing transcript — proves the device saw
    the QR (and binds it to this agent identity + this device key)."""
    return hashlib.blake2b(
        PAIR1_CTX + agent_id.encode() + agent_pk + device_pk,
        key=secret, digest_size=32).digest()


def pair2_transcript(agent_id: str, agent_pk: bytes, device_pk: bytes,
                     mac1: bytes) -> bytes:
    return PAIR2_CTX + agent_id.encode() + agent_pk + device_pk + mac1


def hs1_transcript(device_pk: bytes, eph_pk: bytes, nonce: bytes) -> bytes:
    return HS1_CTX + device_pk + eph_pk + nonce


def hs2_transcript(device_pk: bytes, client_eph: bytes, nonce: bytes,
                   agent_eph: bytes) -> bytes:
    return HS2_CTX + device_pk + client_eph + nonce + agent_eph


class Pairing:
    """One active pairing window. Never persisted, never sent anywhere —
    the QR string is the only copy that leaves this object."""

    def __init__(self, agent_id: str, agent_pk: bytes, secret: bytes,
                 expires_at: float):
        self.agent_id = agent_id
        self.agent_pk = agent_pk
        self.secret = secret
        self.expires_at = expires_at
        self.used = False

    @classmethod
    def generate(cls, agent_id: str, agent_pk: bytes,
                 ttl: float = PAIRING_TTL) -> "Pairing":
        return cls(agent_id, agent_pk, secrets.token_bytes(32),
                   time.time() + ttl)

    @property
    def active(self) -> bool:
        return not self.used and time.time() < self.expires_at

    def qr_payload(self) -> str:
        return (f"{QR_PREFIX}.{self.agent_id}.{_b64(self.agent_pk)}"
                f".{_b64(self.secret)}")

    def verify_mac1(self, device_pk: bytes, mac1: bytes) -> bool:
        expected = pair_mac1(self.secret, self.agent_id, self.agent_pk,
                             device_pk)
        return hmac_mod.compare_digest(expected, mac1)

    def burn(self) -> None:
        self.used = True
        self.secret = b"\x00" * 32


# ======================================================================
# Per-connection handshake (agent side)
# ======================================================================

class HandshakeError(Exception):
    """Refuse the handshake; `code` travels back in hs_err before the hard
    close. Codes: no_pairing, bad_mac, unpaired, bad_sig, protocol."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AgentSession:
    """E2E state for ONE relay client connection on the agent: handles the
    pairing + handshake HS messages and owns the connection's E2ECodec.
    handle_hs() is synchronous and returns the reply message (or None);
    raises HandshakeError to refuse — the caller sends hs_err and hard-
    closes the client."""

    def __init__(self, identity: Identity, devices: Devices,
                 pairing: Pairing | None = None):
        self.identity = identity
        self.devices = devices
        self.pairing = pairing
        self.codec = E2ECodec()
        self.device_pk_b64: str | None = None

    @property
    def established(self) -> bool:
        return self.codec.established

    def handle_hs(self, msg: dict) -> dict | None:
        if self.established:
            # Fixed protocol: nothing is ever renegotiated. Any HS frame
            # after establishment is an attack or a broken peer.
            raise HandshakeError("protocol")
        t = msg.get("type")
        if msg.get("v") != HS_VERSION:
            raise HandshakeError("protocol")
        if t == "pair1":
            return self._pair1(msg)
        if t == "hs1":
            return self._hs1(msg)
        raise HandshakeError("protocol")

    # -------- pairing --------

    def _pair1(self, msg: dict) -> dict:
        p = self.pairing
        if p is None or not p.active:
            raise HandshakeError("no_pairing")
        try:
            device_pk = _b64d(msg.get("device_pk"), 32)
            mac1 = _b64d(msg.get("mac1"), 32)
        except ValueError:
            raise HandshakeError("protocol")
        if not p.verify_mac1(device_pk, mac1):
            # Wrong secret: refuse WITHOUT burning, so a typo can't lock
            # the legitimate user out of the 10-minute window.
            raise HandshakeError("bad_mac")
        p.burn()  # single use
        name = str(msg.get("name") or "device")
        self.devices.add(_b64(device_pk), name)
        sig = self.identity.signing_key.sign(
            pair2_transcript(p.agent_id, p.agent_pk, device_pk, mac1)
        ).signature
        return {"type": "pair2", "v": HS_VERSION,
                "agent_pk": self.identity.public_key_b64, "sig": _b64(sig)}

    # -------- session handshake --------

    def _hs1(self, msg: dict) -> dict:
        try:
            device_pk = _b64d(msg.get("device_pk"), 32)
            client_eph = _b64d(msg.get("eph_pk"), 32)
            nonce = _b64d(msg.get("nonce"), 16)
            sig = _b64d(msg.get("sig"), 64)
        except ValueError:
            raise HandshakeError("protocol")
        device_pk_b64 = _b64(device_pk)
        if not self.devices.is_paired(device_pk_b64):
            raise HandshakeError("unpaired")
        try:
            VerifyKey(device_pk).verify(
                hs1_transcript(device_pk, client_eph, nonce), sig)
        except BadSignatureError:
            raise HandshakeError("bad_sig")
        agent_eph_pk, agent_eph_sk = nb.crypto_kx_keypair()
        try:
            rx, tx = nb.crypto_kx_server_session_keys(
                agent_eph_pk, agent_eph_sk, client_eph)
        except Exception:
            raise HandshakeError("protocol")
        reply_sig = self.identity.signing_key.sign(
            hs2_transcript(device_pk, client_eph, nonce, agent_eph_pk)
        ).signature
        self.codec.set_keys(tx=tx, rx=rx)
        self.device_pk_b64 = device_pk_b64
        return {"type": "hs2", "v": HS_VERSION,
                "eph_pk": _b64(agent_eph_pk), "sig": _b64(reply_sig)}
