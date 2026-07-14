"""Server configuration: auth token, paths, network detection."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    """data/ next to the repo when running from a source checkout;
    ~/.better-claude-rc when running from an installed wheel (uvx).
    BCRC_DATA_DIR overrides both."""
    env = os.environ.get("BCRC_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if (ROOT / "pyproject.toml").exists():
        return ROOT / "data"
    return Path.home() / ".better-claude-rc"


DATA_DIR = _data_dir()
SESSIONS_DIR = DATA_DIR / "sessions"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_PORT = 8787


@dataclass
class Config:
    token: str
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    browse_root: str = str(Path.home())
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text())
        else:
            raw = {}
        changed = False
        if not raw.get("token"):
            raw["token"] = secrets.token_urlsafe(24)
            changed = True
        cfg = cls(
            token=raw["token"],
            host=raw.get("host", "0.0.0.0"),
            port=int(raw.get("port", DEFAULT_PORT)),
            browse_root=raw.get("browse_root", str(Path.home())),
            extra={k: v for k, v in raw.items()
                   if k not in ("token", "host", "port", "browse_root")},
        )
        if changed:
            cfg.save()
        return cfg

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": self.token,
            "host": self.host,
            "port": self.port,
            "browse_root": self.browse_root,
            **self.extra,
        }
        CONFIG_PATH.write_text(json.dumps(payload, indent=2))


def lan_ip() -> str | None:
    """Best-effort LAN IP (no traffic is actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None


def tailscale_ip() -> str | None:
    """Tailscale IPv4 if the CLI is installed and connected."""
    for cmd in (["tailscale", "ip", "-4"],
                ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().splitlines()[0]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None
