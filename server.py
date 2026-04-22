#!/usr/bin/env python3
"""
Context Window Viewer
=====================

A zero-dependency local web app that lets you browse every conversation on your
machine from Claude Code / Cowork and Codex CLI and inspect the raw context
window as an expandable tree.

Default session roots (auto-detected per OS):
  macOS    ~/.claude/projects
           ~/Library/Application Support/Claude/local-agent-mode-sessions
           ~/.codex/sessions, ~/.codex/archived_sessions
  Linux    ~/.claude/projects
           ~/.config/Claude/local-agent-mode-sessions
           ~/.codex/sessions, ~/.codex/archived_sessions
  Windows  %USERPROFILE%\\.claude\\projects
           %APPDATA%\\Claude\\local-agent-mode-sessions
           %USERPROFILE%\\.codex\\sessions, %USERPROFILE%\\.codex\\archived_sessions

Configuration (resolved in priority order):
  1. CLI flags               --claude DIR / --codex DIR (repeatable)
  2. Environment variables   CLAUDE_SESSIONS_PATH, CODEX_SESSIONS_PATH,
                             HOST, PORT
                             (path lists use the OS path separator:
                              ':' on mac/linux, ';' on Windows)
  3. .env file               same keys as above, loaded from CWD or the
                             directory containing this script
  4. Built-in defaults       the per-OS paths listed above

Usage
-----
    python3 server.py                    # opens on http://127.0.0.1:8765
    python3 server.py --port 9000
    python3 server.py --claude DIR       # override Claude sessions root
    python3 server.py --codex  DIR       # override Codex sessions root

Design
------
* One required dependency: tiktoken (for token counting).
* Walks each root for *.jsonl files and parses each line as JSON.
* Normalises wildly different event shapes into a single Block model that the
  UI renders as a collapsible tree.
* Token counts use tiktoken's cl100k_base encoding.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, parse_qs


# ---------- tokenizer -----------------------------------------------------
import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")


def est_tokens(text) -> int:
    if not text:
        return 0
    # Defensive: parsers should always produce string text, but a dict/list
    # slipping through shouldn't crash the whole session render.
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(text)
    return len(_ENC.encode(text, disallowed_special=()))


# ---------- data model ----------------------------------------------------
@dataclass
class Block:
    """Single renderable node in the context tree."""
    kind: str          # system | user | assistant | tool_use | tool_result | meta | raw
    label: str         # short summary shown in the collapsed row
    text: str = ""     # full text body (preview on the right)
    children: list["Block"] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        own_tokens = est_tokens(self.text)
        # `tokens` historically meant own + immediate children. Keep that for
        # compatibility with the mini-bar, but also expose the own-text count
        # so the client can aggregate without unit-mismatched chars/4 math.
        return {
            "kind": self.kind,
            "label": self.label,
            "text": self.text,
            "children": [c.to_json() for c in self.children],
            "meta": self.meta,
            "chars": len(self.text) + sum(len(c.text) for c in self.children),
            "ownTokens": own_tokens,
            "tokens": own_tokens + sum(est_tokens(c.text) for c in self.children),
        }


@dataclass
class Session:
    id: str
    client: str          # "claude" or "codex"
    title: str
    path: str
    mtime: float
    blocks: list[Block] = field(default_factory=list)
    # Session-level metadata snapshotted from the first event that carries it
    # (entrypoint, cwd, gitBranch, version, slug, sessionId, userType). These
    # are repeated verbatim on every Claude Code event, so we lift them once.
    context: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict:
        total_chars = sum(len(b.text) for b in _walk(self.blocks))
        total_tokens = sum(est_tokens(b.text) for b in _walk(self.blocks))
        return {
            "id": self.id,
            "client": self.client,
            "title": self.title,
            "path": self.path,
            "mtime": self.mtime,
            "blockCount": len(self.blocks),
            "chars": total_chars,
            "tokens": total_tokens,
            "detectedModel": self._detect_model(),
            "context": self.context,
        }

    def full(self) -> dict:
        return {**self.summary(), "blocks": [b.to_json() for b in self.blocks]}

    def _detect_model(self) -> str | None:
        """Return the most-frequently-seen model string across all blocks."""
        counts: dict[str, int] = {}
        for b in _walk(self.blocks):
            m = (b.meta or {}).get("model") if isinstance(b.meta, dict) else None
            if isinstance(m, str) and m:
                counts[m] = counts.get(m, 0) + 1
        if not counts:
            return None
        return max(counts.items(), key=lambda kv: kv[1])[0]


def _walk(blocks: Iterable[Block]) -> Iterable[Block]:
    for b in blocks:
        yield b
        yield from _walk(b.children)


# ---------- parsing helpers -----------------------------------------------
def _stringify(value: Any, limit: int = 200_000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)[:limit]
    except (TypeError, ValueError):
        return repr(value)[:limit]


def _short(text: str, n: int = 80) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def humanize_bytes(n) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if n >= 10 or unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_PERSISTED_RE = re.compile(r"^<persisted-output>", re.MULTILINE)


def _blocks_from_content(content: Any, tool_use_result: Any = None) -> list[Block]:
    """
    Claude-style `message.content` is either a string or a list of blocks like
    [{"type": "text", "text": ...}, {"type": "tool_use", ...},
     {"type": "tool_result", "content": [...]}].

    `tool_use_result`, when given, is the top-level `toolUseResult` field from
    the outer user event. Claude Code stashes the full stdout (up to ~30K) and
    a persisted-output pointer there, while `message.content` only has a ~2KB
    preview the model actually saw. We keep block.text = what-the-model-saw
    (correct for token math) and attach stdout / persistedOutputPath in meta
    so the UI can surface the real content on demand.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [Block(kind="text", label=_short(content) or "(empty text)", text=content)]
    if not isinstance(content, list):
        return [Block(kind="raw", label="(non-standard content)", text=_stringify(content))]

    out: list[Block] = []
    for item in content:
        if not isinstance(item, dict):
            out.append(Block(kind="raw", label="(raw block)", text=_stringify(item)))
            continue
        t = item.get("type", "unknown")
        if t == "text":
            txt = item.get("text", "")
            out.append(Block(kind="text", label=_short(txt) or "(empty text)", text=txt))
        elif t == "thinking":
            txt = item.get("thinking") or item.get("text", "")
            out.append(Block(
                kind="thinking",
                label=f"thinking: {_short(txt)}",
                text=txt,
                meta={"signature": item.get("signature")},
            ))
        elif t == "tool_use":
            name = item.get("name", "tool")
            inp = _stringify(item.get("input"))
            out.append(Block(
                kind="tool_use",
                label=f"{name}({_short(inp, 60)})",
                text=inp,
                meta={"id": item.get("id"), "name": name},
            ))
        elif t == "tool_result":
            inner = item.get("content", "")
            sub = _blocks_from_content(inner) if not isinstance(inner, str) else [
                Block(kind="text", label=_short(inner) or "(empty)", text=inner)
            ]
            # The children carry the actual text payload; keep the parent's own
            # `text` empty so token/char aggregates don't double-count it.
            inner_tokens = sum(est_tokens(b.text) for b in sub)
            meta: dict[str, Any] = {
                "tool_use_id": item.get("tool_use_id"),
                "is_error": item.get("is_error"),
            }
            # Detect Claude Code's persisted-output pattern: the 2KB preview
            # string starts with "<persisted-output>". When we see that AND an
            # outer toolUseResult exists, lift its stdout + persisted pointer
            # into meta so the client can show the real payload on demand.
            is_persisted = (
                isinstance(inner, str) and _PERSISTED_RE.match(inner.lstrip())
                or isinstance(tool_use_result, dict) and tool_use_result.get("persistedOutputPath")
            )
            if is_persisted and isinstance(tool_use_result, dict):
                meta["persisted"] = True
                if tool_use_result.get("stdout"):
                    meta["fullStdout"] = tool_use_result["stdout"]
                if tool_use_result.get("stderr"):
                    meta["stderr"] = tool_use_result["stderr"]
                if tool_use_result.get("persistedOutputPath"):
                    meta["persistedOutputPath"] = tool_use_result["persistedOutputPath"]
                if tool_use_result.get("persistedOutputSize"):
                    meta["persistedOutputSize"] = tool_use_result["persistedOutputSize"]
            size_note = ""
            if meta.get("persistedOutputSize"):
                size_note = f" · full {humanize_bytes(meta['persistedOutputSize'])} on disk"
            out.append(Block(
                kind="tool_result",
                label=f"tool_result ({inner_tokens} tok{size_note})",
                text="",
                children=sub,
                meta=meta,
            ))
        elif t in ("image", "input_image"):
            src = item.get("source", {})
            out.append(Block(
                kind="image",
                label=f"image ({src.get('media_type') or src.get('type', 'unknown')})",
                text=_stringify(src)[:2000],
            ))
        elif t == "document":
            out.append(Block(kind="document", label="document", text=_stringify(item)))
        else:
            out.append(Block(kind=t or "raw", label=f"({t})", text=_stringify(item)))
    return out


