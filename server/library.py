"""Discovery of skills, slash commands, agents and MCP servers.

Reads the same places the Claude Code CLI does:
  - user scope:    ~/.claude/{skills,commands,agents}, ~/.claude.json mcpServers
  - project scope: <cwd>/.claude/{skills,commands,agents}, <cwd>/.mcp.json,
                   ~/.claude.json projects[<cwd>].mcpServers
  - plugins:       ~/.claude/plugins/** (best effort)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# ~/.claude.json is large; cache parsed copy keyed by mtime.
_claude_json_cache: tuple[float, dict] | None = None


def _read_claude_json() -> dict:
    global _claude_json_cache
    try:
        mtime = CLAUDE_JSON.stat().st_mtime
    except OSError:
        return {}
    if _claude_json_cache and _claude_json_cache[0] == mtime:
        return _claude_json_cache[1]
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    _claude_json_cache = (mtime, data)
    return data


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tolerant single-level YAML frontmatter parser (no yaml dependency)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km:
            current_key = km.group(1).lower()
            val = km.group(2).strip().strip("\"'")
            # ignore folded/literal markers, accumulate continuation lines
            fields[current_key] = "" if val in (">", ">-", "|", "|-") else val
        elif current_key and line.startswith((" ", "\t")):
            fields[current_key] = (fields[current_key] + " " + line.strip()).strip()
    return fields


def _head(path: Path, limit: int = 16384) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _scan_skills(base: Path, scope: str, source: str = "") -> list[dict]:
    out = []
    root = base / "skills"
    if not root.is_dir():
        return out
    for skill_md in sorted(root.glob("*/SKILL.md")):
        fm = _parse_frontmatter(_head(skill_md))
        out.append({
            "name": fm.get("name") or skill_md.parent.name,
            "description": fm.get("description", ""),
            "scope": scope,
            "source": source,
            "path": str(skill_md),
        })
    return out


def _scan_commands(base: Path, scope: str, source: str = "") -> list[dict]:
    out = []
    root = base / "commands"
    if not root.is_dir():
        return out
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        name = ":".join([*rel.parts[:-1], rel.stem])
        fm = _parse_frontmatter(_head(md))
        out.append({
            "name": name,
            "description": fm.get("description", ""),
            "argument_hint": fm.get("argument-hint", ""),
            "model": fm.get("model", ""),
            "scope": scope,
            "source": source,
            "path": str(md),
        })
    return out


def _scan_agents(base: Path, scope: str, source: str = "") -> list[dict]:
    out = []
    root = base / "agents"
    if not root.is_dir():
        return out
    for md in sorted(root.glob("*.md")):
        fm = _parse_frontmatter(_head(md))
        out.append({
            "name": fm.get("name") or md.stem,
            "description": fm.get("description", ""),
            "model": fm.get("model", ""),
            "tools": fm.get("tools", ""),
            "scope": scope,
            "source": source,
            "path": str(md),
        })
    return out


def _plugin_roots() -> list[tuple[str, Path]]:
    """(plugin_name, root_dir) pairs for installed plugins, best effort."""
    roots: list[tuple[str, Path]] = []
    plugins_dir = CLAUDE_HOME / "plugins"
    seen: set[Path] = set()

    def add(name: str, p: Path) -> None:
        if p.is_dir() and p not in seen:
            seen.add(p)
            roots.append((name, p))

    installed = plugins_dir / "installed_plugins.json"
    if installed.exists():
        try:
            data = json.loads(installed.read_text())
            entries: list[Any] = []
            if isinstance(data, dict):
                if isinstance(data.get("plugins"), dict):
                    for k, v in data["plugins"].items():
                        entries.append((k, v))
                else:
                    for k, v in data.items():
                        entries.append((k, v))
            for key, val in entries:
                name = str(key).split("@")[0]
                candidates = []
                if isinstance(val, dict):
                    for pk in ("installPath", "path", "pluginPath", "root"):
                        if isinstance(val.get(pk), str):
                            candidates.append(Path(val[pk]))
                elif isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            for pk in ("installPath", "path", "pluginPath", "root"):
                                if isinstance(item.get(pk), str):
                                    candidates.append(Path(item[pk]))
                for c in candidates:
                    add(name, c)
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: common on-disk layouts
    for pattern in ("repos/*/*", "cache/*/*", "marketplaces/*/plugins/*"):
        for p in plugins_dir.glob(pattern):
            if p.is_dir() and (
                (p / "skills").is_dir() or (p / "commands").is_dir()
                or (p / "agents").is_dir() or (p / ".claude-plugin").is_dir()
            ):
                add(p.name, p)
    return roots


def _mcp_transport(cfg: dict) -> str:
    t = cfg.get("type")
    if t:
        return t
    if cfg.get("url"):
        return "http/sse"
    if cfg.get("command"):
        return "stdio"
    return "unknown"


def _mcp_servers(cwd: str | None) -> list[dict]:
    out = []
    data = _read_claude_json()
    for name, cfg in (data.get("mcpServers") or {}).items():
        out.append({
            "name": name, "scope": "user",
            "transport": _mcp_transport(cfg),
            "detail": cfg.get("url") or cfg.get("command", ""),
        })
    if cwd:
        proj = (data.get("projects") or {}).get(cwd, {})
        for name, cfg in (proj.get("mcpServers") or {}).items():
            out.append({
                "name": name, "scope": "local",
                "transport": _mcp_transport(cfg),
                "detail": cfg.get("url") or cfg.get("command", ""),
            })
        mcp_json = Path(cwd) / ".mcp.json"
        if mcp_json.exists():
            try:
                pdata = json.loads(mcp_json.read_text())
                for name, cfg in (pdata.get("mcpServers") or {}).items():
                    out.append({
                        "name": name, "scope": "project",
                        "transport": _mcp_transport(cfg),
                        "detail": cfg.get("url") or cfg.get("command", ""),
                    })
            except (OSError, json.JSONDecodeError):
                pass
    return out


_library_cache: dict[str, tuple[float, dict]] = {}


def get_library(cwd: str | None = None) -> dict:
    """Full library snapshot for user scope + optional project scope."""
    key = cwd or ""
    cached = _library_cache.get(key)
    if cached and time.monotonic() - cached[0] < 15:
        return cached[1]

    skills = _scan_skills(CLAUDE_HOME, "user")
    commands = _scan_commands(CLAUDE_HOME, "user")
    agents = _scan_agents(CLAUDE_HOME, "user")

    for plugin_name, root in _plugin_roots():
        skills += _scan_skills(root, "plugin", plugin_name)
        commands += _scan_commands(root, "plugin", plugin_name)
        agents += _scan_agents(root, "plugin", plugin_name)

    if cwd:
        base = Path(cwd) / ".claude"
        skills += _scan_skills(base, "project")
        commands += _scan_commands(base, "project")
        agents += _scan_agents(base, "project")

    result = {
        "skills": skills,
        "commands": commands,
        "agents": agents,
        "mcp_servers": _mcp_servers(cwd),
        "cwd": cwd,
    }
    _library_cache[key] = (time.monotonic(), result)
    return result
