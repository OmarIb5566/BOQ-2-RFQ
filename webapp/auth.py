"""Accounts: sign in / out, first-run admin set-up, user administration, CSRF protection, idle sign-out."""
from __future__ import annotations

import secrets
import time
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import security
from .db import now

bp = Blueprint("auth", __name__)
MIN_PW = 8


def db():
    return current_app.config["DB"]


# ------------------------------------------------------------------ request hooks
@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        u = db().one("SELECT * FROM users WHERE id=? AND active=1", (uid,))
        t = int(time.time())
        limit = current_app.config["REMEMBER_SECONDS" if session.permanent else "IDLE_SECONDS"]
        if not u or session.get("sv", 0) != u["ses_ver"]:
            session.clear()                       # disabled, deleted, or the password was changed elsewhere
        elif t - session.get("seen", t) > limit:
            session.clear()
            flash("You were signed out after a period of inactivity.", "error")
        else:
            g.user = dict(u)
            if not security.is_background() and t - session.get("seen", 0) > 30:
                session["seen"] = t
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    if request.method == "POST":
        tok = request.headers.get("X-CSRF") or request.form.get("_csrf")
        if not tok or not secrets.compare_digest(tok, session["csrf"]):
            if request.is_json or request.headers.get("X-CSRF") is not None:
                return jsonify(ok=False, error="Your session expired — reload the page."), 400
            abort(400, "Form expired — go back and reload the page.")


@bp.app_context_processor
def inject():
    return {"me": g.get("user"), "csrf_token": session.get("csrf", "")}


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if g.user is None:
            if not db().one("SELECT 1 FROM users LIMIT 1"):
                return redirect(url_for("auth.setup"))
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Signed out — sign in again."), 401
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*a, **kw):
        if g.user["role"] != "admin":
            abort(403)
        return view(*a, **kw)
    return wrapped