def _role_kind(role: str | None) -> str:
    return {
        "system": "system",
        "user": "user",
        "assistant": "assistant",
        "tool": "tool_result",
        "developer": "system",
    }.get((role or "").lower(), "raw")


def _parse_attachment(entry: dict) -> Block:
    """Render one Claude Code `type:"attachment"` event as a Block.

    Most subtypes are *framework context* injected into the model (skill
    listing, MCP instructions, tool list) — we mark those `kind="framework"`
    so they show up in the per-kind aggregates as their own slice rather than
    hiding inside `meta`. State-change subtypes (plan mode, date change,
    permissions, hooks, etc.) stay as `meta`.
    """
    att = entry.get("attachment") or {}
    sub = att.get("type", "unknown")
    common_meta = {
        "subtype": sub,
        "timestamp": entry.get("timestamp"),
        "uuid": entry.get("uuid"),
        "parentUuid": entry.get("parentUuid"),
    }

    if sub == "deferred_tools_delta":
        added = att.get("addedNames") or []
        removed = att.get("removedNames") or []
        label = f"tools: +{len(added)}" + (f" / -{len(removed)}" if removed else "")
        body = "\n".join(added)
        if removed:
            body += "\n\n\u2014 removed \u2014\n" + "\n".join(removed)
        return Block(kind="framework", label=label, text=body,
                     meta={**common_meta, "added": added, "removed": removed})

    if sub == "mcp_instructions_delta":
        names = att.get("addedNames") or []
        blocks = att.get("addedBlocks") or []
        body = "\n\n".join(blocks) if isinstance(blocks, list) else _stringify(blocks)
        label = f"MCP instructions: {len(names)} server{'s' if len(names) != 1 else ''}"
        return Block(kind="framework", label=label, text=body,
                     meta={**common_meta, "servers": names})

    if sub == "skill_listing":
        count = att.get("skillCount", 0)
        body = att.get("content", "")
        label = f"skill manifest: {count} skill{'s' if count != 1 else ''}"
        return Block(kind="framework", label=label, text=body,
                     meta={**common_meta, "skillCount": count,
                           "isInitial": att.get("isInitial")})

    if sub in ("plan_mode", "plan_mode_exit"):
        p = att.get("planFilePath", "")
        verb = "exited" if sub == "plan_mode_exit" else "entered"
        exists = att.get("planExists", False)
        label = f"plan mode {verb}" + (" (file exists)" if exists else "")
        return Block(kind="meta", label=label, text=p,
                     meta={**common_meta, "planFilePath": p, "planExists": exists,
                           "reminderType": att.get("reminderType"),
                           "isSubAgent": att.get("isSubAgent")})

    if sub == "command_permissions":
        allowed = att.get("allowedTools") or []
        label = f"permissions: {len(allowed)} allowed" if allowed else "permissions: none"
        return Block(kind="meta", label=label, text="\n".join(allowed),
                     meta={**common_meta, "allowedTools": allowed})

    if sub == "hook_success":
        name = att.get("hookName", "?")
        ms = att.get("durationMs", 0)
        ec = att.get("exitCode", 0)
        stdout = att.get("stdout", "") or ""
        stderr = att.get("stderr", "") or ""
        status = "" if ec == 0 else f" (exit {ec})"
        label = f"hook: {name} ({ms}ms){status}"
        parts = []
        cmd = att.get("command")
        if cmd:
            parts.append(f"$ {cmd}")
        if stdout:
            parts.append(f"\u2014 stdout \u2014\n{stdout}")
        if stderr:
            parts.append(f"\u2014 stderr \u2014\n{stderr}")
        return Block(kind="meta", label=label, text="\n\n".join(parts),
                     meta={**common_meta, "hookName": name, "durationMs": ms,
                           "exitCode": ec, "toolUseID": att.get("toolUseID"),
                           "hookEvent": att.get("hookEvent")})

    if sub == "todo_reminder":
        n = att.get("itemCount", 0)
        return Block(kind="meta", label=f"todo reminder ({n} item{'s' if n != 1 else ''})",
                     text="", meta={**common_meta, "itemCount": n})

    if sub == "date_change":
        d = att.get("newDate", "")
        return Block(kind="meta", label=f"date change: {d}", text="",
                     meta={**common_meta, "newDate": d})

    if sub == "ultrathink_effort":
        extras = {k: v for k, v in att.items() if k != "type"}
        return Block(kind="meta", label="ultrathink effort",
                     text=_stringify(extras), meta={**common_meta, **extras})

    # VS Code-client subtypes:
    if sub == "nested_memory":
        # CLAUDE.md or imported-memory content resolved from a subdirectory.
        raw = att.get("content")
        content = raw if isinstance(raw, str) else _stringify(raw if raw is not None else att)
        path = att.get("path") or att.get("filePath") or ""
        return Block(kind="framework",
                     label=f"nested memory: {path or '(content)'}",
                     text=content,
                     meta={**common_meta, "path": path})

    if sub == "hook_additional_context":
        # Hook injected extra context mid-turn.
        raw = att.get("content")
        content = raw if isinstance(raw, str) else _stringify(raw if raw is not None else att)
        name = att.get("hookName", "")
        return Block(kind="framework",
                     label=f"hook context" + (f": {name}" if name else ""),
                     text=content,
                     meta={**common_meta, "hookName": name})

    if sub == "plan_mode_reentry":
        p = att.get("planFilePath", "")
        return Block(kind="meta", label="plan mode re-entered", text=p,
                     meta={**common_meta, "planFilePath": p})

    if sub == "edited_text_file":
        # File edit bundled with a message.
        path = att.get("filePath") or att.get("path") or ""
        return Block(kind="meta", label=f"edited file: {path or '(unknown)'}",
                     text=_stringify(att), meta={**common_meta, "filePath": path})

    # CLI-client subtypes:
    if sub == "selected_lines_in_ide":
        raw = att.get("content")
        content = raw if isinstance(raw, str) else _stringify(raw if raw is not None else att)
        return Block(kind="framework", label="IDE selection", text=content,
                     meta=common_meta)

    if sub == "task_reminder":
        # Same shape as todo_reminder; differently named.
        n = att.get("itemCount", 0)
        return Block(kind="meta",
                     label=f"task reminder ({n} item{'s' if n != 1 else ''})",
                     text="", meta={**common_meta, "itemCount": n})

    if sub == "file":
        # Direct file inclusion via @path.
        name = att.get("filename") or att.get("displayPath") or ""
        content = att.get("content") or ""
        return Block(kind="framework",
                     label=f"file: {att.get('displayPath') or name}",
                     text=str(content) if content else "",
                     meta={**common_meta,
                           "filename": name,
                           "displayPath": att.get("displayPath")})

    if sub == "compact_file_reference":
        # A filename breadcrumb kept after /compact purged the file's content.
        # No content — render it as a dimmed meta row.
        name = att.get("filename") or ""
        dp = att.get("displayPath") or name
        return Block(kind="meta", label=f"compact ref: {dp}", text="",
                     meta={**common_meta,
                           "filename": name,
                           "displayPath": dp,
                           "compactRef": True})

    # Unknown attachment subtype — keep visible so we notice new ones.
    return Block(kind="meta", label=f"attachment: {sub}",
                 text=_stringify(att), meta=common_meta)


