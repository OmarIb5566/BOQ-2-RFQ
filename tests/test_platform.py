"""Phase 1: job queue + workers, roles and membership, optimistic locking, presence, LDAP adapter, settings file,
path rebasing, migrations, backup / restore with checksums and rollback."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from conftest import ROOT, add_user, api_post, create_admin, csrf_of, login, run_id_from, wait_job


# ---------------------------------------------------------------------- job queue (no worker thread: run by hand)
@pytest.fixture
def rt(settings, make_app):
    app = make_app()
    return app.config["RUNTIME"]


@pytest.fixture
def quiet_app(settings, monkeypatch):
    """An app WITHOUT the inline worker: jobs only run when a test says so."""
    from webapp import create_app
    return create_app(settings, start_worker=False)


def test_job_runs_and_reports(quiet_app):
    rt, jobs = quiet_app.config["RUNTIME"], quiet_app.config["JOBS"]
    jid = jobs.submit("noop", "Test", "", None, {"steps": 3})
    assert jobs.get(jid)["status"] == "queued"
    wid = jobs.register_worker("test")
    assert jobs.run_one(wid, rt) is True
    j = jobs.get(jid)
    assert j["status"] == "done" and j["message"] == "ok" and j["progress"] == 1
    assert jobs.run_one(wid, rt) is False                       # queue empty


def test_unknown_job_kind_is_refused(quiet_app):
    with pytest.raises(ValueError):
        quiet_app.config["JOBS"].submit("no-such-task", "x")


def test_failed_job_is_not_retried_but_transient_is(quiet_app):
    rt, jobs, db = quiet_app.config["RUNTIME"], quiet_app.config["JOBS"], quiet_app.config["DB"]
    wid = jobs.register_worker("test")
    bad = jobs.submit("noop", "Bad", "", None, {"fail": "broken input"})
    jobs.run_one(wid, rt)
    assert jobs.get(bad)["status"] == "failed" and "broken input" in jobs.get(bad)["error"]
    tr = jobs.submit("noop", "Flaky", "", None, {"transient": True}, max_attempts=3)
    jobs.run_one(wid, rt)
    j = dict(db.one("SELECT * FROM jobs WHERE id=?", (tr,)))
    assert j["status"] == "queued" and j["not_before"] > time.time() and j["attempts"] == 1
    db.x("UPDATE jobs SET not_before=0 WHERE id=?", (tr,))
    jobs.run_one(wid, rt)
    db.x("UPDATE jobs SET not_before=0 WHERE id=?", (tr,))
    jobs.run_one(wid, rt)
    assert jobs.get(tr)["status"] == "failed"                    # gave up after max_attempts


def test_crash_recovery_requeues_then_interrupts(quiet_app):
    jobs, db = quiet_app.config["JOBS"], quiet_app.config["DB"]
    jid = jobs.submit("noop", "Crashy", "", None, max_attempts=2)
    db.x("UPDATE jobs SET status='running', attempts=1, heartbeat=? WHERE id=?", (time.time() - 600, jid))
    assert jobs.recover() == 1
    assert jobs.get(jid)["status"] == "queued"
    db.x("UPDATE jobs SET status='running', attempts=2, heartbeat=? WHERE id=?", (time.time() - 600, jid))
    jobs.recover()
    assert jobs.get(jid)["status"] == "interrupted"
    fresh = jobs.submit("noop", "Alive", "", None)
    db.x("UPDATE jobs SET status='running', heartbeat=? WHERE id=?", (time.time(), fresh))
    jobs.recover()
    assert jobs.get(fresh)["status"] == "running"                # a live job is left alone


def test_cancel(quiet_app):
    rt, jobs = quiet_app.config["RUNTIME"], quiet_app.config["JOBS"]
    jid = jobs.submit("noop", "x")
    assert jobs.cancel(jid) == "cancelled"
    assert jobs.run_one(jobs.register_worker("t"), rt) is False
    assert jobs.retry(jid) and jobs.get(jid)["status"] == "queued"


def test_fair_queue_takes_turns_between_users(quiet_app):
    rt, jobs, db = quiet_app.config["RUNTIME"], quiet_app.config["JOBS"], quiet_app.config["DB"]
    for _ in range(3):
        jobs.submit("noop", "A", "", 1, {"steps": 1, "sleep": 0})
        time.sleep(0.01)
    jobs.submit("noop", "B", "", 2, {"steps": 1, "sleep": 0})
    wid = jobs.register_worker("t")
    order = []
    for _ in range(4):
        before = {r["id"] for r in db.q("SELECT id FROM jobs WHERE status='done'")}
        jobs.run_one(wid, rt)
        done = [r for r in db.q("SELECT * FROM jobs WHERE status='done'") if r["id"] not in before]
        order.append(done[0]["user_id"])
        time.sleep(1.1)                                         # started timestamps have 1 s resolution
    assert order[:2] == [1, 2]                                   # B does not wait behind all of A's jobs


def test_eta_from_history(quiet_app):
    jobs, db = quiet_app.config["JOBS"], quiet_app.config["DB"]
    for s in (10, 20, 30):
        jid = jobs.submit("noop", "x")
        db.x("UPDATE jobs SET status='done', seconds=?, finished=? WHERE id=?", (s, "2026-01-01T00:00:00", jid))
    q = jobs.submit("noop", "y")
    assert jobs.get(q)["eta_s"] == 20


def test_external_worker_process_runs_jobs(env_for_subprocess, tmp_path):
    """worker.py as its own process picks up a job submitted to the shared database."""
    from boq_agent.config import Settings
    from webapp import create_app
    app = create_app(Settings(workspace=tmp_path / "data" / "workspace", use_llm=False), start_worker=False)
    jid = app.config["JOBS"].submit("noop", "From the web", "", None, {"steps": 2})
    p = subprocess.Popen([sys.executable, "worker.py"], cwd=ROOT, env=env_for_subprocess,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        for _ in range(100):
            if app.config["JOBS"].get(jid)["status"] == "done":
                break
            time.sleep(0.2)
        assert app.config["JOBS"].get(jid)["status"] == "done"
        assert app.config["JOBS"].worker_status()                 # it registered itself
    finally:
        p.terminate()
        out = p.communicate(timeout=20)[0]
    assert "Traceback" not in out, out


@pytest.fixture
def env_for_subprocess(tmp_path):
    e = {k: v for k, v in os.environ.items() if not k.startswith("BOQ_")}
    e.update(BOQ_WORKSPACE=str(tmp_path / "data" / "workspace"), PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return e


# ---------------------------------------------------------------------- roles and membership
def _project(app, client, villa_xlsx, shared=True):
    data = {"_csrf": csrf_of(client), "name": "Villa", "files": [(io.BytesIO(villa_xlsx.read_bytes()), "villa.xlsx")]}
    if shared:
        data["shared"] = "on"
    r = client.post("/new", data=data, content_type="multipart/form-data")
    run = run_id_from(r.headers["Location"])
    assert wait_job(app, run)["status"] == "done"
    return run


def test_private_project_visible_only_to_owner_members_and_managers(app, admin_client, villa_xlsx):
    for name, role in (("eng", "engineer"), ("eng2", "engineer"), ("mgr", "manager"), ("view", "viewer")):
        add_user(admin_client, name, role=role)
    eng = app.test_client()
    login(eng, "eng", "user-pass-123")
    run = _project(app, eng, villa_xlsx, shared=False)
    clients = {}
    for n in ("eng2", "mgr", "view"):
        clients[n] = app.test_client()
        login(clients[n], n, "user-pass-123")
    assert clients["eng2"].get(f"/p/{run}/packages").status_code == 403
    assert clients["view"].get(f"/p/{run}/packages").status_code == 403
    assert clients["mgr"].get(f"/p/{run}/packages").status_code == 200
    assert run not in clients["eng2"].get("/projects").text
    # add eng2 as a viewer-member: may read, may not change
    uid = app.config["DB"].one("SELECT id FROM users WHERE username='eng2'")["id"]
    eng.post(f"/p/{run}/members", data={"_csrf": csrf_of(eng), "action": "add", "user_id": uid, "role": "viewer"})
    assert clients["eng2"].get(f"/p/{run}/packages").status_code == 200
    r = api_post(clients["eng2"], f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "x"})
    assert r.status_code == 403
    eng.post(f"/p/{run}/members", data={"_csrf": csrf_of(eng), "action": "add", "user_id": uid, "role": "editor"})
    assert api_post(clients["eng2"], f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "Concrete A"}).status_code == 200
    # the member got a notification
    n = app.config["DB"].one("SELECT COUNT(*) n FROM notifications WHERE user_id=?", (uid,))["n"]
    assert n >= 1


def test_viewer_cannot_change_shared_project_or_create(app, admin_client, villa_xlsx):
    run = _project(app, admin_client, villa_xlsx)
    add_user(admin_client, "vv", role="viewer")
    v = app.test_client()
    login(v, "vv", "user-pass-123")
    assert v.get(f"/p/{run}/packages").status_code == 200
    assert api_post(v, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "x"}).status_code == 403
    assert v.get("/new").status_code == 403
    assert api_post(v, f"/api/p/{run}/comments", {"target": "project", "body": "Looks fine"}).status_code == 200


def test_old_user_role_is_migrated(tmp_path):
    from webapp.db import DB
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL DEFAULT '', pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
        active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL, last_login TEXT);
        INSERT INTO users (username, pw_hash, role, created) VALUES ('a','h','admin','x'), ('b','h','user','x');""")
    c.commit()
    c.close()
    db = DB(p)
    assert {r["username"]: r["role"] for r in db.q("SELECT * FROM users")} == {"a": "admin", "b": "engineer"}
    from webapp.db import LATEST
    assert db.version() == LATEST
    assert db.migrate() == []                                    # second start: nothing to do


