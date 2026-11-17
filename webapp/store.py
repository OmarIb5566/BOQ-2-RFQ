"""Project sessions for the web app: cached loading, one writer at a time, stage + summary for the index.

* A big project's session.json is several MB; it is parsed once and kept in memory until the file changes.
* Every change to a project runs under that project's lock, so two users (or a user and a background job)
  never overwrite each other's work.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from boq_agent.agent import BOQAgent
from boq_agent.config import Settings
from boq_agent.models import Session

STAGES = [
    ("analyzing", "Analyzing"), ("failed", "Failed"), ("decisions", "Decisions needed"),
    ("ready", "Ready to export"), ("exported", "Exported"), ("rfqs", "RFQs drafted"),
]
STAGE_LABEL = dict(STAGES)


class Store:
    def __init__(self, settings: Settings, db, max_cached: int = 12):
        self.settings = settings
        self.db = db
        self._cache: "OrderedDict[str, tuple[float, Session]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self.max_cached = max_cached

    # ------------------------------------------------------------------ paths
    def run_dir(self, run_id: str) -> Path:
        d = (self.settings.workspace / run_id).resolve()
        if self.settings.workspace.resolve() not in d.parents:
            raise ValueError("bad project id")
        return d

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "session.json").exists()

    # ------------------------------------------------------------------ sessions
    def load(self, run_id: str) -> Session:
        p = self.run_dir(run_id) / "session.json"
        mtime = os.path.getmtime(p)
        with self._cache_lock:
            hit = self._cache.get(run_id)
            if hit and hit[0] == mtime:
                self._cache.move_to_end(run_id)
                return hit[1]
        s = Session.model_validate_json(p.read_text(encoding="utf-8"))
        with self._cache_lock:
            self._cache[run_id] = (mtime, s)
            while len(self._cache) > self.max_cached:
                self._cache.popitem(last=False)
        return s

    def forget(self, run_id: str) -> None:
        with self._cache_lock:
            self._cache.pop(run_id, None)

    @contextmanager
    def edit(self, run_id: str, user_id: Optional[int] = None) -> Iterator[tuple[BOQAgent, Session]]:
        """with store.edit(run) as (agent, session): … — the agent saves; the index is refreshed after."""
        lock = self._locks[run_id]
        with lock:
            session = self.load(run_id)
            agent = self.agent()
            try:
                yield agent, session
            except Exception:
                self.forget(run_id)            # the in-memory copy may be half-changed: re-read next time
                raise
            self.saved(run_id, user_id, session)

    def lock(self, run_id: str) -> threading.RLock:
        return self._locks[run_id]

    def saved(self, run_id: str, user_id: Optional[int] = None, session: Optional[Session] = None) -> None:
        """After the engine wrote session.json: refresh the cache entry and the project index.
        The session just saved is kept as the cached copy (no re-parse of a multi-MB file)."""
        p = self.run_dir(run_id) / "session.json"
        if session is not None and p.exists():
            with self._cache_lock:
                self._cache[run_id] = (os.path.getmtime(p), session)
            s = session
        else:
            self.forget(run_id)
            s = self.load(run_id)
        self.db.upsert_project(run_id, stage=stage_of(s), summary=summary_of(s),
                               **({"updated_by": user_id} if user_id else {}))

    def agent(self, progress=None, log=None) -> BOQAgent:
        return BOQAgent(self.settings, log=log or (lambda m: None), progress=progress)


def stage_of(s: Session) -> str:
    if s.rfq.get("files"):
        return "rfqs"
    if s.status == "exported":
        return "exported"
    if any(p.decision == "pending" for p in s.plan.proposals):
        return "decisions"
    return "ready"


def summary_of(s: Session) -> str:
    d = s.document
    n_boq = len(d.files) or 1
    parts = [f"{n_boq} BOQ{'s' if n_boq > 1 else ''}", f"{len(d.items)} items",
             f"{len(s.plan.work_packages)} packages"]
    docs = s.documents
    n_spec = sum(f.kind == "spec" for f in docs.files)
    n_dwg = sum(f.kind == "drawing" for f in docs.files)
    if n_spec:
        parts.append(f"{n_spec} spec file{'s' if n_spec > 1 else ''}")
    if n_dwg:
        parts.append(f"{len(docs.sheets)} sheets")
    return " · ".join(parts)