# Keys on Claude Code events that hold session-level metadata. These are
# repeated verbatim on every event; we snapshot them onto Session.context once.
SESSION_CONTEXT_KEYS = ("entrypoint", "cwd", "gitBranch", "version",
                        "slug", "sessionId", "userType")


# Fields likely to be a time duration expressed in milliseconds — used for
# formatting durationMs into "74.1s" labels.
def _fmt_ms(ms: Any) -> str:
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return "?"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms/1000:.1f}s"


_LOCAL_CMD_RE = re.compile(
    r"<command-name>([^<]*)</command-name>.*?(?:<command-args>([^<]*)</command-args>)?",
    re.DOTALL,
)


def _parse_system_event(entry: dict) -> Block:
    """Claude Code `type:"system"` events carry framework signals via the
    `subtype` discriminator. Confirmed subtypes (CLI 2.1.108): compact_boundary,
    turn_duration, away_summary, local_command. Others (memory_saved, api_metrics,
    etc.) appear in the TS schema but weren't observed in live sessions — we
    still surface them as meta blocks so they don't get silently dropped.
    """
    sub = entry.get("subtype", "?")
    common = {
        "subtype": sub,
        "timestamp": entry.get("timestamp"),
        "uuid": entry.get("uuid"),
        "parentUuid": entry.get("parentUuid"),
        "logicalParentUuid": entry.get("logicalParentUuid"),
        "level": entry.get("level"),
    }
    # Drop None values to keep meta compact
    common = {k: v for k, v in common.items() if v is not None}

    if sub == "compact_boundary":
        cm = entry.get("compactMetadata") or {}
        pre = cm.get("preTokens", 0)
        post = cm.get("postTokens", 0)
        dur = cm.get("durationMs", 0)
        trigger = cm.get("trigger", "?")
        saved = max(0, pre - post)
        pct = (100 * saved / pre) if pre else 0
        label = (f"compact · {pre:,} → {post:,} tok (-{pct:.0f}%) "
                 f"· {trigger} ({_fmt_ms(dur)})")
        return Block(kind="compact", label=label, text=entry.get("content", ""),
                     meta={**common, "compactMetadata": cm,
                           "preTokens": pre, "postTokens": post,
                           "durationMs": dur, "trigger": trigger,
                           "tokensSaved": saved})

    if sub == "turn_duration":
        dur = entry.get("durationMs", 0)
        msgs = entry.get("messageCount")
        label = f"turn · {_fmt_ms(dur)}" + (f" · {msgs} msgs" if msgs else "")
        return Block(kind="metric", label=label, text="",
                     meta={**common, "durationMs": dur, "messageCount": msgs})

    if sub == "away_summary":
        text = entry.get("content", "") or ""
        return Block(kind="framework", label=f"away summary · {_short(text, 70)}",
                     text=text, meta=common)

    if sub == "local_command":
        raw = entry.get("content", "") or ""
        m = _LOCAL_CMD_RE.search(raw)
        name = (m.group(1).strip() if m else "?")
        args = (m.group(2).strip() if m and m.group(2) else "")
        label_name = name or "(local command)"
        label = f"/{label_name.lstrip('/')}" + (f" {args}" if args else "")
        return Block(kind="meta", label=label, text=raw,
                     meta={**common, "commandName": name, "commandArgs": args})

    # Fallback for untested subtypes. Preserve the payload so a future user's
    # session with, say, `api_metrics` lands visibly instead of vanishing.
    text = entry.get("content", "") or _stringify(
        {k: v for k, v in entry.items()
         if k not in ("type", "subtype", "uuid", "parentUuid",
                      "timestamp", "userType", "entrypoint", "cwd",
                      "sessionId", "version", "gitBranch", "slug",
                      "logicalParentUuid", "isSidechain", "isMeta", "level")})
    return Block(kind="meta", label=f"system/{sub}", text=text, meta=common)