# ---------------------------------------------------------------------- optimistic locking + presence
def test_stale_change_is_refused_with_who_changed_it(app, admin_client, villa_xlsx):
    run = _project(app, admin_client, villa_xlsx)
    add_user(admin_client, "sara", role="engineer")
    sara = app.test_client()
    login(sara, "sara", "user-pass-123")
    v = app.config["STORE"].version(run)
    r = api_post(admin_client, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "Mine"}, headers={"X-Version": str(v)})
    assert r.status_code == 200 and r.get_json()["version"] == v + 1
    r = api_post(sara, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "Hers"}, headers={"X-Version": str(v)})
    assert r.status_code == 409 and r.get_json()["conflict"] and "Admin User" in r.get_json()["error"]
    r = api_post(sara, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "Hers"}, headers={"X-Version": str(v + 1)})
    assert r.status_code == 200


def test_presence_shows_others(app, admin_client, villa_xlsx):
    run = _project(app, admin_client, villa_xlsx)
    add_user(admin_client, "omar2", role="engineer")
    o = app.test_client()
    login(o, "omar2", "user-pass-123")
    api_post(o, f"/api/p/{run}/presence", {"page": "packages"})
    r = api_post(admin_client, f"/api/p/{run}/presence", {"page": "cases"}).get_json()
    assert [x["name"] for x in r["others"]] == ["Omar2"] and r["others"][0]["page"] == "packages"


