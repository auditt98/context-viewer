<div align="center">

# Context Viewer

**See exactly where your Claude Code context budget goes — and why.**

[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776ab?logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-2e7d32)](LICENSE)
[![Runtime: stdlib + tiktoken](https://img.shields.io/badge/runtime-stdlib%20%2B%20tiktoken-orange)](requirements.txt)
[![Privacy: local-only](https://img.shields.io/badge/privacy-local--only-8e44ad)](#privacy)

A tiny local web app that reads your Claude Code, Cowork, and Codex CLI session
files and breaks down every token in the context window — system prompts, tool
calls, tool results, framework injections, the hidden prologue, and more.

<img width="1280" height="640" alt="social-preview" src="https://github.com/user-attachments/assets/ecdaf598-fc41-4a39-a370-21b07a785880" />

[Quickstart](#quickstart) · [Features](#features) · [Configuration](#configuration) · [Privacy](#privacy) · [Changelog](#changelog)

</div>


---

## Why

Long AI sessions drift toward their context limit silently. It usually isn't
your prompts — it's an oversized `Read`, a 33KB skill manifest injected on
every turn, an unused CLAUDE.md, or cache-read tokens ballooning because the
model keeps re-reading the same 60K of prologue. This tool makes that
**visible, measurable, and actionable**.

> You won't find another viewer that tells you the model saw a **2KB preview
> of a 369KB tool result** the JSONL didn't bother to log, that your
> framework overhead is **35.9% of total**, or that turn 28 cost you **$0.15
> while the median turn cost $0.02**.

## Features

### Analysis

- **$ cost per session** with full Anthropic / OpenAI prompt-caching math (Opus, Sonnet, Haiku, GPT-5.x, GPT-4.1, o-series)
- **Per-turn cost badges** on assistant rows so you can spot the 3 turns that dominated your bill
- **Context overhead panel** itemizing the explicit system prompt, skill manifest, MCP instructions, tool catalog, harness envelopes, and the inferred hidden Claude Code prologue
- **Compaction timeline** on the growth chart — dashed markers with `pre → post` tokens and duration
- **Turn latency panel** (median / p95 / max) from Claude Code's `turn_duration` events
- **Cumulative-fill mini-bars** on every Detailed row, scaled to your active context window (200K / 400K / 1M / custom)
- **Heaviest-blocks table** — click to jump straight to the offender
- **Token math uses real tiktoken** throughout (no chars/4 estimates)

### Deep Claude Code parsing

- `type:"attachment"` subtypes: skill listings, MCP instructions, tool catalog, plan-mode, hook success/additional-context, todo reminders, date changes, nested memory, IDE selection, file attachments, compact file references
- `type:"system"` subtypes: `compact_boundary`, `turn_duration`, `away_summary`, `local_command`
- **Persisted-output offload** — oversized tool results are loaded from disk on demand (full `.txt` file, not the 2KB preview the model saw)
- **Sub-agent file grouping** — `subagents/` files are collapsed under their parent session instead of polluting the sidebar
- **Tool-use ↔ tool-result linking** via `sourceToolAssistantUUID` (↑ / ↓ jump chips)
- **Permission-mode badges** on user rows (plan / bypassPermissions / acceptEdits)
- **Compact-file-reference** rows are dimmed with a "(purged by /compact)" suffix
- Session-context chips in the header: entrypoint, version, git branch, cwd, slug
- AI-generated titles (`ai-title` events) preferred over heuristic title peeking

### Browsing & UX

- **Dark mode** with OS `prefers-color-scheme` and persisted preference
- **Keyboard shortcuts** — `j`/`k` session nav, `/` focus search, `g s`/`g d` switch tabs, `[`/`]` collapse/expand, `t` theme, `?` overlay, `Esc` clear
- **URL deep links** — `#session=...&tab=detailed&path=...`; share a link to any block
- **Global content search** across every session with regex + case toggles
- **Live updates** via file watching + Server-Sent Events — new turns appear as Claude Code writes them
- **Markdown + code rendering** for assistant text, **ANSI colours** for terminal output
- **Tool-specific rendering cards** for `Bash`, `Read`, `Edit`/`MultiEdit`, `Write`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `Task`, `TodoWrite`, `NotebookEdit`
- **Copy block text** and **export session** as Markdown, JSON, or standalone HTML
- Rich per-turn hover tooltip on the growth chart

## Quickstart

```bash
pip install -r requirements.txt
python3 server.py
```

That's it. The viewer opens [http://127.0.0.1:8765](http://127.0.0.1:8765)
automatically and auto-detects the standard session directories on your OS.
If you have any Claude Code, Cowork, or Codex CLI sessions on your machine,
they'll appear in the sidebar.

Requirements: **Python 3.9+** and **`tiktoken`** (the only dependency).

Works on macOS, Linux, and Windows (use `py` instead of `python3` on Windows).

## Where it looks for sessions

Auto-detected per OS:

| OS      | Claude Code                        | Claude desktop / Cowork                                             | Codex CLI                                                      |
| ------- | ---------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------- |
| macOS   | `~/.claude/projects`               | `~/Library/Application Support/Claude/local-agent-mode-sessions`    | `~/.codex/sessions`, `~/.codex/archived_sessions`              |
| Linux   | `~/.claude/projects`               | `~/.config/Claude/local-agent-mode-sessions`                        | `~/.codex/sessions`, `~/.codex/archived_sessions`              |
| Windows | `%USERPROFILE%\.claude\projects`   | `%APPDATA%\Claude\local-agent-mode-sessions`                        | `%USERPROFILE%\.codex\sessions`, `...\archived_sessions`       |

Missing directories are skipped silently.

## Configuration

All three of these work. Order of precedence: **CLI flag → env var → `.env` → built-in default**.

### `.env` file (recommended)

```bash
cp .env.example .env
```

```dotenv
# macOS / Linux — ':' separates multiple paths
CLAUDE_SESSIONS_PATH=~/.claude/projects:~/work/claude-archive
CODEX_SESSIONS_PATH=~/.codex/sessions

PORT=9000
HOST=127.0.0.1
```

On Windows, use `;` instead of `:` for multiple paths.

### CLI flags

```bash
python3 server.py --claude /path/to/sessions     # add a Claude root (repeatable)
python3 server.py --codex  /path/to/sessions     # add a Codex root (repeatable)
python3 server.py --port 9000
python3 server.py --host 0.0.0.0                 # bind to all interfaces (careful!)
python3 server.py --no-browser                   # don't auto-open
```

On startup the server prints which `.env` it loaded and which directories exist, so misconfigured paths surface immediately.

## How it works

```
 JSONL session file  ──►  parser normalises events into a unified Block tree
                                      │
                                      ▼
                   aggregates by kind, computes $, infers hidden overhead
                                      │
                                      ▼
                   ThreadingHTTPServer serves index.html + JSON APIs
                                      │
                                      ▼
                   vanilla-JS client renders Simple / Detailed views
```

Every event — user messages, assistant turns, tool calls, attachments,
system subtypes, sub-agent runs, compactions — becomes a typed `Block` with
`kind`, `label`, `text`, `children`, `meta`. The client aggregates
by kind for the Simple view and walks the tree for the Detailed view. Token
counts use `tiktoken`'s `cl100k_base` encoding server-side; prices come from
a current (checked 2026-04) model → $ table.

Two files carry the whole app: [`server.py`](server.py) (~1.7K LoC) and
[`index.html`](index.html) (~3.1K LoC).

## Privacy

Nothing leaves your machine. The server binds to `127.0.0.1` by default,
every file read happens in the same process that serves the page, and there
are **no telemetry calls, no cloud sync, no outbound network** of any kind.
The only time the viewer reads a file outside your session directories is
when you click "Load full file" on a persisted tool-result — and even then
it's the file Claude Code itself wrote to your `~/.claude/...` folder.

## Troubleshooting

**"No conversations found."**
The default directories don't exist or are empty. Check with `ls` (or `dir`
on Windows) and point the viewer at the right path via `.env` or
`--claude` / `--codex`.

**Sessions don't appear after a refresh.**
Reload the page — the sidebar is cached on the server. Killing and
restarting `server.py` also clears it. With live updates on (the default),
new sessions should appear within ~2s of being written by Claude Code.

**Very large session feels slow.**
The viewer caps at 20,000 events per file to stay responsive. A yellow
banner in the Simple view tells you if a file got truncated.

**"Output too large" on a tool_result.**
That's Claude Code's own message — it persisted the real output to
`<session-dir>/tool-results/<id>.txt`. Expand the tool_result and click
**Load full file (N KB)** to read it.

**Codex rollout shows a lot of `meta` blocks.**
Codex logs include session metadata and turn boundaries alongside actual
context events. Only `message`, `function_call`, `function_call_output`, and
`reasoning` become first-class nodes; the rest collapse under `meta` so
nothing is silently dropped.

## Files

```
context-viewer/
├── server.py         # HTTP server, JSONL parser, pricing, watcher, SSE
├── index.html        # single-file UI (vanilla JS, CSS, dark/light themes)
├── requirements.txt  # tiktoken
├── .env.example      # copy to .env to override paths / host / port
└── README.md
```

No build step. No npm. No frameworks.

---

## Changelog

### Unreleased

**Analysis & visualization**
- Estimated $ cost KPI per session with prompt-caching-aware billing (Opus / Sonnet / Haiku / GPT-5.x / GPT-4.1 / o-series)
- Per-turn cost badges on assistant rows in the Detailed view (red pill when ≥ $0.05)
- Context overhead panel itemizing explicit system prompt, framework injections (skill manifest, MCP instructions, tool catalog, harness envelopes), and the inferred hidden Claude Code prologue
- Compaction timeline markers on the growth chart with before/after token counts and duration
- Turn latency panel showing per-turn duration with median / p95 / max stats
- Rich per-turn hover tooltip on the growth chart (per-kind breakdown, cumulative, duration, compaction note)
- Cumulative-fill mini-bars on every Detailed row, scaled to the active context window
- Heaviest-blocks table is clickable — jumps straight to the block

**Richer Claude Code parsing**
- Parses `type:"attachment"` events (skill listings, MCP instructions, tool catalog, plan-mode, hooks, todo reminders, date changes, nested memory, IDE selection, file attachments)
- Parses `type:"system"` subtypes: `compact_boundary`, `turn_duration`, `away_summary`, `local_command`
- Handles Claude Code's persisted-output offload — oversized tool results load from disk on demand
- Sub-agent JSONL files grouped with their parent session instead of polluting the sidebar
- New `framework` block kind distinguishes harness-injected content from real user input
- Tool-use ↔ tool-result linking via `sourceToolAssistantUUID`
- Permission-mode badges on user rows (plan / bypassPermissions / acceptEdits)
- Compact-file-reference rows dimmed with "(purged by /compact)" suffix
- Session-context snapshot (entrypoint, cwd, git branch, version, slug) shown as chips in the header
- AI-generated titles (`ai-title` events) preferred over heuristic peeking
- Switched to real tiktoken counts everywhere client-side (no more chars/4 estimates)

**Browsing & UX**
- Dark mode with OS `prefers-color-scheme` detection and persisted preference
- Keyboard navigation: `j`/`k` / `/` / `g s`·`g d` / `[`·`]` / `t` / `?` / `Esc`
- URL deep links for session / tab / specific block path
- Global content search across all sessions with regex + case toggles, debounced
- Live updates: server watches session roots and pushes events over SSE
- Copy button on every expanded block body
- Export session as Markdown, JSON, or self-contained HTML
- Context-window picker available in both Simple and Detailed views
- Markdown + inline code rendering, ANSI colours in tool results
- Tool-specific rendering cards for Bash, Read, Edit/MultiEdit, Write, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, NotebookEdit

**Quality**
- Hardened JSONL parser against non-string `text` fields
- Filters out framework-only user events (IDE file-open, interrupt markers) from turn counting
- Minimum 1px bar segment so small turns remain visible on the growth chart

### Initial release

- Zero-dependency local web app (Python stdlib + `tiktoken`) that reads Claude Code / Cowork / Codex CLI `.jsonl` files
- Normalizes wildly different event shapes into a single expandable block tree
- Simple KPI + detailed tree views with growth-over-turns chart and heaviest-block detection
- Auto-detects sessions per OS; overridable via CLI flags, env vars, or `.env`

---

## License

[MIT](LICENSE). Do whatever you want with it.
