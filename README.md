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

**Left sidebar** — every session found, newest first. Filter by client and search by title or path.

**Right panel — Simple tab**
- Total tokens and % of your context window used
- Where the tokens go (tool results vs. thinking vs. text vs. tool calls)
- Heaviest individual blocks — click to jump to them
- Growth-over-turns chart
- Auto-detected model + editable context-window limit

**Right panel — Detailed tab**
- Full expandable tree of every block in the session
- Per-block token/char counts
- Search inside the conversation
- Filter by block kind

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

## License

MIT. See `LICENSE`.