# ---------------------------------------------------------------------- audit log
def test_changes_are_audited(app, admin_client, villa_xlsx):
    run = _project(app, admin_client, villa_xlsx)
    api_post(admin_client, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "X"})
    acts = [r["action"] for r in app.config["DB"].q("SELECT action FROM audit WHERE run_id=?", (run,))]
    assert "api_rename_wp" in acts and "project created" in acts
    assert "Who did what" in admin_client.get(f"/p/{run}/activity").text


# ---------------------------------------------------------------------- LDAP adapter (fake directory)
class FakeDir:
    def __init__(self, users):
        self.users = users

    def __call__(self, url, user_dn, password):
        u = self.users.get(user_dn)
        if not u or u["pw"] != password:
            return None
        outer = u

        class C:
            def find_user(self, base, flt):
                return {"displayName": outer["name"], "memberOf": outer["groups"]}
        return C()


def test_ldap_sign_in_maps_groups_to_roles(make_app, monkeypatch):
    from webapp import identity
    app = make_app(BOQ_AUTH="ldap+local", BOQ_LDAP_URL="ldaps://dc", BOQ_LDAP_USER_DN="{username}@corp",
                   BOQ_LDAP_ROLE_MAP="CN=Mgr,DC=corp=manager; CN=Eng,DC=corp=engineer")
    monkeypatch.setattr(identity, "_CONNECT", FakeDir({
        "nour@corp": {"pw": "dir-pass", "name": "Nour Hassan", "groups": ["CN=Eng,DC=corp", "CN=Mgr,DC=corp"]},
        "guest@corp": {"pw": "dir-pass", "name": "Guest", "groups": ["CN=Other,DC=corp"]}}))
    create_admin(app, app.test_client())
    c = app.test_client()
    assert login(c, "nour", "dir-pass").status_code == 302
    u = app.config["DB"].one("SELECT * FROM users WHERE username='nour'")
    assert u["role"] == "manager" and u["full_name"] == "Nour Hassan" and u["pw_hash"] == identity.LDAP_MARK
    assert login(app.test_client(), "guest", "dir-pass").status_code == 200     # in no mapped group
    assert login(app.test_client(), "nour", "wrong").status_code == 200
    assert login(app.test_client(), "admin", "admin-pass-123").status_code == 302  # local fallback


