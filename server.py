"""Start the BOQ → RFQ web app.

    .venv\\Scripts\\python server.py                 # http://127.0.0.1:8080 (this PC only)
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

from webapp import create_app, setup_url


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="BOQ → RFQ web app")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--lan", action="store_true", help="listen on the office network, not only on this PC")
    ap.add_argument("--host", default=None, help="address to listen on (overrides --lan); behind a reverse proxy: "
                                                  "127.0.0.1")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    app = create_app()
    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    url = f"http://127.0.0.1:{args.port}"
    first_run = setup_url(app, url)
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
    if first_run:
        print("  FIRST RUN : create the administrator account here (one-time link):")
        print(f"              {first_run}")
    print(f"  Jobs      : {'separate worker.py service' if app.config['WORKER_MODE'] == 'external' else 'run by this server'}")
    print("  Close this window to stop the app.")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(first_run or url)).start()
    serve(app, host=host, port=args.port, threads=args.threads, channel_timeout=600,
          max_request_body_size=4 * 1024 ** 3, ident="BOQ2RFQ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
