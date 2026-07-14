"""Command line entry point: `better-claude-rc [serve|connect]`.

serve   — run the local server (default; today's behavior).
connect — dial out to the hosted relay platform (stub; lands with M2).
"""

from __future__ import annotations

import argparse
import sys

SUBCOMMANDS = {"serve", "connect"}


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .config import Config, lan_ip, tailscale_ip

    cfg = Config.load()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port

    lan = lan_ip()
    ts = tailscale_ip()
    urls = []
    if ts:
        urls.append(("Tailscale (use from anywhere)", f"http://{ts}:{cfg.port}"))
    if lan:
        urls.append(("LAN (same wifi)", f"http://{lan}:{cfg.port}"))
    urls.append(("This Mac", f"http://localhost:{cfg.port}"))

    print()
    print("  ┌─────────────────────────────────────────────┐")
    print("  │  Better Claude RC — remote control server   │")
    print("  └─────────────────────────────────────────────┘")
    for label, url in urls:
        print(f"  {label:28s} {url}")
    print(f"\n  Auth token: {cfg.token}")

    # One-time login code (2 min, single use). It travels in the URL
    # *fragment* — never in a query string — and the QR never carries the
    # long-term token.
    from . import main as main_mod
    code = main_mod.issue_login_code()
    primary = urls[0][1] + f"/#login={code}"
    print(f"\n  One-tap login link (valid 2 min, single use):\n  {primary}\n")

    if not args.no_qr:
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(primary)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
            print("  Scan with the iPhone camera to open + log in.\n")
        except Exception:
            pass

    uvicorn.run(main_mod.app, host=cfg.host, port=cfg.port, log_level="warning")


def _connect(args: argparse.Namespace) -> None:
    """Run the local server (127.0.0.1 unless --lan) + the relay uplink."""
    import asyncio
    import os
    import socket

    from . import VERSION, proto
    from . import main as main_mod
    from .config import DATA_DIR
    from .identity import EnrollError, Identity, enroll, platform_http_url

    cfg = main_mod.config
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    port = args.port or cfg.port
    relay = (args.relay or os.environ.get("BCRC_RELAY_URL")
             or cfg.extra.get("relay_url"))
    if not relay:
        print("No relay URL. Pass --relay wss://… or set BCRC_RELAY_URL.")
        sys.exit(2)

    # Ed25519 identity: generated on this machine, seed never leaves
    # data/identity.json (0600). The platform stores the public key only.
    identity = Identity.load_or_create(DATA_DIR / "identity.json")

    if args.enroll_token:
        try:
            identity.machine_id = enroll(
                platform_http_url(relay), args.enroll_token, identity,
                name=socket.gethostname(), agent_version=VERSION,
                proto=proto.PROTO)
        except EnrollError as e:
            print(f"[bcrc] {e}")
            sys.exit(1)
        identity.save()
        print(f"[bcrc] machine enrolled — id {identity.machine_id}")

    machine_id = identity.machine_id
    if not machine_id:
        # Dev fallback (relay without a database): a locally-chosen id. A
        # production relay refuses it — run `connect <enroll-token>` first.
        machine_id = (os.environ.get("BCRC_MACHINE_ID")
                      or cfg.extra.get("machine_id"))
        if not machine_id:
            print("This machine is not enrolled. Get a command from the "
                  "dashboard:\n  uvx better-claude-rc connect <enroll-token>")
            sys.exit(2)
        identity = None  # unsigned dev header

    print()
    print("  Better Claude RC — connect mode")
    print(f"  Local app     http://{'localhost' if host == '127.0.0.1' else host}:{port}")
    print(f"  Relay         {relay}")
    print(f"  Machine id    {machine_id}")
    print()

    try:
        asyncio.run(_connect_run(main_mod, host, port, relay, machine_id,
                                 identity))
    except KeyboardInterrupt:
        pass


async def _connect_run(main_mod, host: str, port: int,
                       relay: str, machine_id: str, identity=None) -> None:
    import asyncio
    import contextlib
    import os

    import uvicorn

    from .connect import Uplink

    server = uvicorn.Server(uvicorn.Config(
        main_mod.app, host=host, port=port, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        if server_task.done():
            server_task.result()  # surface bind errors
            return
        await asyncio.sleep(0.05)
    while main_mod.manager is None:
        await asyncio.sleep(0.05)

    uplink = Uplink(
        main_mod.mgr(), main_mod.terms, main_mod.config, relay, machine_id,
        identity=identity,
        ping_interval=float(os.environ.get("BCRC_PING_INTERVAL", 20)),
        ping_timeout=float(os.environ.get("BCRC_PING_TIMEOUT", 25)),
    )
    uplink_task = asyncio.create_task(uplink.run())
    try:
        done, _ = await asyncio.wait({server_task, uplink_task},
                                     return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (uplink_task, server_task):
            if not task.done():
                if task is server_task:
                    server.should_exit = True
                else:
                    task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await uplink_task
        await server_task


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # "serve" is the default subcommand: `better-claude-rc --no-qr` works.
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "serve")

    parser = argparse.ArgumentParser(
        prog="better-claude-rc",
        description="Mobile PWA remote control for Claude Code",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the local server (default)")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-qr", action="store_true")
    p_serve.set_defaults(func=_serve)

    p_connect = sub.add_parser("connect", help="connect to the hosted platform")
    p_connect.add_argument("enroll_token", nargs="?", default=None)
    p_connect.add_argument("--relay", default=None,
                           help="relay URL (or BCRC_RELAY_URL)")
    p_connect.add_argument("--port", type=int, default=None,
                           help="local server port")
    p_connect.add_argument("--lan", action="store_true",
                           help="bind the local server to 0.0.0.0 "
                                "(default: 127.0.0.1)")
    p_connect.set_defaults(func=_connect)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
