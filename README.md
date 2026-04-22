# Context Window Viewer

A tiny local web app that reads your Claude Code / Cowork and Codex CLI conversations and shows exactly what's sitting in the context window — every system prompt, tool call, tool result, and thinking block — with token counts so you can find what's eating your budget.

Just Python 3.9+ and one dependency (`tiktoken`).

Works on macOS, Linux, and Windows.

---

## Quickstart

### 1. Get the code

Clone this repository, then:

```bash
cd context-viewer
```

### 2. Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

On Windows use `py` instead of `python3`.

### 3. Run it

**macOS / Linux:**

```bash
python3 server.py
```

**Windows:**

```powershell
py server.py
```

### 4. Open the browser

It opens <http://127.0.0.1:8765> automatically. If not, open that URL manually.

That's it. If you have any Claude Code, Cowork, or Codex sessions on your machine, they'll appear in the left sidebar.

---

## Where does it look for sessions?

The viewer auto-detects the standard locations per OS:

| OS      | Claude Code                        | Claude desktop / Cowork                                             | Codex CLI                                                      |
| ------- | ---------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------- |
| macOS   | `~/.claude/projects`               | `~/Library/Application Support/Claude/local-agent-mode-sessions`    | `~/.codex/sessions`, `~/.codex/archived_sessions`              |
| Linux   | `~/.claude/projects`               | `~/.config/Claude/local-agent-mode-sessions`                        | `~/.codex/sessions`, `~/.codex/archived_sessions`              |
| Windows | `%USERPROFILE%\.claude\projects`   | `%APPDATA%\Claude\local-agent-mode-sessions`                        | `%USERPROFILE%\.codex\sessions`, `...\archived_sessions`       |

If a directory doesn't exist on your machine, the viewer just skips it.

---

## Custom paths? Use a `.env` file

If your sessions live somewhere else — or you want to add a second directory — drop a `.env` file next to `server.py`.

### 1. Copy the template

```bash
cp .env.example .env
```

### 2. Edit `.env`

```bash
# macOS / Linux — paths separated by ':'
CLAUDE_SESSIONS_PATH=~/.claude/projects:~/work/claude-archive
CODEX_SESSIONS_PATH=~/.codex/sessions

# Or override the server:
PORT=9000
HOST=127.0.0.1
```

On Windows, separate paths with `;`:

```
CLAUDE_SESSIONS_PATH=%USERPROFILE%\.claude\projects;D:\claude-backup
```

### 3. Restart the server

On startup the viewer prints which `.env` it loaded and which directories it found — so you'll know immediately if a path is wrong.

---

## Other ways to configure

| What you want          | How                                                                 |
| ---------------------- | ------------------------------------------------------------------- |
| One-off custom path    | `python3 server.py --claude /path/to/dir`                           |
| Different port         | `python3 server.py --port 9000` or `PORT=9000` in `.env`            |
| Don't auto-open browser | `python3 server.py --no-browser`                                   |
| Bind to all interfaces | `python3 server.py --host 0.0.0.0` (careful — now reachable on LAN) |

Config resolution order (first match wins): **CLI flag** → **environment variable** → **`.env` file** → **built-in per-OS default**.

---

## What you'll see

**Left sidebar** — every session found, newest first. Filter by client, search by title, or flip to "Search contents" to grep across all conversations. Sub-agent files are grouped under their parent session.

