"""Every module that registers tasks or hooks, imported in ONE place.

The web app and worker.py both call load_all(), so a worker never misses a task or an after-analysis hook that the
web app knows about (a job submitted by the web app must be runnable by any worker).
"""
from __future__ import annotations

import importlib

MODULES = [
    "webapp.tasks",          # analysis, export, RFQs, duplicate …
    "webapp.fleet",          # AI fleet: call log, cache, verification, meaning vectors
    "webapp.revisions",      # BOQ revisions: carry-forward, comparison
    "webapp.register",       # RFQ register, pricing sheets, bid comparison
    "webapp.collab",         # comments, notifications, search index, trash purge
    "webapp.decisions",      # special-case decisions remembered across projects
]


def load_all() -> list[str]:
    for m in MODULES:
        importlib.import_module(m)
    return list(MODULES)
