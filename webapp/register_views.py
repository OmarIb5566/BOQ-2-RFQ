"""RFQ register screens: the list (filters, overdue, export / import), one RFQ (status moves, quote, history,
comments) and the bid comparison of a work package."""
from __future__ import annotations

import tempfile
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file,
                   url_for)

from . import perms
from . import register as rfqreg
from .auth import login_required
from .db import now

bp = Blueprint("register_views", __name__)


def cfg(k):
    return current_app.config[k]


def _visible_rfqs(where: list[str], args: list) -> list[dict]:
    db = cfg("DB")
    vis, vargs = perms.visible_sql(g.user)
    sql = ("SELECT r.*, p.name project, u.full_name assigned FROM rfqs r LEFT JOIN projects p ON p.run_id=r.run_id "
           "LEFT JOIN users u ON u.id=r.assigned_to WHERE r.deleted=0 AND (r.run_id='' OR (p.deleted=0 AND "
           + vis + "))" + "".join(f" AND {w}" for w in where) + " ORDER BY r.updated DESC LIMIT 2000")
    rows = [dict(r) for r in db.q(sql, (*vargs, *args))]
    for r in rows:
        r["overdue"] = rfqreg.is_overdue(r)
    return rows


def _filters() -> tuple[list[str], list]:
    where, args = [], []
    a = request.args
    if a.get("run"):
        where.append("r.run_id=?")
        args.append(a["run"])
    if a.get("status"):
        where.append("r.status=?")
        args.append(a["status"])
    if a.get("supplier"):
        where.append("r.supplier LIKE ?")
        args.append(f"%{a['supplier']}%")
    if a.get("q"):
        where.append("(r.number LIKE ? OR r.package LIKE ? OR r.supplier LIKE ? OR p.name LIKE ?)")
        args += [f"%{a['q']}%"] * 4
    if a.get("mine"):
        where.append("(r.assigned_to=? OR r.created_by=?)")
        args += [g.user["id"]] * 2
    if a.get("open"):
        where.append("r.status IN ('draft','approved','issued','clarification','quoted','evaluated')")
    return where, args


def _rfq(rfq_id: str) -> tuple[dict, dict | None]:
    db = cfg("DB")
    r = db.one("SELECT r.*, p.name project, u.full_name assigned FROM rfqs r LEFT JOIN projects p ON "
               "p.run_id=r.run_id LEFT JOIN users u ON u.id=r.assigned_to WHERE r.id=? AND r.deleted=0", (rfq_id,))
    if r is None:
        abort(404)
    r = dict(r)
    row = None
    if r["run_id"]:
        p = db.one("SELECT * FROM projects WHERE run_id=?", (r["run_id"],))
        if p is None or not perms.can_view(db, g.user, dict(p)):
            abort(403)
        row = dict(p)
        r["can_edit"] = perms.can_edit(db, g.user, row)
    else:
        r["can_edit"] = g.user["role"] in ("admin", "manager", "engineer")
    r["overdue"] = rfqreg.is_overdue(r)
    return r, row


@bp.route("/register")
@login_required
def register():
    return register_list()


def register_list():
    where, args = _filters()
    if request.args.get("overdue"):
        rows = [r for r in _visible_rfqs(where, args) if r["overdue"]]
    else:
        rows = _visible_rfqs(where, args)
    fmt = request.args.get("format")
    if fmt in ("xlsx", "pdf"):
        tmp = Path(tempfile.mkdtemp(prefix="boq-reg-")) / f"RFQ register {now()[:10]}.{fmt}"
        if fmt == "xlsx":
            rfqreg.export_xlsx(rows, tmp)
        else:
            rfqreg.export_pdf(rows, tmp, "RFQ register")
        cfg("DB").audit("register exported", g.user["id"], ip=request.remote_addr, format=fmt, rows=len(rows))
        return send_file(tmp, as_attachment=True, download_name=tmp.name)
    from collections import Counter
    counts = Counter(r["status"] for r in rows)
    projects = {}
    if request.args.get("run"):
        p = cfg("DB").one("SELECT name FROM projects WHERE run_id=?", (request.args["run"],))
        projects["current"] = p["name"] if p else request.args["run"]
    from .collab_views import saved_views
    return render_template("register/list.html", rows=rows, counts=counts, statuses=rfqreg.STATUSES,
                           label=rfqreg.STATUS_LABEL, args=request.args, views=saved_views("register"),
                           n_overdue=sum(1 for r in rows if r["overdue"]), projects=projects, today=rfqreg.today())


