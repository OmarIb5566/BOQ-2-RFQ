"""Start the BOQ → RFQ web app.

    .venv\\Scripts\\python server.py                 # http://localhost:8080 (this PC only)
    .venv\\Scripts\\python server.py --lan           # also reachable from the office network
    .venv\\Scripts\\python server.py --port 9000

Waitress serves many users at once (one thread per request); heavy work (analysis, export) runs in the
app's own job queue, one job at a time, so the GPU is never shared between two analyses.
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

from waitress import serve

from webapp import create_app


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="BOQ → RFQ web app")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lan", action="store_true", help="listen on the office network, not only on this PC")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    app = create_app()
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    url = f"http://localhost:{args.port}"
    print("=" * 64)
    print("  BOQ -> RFQ  web app")
    print(f"  Open      : {url}")
    if args.lan:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"  Colleagues: http://{ip}:{args.port}")
        except OSError:
            pass
    print(f"  Workspace : {app.config['SETTINGS'].workspace}")
    print("  Close this window to stop the app.")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    serve(app, host=host, port=args.port, threads=args.threads, channel_timeout=600,
          max_request_body_size=4 * 1024 ** 3, ident="BOQ2RFQ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
