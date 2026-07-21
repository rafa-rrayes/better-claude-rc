"""Session engine: each UI session owns a ClaudeSDKClient (one `claude`
subprocess) plus a persisted, seq-numbered event log that web clients can
replay after reconnecting.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .config import SESSIONS_DIR
from .events import clip, clip_input, normalize_message, tool_summary
from .history import history_events
from .watch import rows_to_events, transcript_path

PERMISSION_MODES = ["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]
MODEL_CHOICES = ["default", "opus", "sonnet", "haiku"]

TAIL_POLL_SECS = 1.0
# Runner states during which our own subprocess writes the transcript —
# the tailer must not read then, or it would re-emit our own turns.
BUSY_STATES = ("starting", "working", "compacting", "awaiting")
# A terminal turn has no end marker in the transcript; treat it as over
# once no new rows have landed for this long.
EXT_TURN_QUIET_SECS = 30.0


def now_ms() -> int:
    return int(time.time() * 1000)


def _msg_snippet(ev: dict) -> dict | None:
    """Card-preview snippet for a message event (user_text / assistant text)."""
    kind = ev.get("kind")
    if kind not in ("user_text", "text"):
        return None
    text = " ".join(str(ev.get("text") or "").split())
    if not text:
        return None
    return {"role": "user" if kind == "user_text" else "assistant",
            "text": text[:280]}


class SessionRunner:
    def __init__(self, manager: "SessionManager", sid: str, cwd: str,
                 title: str = "", model: str = "default",
                 permission_mode: str = "default"):
        self.manager = manager
        self.sid = sid
        self.cwd = cwd
        self.title = title
        self.model = model or "default"
        self.mode = permission_mode or "default"
        self.created_at = now_ms()
        self.last_active = now_ms()

        self.state = "detached"       # starting|idle|working|awaiting|compacting|detached|error
        self.detail = ""
        self.claude_ids: list[str] = []
        self.cost_usd: float | None = None
        self.context: dict | None = None
        self.meta: dict = {}
        self.last_msg: dict | None = None   # {"role", "text"} card preview
        self._last_msg_scanned = True       # False → backfill from events.jsonl

        self.seq = 0
        self.events: list[dict] = []
        self._events_loaded = True    # False when constructed from disk lazily

        self.client: ClaudeSDKClient | None = None
        self._reader: asyncio.Task | None = None
        self._closing = False
        self._starting_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.pending_info: dict[str, dict] = {}
        self._stderr = deque(maxlen=60)

        # Terminal↔webapp sync: tail the claude transcript for turns
        # appended by other processes (a terminal on the same session).
        self.context_stale = False    # external turns our client hasn't seen
        self._watcher: asyncio.Task | None = None
        self._tail_ff = True          # fast-forward offset to EOF next tick
        self._ext_turn = False        # a terminal turn is in flight
        self._ext_ts = 0              # last external row seen (ms)
        self.ensure_watcher()

    # ---------- persistence ----------

    @property
    def dir(self) -> Path:
        return SESSIONS_DIR / self.sid

    def save_meta(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "meta.json").write_text(json.dumps({
            "sid": self.sid, "cwd": self.cwd, "title": self.title,
            "model": self.model, "mode": self.mode,
            "created_at": self.created_at, "last_active": self.last_active,
            "claude_ids": self.claude_ids, "cost_usd": self.cost_usd,
            "seq": self.seq, "last_msg": self.last_msg,
        }))

    @classmethod
    def from_disk(cls, manager: "SessionManager", meta: dict) -> "SessionRunner":
        r = cls(manager, meta["sid"], meta.get("cwd", ""),
                meta.get("title", ""), meta.get("model", "default"),
                meta.get("mode", "default"))
        r.created_at = meta.get("created_at", now_ms())
        r.last_active = meta.get("last_active", r.created_at)
        r.claude_ids = meta.get("claude_ids", [])
        r.cost_usd = meta.get("cost_usd")
        r.seq = meta.get("seq", 0)
        r.last_msg = meta.get("last_msg")
        r._last_msg_scanned = "last_msg" in meta  # older metas: scan events once
        r._events_loaded = False
        return r

    def ensure_events_loaded(self) -> None:
        if self._events_loaded:
            return
        self._events_loaded = True
        path = self.dir / "events.jsonl"
        if not path.exists():
            return
        events = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        with contextlib.suppress(json.JSONDecodeError):
                            events.append(json.loads(line))
        except OSError:
            pass
        self.events = events
        if events:
            self.seq = max(self.seq, events[-1].get("seq", 0))

    def _scan_last_msg(self) -> None:
        """Backfill last_msg for sessions whose meta.json predates it:
        read the tail of events.jsonl once and take the newest message."""
        self._last_msg_scanned = True
        path = self.dir / "events.jsonl"
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return
        for line in reversed(tail.splitlines()):
            with contextlib.suppress(json.JSONDecodeError):
                snip = _msg_snippet(json.loads(line))
                if snip:
                    self.last_msg = snip
                    return

    # ---------- event pipeline ----------

    def emit(self, ev: dict) -> None:
        transient = ev.pop("transient", False)
        if transient:
            self.manager.broadcast_event(self.sid, {**ev, "seq": None})
            return
        self.ensure_events_loaded()
        self.seq += 1
        ev = {"seq": self.seq, "ts": now_ms(), **ev}
        self.events.append(ev)
        self.last_msg = _msg_snippet(ev) or self.last_msg
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        except OSError:
            pass
        self.manager.broadcast_event(self.sid, ev)

    def set_status(self, state: str, detail: str = "") -> None:
        if state == self.state and detail == self.detail:
            return
        self.state = state
        self.detail = detail
        self.manager.broadcast_event(
            self.sid, {"kind": "status", "state": state, "detail": detail, "seq": None})
        self.manager.schedule_sessions_update()

    def notice(self, text: str, level: str = "info") -> None:
        self.emit({"kind": "notice", "level": level, "text": text})

    # ---------- terminal↔webapp sync ----------

    def ensure_watcher(self) -> None:
        if self._watcher is not None and not self._watcher.done():
            return
        with contextlib.suppress(RuntimeError):  # no loop yet (import time)
            self._watcher = asyncio.create_task(self._watch_loop())

    def cancel_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            self._watcher = None

    async def _watch_loop(self) -> None:
        """Tail this session's claude transcript for rows written by OTHER
        processes (terminal `claude` on the same session id) and mirror
        them into the event log live. Our own subprocess's rows are never
        re-emitted: tailing pauses while a turn runs and the offset
        fast-forwards to EOF when the turn's result lands."""
        cid: str | None = None
        path: Path | None = None
        offset: int | None = None  # None → seek to EOF on next read
        while True:
            await asyncio.sleep(TAIL_POLL_SECS)
            try:
                if (self._ext_turn
                        and now_ms() - self._ext_ts > EXT_TURN_QUIET_SECS * 1000):
                    self._ext_turn = False
                    if self.client is None and self.state == "working":
                        self.set_status("detached", "Synced from terminal")
                cur = self.claude_ids[-1] if self.claude_ids else None
                if cur is None:
                    continue
                if cur != cid or path is None:
                    cid = cur
                    path = await asyncio.to_thread(transcript_path, cid, self.cwd)
                    offset = None
                if path is None:
                    continue
                if self.state in BUSY_STATES and not self._ext_turn:
                    continue
                if self._tail_ff:
                    self._tail_ff = False
                    offset = None
                try:
                    size = path.stat().st_size
                except OSError:
                    path = None
                    continue
                if offset is None or size < offset:
                    offset = size
                    continue
                if size == offset:
                    continue
                chunk = await asyncio.to_thread(self._read_span, path, offset, size)
                end = chunk.rfind(b"\n")
                if end < 0:
                    continue  # partial line still being written
                offset += end + 1
                evs = rows_to_events(
                    chunk[:end].decode("utf-8", errors="replace").splitlines())
                if not evs:
                    continue
                self.context_stale = True
                self.last_active = now_ms()
                for ev in evs:
                    self.emit(ev)
                self._ext_ts = now_ms()
                if self.client is None:
                    self._ext_turn = True
                    detail = "Running in terminal…"
                    for ev in reversed(evs):
                        if ev.get("kind") == "tool_start":
                            tool, summ = ev.get("tool", ""), ev.get("summary") or ""
                            detail = f"{tool}: {summ}" if summ else f"Running {tool}"
                            break
                    self.set_status("working", detail)
                elif self.state == "idle":
                    self.set_status("idle", "Synced from terminal")
                self.save_meta()
                self.manager.schedule_sessions_update()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue  # never let a tail hiccup kill the mirror

    @staticmethod
    def _read_span(path: Path, start: int, end: int) -> bytes:
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(end - start)

    async def _sync_client(self) -> None:
        """Make the live client's context match the transcript on disk —
        rotate through resume when a terminal appended turns behind us."""
        if self.context_stale and self.client is not None:
            await self._teardown_client()
        self.context_stale = False
        await self.ensure_started()

    # ---------- lifecycle ----------

    def _build_options(self, resume: str | None, fork: bool) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.cwd,
            model=None if self.model in ("", "default") else self.model,
            permission_mode=self.mode,
            include_partial_messages=True,
            include_hook_events=True,
            can_use_tool=self._on_permission,
            stderr=lambda line: self._stderr.append(line),
            resume=resume,
            fork_session=fork,
            # setting_sources=None → load user/project/local settings like the
            # terminal CLI, so CLAUDE.md, MCP servers, hooks and plugins match.
        )

    async def start(self, resume: str | None = None, fork: bool = False,
                    announce: bool = True) -> None:
        async with self._starting_lock:
            if self.client is not None:
                return
            self.ensure_events_loaded()
            self._closing = False
            self._ext_turn = False  # our subprocess owns the transcript now
            self.set_status("starting", "Starting Claude Code…")
            client = ClaudeSDKClient(self._build_options(resume, fork))
            try:
                await client.connect()
            except Exception as e:
                self.set_status("error", "Failed to start")
                tail = "\n".join(list(self._stderr)[-8:])
                self.notice(f"Failed to start session: {e}\n{tail}", "error")
                raise
            self.client = client
            self._reader = asyncio.create_task(self._read_loop(client))
            self._tail_ff = True  # skip rows our own boot writes (hooks etc.)
            self.set_status("idle", "Ready")
            if resume and announce:
                self.notice("Resumed session" + (" (forked)" if fork else ""))
            self.ensure_watcher()
            asyncio.create_task(self._post_connect())

    async def _post_connect(self) -> None:
        client = self.client
        if client is None:
            return
        with contextlib.suppress(Exception):
            info = await client.get_server_info()
            if info:
                cmds = []
                for c in info.get("commands") or []:
                    if isinstance(c, dict):
                        cmds.append({"name": c.get("name", ""),
                                     "description": c.get("description", ""),
                                     "argument_hint": c.get("argumentHint")
                                     or c.get("argument_hint", "")})
                    else:
                        cmds.append({"name": str(c), "description": "",
                                     "argument_hint": ""})
                if cmds:
                    self.meta["commands"] = cmds
                    self.emit({"kind": "commands", "commands": cmds})
        await self.refresh_context()

    async def _read_loop(self, client: ClaudeSDKClient) -> None:
        try:
            async for msg in client.receive_messages():
                if self.client is not client:
                    break
                for ev in normalize_message(msg):
                    self._handle(ev)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if not self._closing and self.client is client:
                tail = "\n".join(list(self._stderr)[-8:])
                self.notice(f"Session process ended: {e}"
                            + (f"\n{tail}" if tail.strip() else ""), "error")
        finally:
            if self.client is client:
                self.client = None
                if not self._closing:
                    self._fail_pending("Session process ended")
                    self.set_status("detached", "Process ended — will resume on next message")

    def _handle(self, ev: dict) -> None:
        kind = ev.get("kind")
        if kind == "meta":
            csid = ev.get("claude_session_id")
            if csid and csid not in self.claude_ids:
                self.claude_ids.append(csid)
            self.meta.update({k: v for k, v in ev.items() if k != "kind"})
            self.save_meta()
        elif kind == "delta" or kind == "text_open":
            self.set_status("working", "Writing…")
        elif kind == "thinking_delta" or kind == "thinking_open":
            self.set_status("working", "Thinking…")
        elif kind == "status_hint":
            self.set_status("working", ev.get("detail", "Working…"))
            return  # purely transient
        elif kind == "tool_start":
            summary = ev.get("summary") or ""
            tool = ev.get("tool", "")
            self.set_status("working", f"{tool}: {summary}" if summary else f"Running {tool}")
        elif kind == "result":
            self.last_active = now_ms()
            if ev.get("cost_usd") is not None:
                self.cost_usd = ev["cost_usd"]
            csid = ev.get("claude_session_id")
            if csid and csid not in self.claude_ids:
                self.claude_ids.append(csid)
            if ev.get("is_error"):
                errs = ev.get("errors") or []
                self.set_status("idle", "Turn failed")
                if ev.get("subtype") != "success" or errs:
                    self.notice("Turn ended with error: "
                                + (", ".join(map(str, errs)) or ev.get("subtype", "")),
                                "error")
            else:
                secs = (ev.get("duration_ms") or 0) / 1000
                cost = ev.get("cost_usd")
                self.set_status("idle", f"Done in {secs:.0f}s"
                                + (f" · ${cost:.2f} total" if cost else ""))
            self.save_meta()
            self._tail_ff = True  # skip our own turn's transcript rows
            asyncio.create_task(self.refresh_context())
        elif kind == "compact":
            if self.state == "compacting":
                self.set_status("idle", "Compacted")
            self._tail_ff = True
            asyncio.create_task(self.refresh_context())
        self.emit(ev)

    def _fail_pending(self, reason: str) -> None:
        for rid, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_result({"behavior": "deny", "message": reason})
        self.pending.clear()

    # ---------- permissions ----------

    async def _on_permission(self, tool_name: str, tool_input: dict,
                             context: ToolPermissionContext) -> Any:
        rid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut

        if tool_name == "AskUserQuestion":
            ev = {"kind": "question", "request_id": rid,
                  "questions": tool_input.get("questions") or []}
        elif tool_name == "ExitPlanMode":
            ev = {"kind": "plan_approval", "request_id": rid,
                  "plan": clip(tool_input.get("plan") or "", 20000)}
        else:
            suggestions = []
            for s in (context.suggestions or []):
                with contextlib.suppress(Exception):
                    suggestions.append(s.to_dict())
            ev = {
                "kind": "permission", "request_id": rid,
                "tool": tool_name,
                "input": clip_input(tool_input),
                "summary": tool_summary(tool_name, tool_input),
                "title": context.title or "",
                "display_name": context.display_name or "",
                "description": context.description or "",
                "reason": context.decision_reason or "",
                "suggestions": suggestions,
            }
        self.pending_info[rid] = {"tool": tool_name, "input": tool_input,
                                  "context": context}
        self.emit(ev)
        self.set_status("awaiting", f"Needs your approval: {tool_name}")
        self.manager.schedule_sessions_update()

        try:
            resp: dict = await fut
        finally:
            self.pending.pop(rid, None)
            info = self.pending_info.pop(rid, None)

        self.set_status("working", "Continuing…")
        behavior = resp.get("behavior", "deny")
        self.emit({"kind": "permission_resolved", "request_id": rid,
                   "behavior": behavior, "note": resp.get("message", "")})

        if tool_name == "AskUserQuestion" and behavior == "allow":
            return PermissionResultAllow(updated_input={
                "questions": tool_input.get("questions") or [],
                "answers": resp.get("answers") or {},
            })
        if behavior == "allow":
            updated_permissions = None
            picked = resp.get("apply_suggestions") or []
            if picked and info:
                sugg = info["context"].suggestions or []
                chosen = []
                for i in picked:
                    if isinstance(i, int) and 0 <= i < len(sugg):
                        s = sugg[i]
                        if getattr(s, "destination", None) is None:
                            s = dataclasses.replace(s, destination="session")
                        chosen.append(s)
                updated_permissions = chosen or None
            return PermissionResultAllow(
                updated_input=resp.get("updated_input") or None,
                updated_permissions=updated_permissions,
            )
        return PermissionResultDeny(
            message=resp.get("message") or "User denied this action",
            interrupt=bool(resp.get("interrupt", False)),
        )

    def respond_permission(self, request_id: str, resp: dict) -> bool:
        fut = self.pending.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(resp)
        return True

    async def backfill_history(self, claude_id: str) -> None:
        """Replay a past session's transcript into this UI session's log,
        so resuming shows the whole prior conversation like the terminal."""
        self.ensure_events_loaded()
        if any(ev.get("kind") == "resumed" for ev in self.events):
            return
        evs, total = await asyncio.to_thread(history_events, claude_id, self.cwd)
        for ev in evs:
            self.emit(ev)
        self.emit({"kind": "resumed", "claude_session_id": claude_id,
                   "shown": len(evs), "total": total})

    # ---------- user actions ----------

    async def ensure_started(self) -> None:
        if self.client is not None:
            return
        resume = self.claude_ids[-1] if self.claude_ids else None
        # announce=False: auto-resume (after restart or terminal handoff)
        # should be invisible, like the terminal picking up where it left off.
        await self.start(resume=resume, announce=False)

    async def send(self, text: str) -> None:
        text = (text or "").rstrip()
        if not text:
            return
        self.last_active = now_ms()
        if not self.title:
            self.title = text.splitlines()[0][:64]
        self.emit({"kind": "user_text", "text": text})
        self.save_meta()
        await self._sync_client()
        self.set_status("working", "Sending…")
        assert self.client is not None
        await self.client.query(text)

    async def interrupt(self) -> None:
        self._fail_pending("Interrupted by user")
        if self.client is None:
            self.set_status("detached", "")
            return
        try:
            await self.client.interrupt()
            self.notice("Interrupted")
            self.set_status("idle", "Interrupted")
        except Exception as e:
            self.notice(f"Interrupt failed: {e}", "warn")

    async def clear(self) -> None:
        """/clear — fresh context in the same chat thread."""
        self._fail_pending("Conversation cleared")
        await self._teardown_client()
        self.emit({"kind": "cleared"})
        self.context = None
        self.context_stale = False
        await self.start()
        self.notice("Conversation cleared — fresh context started")

    async def compact(self, instructions: str = "") -> None:
        await self._sync_client()
        self.set_status("compacting", "Compacting conversation…")
        self.notice("Compacting conversation…")
        assert self.client is not None
        await self.client.query("/compact" + (f" {instructions}" if instructions else ""))

    async def set_model(self, model: str) -> None:
        self.model = model or "default"
        if self.client is not None:
            await self.client.set_model(None if self.model == "default" else self.model)
        self.save_meta()
        self.notice(f"Model set to {self.model}"
                    + ("" if self.client else " (applies when session starts)"))
        self.manager.schedule_sessions_update()

    async def set_mode(self, mode: str) -> None:
        if mode not in PERMISSION_MODES:
            raise ValueError(f"invalid mode {mode}")
        self.mode = mode
        if self.client is not None:
            await self.client.set_permission_mode(mode)
        self.save_meta()
        self.notice(f"Permission mode: {mode}")
        self.manager.schedule_sessions_update()

    async def refresh_context(self) -> None:
        client = self.client
        if client is None:
            return
        with contextlib.suppress(Exception):
            usage = await client.get_context_usage()
            self.context = {
                "total_tokens": usage.get("totalTokens"),
                "max_tokens": usage.get("maxTokens"),
                "percentage": round(usage.get("percentage") or 0, 1),
                "model": usage.get("model"),
                "categories": [
                    {"name": c.get("name"), "tokens": c.get("tokens")}
                    for c in (usage.get("categories") or [])
                ],
            }
            self.manager.broadcast_event(
                self.sid, {"kind": "context", **self.context, "seq": None})
            self.manager.schedule_sessions_update()

    async def _teardown_client(self) -> None:
        client, self.client = self.client, None
        self._closing = True
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        self._closing = False

    async def detach_for_terminal(self) -> None:
        """Hand the claude session over to a True View terminal: stop our
        subprocess so the TUI owns the transcript. The watcher keeps
        mirroring its turns into the chat log, and the next chat message
        resumes our client via the context_stale rotation."""
        self._fail_pending("Handed off to terminal view")
        await self._teardown_client()
        self._tail_ff = True  # mirror only rows the terminal writes from now
        self.set_status("detached", "In terminal view")
        self.save_meta()

    async def stop(self) -> None:
        """Stop the subprocess; session stays resumable."""
        self._fail_pending("Session stopped")
        await self._teardown_client()
        self.set_status("detached", "Stopped — resumes on next message")
        self.notice("Session process stopped")
        self.save_meta()

    def snapshot(self) -> dict:
        if self.last_msg is None and not self._last_msg_scanned:
            self._scan_last_msg()
        proj = Path(self.cwd)
        return {
            "sid": self.sid,
            "title": self.title or "(new session)",
            "cwd": self.cwd,
            "project": proj.name or self.cwd,
            "state": self.state,
            "detail": self.detail,
            "model": self.meta.get("model") or self.model,
            "mode": self.mode,
            "cost_usd": self.cost_usd,
            "context_pct": (self.context or {}).get("percentage"),
            "claude_session_id": self.claude_ids[-1] if self.claude_ids else None,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "live": self.client is not None,
            "pending_permissions": len(self.pending),
            "last_msg": self.last_msg,
        }


