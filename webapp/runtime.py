"""What the web app and the worker processes share: settings, database, project store, job queue.

    rt = build_runtime()          # same folders, same database, same task code in every process
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from boq_agent.config import Settings

from .db import DB
from .jobs import Jobs
from .store import Store


@dataclass
class Runtime:
    settings: Settings
    db: DB
    store: Store
    jobs: Jobs
    instance_path: Path


def instance_dir(settings: Settings) -> Path:
    """instance/ (database, secret key) sits next to the workspace unless BOQ_INSTANCE says otherwise."""
    return Path(os.getenv("BOQ_INSTANCE") or Path(settings.workspace).resolve().parent / "instance")


def build_runtime(settings: Optional[Settings] = None) -> Runtime:
    settings = settings or Settings()
    settings.ensure_dirs()
    inst = instance_dir(settings)
    inst.mkdir(parents=True, exist_ok=True)
    db = DB(inst / "boq2rfq.db")
    store = Store(settings, db)
    jobs = Jobs(db)
    from . import extensions
    extensions.load_all()                       # the same task / hook modules in the web app and the workers
    from .collab import index_search
    store.on_saved.append(index_search)
    return Runtime(settings=settings, db=db, store=store, jobs=jobs, instance_path=inst)


def worker_mode() -> str:
    """inline = a worker thread inside the web server (one PC); external = run worker.py as its own service."""
    m = os.getenv("BOQ_WORKER", "inline").strip().lower()
    return m if m in ("inline", "external") else "inline"
