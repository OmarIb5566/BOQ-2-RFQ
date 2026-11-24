"""Working together: project members, "who else is here", comments with @mentions, package owners,
the notification bell, search across projects (words and meaning), saved filter views."""
from __future__ import annotations

import time

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from . import collab, perms
from .auth import login_required
from .db import new_id, now

bp = Blueprint("collab_views", __name__)

PRESENCE_S = 45


def cfg(k):
    return current_app.config[k]


def _project(run_id: str) -> dict:
    from .views import _row
    return _row(run_id)


def body() -> dict:
    return request.get_json(silent=True) or {}


def _ok(**kw):
    return jsonify(ok=True, **kw)


def _err(msg, code=400):
    return jsonify(ok=False, error=msg), code


# ---------------------------------------------------------------------- members
@bp.route("/p/<run_id>/members", methods=["GET", "POST"])
@login_required
def members(run_id):
    row = _project(run_id)
    db = cfg("DB")
    if request.method == "POST":
        if not row["can_manage"]:
            abort(403)
        act = request.form.get("action")
        uid = request.form.get("user_id", type=int)
        if act == "add" and uid:
            role = request.form.get("role") if request.form.get("role") in perms.MEMBER_ROLES else "editor"
            db.x("INSERT INTO project_members (run_id, user_id, role, added) VALUES (?,?,?,?) "
                 "ON CONFLICT(run_id, user_id) DO UPDATE SET role=excluded.role", (run_id, uid, role, now()))
            collab.notify(db, [uid], "member", f"{g.user['full_name']} added you to “{row['name']}” as {role}.",
                          url_for("main.project", run_id=run_id), run_id, g.user["id"])
            db.audit("member added", g.user["id"], run_id, request.remote_addr, member=uid, role=role)
            flash("Member saved.", "ok")
        elif act == "remove" and uid:
            db.x("DELETE FROM project_members WHERE run_id=? AND user_id=?", (run_id, uid))
            db.audit("member removed", g.user["id"], run_id, request.remote_addr, member=uid)
            flash("Member removed.", "ok")
        elif act == "owner" and uid:
            db.upsert_project(run_id, owner_id=uid)
            db.audit("owner changed", g.user["id"], run_id, request.remote_addr, owner=uid)
            flash("Owner changed.", "ok")
        return redirect(url_for("collab_views.members", run_id=run_id))
    rows = db.q("SELECT m.*, u.full_name, u.username, u.role grole FROM project_members m JOIN users u ON u.id=m.user_id "
                "WHERE m.run_id=? ORDER BY u.full_name", (run_id,))
    people = db.q("SELECT id, full_name, username, role FROM users WHERE active=1 ORDER BY full_name")
    return render_template("project/members.html", p=row, members=[dict(r) for r in rows],
                           people=[dict(r) for r in people], role_label=perms.ROLE_LABEL)


# ---------------------------------------------------------------------- presence
@bp.route("/api/p/<run_id>/presence", methods=["POST"])
@login_required
def presence(run_id):
    try:
        _project(run_id)
    except Exception:  # noqa: BLE001
        return _err("not found", 404)
    db = cfg("DB")
    page = str(body().get("page", ""))[:40]
    t = time.time()
    db.x("INSERT INTO presence (run_id, user_id, page, seen) VALUES (?,?,?,?) ON CONFLICT(run_id, user_id) "
         "DO UPDATE SET page=excluded.page, seen=excluded.seen", (run_id, g.user["id"], page, t))
    rows = db.q("SELECT p.page, u.full_name, u.id FROM presence p JOIN users u ON u.id=p.user_id WHERE p.run_id=? "
                "AND p.user_id<>? AND p.seen>? ORDER BY u.full_name", (run_id, g.user["id"], t - PRESENCE_S))
    ver = cfg("STORE").version(run_id)
    return _ok(others=[{"name": r["full_name"], "page": r["page"]} for r in rows], version=ver)


# ---------------------------------------------------------------------- comments
def _target_label(run_id: str, target: str) -> str:
    kind, _, key = target.partition(":")
    if kind == "wp":
        try:
            s = cfg("STORE").load(run_id)
            wp = next((w for w in s.plan.work_packages if w.key == key), None)
            return f"{wp.wp_id} {wp.name_en}" if wp else key
        except Exception:  # noqa: BLE001
            return key
    if kind == "rfq":
        r = cfg("DB").one("SELECT number FROM rfqs WHERE id=?", (key,))
        return r["number"] if r else key
    return "the project"