def _parse_claude_entry(entry: dict) -> Block | list[Block] | None:
    etype = entry.get("type")

    if etype == "summary":
        text = entry.get("summary", "")
        return Block(kind="meta", label=f"summary: {_short(text)}", text=text)

    if etype == "ai-title":
        t = entry.get("aiTitle", "") or ""
        return Block(kind="meta", label=f"ai-title: {_short(t, 90)}", text=t,
                     meta={"aiTitle": t})

    if etype == "last-prompt":
        t = entry.get("lastPrompt", "") or ""
        return Block(kind="meta", label=f"last-prompt: {_short(t, 60)}", text=t,
                     meta={"promptId": entry.get("promptId")})

    if etype == "queue-operation":
        op = entry.get("operation", "")
        return Block(kind="meta", label=f"queue: {op}", text="",
                     meta={"operation": op, "timestamp": entry.get("timestamp")})

    if etype == "file-history-snapshot":
        snap = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {}
        tracked = snap.get("trackedFileBackups") or {}
        is_update = bool(entry.get("isSnapshotUpdate"))
        paths = list(tracked.keys())
        verb = "update" if is_update else "initial"
        n = len(paths)
        label = f"snapshot {verb}: {n} file{'s' if n != 1 else ''}"
        return Block(kind="meta", label=label, text="\n".join(paths),
                     meta={"messageId": entry.get("messageId"),
                           "isSnapshotUpdate": is_update,
                           "trackedCount": n,
                           "tracked": tracked})

    if etype == "attachment":
        return _parse_attachment(entry)

    if etype == "progress":
        # Streaming tool-progress updates. Seen in sub-agent files.
        ptype = entry.get("ptype") or entry.get("progressType") or (
            entry.get("data", {}) or {}).get("type") if isinstance(entry.get("data"), dict) else None
        tool_use_id = entry.get("toolUseID") or entry.get("tool_use_id")
        label = f"progress" + (f" · {ptype}" if ptype else "")
        return Block(kind="meta", label=label,
                     text=_stringify({k: v for k, v in entry.items()
                                      if k not in ("type", "timestamp", "uuid",
                                                   "parentUuid", "userType",
                                                   "entrypoint", "cwd", "sessionId",
                                                   "version", "gitBranch", "slug")}),
                     meta={"progressType": ptype, "toolUseID": tool_use_id,
                           "timestamp": entry.get("timestamp"),
                           "uuid": entry.get("uuid")})

    if etype == "permission-mode":
        mode = entry.get("permissionMode", "?")
        return Block(kind="meta", label=f"permission mode: {mode}", text="",
                     meta={"permissionMode": mode})

    if etype == "system":
        return _parse_system_event(entry)

    if etype in ("user", "assistant"):
        msg = entry.get("message") or {}
        role = msg.get("role") or etype
        content = msg.get("content")
        # `toolUseResult` on user events holds the full stdout + persisted-file
        # pointer for oversized tool outputs. Pass it through so the tool_result
        # block parser can reveal what the JSONL otherwise hides.
        kids = _blocks_from_content(content, entry.get("toolUseResult"))
        joined = "\n".join(c.text for c in kids) if kids else _stringify(msg)
        # Claude Code flags framework-injected "user" events (slash-command
        # bodies, skill doc loads, resume stubs) as isMeta — these aren't
        # real user input, they're context the harness stuffs in. Surface
        # them as their own kind so aggregates tell the truth.
        is_meta = bool(entry.get("isMeta"))
        kind = "framework" if is_meta and etype == "user" else _role_kind(role)
        prefix = "framework" if kind == "framework" else role
        label = f"{prefix} \u00b7 {_short(joined)}" if joined else prefix
        meta = {
            "timestamp": entry.get("timestamp"),
            "uuid": entry.get("uuid"),
            "parentUuid": entry.get("parentUuid"),
            "model": msg.get("model"),
            "usage": msg.get("usage"),
        }
        # Preserve additional per-event signals only when set (keep meta tight)
        for k in ("isMeta", "isSidechain", "isApiErrorMessage", "requestId",
                  "promptId", "permissionMode",
                  "sourceToolUseID", "sourceToolAssistantUUID"):
            v = entry.get(k)
            if v:
                meta[k] = v
        return Block(kind=kind, label=label, text="", children=kids, meta=meta)

    # Unknown top-level event — keep it visible as raw so nothing is silently dropped.
    return Block(
        kind="meta",
        label=f"({etype or 'event'})",
        text=_stringify(entry),
        meta={"type": etype},
    )


def _parse_codex_entry(entry: dict) -> Block | list[Block] | None:
    """
    Codex CLI rollout events are a grab bag. The ones that matter for context:
      {"type":"message","role":"user"|"assistant"|"system","content":[...]}
      {"type":"function_call", "name":..., "arguments":...}
      {"type":"function_call_output", "output":...}
      {"type":"reasoning", "summary":[{"text":...}]} / {"content":[...]}
    Also session_meta, turn_context, event_msg, etc.
    We render message/function events as context blocks and relegate everything
    else to a collapsed "meta" node so the tree stays readable.
    """
    etype = entry.get("type")

    # Some rollouts wrap payloads inside "payload"
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else None
    if payload and "type" in payload:
        entry = {**payload, **{k: v for k, v in entry.items() if k not in ("payload",)}}
        etype = entry.get("type")

    if etype in ("message", "response_item"):
        role = entry.get("role") or entry.get("author", {}).get("role")
        content = entry.get("content")
        if isinstance(content, list):
            kids: list[Block] = []
            for part in content:
                if not isinstance(part, dict):
                    kids.append(Block(kind="raw", label="(raw)", text=_stringify(part)))
                    continue
                ptype = part.get("type", "")
                # Codex sometimes uses "input_text"/"output_text" block types.
                txt = part.get("text") or part.get("content") or ""
                if ptype in ("input_text", "output_text", "text"):
                    kids.append(Block(kind="text", label=_short(txt) or f"({ptype})", text=txt))
                elif ptype in ("input_image", "image"):
                    kids.append(Block(kind="image", label=f"image ({ptype})", text=_stringify(part)[:2000]))
                else:
                    kids.append(Block(kind=ptype or "raw", label=f"({ptype})", text=_stringify(part)))
        else:
            kids = _blocks_from_content(content)
        joined = "\n".join(c.text for c in kids)
        return Block(
            kind=_role_kind(role),
            label=f"{role or 'message'} \u00b7 {_short(joined)}",
            children=kids,
            meta={k: entry.get(k) for k in ("id", "timestamp", "model") if entry.get(k) is not None},
        )

    if etype in ("function_call", "tool_call"):
        name = entry.get("name") or entry.get("function", {}).get("name", "tool")
        args = entry.get("arguments")
        if args is None and isinstance(entry.get("function"), dict):
            args = entry["function"].get("arguments")
        text = _stringify(args)
        return Block(
            kind="tool_use",
            label=f"{name}({_short(text, 60)})",
            text=text,
            meta={"call_id": entry.get("call_id") or entry.get("id"), "name": name},
        )

    if etype in ("function_call_output", "tool_result", "tool_output"):
        out = entry.get("output") or entry.get("result") or entry.get("content")
        text = _stringify(out)
        return Block(
            kind="tool_result",
            label=f"tool_result ({est_tokens(text)} tok)",
            text=text,
            meta={"call_id": entry.get("call_id") or entry.get("id")},
        )

    if etype == "reasoning":
        summary = entry.get("summary") or entry.get("content") or []
        parts: list[str] = []
        if isinstance(summary, list):
            for s in summary:
                if isinstance(s, dict):
                    parts.append(s.get("text", ""))
                else:
                    parts.append(str(s))
        txt = "\n".join(p for p in parts if p)
        return Block(kind="thinking", label=f"reasoning \u00b7 {_short(txt)}", text=txt)

    if etype in ("session_meta", "turn_context", "event_msg", None):
        # For session_meta, the system prompt lives inside `payload` (which
        # typically has no `type` of its own, so the earlier unwrap at the top
        # of this function doesn't fire). Look in payload first, then outer.
        inner = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
        sys_text = ""
        bi = inner.get("base_instructions")
        if isinstance(bi, dict):
            sys_text = bi.get("text") or ""
        elif isinstance(inner.get("instructions"), str):
            sys_text = inner["instructions"]

        if sys_text:
            system_block = Block(
                kind="system",
                label=f"system · {_short(sys_text)}",
                text=sys_text,
            )
            drop = ("base_instructions", "instructions")
            if inner is entry:
                stripped = {k: v for k, v in entry.items() if k not in drop}
            else:
                stripped = {**entry,
                            "payload": {k: v for k, v in inner.items() if k not in drop}}
            meta_block = Block(
                kind="meta",
                label=f"({etype or 'event'})",
                text=_stringify(stripped),
                meta={"type": etype},
            )
            return [system_block, meta_block]

        return Block(
            kind="meta",
            label=f"({etype or 'event'})",
            text=_stringify(entry),
            meta={"type": etype},
        )

    return Block(kind="meta", label=f"({etype})", text=_stringify(entry), meta={"type": etype})


