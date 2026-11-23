"""Collaboration helpers (no Flask): search index, notifications, @mentions, trash purge.

The search index (search_items + search_fts) is refreshed every time a project is saved (Store.on_saved), so search
results always show the current package of an item.
"""
from __future__ import annotations

import re
import shutil
import time
from typing import Iterable, Optional

from .db import new_id, now

MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9._\-@]{2,40})")


# ---------------------------------------------------------------------- full-text search index
def has_fts(db) -> bool:
    return bool(db.one("SELECT 1 FROM sqlite_master WHERE name='search_fts'"))


def index_search(runtime_or_db, run_id: str, session) -> int:
    db = getattr(runtime_or_db, "db", runtime_or_db)
    items = session.document.items
    wp_of = {}
    for wp in session.plan.work_packages:
        for u in wp.item_uids:
            wp_of[u] = wp
    rows = []
    for it in items:
        wp = wp_of.get(it.uid)
        rows.append((run_id, it.uid, it.item_no or "", it.full_description[:4000], it.unit or "", str(it.quantity or ""),
                     f"{wp.wp_id} {wp.name_en}" if wp else "", wp.key if wp else ""))
    fts = has_fts(db)
    with db.tx() as c:
        c.execute("DELETE FROM search_items WHERE run_id=?", (run_id,))
        c.executemany("INSERT INTO search_items (run_id, uid, item_no, text, unit, qty, package, wp_key) "
                      "VALUES (?,?,?,?,?,?,?,?)", rows)
        if fts:
            c.execute("DELETE FROM search_fts WHERE run_id=?", (run_id,))
            c.executemany("INSERT INTO search_fts (run_id, uid, item_no, text, package) VALUES (?,?,?,?,?)",
                          [(r[0], r[1], r[2], r[3], r[6]) for r in rows])
    return len(rows)


def drop_index(db, run_id: str) -> None:
    with db.tx() as c:
        for t in ("search_items", "item_vectors"):
            c.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))
        if has_fts(db):
            c.execute("DELETE FROM search_fts WHERE run_id=?", (run_id,))


def fts_query(q: str) -> str:
    """User words -> a safe FTS5 query (every word must appear; prefix match on the last word)."""
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    if not words:
        return ""
    parts = [f'"{w}"' for w in words[:-1]] + [f'"{words[-1]}"*']
    return " ".join(parts)


def text_search(db, run_ids: list[str], q: str, limit: int = 60) -> list[dict]:
    if not run_ids or not q.strip():
        return []
    marks = ",".join("?" * len(run_ids))
    if has_fts(db) and fts_query(q):
        try:
            rows = db.q(f"SELECT s.* FROM search_fts f JOIN search_items s ON s.run_id=f.run_id AND s.uid=f.uid "
                        f"WHERE search_fts MATCH ? AND f.run_id IN ({marks}) ORDER BY rank LIMIT ?",
                        (fts_query(q), *run_ids, limit))
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001  (fall back to LIKE)
            pass
    like = [f"%{w}%" for w in q.split()[:6]]
    cond = " AND ".join("(text LIKE ? OR item_no LIKE ? OR package LIKE ?)" for _ in like)
    args = [a for w in like for a in (w, w, w)]
    return [dict(r) for r in db.q(f"SELECT * FROM search_items WHERE run_id IN ({marks}) AND {cond} LIMIT ?",
                                  (*run_ids, *args, limit))]


# ---------------------------------------------------------------------- notifications
def notify(db, user_ids: Iterable[int], kind: str, text: str, url: str = "", run_id: Optional[str] = None,
           actor_id: Optional[int] = None) -> int:
    n = 0
    for uid in dict.fromkeys(u for u in user_ids if u and u != actor_id):
        db.x("INSERT INTO notifications (id, user_id, ts, kind, text, url, run_id, actor_id) VALUES (?,?,?,?,?,?,?,?)",
             (new_id(), uid, now(), kind, text[:500], url, run_id, actor_id))
        n += 1
    return n


def mentioned_users(db, body: str) -> list[dict]:
    names = {m.lower() for m in MENTION.findall(body or "")}
    if not names:
        return []
    marks = ",".join("?" * len(names))
    return [dict(r) for r in db.q(f"SELECT id, username, full_name, role, active FROM users WHERE active=1 AND "
                                  f"lower(username) IN ({marks})", tuple(names))]


# ---------------------------------------------------------------------- trash
def purge_trash(runtime, days: int) -> int:
    """Projects in the trash for more than `days` days are deleted for good (files + database rows)."""
    db, store = runtime.db, runtime.store
    import datetime as dt
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.q("SELECT run_id FROM projects WHERE deleted=1 AND deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
    n = 0
    for r in rows:
        rid = r["run_id"]
        try:
            d = store.run_dir(rid)
        except ValueError:
            continue
        shutil.rmtree(d, ignore_errors=True)
        store.forget(rid)
        drop_index(db, rid)
        with db.tx() as c:
            for t in ("projects", "project_members", "presence", "package_owners", "comments", "uploads"):
                c.execute(f"DELETE FROM {t} WHERE run_id=?", (rid,))
            c.execute("UPDATE rfqs SET deleted=1 WHERE run_id=?", (rid,))
        db.audit("project purged", None, rid, days=days)
        n += 1
    return n


def maybe_schedule_purge(runtime, days: int) -> None:
    """Queue the daily trash purge (at most once a day, whoever starts first)."""
    db = runtime.db
    last = db.setting("last_purge", 0) or 0
    if time.time() - float(last) < 86400:
        return
    db.set_setting("last_purge", time.time())
    runtime.jobs.submit("purge-trash", "Emptying old trash", "", None, {"days": days})
