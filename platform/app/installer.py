"""The curl-pipe-bash installer served at GET /i/<enroll-token>.

The platform hosts the agent wheel itself (under /dist/), so enrollment
never depends on PyPI: the dashboard hands out
`curl -fsSL <base>/i/<token> | bash`, and the script below installs uv if
needed, installs the agent from the self-hosted wheel, and runs
`connect <token> --relay wss://<base-host>`.

The wheel is built into the Docker image (platform/Dockerfile) and found via
PLATFORM_DIST_DIR; a source checkout falls back to the repo's dist/ from
`uv build --package better-claude-rc`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Wheel filenames normalize the project name: better-claude-rc -> better_claude_rc
_WHEEL_RE = re.compile(r"^better_claude_rc-.+\.whl$")

# Mirrors db.ENROLL_PREFIX + create_token's token_urlsafe(32) (43 chars).
TOKEN_RE = re.compile(r"^bcrc_enr_[A-Za-z0-9_-]{30,80}$")


def dist_dir() -> Path:
    env = os.environ.get("PLATFORM_DIST_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "dist"


def find_wheel() -> str | None:
    """Newest agent wheel filename in dist_dir(), or None."""
    try:
        wheels = [p for p in dist_dir().iterdir() if _WHEEL_RE.match(p.name)]
    except OSError:
        return None
    if not wheels:
        return None
    return max(wheels, key=lambda p: p.stat().st_mtime).name


def relay_url(base: str) -> str:
    """wss:// twin of the platform's public https:// origin."""
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


# POSIX sh, no bashisms. main() wrapper so a truncated download runs nothing.
_TEMPLATE = """\
#!/bin/sh
# Better Claude RC installer — enrolls this machine with __BASE_URL__
set -eu

main() {
  WHEEL_URL="__BASE_URL__/dist/__WHEEL__"
  RELAY_URL="__RELAY_URL__"
  TOKEN="__TOKEN__"

  if ! command -v uv >/dev/null 2>&1; then
    echo "[bcrc] installing uv (https://astral.sh/uv) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"  # uv's default install location
  fi

  echo "[bcrc] installing better-claude-rc ..."
  uv tool install --quiet --force "better-claude-rc @ $WHEEL_URL"

  BIN="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
  echo "[bcrc] enrolling this machine ..."
  exec "$BIN/better-claude-rc" connect "$TOKEN" --relay "$RELAY_URL"
}

main "$@"
"""


def render_script(base: str, token: str, wheel: str) -> str:
    return (_TEMPLATE
            .replace("__BASE_URL__", base)
            .replace("__RELAY_URL__", relay_url(base))
            .replace("__WHEEL__", wheel)
            .replace("__TOKEN__", token))
