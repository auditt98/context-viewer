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
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse, parse_qs


# ---------- tokenizer -----------------------------------------------------
import tiktoken

_ENC = tiktoken.get_encoding("cl100k_base")


def est_tokens(text: str) -> int:
    if not text:
        return 0
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
        return {
            "kind": self.kind,
            "label": self.label,
            "text": self.text,
            "children": [c.to_json() for c in self.children],
            "meta": self.meta,
            "chars": len(self.text) + sum(len(c.text) for c in self.children),
            "tokens": est_tokens(self.text) + sum(est_tokens(c.text) for c in self.children),
        }


@dataclass
class Session:
    id: str
    client: str          # "claude" or "codex"
    title: str
    path: str
    mtime: float
    blocks: list[Block] = field(default_factory=list)

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


def _blocks_from_content(content: Any) -> list[Block]:
    """
    Claude-style `message.content` is either a string or a list of blocks like
    [{"type": "text", "text": ...}, {"type": "tool_use", ...},
     {"type": "tool_result", "content": [...]}].
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
            out.append(Block(
                kind="tool_result",
                label=f"tool_result ({inner_tokens} tok)",
                text="",
                children=sub,
                meta={
                    "tool_use_id": item.get("tool_use_id"),
                    "is_error": item.get("is_error"),
                },
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


def _parse_claude_entry(entry: dict) -> Block | None:
    etype = entry.get("type")
    if etype == "summary":
        text = entry.get("summary", "")
        return Block(kind="meta", label=f"summary: {_short(text)}", text=text)
    if etype in ("user", "assistant", "system"):
        msg = entry.get("message") or {}
        role = msg.get("role") or etype
        content = msg.get("content")
        kids = _blocks_from_content(content)
        # collapse single text children into the parent preview for nicer labels
        joined = "\n".join(c.text for c in kids) if kids else _stringify(msg)
        label = f"{role} \u00b7 {_short(joined)}" if joined else role
        return Block(
            kind=_role_kind(role),
            label=label,
            text="",
            children=kids,
            meta={
                "timestamp": entry.get("timestamp"),
                "uuid": entry.get("uuid"),
                "parentUuid": entry.get("parentUuid"),
                "model": msg.get("model"),
                "usage": msg.get("usage"),
            },
        )
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


def _peek_title(path: Path, client: str, *, max_lines: int = 10, max_bytes: int = 65536) -> str:
    """Cheap title extraction — read only the first few lines."""
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
            if e.get("type") == "summary":
                s = e.get("summary", "")
                if s: return _short(s, 90)
            if e.get("type") == "user" and first_user is None:
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
    for b in blocks:
        if b.kind == "meta" and b.label.startswith("summary:"):
            return b.label[len("summary:"):].strip() or fallback
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


def _list_one(root: Path, client: str) -> list[dict]:
    """Very cheap metadata-only scan — no file reads, just `stat()`."""
    if not root.exists():
        return []
    out: list[dict] = []
    for path in sorted(root.rglob("*.jsonl")):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append({
            "id": f"{client}::{path}",
            "client": client,
            "title": _fallback_title(path, client),
            "path": str(path),
            "mtime": st.st_mtime,
            "size": st.st_size,
        })
    return out


def list_claude_sessions(root: Path) -> list[dict]:
    return _list_one(root, "claude")


def list_codex_sessions(root: Path) -> list[dict]:
    return _list_one(root, "codex")


MAX_ENTRIES_PER_FILE = 20_000  # safety cap — keeps UI responsive on huge audit logs


def _parse_file(path: Path, client: str) -> Session:
    """Full parse for a single file. Robust to bad data. Capped for safety."""
    blocks: list[Block] = []
    seen = 0
    truncated = False
    for e in _load_jsonl(path):
        seen += 1
        if seen > MAX_ENTRIES_PER_FILE:
            truncated = True
            break
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
    )


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

    def list_all(self) -> list[dict]:
        """Very cheap directory scan: stat + path-derived title only."""
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
        self._meta = {it["id"]: it for it in out}
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


def make_handler(state: State):
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

    print("Context Window Viewer")
    if env_hits:
        print(f"  .env loaded from: {', '.join(str(h) for h in env_hits)}")
    for root in claude_roots:
        print(f"  Claude: {root} ({'exists' if root.exists() else 'missing'})")
    for root in codex_roots:
        print(f"  Codex:  {root} ({'exists' if root.exists() else 'missing'})")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
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
