"""Entry point for the packaged BOQ2RFQ.exe.

Starts the web app (Waitress) on a free port and opens the browser. Closing the console window stops it.
    BOQ2RFQ.exe          this PC only
    BOQ2RFQ.exe --lan    also reachable for colleagues on the office network
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

# Imported here so PyInstaller bundles them.
import boq_agent.agent  # noqa: F401
import lxml.etree  # noqa: F401
import openpyxl  # noqa: F401
import pymupdf  # noqa: F401
import requests
import xlrd  # noqa: F401
from waitress import serve

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent


def free_port(start: int = 8080) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def check_ollama() -> None:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    model = os.getenv("BOQ_LLM_MODEL", "gemma4:e4b")
    try:
        tags = requests.get(f"{url}/api/tags", timeout=3).json()
        names = {m.get("name") for m in tags.get("models", [])}
        if model in names:
            print(f"  AI model  : {model} (ready)")
        else:
            print(f"  AI model  : NOT FOUND - open a Command Prompt and run:  ollama pull {model}")
            print("              (the app still works, in less accurate keyword-only mode)")
    except Exception:
        print("  AI model  : Ollama is not running - start Ollama from the Start menu.")
        print("              (the app still works, in less accurate keyword-only mode)")


def main() -> None:
    os.environ.setdefault("BOQ_WORKSPACE", str(APP_DIR / "workspace"))
    from webapp import create_app

    lan = "--lan" in sys.argv
    port = free_port()
    url = f"http://localhost:{port}"
    print("=" * 64)
    print("  BOQ -> RFQ")
    print(f"  Address   : {url}")
    if lan:
        try:
            print(f"  Colleagues: http://{socket.gethostbyname(socket.gethostname())}:{port}")
        except OSError:
            pass
    print(f"  Outputs   : {os.environ['BOQ_WORKSPACE']}")
    check_ollama()
    print("  Keep this window open while using the app. Close it to stop.")
    print("=" * 64)
    app = create_app()
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()
    serve(app, host="0.0.0.0" if lan else "127.0.0.1", port=port, threads=16, channel_timeout=600,
          max_request_body_size=4 * 1024 ** 3, ident="BOQ2RFQ")


if __name__ == "__main__":
    main()
