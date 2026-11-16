# Better Claude RC

A self-hosted, mobile-first PWA that turns your phone into a remote control for [Claude Code](https://claude.com/claude-code) sessions running on your Mac.

<!-- TODO: add a screenshot, e.g. docs/screenshot.png -->

Start, resume and drive real Claude Code sessions from your phone. You get a structured event stream (tool cards, diffs, thinking) and tap-to-approve permissions, instead of a scraped terminal. A small FastAPI backend runs on your Mac, built on the [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python). The frontend is a vanilla-JS PWA with no build step.

## Features

- **Start sessions from the phone.** Pick a recent project (read from `~/.claude`) or browse directories, then start a Claude Code session with the model and permission mode you choose.
- **Resume and fork.** Open any past session and see its history backfilled from the transcript. Resume keeps the same session id (or fork into a new one) and appends to the same transcript file the terminal uses.
- **Structured event stream.** Streamed text, tool-call cards (Bash output, Edit diffs, TodoWrite checklists), thinking blocks, compact boundaries, cost and context meters.
- **Tap-to-approve permissions.** Permission prompts surface on the phone as Allow / Deny (with "always allow" suggestions), plan-mode approval, and AskUserQuestion option buttons.
- **Terminal and phone in sync.** Turns typed in the terminal `claude` show up in the app (the server tails the transcript). A terminal view (xterm.js) is also available.
- **Multi-session tabs and reconnect replay.** Events are sequence-numbered and persisted per session, so backgrounding the app and returning replays what you missed.
- **Library screens.** Browse skills, slash commands, agents and MCP servers (global and per project).
- **Installable PWA.** Add it to the home screen for a full-screen app.

## Requirements

- **macOS** (the launch banner and network detection target macOS).
- **[uv](https://docs.astral.sh/uv/getting-started/installation/).** It fetches Python 3.14 for you (`requires-python >= 3.14`).
- **Claude Code CLI** installed and logged in. Sessions load your real `~/.claude` config (CLAUDE.md, MCP servers, hooks, commands), as in the terminal.
- A phone on the same network as the Mac, or both on [Tailscale](https://tailscale.com).

## Quickstart (local mode)

On the Mac:

```bash
git clone https://github.com/rafa-rrayes/better-claude-rc
cd better-claude-rc
uv sync
uv run python -m server          # or: uv run better-claude-rc
```

The server prints its URLs, the auth token, a one-tap login link (valid 2 minutes, single use) and a QR code. If the Tailscale CLI is connected, the Tailscale URL is listed first.

On the phone:

1. Scan the QR with the camera (or open the login link). You are logged in and the app opens.
2. Add it to the home screen (iOS: Share, then Add to Home Screen; Android: browser menu, then Install app).
3. Next time, open the app from the home screen. The login cookie lasts 180 days.

Options:

```bash
uv run python -m server --port 9000        # default port is 8787
uv run python -m server --no-qr            # no QR code
uv run python -m server --host 127.0.0.1   # this Mac only
```

## Configuration

State lives in `data/` inside the checkout (or `~/.better-claude-rc` when installed as a wheel). Set `BCRC_DATA_DIR` to override.

`data/config.json` is created on first run:

| Key | Default | Meaning |
| --- | --- | --- |
| `token` | random | The auth token. Delete the file to rotate it. |
| `host` | `0.0.0.0` | Bind address (`--host` overrides). |
| `port` | `8787` | Port (`--port` overrides). |
| `browse_root` | `~` | The directory browser and file reads are confined to this tree. |
| `relay_url` | unset | Only used by connect mode (see below). |

## Architecture

```
phone (PWA)  <--HTTP/WebSocket-->  FastAPI server on your Mac  -->  claude-agent-sdk -> Claude Code
                                     |-- tails ~/.claude transcripts (terminal sync)
                                     `-- pty manager (terminal view)
```

- `server/` is the backend: `main.py` (HTTP + `/ws`), `rpc.py` (method table), `sessions.py` (SDK sessions, permissions, replay), `watch.py` (transcript tailer), `terminal.py`, `library.py`, `projects.py`.
- `server/web/` is the PWA (`app.js`, `transport.js`, `crypto.js`, `style.css`), served by the same server.
- `platform/` is an optional hosted relay, covered next.

### Connect mode and the hosted platform (optional)

Local mode needs your phone to reach the Mac directly. The alternative is `better-claude-rc connect`: the Mac dials out to a relay, so no inbound port is needed. This repo contains the relay and account service (`platform/`: FastAPI, Postgres, email magic links, Stripe billing). `docs/DEPLOY.md` and `docs/RUNBOOK.md` describe running it yourself. The author also runs one at [bcrc.rrayes.com](https://bcrc.rrayes.com). Enrolling a machine there uses a one-line installer from the dashboard; the free plan allows 1 live Claude session and the pro plan 10. Local mode is unaffected by any of this and needs no account.

## Security model

This server controls Claude Code on your Mac, which can run commands and edit files. Treat access to it like shell access.

### Local mode (`python -m server`)

- **Authentication.** A random token (`secrets.token_urlsafe(24)`) is generated on first run and stored in plain text in `data/config.json`. Every API request and the WebSocket must present it, as an `Authorization: Bearer` header or the `bcrc_token` cookie. Comparison is constant-time.
- **Pairing the phone.** The QR and login link carry a one-time login code (2 minutes, single use, in the URL fragment so it is not sent to the server or put in query strings), not the token. Redeeming it sets an HttpOnly, SameSite=Lax cookie holding the token. You can also log in by pasting the token.
- **WebSocket.** Cookie-only auth (no tokens in query strings) plus a same-origin check on browser upgrades.
- **Filesystem confinement.** The directory browser, file reads and new-session working directories must be inside `browse_root` (home by default). Claude itself, once running, can do whatever your permission mode allows.
- **Network exposure.** The server binds `0.0.0.0` by default and speaks plain HTTP. The token and cookie travel unencrypted, so anyone on the network path can read them. The cookie has no `Secure` flag because the app is served over HTTP. libsodium is not used in this mode.
- **Recommended setup.** Use Tailscale (traffic is WireGuard-encrypted and nothing is exposed publicly) or a trusted home LAN. Do not port-forward it or put it on the public internet. If you only use Tailscale, you can pass `--host` with your Tailscale IP, or `127.0.0.1` if you front it yourself. Keep the printed login link private.

### Connect mode (`better-claude-rc connect`)

- **Outbound only.** The agent dials out to the relay. Its local server binds `127.0.0.1` unless you pass `--lan`.
- **Identity.** The Mac generates an Ed25519 key pair; the seed stays in `data/identity.json` (mode 0600, data dir 0700) and the platform stores only the public key. The phone has its own Ed25519 key in the browser's IndexedDB.
- **Pairing.** A 32-byte single-use secret (valid 10 minutes, held only in the agent's memory) is shown as a QR and code in the terminal. The relay never sees it. It authenticates the phone's first pairing message and lets the phone pin the Mac's public key. Paired phone keys are stored in `data/devices.json` and can be revoked.
- **What libsodium does.** Both ends use libsodium (PyNaCl on the Mac, the vendored `libsodium.js` in the phone's browser): `crypto_sign` (Ed25519) signatures on the handshake, `crypto_kx` for a fresh key exchange per connection, `crypto_secretstream_xchacha20poly1305` to encrypt and authenticate every frame, and keyed BLAKE2b for the pairing MAC and key derivation.
- **What the relay sees.** The relay routes opaque ciphertext. The agent refuses plaintext, tampered or replayed frames and unpaired devices, and closes the connection (`scripts/e2e_crypto.py` tests these cases). The relay still sees account email, machine names, connection timing and traffic volume. Session ids travel inside the encrypted stream.
- **Caveat.** The relay serves the web app code to your phone, so you are trusting the relay operator not to ship malicious JavaScript. Self-host it if that matters to you.

## Limitations

- macOS only; Python 3.14+.
- One driver per session at a time. The app mirrors a live terminal session, but typing in both at once is unsupported and can fork the transcript chain. An already-open terminal TUI cannot live-refresh; it sees the phone's turns on its next `--resume` or `--continue`.
- Local mode is plain HTTP, so use Tailscale or a trusted LAN.
- No tests drive a real Claude turn in CI; the `scripts/e2e_*.py` files do, and need a logged-in `claude`.
- Beta: expect rough edges.

## Related projects

Same author, different scope:

- **better-claude-rc** (this repo): the lightweight option. One Mac, phone first, runs on your machine with no account.
- **[valet](https://github.com/rafa-rrayes/valet)**: a control plane for always-on agents across containers and machines, with web and iOS/macOS apps.
- **[cockpit](https://github.com/rafa-rrayes/cockpit)**: a VS Code extension giving a live overview of all your Claude Code sessions.

## Development

```bash
uv sync
uv run pytest                          # auth/unit tests, no Claude needed
uv run python scripts/smoke_api.py     # REST smoke test
uv run python scripts/e2e_crypto.py    # E2E crypto negative tests (in-process relay)
uv run python scripts/e2e_live.py      # live end-to-end; needs a logged-in `claude`
```

The frontend has no build step: edit `server/web/` and reload.

## License

[MIT](LICENSE) (c) 2026 Rafa Rayes.

The PWA vendors third-party files under `server/web/vendor/`, each under its own license (see the `VENDOR.md` in each folder):

- **xterm.js** and its fit addon: MIT (`xterm.css` keeps its original copyright header).
- **libsodium.js** 0.7.15: ISC.
- **jsQR** 1.4.0: Apache-2.0.