def test_ldap_only_mode_refuses_local_passwords(make_app, monkeypatch):
    from webapp import identity
    app = make_app()
    create_admin(app, app.test_client())
    monkeypatch.setenv("BOQ_AUTH", "ldap")
    monkeypatch.setenv("BOQ_LDAP_URL", "ldaps://dc")
    monkeypatch.setattr(identity, "_CONNECT", FakeDir({}))
    assert login(app.test_client(), "admin", "admin-pass-123").status_code == 200


def test_ldap_filter_escaping():
    from webapp.identity import _escape
    assert _escape("a*)(cn=*") == "a\\2a\\29\\28cn=\\2a"


# ---------------------------------------------------------------------- settings file
def test_settings_file_feeds_environment(tmp_path, monkeypatch):
    from boq_agent import settings_file
    f = tmp_path / "boq2rfq.toml"
    f.write_text('[server]\nBOQ_IDLE_MINUTES = 15\nBOQ_HTTPS = true\n[ai]\nOLLAMA_URL = "http://10.0.0.9:11434"\n',
                 encoding="utf-8")
    monkeypatch.delenv("BOQ_IDLE_MINUTES", raising=False)
    monkeypatch.delenv("BOQ_HTTPS", raising=False)
    monkeypatch.setenv("OLLAMA_URL", "http://env-wins:11434")
    got = settings_file.load(f)
    assert os.environ["BOQ_IDLE_MINUTES"] == "15" and os.environ["BOQ_HTTPS"] == "1"
    assert os.environ["OLLAMA_URL"] == "http://env-wins:11434" and "OLLAMA_URL" not in got