def _target_url(run_id: str, target: str) -> str:
    kind, _, key = target.partition(":")
    if kind == "wp":
        return url_for("main.package", run_id=run_id, key=key) + "#comments"
    if kind == "rfq":
        return url_for("register_views.rfq_page", rfq_id=key)
    return url_for("main.tab", run_id=run_id, tab="activity")


@bp.route("/api/p/<run_id>/comments")
@login_required
def comments(run_id):
    _project(run_id)
    target = request.args.get("target", "project")
    rows = cfg("DB").q("SELECT c.*, u.full_name, u.username FROM comments c JOIN users u ON u.id=c.user_id "
                       "WHERE c.run_id=? AND c.target=? AND c.deleted=0 ORDER BY c.ts", (run_id, target))
    return _ok(comments=[{"id": r["id"], "body": r["body"], "ts": r["ts"], "who": r["full_name"],
                          "mine": r["user_id"] == g.user["id"], "edited": bool(r["edited"])} for r in rows])


@bp.route("/api/p/<run_id>/comments", methods=["POST"])
@login_required
def add_comment(run_id):
    row = _project(run_id)
    b = body()
    db = cfg("DB")
    if b.get("delete"):
        c = db.one("SELECT * FROM comments WHERE id=? AND run_id=?", (b["delete"], run_id))
        if not c or (c["user_id"] != g.user["id"] and not perms.is_admin(g.user)):
            return _err("You can only delete your own comments.", 403)
        db.x("UPDATE comments SET deleted=1 WHERE id=?", (c["id"],))
        return _ok(message="Comment deleted.")
    text = (b.get("body") or "").strip()
    target = str(b.get("target") or "project")[:200]
    if not text:
        return _err("Write something first.")
    if len(text) > 5000:
        return _err("A comment can have at most 5000 characters.")
    cid = new_id()
    db.x("INSERT INTO comments (id, run_id, target, user_id, body, ts) VALUES (?,?,?,?,?,?)",
         (cid, run_id, target, g.user["id"], text, now()))
    where = _target_label(run_id, target)
    url = _target_url(run_id, target)
    told = set()
    for u in collab.mentioned_users(db, text):
        if perms.can_view(db, u, row):
            collab.notify(db, [u["id"]], "mention", f"{g.user['full_name']} mentioned you on {where}: {text[:140]}",
                          url, run_id, g.user["id"])
            told.add(u["id"])
    # people already in the conversation, the package owner and the project owner hear about new comments
    others = {r["user_id"] for r in db.q("SELECT DISTINCT user_id FROM comments WHERE run_id=? AND target=?",
                                         (run_id, target))}
    if target.startswith("wp:"):
        o = db.one("SELECT user_id FROM package_owners WHERE run_id=? AND wp_key=?", (run_id, target[3:]))
        if o:
            others.add(o["user_id"])
    others.add(row["owner_id"])
    collab.notify(db, others - told, "comment", f"{g.user['full_name']} commented on {where}: {text[:140]}", url,
                  run_id, g.user["id"])
    db.audit("comment", g.user["id"], run_id, request.remote_addr, target=target)
    return _ok(message="Comment added.", id=cid, mentioned=len(told))


# ---------------------------------------------------------------------- package owners (assignment)
@bp.route("/api/p/<run_id>/owner", methods=["POST"])
@login_required
def assign_owner(run_id):
    row = _project(run_id)
    if not row["can_edit"]:
        return _err("You can view this project but not change it.", 403)
    b = body()
    db = cfg("DB")
    key, uid = str(b.get("wp_key", "")), b.get("user_id")
    if not uid:
        db.x("DELETE FROM package_owners WHERE run_id=? AND wp_key=?", (run_id, key))
        return _ok(message="Nobody is assigned now.")
    u = db.one("SELECT * FROM users WHERE id=? AND active=1", (int(uid),))
    if not u or not perms.can_edit(db, dict(u), row):
        return _err("That person cannot edit this project — add them as a member first.")
    db.x("INSERT INTO package_owners (run_id, wp_key, user_id, assigned_by, ts) VALUES (?,?,?,?,?) "
         "ON CONFLICT(run_id, wp_key) DO UPDATE SET user_id=excluded.user_id, assigned_by=excluded.assigned_by, "
         "ts=excluded.ts", (run_id, key, u["id"], g.user["id"], now()))
    where = _target_label(run_id, f"wp:{key}")
    collab.notify(db, [u["id"]], "assigned", f"{g.user['full_name']} assigned {where} to you.",
                  _target_url(run_id, f"wp:{key}"), run_id, g.user["id"])
    db.audit("package assigned", g.user["id"], run_id, request.remote_addr, wp=key, to=u["id"])
    return _ok(message=f"Assigned to {u['full_name']}.")


