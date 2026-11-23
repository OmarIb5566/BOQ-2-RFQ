"""Background worker: runs the analysis / export / RFQ jobs from the shared job queue.

    .venv\\Scripts\\python worker.py            (Windows)
    .venv/bin/python worker.py                (Linux)

Run it as a service next to server.py and set BOQ_WORKER=external for the web server (so the web server does not
also run jobs). Use the same boq2rfq.toml / environment as the web server: same workspace, same database.
One worker per GPU-backed model server; several workers on one GPU only make each job slower.
"""
from __future__ import annotations

import signal
import sys


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from webapp.runtime import build_runtime
    rt = build_runtime()
    stopping = {"now": False}

    def stop(*_):
        stopping["now"] = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"BOQ -> RFQ worker · workspace {rt.settings.workspace} · database {rt.instance_path / 'boq2rfq.db'}",
          flush=True)
    rt.jobs.work_forever(rt, "external", stop=lambda: stopping["now"])
    print("worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