# ---------------------------------------------------------------------- moved data folders
def test_paths_follow_a_moved_workspace(app, admin_client, villa_xlsx, tmp_path):
    from webapp.store import rebase_paths
    run = _project(app, admin_client, villa_xlsx)
    s = app.config["STORE"].load(run)
    old_root = Path("/old/server/workspace") / run
    s.document.files[0].work_path = str(old_root / "F1" / "x.xlsx")
    n = rebase_paths(s, app.config["STORE"].run_dir(run))
    assert n >= 1
    assert s.document.files[0].work_path == str(app.config["STORE"].run_dir(run) / "F1" / "x.xlsx")


# ---------------------------------------------------------------------- backup / restore
def test_backup_verify_restore_roundtrip(app, admin_client, villa_xlsx, tmp_path, monkeypatch, settings):
    import manage
    run = _project(app, admin_client, villa_xlsx)
    app.config["JOBS"]._threads.clear()
    monkeypatch.setenv("BOQ_WORKSPACE", str(settings.workspace))
    out = tmp_path / "b.zip"
    man = manage.backup(out, quiet=True)
    assert man["tables"]["projects"]["rows"] == 1 and any(run in f for f in man["files"])
    assert manage.verify(out, quiet=True) == []
    # tamper with the backup -> verify catches it, restore refuses
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(out) as zi, zipfile.ZipFile(bad, "w") as zo:
        for i in zi.infolist():
            data = zi.read(i)
            if i.filename.endswith("session.json"):
                data = data.replace(b'"', b"'", 1)
            zo.writestr(i, data)
    assert manage.verify(bad, quiet=True)
    with pytest.raises(SystemExit):
        manage.restore(bad, yes=True)
    # while the app still has the database open, restore refuses cleanly (Windows locks open files)
    app.config["DB"].x("DELETE FROM projects")
    if os.name == "nt":
        with pytest.raises(SystemExit, match="in use"):
            manage.restore(out, yes=True)
        assert (Path(app.instance_path) / "boq2rfq.db").exists()       # nothing was moved
    # "stop the server": close this test's connection and the worker thread's
    stop_app(app)
    manage.restore(out, yes=True)
    from webapp.db import DB
    db2 = DB(Path(app.instance_path) / "boq2rfq.db")
    assert db2.one("SELECT COUNT(*) n FROM projects")["n"] == 1
    kept = [p for p in Path(app.instance_path).parent.iterdir() if ".before-restore-" in p.name]
    assert len(kept) == 2


def stop_app(app) -> None:
    """Simulate stopping the server: end the inline worker loop and close every open database connection."""
    import gc
    app.config["DB"].close()
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass


def test_json_export_import(app, admin_client, villa_xlsx, tmp_path, monkeypatch, settings):
    import manage
    _project(app, admin_client, villa_xlsx)
    monkeypatch.setenv("BOQ_WORKSPACE", str(settings.workspace))
    out = tmp_path / "d.json"
    manage.export_json(out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["tables"]["users"] and data["checksums"]["users"]
    with pytest.raises(SystemExit, match="not empty"):
        manage.import_json(out)
    monkeypatch.setenv("BOQ_WORKSPACE", str(tmp_path / "fresh" / "workspace"))
    manage.import_json(out)
    from webapp.db import DB
    db = DB(tmp_path / "fresh" / "instance" / "boq2rfq.db")
    assert db.one("SELECT COUNT(*) n FROM projects")["n"] == 1
    data["tables"]["users"][0]["full_name"] = "tampered"
    out.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("BOQ_WORKSPACE", str(tmp_path / "fresh2" / "workspace"))
    with pytest.raises(SystemExit, match="Checksum"):
        manage.import_json(out)


def test_manage_reset_password_unlocks(app, admin_client, monkeypatch, settings):
    import manage
    for _ in range(6):
        login(app.test_client(), "admin", "bad")
    monkeypatch.setenv("BOQ_WORKSPACE", str(settings.workspace))
    manage.reset_password("admin", "brand-new-pass")
    assert login(app.test_client(), "admin", "brand-new-pass").status_code == 302