**Right panel — Simple tab**
- KPI cards: total tokens, % of context, message count, top consumer, estimated $ cost
- Where the tokens go — per-kind breakdown (tool results, thinking, text, tool calls, framework, …)
- Heaviest individual blocks — click to jump to them
- Growth-over-turns chart with compaction markers and a rich per-turn hover tooltip
- Turn latency chart (from Claude Code's `turn_duration` events)
- Context-overhead panel itemizing framework / system / inferred hidden prologue tokens
- Auto-detected model, editable context-window limit (200K / 400K / 1M / custom)

**Right panel — Detailed tab**
- Full expandable tree of every block in the session with per-row cost badges on assistant turns
- Mini-bar visualizes cumulative context fill against the selected window
- Tool-specific rendering cards (Bash, Read, Edit, Grep, Glob, Write, WebFetch, Task, TodoWrite, …)
- Markdown + code rendering for assistant text, ANSI colours for terminal output
- Tool-use ↔ tool-result jump links (↑ / ↓ chips on matching rows)
- Load-on-demand for oversized tool outputs that Claude Code persisted to disk
- Search inside the session, filter by block kind, context-window picker

**Header**
- Session context chips (entrypoint, version, git branch, cwd, slug) and clickable sub-agent chips
- Export the session as Markdown / JSON / standalone HTML

---

## Privacy

Nothing leaves your machine. The server binds to `127.0.0.1` by default and every file read happens in the same process that serves the page. No telemetry, no network calls, no cloud.

---

## Troubleshooting

**"No conversations found."**
The default directories don't exist or are empty. Check with `ls` (or `dir` on Windows) and point the viewer at the right path via `.env` or `--claude` / `--codex`.

**Sessions aren't showing up after a refresh.**
Reload the page — the sidebar is cached on the server for speed. Killing and restarting `server.py` also works.

**Very large session feels slow.**
The viewer caps at 20,000 events per file to stay responsive. A yellow banner tells you if a file got truncated.

**Codex rollout shows a lot of `meta` blocks.**
Codex logs include session metadata and turn boundaries alongside actual context events. Only `message`, `function_call`, `function_call_output`, and `reasoning` become first-class nodes; the rest are collapsed under `meta` so nothing is silently dropped.

---

## Files

```
context-viewer/
├── server.py         # Python HTTP server + JSONL parser
├── index.html        # single-file UI (vanilla JS, light theme)
├── requirements.txt  # tiktoken
├── .env.example      # copy to .env to override paths / host / port
└── README.md
```

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
- Heaviest-blocks table is clickable — jumps straight to the block in the Detailed view

**Richer Claude Code parsing**
- Parses `type:"attachment"` events (skill listings, MCP instructions, tool catalog, plan-mode transitions, hook success, todo reminders, date changes, nested memory, IDE selection, file attachments, …)
- Parses `type:"system"` subtypes: `compact_boundary`, `turn_duration`, `away_summary`, `local_command`
- Handles Claude Code's persisted-output offload — oversized tool results can be loaded from disk on demand (up to the full `.txt` file)
- Sub-agent JSONL files (under `<session>/subagents/`) are grouped with their parent session instead of polluting the sidebar
- New `framework` block kind distinguishes harness-injected content from real user input
- Tool-use ↔ tool-result linking via `sourceToolAssistantUUID`
- Permission-mode badges on user rows (plan / bypassPermissions / acceptEdits)
- Compact-file-reference rows are dimmed with a "(purged by /compact)" suffix
- Session-context snapshot (entrypoint, cwd, git branch, version, slug) shown as chips in the header
- AI-generated titles (`ai-title` events) preferred over heuristic title peeking
- Switched to real tiktoken counts everywhere on the client (no more chars/4 estimates)

**Browsing & UX**
- Dark mode with OS `prefers-color-scheme` detection and persisted preference
- Keyboard navigation: `j`/`k` sessions, `/` focus search, `g s`/`g d` switch tabs, `[`/`]` collapse/expand, `t` toggle theme, `?` overlay, `Esc` clear
- URL deep links (`#session=...&tab=detailed&path=...`) — share a link to any block
- Global content search across all sessions with regex + case toggles, debounced
- Live updates: server watches session roots and pushes `session-added`/`updated`/`removed` events over SSE
- Copy button on every expanded block body
- Export a session as Markdown, JSON, or self-contained HTML
- Context-window picker available from both Simple and Detailed views
- Markdown + inline code rendering for assistant text, ANSI colours in tool results
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

MIT. See `LICENSE`.
