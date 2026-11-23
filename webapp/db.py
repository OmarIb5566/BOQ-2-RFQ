"""SQLite: users, the project index (who owns what, name, stage) and job history.

The project *content* stays in workspace/<run_id>/session.json (the engine's format); this database is only
the index that makes listing, searching, sharing and access control fast for many users.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    full_name TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',          -- admin | user
    active INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id INTEGER REFERENCES users(id),
    shared INTEGER NOT NULL DEFAULT 1,          -- 1 = the whole team can open and edit it
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'analyzing',    -- analyzing | failed | decisions | ready | exported | rfqs
    summary TEXT NOT NULL DEFAULT '',           -- "3 BOQs · 412 items · 18 packages"
    error TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0,
    updated_by INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    user_id INTEGER,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,                       -- queued | running | done | failed | interrupted
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    ip TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_user ON login_failures(username, ts);
CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures(ip, ts);
"""

# columns added after the first release: (table, column, definition) — added to existing databases on start-up
ADDED_COLUMNS = [
    ("users", "ses_ver", "INTEGER NOT NULL DEFAULT 0"),     # bumped on password change -> other sessions end
]


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.conn() as c:
            c.executescript(SCHEMA)
            for table, col, definition in ADDED_COLUMNS:
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.c = c
        return c

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> int:
        cur = self.conn().execute(sql, args)
        return cur.lastrowid

    # ------------------------------------------------------------------ projects
    def upsert_project(self, run_id: str, **fields: Any) -> None:
        fields.setdefault("updated", now())
        if self.one("SELECT 1 FROM projects WHERE run_id=?", (run_id,)):
            sets = ", ".join(f"{k}=?" for k in fields)
            self.x(f"UPDATE projects SET {sets} WHERE run_id=?", (*fields.values(), run_id))
        else:
            fields.setdefault("created", fields["updated"])
            fields.setdefault("name", run_id)
            cols = ", ".join(["run_id", *fields])
            marks = ", ".join("?" * (len(fields) + 1))
            self.x(f"INSERT INTO projects ({cols}) VALUES ({marks})", (run_id, *fields.values()))
