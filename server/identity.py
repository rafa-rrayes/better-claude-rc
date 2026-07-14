"""Agent identity: an Ed25519 keypair generated ON the machine (plan
§credentials). The seed lives in data/identity.json (0600) and never leaves;
the platform stores the PUBLIC key only, so a stolen platform DB yields zero
impersonation capability.

M3 scope: identity keypair + enrollment + the signed relay auth header
    X-BCRC-Agent: <agent_id>.<ts>.<Ed25519 sig over method|path|ts>
Pairing/devices.json/session handshake land in M4 and extend this module.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

from nacl.signing import SigningKey

log = logging.getLogger("bcrc.identity")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _b64url_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


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
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                log.warning("%s permissions were %o — fixing to 0600",
                            path, mode)
                path.chmod(0o600)
            raw = json.loads(path.read_text())
            key = SigningKey(base64.b64decode(raw["seed"]))
            return cls(path, key, raw.get("machine_id") or None)
        ident = cls(path, SigningKey.generate())
        ident.save()
        log.info("generated new agent identity at %s", path)
        return ident

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "seed": _b64(bytes(self.signing_key)),
            "machine_id": self.machine_id,
        }, indent=2)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        self.path.chmod(0o600)  # in case the file pre-existed

    # -------- keys + signed header --------

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
        headers={"Content-Type": "application/json"}, method="POST")
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
