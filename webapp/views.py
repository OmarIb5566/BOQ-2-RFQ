"""Pages and actions.

Pages (server-rendered):  /projects  /new  /p/<run>/<tab>  /p/<run>/package/<key>
Actions (JSON, POST):     /api/p/<run>/…   — each runs under the project's lock and returns {ok, message}
Long work (analysis, export, re-reading, RFQ detection / generation) is queued as a background job.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from collections import Counter, OrderedDict
from dataclasses import asdict
from functools import wraps
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file,
                   url_for)

from boq_agent.config import Settings
from boq_agent.ingest.intake import UPLOAD_EXT
from boq_agent.ingest.sniff import HEAD_BYTES, check_head
from boq_agent.knowledge.rules import MISC
from boq_agent.text_utils import safe_filename, truncate

from .auth import login_required
from .db import now
from .store import STAGE_LABEL, STAGES, stage_of, summary_of

bp = Blueprint("main", __name__)
TABS = [("overview", "Overview"), ("cases", "Special cases"), ("packages", "Work packages"),
        ("items", "Items"), ("documents", "Specs & drawings"), ("export", "Export"), ("rfq", "RFQs"),
        ("activity", "Activity")]


def cfg(k):
    return current_app.config[k]


# ====================================================================== access helpers
def _row(run_id: str):
    r = cfg("DB").one("SELECT p.*, u.full_name owner_name, ub.full_name updated_by_name FROM projects p "
                      "LEFT JOIN users u ON u.id=p.owner_id LEFT JOIN users ub ON ub.id=p.updated_by "
                      "WHERE run_id=?", (run_id,))
    if r is None:
        abort(404)
    r = dict(r)
    if not (g.user["role"] == "admin" or r["owner_id"] == g.user["id"] or r["shared"]):
        abort(403)
    return r


def api(view):
    """JSON action: access check, busy check, errors -> {ok: false, error}."""
    @wraps(view)
    @login_required
    def wrapped(run_id, *a, **kw):
        try:
            _row(run_id)
        except Exception:  # noqa: BLE001
            return jsonify(ok=False, error="Project not found or not shared with you."), 404
        job = cfg("JOBS").active_for(run_id)
        if job and request.endpoint not in ("main.api_project", "main.api_draft"):
            return jsonify(ok=False, error=f"Please wait — “{job['label']}” is running for this project."), 409
        try:
            out = view(run_id, *a, **kw)
        except (ValueError, RuntimeError, KeyError) as e:
            return jsonify(ok=False, error=str(e)), 400
        return out if not isinstance(out, dict) else jsonify(ok=True, **out)
    return wrapped


def body() -> dict:
    return request.get_json(silent=True) or {}


# ====================================================================== projects
@bp.route("/")
@login_required
def home():
    return redirect(url_for("main.projects"))


@bp.route("/projects")
@login_required
def projects():
    db = cfg("DB")
    scope = request.args.get("scope", "all")
    stage = request.args.get("stage", "")
    q = request.args.get("q", "").strip()
    trash = request.args.get("trash") == "1"
    where, args = ["p.deleted=?"], [1 if trash else 0]
    if g.user["role"] != "admin":
        where.append("(p.owner_id=? OR p.shared=1)")
        args.append(g.user["id"])
    if scope == "mine":
        where.append("p.owner_id=?")
        args.append(g.user["id"])
    elif scope == "starred":
        where.append("p.starred=1")
    if stage:
        where.append("p.stage=?")
        args.append(stage)
    if q:
        where.append("(p.name LIKE ? OR p.summary LIKE ? OR u.full_name LIKE ?)")
        args += [f"%{q}%"] * 3
    rows = db.q("SELECT p.*, u.full_name owner_name FROM projects p LEFT JOIN users u ON u.id=p.owner_id "
                f"WHERE {' AND '.join(where)} ORDER BY p.starred DESC, p.updated DESC", tuple(args))
    counts = Counter(r["stage"] for r in db.q(
        "SELECT stage FROM projects WHERE deleted=0" + ("" if g.user["role"] == "admin" else
                                                        " AND (owner_id=? OR shared=1)"),
        () if g.user["role"] == "admin" else (g.user["id"],)))
    jobs = {j["run_id"]: j for j in (cfg("JOBS").view(r) for r in db.q(
        "SELECT * FROM jobs WHERE status IN ('queued','running')"))}
    return render_template("projects.html", rows=[dict(r) for r in rows], scope=scope, stage=stage, q=q,
                           trash=trash, counts=counts, stages=STAGES, stage_label=STAGE_LABEL, jobs=jobs)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_project():
    if request.method == "GET":
        return render_template("new.html", exts=sorted(UPLOAD_EXT | {".zip"}))
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("Choose at least one file (the BOQ).", "error")
        return redirect(url_for("main.new_project"))
    bad = [f.filename for f in files if Path(f.filename).suffix.lower() not in UPLOAD_EXT | {".zip"}]
    if bad:
        flash("Not supported: " + ", ".join(bad), "error")
        return redirect(url_for("main.new_project"))
    fakes = [e for e in (check_head(_peek(f), Path(f.filename).name) for f in files) if e]
    if fakes:
        flash("Not uploaded — " + "; ".join(fakes), "error")
        return redirect(url_for("main.new_project"))
    name = request.form.get("name", "").strip() or Path(files[0].filename).stem + (
        f" +{len(files) - 1}" if len(files) > 1 else "")
    run_id = _new_run_id(name)
    inc = cfg("STORE").run_dir(run_id) / "_incoming"
    inc.mkdir(parents=True)
    saved = []
    for i, f in enumerate(files):
        dst = inc / str(i) / safe_filename(Path(f.filename).name, 150)
        dst.parent.mkdir()
        f.save(dst)
        saved.append({"path": str(dst), "name": Path(f.filename).name})
    opts = {"use_ai": request.form.get("use_ai") == "on"}
    (inc / "manifest.json").write_text(json.dumps({"files": saved, **opts}, ensure_ascii=False), encoding="utf-8")
    cfg("DB").upsert_project(run_id, name=name[:120], owner_id=g.user["id"], shared=1 if request.form.get("shared") else 0,
                             stage="analyzing", summary=f"{len(files)} file(s) uploaded", updated_by=g.user["id"])
    _submit_analysis(run_id)
    return redirect(url_for("main.project", run_id=run_id))


def _peek(f) -> bytes:
    """First bytes of an uploaded file without consuming it (pages / pixels are checked later, in the job)."""
    head = f.stream.read(HEAD_BYTES)
    f.stream.seek(0)
    return head


def _new_run_id(name: str) -> str:
    base = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{safe_filename(name, 40).replace(' ', '_')}"
    run_id, n = base, 2
    while (cfg("SETTINGS").workspace / run_id).exists():
        run_id, n = f"{base}-{n}", n + 1
    return run_id


def _submit_analysis(run_id: str) -> str:
    store, db = cfg("STORE"), cfg("DB")
    man = json.loads((store.run_dir(run_id) / "_incoming" / "manifest.json").read_text(encoding="utf-8"))
    uid = g.user["id"]
    settings = cfg_settings(man.get("use_ai", True))

    def job(progress):
        from boq_agent.agent import BOQAgent

        agent = BOQAgent(settings, log=lambda m: None, progress=progress)
        try:
            with store.lock(run_id):
                s = agent.analyze([Path(f["path"]) for f in man["files"]], [f["name"] for f in man["files"]],
                                  run_id=run_id)
                db.upsert_project(run_id, error="")
                store.saved(run_id, uid, s)
        except Exception as e:
            db.upsert_project(run_id, stage="failed", error=str(e)[:1000])
            raise
        return f"{len(s.document.items)} items → {len(s.plan.work_packages)} work packages"

    return cfg("JOBS").submit("analyze", "Analysis", job, run_id, uid)


def cfg_settings(use_ai: bool = True) -> Settings:
    base = cfg("SETTINGS")
    return base if use_ai else Settings(**{**base.__dict__, "use_llm": False})


# ====================================================================== project pages
@bp.route("/p/<run_id>/")
@login_required
def project(run_id):
    row = _row(run_id)
    store = cfg("STORE")
    if not store.exists(run_id):
        return render_template("project/pending.html", p=row, job=cfg("JOBS").active_for(run_id),
                               last=_last_job(run_id))
    tab = "cases" if row["stage"] == "decisions" else "rfq" if row["stage"] in ("exported", "rfqs") else "packages"
    return redirect(url_for("main.tab", run_id=run_id, tab=tab))


def _last_job(run_id):
    r = cfg("DB").one("SELECT * FROM jobs WHERE run_id=? ORDER BY created DESC LIMIT 1", (run_id,))
    return dict(r) if r else None


@bp.route("/p/<run_id>/<tab>")
@login_required
def tab(run_id, tab):
    if tab not in dict(TABS):
        abort(404)
    row = _row(run_id)
    store = cfg("STORE")
    if not store.exists(run_id):
        return redirect(url_for("main.project", run_id=run_id))
    s = store.load(run_id)
    ctx = _common(row, s)
    ctx.update(globals()[f"_tab_{tab}"](s, row))
    return render_template(f"project/{tab}.html", tab=tab, **ctx)


@bp.route("/p/<run_id>/package/<path:key>")
@login_required
def package(run_id, key):
    row = _row(run_id)
    s = cfg("STORE").load(run_id)
    wp = next((w for w in s.plan.work_packages if w.key == key), None)
    if wp is None:
        flash("That work package no longer exists (the packaging changed).", "error")
        return redirect(url_for("main.tab", run_id=run_id, tab="packages"))
    ctx = _common(row, s)
    items = s.document.item_map()
    secs = s.document.section_map()
    docs = s.documents
    refs = docs.ref_map()
    links = docs.links_for(wp.key)
    linked = {l.ref for l in links}
    dfm = docs.file_map()
    addable = [(r.uid, ("Spec · " if hasattr(r, "pages") else "Drawing · ") + _ref_label(r, dfm))
               for r in refs.values() if r.uid not in linked and dfm.get(r.file_id) and dfm[r.file_id].kind in
               ("spec", "drawing") and getattr(r, "kind", "") != "not a drawing"]
    ctx.update(wp=wp, tab="packages",
               rows=[{"no": items[u].item_no, "desc": items[u].full_description, "unit": items[u].unit,
                      "qty": items[u].quantity, "section": secs[items[u].section_uid].title,
                      "cat": items[u].category or "", "method": items[u].method} for u in wp.item_uids],
               links=[{"l": l, "label": _ref_label(refs[l.ref], dfm), "file": dfm[refs[l.ref].file_id].filename}
                      for l in links if l.ref in refs],
               addable=addable, renamed=wp.key in s.wp_names,
               files=[(t, p) for t, p in (("BOQ cut-out", wp.pdf_file), ("Specifications", wp.spec_pdf),
                                          ("Drawings", wp.drawings_pdf)) if p and Path(p).exists()])
    return render_template("project/package.html", **ctx)


@bp.route("/p/<run_id>/case-items")
@login_required
def case_items(run_id):
    _row(run_id)
    s = cfg("STORE").load(run_id)
    p = next((p for p in s.plan.proposals if p.proposal_id == request.args.get("pid")), None)
    if p is None:
        return "<p class='muted'>This case changed — reload the page.</p>"
    items = s.document.item_map()
    return render_template("project/_items_table.html", rows=[
        {"no": items[u].item_no, "desc": items[u].full_description, "unit": items[u].unit, "qty": items[u].quantity}
        for u in p.item_uids])


@bp.route("/p/<run_id>/file/<path:rel>")
@login_required
def file(run_id, rel):
    _row(run_id)
    root = cfg("STORE").run_dir(run_id)
    p = (root / rel).resolve()
    if root not in p.parents or not p.is_file():
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=p.name)


def rel(run_id: str, path) -> str:
    """URL of a file inside the project folder (for links in templates)."""
    if not path:
        return ""
    try:
        r = Path(path).resolve().relative_to(cfg("STORE").run_dir(run_id))
    except ValueError:
        return ""
    return url_for("main.file", run_id=run_id, rel=r.as_posix())


# ====================================================================== tab contexts
def _common(row, s) -> dict:
    pending = [p for p in s.plan.proposals if p.decision == "pending"]
    docs = s.documents
    return {
        "p": row, "s": s, "tabs": TABS, "pending": pending, "stage_label": STAGE_LABEL,
        "job": cfg("JOBS").active_for(row["run_id"]),
        "can_admin": g.user["role"] == "admin" or row["owner_id"] == g.user["id"],
        "has_docs": bool(docs.files),
        "outdated": bool(s.zip_file and s.status != "exported"),
        "counts": {"cases": len(pending), "packages": len(s.plan.work_packages), "items": len(s.document.items),
                   "documents": len(docs.sections) + len(docs.sheets)},
        "rel": lambda pth: rel(row["run_id"], pth),
    }


def _tab_overview(s, row) -> dict:
    d = s.document
    n_items = Counter()
    for sec in d.sections:
        n_items[sec.file_id] += len(sec.item_uids)
    jobs = [cfg("JOBS").view(r) for r in cfg("DB").q(
        "SELECT * FROM jobs WHERE run_id=? ORDER BY created DESC LIMIT 6", (row["run_id"],))]
    return {"boqs": [(f, n_items[f.id]) for f in d.file_list()], "warnings": d.warnings, "recent_jobs": jobs,
            "doc_files": s.documents.files, "notes": s.plan.notes,
            "low_conf": sum(1 for it in d.items if it.method.startswith("llm") and it.confidence < 0.6)}


def _tab_cases(s, row) -> dict:
    order = {"pending": 0, "approved": 1, "denied": 1}
    show = request.args.get("show", "all")
    cases = sorted(s.plan.proposals, key=lambda p: order[p.decision])
    if show == "pending":
        cases = [p for p in cases if p.decision == "pending"]
    groups = OrderedDict()
    for p in cases:
        groups.setdefault(p.rule_id, []).append(p)
    return {"groups": groups, "show": show,
            "rule_titles": {"XBOQ_MERGE": "Same package in several BOQs"}}


def _tab_packages(s, row) -> dict:
    d = s.document
    fmap = d.file_map()
    docs = s.documents
    out = []
    for wp in s.plan.work_packages:
        out.append({"wp": wp, "boqs": ", ".join(fmap[f].label for f in wp.source_files if f in fmap),
                    "specs": len(docs.links_for(wp.key, "spec")), "sheets": len(docs.links_for(wp.key, "drawing")),
                    "renamed": wp.key in s.wp_names})
    return {"wps": out, "multi": d.multi_file}


def _category_names(agent_rules) -> "OrderedDict[str, str]":
    names = OrderedDict([("NONE", "Stays with its section"), (MISC, "Other items of the division")])
    for cid in agent_rules.categories:
        names[cid] = agent_rules.wp_names(cid)[0]
    return names


def _tab_items(s, row) -> dict:
    rules = cfg("STORE").agent().rules
    names = _category_names(rules)
    secs = s.document.section_map()
    fmap = s.document.file_map()
    wp_of = {u: wp for wp in s.plan.work_packages for u in wp.item_uids}
    items = [{"uid": it.uid, "no": it.item_no, "desc": truncate(it.full_description, 220),
              "section": truncate(secs[it.section_uid].title, 60), "unit": it.unit, "qty": it.quantity,
              "cat": it.category or "NONE", "cat_name": names.get(it.category or "NONE", it.category or ""),
              "method": it.method, "conf": it.confidence, "reason": it.reason,
              "wp": wp_of[it.uid].wp_id if it.uid in wp_of else "", "variant": bool(it.variant_group)}
             for it in s.document.items]
    divs = [("", "— not set —")] + [(c, f"Div {c} · {d['name_en']}") for c, d in rules.divisions.items()]
    sections = [{"uid": x.uid, "title": x.title, "div": x.division or "", "n": len(x.item_uids),
                 "method": x.division_method, "file": fmap[x.file_id].label if x.file_id in fmap else ""}
                for x in s.document.sections]
    return {"items": items, "cat_names": names, "divs": divs, "sections": sections, "multi": s.document.multi_file}


def _ref_label(r, dfm) -> str:
    if hasattr(r, "pages"):
        pg = f"p. {r.pages[0] + 1}–{r.pages[-1] + 1}" if len(r.pages) > 1 else (f"p. {r.pages[0] + 1}" if r.pages else "")
    else:
        pg = f"p. {r.page + 1}"
    f = dfm.get(r.file_id)
    return f"{truncate(r.label, 70)} — {truncate(Path(f.filename).stem, 30) if f else ''} {pg}"


def _tab_documents(s, row) -> dict:
    docs = s.documents
    dfm = docs.file_map()
    refs = docs.ref_map()
    wp_by_key = {w.key: w for w in s.plan.work_packages}
    by_ref: dict[str, list] = {}
    for l in docs.links:
        if l.wp_key in wp_by_key:
            by_ref.setdefault(l.ref, []).append((wp_by_key[l.wp_key], l))
    files = []
    for f in docs.files:
        secs = [x for x in docs.sections if x.file_id == f.id]
        shs = [x for x in docs.sheets if x.file_id == f.id]
        files.append({"f": f, "secs": secs, "sheets": shs, "review": sum(x.review for x in shs)})
    return {"files": files, "by_ref": by_ref, "refs": refs, "dfm": dfm,
            "unlinked": [refs[u] for u in docs.stats.get("unlinked", []) if u in refs],
            "label": lambda r: _ref_label(r, dfm), "stats": docs.stats}


def _tab_export(s, row) -> dict:
    files = []
    for wp in s.plan.work_packages:
        parts = [(t, p) for t, p in (("BOQ", wp.pdf_file), ("Specs", wp.spec_pdf), ("Drawings", wp.drawings_pdf))
                 if p and Path(p).exists()]
        files.append((wp, parts))
    zp = Path(s.zip_file) if s.zip_file else None
    return {"files": files, "zip": zp if zp and zp.exists() else None,
            "zip_size": zp.stat().st_size if zp and zp.exists() else 0}


def _tab_activity(s, row) -> dict:
    jobs = [cfg("JOBS").view(r) for r in cfg("DB").q(
        "SELECT j.*, u.full_name who FROM jobs j LEFT JOIN users u ON u.id=j.user_id WHERE run_id=? "
        "ORDER BY created DESC LIMIT 50", (row["run_id"],))]
    return {"jobs": jobs, "log": list(reversed(s.log[-400:]))}


# ---------------------------------------------------------------------- RFQ
def _template_path(s) -> Path:
    t = s.rfq_draft.get("template")
    return Path(t) if t and Path(t).exists() else cfg("SETTINGS").rfq_template


def _field_cfg() -> dict:
    from boq_agent.rfq.detect import load_field_config
    return load_field_config(cfg("SETTINGS").rfq_fields)


def _tab_rfq(s, row) -> dict:
    from boq_agent.rfq.builder import attachment_drops, field_specs, package_name

    draft = s.rfq_draft
    tpl = _template_path(s)
    fcfg = _field_cfg()
    specs = field_specs(tpl, fcfg)
    pre = draft.get("prefill") if draft.get("prefill_template") == str(tpl) else None
    pre_by = {p["spec"]["name"]: p for p in (pre or {}).get("project", [])}
    values = draft.get("values", {})
    fields = []
    for sp in specs:
        if sp.scope != "project":
            continue
        p = pre_by.get(sp.name, {})
        fields.append({"spec": sp, "value": values.get(sp.name, p.get("value", "")), "detected": p.get("value", ""),
                       "auto": bool(p.get("auto")), "source": p.get("source", ""),
                       "suggestions": [x[0] for x in p.get("suggestions", [])],
                       "confirmed": sp.name in draft.get("confirmed", []),
                       "same_as": sp.detect.split(":", 1)[1] if sp.detect.startswith("same_as:") else "",
                       "different": bool(draft.get("diff_date", {}).get(sp.name))})
    sups = draft.get("suppliers", {})
    skip = set(draft.get("skip", []))
    pk = []
    for wp in s.plan.work_packages:
        att = ["BOQ"] + [t for t, ok in (("Drawings", wp.drawings_pdf), ("Specifications", wp.spec_pdf)) if ok]
        pk.append({"wp": wp, "name": package_name(s, wp), "suppliers": sups.get(wp.key, []),
                   "include": wp.key not in skip, "attach": att,
                   "drops": attachment_drops(wp, fcfg)})
    rz = Path(s.rfq["zip"]) if s.rfq.get("zip") else None
    folders = OrderedDict()
    for f in s.rfq.get("files", []):
        fp = Path(f)
        if fp.exists():
            folders.setdefault(fp.parent.name, []).append(fp)
    return {"fields": fields, "packages": pk, "template": tpl, "custom_template": tpl != cfg("SETTINGS").rfq_template,
            "prefill_ready": pre is not None, "rfq_zip": rz if rz and rz.exists() else None,
            "rfq_folders": folders, "generated": s.rfq.get("created", ""),
            "supplier_fields": (pre or {}).get("supplier_fields", [x.name for x in specs if x.scope == "supplier"])}


# ====================================================================== actions
@bp.route("/api/p/<run_id>/decide", methods=["POST"])
@api
def api_decide(run_id):
    b = body()
    ids, decision = b.get("ids") or [], b.get("decision")
    if decision not in ("approved", "denied", "pending") or not ids:
        raise ValueError("Nothing to decide.")
    with cfg("STORE").edit(run_id, g.user["id"]) as (agent, s):
        agent.decide_many(s, ids, decision)
        left = sum(p.decision == "pending" for p in s.plan.proposals)
    verb = {"approved": "Approved", "denied": "Denied", "pending": "Reopened"}[decision]
    return {"message": f"{verb} {len(ids)} case(s). " + (f"{left} still open." if left else "All cases decided."),
            "left": left}


@bp.route("/api/p/<run_id>/item", methods=["POST"])
@api
def api_item(run_id):
    b = body()
    cat = b.get("category")
    with cfg("STORE").edit(run_id, g.user["id"]) as (agent, s):
        it = s.document.item_map()[b["uid"]]
        agent.set_item_category(s, it.uid, None if cat in (None, "", "NONE") else cat)
        n = sum(1 for x in s.document.items if it.variant_group and x.variant_group == it.variant_group) or 1
        wp = next((w for w in s.plan.work_packages if it.uid in w.item_uids), None)
    return {"message": f"Item {it.item_no or ''} moved" + (f" with its {n - 1} variant(s)" if n > 1 else "")
                       + (f" → {wp.wp_id} {wp.name_en}" if wp else "."), "wp": wp.wp_id if wp else ""}


@bp.route("/api/p/<run_id>/division", methods=["POST"])
@api
def api_division(run_id):
    b = body()
    uid, div = b["section_uid"], b.get("division") or None
    store, user = cfg("STORE"), g.user["id"]

    def job(progress):
        with store.edit(run_id, user) as (agent, s):
            agent.progress = progress
            agent.set_section_division(s, uid, div)
        return "Section re-classified"
    cfg("JOBS").submit("division", "Re-classifying a section", job, run_id, user)
    return {"message": "Re-classifying the section's items …", "job": True}


@bp.route("/api/p/<run_id>/wp/rename", methods=["POST"])
@api
def api_rename_wp(run_id):
    b = body()
    with cfg("STORE").edit(run_id, g.user["id"]) as (agent, s):
        agent.rename_wp(s, b["key"], b.get("name", ""))
    return {"message": "Name restored." if not b.get("name", "").strip() else "Package renamed."}


@bp.route("/api/p/<run_id>/docs/kind", methods=["POST"])
@api
def api_doc_kind(run_id):
    b = body()
    if b.get("kind") not in ("spec", "drawing", "ignored"):
        raise ValueError("Unknown type.")
    store, user = cfg("STORE"), g.user["id"]

    def job(progress):
        with store.edit(run_id, user) as (agent, s):
            agent.progress = progress
            agent.set_doc_kind(s, b["file_id"], b["kind"])
        return "Documents re-read and matched"
    cfg("JOBS").submit("docs", "Re-reading documents", job, run_id, user)
    return {"message": "Re-reading the file …", "job": True}


@bp.route("/api/p/<run_id>/docs/link", methods=["POST"])
@api
def api_doc_link(run_id):
    b = body()
    with cfg("STORE").edit(run_id, g.user["id"]) as (agent, s):
        refs = s.documents.ref_map()
        r = refs.get(b["ref"])
        if r is None:
            raise ValueError("That document is no longer there.")
        kind = "spec" if hasattr(r, "pages") else "drawing"
        cur = [l.ref for l in s.documents.links_for(b["wp_key"], kind)]
        new = (cur + [r.uid]) if b.get("on") else [x for x in cur if x != r.uid]
        agent.set_doc_links(s, b["wp_key"], kind, list(dict.fromkeys(new)))
    return {"message": ("Attached: " if b.get("on") else "Removed: ") + truncate(r.label, 60)}


@bp.route("/api/p/<run_id>/export", methods=["POST"])
@api
def api_export(run_id):
    s = cfg("STORE").load(run_id)
    left = sum(p.decision == "pending" for p in s.plan.proposals)
    if left:
        raise ValueError(f"Decide the {left} open special case(s) first.")
    store, user = cfg("STORE"), g.user["id"]

    def job(progress):
        with store.edit(run_id, user) as (agent, s):
            agent.progress = progress
            agent.export(s)
        return f"{len(s.plan.work_packages)} work packages exported"
    cfg("JOBS").submit("export", "Export", job, run_id, user)
    return {"message": "Export started …", "job": True}


# ---------------------------------------------------------------------- RFQ actions
@bp.route("/api/p/<run_id>/rfq/detect", methods=["POST"])
@api
def api_rfq_detect(run_id):
    store, user = cfg("STORE"), g.user["id"]

    def job(progress):
        progress(0.2, "Reading the BOQ title area, headers and footers …")
        with store.edit(run_id, user) as (agent, s):
            tpl = _template_path(s)
            pre = agent.rfq_prefill(s, tpl)
            s.rfq_draft["prefill"] = {"project": [asdict(p) for p in pre["project"]],
                                      "supplier_fields": pre["supplier_fields"]}
            s.rfq_draft["prefill_template"] = str(tpl)
            vals = s.rfq_draft.setdefault("values", {})
            for p in pre["project"]:
                if p.value and not vals.get(p.spec.name):
                    vals[p.spec.name] = p.value
            agent.save(s)
        return "Project details detected"
    cfg("JOBS").submit("rfq-detect", "Detecting project details", job, run_id, user)
    return {"message": "Reading project details from the BOQ …", "job": True}


@bp.route("/api/p/<run_id>/rfq/draft", methods=["POST"])
@api
def api_draft(run_id):
    """Autosave of the RFQ form (values, confirmations, suppliers)."""
    b = body()
    store = cfg("STORE")
    with store.lock(run_id):
        s = store.load(run_id)
        d = s.rfq_draft
        for k in ("values", "confirmed", "diff_date", "suppliers", "skip"):
            if k in b:
                d[k] = b[k]
        store.agent().save(s)
        store.saved(run_id, g.user["id"], s)
    return {"message": "Saved"}


def _rfq_inputs(s):
    """(values, packages, supplier_fields, missing confirmations, template) from the saved draft."""
    tpl = _template_path(s)
    ctx = _tab_rfq(s, None)
    fmt = _field_cfg().get("date_format", "%d %B %Y")
    values, need = {}, []

    def as_date(v):
        try:
            return dt.date.fromisoformat(v).strftime(fmt)
        except (TypeError, ValueError):
            return v or ""
    for f in ctx["fields"]:
        sp = f["spec"]
        if f["same_as"] and not f["different"]:
            continue
        v = (f["value"] or "").strip()
        values[sp.name] = as_date(v) if sp.type == "date" else v
        if f["auto"] and v and v == f["detected"] and not f["confirmed"]:
            need.append(sp.label)
    for f in ctx["fields"]:
        if f["same_as"] and not f["different"]:
            values[f["spec"].name] = values.get(f["same_as"], "")
    pkgs = []
    from boq_agent.rfq.builder import field_specs
    pkg_specs = [x for x in field_specs(tpl, _field_cfg()) if x.scope == "package"]
    for p in ctx["packages"]:
        if not p["include"]:
            continue
        pkgs.append({"wp_id": p["wp"].wp_id, "values": {x.name: (p["name"] if x.detect == "wp_name" else "")
                                                        for x in pkg_specs},
                     "suppliers": [x for x in p["suppliers"] if x.strip()]})
    return values, pkgs, ctx["supplier_fields"], need, tpl


@bp.route("/api/p/<run_id>/rfq/preview", methods=["POST"])
@api
def api_rfq_preview(run_id):
    from boq_agent.rfq.builder import attachment_drops
    from boq_agent.rfq.template import preview

    s = cfg("STORE").load(run_id)
    values, pkgs, sup_fields, _, tpl = _rfq_inputs(s)
    key = body().get("wp_key")
    wp = next((w for w in s.plan.work_packages if w.key == key), None) or s.plan.work_packages[0]
    pk = next((p for p in pkgs if p["wp_id"] == wp.wp_id), {"values": {}, "suppliers": []})
    sup = pk["suppliers"][0] if pk["suppliers"] else ""
    lines = preview(tpl, {**values, **pk["values"], **{f: sup for f in sup_fields}}, attachment_drops(wp, _field_cfg()))
    return {"lines": [t for t in lines if t.strip()], "title": f"{wp.wp_id} · {wp.name_en}" + (f" · {sup}" if sup else "")}


@bp.route("/api/p/<run_id>/rfq/generate", methods=["POST"])
@api
def api_rfq_generate(run_id):
    store, user = cfg("STORE"), g.user["id"]
    s = store.load(run_id)
    if s.status != "exported":
        raise ValueError("Export the work packages first — each RFQ attaches its BOQ cut-out.")
    values, pkgs, sup_fields, need, tpl = _rfq_inputs(s)
    if need:
        raise ValueError("Confirm the values found in the BOQ first: " + ", ".join(need))
    if not pkgs:
        raise ValueError("No work package is selected for RFQs.")

    def job(progress):
        progress(0.1, "Writing the letters …")
        with store.edit(run_id, user) as (agent, s2):
            agent.generate_rfqs(s2, values, pkgs, sup_fields, tpl)
        return f"{len(s2.rfq.get('files', []))} RFQ letter(s) written"
    cfg("JOBS").submit("rfq", "Writing RFQs", job, run_id, user)
    n = sum(max(1, len(p["suppliers"])) for p in pkgs)
    return {"message": f"Writing {n} RFQ letter(s) …", "job": True}


@bp.route("/api/p/<run_id>/rfq/template", methods=["POST"])
@api
def api_rfq_template(run_id):
    store = cfg("STORE")
    with store.edit(run_id, g.user["id"]) as (agent, s):
        if request.form.get("reset"):
            s.rfq_draft.pop("template", None)
            msg = "Back to the company template."
        else:
            f = request.files.get("template")
            if not f or not f.filename.lower().endswith(".docx"):
                raise ValueError("Choose a Word template (.docx).")
            if check_head(_peek(f), "template.docx"):
                raise ValueError("That file is not a real Word document (.docx).")
            f.stream.seek(0, 2)
            if f.stream.tell() > 20 * 1024 ** 2:
                raise ValueError("The Word template is too large (20 MB at most).")
            f.stream.seek(0)
            dst = store.run_dir(run_id) / "rfq_template.docx"
            f.save(dst)
            from boq_agent.rfq.template import discover_fields
            try:
                n = len(discover_fields(dst))
            except ValueError:
                dst.unlink(missing_ok=True)
                raise
            except Exception as e:  # noqa: BLE001  (damaged zip / XML)
                dst.unlink(missing_ok=True)
                raise ValueError(f"That Word document cannot be read ({type(e).__name__}).") from e
            s.rfq_draft["template"] = str(dst)
            msg = f"Template uploaded — {n} field(s) found."
        s.rfq_draft.pop("prefill", None)
        agent.save(s)
    return {"message": msg}


# ---------------------------------------------------------------------- project management
@bp.route("/api/p/<run_id>/project", methods=["POST"])
@api
def api_project(run_id):
    row = _row(run_id)
    b = body()
    db = cfg("DB")
    owner = g.user["role"] == "admin" or row["owner_id"] == g.user["id"]
    if "name" in b:
        name = " ".join(str(b["name"]).split())[:120]
        if not name:
            raise ValueError("The name cannot be empty.")
        db.upsert_project(run_id, name=name, updated_by=g.user["id"])
        return {"message": "Renamed."}
    if "starred" in b:
        db.x("UPDATE projects SET starred=? WHERE run_id=?", (1 if b["starred"] else 0, run_id))
        return {"message": "Starred." if b["starred"] else "Unstarred."}
    if "shared" in b:
        if not owner:
            raise ValueError("Only the owner can change sharing.")
        db.upsert_project(run_id, shared=1 if b["shared"] else 0)
        return {"message": "Shared with the team." if b["shared"] else "Now private to its owner."}
    if "deleted" in b:
        if not owner:
            raise ValueError("Only the owner or an administrator can delete a project.")
        db.x("UPDATE projects SET deleted=? WHERE run_id=?", (1 if b["deleted"] else 0, run_id))
        return {"message": "Moved to the trash — it can be restored from there." if b["deleted"] else "Restored."}
    raise ValueError("Nothing to change.")


@bp.route("/api/p/<run_id>/duplicate", methods=["POST"])
@api
def api_duplicate(run_id):
    """A copy to try a different packaging without touching the original (outputs are not copied)."""
    row = _row(run_id)
    store, db, user = cfg("STORE"), cfg("DB"), g.user["id"]
    if not store.exists(run_id):
        raise ValueError("Only an analysed project can be duplicated.")
    name = f"{row['name']} (copy)"
    new_id = _new_run_id(name)
    db.upsert_project(new_id, name=name, owner_id=user, shared=row["shared"], stage="analyzing",
                      summary="Copying …", updated_by=user)

    def job(progress):
        src, dst = store.run_dir(run_id), store.run_dir(new_id)
        progress(0.1, "Copying the project files …")
        with store.lock(run_id):
            shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
                "work_packages", "rfq", "*.zip", "session.tmp"))
        text = (dst / "session.json").read_text(encoding="utf-8")
        for a, b in ((str(src), str(dst)), (str(src).replace("\\", "/"), str(dst).replace("\\", "/"))):
            text = text.replace(json.dumps(a)[1:-1], json.dumps(b)[1:-1])
        from boq_agent.models import Session
        s = Session.model_validate_json(text)
        s.run_id = new_id
        s.status, s.output_dir, s.zip_file, s.rfq = "planned", None, None, {}
        for wp in s.plan.work_packages:
            wp.pdf_file = wp.spec_pdf = wp.drawings_pdf = None
        store.agent().save(s)
        store.saved(new_id, user, s)
        return "Copy ready"
    cfg("JOBS").submit("duplicate", "Duplicating", job, new_id, user)
    return {"message": "Making a copy …", "url": url_for("main.project", run_id=new_id)}


@bp.route("/api/p/<run_id>/retry", methods=["POST"])
@api
def api_retry(run_id):
    if cfg("STORE").exists(run_id):
        raise ValueError("This project was already analysed.")
    if not (cfg("STORE").run_dir(run_id) / "_incoming" / "manifest.json").exists():
        raise ValueError("The uploaded files are not available any more — start a new project.")
    cfg("DB").upsert_project(run_id, stage="analyzing", error="")
    _submit_analysis(run_id)
    return {"message": "Analysis restarted …", "job": True}


# ---------------------------------------------------------------------- jobs & status
@bp.route("/api/jobs/<jid>")
@login_required
def api_job(jid):
    j = cfg("JOBS").get(jid)
    if j is None:
        return jsonify(ok=False, error="unknown job"), 404
    return jsonify(ok=True, job=j)


@bp.route("/api/jobs")
@login_required
def api_jobs():
    run = request.args.get("run")
    db, jobs = cfg("DB"), cfg("JOBS")
    if run:
        _row(run)
        j = jobs.active_for(run)
        last = _last_job(run)
        return jsonify(ok=True, active=j, last=last)
    rows = db.q("SELECT j.*, p.name pname FROM jobs j LEFT JOIN projects p ON p.run_id=j.run_id "
                "WHERE j.status IN ('queued','running') ORDER BY j.created")
    mine = db.q("SELECT j.*, p.name pname FROM jobs j LEFT JOIN projects p ON p.run_id=j.run_id "
                "WHERE j.user_id=? AND j.status IN ('done','failed') AND j.finished > ? ORDER BY j.finished DESC",
                (g.user["id"], request.args.get("since", now())))
    return jsonify(ok=True, active=[{**jobs.view(r), "pname": r["pname"]} for r in rows],
                   finished=[dict(r) for r in mine], now=now())


_status_cache: dict = {}


@bp.route("/api/status")
@login_required
def api_status():
    import time

    hit = _status_cache.get("v")
    if not hit or time.time() - hit[0] > 30:
        a = cfg("STORE").agent()
        from boq_agent.llm.client import OllamaClient
        st = cfg("SETTINGS")
        llm = a.llm.status()
        vis = OllamaClient(st.ollama_url, st.vision_model).status()
        hit = (time.time(), {"llm": llm[0], "llm_msg": llm[1], "vision": vis[0], "vision_msg": vis[1]})
        _status_cache["v"] = hit
    q = cfg("DB").one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")["n"]
    return jsonify(ok=True, queue=q, **hit[1])


@bp.app_template_filter("ago")
def ago(ts: str) -> str:
    if not ts:
        return ""
    try:
        d = dt.datetime.now() - dt.datetime.fromisoformat(ts)
    except ValueError:
        return ts
    s = int(d.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    if s < 86400 * 7:
        return f"{s // 86400} d ago"
    return ts[:10]


@bp.app_template_filter("size")
def size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@bp.app_template_global()
def stage_index(stage: str) -> int:
    order = ["decisions", "ready", "exported", "rfqs"]
    return order.index(stage) if stage in order else -1


def import_runs(owner_id: int) -> int:
    """Existing workspace runs (made before the web app) become projects of the given user."""
    store, db = cfg("STORE"), cfg("DB")
    n = 0
    for d in sorted(store.settings.workspace.iterdir()):
        if not (d / "session.json").exists() or d.name.startswith("_"):
            continue
        if db.one("SELECT 1 FROM projects WHERE run_id=?", (d.name,)):
            continue
        try:
            s = store.load(d.name)
        except Exception:  # noqa: BLE001
            continue
        name = Path(s.document.filename).stem if s.document.filename else d.name
        ts = s.created or now()
        db.upsert_project(d.name, name=name, owner_id=owner_id, shared=1, created=ts,
                          updated=dt.datetime.fromtimestamp((d / "session.json").stat().st_mtime).isoformat(
                              timespec="seconds"), stage=stage_of(s), summary=summary_of(s))
        store.forget(d.name)
        n += 1
    return n