# ---------------------------------------------------------------------- notifications
@bp.route("/api/notifications")
@login_required
def notifications():
    db = cfg("DB")
    rows = db.q("SELECT n.*, u.full_name actor FROM notifications n LEFT JOIN users u ON u.id=n.actor_id "
                "WHERE n.user_id=? ORDER BY n.ts DESC LIMIT 20", (g.user["id"],))
    unread = db.one("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read=0", (g.user["id"],))["n"]
    return _ok(unread=unread, items=[{"id": r["id"], "text": r["text"], "url": r["url"], "ts": r["ts"],
                                      "read": bool(r["read"])} for r in rows])


@bp.route("/api/notifications/read", methods=["POST"])
@login_required
def notifications_read():
    b = body()
    db = cfg("DB")
    if b.get("id"):
        db.x("UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (b["id"], g.user["id"]))
    else:
        db.x("UPDATE notifications SET read=1 WHERE user_id=?", (g.user["id"],))
    return _ok()


# ---------------------------------------------------------------------- search across projects
@bp.route("/search")
@login_required
def search():
    db = cfg("DB")
    q = request.args.get("q", "").strip()[:200]
    mode = "meaning" if request.args.get("mode") == "meaning" else "words"
    vis, args = perms.visible_sql(g.user)
    projects = {r["run_id"]: dict(r) for r in db.q(f"SELECT p.run_id, p.name FROM projects p WHERE p.deleted=0 AND {vis}",
                                                    args)}
    results, note = [], ""
    if q:
        ids = list(projects)
        if mode == "meaning":
            from .fleet import semantic_search
            hits = semantic_search(cfg("RUNTIME"), ids, q)
            if hits is None:
                note = "Search by meaning needs the embedding model on the AI server — showing word matches instead."
                mode = "words"
            else:
                for score, rid, uid in hits:
                    r = db.one("SELECT * FROM search_items WHERE run_id=? AND uid=?", (rid, uid))
                    if r:
                        results.append({**dict(r), "score": round(score, 2)})
        if mode == "words":
            results = collab.text_search(db, ids, q)
    for r in results:
        r["project"] = projects.get(r["run_id"], {}).get("name", r["run_id"])
    return render_template("search.html", q=q, mode=mode, results=results, note=note)


# ---------------------------------------------------------------------- saved filter views
@bp.route("/api/views", methods=["POST"])
@login_required
def save_view():
    b = body()
    db = cfg("DB")
    if b.get("delete"):
        db.x("DELETE FROM saved_views WHERE id=? AND user_id=?", (b["delete"], g.user["id"]))
        return _ok(message="View removed.")
    name = " ".join(str(b.get("name", "")).split())[:60]
    query = str(b.get("query", ""))[:500].lstrip("?")
    page = str(b.get("page", "projects"))[:40]
    if not name:
        return _err("Give the view a name.")
    db.x("INSERT INTO saved_views (id, user_id, page, name, query, ts) VALUES (?,?,?,?,?,?)",
         (new_id(), g.user["id"], page, name, query, now()))
    return _ok(message=f"View “{name}” saved.")


def saved_views(page: str) -> list[dict]:
    return [dict(r) for r in cfg("DB").q("SELECT * FROM saved_views WHERE user_id=? AND page=? ORDER BY name",
                                         (g.user["id"], page))]


# ---------------------------------------------------------------------- remembered special-case decisions
@bp.route("/decisions")
@login_required
def decisions_page():
    from . import decisions
    rows = decisions.overview(cfg("DB"))
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["rule_id"], []).append(r)
    return render_template("decisions.html", groups=groups, enabled=decisions.enabled(cfg("DB")),
                           can_change=g.user["role"] in ("admin", "manager", "engineer"))


@bp.route("/api/decisions", methods=["POST"])
@login_required
def change_decision():
    from . import decisions
    if g.user["role"] not in ("admin", "manager", "engineer"):
        return _err("Your role cannot change remembered decisions.", 403)
    b = body()
    db = cfg("DB")
    key = str(b.get("key", ""))
    if b.get("forget"):
        n = decisions.forget_key(db, key)
        db.audit("decision forgotten", g.user["id"], ip=request.remote_addr, case=key, entries=n)
        return _ok(message="Forgotten — new projects will ask about this case again.")
    if not decisions.set_default(db, g.user["id"], key, b.get("decision")):
        return _err("That decision is no longer remembered — reload the page.", 404)
    db.audit("decision changed", g.user["id"], ip=request.remote_addr, case=key, decision=b.get("decision"))
    return _ok(message="Saved — new projects will use this decision.")
