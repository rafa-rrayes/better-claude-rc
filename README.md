# Better Claude RC

A self-hosted, mobile-first PWA that turns your phone into a proper remote
control for [Claude Code](https://claude.com/claude-code) sessions running on
your Mac.

Start, resume, and drive real Claude Code sessions from your phone — with a
structured event stream (tool cards, diffs, thinking), tap-to-approve
permissions, and live two-way sync with the terminal on your Mac. It's a small
FastAPI + [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python)
backend and a dependency-free vanilla-JS frontend. **No build step, no cloud,
no account** — the server runs on your machine and your phone talks to it over
your own network.

> Built as a genuinely usable replacement for the official Claude Code remote
> flow: new sessions from the phone, structured (not terminal-scraped) output,
> and a UI designed for a small screen.

## Features

- **Start & pick projects from your phone** — recent projects from `~/.claude`
  plus a directory browser; "New session" spawns a real Claude Code session in
  any working directory, with model and permission-mode choice.
- **Resume & fork with terminal parity** — open any past session and see its
  full history backfilled from the transcript; resume keeps the *same* Claude
  session id (or fork into a new one) and appends to the same transcript file
  the terminal reads.
- **Structured event stream** — text streamed as deltas, tool-call cards (Bash
  output, Edit diffs, TodoWrite checklists), thinking blocks, compact
  boundaries, and cost/context meters. Not terminal scraping — real structured
  messages from the SDK.
- **Tap-to-approve permissions** — remote permission prompts park on the server
  and surface on the phone as Allow / Deny (with "always allow" suggestions),
  plan-mode approval, and AskUserQuestion option buttons.
- **Live terminal ↔ webapp sync** — turns typed in the terminal `claude` appear
  in the webapp in real time (transcript tailer); the phone's next message
  silently rotates through resume so both sides share full context on one
  session id.
- **Multi-session tabs** — browser-style open-session switcher; jump between
  live sessions, see per-session status, close individually or all at once.
- **Reconnect with replay** — events are seq-numbered and persisted per session,
  so backgrounding the app on iOS and coming back replays exactly what was
  missed.
- **Library screens** — browse Skills, Slash commands, Agents, and MCP servers
  (global + per-project) read from `~/.claude/*` and `~/.claude.json` /
  `.mcp.json`, with live MCP status from the session init message.
- **Installable PWA** — add to the iOS home screen for a full-screen,
  app-like experience with icons and manifest.

## Quickstart

Requires `uv` ([install](https://docs.astral.sh/uv/getting-started/installation/)).

```bash
uv sync                     # create the venv + install deps (uv fetches Python 3.14)
uv run python -m server     # start the server
```

On start, the server prints its URLs, the auth token, a **one-tap login URL**,
and a **QR code**. Scan the QR with your iPhone camera (or open the login URL)
to launch the PWA and log in in one step.

```bash
uv run python -m server --port 9000   # custom port
uv run python -m server --no-qr       # skip the QR code
uv run python -m server --host 127.0.0.1   # bind localhost only (see Security)
```

## Requirements

- **macOS** (network detection and the launch banner target macOS).
- **Python ≥ 3.14** — `uv sync` fetches it automatically; you don't need it
  pre-installed.
- **Claude Code CLI** installed and authenticated (`claude` on your PATH,
  already logged in). Sessions load your real `~/.claude` config — CLAUDE.md,
  MCP servers, hooks, and custom commands all apply, exactly as in the terminal.

## Remote access

- **Tailscale (recommended).** If the Tailscale CLI is installed and connected,
  the server auto-detects your Tailscale IP and prints it as the primary URL, so
  you can use the app securely from anywhere — no port forwarding, no exposing
  anything to the public internet.
- **LAN fallback.** On the same Wi-Fi, the printed LAN URL works directly.

## Security model

This server exposes control of Claude Code sessions on your Mac. Treat it
accordingly:

- **Token auth.** A random token is generated on first run and stored in
  `data/config.json`. Every request (and the WebSocket) must present it. Login
  sets an **HttpOnly cookie**; the token is embedded in the QR / one-tap login
  URL so onboarding is a single scan.
- **The server binds `0.0.0.0` by default** — it is visible to everyone on your
  local network. That's intended for LAN/Tailscale use. **Do not port-forward it
  to the public internet.** For off-network access, use Tailscale (the token is
  your only credential; keep the login URL private). To restrict to the local
  machine, run with `--host 127.0.0.1`.
- **Nothing sensitive is committed.** The token and all session transcripts live
  under `data/`, which is gitignored and regenerated on first run.

## Handoff semantics

The terminal `claude` and the webapp mirror **one** transcript file (the single
source of truth both append to):

- The webapp can mirror a live terminal session and vice versa; opening a
  session on either side backfills its history and stays in sync.
- **One driver at a time.** Two processes actively driving the *same* session id
  simultaneously would fork the parent chain — so hand off, don't co-drive. The
  webapp mirrors a live terminal, but both typing at once is unsupported by
  design. An already-open terminal TUI can't live-refresh mid-session (a CLI
  limitation); it picks up the phone's turns on the next `--resume` / `--continue`.

## Development

The backend is a normal FastAPI app under `server/`; the frontend is a single
vanilla-JS SPA under `web/` (no build step — edit and reload).

```bash
uv run python scripts/smoke_api.py    # REST smoke suite
uv run python scripts/e2e_live.py     # live end-to-end (text / Bash / compact / resume / sync / hooks)
```

The `scripts/e2e_*.py` files each exercise one slice end-to-end against a live
server (resume parity, permission approve/deny, terminal↔webapp sync, Stop-hook
status feedback).

## License

[MIT](LICENSE) © 2026 Rafa Rayes
