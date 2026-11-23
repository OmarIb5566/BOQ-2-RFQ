"""Revisions & history: the chain of revisions of a project, its uploaded inputs, uploading a revised BOQ,
and the comparison between two revisions."""
from __future__ import annotations

import json

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import perms
from .auth import login_required
from .revisions import compare

bp = Blueprint("revisions_views", __name__)


def cfg(k):
    return current_app.config[k]


def chain(run_id: str) -> list[dict]:
    """All revisions of the project family, oldest first (only those the user may see)."""
    db = cfg("DB")
    root = run_id
    seen = set()
    while True:
        r = db.one("SELECT parent_run FROM projects WHERE run_id=?", (root,))
        if not r or not r["parent_run"] or r["parent_run"] in seen:
            break
        seen.add(root)
        root = r["parent_run"]
    out, todo = [], [root]
    while todo:
        rid = todo.pop(0)
        r = db.one("SELECT p.*, u.full_name owner_name FROM projects p LEFT JOIN users u ON u.id=p.owner_id "
                   "WHERE p.run_id=?", (rid,))
        if r and perms.can_view(db, g.user, dict(r)):
            out.append(dict(r))
        todo += [c["run_id"] for c in db.q("SELECT run_id FROM projects WHERE parent_run=? ORDER BY created", (rid,))]
    return sorted(out, key=lambda x: (x["revision"], x["created"]))


@bp.route("/p/<run_id>/revisions", methods=["GET", "POST"])
@login_required
def revisions(run_id):
    from .views import _row
    row = _row(run_id)
    db = cfg("DB")
    if request.method == "POST":
        if not row["can_manage"]:
            abort(403)
        st = json.loads(row.get("settings") or "{}")
        st["use_ai"] = request.form.get("use_ai") == "on"
        db.upsert_project(run_id, settings=json.dumps(st))
        flash("Project settings saved.", "ok")
        return redirect(url_for("revisions_views.revisions", run_id=run_id))
    revs = chain(run_id)
    inputs = {r["run_id"]: [dict(x) for x in db.q(
        "SELECT u.*, us.full_name who FROM uploads u LEFT JOIN users us ON us.id=u.user_id WHERE u.run_id=? "
        "ORDER BY u.ts", (r["run_id"],))] for r in revs}
    return render_template("project/revisions.html", p=row, revs=revs, inputs=inputs,
                           settings=json.loads(row.get("settings") or "{}"))


@bp.route("/p/<run_id>/revise", methods=["GET", "POST"])
@login_required
def upload_revision(run_id):
    from .views import _row, check_uploads, create_from_upload
    row = _row(run_id)
    if not row["can_edit"] or not perms.can_create(g.user):
        abort(403)
    if not cfg("STORE").exists(run_id):
        flash("Only an analysed project can get a revision.", "error")
        return redirect(url_for("main.project", run_id=run_id))
    if request.method == "GET":
        from boq_agent.ingest.intake import UPLOAD_EXT
        return render_template("project/revise.html", p=row, exts=sorted(UPLOAD_EXT | {".zip"}))
    files = [f for f in request.files.getlist("files") if f and f.filename]
    err = check_uploads(files)
    if err:
        flash(err, "error")
        return redirect(url_for("revisions_views.upload_revision", run_id=run_id))
    st = json.loads(row.get("settings") or "{}")
    base = row["name"].split(" (Rev ")[0]
    name = f"{base} (Rev {(row['revision'] or 0) + 1})"
    new = create_from_upload(files, name, use_ai=request.form.get("use_ai", "on" if st.get("use_ai", True) else "")
                             == "on", parent=row)
    st["superseded_by"] = new
    cfg("DB").upsert_project(run_id, settings=json.dumps(st))
    return redirect(url_for("main.project", run_id=new))


@bp.route("/p/<run_id>/compare")
@login_required
def compare_view(run_id):
    from .views import _row
    row = _row(run_id)
    db, store = cfg("DB"), cfg("STORE")
    other = request.args.get("with") or row.get("parent_run")
    if not other:
        flash("This project has no earlier revision to compare with.", "error")
        return redirect(url_for("revisions_views.revisions", run_id=run_id))
    orow = _row(other)
    if not (store.exists(run_id) and store.exists(other)):
        flash("Both revisions must be analysed first.", "error")
        return redirect(url_for("revisions_views.revisions", run_id=run_id))
    issued = {r["wp_key"] for r in db.q("SELECT wp_key FROM rfqs WHERE run_id=? AND deleted=0 AND status IN "
                                       "('issued','clarification','quoted','evaluated','awarded')", (other,))}
    res = compare(store.load(other), store.load(run_id), issued)
    show = request.args.get("show", "changes")
    rows = [r for r in res["items"] if show == "all" or r["status"] != "unchanged"]
    return render_template("project/compare.html", p=row, o=orow, res=res, rows=rows[:2000], show=show)
