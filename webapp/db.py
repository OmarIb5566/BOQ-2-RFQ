"""SQLite: users, the project index, jobs, and the collaboration / workflow tables.

The project *content* stays in workspace/<run_id>/session.json (the engine's format); this database is the index
plus everything shared between users (jobs, RFQ register, comments, audit log …).

Schema changes are numbered MIGRATIONS, applied in order on start-up and recorded in schema_version, so an
existing instance/boq2rfq.db upgrades itself without losing users or projects. Rules for new migrations:
text ids for new tables, no triggers or stored procedures, JSON stored as TEXT, file paths relative to the
workspace. Never edit a released migration — add a new one.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    full_name TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id INTEGER REFERENCES users(id),
    shared INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'analyzing',
    summary TEXT NOT NULL DEFAULT '',
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
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_run ON jobs(run_id);
"""


def _m1(c):          # Phase 0: sign-in throttling, session version
    c.execute("""CREATE TABLE IF NOT EXISTS login_failures (id INTEGER PRIMARY KEY AUTOINCREMENT,
                 username TEXT NOT NULL, ip TEXT NOT NULL, ts REAL NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS login_failures_user ON login_failures(username, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS login_failures_ip ON login_failures(ip, ts)")
    add_column(c, "users", "ses_ver", "INTEGER NOT NULL DEFAULT 0")


def _m2(c):          # Phase 1: database job queue + separate worker processes
    for col, d in (("args", "TEXT NOT NULL DEFAULT '{}'"), ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                   ("max_attempts", "INTEGER NOT NULL DEFAULT 3"), ("heartbeat", "REAL"), ("worker", "TEXT"),
                   ("not_before", "REAL NOT NULL DEFAULT 0"), ("cancel", "INTEGER NOT NULL DEFAULT 0"),
                   ("seconds", "REAL")):
        add_column(c, "jobs", col, d)
    c.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created)")
    c.execute("""CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, host TEXT NOT NULL, pid INTEGER NOT NULL,
                 mode TEXT NOT NULL, started TEXT NOT NULL, seen REAL NOT NULL, job_id TEXT)""")


def _m3(c):          # Phase 1: roles, project membership, optimistic locking, revisions, trash dates, language
    c.execute("UPDATE users SET role='engineer' WHERE role='user'")
    add_column(c, "users", "lang", "TEXT NOT NULL DEFAULT ''")
    add_column(c, "users", "tour_done", "INTEGER NOT NULL DEFAULT 0")
    add_column(c, "users", "seen_version", "TEXT NOT NULL DEFAULT ''")
    for col, d in (("version", "INTEGER NOT NULL DEFAULT 0"), ("deleted_at", "TEXT"), ("parent_run", "TEXT"),
                   ("revision", "INTEGER NOT NULL DEFAULT 0"), ("settings", "TEXT NOT NULL DEFAULT '{}'")):
        add_column(c, "projects", col, d)
    c.execute("""CREATE TABLE IF NOT EXISTS project_members (run_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                 role TEXT NOT NULL, added TEXT NOT NULL, PRIMARY KEY (run_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER,
                 run_id TEXT, action TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', ip TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS audit_run ON audit(run_id, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS presence (run_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                 page TEXT NOT NULL DEFAULT '', seen REAL NOT NULL, PRIMARY KEY (run_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL,
                 updated TEXT NOT NULL, updated_by INTEGER)""")


def _m4(c):          # Phase 2: AI fleet — call log, memory of user corrections, meaning vectors
    c.execute("""CREATE TABLE IF NOT EXISTS llm_calls (id TEXT PRIMARY KEY, ts TEXT NOT NULL, job_id TEXT,
                 run_id TEXT, role TEXT NOT NULL, model TEXT NOT NULL, op TEXT NOT NULL DEFAULT '',
                 tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
                 seconds REAL NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1,
                 error TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS corrections (id TEXT PRIMARY KEY, ts TEXT NOT NULL, user_id INTEGER,
                 run_id TEXT, kind TEXT NOT NULL, text TEXT NOT NULL, norm TEXT NOT NULL, value TEXT NOT NULL,
                 vector BLOB)""")
    c.execute("CREATE INDEX IF NOT EXISTS corrections_norm ON corrections(kind, norm)")
    c.execute("""CREATE TABLE IF NOT EXISTS item_vectors (run_id TEXT NOT NULL, uid TEXT NOT NULL,
                 model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY (run_id, uid))""")


def _m5(c):          # Phase 3: uploads (duplicate detection, input history) + revisions
    c.execute("""CREATE TABLE IF NOT EXISTS uploads (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                 name TEXT NOT NULL, size INTEGER NOT NULL, user_id INTEGER, ts TEXT NOT NULL,
                 revision INTEGER NOT NULL DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS uploads_sha ON uploads(sha256)")
    c.execute("CREATE INDEX IF NOT EXISTS uploads_run ON uploads(run_id)")


def _m6(c):          # Phase 4: the RFQ register
    c.execute("""CREATE TABLE IF NOT EXISTS rfqs (id TEXT PRIMARY KEY, number TEXT UNIQUE NOT NULL,
                 run_id TEXT NOT NULL, wp_key TEXT NOT NULL, wp_id TEXT NOT NULL DEFAULT '',
                 package TEXT NOT NULL DEFAULT '', supplier TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                 due_date TEXT, issued_at TEXT, letter TEXT NOT NULL DEFAULT '', quote_file TEXT NOT NULL DEFAULT '',
                 quote_amount REAL, currency TEXT NOT NULL DEFAULT 'EGP', notes TEXT NOT NULL DEFAULT '',
                 assigned_to INTEGER, created_by INTEGER, created TEXT NOT NULL, updated TEXT NOT NULL,
                 version INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS rfqs_run ON rfqs(run_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS rfq_events (id TEXT PRIMARY KEY, rfq_id TEXT NOT NULL, ts TEXT NOT NULL,
                 user_id INTEGER, action TEXT NOT NULL, from_status TEXT, to_status TEXT,
                 note TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS rfq_events_rfq ON rfq_events(rfq_id, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)""")


def _m7(c):          # Phase 5: comments, notifications, saved views, package owners, full-text search
    c.execute("""CREATE TABLE IF NOT EXISTS comments (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, target TEXT NOT NULL,
                 user_id INTEGER NOT NULL, body TEXT NOT NULL, ts TEXT NOT NULL, edited TEXT,
                 deleted INTEGER NOT NULL DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS comments_target ON comments(run_id, target, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                 ts TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
                 run_id TEXT, actor_id INTEGER, read INTEGER NOT NULL DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, read, ts)")
    c.execute("""CREATE TABLE IF NOT EXISTS saved_views (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                 page TEXT NOT NULL, name TEXT NOT NULL, query TEXT NOT NULL, ts TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS package_owners (run_id TEXT NOT NULL, wp_key TEXT NOT NULL,
                 user_id INTEGER NOT NULL, assigned_by INTEGER, ts TEXT NOT NULL, PRIMARY KEY (run_id, wp_key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS search_items (run_id TEXT NOT NULL, uid TEXT NOT NULL,
                 item_no TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '',
                 qty TEXT NOT NULL DEFAULT '', package TEXT NOT NULL DEFAULT '', wp_key TEXT NOT NULL DEFAULT '',
                 PRIMARY KEY (run_id, uid))""")
    if fts5_available(c):
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(run_id UNINDEXED, uid UNINDEXED,
                     item_no, text, package, tokenize='unicode61 remove_diacritics 2')""")


MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "sign-in throttling and session version", _m1),
    (2, "database job queue and workers", _m2),
    (3, "roles, membership, locking, audit, presence, settings", _m3),
    (4, "AI call log, corrections memory, item vectors", _m4),
    (5, "uploads and revisions", _m5),
    (6, "RFQ register", _m6),
    (7, "comments, notifications, saved views, search", _m7),
]
LATEST = MIGRATIONS[-1][0]


def add_column(c, table: str, col: str, definition: str) -> None:
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if col not in have:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")


def fts5_available(c) -> bool:
    try:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        c.execute("DROP TABLE _fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def now() -> str:
    """Server-local time, seconds precision (all stored timestamps use this one clock)."""
    return dt.datetime.now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    def migrate(self) -> list[int]:
        """Apply the migrations this database does not have yet (each in its own transaction)."""
        c = self.conn()
        c.executescript(BASE_SCHEMA)
        c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                  "applied TEXT NOT NULL)")
        done = {r[0] for r in c.execute("SELECT version FROM schema_version")}
        applied = []
        for ver, name, fn in MIGRATIONS:
            if ver in done:
                continue
            c.execute("BEGIN IMMEDIATE")
            try:
                fn(c)
                c.execute("INSERT INTO schema_version (version, name, applied) VALUES (?,?,?)", (ver, name, now()))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
            applied.append(ver)
        return applied

    def version(self) -> int:
        r = self.one("SELECT MAX(version) v FROM schema_version")
        return int(r["v"] or 0)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.c = c
        return c

    def close(self) -> None:
        """Close this thread's connection (backup / restore need the file closed on Windows)."""
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
            self._local.c = None

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> int:
        cur = self.conn().execute(sql, args)
        return cur.lastrowid

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """An immediate (write-locking) transaction: for read-then-write steps two processes must not interleave."""
        c = self.conn()
        for attempt in range(50):
            try:
                c.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.1)
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

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

    # ------------------------------------------------------------------ settings set by administrators
    def setting(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM app_settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value: Any, user_id: Optional[int] = None) -> None:
        self.x("INSERT INTO app_settings (key, value, updated, updated_by) VALUES (?,?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated, "
               "updated_by=excluded.updated_by", (key, json.dumps(value, ensure_ascii=False), now(), user_id))

    # ------------------------------------------------------------------ audit log
    def audit(self, action: str, user_id: Optional[int] = None, run_id: Optional[str] = None,
              ip: Optional[str] = None, **detail: Any) -> None:
        self.x("INSERT INTO audit (id, ts, user_id, run_id, action, detail, ip) VALUES (?,?,?,?,?,?,?)",
               (new_id(), now(), user_id, run_id, action, json.dumps(detail, ensure_ascii=False, default=str), ip))

    def next_number(self, name: str) -> int:
        with self.tx() as c:
            r = c.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            v = (r[0] if r else 0) + 1
            c.execute("INSERT INTO counters (name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=?",
                      (name, v, v))
        return v
