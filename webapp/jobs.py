"""Background jobs: a queue in the database, run by a worker.

* A job is a NAMED task (webapp/tasks.py) plus JSON arguments — not a Python closure — so it can run in another
  process: `worker.py` (production) or the web server's own worker thread (BOQ_WORKER=inline, the default for a
  single PC).
* One job runs at a time per worker: the GPU (Ollama) and Microsoft Excel are single resources.
* Fair queue: the next job is taken from the user who has waited longest, so one person's ten uploads do not
  block everyone else.
* Heartbeats: a running job updates its heartbeat every few seconds. A job whose worker died (no heartbeat for
  STALE_S) is put back in the queue (or marked interrupted after max_attempts).
* Temporary failures (the AI engine not reachable, the database busy) are retried later; other errors fail the job.
* Cancel: a queued job is dropped; a running job stops at its next progress report.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import traceback
import uuid
from typing import Callable, Optional

from .db import DB, now

STALE_S = 90            # no heartbeat for this long -> the worker is gone
HEARTBEAT_S = 5
POLL_S = 1.0
RETRY_DELAY_S = 30      # first retry after 30 s, then 60 s, 90 s …


class Cancelled(Exception):
    pass


def is_transient(e: BaseException) -> bool:
    """Errors worth retrying later: the model server or the database was briefly unavailable."""
    try:
        import requests
        if isinstance(e, (requests.ConnectionError, requests.Timeout)):
            return True
    except ImportError:
        pass
    from boq_agent.llm.client import LLMError
    if isinstance(e, LLMError) and ("connect" in str(e).lower() or "timed out" in str(e).lower()):
        return True
    return isinstance(e, sqlite3.OperationalError) and "locked" in str(e)


class Jobs:
    """The queue. `start_worker()` runs a worker thread in this process (inline mode)."""

    def __init__(self, db: DB):
        self.db = db
        self._threads: list[threading.Thread] = []
        self._stop = False

    # ------------------------------------------------------------------ queue
    def submit(self, kind: str, label: str, run_id: str = "", user_id: Optional[int] = None,
               args: Optional[dict] = None, max_attempts: int = 3) -> str:
        from . import tasks
        if kind not in tasks.REGISTRY:
            raise ValueError(f"unknown job kind {kind!r}")
        jid = uuid.uuid4().hex[:12]
        self.db.x("INSERT INTO jobs (id, run_id, user_id, kind, label, status, created, args, max_attempts, message) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (jid, run_id, user_id, kind, label, "queued", now(), json.dumps(args or {}, ensure_ascii=False),
                   max_attempts, "Waiting in the queue …"))
        return jid

    def cancel(self, jid: str) -> str:
        r = self.db.one("SELECT status FROM jobs WHERE id=?", (jid,))
        if r is None:
            return "unknown"
        if r["status"] == "queued":
            self.db.x("UPDATE jobs SET status='cancelled', finished=?, message='Cancelled' WHERE id=? "
                      "AND status='queued'", (now(), jid))
            return "cancelled"
        if r["status"] == "running":
            self.db.x("UPDATE jobs SET cancel=1 WHERE id=?", (jid,))
            return "stopping"
        return r["status"]

    def retry(self, jid: str) -> bool:
        n = self.db.x("UPDATE jobs SET status='queued', attempts=0, error='', progress=0, cancel=0, not_before=0, "
                      "message='Waiting in the queue …', finished=NULL WHERE id=? "
                      "AND status IN ('failed','interrupted','cancelled')", (jid,))
        return bool(self.db.one("SELECT 1 FROM jobs WHERE id=? AND status='queued'", (jid,))) or bool(n)

    def active_for(self, run_id: str) -> Optional[dict]:
        r = self.db.one("SELECT * FROM jobs WHERE run_id=? AND status IN ('queued','running') "
                        "ORDER BY created LIMIT 1", (run_id,))
        return self.view(r) if r else None

    def get(self, jid: str) -> Optional[dict]:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
        return self.view(r) if r else None

    def view(self, r) -> dict:
        d = dict(r)
        d.pop("args", None)
        if d["status"] == "queued":
            ahead = self.db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running') AND created < ?",
                                (d["created"],))["n"]
            d["position"] = ahead
            if d.get("not_before", 0) > time.time():
                d["message"] = f"Will retry in {int(d['not_before'] - time.time())} s ({d['error'][:80]})"
            else:
                d["message"] = f"Waiting — {ahead} job(s) ahead" if ahead else "Starting …"
        eta = self.estimate(d)
        if eta is not None:
            d["eta_s"] = eta
        return d

    def recent(self, user_id: Optional[int] = None, limit: int = 20) -> list[dict]:
        if user_id is None:
            rows = self.db.q("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        else:
            rows = self.db.q("SELECT * FROM jobs WHERE user_id=? ORDER BY created DESC LIMIT ?", (user_id, limit))
        return [self.view(r) for r in rows]

    def estimate(self, d: dict) -> Optional[int]:
        """Seconds left, from the median duration of the last finished jobs of the same kind (None = no history)."""
        rows = self.db.q("SELECT seconds FROM jobs WHERE kind=? AND status='done' AND seconds IS NOT NULL "
                         "ORDER BY finished DESC LIMIT 15", (d["kind"],))
        if len(rows) < 3:
            return None
        xs = sorted(r["seconds"] for r in rows)
        typical = xs[len(xs) // 2]
        if d["status"] == "running" and d.get("started"):
            import datetime as dt
            spent = (dt.datetime.now() - dt.datetime.fromisoformat(d["started"])).total_seconds()
            return max(0, int(typical - spent))
        return int(typical) if d["status"] == "queued" else None

    # ------------------------------------------------------------------ worker side
    def recover(self) -> int:
        """Jobs whose worker stopped heart-beating go back to the queue (or are marked interrupted)."""
        cutoff = time.time() - STALE_S
        n = 0
        with self.db.tx() as c:
            rows = c.execute("SELECT id, run_id, kind, attempts, max_attempts FROM jobs WHERE status='running' "
                             "AND (heartbeat IS NULL OR heartbeat < ?)", (cutoff,)).fetchall()
            for r in rows:
                if r["attempts"] < r["max_attempts"]:
                    c.execute("UPDATE jobs SET status='queued', worker=NULL, message='Restarting after an "
                              "interruption …' WHERE id=?", (r["id"],))
                else:
                    c.execute("UPDATE jobs SET status='interrupted', finished=?, error='The worker stopped while "
                              "this job was running.' WHERE id=?", (now(), r["id"]))
                    if r["kind"] == "analyze":
                        c.execute("UPDATE projects SET stage='failed', error='Analysis was interrupted.' "
                                  "WHERE run_id=? AND stage='analyzing'", (r["run_id"],))
                n += 1
            c.execute("DELETE FROM workers WHERE seen < ?", (cutoff,))
        return n

    def claim(self, worker_id: str) -> Optional[dict]:
        """Take the next job — fair across users: the user whose last job started longest ago goes first."""
        with self.db.tx() as c:
            r = c.execute(
                "SELECT j.* FROM jobs j WHERE j.status='queued' AND j.not_before <= ? ORDER BY "
                "(SELECT COALESCE(MAX(started), '') FROM jobs k WHERE k.user_id IS j.user_id AND k.started IS NOT NULL), "
                "j.created LIMIT 1", (time.time(),)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE jobs SET status='running', started=?, heartbeat=?, worker=?, attempts=attempts+1, "
                      "progress=0, message='Starting …' WHERE id=?", (now(), time.time(), worker_id, r["id"]))
        return dict(r)

    def run_one(self, worker_id: str, runtime) -> bool:
        """Claim and run one job. Returns False when the queue had nothing to do."""
        from . import tasks
        job = self.claim(worker_id)
        if job is None:
            return False
        jid = job["id"]
        self.db.x("UPDATE workers SET job_id=? WHERE id=?", (jid, worker_id))
        state = {"last": 0.0, "stop": False}

        def progress(p: float, msg: str) -> None:
            t = time.time()
            if t - state["last"] >= 0.5 or p >= 1:
                state["last"] = t
                self.db.x("UPDATE jobs SET progress=?, message=?, heartbeat=? WHERE id=?",
                          (max(0.0, min(1.0, float(p))), str(msg)[:300], t, jid))
                if self.db.one("SELECT cancel FROM jobs WHERE id=?", (jid,))["cancel"]:
                    raise Cancelled()

        def beat():
            while not state["stop"]:
                self.db.x("UPDATE jobs SET heartbeat=? WHERE id=? AND status='running'", (time.time(), jid))
                self.db.x("UPDATE workers SET seen=? WHERE id=?", (time.time(), worker_id))
                for _ in range(HEARTBEAT_S * 10):
                    if state["stop"]:
                        break
                    time.sleep(0.1)
        hb = threading.Thread(target=beat, daemon=True)
        hb.start()
        t0 = time.time()
        try:
            fn = tasks.REGISTRY[job["kind"]]
            ctx = tasks.JobContext(job_id=jid, run_id=job["run_id"] or "", user_id=job["user_id"],
                                   args=json.loads(job["args"] or "{}"), progress=progress, runtime=runtime)
            result = fn(ctx) or "Done"
            self.db.x("UPDATE jobs SET status='done', progress=1, message=?, finished=?, seconds=?, error='' "
                      "WHERE id=?", (result[:500], now(), round(time.time() - t0, 1), jid))
        except Cancelled:
            self.db.x("UPDATE jobs SET status='cancelled', finished=?, message='Cancelled' WHERE id=?", (now(), jid))
            tasks.on_failed(runtime, job, "Cancelled by the user.")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if is_transient(e) and job["attempts"] + 1 < job["max_attempts"]:
                wait = RETRY_DELAY_S * (job["attempts"] + 1)
                self.db.x("UPDATE jobs SET status='queued', not_before=?, error=?, message=? WHERE id=?",
                          (time.time() + wait, str(e)[:500], f"Temporary problem — retrying in {wait} s", jid))
            else:
                self.db.x("UPDATE jobs SET status='failed', finished=?, error=?, message='Failed', seconds=? "
                          "WHERE id=?", (now(), str(e)[:2000], round(time.time() - t0, 1), jid))
                tasks.on_failed(runtime, job, str(e))
        finally:
            state["stop"] = True
            self.db.x("UPDATE workers SET job_id=NULL WHERE id=?", (worker_id,))
        return True

    def register_worker(self, mode: str) -> str:
        wid = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.db.x("INSERT INTO workers (id, host, pid, mode, started, seen) VALUES (?,?,?,?,?,?)",
                  (wid, socket.gethostname(), os.getpid(), mode, now(), time.time()))
        return wid

    def work_forever(self, runtime, mode: str, stop: Optional[Callable[[], bool]] = None) -> None:
        """The worker loop (worker.py or the inline thread)."""
        try:
            import pythoncom          # Excel automation needs COM initialised on this thread
            pythoncom.CoInitialize()
        except Exception:  # noqa: BLE001
            pass
        wid = self.register_worker(mode)
        last_recover = 0.0
        while not (stop and stop()):
            try:
                if time.time() - last_recover > 30:
                    self.recover()
                    last_recover = time.time()
                self.db.x("UPDATE workers SET seen=? WHERE id=?", (time.time(), wid))
                if not self.run_one(wid, runtime):
                    time.sleep(POLL_S)
            except Exception:  # noqa: BLE001  (never let the loop die)
                traceback.print_exc()
                time.sleep(2)
        try:
            self.db.x("DELETE FROM workers WHERE id=?", (wid,))
        except Exception:  # noqa: BLE001  (database already closed at shutdown)
            pass

    def stop(self, timeout: float = 10) -> None:
        """Stop the inline worker threads after their current job (tests, clean shutdown)."""
        self._stop = True
        for t in self._threads:
            t.join(timeout)

    def start_worker(self, runtime) -> threading.Thread:
        t = threading.Thread(target=self.work_forever, args=(runtime, "inline", lambda: self._stop),
                             name="boq-jobs", daemon=True)
        t.start()
        self._threads.append(t)
        return t

    def worker_status(self) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM workers ORDER BY started")]
