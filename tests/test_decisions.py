"""Special-case decisions remembered across projects, and the page to change them."""
from __future__ import annotations

import io

from conftest import add_user, api_post, csrf_of, login, make_xlsx, run_id_from, wait_job


def upload(app, client, path, name="Villa"):
    r = client.post("/new", data={"_csrf": csrf_of(client), "name": name, "shared": "on",
                                  "files": [(io.BytesIO(path.read_bytes()), path.name)]},
                    content_type="multipart/form-data")
    run = run_id_from(r.headers["Location"])
    job = wait_job(app, run)
    assert job["status"] == "done", job["error"]
    return run, job


def session(app, run):
    return app.config["STORE"].load(run)


def decide_all(client, app, run, decision):
    ids = [p.proposal_id for p in session(app, run).plan.proposals]
    assert ids
    assert api_post(client, f"/api/p/{run}/decide", {"ids": ids, "decision": decision}).status_code == 200
    return ids


def test_decisions_carry_to_a_different_project(app, admin_client, villa_xlsx, tmp_path):
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    assert all(p.decision == "pending" for p in session(app, a).plan.proposals)     # nothing remembered yet
    decide_all(admin_client, app, a, "denied")
    b, job = upload(app, admin_client, make_xlsx(tmp_path / "other.xlsx"), "B")
    s = session(app, b)
    assert all(p.decision == "denied" for p in s.plan.proposals)
    assert "decided as before" in job["message"]
    assert set(s.remembered) == {p.proposal_id for p in s.plan.proposals}
    note = next(iter(s.remembered.values()))
    assert note["from_run"] == a and note["match"] == "same case" and note["from_name"] == "A"
    page = admin_client.get(f"/p/{b}/cases").text
    assert "decided automatically" in page and "Remembered" in page and "“A”" in page


def test_changing_a_remembered_decision_in_a_project(app, admin_client, villa_xlsx, tmp_path):
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    decide_all(admin_client, app, a, "denied")
    b, _ = upload(app, admin_client, make_xlsx(tmp_path / "b.xlsx"), "B")
    ids = decide_all(admin_client, app, b, "approved")                # the user disagrees in project B
    s = session(app, b)
    assert not s.remembered and all(p.decision == "approved" for p in s.plan.proposals if p.proposal_id in ids)
    c, _ = upload(app, admin_client, make_xlsx(tmp_path / "c.xlsx"), "C")
    assert all(p.decision == "approved" for p in session(app, c).plan.proposals)   # the latest decision wins


def test_rule_level_fallback(app, admin_client, villa_xlsx):
    from webapp.db import new_id, now
    db = app.config["DB"]
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    p = session(app, a).plan.proposals[0]
    db.x("INSERT INTO decision_memory (id, ts, user_id, run_id, rule_id, case_key, decision, title) "
         "VALUES (?,?,?,?,?,?,?,?)", (new_id(), now(), 1, "elsewhere", p.rule_id, f"{p.rule_id}|OTHER|OTHER",
                                       "approved", "a similar case"))
    b, _ = upload(app, admin_client, villa_xlsx, "B")
    s = session(app, b)
    q = next(x for x in s.plan.proposals if x.rule_id == p.rule_id)
    assert q.decision == "approved" and s.remembered[q.proposal_id]["match"] == "same rule"


def test_revision_keeps_its_own_decisions_first(app, admin_client, villa_xlsx, tmp_path):
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    decide_all(admin_client, app, a, "denied")
    other, _ = upload(app, admin_client, make_xlsx(tmp_path / "o.xlsx"), "Other")
    decide_all(admin_client, app, other, "approved")                  # a newer decision elsewhere
    r = admin_client.post(f"/p/{a}/revise", data={"_csrf": csrf_of(admin_client), "use_ai": "",
                                                   "files": [(io.BytesIO(villa_xlsx.read_bytes()), "villa.xlsx")]},
                          content_type="multipart/form-data")
    rev = run_id_from(r.headers["Location"])
    assert wait_job(app, rev)["status"] == "done"
    s = session(app, rev)
    assert all(p.decision == "denied" for p in s.plan.proposals)      # its own project's decisions, not the newer one
    assert not s.remembered


def test_decisions_page_change_and_forget(app, admin_client, villa_xlsx, tmp_path):
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    decide_all(admin_client, app, a, "denied")
    page = admin_client.get("/decisions").text
    assert "Remembered decisions" in page and "Decision for new projects" in page
    key = app.config["DB"].one("SELECT case_key FROM decision_memory LIMIT 1")["case_key"]
    assert api_post(admin_client, "/api/decisions", {"key": key, "decision": "approved"}).status_code == 200
    b, _ = upload(app, admin_client, make_xlsx(tmp_path / "b.xlsx"), "B")
    s = session(app, b)
    changed = [p for p in s.plan.proposals if f"{p.rule_id}|{p.approve_key}|{p.deny_key}" == key]
    assert changed and changed[0].decision == "approved"
    assert s.remembered[changed[0].proposal_id]["from_name"] == "Remembered decisions"
    for k in [r["case_key"] for r in app.config["DB"].q("SELECT DISTINCT case_key FROM decision_memory")]:
        assert api_post(admin_client, "/api/decisions", {"key": k, "forget": True}).status_code == 200
    c, _ = upload(app, admin_client, make_xlsx(tmp_path / "c.xlsx"), "C")
    assert all(p.decision == "pending" for p in session(app, c).plan.proposals)
    assert "Nothing remembered yet" in admin_client.get("/decisions").text


def test_viewer_sees_but_cannot_change(app, admin_client, villa_xlsx):
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    decide_all(admin_client, app, a, "denied")
    add_user(admin_client, "vw", role="viewer")
    v = app.test_client()
    login(v, "vw", "user-pass-123")
    page = v.get("/decisions").text
    assert "data-post=\"/api/decisions\"" not in page
    key = app.config["DB"].one("SELECT case_key FROM decision_memory LIMIT 1")["case_key"]
    assert api_post(v, "/api/decisions", {"key": key, "decision": "approved"}).status_code == 403


def test_switch_off(app, admin_client, villa_xlsx, tmp_path):
    a, _ = upload(app, admin_client, villa_xlsx, "A")
    decide_all(admin_client, app, a, "denied")
    from webapp import fleet
    c = fleet.config(app.config["DB"])
    c["decisions_on"] = False
    app.config["DB"].set_setting("models", c)
    b, _ = upload(app, admin_client, make_xlsx(tmp_path / "b.xlsx"), "B")
    assert all(p.decision == "pending" for p in session(app, b).plan.proposals)
    assert "switched off" in admin_client.get("/decisions").text
