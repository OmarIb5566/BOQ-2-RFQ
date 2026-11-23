"""Admin console: system health, job queue, audit log, effective settings (users are in auth.py).

Administrators see everything; managers see the health and queue pages (to know why their job waits).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import fields
from functools import wraps
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from .auth import login_required
from .db import now

bp = Blueprint("admin", __name__, url_prefix="/admin")


def cfg(k):
    return current_app.config[k]


def role_required(*roles):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(*a, **kw):
            if g.user["role"] not in roles:
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


# ---------------------------------------------------------------------- health
_slow_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    hit = _slow_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _slow_cache[key] = (time.time(), v)
    return v


def gpu_info() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 4:
            gpus.append({"name": parts[0], "used_mb": int(float(parts[1])), "total_mb": int(float(parts[2])),
                         "util": int(float(parts[3]))})
    return gpus


def health() -> list[dict]:
    """[{name, ok (True/False/None = not used), detail}] — every check is cheap or cached."""
    st, db, jobs = cfg("SETTINGS"), cfg("DB"), cfg("JOBS")
    checks = []
    from .db import LATEST
    v = db.version()
    checks.append({"name": "Database", "ok": v == LATEST,
                   "detail": f"{db.path} · schema version {v}/{LATEST} · "
                             f"{db.path.stat().st_size / 1048576:.1f} MB" if db.path.exists() else str(db.path)})
    workers = jobs.worker_status()
    alive = [w for w in workers if time.time() - w["seen"] < 60]
    mode = cfg("WORKER_MODE")
    checks.append({"name": "Background worker", "ok": bool(alive),
                   "detail": (f"{len(alive)} running ({', '.join(sorted({w['mode'] for w in alive}))})" if alive else
                              ("No worker is running — start worker.py (BOQ_WORKER=external)." if mode == "external"
                               else "The worker thread is not running."))})
    from .fleet import fleet_status
    for role in fleet_status():
        checks.append({"name": f"AI model · {role['role']}", "ok": role["ok"], "detail": role["detail"]})
    for gpu in _cached("gpu", 10, gpu_info):
        checks.append({"name": f"GPU · {gpu['name']}", "ok": True,
                       "detail": f"{gpu['used_mb']:,} / {gpu['total_mb']:,} MB used · {gpu['util']}% busy"})
    du = shutil.disk_usage(st.workspace)
    checks.append({"name": "Disk (workspace)", "ok": du.free > 5 * 1024 ** 3,
                   "detail": f"{st.workspace} · {du.free / 1024 ** 3:.1f} GB free of {du.total / 1024 ** 3:.0f} GB"})

    def excel():
        from boq_agent.excel_com import excel_available
        return excel_available()
    has_excel = _cached("excel", 600, excel) if os.name == "nt" else False
    checks.append({"name": "Microsoft Excel", "ok": True if has_excel else None,
                   "detail": "available (exact-look snapshots, .xlsb)" if has_excel else
                   "not available — built-in renderer is used; .xlsb files cannot be read"})
    tess = Path(st.tesseract_dir) / ("tesseract.exe" if os.name == "nt" else "tesseract")
    has_tess = tess.exists() or bool(shutil.which("tesseract"))
    checks.append({"name": "Tesseract OCR", "ok": True if has_tess else None,
                   "detail": "available (scanned PDFs)" if has_tess else "not installed — scanned BOQs cannot be read"})
    from . import identity
    checks.append({"name": "Sign-in", "ok": True, "detail": {"local": "local accounts",
                                                              "ldap": "company directory (LDAP) only",
                                                              "ldap+local": "company directory, local fallback"}[
        identity.mode()]})
    return checks


@bp.route("/")
@role_required("admin", "manager")
def home():
    return redirect(url_for("admin.health_page"))


@bp.route("/health")
@role_required("admin", "manager")
def health_page():
    q = cfg("DB").one("SELECT SUM(status='queued') q, SUM(status='running') r, "
                      "SUM(status='failed' AND finished > date('now','-1 day')) f FROM jobs")
    return render_template("admin/health.html", checks=health(), q=dict(q), section="health")


@bp.route("/api/health")
@role_required("admin", "manager")
def api_health():
    return jsonify(ok=True, checks=health())


# ---------------------------------------------------------------------- queue
@bp.route("/queue")
@role_required("admin", "manager")
def queue():
    db, jobs = cfg("DB"), cfg("JOBS")
    rows = db.q("SELECT j.*, p.name pname, u.full_name uname FROM jobs j LEFT JOIN projects p ON p.run_id=j.run_id "
                "LEFT JOIN users u ON u.id=j.user_id ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' "
                "THEN 1 ELSE 2 END, j.created DESC LIMIT 150")
    return render_template("admin/queue.html", rows=[{**jobs.view(r), "pname": r["pname"], "uname": r["uname"]}
                                                     for r in rows],
                           workers=jobs.worker_status(), mode=cfg("WORKER_MODE"), section="queue", now_ts=time.time())


@bp.route("/jobs/<jid>/<action>", methods=["POST"])
@role_required("admin", "manager")
def job_action(jid, action):
    jobs = cfg("JOBS")
    if action == "cancel":
        res = jobs.cancel(jid)
        msg = {"cancelled": "Job cancelled.", "stopping": "The job stops at its next step."}.get(res, "Nothing to do.")
    elif action == "retry":
        msg = "Job queued again." if jobs.retry(jid) else "Only failed / interrupted / cancelled jobs can be retried."
    elif action == "test" and jid == "worker":
        jobs.submit("noop", "Worker test", "", g.user["id"], {"steps": 3})
        msg = "A test job was queued — it should finish within seconds."
    else:
        abort(404)
    cfg("DB").audit(f"job {action}", g.user["id"], ip=request.remote_addr, job=jid)
    flash(msg, "ok")
    return redirect(url_for("admin.queue"))


# ---------------------------------------------------------------------- audit log
@bp.route("/audit")
@role_required("admin")
def audit():
    db = cfg("DB")
    where, args = [], []
    for col in ("action", "run_id"):
        v = request.args.get(col, "").strip()
        if v:
            where.append(f"a.{col} LIKE ?")
            args.append(f"%{v}%")
    if request.args.get("user"):
        where.append("(u.username LIKE ? OR u.full_name LIKE ?)")
        args += [f"%{request.args['user']}%"] * 2
    page = max(0, request.args.get("page", 0, type=int))
    rows = db.q("SELECT a.*, u.username, u.full_name FROM audit a LEFT JOIN users u ON u.id=a.user_id "
                + (f"WHERE {' AND '.join(where)} " if where else "") + "ORDER BY a.ts DESC LIMIT 101 OFFSET ?",
                (*args, page * 100))
    out = []
    for r in rows[:100]:
        d = dict(r)
        try:
            d["detail"] = json.loads(d["detail"] or "{}")
        except ValueError:
            d["detail"] = {}
        out.append(d)
    return render_template("admin/audit.html", rows=out, page=page, more=len(rows) > 100, args=request.args,
                           section="audit")


# ---------------------------------------------------------------------- effective settings
SECRET_WORDS = ("PASSWORD", "SECRET", "TOKEN", "KEY")


def effective_settings() -> list[dict]:
    from boq_agent import settings_file
    st = cfg("SETTINGS")
    out = []
    for f in fields(st):
        out.append({"group": "Engine", "name": f.name, "value": str(getattr(st, f.name)), "source": ""})
    web_keys = ["BOQ_WORKER", "BOQ_AUTH", "BOQ_IDLE_MINUTES", "BOQ_REMEMBER_DAYS", "BOQ_HTTPS", "BOQ_BEHIND_PROXY",
                "BOQ_MAX_UPLOAD_MB", "BOQ_MAX_PDF_PAGES", "BOQ_MAX_IMAGE_MP", "BOQ_DEFAULT_LANG", "BOQ_TRASH_DAYS",
                "BOQ_MODEL_PROFILE", "BOQ_LDAP_URL", "BOQ_LDAP_USER_DN", "BOQ_LDAP_BASE_DN", "BOQ_LDAP_USER_FILTER",
                "BOQ_LDAP_ROLE_MAP", "BOQ_LDAP_DEFAULT_ROLE", "BOQ_INSTANCE", "BOQ_CONFIG", "OLLAMA_URL",
                "BOQ_LLM_MODEL", "BOQ_VISION_MODEL", "BOQ_WORKSPACE"]
    for k in web_keys:
        v = os.environ.get(k)
        src = "settings file" if k in settings_file.LOADED else ("environment" if v is not None else "default")
        if v is not None and any(w in k for w in SECRET_WORDS):
            v = "••••••"
        out.append({"group": "Server", "name": k, "value": v if v is not None else "", "source": src})
    for r in cfg("DB").q("SELECT key, value, updated FROM app_settings ORDER BY key"):
        out.append({"group": "Set in the admin console", "name": r["key"], "value": r["value"],
                    "source": f"changed {r['updated']}"})
    return out


@bp.route("/settings")
@role_required("admin")
def settings():
    from boq_agent import settings_file
    return render_template("admin/settings.html", rows=effective_settings(), section="settings",
                           config_file=settings_file.SOURCE[0] if settings_file.SOURCE else
                           str(settings_file.config_path()) + " (not present — defaults and environment only)",
                           generated=now())
