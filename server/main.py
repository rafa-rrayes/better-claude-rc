"""FastAPI app: auth, REST API, multiplexed WebSocket, static frontend."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import library, projects
from .config import Config
from .sessions import MODEL_CHOICES, PERMISSION_MODES, SessionManager

VERSION = "0.1.0"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

config = Config.load()
manager: SessionManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = SessionManager()
    yield
    await manager.shutdown()


app = FastAPI(title="Better Claude RC", lifespan=lifespan)

COOKIE = "bcrc_token"


def _token_ok(token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, config.token)


def _request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE) or request.query_params.get("token")


async def require_auth(request: Request) -> None:
    if not _token_ok(_request_token(request)):
        raise HTTPException(status_code=401, detail="unauthorized")


def mgr() -> SessionManager:
    assert manager is not None
    return manager


# ---------------- auth ----------------

@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    if not _token_ok(body.get("token")):
        raise HTTPException(status_code=401, detail="bad token")
    response.set_cookie(
        COOKIE, config.token, max_age=180 * 24 * 3600,
        httponly=True, samesite="lax",
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------- state / sessions ----------------

@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return {
        "ok": True,
        "version": VERSION,
        "home": str(Path.home()),
        "sessions": mgr().snapshots(),
        "defaults": {"models": MODEL_CHOICES, "modes": PERMISSION_MODES},
    }


@app.post("/api/sessions", dependencies=[Depends(require_auth)])
async def create_session(request: Request):
    body = await request.json()
    cwd = body.get("cwd", "")
    if not cwd or not Path(cwd).is_dir():
        raise HTTPException(status_code=400, detail="cwd is not a directory")
    runner = await mgr().create(
        cwd=cwd,
        model=body.get("model", "default"),
        mode=body.get("mode", "default"),
        resume=body.get("resume"),
        fork=bool(body.get("fork")),
        title=body.get("title", ""),
    )
    return runner.snapshot()


@app.post("/api/sessions/{sid}/stop", dependencies=[Depends(require_auth)])
async def stop_session(sid: str):
    runner = mgr().get(sid)
    if runner is None:
        raise HTTPException(status_code=404, detail="no such session")
    await runner.stop()
    return runner.snapshot()


@app.delete("/api/sessions/{sid}", dependencies=[Depends(require_auth)])
async def delete_session(sid: str):
    await mgr().delete(sid)
    return {"ok": True}


# ---------------- discovery ----------------

@app.get("/api/projects", dependencies=[Depends(require_auth)])
async def api_projects():
    return {"projects": await asyncio.to_thread(projects.recent_projects)}


@app.get("/api/projects/sessions", dependencies=[Depends(require_auth)])
async def api_project_sessions(cwd: str):
    return {"sessions": await asyncio.to_thread(projects.past_sessions, cwd)}


@app.get("/api/projects/preview", dependencies=[Depends(require_auth)])
async def api_session_preview(session_id: str, cwd: str | None = None):
    return {"messages": await asyncio.to_thread(
        projects.session_preview, session_id, cwd)}


@app.get("/api/browse", dependencies=[Depends(require_auth)])
async def api_browse(path: str | None = None):
    return await asyncio.to_thread(projects.browse, path, config.browse_root)


@app.get("/api/library", dependencies=[Depends(require_auth)])
async def api_library(cwd: str | None = None):
    return await asyncio.to_thread(library.get_library, cwd)


@app.get("/api/file", dependencies=[Depends(require_auth)])
async def api_file(path: str):
    p = Path(path).expanduser()
    if not str(p.resolve()).startswith(config.browse_root):
        raise HTTPException(status_code=403, detail="outside browse root")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="not a file")
    if p.stat().st_size > 512 * 1024:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        return {"path": str(p), "content": p.read_text(errors="replace")}
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- websocket ----------------

class WSConn:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.subs: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._sender: asyncio.Task | None = None

    def push(self, payload: dict) -> None:
        self.queue.put_nowait(payload)

    async def _send_loop(self) -> None:
        while True:
            payload = await self.queue.get()
            await self.ws.send_text(json.dumps(payload, default=str))

    def start(self) -> None:
        self._sender = asyncio.create_task(self._send_loop())

    async def close(self) -> None:
        if self._sender:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = ws.cookies.get(COOKIE) or ws.query_params.get("token")
    if not _token_ok(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    m = mgr()
    conn = WSConn(ws)
    conn.start()
    m.conns.add(conn)
    conn.push({"type": "hello", "version": VERSION})
    conn.push({"type": "sessions", "sessions": m.snapshots()})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                await handle_ws(m, conn, msg)
            except Exception as e:
                conn.push({"type": "error", "message": str(e),
                           "ref": msg.get("type")})
    except WebSocketDisconnect:
        pass
    finally:
        m.conns.discard(conn)
        await conn.close()


async def handle_ws(m: SessionManager, conn: WSConn, msg: dict) -> None:
    mtype = msg.get("type")
    if mtype == "ping":
        conn.push({"type": "pong"})
        return
    if mtype == "sessions":
        conn.push({"type": "sessions", "sessions": m.snapshots()})
        return

    sid = msg.get("session_id", "")
    runner = m.get(sid)

    if mtype == "attach":
        if runner is None:
            conn.push({"type": "error", "message": "no such session", "ref": sid})
            return
        runner.ensure_events_loaded()
        since = int(msg.get("since_seq") or 0)
        events = [e for e in runner.events if (e.get("seq") or 0) > since]
        conn.subs.add(sid)
        conn.push({
            "type": "attached", "session_id": sid,
            "snapshot": runner.snapshot(),
            "status": {"state": runner.state, "detail": runner.detail},
            "context": runner.context,
            "meta": runner.meta,
            "events": events,
        })
        return

    if mtype == "detach":
        conn.subs.discard(sid)
        return

    if runner is None:
        conn.push({"type": "error", "message": "no such session", "ref": sid})
        return

    if mtype == "send":
        asyncio.create_task(_safe(runner.send(msg.get("text", "")), runner))
    elif mtype == "interrupt":
        asyncio.create_task(_safe(runner.interrupt(), runner))
    elif mtype == "permission":
        ok = runner.respond_permission(msg.get("request_id", ""), msg)
        if not ok:
            conn.push({"type": "error",
                       "message": "permission request no longer pending"})
    elif mtype == "clear":
        asyncio.create_task(_safe(runner.clear(), runner))
    elif mtype == "compact":
        asyncio.create_task(_safe(runner.compact(msg.get("instructions", "")), runner))
    elif mtype == "set_model":
        asyncio.create_task(_safe(runner.set_model(msg.get("model", "default")), runner))
    elif mtype == "set_mode":
        asyncio.create_task(_safe(runner.set_mode(msg.get("mode", "default")), runner))
    elif mtype == "refresh_context":
        asyncio.create_task(_safe(runner.refresh_context(), runner))


async def _safe(coro, runner) -> None:
    try:
        await coro
    except Exception as e:
        with contextlib.suppress(Exception):
            runner.notice(f"Action failed: {e}", "error")


# ---------------- static frontend ----------------

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/ws")):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