# ---------- loaders -------------------------------------------------------
def _load_jsonl(path: Path) -> Iterable[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    yield {"type": "__parse_error__", "line": line_num, "error": str(e), "raw": line[:500]}
    except OSError as e:
        yield {"type": "__read_error__", "error": str(e)}


def _peek_title(path: Path, client: str, *, max_lines: int = 200, max_bytes: int = 262144) -> str:
    """Cheap title extraction — scan the first ~256KB for a usable title.

    Priority (Claude): `ai-title` > `summary` > first real user message.
    `ai-title` is Claude Code's own generated title and almost always wins when
    present; it may appear a dozen or so events in, so scan beyond line 10.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(max_bytes)
    except OSError:
        return path.stem
    first_user: str | None = None
    for i, line in enumerate(chunk.splitlines()):
        if i >= max_lines:
            break
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict):
            continue
        if client == "claude":
            if e.get("type") == "ai-title":
                t = e.get("aiTitle") or ""
                if t.strip():
                    return _short(t, 90)
            if e.get("type") == "summary":
                s = e.get("summary", "")
                if s: return _short(s, 90)
            if e.get("type") == "user" and not e.get("isMeta") and first_user is None:
                msg = e.get("message") or {}
                c = msg.get("content")
                if isinstance(c, str) and c.strip():
                    first_user = _short(c, 90)
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            txt = part.get("text", "")
                            if txt.strip():
                                first_user = _short(txt, 90); break
        elif client == "codex":
            if e.get("type") == "message" and e.get("role") == "user" and first_user is None:
                content = e.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            txt = part.get("text") or ""
                            if txt.strip():
                                first_user = _short(txt, 90); break
                elif isinstance(content, str) and content.strip():
                    first_user = _short(content, 90)
    return first_user or path.stem


def _derive_title(blocks: list[Block], fallback: str) -> str:
    # AI-generated title wins if present — it's what Claude Code itself
    # picked, and matches the UI in other Claude surfaces.
    for b in blocks:
        if b.kind == "meta" and b.label.startswith("ai-title:"):
            t = (b.meta or {}).get("aiTitle") if isinstance(b.meta, dict) else None
            if isinstance(t, str) and t.strip():
                return _short(t, 90)
    # Conversation summary second
    for b in blocks:
        if b.kind == "meta" and b.label.startswith("summary:"):
            return b.label[len("summary:"):].strip() or fallback
    # First *real* user message third (skip framework-injected content)
    for b in blocks:
        if b.kind == "user":
            for c in b.children:
                if c.kind == "text" and c.text.strip():
                    return _short(c.text, 90)
    return fallback


def _fallback_title(path: Path, client: str) -> str:
    """Derive a readable title from the path alone — no file IO."""
    stem = path.stem
    if client == "claude":
        parent = path.parent.name
        # Cowork slugs look like "-sessions-bold-wonderful-fermi"
        if parent.startswith("-sessions-"):
            return parent[len("-sessions-"):].replace("-", " ")
        if parent and parent != "projects":
            return parent
        return stem
    if client == "codex":
        # e.g. rollout-2026-02-21T10-43-28-019c7e4b-...
        m = re.match(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})", stem)
        if m:
            return f"Codex · {m.group(1).replace('T', ' ').replace('-', ':', 2).replace('-', ':')}"
        return stem
    return stem


_SUBAGENT_RE = re.compile(r"/([0-9a-f-]{36})/subagents/agent-([^/]+)\.jsonl$")


def _classify_jsonl(path: Path) -> tuple[str, str | None]:
    """Return (role, parentSessionId | None) for a Claude Code JSONL path.

    Claude Code writes subagent transcripts to:
      .../<project>/<parent-session-uuid>/subagents/agent-<agent-id>.jsonl
    Those shouldn't pollute the top-level session list; they belong under
    their parent session.
    """
    m = _SUBAGENT_RE.search(str(path))
    if m:
        return "subagent", m.group(1)
    return "session", None


def _list_one(root: Path, client: str) -> list[dict]:
    """Very cheap metadata-only scan — no file reads, just `stat()`.

    For Claude sessions, subagent JSONLs (nested under
    <session-uuid>/subagents/) are not returned as top-level entries. They're
    attached to the parent session's `subagents` list instead.
    """
    if not root.exists():
        return []
    entries: list[dict] = []
    subagents: dict[str, list[dict]] = {}  # parent sessionId -> [subagent metas]
    for path in sorted(root.rglob("*.jsonl")):
        try:
            st = path.stat()
        except OSError:
            continue
        role, parent_id = ("session", None)
        if client == "claude":
            role, parent_id = _classify_jsonl(path)
        if role == "subagent" and parent_id:
            subagents.setdefault(parent_id, []).append({
                "id": f"{client}::{path}",
                "path": str(path),
                "mtime": st.st_mtime,
                "size": st.st_size,
                "agentId": path.stem.replace("agent-", "", 1),
            })
            continue
        entries.append({
            "id": f"{client}::{path}",
            "client": client,
            "title": _fallback_title(path, client),
            "path": str(path),
            "mtime": st.st_mtime,
            "size": st.st_size,
        })
    # Attach subagent lists to their parent sessions. A session's parent id is
    # the uuid portion of its own filename stem (Claude Code convention).
    if subagents:
        by_stem = {Path(e["path"]).stem: e for e in entries}
        for parent_id, subs in subagents.items():
            parent = by_stem.get(parent_id)
            if parent is not None:
                parent["subagents"] = sorted(subs, key=lambda x: x["mtime"])
    return entries


def list_claude_sessions(root: Path) -> list[dict]:
    return _list_one(root, "claude")


def list_codex_sessions(root: Path) -> list[dict]:
    return _list_one(root, "codex")


MAX_ENTRIES_PER_FILE = 20_000  # safety cap — keeps UI responsive on huge audit logs


def _parse_file(path: Path, client: str) -> Session:
    """Full parse for a single file. Robust to bad data. Capped for safety."""
    blocks: list[Block] = []
    ctx: dict[str, Any] = {}  # session-level metadata snapshot
    seen = 0
    truncated = False
    for e in _load_jsonl(path):
        seen += 1
        if seen > MAX_ENTRIES_PER_FILE:
            truncated = True
            break
        # Snapshot session-level metadata the first time we see it. These
        # fields repeat verbatim on every event, so we lift them once.
        if client == "claude":
            for k in SESSION_CONTEXT_KEYS:
                if k not in ctx and e.get(k) is not None:
                    ctx[k] = e[k]
        try:
            if e.get("type") == "__parse_error__":
                blocks.append(Block(kind="meta", label=f"parse error line {e.get('line')}",
                                    text=e.get("raw", "")))
                continue
            if e.get("type") == "__read_error__":
                blocks.append(Block(kind="meta", label="read error", text=e.get("error", "")))
                continue
            parsed = (_parse_claude_entry(e) if client == "claude" else _parse_codex_entry(e))
            if isinstance(parsed, list):
                blocks.extend(parsed)
            elif parsed is not None:
                blocks.append(parsed)
        except Exception as exc:  # noqa: BLE001 - never let one bad entry kill the session
            blocks.append(Block(kind="meta", label=f"handler error: {type(exc).__name__}",
                                text=f"{exc}\n{_stringify(e)[:2000]}"))
    if truncated:
        blocks.append(Block(
            kind="meta",
            label=f"\u2026truncated after {MAX_ENTRIES_PER_FILE:,} entries",
            text=f"This file has more than {MAX_ENTRIES_PER_FILE:,} events. "
                 "Only the first batch was parsed to keep the UI responsive.",
        ))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    title = _derive_title(blocks, fallback=path.stem)
    return Session(
        id=f"{client}::{path}",
        client=client,
        title=title,
        path=str(path),
        mtime=mtime,
        blocks=blocks,
        context=ctx,
    )


# ---------- export formats ------------------------------------------------
def _md_escape(text: str) -> str:
    """Escape characters that would break the containing fenced block or header.
    Plain text doesn't need much — backticks are the main hazard inside fences,
    which we handle by picking a longer fence than any internal run.
    """
    return text


def _fence_for(text: str) -> str:
    """Pick a fence length longer than the longest run of backticks in ``text``."""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(3, longest + 1)


def _walk_body_text(block: Block) -> str:
    """Concatenate the visible prose from a block and its children."""
    parts: list[str] = []
    if block.text:
        parts.append(block.text)
    for c in block.children:
        parts.append(_walk_body_text(c))
    return "\n".join(p for p in parts if p)


def session_to_markdown(s: Session) -> str:
    total = sum(est_tokens(b.text) for b in _walk(s.blocks))
    out: list[str] = []
    out.append(f"# {s.title or '(untitled)'}")
    out.append("")
    out.append(f"- **Client:** {s.client}")
    out.append(f"- **Path:** `{s.path}`")
    model = s._detect_model()
    if model:
        out.append(f"- **Model:** `{model}`")
    out.append(f"- **Tokens (tiktoken):** {total:,}")
    out.append(f"- **Blocks:** {len(s.blocks):,}")
    out.append("")
    out.append("---")

    def render(b: Block, depth: int = 2):
        prefix = "#" * min(depth, 6)
        kind = b.kind
        label = b.label or ""
        if kind in ("system", "user", "assistant"):
            out.append(f"\n{prefix} {kind.capitalize()}")
            for c in b.children:
                render(c, depth + 1)
            return
        if kind == "text":
            if b.text.strip():
                out.append("")
                out.append(b.text)
            return
        if kind == "thinking":
            out.append(f"\n{prefix} Thinking")
            if b.text.strip():
                out.append("")
                out.append(b.text)
            return
        if kind == "tool_use":
            name = (b.meta or {}).get("name", "tool")
            fence = _fence_for(b.text)
            out.append(f"\n{prefix} Tool use: {name}")
            out.append(f"{fence}json")
            out.append(b.text or "{}")
            out.append(fence)
            return
        if kind == "tool_result":
            err = " (error)" if (b.meta or {}).get("is_error") else ""
            body = _walk_body_text(b)
            fence = _fence_for(body)
            out.append(f"\n{prefix} Tool result{err}")
            if body.strip():
                out.append(fence)
                out.append(body)
                out.append(fence)
            return
        if kind == "image":
            out.append(f"\n{prefix} Image")
            out.append(f"> `{label}`")
            return
        if kind == "document":
            out.append(f"\n{prefix} Document")
            return
        if kind == "meta":
            # Keep meta nodes terse — just the label, skip verbose payloads.
            out.append(f"\n_{label}_")
            return
        # Fallback for raw / unknown
        out.append(f"\n{prefix} {kind}")
        if b.text.strip():
            fence = _fence_for(b.text)
            out.append(fence)
            out.append(b.text)
            out.append(fence)

    for b in s.blocks:
        render(b, depth=2)
    return "\n".join(out) + "\n"


def session_to_json(s: Session) -> str:
    return json.dumps(s.full(), indent=2, ensure_ascii=False)


def session_to_html(s: Session) -> str:
    """Self-contained HTML — the markdown export wrapped in minimal styling.

    This isn't the full interactive viewer; it's a readable static transcript
    suitable for sharing. Uses inline CSS so a single file is portable.
    """
    md = session_to_markdown(s)
    # Escape for safe embedding inside the <pre> below — we do NOT try to
    # render the markdown here; tools like Obsidian / GitHub / a preview window
    # can handle the .md extension. The HTML mode is for "open in browser".
    safe = (md.replace("&", "&amp;")
              .replace("<", "&lt;")
              .replace(">", "&gt;"))
    title = (s.title or "(untitled)").replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{title}</title>"
        "<style>"
        "body{font:14px/1.55 ui-sans-serif,system-ui,sans-serif;max-width:900px;"
        "margin:32px auto;padding:0 20px;color:#1a1f2e;background:#fafbfc}"
        "pre{white-space:pre-wrap;word-wrap:break-word;background:#f3f5f8;"
        "padding:12px 14px;border-radius:6px;border:1px solid #e3e7ee;"
        "font:12px/1.5 ui-monospace,Menlo,Consolas,monospace}"
        "</style></head><body>"
        f"<pre>{safe}</pre>"
        "</body></html>\n"
    )


EXPORT_FORMATS = {
    "md":   ("text/markdown; charset=utf-8",       ".md",   session_to_markdown),
    "json": ("application/json; charset=utf-8",    ".json", session_to_json),
    "html": ("text/html; charset=utf-8",           ".html", session_to_html),
}


# ---------- HTTP server ---------------------------------------------------
HERE = Path(__file__).parent
INDEX_HTML = HERE / "index.html"


class State:
    def __init__(self, claude_roots: list[Path], codex_roots: list[Path]):
        self.claude_roots = claude_roots
        self.codex_roots = codex_roots
        # Metadata cache keyed by session id -> (client, path)
        self._meta: dict[str, dict] = {}
        # Full-parse cache keyed by session id -> Session
        self._full: dict[str, Session] = {}
        # Peeked-title cache keyed by session id -> title (from first-lines scan)
        self._titles: dict[str, str] = {}
        self._titles_lock = threading.Lock()
        # Raw-file content cache for global search: sid -> (mtime, text)
        self._search_text: dict[str, tuple[float, str]] = {}
        self._search_lock = threading.Lock()

    def list_all(self) -> list[dict]:
        """Very cheap directory scan: stat + path-derived title only.

        Parent sessions are returned; sub-agent files are attached to their
        parents as `subagents: [{id, path, mtime, size, agentId}]`. All IDs
        (including sub-agents) land in `self._meta` so `state.get(sid)` can
        still open a sub-agent when the client clicks one — they're just not
        top-level sidebar entries.
        """
        items: list[dict] = []
        for root in self.claude_roots:
            items.extend(list_claude_sessions(root))
        for root in self.codex_roots:
            items.extend(list_codex_sessions(root))
        seen: dict[str, dict] = {}
        for it in items:
            seen.setdefault(it["id"], it)
        out = sorted(seen.values(), key=lambda x: x["mtime"], reverse=True)
        # Upgrade titles from cache if we already peeked them.
        for it in out:
            cached = self._titles.get(it["id"])
            if cached:
                it["title"] = cached
        # Rebuild _meta including sub-agents so ID lookups resolve. Sub-agents
        # aren't returned in the main list, but state.get(sid) needs to find
        # them when the user clicks a sub-agent chip.
        meta_map: dict[str, dict] = {}
        for it in out:
            meta_map[it["id"]] = it
            for sa in it.get("subagents") or []:
                # Synthesize a minimal meta entry for each sub-agent.
                meta_map[sa["id"]] = {
                    "id":    sa["id"],
                    "client": "claude",
                    "title": f"sub-agent {sa['agentId'][:12]}",
                    "path":  sa["path"],
                    "mtime": sa["mtime"],
                    "size":  sa["size"],
                    "isSubagent": True,
                    "parentId": it["id"],
                }
        self._meta = meta_map
        return out

    def titles_for(self, ids: list[str] | None = None) -> dict[str, str]:
        """Compute peeked titles for the given ids (or all known). Cached."""
        if not self._meta:
            self.list_all()
        target = ids if ids else list(self._meta.keys())
        out: dict[str, str] = {}
        for sid in target:
            cached = self._titles.get(sid)
            if cached:
                out[sid] = cached
                continue
            meta = self._meta.get(sid)
            if not meta:
                continue
            try:
                title = _peek_title(Path(meta["path"]), meta["client"])
            except Exception:
                title = meta["title"]
            with self._titles_lock:
                self._titles[sid] = title
            out[sid] = title
        return out

    def get(self, sid: str) -> Session | None:
        # Refresh metadata cache if the id is unknown
        if sid not in self._meta:
            self.list_all()
        meta = self._meta.get(sid)
        if not meta:
            return None
        if sid in self._full:
            return self._full[sid]
        path = Path(meta["path"])
        try:
            sess = _parse_file(path, meta["client"])
        except Exception as exc:  # noqa: BLE001
            sess = Session(
                id=sid, client=meta["client"], title=meta["title"],
                path=meta["path"], mtime=meta["mtime"],
                blocks=[Block(kind="meta", label=f"fatal parse error: {type(exc).__name__}",
                              text=str(exc))],
            )
        self._full[sid] = sess
        return sess

    # ---- global search -----------------------------------------------------
    def _content_for(self, sid: str) -> str | None:
        """Read the JSONL file for a session, cached by mtime."""
        meta = self._meta.get(sid)
        if not meta:
            return None
        path = Path(meta["path"])
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._search_text.get(sid)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        with self._search_lock:
            self._search_text[sid] = (mtime, text)
        return text

    def search(self, query: str, *, regex: bool = False, case: bool = False,
               clients: set[str] | None = None,
               max_sessions: int = 100, max_hits: int = 20) -> list[dict]:
        if not query:
            return []
        flags = 0 if case else re.IGNORECASE
        try:
            pat = re.compile(query if regex else re.escape(query), flags)
        except re.error:
            return []
        if not self._meta:
            self.list_all()
        results: list[dict] = []
        # Iterate most-recent sessions first for relevance.
        for meta in sorted(self._meta.values(), key=lambda x: x["mtime"], reverse=True):
            if clients and meta["client"] not in clients:
                continue
            text = self._content_for(meta["id"])
            if not text:
                continue
            hits = []
            for m in pat.finditer(text):
                if len(hits) >= max_hits:
                    break
                s = max(0, m.start() - 60)
                e = min(len(text), m.end() + 60)
                snippet = re.sub(r"\s+", " ", text[s:e]).strip()
                # Mark match range relative to the snippet for client highlight
                rs = m.start() - s
                re_ = rs + (m.end() - m.start())
                hits.append({"snippet": snippet, "mark": [rs, re_]})
            if hits:
                results.append({
                    "id":     meta["id"],
                    "client": meta["client"],
                    "title":  self._titles.get(meta["id"]) or meta["title"],
                    "path":   meta["path"],
                    "mtime":  meta["mtime"],
                    "count":  len(hits),
                    "hits":   hits,
                })
            if len(results) >= max_sessions:
                break
        return results


class Watcher(threading.Thread):
    """Polls session roots every ``interval`` seconds. On change, invalidates
    the matching State caches and publishes an event to every subscriber.

    Polling (rather than fsevents / inotify) keeps the install dep-free; a few
    hundred stat() calls per tick is cheap.
    """

    def __init__(self, state: State, interval: float = 2.0):
        super().__init__(daemon=True, name="viewer-watcher")
        self.state = state
        self.interval = interval
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._snapshot: dict[str, float] = {}

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Drop events for slow subscribers rather than blocking the
                # watcher thread. They'll see the next event.
                pass

    def run(self) -> None:
        try:
            self._snapshot = {m["id"]: m["mtime"] for m in self.state.list_all()}
        except Exception:  # noqa: BLE001
            self._snapshot = {}
        while True:
            time.sleep(self.interval)
            try:
                current = {m["id"]: m["mtime"] for m in self.state.list_all()}
            except Exception:  # noqa: BLE001
                continue
            added   = current.keys() - self._snapshot.keys()
            removed = self._snapshot.keys() - current.keys()
            changed = {sid for sid in current
                       if sid in self._snapshot and current[sid] != self._snapshot[sid]}
            for sid in changed:
                # Invalidate caches so the next get() re-parses fresh.
                self.state._full.pop(sid, None)
                self.state._search_text.pop(sid, None)
                self._publish({"kind": "session-updated", "id": sid})
            for sid in added:
                self._publish({"kind": "session-added", "id": sid})
            for sid in removed:
                self.state._full.pop(sid, None)
                self.state._search_text.pop(sid, None)
                self._publish({"kind": "session-removed", "id": sid})
            self._snapshot = current


def make_handler(state: State, watcher: Watcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args):  # quiet
            sys.stderr.write("[viewer] " + (fmt % args) + "\n")

        def _send_json(self, payload: Any, status: int = 200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                try:
                    self._send_bytes(INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._send_json({"error": "index.html missing"}, 500)
                return
            if path == "/api/sessions":
                items = state.list_all()
                self._send_json({
                    "claudeRoot": " | ".join(str(p) for p in state.claude_roots),
                    "codexRoot":  " | ".join(str(p) for p in state.codex_roots),
                    "sessions": items,
                })
                return
            if path == "/api/titles":
                qs = parse_qs(parsed.query)
                raw = (qs.get("ids") or [""])[0]
                ids = [x for x in raw.split(",") if x] if raw else None
                self._send_json({"titles": state.titles_for(ids)})
                return
            if path == "/api/session":
                qs = parse_qs(parsed.query)
                sid = (qs.get("id") or [""])[0]
                s = state.get(sid)
                if not s:
                    self._send_json({"error": "not found", "id": sid}, 404)
                    return
                self._send_json(s.full())
                return
            if path == "/api/events":
                # Server-Sent Events — one long-lived connection per browser tab.
                # Uses 20s keepalive comments so intermediate proxies don't close
                # the socket. `X-Accel-Buffering: no` is an nginx-specific hint.
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    return
                sub = watcher.subscribe()
                try:
                    self.wfile.write(b"event: hello\ndata: {}\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            event = sub.get(timeout=20)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        body = json.dumps(event).encode("utf-8")
                        self.wfile.write(f"event: {event.get('kind','message')}\n".encode("utf-8"))
                        self.wfile.write(b"data: " + body + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    watcher.unsubscribe(sub)
                return
            if path == "/api/search":
                qs = parse_qs(parsed.query)
                q = (qs.get("q") or [""])[0]
                regex_flag = (qs.get("regex") or ["0"])[0] == "1"
                case_flag = (qs.get("case") or ["0"])[0] == "1"
                clients_raw = (qs.get("clients") or [""])[0]
                clients = {c for c in clients_raw.split(",") if c} or None
                results = state.search(q, regex=regex_flag, case=case_flag,
                                       clients=clients)
                self._send_json({"q": q, "count": len(results), "results": results})
                return
            if path == "/api/tool-result":
                # Serves Claude Code's offloaded tool-result files, which live
                # at <session-jsonl-parent>/<session-uuid>/tool-results/<name>.
                # Security: only serve from that exact directory, reject any
                # basename containing path separators or traversal bits.
                qs = parse_qs(parsed.query)
                sid = (qs.get("id") or [""])[0]
                name = (qs.get("name") or [""])[0]
                if not name or "/" in name or "\\" in name or name.startswith(".") or ".." in name:
                    self._send_json({"error": "invalid name"}, 400)
                    return
                meta = state._meta.get(sid)
                if not meta:
                    self._send_json({"error": "session not found", "id": sid}, 404)
                    return
                # Session JSONL lives at .../<project>/<uuid>.jsonl;
                # its tool-results folder is .../<project>/<uuid>/tool-results/
                session_path = Path(meta["path"])
                safe_dir = session_path.parent / session_path.stem / "tool-results"
                target = (safe_dir / name).resolve()
                if not str(target).startswith(str(safe_dir.resolve()) + os.sep):
                    self._send_json({"error": "path escape"}, 403)
                    return
                if not target.is_file():
                    self._send_json({"error": "not found", "target": str(target)}, 404)
                    return
                try:
                    body = target.read_bytes()
                except OSError as e:
                    self._send_json({"error": f"read failed: {e}"}, 500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/session/export":
                qs = parse_qs(parsed.query)
                sid = (qs.get("id") or [""])[0]
                fmt = (qs.get("fmt") or ["md"])[0].lower()
                if fmt not in EXPORT_FORMATS:
                    self._send_json({"error": f"unknown fmt: {fmt}"}, 400)
                    return
                s = state.get(sid)
                if not s:
                    self._send_json({"error": "not found", "id": sid}, 404)
                    return
                ctype, ext, renderer = EXPORT_FORMATS[fmt]
                body = renderer(s).encode("utf-8")
                # Safe filename from the session title (ASCII-only, no slashes).
                safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", s.title or "session").strip("_") or "session"
                filename = f"{safe_stem[:80]}{ext}"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json({"error": "not found"}, 404)

    return Handler


def _default_roots() -> tuple[list[str], list[str]]:
    """Per-OS defaults for Claude / Codex session roots.

    Claude Code itself stores transcripts under ``~/.claude/projects`` on every
    platform. The desktop / Cowork app uses the standard per-OS app-data
    directory, so we branch on :data:`sys.platform`.
    """
    claude = ["~/.claude/projects"]
    if sys.platform == "darwin":
        claude.append("~/Library/Application Support/Claude/local-agent-mode-sessions")
    elif sys.platform.startswith("win"):
        # %APPDATA% -> ~/AppData/Roaming; expanduser handles the HOME side.
        appdata = os.environ.get("APPDATA")
        if appdata:
            claude.append(str(Path(appdata) / "Claude" / "local-agent-mode-sessions"))
        else:
            claude.append("~/AppData/Roaming/Claude/local-agent-mode-sessions")
    else:  # linux / bsd
        xdg = os.environ.get("XDG_CONFIG_HOME", "~/.config")
        claude.append(f"{xdg}/Claude/local-agent-mode-sessions")

    codex = ["~/.codex/sessions", "~/.codex/archived_sessions"]
    return claude, codex


DEFAULT_CLAUDE_ROOTS, DEFAULT_CODEX_ROOTS = _default_roots()


# ---------- .env loader (zero-dep) ----------
def _load_env_file(path: Path) -> int:
    """Parse a simple KEY=VALUE .env file.

    - Lines starting with '#' and blank lines are skipped.
    - Values may be wrapped in single or double quotes (stripped).
    - Existing env vars win (we use :py:meth:`dict.setdefault` semantics),
      so an explicit ``export FOO=bar`` in the shell beats the file.
    """
    if not path.is_file():
        return 0
    loaded = 0
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
                loaded += 1
    except OSError:
        pass
    return loaded


def _split_paths(raw: str) -> list[str]:
    """Split a path list on the OS separator, ignoring blanks."""
    return [p for p in raw.split(os.pathsep) if p.strip()]


def main():
    # Load .env FIRST (so env-var lookups below see it), checking the cwd and
    # the directory containing this script.
    env_candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    env_hits: list[Path] = []
    for c in env_candidates:
        if _load_env_file(c):
            env_hits.append(c)

    p = argparse.ArgumentParser(
        description="Browse Claude Code / Cowork / Codex conversations as context trees.",
    )
    p.add_argument(
        "--host",
        default=os.environ.get("HOST", "127.0.0.1"),
        help="Bind address (env: HOST). Default: 127.0.0.1",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8765")),
        help="Port (env: PORT). Default: 8765",
    )
    p.add_argument(
        "--claude", action="append", default=None,
        help=("Claude sessions directory (repeatable). "
              "Env: CLAUDE_SESSIONS_PATH (OS-pathsep separated). "
              "Defaults: " + ", ".join(DEFAULT_CLAUDE_ROOTS)),
    )
    p.add_argument(
        "--codex", action="append", default=None,
        help=("Codex sessions directory (repeatable). "
              "Env: CODEX_SESSIONS_PATH (OS-pathsep separated). "
              "Defaults: " + ", ".join(DEFAULT_CODEX_ROOTS)),
    )
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    # Priority: CLI flag > env var > built-in default.
    claude_inputs = (
        args.claude
        or _split_paths(os.environ.get("CLAUDE_SESSIONS_PATH", ""))
        or DEFAULT_CLAUDE_ROOTS
    )
    codex_inputs = (
        args.codex
        or _split_paths(os.environ.get("CODEX_SESSIONS_PATH", ""))
        or DEFAULT_CODEX_ROOTS
    )

    # Normalise: expanduser for ~, expandvars for %APPDATA% / $HOME.
    def _resolve(raw: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(raw)))

    claude_roots = [_resolve(p_) for p_ in claude_inputs]
    codex_roots  = [_resolve(p_) for p_ in codex_inputs]
    state = State(claude_roots, codex_roots)
    watcher = Watcher(state)
    watcher.start()

    print("Context Window Viewer")
    if env_hits:
        print(f"  .env loaded from: {', '.join(str(h) for h in env_hits)}")
    for root in claude_roots:
        print(f"  Claude: {root} ({'exists' if root.exists() else 'missing'})")
    for root in codex_roots:
        print(f"  Codex:  {root} ({'exists' if root.exists() else 'missing'})")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(state, watcher))
    url = f"http://{args.host}:{args.port}/"
    print(f"  Open {url}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
