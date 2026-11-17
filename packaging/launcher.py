"""Entry point for the packaged BOQ2RFQ.exe.

Starts the Streamlit server on a free local port and opens the browser.
Closing the console window stops the app.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

# Imported here (not only inside BOQ2RFQ.py) so PyInstaller bundles them.
import boq_agent.agent  # noqa: F401
import lxml.etree  # noqa: F401
import openpyxl  # noqa: F401
import pandas  # noqa: F401
import pymupdf  # noqa: F401
import requests
import streamlit.web.cli as stcli
import xlrd  # noqa: F401

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))


def free_port(start: int = 8501) -> int:
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
    port = free_port()
    url = f"http://localhost:{port}"

    print("=" * 64)
    print("  BOQ -> RFQ")
    print(f"  Address   : {url}")
    print(f"  Outputs   : {os.environ['BOQ_WORKSPACE']}")
    check_ollama()
    print("  Keep this window open while using the app. Close it to stop.")
    print("=" * 64)

    threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    sys.argv = [
        "streamlit", "run", str(BUNDLE_DIR / "BOQ2RFQ.py"),
        "--global.developmentMode=false",
        "--server.headless=true",
        f"--server.port={port}",
        "--server.address=localhost",
        "--browser.gatherUsageStats=false",
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