class SessionManager:
    def __init__(self) -> None:
        self.runners: dict[str, SessionRunner] = {}
        self.conns: set[Any] = set()   # WSConn objects (main.py)
        self._sessions_update_handle: asyncio.Task | None = None
        self.load_from_disk()

    def load_from_disk(self) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        for meta_path in SESSIONS_DIR.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text())
                runner = SessionRunner.from_disk(self, meta)
                self.runners[runner.sid] = runner
            except (OSError, json.JSONDecodeError, KeyError):
                continue

    # ---------- session ops ----------

    async def create(self, cwd: str, model: str = "default",
                     mode: str = "default", resume: str | None = None,
                     fork: bool = False, title: str = "") -> SessionRunner:
        sid = uuid.uuid4().hex[:10]
        runner = SessionRunner(self, sid, cwd, title=title, model=model,
                               permission_mode=mode)
        if resume:
            runner.claude_ids.append(resume)
        self.runners[sid] = runner
        runner.save_meta()
        self.schedule_sessions_update()

        async def _bg_start():
            if resume:
                with contextlib.suppress(Exception):
                    await runner.backfill_history(resume)
            with contextlib.suppress(Exception):
                await runner.start(resume=resume, fork=fork)
        asyncio.create_task(_bg_start())
        return runner

    def get(self, sid: str) -> SessionRunner | None:
        return self.runners.get(sid)

    async def delete(self, sid: str) -> None:
        runner = self.runners.pop(sid, None)
        if runner is None:
            return
        runner.cancel_watcher()
        await runner.stop()
        with contextlib.suppress(OSError):
            for p in runner.dir.iterdir():
                p.unlink()
            runner.dir.rmdir()
        self.schedule_sessions_update()

    async def shutdown(self) -> None:
        await asyncio.gather(*(r.stop() for r in self.runners.values()
                               if r.client is not None),
                             return_exceptions=True)

    def snapshots(self) -> list[dict]:
        snaps = [r.snapshot() for r in self.runners.values()]
        snaps.sort(key=lambda s: s["last_active"], reverse=True)
        return snaps

    # ---------- broadcast ----------

    def broadcast_event(self, sid: str, ev: dict) -> None:
        for conn in list(self.conns):
            if sid in conn.subs:
                conn.push({"type": "event", "session_id": sid, "ev": ev})

    def schedule_sessions_update(self) -> None:
        if self._sessions_update_handle and not self._sessions_update_handle.done():
            return

        async def _fire():
            await asyncio.sleep(0.15)
            payload = {"type": "sessions", "sessions": self.snapshots()}
            for conn in list(self.conns):
                conn.push(payload)
        try:
            self._sessions_update_handle = asyncio.create_task(_fire())
        except RuntimeError:
            pass
