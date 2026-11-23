"""Phase 0 sign-in hardening: first-run lock, throttling, idle time-out, session versioning, safe redirects."""
from __future__ import annotations

import time
from pathlib import Path

from conftest import ADMIN_PW, add_user, create_admin, csrf_of, login


def instance_dir(app) -> Path:
    return Path(app.instance_path)


# ---------------------------------------------------------------------- first-run set-up
def test_setup_is_locked_without_the_token(app):
    c = app.test_client()
    assert c.get("/").status_code == 302 and c.get("/").headers["Location"].endswith("/setup")
    r = c.get("/setup")
    assert r.status_code == 403 and "Set-up is locked" in r.text
    assert c.get("/setup?token=wrong").status_code == 403
    r = c.post("/setup", data=dict(_csrf=csrf_of(c), full_name="X", username="x", password="p" * 9, password2="p" * 9))
    assert r.status_code == 403
    assert app.config["DB"].one("SELECT COUNT(*) n FROM users")["n"] == 0
    assert (instance_dir(app) / "setup.token").exists()


def test_setup_with_token_creates_admin_and_burns_the_token(app):
    c = app.test_client()
    tok = app.config["SETUP_TOKEN"]
    create_admin(app, c)
    assert not (instance_dir(app) / "setup.token").exists()
    assert app.config["SETUP_TOKEN"] == ""
    assert c.get("/projects").status_code == 200
    other = app.test_client()
    assert other.get(f"/setup?token={tok}").headers["Location"].endswith("/login")     # done once, never again


def test_setup_url_only_on_first_run(app):
    from webapp import setup_url
    assert setup_url(app, "http://127.0.0.1:8080").startswith("http://127.0.0.1:8080/setup?token=")
    create_admin(app, app.test_client())
    assert setup_url(app, "http://127.0.0.1:8080") == ""


# ---------------------------------------------------------------------- throttling
def test_wrong_passwords_lock_the_user_name(app, admin_client):
    add_user(admin_client, "sara")
    c = app.test_client()
    for _ in range(5):
        assert login(c, "sara", "wrong-password").status_code == 200
    r = login(c, "sara", "user-pass-123")                        # right password, but locked now
    assert r.status_code == 429 and "Too many wrong passwords" in r.text
    assert login(app.test_client(), "admin", ADMIN_PW).status_code == 302      # other users are not affected


def test_unknown_user_names_count_too_and_ip_limit(app):
    c = app.test_client()
    create_admin(app, app.test_client())
    for i in range(20):
        login(c, f"nobody{i}", "x")                               # 20 different names from one address
    r = login(c, "admin", ADMIN_PW)
    assert r.status_code == 429


def test_admin_reset_lifts_the_lock(app, admin_client):
    add_user(admin_client, "omar")
    c = app.test_client()
    for _ in range(5):
        login(c, "omar", "bad")
    uid = app.config["DB"].one("SELECT id FROM users WHERE username='omar'")["id"]
    admin_client.post("/admin/users", data=dict(_csrf=csrf_of(admin_client), action="reset", uid=uid,
                                                password="new-pass-456"))
    assert login(c, "omar", "new-pass-456").status_code == 302


def test_successful_sign_in_clears_failures(app, admin_client):
    add_user(admin_client, "lina")
    c = app.test_client()
    for _ in range(4):
        login(c, "lina", "bad")
    assert login(c, "lina", "user-pass-123").status_code == 302
    assert app.config["DB"].one("SELECT COUNT(*) n FROM login_failures WHERE username='lina'")["n"] == 0


# ---------------------------------------------------------------------- idle time-out
def _age_session(client, seconds: int) -> None:
    with client.session_transaction() as s:
        s["seen"] = int(time.time()) - seconds