# ------------------------------------------------------------------ pages
def setup_token_ok() -> bool:
    """First-run set-up needs the one-time token printed in the server window (or saved in instance/setup.token),
    so nobody else on the network can make themselves the administrator before you do."""
    want = current_app.config.get("SETUP_TOKEN") or ""
    got = request.values.get("token", "")
    if want and got and secrets.compare_digest(got, want):
        session["setup_ok"] = True
    return bool(want) and bool(session.get("setup_ok"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First run: create the administrator account."""
    if db().one("SELECT 1 FROM users LIMIT 1"):
        return redirect(url_for("auth.login"))
    if not setup_token_ok():
        return render_template("auth/setup_locked.html"), 403
    if request.method == "POST":
        err = _create_user(request.form.get("username", ""), request.form.get("full_name", ""),
                           request.form.get("password", ""), request.form.get("password2", ""), "admin")
        if err:
            flash(err, "error")
        else:
            u = db().one("SELECT * FROM users WHERE username=?", (request.form["username"].strip(),))
            session.clear()
            _start_session(u, remember=False)
            current_app.config["CLEAR_SETUP_TOKEN"]()
            current_app.config["IMPORT_RUNS"](u["id"])      # existing runs on disk become this admin's projects
            flash("Welcome! Your administrator account is ready.", "ok")
            return redirect(url_for("main.projects"))
    return render_template("auth/setup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not db().one("SELECT 1 FROM users LIMIT 1"):
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()[:80]
        ip = request.remote_addr or "?"
        wait = security.login_blocked(db(), username, ip)
        if wait:
            flash(f"Too many wrong passwords. Try again in {max(1, round(wait / 60))} minute(s), "
                  "or ask an administrator to reset your password.", "error")
            return render_template("auth/login.html"), 429
        u = db().one("SELECT * FROM users WHERE username=? AND active=1", (username,))
        ok = check_password_hash(u["pw_hash"] if u else _DUMMY_HASH, request.form.get("password", ""))
        if u and ok:
            security.login_succeeded(db(), username)
            session.clear()
            _start_session(u, remember=bool(request.form.get("remember")))
            db().x("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"]))
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt
                            else url_for("main.projects"))
        security.login_failed(db(), username, ip)
        flash("Wrong user name or password.", "error")
    return render_template("auth/login.html", remember_days=current_app.config["REMEMBER_SECONDS"] // 86400)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        if not check_password_hash(g.user["pw_hash"], request.form.get("current", "")):
            flash("Your current password is not right.", "error")
        else:
            pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
            err = _pw_problem(pw, pw2)
            if err:
                flash(err, "error")
            else:
                db().x("UPDATE users SET pw_hash=?, ses_ver=ses_ver+1 WHERE id=?",
                       (generate_password_hash(pw), g.user["id"]))
                session["sv"] = g.user["ses_ver"] + 1           # this session stays; all others end
                flash("Password changed. You were signed out everywhere else.", "ok")
        name = request.form.get("full_name")
        if name is not None and name.strip() != g.user["full_name"]:
            db().x("UPDATE users SET full_name=? WHERE id=?", (name.strip()[:80], g.user["id"]))
        return redirect(url_for("auth.account"))
    return render_template("auth/account.html")


@bp.route("/admin/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        act = request.form.get("action")
        uid = request.form.get("uid", type=int)
        if act == "add":
            err = _create_user(request.form.get("username", ""), request.form.get("full_name", ""),
                               request.form.get("password", ""), request.form.get("password", ""),
                               "admin" if request.form.get("role") == "admin" else "user")
            flash(err or "User added — give them their user name and password.", "error" if err else "ok")
        elif act == "reset" and uid:
            pw = request.form.get("password", "")
            err = _pw_problem(pw, pw)
            if not err:
                db().x("UPDATE users SET pw_hash=?, ses_ver=ses_ver+1 WHERE id=?", (generate_password_hash(pw), uid))
                security.login_succeeded(db(), (db().one("SELECT username FROM users WHERE id=?", (uid,))
                                                or {"username": ""})["username"])   # lift a lock-out
            flash(err or "Password reset — the user is signed out everywhere.", "error" if err else "ok")
        elif act in ("disable", "enable") and uid:
            if uid == g.user["id"]:
                flash("You cannot disable your own account.", "error")
            else:
                db().x("UPDATE users SET active=? WHERE id=?", (1 if act == "enable" else 0, uid))
                flash("User " + ("enabled." if act == "enable" else "disabled — their projects stay."), "ok")
        elif act == "role" and uid and uid != g.user["id"]:
            db().x("UPDATE users SET role=? WHERE id=?",
                   ("admin" if request.form.get("role") == "admin" else "user", uid))
            flash("Role changed.", "ok")
        return redirect(url_for("auth.users"))
    rows = db().q("SELECT u.*, (SELECT COUNT(*) FROM projects p WHERE p.owner_id=u.id AND p.deleted=0) n "
                  "FROM users u ORDER BY u.active DESC, u.username")
    return render_template("auth/users.html", users=rows)


# ------------------------------------------------------------------ helpers
_DUMMY_HASH = generate_password_hash(secrets.token_hex(8))      # same work for unknown user names (no timing hint)


def _start_session(u, remember: bool) -> None:
    session["uid"] = u["id"]
    session["sv"] = u["ses_ver"]
    session["seen"] = int(time.time())
    session.permanent = remember
def _pw_problem(pw: str, pw2: str) -> str:
    if len(pw) < MIN_PW:
        return f"The password needs at least {MIN_PW} characters."
    if pw != pw2:
        return "The two passwords are not the same."
    return ""


def _create_user(username: str, full_name: str, pw: str, pw2: str, role: str) -> str:
    username = username.strip()
    if not username or len(username) > 40 or not all(c.isalnum() or c in "._-@" for c in username):
        return "User name: letters, digits and . _ - @ only."
    if db().one("SELECT 1 FROM users WHERE username=?", (username,)):
        return "That user name is taken."
    err = _pw_problem(pw, pw2)
    if err:
        return err
    db().x("INSERT INTO users (username, full_name, pw_hash, role, created) VALUES (?,?,?,?,?)",
           (username, full_name.strip()[:80] or username, generate_password_hash(pw), role, now()))
    return ""
