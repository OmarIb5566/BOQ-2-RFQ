"""Admin › AI models (switch the model of each role) and Admin › Usage (who used how much)."""
from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from . import fleet
from .admin import role_required

bp = Blueprint("fleet_views", __name__, url_prefix="/admin")


def cfg(k):
    return current_app.config[k]


@bp.route("/models", methods=["GET", "POST"])
@role_required("admin")
def models():
    db, st = cfg("DB"), cfg("SETTINGS")
    local = fleet.installed(st.ollama_url, ttl=0 if request.method == "POST" else 30)
    names = sorted({m["name"] for m in local})
    if request.method == "POST":
        c = fleet.config(db)
        new = {k: c[k] for k in ("profile", "cache_on", "memory_on", "verify_on", "fallbacks", *fleet.ROLES)}
        for r in fleet.ROLES:
            v = request.form.get(r, "")
            if v and v not in names:
                flash(f"{v} is not a local model on this server.", "error")
                return redirect(url_for("fleet_views.models"))
            if v:
                new[r] = v
            fb = [x for x in request.form.getlist(f"fallback_{r}") if x in names and x != new[r]]
            new["fallbacks"][r] = fb[:3]
        new["profile"] = "resident" if request.form.get("profile") == "resident" else "staged"
        for k in ("cache_on", "memory_on", "verify_on"):
            new[k] = request.form.get(k) == "on"
        db.set_setting("models", new, g.user["id"])
        db.audit("models changed", g.user["id"], ip=request.remote_addr, **{r: new[r] for r in fleet.ROLES},
                 profile=new["profile"], verify_on=new["verify_on"])
        flash("Saved — new jobs use these models.", "ok")
        return redirect(url_for("fleet_views.models"))
    sizes = {m["name"]: m.get("size", 0) for m in local}
    return render_template("admin/models.html", c=fleet.config(db), names=names, sizes=sizes, roles=fleet.ROLES,
                           role_help=fleet.ROLE_HELP, status={s["role"]: s for s in fleet.fleet_status()},
                           section="models")


@bp.route("/usage")
@role_required("admin", "manager")
def usage():
    db = cfg("DB")
    days = max(1, min(365, request.args.get("days", 30, type=int)))
    since = f"-{days} day"
    users = db.q("""SELECT u.id, u.full_name, u.username,
        (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id AND j.created > datetime('now', 'localtime', ?)) jobs,
        (SELECT COUNT(*) FROM jobs j WHERE j.user_id=u.id AND j.kind='analyze' AND j.created > datetime('now', 'localtime', ?)) analyses,
        (SELECT COALESCE(SUM(j.seconds),0) FROM jobs j WHERE j.user_id=u.id AND j.created > datetime('now', 'localtime', ?)) job_s,
        (SELECT COALESCE(SUM(c.seconds),0) FROM llm_calls c JOIN jobs j ON j.id=c.job_id WHERE j.user_id=u.id
             AND c.ts > datetime('now', 'localtime', ?)) ai_s,
        (SELECT COUNT(*) FROM projects p WHERE p.owner_id=u.id AND p.deleted=0) projects
        FROM users u WHERE u.active=1 ORDER BY job_s DESC""", (since,) * 4)
    by_model = db.q("""SELECT role, model, COUNT(*) calls, SUM(cached) cached, SUM(1-ok) failed,
        COALESCE(SUM(seconds),0) s, COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout
        FROM llm_calls WHERE ts > datetime('now', 'localtime', ?) GROUP BY role, model ORDER BY s DESC""", (since,))
    return render_template("admin/usage.html", users=[dict(r) for r in users], by_model=[dict(r) for r in by_model],
                           days=days, section="usage")