@bp.route("/register/<rfq_id>")
@login_required
def rfq_page(rfq_id):
    r, row = _rfq(rfq_id)
    db = cfg("DB")
    events = [dict(e) for e in db.q("SELECT e.*, u.full_name who FROM rfq_events e LEFT JOIN users u ON "
                                    "u.id=e.user_id WHERE e.rfq_id=? ORDER BY e.ts DESC", (rfq_id,))]
    moves = register_moves(r)
    people = [dict(u) for u in db.q("SELECT id, full_name FROM users WHERE active=1 AND role<>'viewer' "
                                    "ORDER BY full_name")]
    pricing = None
    if row and cfg("STORE").exists(r["run_id"]):
        s = cfg("STORE").load(r["run_id"])
        pricing = rfqreg.pricing_sheet_for(s, cfg("STORE").run_dir(r["run_id"]), r["wp_key"])
    quote = rfqreg.load_quote(r)
    return render_template("register/rfq.html", r=r, p=row, events=events, moves=moves, people=people,
                           label=rfqreg.STATUS_LABEL, pricing=pricing, quote=quote)


def register_moves(r: dict) -> list[str]:
    return perms.rfq_moves(g.user, r["status"]) if r["can_edit"] else []


@bp.route("/api/rfq/<rfq_id>", methods=["POST"])
@login_required
def rfq_action(rfq_id):
    r, row = _rfq(rfq_id)
    db = cfg("DB")
    if not r["can_edit"]:
        return jsonify(ok=False, error="You can view this RFQ but not change it."), 403
    sent = (request.headers.get("X-Version") or request.form.get("version")
            or str((request.get_json(silent=True) or {}).get("version", "")))
    if sent and sent.isdigit() and int(sent) != r["version"]:
        return jsonify(ok=False, conflict=True, error="Someone else changed this RFQ meanwhile — reload to see it."), 409
    if request.files.get("quote"):
        amt = request.form.get("amount", "").replace(",", "").strip()
        try:
            amount = float(amt) if amt else None
        except ValueError:
            return jsonify(ok=False, error="The amount must be a number."), 400
        try:
            msg = rfqreg.save_quote(db, cfg("STORE"), r, request.files["quote"], amount, g.user)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        db.audit("quote recorded", g.user["id"], r["run_id"] or None, request.remote_addr, rfq=r["number"])
        return jsonify(ok=True, message=msg)
    b = request.get_json(silent=True) or {}
    if b.get("to"):
        err = rfqreg.move(db, r, b["to"], g.user, str(b.get("note", ""))[:1000])
        if err:
            return jsonify(ok=False, error=err), 400
        db.audit("rfq status", g.user["id"], r["run_id"] or None, request.remote_addr, rfq=r["number"],
                 to=b["to"])
        if r.get("created_by") and r["created_by"] != g.user["id"]:
            from .collab import notify
            notify(db, [r["created_by"], r.get("assigned_to")], "rfq",
                   f"{g.user['full_name']} moved {r['number']} ({r['supplier']}) to "
                   f"{rfqreg.STATUS_LABEL[b['to']]}.", url_for("register_views.rfq_page", rfq_id=rfq_id),
                   r["run_id"] or None, g.user["id"])
        return jsonify(ok=True, message=f"{r['number']} → {rfqreg.STATUS_LABEL[b['to']]}")
    fields = {}
    if "due_date" in b:
        fields["due_date"] = rfqreg.parse_date(b["due_date"])
    if "supplier" in b:
        fields["supplier"] = str(b["supplier"])[:200]
    if "notes" in b:
        fields["notes"] = str(b["notes"])[:2000]
    if "assigned_to" in b:
        fields["assigned_to"] = int(b["assigned_to"]) if str(b["assigned_to"]).isdigit() else None
    if "quote_amount" in b:
        try:
            fields["quote_amount"] = float(str(b["quote_amount"]).replace(",", "")) if b["quote_amount"] not in (
                "", None) else None
        except ValueError:
            return jsonify(ok=False, error="The amount must be a number."), 400
    if not fields:
        return jsonify(ok=False, error="Nothing to change."), 400
    sets = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE rfqs SET {sets}, updated=?, version=version+1 WHERE id=?", (*fields.values(), now(), rfq_id))
    rfqreg.event(db, rfq_id, g.user["id"], "edited", None, None, ", ".join(f"{k}: {v}" for k, v in fields.items()))
    if fields.get("assigned_to"):
        from .collab import notify
        notify(db, [fields["assigned_to"]], "assigned", f"{g.user['full_name']} assigned {r['number']} to you.",
               url_for("register_views.rfq_page", rfq_id=rfq_id), r["run_id"] or None, g.user["id"])
    return jsonify(ok=True, message="Saved.")