def test_idle_session_is_signed_out(make_app):
    app = make_app(BOQ_IDLE_MINUTES="1")
    c = app.test_client()
    create_admin(app, c)
    _age_session(c, 30)
    assert c.get("/projects").status_code == 200
    _age_session(c, 61)
    r = c.get("/projects")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert "inactivity" in c.get("/login").text


def test_background_polling_does_not_keep_the_session_alive(make_app):
    app = make_app(BOQ_IDLE_MINUTES="1")
    c = app.test_client()
    create_admin(app, c)
    _age_session(c, 50)
    assert c.get("/api/jobs", headers={"X-Background": "1"}).status_code == 200
    with c.session_transaction() as s:
        assert int(time.time()) - s["seen"] >= 50                 # not refreshed by the poll
    assert c.get("/projects").status_code == 200                 # a real page view refreshes it
    with c.session_transaction() as s:
        assert int(time.time()) - s["seen"] < 5
    _age_session(c, 61)
    assert c.get("/api/jobs", headers={"X-Background": "1"}).status_code == 401


def test_keep_me_signed_in_uses_the_longer_limit(make_app):
    app = make_app(BOQ_IDLE_MINUTES="1", BOQ_REMEMBER_DAYS="7")
    create_admin(app, app.test_client())
    c = app.test_client()
    assert login(c, "admin", ADMIN_PW, remember=True).status_code == 302
    _age_session(c, 3600)
    assert c.get("/projects").status_code == 200
    _age_session(c, 8 * 86400)
    assert c.get("/projects").status_code == 302


# ---------------------------------------------------------------------- session versioning
def test_password_change_ends_other_sessions(app, admin_client):
    add_user(admin_client, "nour")
    a, b = app.test_client(), app.test_client()
    login(a, "nour", "user-pass-123")
    login(b, "nour", "user-pass-123")
    r = a.post("/account", data=dict(_csrf=csrf_of(a), current="user-pass-123", password="changed-789",
                                     password2="changed-789"))
    assert r.status_code == 302
    assert a.get("/projects").status_code == 200                 # the session that changed it stays
    assert b.get("/projects").status_code == 302                 # the other one is signed out


def test_disabled_user_is_signed_out(app, admin_client):
    add_user(admin_client, "kareem")
    c = app.test_client()
    login(c, "kareem", "user-pass-123")
    uid = app.config["DB"].one("SELECT id FROM users WHERE username='kareem'")["id"]
    admin_client.post("/admin/users", data=dict(_csrf=csrf_of(admin_client), action="disable", uid=uid))
    assert c.get("/projects").status_code == 302


# ---------------------------------------------------------------------- redirects / cookies
def test_next_parameter_cannot_leave_the_site(app):
    create_admin(app, app.test_client())
    for bad in ("//evil.example/", "/\\evil.example", "https://evil.example/"):
        c = app.test_client()
        r = c.post(f"/login?next={bad}", data=dict(_csrf=csrf_of(c), username="admin", password=ADMIN_PW))
        assert r.headers["Location"] == "/projects", bad
    c = app.test_client()
    r = c.post("/login?next=/new", data=dict(_csrf=csrf_of(c), username="admin", password=ADMIN_PW))
    assert r.headers["Location"] == "/new"


def test_session_cookie_flags(app):
    c = app.test_client()
    r = c.get("/login")
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith("boq2rfq_session=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_existing_database_gets_new_column(tmp_path):
    """An instance/boq2rfq.db from before this change upgrades itself on start-up (users keep their accounts)."""
    import sqlite3

    from webapp.db import DB
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
              "full_name TEXT NOT NULL DEFAULT '', pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', "
              "active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, last_login TEXT)")
    c.execute("INSERT INTO users (username, pw_hash, created) VALUES ('old', 'h', 'x')")
    c.commit()
    c.close()
    db = DB(p)
    row = db.one("SELECT username, ses_ver FROM users")
    assert row["username"] == "old" and row["ses_ver"] == 0
    assert db.one("SELECT COUNT(*) n FROM login_failures")["n"] == 0
