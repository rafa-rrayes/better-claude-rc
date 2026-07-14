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
    # Implemented in M2 (relay + connect mode). The subcommand exists now so
    # the enrollment one-liner is stable: `uvx better-claude-rc connect <tok>`.
    print("connect mode is not available yet — coming in a future release.")
    if args.enroll_token:
        print(f"(enroll token received: {args.enroll_token[:12]}…)")
    sys.exit(2)


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
    p_connect.set_defaults(func=_connect)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
