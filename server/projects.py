"""Project discovery, past-session listing and directory browsing.

Past sessions come from the SDK's transcript readers (same data the CLI's
/resume picker uses). Recent projects merge ~/.claude.json project entries
with ~/.claude/projects transcript directories.
"""

from __future__ import annotations

import time
from pathlib import Path

from claude_agent_sdk import get_session_messages, list_sessions

from .library import _read_claude_json

PROJECTS_DIR = Path.home() / ".claude" / "projects"

_recent_cache: tuple[float, list[dict]] | None = None


def recent_projects(limit: int = 40) -> list[dict]:
    """Known project paths ordered by most recent session activity."""
    global _recent_cache
    if _recent_cache and time.monotonic() - _recent_cache[0] < 30:
        return _recent_cache[1][:limit]

    # cwd -> last activity (ms)
    activity: dict[str, float] = {}
    counts: dict[str, int] = {}
    try:
        for info in list_sessions():
            cwd = info.cwd
            if not cwd:
                continue
            counts[cwd] = counts.get(cwd, 0) + 1
            ts = info.last_modified or 0
            if ts > activity.get(cwd, 0):
                activity[cwd] = ts
    except Exception:
        pass

    # Include projects known to claude.json even without readable transcripts
    for path in (_read_claude_json().get("projects") or {}):
        activity.setdefault(path, 0)
        counts.setdefault(path, 0)

    items = []
    for path, ts in activity.items():
        p = Path(path)
        items.append({
            "path": path,
            "name": p.name or path,
            "parent": str(p.parent),
            "exists": p.is_dir(),
            "last_active_ms": int(ts),
            "session_count": counts.get(path, 0),
        })
    items.sort(key=lambda x: x["last_active_ms"], reverse=True)
    _recent_cache = (time.monotonic(), items)
    return items[:limit]


def past_sessions(cwd: str, limit: int = 25) -> list[dict]:
    """Resumable transcript sessions for a project directory."""
    out = []
    try:
        infos = list_sessions(directory=cwd, limit=limit)
    except Exception:
        return out
    for info in infos:
        out.append({
            "session_id": info.session_id,
            "summary": info.custom_title or info.summary or info.first_prompt or "(untitled)",
            "first_prompt": (info.first_prompt or "")[:200],
            "last_modified_ms": info.last_modified,
            "created_at_ms": info.created_at,
            "git_branch": info.git_branch,
            "cwd": info.cwd,
        })
    out.sort(key=lambda x: x["last_modified_ms"] or 0, reverse=True)
    return out


def session_preview(session_id: str, cwd: str | None, tail: int = 12) -> list[dict]:
    """Last few messages of a past session, simplified for a peek UI."""
    try:
        messages = get_session_messages(session_id, directory=cwd)
    except Exception:
        return []
    simplified: list[dict] = []
    for m in messages:
        raw = getattr(m, "message", None) or {}
        role = getattr(m, "type", None) or raw.get("role")
        content = raw.get("content") if isinstance(raw, dict) else None
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool: {block.get('name')}]")
                else:
                    t = getattr(block, "text", None)
                    if t:
                        parts.append(t)
            text = "\n".join(p for p in parts if p)
        if role in ("user", "assistant") and text.strip():
            simplified.append({"role": role, "text": text.strip()[:600]})
    return simplified[-tail:]


def browse(path: str | None, root: str) -> dict:
    """List sub-directories for the new-session folder picker."""
    root_p = Path(root).resolve()
    p = Path(path).expanduser().resolve() if path else root_p
    if not str(p).startswith(str(root_p)):
        p = root_p
    if not p.is_dir():
        p = root_p
    dirs = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                is_project = (child / ".git").exists() or (child / ".claude").exists() \
                    or (child / "pyproject.toml").exists() or (child / "package.json").exists()
            except OSError:
                is_project = False
            dirs.append({"name": child.name, "path": str(child), "is_project": is_project})
    except OSError:
        pass
    return {
        "path": str(p),
        "parent": str(p.parent) if p != root_p and str(p.parent).startswith(str(root_p)) else None,
        "dirs": dirs,
    }
