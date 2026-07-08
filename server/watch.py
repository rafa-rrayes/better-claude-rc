"""Live transcript tailing: mirror turns appended to a claude session's
JSONL by OTHER processes (a terminal `claude` on the same session) into
the UI event log, so the webapp shows terminal activity in real time.

Dedup contract (enforced by the caller in sessions.py): the tailer only
reads while this UI session's own subprocess is quiet — rows our own
turns write are skipped by fast-forwarding the read offset to EOF when
the turn's result arrives.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from .history import _assistant_events, _user_events

try:
    # Private SDK helper (pinned via uv.lock): resolves the transcript path
    # with the CLI's exact directory munging, worktree and long-path rules.
    from claude_agent_sdk._internal.sessions import _resolve_session_file_path
except ImportError:  # pragma: no cover — SDK internals moved
    _resolve_session_file_path = None


def transcript_path(session_id: str, cwd: str | None) -> Path | None:
    if _resolve_session_file_path is not None:
        try:
            return _resolve_session_file_path(session_id, cwd)
        except Exception:
            pass
    if not cwd:
        return None
    safe = re.sub(r"[^a-zA-Z0-9]", "-",
                  unicodedata.normalize("NFC", os.path.realpath(cwd)))
    p = Path.home() / ".claude" / "projects" / safe / f"{session_id}.jsonl"
    return p if p.exists() else None


def rows_to_events(lines: list[str]) -> list[dict]:
    """Raw transcript JSONL lines → UI events, marked external.

    Applies the same visibility rules as the SDK's chain reader: only
    top-level user/assistant rows, no meta/sidechain/team entries.
    """
    evs: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") not in ("user", "assistant"):
            continue
        if row.get("isMeta") or row.get("isSidechain") or row.get("teamName"):
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            continue
        if row["type"] == "user":
            new = _user_events(content)
        else:
            new = _assistant_events(content)
        for ev in new:
            ev["external"] = True
        evs.extend(new)
    return evs
