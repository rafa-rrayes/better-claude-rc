"""Entry point: `uv run python -m server`"""

from __future__ import annotations

import argparse

import uvicorn

from .config import Config, lan_ip, tailscale_ip


def main() -> None:
    parser = argparse.ArgumentParser(description="Better Claude RC server")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-qr", action="store_true")
    args = parser.parse_args()

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

    primary = (urls[0][1]) + f"/?token={cfg.token}"
    print(f"\n  One-tap login URL:\n  {primary}\n")

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

    uvicorn.run("server.main:app", host=cfg.host, port=cfg.port,
                log_level="warning")


if __name__ == "__main__":
    main()