@bp.route("/register/<rfq_id>/file/<kind>")
@login_required
def rfq_file(rfq_id, kind):
    r, row = _rfq(rfq_id)
    store = cfg("STORE")
    if kind == "letter":
        p = Path(r["letter"]) if r["letter"] else None
    elif kind == "quote":
        p = Path(r["quote_file"]) if r["quote_file"] else None
    elif kind == "pricing" and row:
        p = rfqreg.pricing_sheet_for(store.load(r["run_id"]), store.run_dir(r["run_id"]), r["wp_key"])
    else:
        abort(404)
    if not p or not p.is_file():
        abort(404)
    allowed = [store.settings.workspace.resolve()]
    if not any(a in p.resolve().parents for a in allowed):
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)


@bp.route("/register/compare/<run_id>/<path:wp_key>")
@login_required
def compare(run_id, wp_key):
    from .views import _row
    row = _row(run_id)
    try:
        cmp = rfqreg.comparison(cfg("DB"), cfg("STORE"), run_id, wp_key)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("register_views.register", run=run_id))
    if request.args.get("format") == "xlsx":
        out = Path(tempfile.mkdtemp(prefix="boq-cmp-")) / f"Bid comparison - {cmp['wp'].wp_id} {cmp['wp'].name_en[:60]}.xlsx"
        rfqreg.comparison_xlsx(cmp, out, row["name"])
        cfg("DB").audit("bid comparison", g.user["id"], run_id, request.remote_addr, wp=wp_key)
        return send_file(out, as_attachment=True, download_name=out.name)
    return render_template("register/compare.html", p=row, cmp=cmp)


@bp.route("/register/new", methods=["POST"])
@login_required
def new_rfq():
    if g.user["role"] == "viewer":
        abort(403)
    from .views import _row
    run_id = request.form.get("run_id", "")
    row = _row(run_id)
    if not row["can_edit"]:
        abort(403)
    s = cfg("STORE").load(run_id)
    wp = next((w for w in s.plan.work_packages if w.key == request.form.get("wp_key")), None)
    if wp is None:
        flash("Choose a work package.", "error")
        return redirect(url_for("register_views.register", run=run_id))
    rid = rfqreg.create(cfg("DB"), run_id, wp.key, wp.wp_id, wp.name_en, request.form.get("supplier", "").strip(),
                          g.user["id"], due_date=rfqreg.parse_date(request.form.get("due_date")))
    return redirect(url_for("register_views.rfq_page", rfq_id=rid))


@bp.route("/register/import", methods=["POST"])
@login_required
def import_register():
    if g.user["role"] not in ("admin", "manager", "engineer"):
        abort(403)
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        flash("Choose the register as an Excel workbook (.xlsx).", "error")
        return redirect(url_for("register_views.register"))
    from boq_agent.ingest.sniff import check_head
    head = f.stream.read(2048)
    f.stream.seek(0)
    if check_head(head, "register.xlsx"):
        flash("That file is not a real Excel workbook.", "error")
        return redirect(url_for("register_views.register"))
    tmp = Path(tempfile.mkdtemp(prefix="boq-imp-")) / "register.xlsx"
    f.save(tmp)
    db = cfg("DB")
    vis, vargs = perms.visible_sql(g.user)
    names = {r["name"].strip().lower(): r["run_id"] for r in db.q(f"SELECT p.run_id, p.name FROM projects p WHERE "
                                                                  f"p.deleted=0 AND {vis}", vargs)}
    try:
        n, problems = rfqreg.import_xlsx(db, tmp, g.user, names)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("register_views.register"))
    db.audit("register imported", g.user["id"], ip=request.remote_addr, created=n)
    flash(f"{n} RFQ(s) imported." + (" " + "; ".join(problems[:5]) if problems else ""), "ok")
    return redirect(url_for("register_views.register"))
