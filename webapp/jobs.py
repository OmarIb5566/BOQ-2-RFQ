"""Background jobs: analysis, export, re-reading documents, RFQ detection.

One worker thread runs the jobs one after the other: the GPU (Ollama) and Microsoft Excel are single
resources, so running two analyses at once would only make both slower. Users keep working in the UI
meanwhile; every job reports progress and its place in the queue.
"""
from __future__ import annotations

import queue
import threading
import traceback
import uuid
from typing import Callable, Optional

from .db import DB, now

JobFn = Callable[[Callable[[float, str], None]], Optional[str]]   # fn(progress) -> optional result message


class Jobs:
    def __init__(self, db: DB, app=None):
        self.db = db
        self.app = app                             # jobs run inside the Flask app context (settings, url_for)
        self.q: "queue.Queue[tuple[str, JobFn]]" = queue.Queue()
        self.live: dict[str, dict] = {}            # id -> progress / message (no DB write per tick)
        self._lock = threading.Lock()
        # jobs that were queued / running when the server stopped will never finish
        db.x("UPDATE jobs SET status='interrupted', finished=?, error='The server was restarted.' "
             "WHERE status IN ('queued','running')", (now(),))
        db.x("UPDATE projects SET stage='failed', error='Analysis was interrupted (server restarted).' "
             "WHERE stage='analyzing'")
        self.worker = threading.Thread(target=self._run, name="boq-jobs", daemon=True)
        self.worker.start()

    def submit(self, kind: str, label: str, fn: JobFn, run_id: str = "", user_id: Optional[int] = None) -> str:
        jid = uuid.uuid4().hex[:12]
        self.db.x("INSERT INTO jobs (id, run_id, user_id, kind, label, status, created) VALUES (?,?,?,?,?,?,?)",
                  (jid, run_id, user_id, kind, label, "queued", now()))
        with self._lock:
            self.live[jid] = {"progress": 0.0, "message": "Waiting in the queue …"}
        self.q.put((jid, fn))
        return jid

    def active_for(self, run_id: str) -> Optional[dict]:
        r = self.db.one("SELECT * FROM jobs WHERE run_id=? AND status IN ('queued','running') "
                        "ORDER BY created LIMIT 1", (run_id,))
        return self.view(r) if r else None

    def get(self, jid: str) -> Optional[dict]:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        return self.view(r) if r else None

    def view(self, r) -> dict:
        d = dict(r)
        live = self.live.get(d["id"])
        if live and d["status"] in ("queued", "running"):
            d.update(live)
        if d["status"] == "queued":
            ahead = self.db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running') AND created < ?",
                                (d["created"],))["n"]
            d["position"] = ahead
            d["message"] = f"Waiting — {ahead} job(s) ahead" if ahead else "Starting …"
        return d

    def recent(self, user_id: Optional[int] = None, limit: int = 20) -> list[dict]:
        if user_id is None:
            rows = self.db.q("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        else:
            rows = self.db.q("SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT ?", (user_id, limit))
        return [self.view(r) for r in rows]

    # ------------------------------------------------------------------ worker
    def _run(self) -> None:
        try:
            import pythoncom          # Excel automation needs COM initialised on this thread
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        while True:
            jid, fn = self.q.get()
            self.db.x("UPDATE jobs SET status='running', started=? WHERE id=?", (now(), jid))

            def progress(p: float, msg: str, _jid=jid) -> None:
                with self._lock:
                    self.live[_jid] = {"progress": max(0.0, min(1.0, float(p))), "message": msg}

            try:
                if self.app is not None:
                    with self.app.app_context():
                        result = fn(progress) or ""
                else:
                    result = fn(progress) or ""
                self.db.x("UPDATE jobs SET status='done', progress=1, message=?, finished=? WHERE id=?",
                          (result or "Done", now(), jid))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.db.x("UPDATE jobs SET status='failed', finished=?, error=?, message=? WHERE id=?",
                          (now(), str(e)[:2000], "Failed", jid))
            finally:
                with self._lock:
                    self.live.pop(jid, None)
                self.q.task_done()
