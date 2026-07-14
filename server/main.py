"""FastAPI app: auth, REST wrappers over rpc, WebSockets, static frontend."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

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

from . import VERSION, rpc
from .config import Config
from .rpc import RpcError
from .sessions import SessionManager
from .term_attach import attach_terminal
from .terminal import TerminalManager

WEB_DIR = Path(__file__).resolve().parent / "web"

config = Config.load()
manager: SessionManager | None = None
terms = TerminalManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    manager = SessionManager()
    yield
    terms.close_all()
    await manager.shutdown()


app = FastAPI(title="Better Claude RC", lifespan=lifespan)

COOKIE = "bcrc_token"

# One-time login codes (QR link): 2 minutes, single use. The link carries the
# code in the URL *fragment* — no secrets in query strings, ever.
LOGIN_CODE_TTL = 120.0
_login_codes: dict[str, float] = {}


def issue_login_code() -> str:
    now = time.time()
    for c, exp in list(_login_codes.items()):
        if exp < now:
            _login_codes.pop(c, None)
    code = secrets.token_urlsafe(16)
    _login_codes[code] = now + LOGIN_CODE_TTL
    return code


def _redeem_login_code(code: str) -> bool:
    exp = _login_codes.pop(code, None)  # single use
    return exp is not None and time.time() <= exp


def _token_ok(token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, config.token)


def _request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE)


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
    code = body.get("code")
    ok = _redeem_login_code(code) if code else _token_ok(body.get("token"))
    if not ok:
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


@app.get("/api/env")
async def api_env():
    """Boot-time transport selection for the PWA (no auth needed)."""
    return {"mode": "local"}


# ---------------- REST wrappers over the rpc method table ----------------
# Kept for curl and local debugging; the PWA talks rpc over /ws.

async def _rest(method: str, params: dict):
    try:
        return await rpc.dispatch(mgr(), config, method, params)
    except RpcError as e:
        raise HTTPException(status_code=rpc.HTTP_STATUS.get(e.code, 500),
                            detail=e.message)


@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return await _rest("state", {})


@app.post("/api/sessions", dependencies=[Depends(require_auth)])
async def create_session(request: Request):
    return await _rest("sessions.create", await request.json())


@app.post("/api/sessions/{sid}/stop", dependencies=[Depends(require_auth)])
async def stop_session(sid: str):
    return await _rest("sessions.stop", {"sid": sid})


@app.delete("/api/sessions/{sid}", dependencies=[Depends(require_auth)])
async def delete_session(sid: str):
    return await _rest("sessions.delete", {"sid": sid})


@app.get("/api/projects", dependencies=[Depends(require_auth)])
async def api_projects():
    return await _rest("projects.recent", {})


@app.get("/api/projects/sessions", dependencies=[Depends(require_auth)])
async def api_project_sessions(cwd: str):
    return await _rest("projects.sessions", {"cwd": cwd})


@app.get("/api/projects/preview", dependencies=[Depends(require_auth)])
async def api_session_preview(session_id: str, cwd: str | None = None):
    return await _rest("projects.preview", {"session_id": session_id, "cwd": cwd})


@app.get("/api/browse", dependencies=[Depends(require_auth)])
async def api_browse(path: str | None = None):
    return await _rest("browse", {"path": path})


@app.get("/api/library", dependencies=[Depends(require_auth)])
async def api_library(cwd: str | None = None):
    return await _rest("library", {"cwd": cwd})


@app.get("/api/file", dependencies=[Depends(require_auth)])
async def api_file(path: str):
    return await _rest("file.read", {"path": path})


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


def _origin_ok(ws: WebSocket) -> bool:
    """Same-origin allowlist for browser WS upgrades. Non-browser clients
    (no Origin header) pass — cross-site WebSocket hijacking is a browser
    attack, and browsers always send Origin on WS upgrades."""
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    try:
        o = urlparse(origin)
    except ValueError:
        return False
    if o.scheme not in ("http", "https") or not o.netloc:
        return False
    host = ws.headers.get("host", "")

    def norm(netloc: str, scheme: str) -> str:
        if ":" in netloc:
            return netloc
        return f"{netloc}:{443 if scheme == 'https' else 80}"

    return norm(o.netloc, o.scheme) == norm(host, o.scheme)


async def _ws_auth(ws: WebSocket) -> bool:
    """Cookie-only WS auth (no tokens in query strings) + Origin allowlist.
    Accepts the socket first so the browser can observe the close code."""
    await ws.accept()
    if not _token_ok(ws.cookies.get(COOKIE)):
        await ws.close(code=4401)
        return False
    if not _origin_ok(ws):
        await ws.close(code=4403)
        return False
    return True


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not await _ws_auth(ws):
        return
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
    if mtype == "rpc":
        rid = msg.get("id")
        try:
            result = await rpc.dispatch(m, config, str(msg.get("method") or ""),
                                        msg.get("params") or {})
            conn.push({"type": "rpc_result", "id": rid, "ok": True,
                       "result": result})
        except RpcError as e:
            conn.push({"type": "rpc_result", "id": rid, "ok": False,
                       "error": {"code": e.code, "message": e.message}})
        except Exception as e:
            conn.push({"type": "rpc_result", "id": rid, "ok": False,
                       "error": {"code": "internal", "message": str(e)}})
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

    if mtype in ("send", "compact", "clear"):
        # The chat is taking the session back — a live True View terminal
        # would be left behind the transcript, so close it first.
        terms.close(sid, "Continued from the app")

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


# ---------------- true view (real claude TUI over a PTY) ----------------

class _LocalTermTransport:
    """TermTransport over the local WebSocket: binary frames are raw
    terminal output, text frames are JSON control messages."""

    def __init__(self, ws: WebSocket):
        self.ws = ws

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def send_json(self, obj: dict) -> None:
        await self.ws.send_json(obj)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close()

    async def recv_json(self) -> dict | None:
        while True:
            try:
                msg = await self.ws.receive()
            except (WebSocketDisconnect, RuntimeError):
                return None
            if msg["type"] == "websocket.disconnect":
                return None
            text = msg.get("text")
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue


@app.websocket("/ws/term/{sid}")
async def ws_terminal(ws: WebSocket, sid: str):
    """Attach an xterm.js client to the session's PTY terminal. The body
    lives in term_attach.attach_terminal, shared with relay term channels.
    Server→client: binary frames are raw terminal output; text frames are
    JSON control messages ({"type": "exit"|"pong"}). Client→server: JSON
    text frames ({"type": "input"|"resize"|"ping"|"kill"})."""
    if not await _ws_auth(ws):
        return
    await attach_terminal(mgr(), terms, sid, _LocalTermTransport(ws))


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
