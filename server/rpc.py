"""RPC method table — the single implementation behind both transports.

The browser calls these over the /ws control channel:
    {type:"rpc", id, method, params}
        -> {type:"rpc_result", id, ok:true,  result}
        -> {type:"rpc_result", id, ok:false, error:{code, message}}

The REST routes in main.py are thin wrappers over the same handlers (kept
for curl and local debugging). In connect mode (relay) the identical rpc
envelope travels inside encrypted DATA frames — no second API surface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import VERSION, library, projects
from .config import Config
from .sessions import MODEL_CHOICES, PERMISSION_MODES, SessionManager

FILE_MAX_BYTES = 512 * 1024


class RpcError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# rpc error code -> HTTP status, for the REST wrappers
HTTP_STATUS = {
    "bad_request": 400,
    "forbidden": 403,
    "not_found": 404,
    "unknown_method": 404,
    "too_large": 413,
    "internal": 500,
}


def _require_str(params: dict, key: str) -> str:
    val = params.get(key)
    if not isinstance(val, str) or not val:
        raise RpcError("bad_request", f"missing required param: {key}")
    return val


def _jailed_path(raw: str, browse_root: str) -> Path:
    """Resolve a user-supplied path and require it inside browse_root."""
    try:
        p = Path(raw).expanduser().resolve()
        root = Path(browse_root).resolve()
    except OSError as e:
        raise RpcError("bad_request", str(e))
    if not p.is_relative_to(root):
        raise RpcError("forbidden", "outside browse root")
    return p


def _runner(m: SessionManager, params: dict):
    sid = _require_str(params, "sid")
    runner = m.get(sid)
    if runner is None:
        raise RpcError("not_found", "no such session")
    return runner


# ---------------- methods ----------------

async def _state(m: SessionManager, config: Config, params: dict) -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "home": str(Path.home()),
        "sessions": m.snapshots(),
        "defaults": {"models": MODEL_CHOICES, "modes": PERMISSION_MODES},
    }


async def _sessions_create(m: SessionManager, config: Config, params: dict) -> dict:
    cwd = _jailed_path(_require_str(params, "cwd"), config.browse_root)
    if not cwd.is_dir():
        raise RpcError("bad_request", "cwd is not a directory")
    runner = await m.create(
        cwd=str(cwd),
        model=params.get("model", "default"),
        mode=params.get("mode", "default"),
        resume=params.get("resume") or None,
        fork=bool(params.get("fork")),
        title=params.get("title", ""),
    )
    return runner.snapshot()


async def _sessions_stop(m: SessionManager, config: Config, params: dict) -> dict:
    runner = _runner(m, params)
    await runner.stop()
    return runner.snapshot()


async def _sessions_delete(m: SessionManager, config: Config, params: dict) -> dict:
    await m.delete(_require_str(params, "sid"))
    return {"ok": True}


async def _projects_recent(m: SessionManager, config: Config, params: dict) -> dict:
    return {"projects": await asyncio.to_thread(projects.recent_projects)}


async def _projects_sessions(m: SessionManager, config: Config, params: dict) -> dict:
    cwd = _require_str(params, "cwd")
    return {"sessions": await asyncio.to_thread(projects.past_sessions, cwd)}


async def _projects_preview(m: SessionManager, config: Config, params: dict) -> dict:
    session_id = _require_str(params, "session_id")
    return {"messages": await asyncio.to_thread(
        projects.session_preview, session_id, params.get("cwd"))}


async def _browse(m: SessionManager, config: Config, params: dict) -> dict:
    return await asyncio.to_thread(
        projects.browse, params.get("path"), config.browse_root)


async def _library(m: SessionManager, config: Config, params: dict) -> dict:
    return await asyncio.to_thread(library.get_library, params.get("cwd"))


async def _file_read(m: SessionManager, config: Config, params: dict) -> dict:
    p = _jailed_path(_require_str(params, "path"), config.browse_root)
    if not p.is_file():
        raise RpcError("not_found", "not a file")
    if p.stat().st_size > FILE_MAX_BYTES:
        raise RpcError("too_large", "file too large")
    try:
        return {"path": str(p), "content": p.read_text(errors="replace")}
    except OSError as e:
        raise RpcError("bad_request", str(e))


METHODS = {
    "state": _state,
    "sessions.create": _sessions_create,
    "sessions.stop": _sessions_stop,
    "sessions.delete": _sessions_delete,
    "projects.recent": _projects_recent,
    "projects.sessions": _projects_sessions,
    "projects.preview": _projects_preview,
    "browse": _browse,
    "library": _library,
    "file.read": _file_read,
}


async def dispatch(m: SessionManager, config: Config,
                   method: str, params: dict) -> dict:
    handler = METHODS.get(method)
    if handler is None:
        raise RpcError("unknown_method", f"unknown method: {method}")
    if not isinstance(params, dict):
        raise RpcError("bad_request", "params must be an object")
    return await handler(m, config, params)
