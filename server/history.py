"""Convert a past session's transcript into replayable UI events.

Used when a UI session resumes an existing claude session so the phone
shows the full prior conversation, exactly like scrolling up in the
terminal. The SDK's get_session_messages() already filters subagent
sidechains and returns only top-level user/assistant rows.
"""

from __future__ import annotations

import re

from claude_agent_sdk import get_session_messages

from .events import _tool_result_text, clip, clip_input, tool_summary

HISTORY_TAIL = 400  # max transcript rows converted (oldest dropped)

_COMPACT_PREFIX = "This session is being continued from a previous conversation"


def _clean_user_text(text: str) -> str:
    """Strip harness noise from transcript user rows; '' means skip."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text,
                  flags=re.S).strip()
    if not text or text.startswith("Caveat: The messages below"):
        return ""
    if text.startswith(_COMPACT_PREFIX):
        return ""  # rendered as a compact divider by the caller
    if "<command-name>" in text:
        m = re.search(r"<command-name>(.*?)</command-name>", text, flags=re.S)
        name = (m.group(1).strip() if m else "").strip()
        args = re.search(r"<command-args>(.*?)</command-args>", text, flags=re.S)
        arg = (args.group(1).strip() if args else "")
        return (name + (" " + arg if arg else "")) if name else ""
    if text.startswith(("<local-command", "<session-start-hook")):
        return ""
    return text


def _user_events(content) -> list[dict]:
    evs: list[dict] = []
    if isinstance(content, str):
        if content.startswith(_COMPACT_PREFIX):
            return [{"kind": "compact", "trigger": "past", "pre_tokens": None}]
        cleaned = _clean_user_text(content)
        if cleaned:
            evs.append({"kind": "user_text", "text": clip(cleaned)})
        return evs
    if not isinstance(content, list):
        return evs
    texts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "tool_result":
            evs.append({
                "kind": "tool_result",
                "tool_use_id": block.get("tool_use_id"),
                "text": _tool_result_text(block.get("content")),
                "is_error": bool(block.get("is_error")),
            })
        elif btype == "image":
            texts.append("[image]")
    joined = "\n".join(t for t in texts if t).strip()
    if joined and joined.startswith(_COMPACT_PREFIX):
        return [{"kind": "compact", "trigger": "past", "pre_tokens": None}] + evs
    cleaned = _clean_user_text(joined) if joined else ""
    if cleaned:
        evs.insert(0, {"kind": "user_text", "text": clip(cleaned)})
    return evs


def _assistant_events(content) -> list[dict]:
    evs: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            evs.append({"kind": "text", "text": clip(content)})
        return evs
    if not isinstance(content, list):
        return evs
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            if (block.get("text") or "").strip():
                evs.append({"kind": "text", "text": clip(block["text"])})
        elif btype == "thinking":
            if (block.get("thinking") or "").strip():
                evs.append({"kind": "thinking", "text": clip(block["thinking"])})
        elif btype == "tool_use":
            name = block.get("name", "")
            tin = block.get("input") or {}
            evs.append({
                "kind": "tool_start",
                "tool_use_id": block.get("id"),
                "tool": name,
                "input": clip_input(tin),
                "summary": tool_summary(name, tin),
                "parent_tool_use_id": None,
            })
    return evs


def history_events(session_id: str, cwd: str | None,
                   tail: int = HISTORY_TAIL) -> tuple[list[dict], int]:
    """(events, total_transcript_rows) for a past session."""
    try:
        messages = get_session_messages(session_id, directory=cwd)
    except Exception:
        return [], 0
    total = len(messages)
    if total > tail:
        messages = messages[-tail:]
    evs: list[dict] = []
    for m in messages:
        raw = m.message if isinstance(m.message, dict) else {}
        content = raw.get("content")
        if m.type == "user":
            evs.extend(_user_events(content))
        elif m.type == "assistant":
            evs.extend(_assistant_events(content))
    return evs, total
