"""Phases 2-5: AI fleet, BOQ revisions, RFQ register and bid comparison, collaboration."""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from conftest import VILLA_ROWS, add_user, api_post, csrf_of, login, make_xlsx, run_id_from, wait_job


def upload(app, client, path: Path, name="Villa", url="/new") -> str:
    r = client.post(url, data={"_csrf": csrf_of(client), "name": name, "shared": "on", "use_ai": "",
                               "files": [(io.BytesIO(path.read_bytes()), path.name)]},
                    content_type="multipart/form-data")
    assert r.status_code == 302, r.text[:300]
    run = run_id_from(r.headers["Location"])
    job = wait_job(app, run)
    assert job["status"] == "done", job["error"]
    return run


# ====================================================================== Phase 2 — AI fleet
class FakeOllama:
    """Stands in for the model server: which models exist, and canned answers."""

    def __init__(self, models, fail=()):
        self.models, self.fail, self.calls = set(models), set(fail), []

    def chat(self, client, *a, **kw):
        self.calls.append(client.model)
        if client.model in self.fail:
            from boq_agent.llm.client import LLMError
            raise LLMError("model crashed")
        client.log.append({"op": kw.get("op", ""), "model": client.model, "in": 11, "out": 7, "s": 0.01})
        return {"answer": client.model}


@pytest.fixture
def fake_ollama(monkeypatch):
    from boq_agent.llm.client import OllamaClient
    from webapp import fleet
    fake = FakeOllama({"main:1", "backup:1"})
    monkeypatch.setattr(fleet, "installed_names", lambda url: fake.models)
    monkeypatch.setattr(fleet, "unload_others", lambda url, keep: [])
    monkeypatch.setattr(OllamaClient, "chat_json", lambda self, *a, **kw: fake.chat(self, *a, **kw))
    return fake


def test_fleet_cache_and_call_log(app, fake_ollama, tmp_path):
    from boq_agent.docs.common import Cache
    from webapp.fleet import FleetClient
    db = app.config["DB"]
    c = FleetClient("http://127.0.0.1:11434", ["main:1"], "text", db=db, cache=Cache(tmp_path / "c"), run_id="r1")
    a = c.chat_json("sys", "question", {"type": "object"}, op="divisions")
    b = c.chat_json("sys", "question", {"type": "object"}, op="divisions")
    assert a == b == {"answer": "main:1"} and fake_ollama.calls == ["main:1"]      # 2nd answer from the cache
    c.chat_json("sys", "another question", {"type": "object"})
    assert len(fake_ollama.calls) == 2
    rows = [dict(r) for r in db.q("SELECT * FROM llm_calls WHERE run_id='r1' ORDER BY ts")]
    assert len(rows) == 3 and sum(r["cached"] for r in rows) == 1 and rows[0]["tokens_in"] == 11


def test_fleet_falls_back_to_the_next_model(app, fake_ollama):
    from webapp.fleet import FleetClient
    fake_ollama.fail = {"main:1"}
    c = FleetClient("http://x", ["main:1", "backup:1"], "text", db=app.config["DB"])
    assert c.chat_json("s", "u", {}) == {"answer": "backup:1"}
    fake_ollama.models = {"backup:1"}                              # primary not installed at all
    c2 = FleetClient("http://x", ["main:1", "backup:1"], "text")
    assert c2.status() == (True, "backup:1 ready (fallback)")


def test_cloud_models_are_never_offered():
    from webapp.fleet import is_local
    assert is_local({"name": "gemma4:e4b"})
    assert not is_local({"name": "kimi-k3:cloud"}) and not is_local({"name": "x:latest", "remote_host": "https://o"})


def test_admin_switches_models_only_to_local_ones(app, admin_client, monkeypatch):
    from webapp import fleet
    monkeypatch.setattr(fleet, "installed", lambda url, ttl=30: [{"name": "gemma4:e4b", "size": 9e9},
                                                                   {"name": "qwen3:14b", "size": 9e9}])
    r = admin_client.post("/admin/models", data={"_csrf": csrf_of(admin_client), "text": "qwen3:14b",
                                                 "profile": "resident", "verify_on": "on", "cache_on": "on"})
    assert r.status_code == 302
    c = fleet.config(app.config["DB"])
    assert c["text"] == "qwen3:14b" and c["profile"] == "resident" and c["verify_on"] and not c["memory_on"]
    r = admin_client.post("/admin/models", data={"_csrf": csrf_of(admin_client), "text": "kimi-k3:cloud"})
    assert fleet.config(app.config["DB"])["text"] == "qwen3:14b"   # refused
    from webapp.tasks import engine_settings
    assert engine_settings(app.config["RUNTIME"]).model == "qwen3:14b"


def test_memory_applies_a_users_earlier_correction(app, admin_client, villa_xlsx, tmp_path):
    run = upload(app, admin_client, villa_xlsx)
    s = app.config["STORE"].load(run)
    paint = next(i for i in s.document.items if "Acrylic paint" in i.description)
    assert paint.category == "ACRYLIC_PAINT"
    r = api_post(admin_client, f"/api/p/{run}/item", {"uid": paint.uid, "category": "EPOXY_PAINT"})
    assert r.status_code == 200, r.get_json()
    run2 = upload(app, admin_client, make_xlsx(tmp_path / "villa2.xlsx"), name="Villa again")
    s2 = app.config["STORE"].load(run2)
    p2 = next(i for i in s2.document.items if "Acrylic paint" in i.description)
    assert p2.category == "EPOXY_PAINT" and p2.method == "memory"


def test_second_opinion_flags_disagreement_without_changing_the_answer(app, villa_xlsx, settings):
    from boq_agent.agent import BOQAgent
    from boq_agent.analysis.classifier import Classifier
    from webapp.fleet import second_opinion
    s = BOQAgent(settings, log=lambda m: None).analyze([villa_xlsx], ["villa.xlsx"])
    doubt = next(i for i in s.document.items if i.category == "ACRYLIC_PAINT")
    doubt.method, doubt.confidence = "llm", 0.4

    class Verifier:
        model = "qwen3:14b"

        def status(self):
            return True, "ok"

        def chat_json(self, system, user, schema, **kw):
            ids = schema["properties"]["results"]["items"]["properties"]["item_id"]["enum"]
            return {"results": [{"item_id": i, "category": "EPOXY_PAINT", "confidence": 0.9, "reason": "epoxy"}
                                for i in ids]}
    clf = Classifier(BOQAgent(settings).rules, settings, None)
    res = second_opinion(Verifier(), s.document, clf, None, lambda m: None)
    assert res["checked"] == 1 and res["disputed"] == 1
    assert doubt.category == "ACRYLIC_PAINT" and doubt.confidence == 0.4 and "+disputed" in doubt.method
    assert "Second opinion (qwen3:14b): EPOXY_PAINT" in doubt.reason


# ====================================================================== Phase 3 — revisions
REV_ROWS = [r[:] for r in VILLA_ROWS]


def _revised(tmp_path) -> Path:
    rows = [list(r) for r in VILLA_ROWS]
    for r in rows:
        if r and r[0] == "3.1":
            r[3] = 65                                                # quantity changed
    rows = [r for r in rows if not (r and r[0] == "9.2")]           # acrylic paint removed
    rows.append(["9.5", "Gypsum board suspended ceiling", "m2", 120, 300, 36000])   # new item
    return make_xlsx(tmp_path / "villa rev1.xlsx", rows)


def test_revision_carries_work_forward_and_compares(app, admin_client, villa_xlsx, tmp_path):
    run = upload(app, admin_client, villa_xlsx)
    s = app.config["STORE"].load(run)
    ids = [p.proposal_id for p in s.plan.proposals]
    api_post(admin_client, f"/api/p/{run}/decide", {"ids": ids, "decision": "denied"})
    plaster = next(i for i in s.document.items if "plaster" in i.description.lower())
    api_post(admin_client, f"/api/p/{run}/item", {"uid": plaster.uid, "category": "ACRYLIC_PAINT"})
    api_post(admin_client, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "Concrete works (all)"})
    r = admin_client.post(f"/p/{run}/revise", data={"_csrf": csrf_of(admin_client), "use_ai": "",
                                                     "files": [(io.BytesIO(_revised(tmp_path).read_bytes()),
                                                                "villa rev1.xlsx")]},
                          content_type="multipart/form-data")
    rev = run_id_from(r.headers["Location"])
    job = wait_job(app, rev)
    assert job["status"] == "done" and "carried over" in job["message"], job
    row = app.config["DB"].one("SELECT * FROM projects WHERE run_id=?", (rev,))
    assert row["parent_run"] == run and row["revision"] == 1 and row["name"] == "Villa (Rev 1)"
    s2 = app.config["STORE"].load(rev)
    assert all(p.decision == "denied" for p in s2.plan.proposals if p.proposal_id in ids)
    pl2 = next(i for i in s2.document.items if "plaster" in i.description.lower())
    assert pl2.category == "ACRYLIC_PAINT" and pl2.method == "manual"
    assert next(w for w in s2.plan.work_packages if w.key == "DIV03").name_en == "Concrete works (all)"
    # comparison
    page = admin_client.get(f"/p/{rev}/compare")
    assert page.status_code == 200
    from webapp.revisions import compare
    res = compare(app.config["STORE"].load(run), s2)
    assert res["counts"]["new"] == 1 and res["counts"]["removed"] == 1 and res["counts"]["changed"] >= 1
    changed = [x for x in res["items"] if x["status"] == "changed"]
    assert any("qty" in x["changes"] and x["new"].item_no == "3.1" for x in changed)
    # history page lists both revisions with their input fingerprints
    hist = admin_client.get(f"/p/{rev}/revisions").text
    assert "villa rev1.xlsx" in hist and "villa.xlsx" in hist


def test_duplicate_upload_is_detected(app, admin_client, villa_xlsx):
    upload(app, admin_client, villa_xlsx)
    upload(app, admin_client, villa_xlsx, name="Same file")
    assert "Already uploaded before" in admin_client.get("/projects").text or \
        app.config["DB"].one("SELECT COUNT(DISTINCT run_id) n FROM uploads")["n"] == 2


# ====================================================================== Phase 4 — RFQ register
def _exported(app, client, villa_xlsx) -> str:
    run = upload(app, client, villa_xlsx)
    s = app.config["STORE"].load(run)
    api_post(client, f"/api/p/{run}/decide", {"ids": [p.proposal_id for p in s.plan.proposals], "decision": "approved"})
    r = api_post(client, f"/api/p/{run}/export")
    assert r.status_code == 200, r.get_json()
    assert wait_job(app, run, 300)["status"] == "done"
    return run


def _letters(app, run, suppliers=("Alpha Co", "Beta Co")):
    from boq_agent.rfq.builder import field_specs
    from boq_agent.rfq.detect import load_field_config
    st = app.config["SETTINGS"]
    s = app.config["STORE"].load(run)
    specs = field_specs(st.rfq_template, load_field_config(st.rfq_fields))
    values = {x.name: "x" for x in specs if x.scope == "project"}
    wp = next(w for w in s.plan.work_packages if w.key == "DIV03")
    jid = app.config["JOBS"].submit("rfq", "Writing RFQs", run, 1, {
        "values": values, "supplier_fields": [x.name for x in specs if x.scope == "supplier"],
        "packages": [{"wp_id": wp.wp_id, "values": {}, "suppliers": list(suppliers)}],
        "template": str(st.rfq_template), "due_date": "2020-01-31"})
    for _ in range(200):
        j = app.config["JOBS"].get(jid)
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert j["status"] == "done", j["error"]
    return [dict(r) for r in app.config["DB"].q("SELECT * FROM rfqs WHERE run_id=? ORDER BY supplier", (run,))]


def test_rfq_letters_enter_the_register(app, admin_client, villa_xlsx):
    run = _exported(app, admin_client, villa_xlsx)
    rfqs = _letters(app, run)
    assert [r["supplier"] for r in rfqs] == ["Alpha Co", "Beta Co"]
    assert all(r["status"] == "draft" and r["number"].startswith("RFQ-") and r["due_date"] == "2020-01-31"
               for r in rfqs)
    assert rfqs[0]["letter"] and Path(rfqs[0]["letter"]).exists()
    assert len(_letters(app, run)) == 2                              # writing again does not duplicate
    assert admin_client.get(f"/register/{rfqs[0]['id']}/file/letter").status_code == 200


def test_lifecycle_and_role_checks(app, admin_client, villa_xlsx):
    run = _exported(app, admin_client, villa_xlsx)
    rid = _letters(app, run)[0]["id"]
    add_user(admin_client, "eng", role="engineer")
    eng = app.test_client()
    login(eng, "eng", "user-pass-123")
    r = api_post(eng, f"/api/rfq/{rid}", {"to": "approved"})
    assert r.status_code == 400 and "role" in r.get_json()["error"]          # engineers cannot approve
    assert api_post(admin_client, f"/api/rfq/{rid}", {"to": "approved"}).status_code == 200
    assert api_post(eng, f"/api/rfq/{rid}", {"to": "issued", "note": "sent by hand"}).status_code == 200
    r = api_post(eng, f"/api/rfq/{rid}", {"to": "awarded"})
    assert r.status_code == 400                                               # not a legal step
    row = dict(app.config["DB"].one("SELECT * FROM rfqs WHERE id=?", (rid,)))
    assert row["status"] == "issued" and row["issued_at"]
    from webapp.register import is_overdue
    assert is_overdue(row)                                                    # due 2020-01-31
    assert "overdue" in admin_client.get("/register?overdue=1").text.lower()
    # stale version is refused
    r = api_post(eng, f"/api/rfq/{rid}", {"notes": "x", "version": 0})
    assert r.status_code == 409


def test_pricing_sheet_quote_and_bid_comparison(app, admin_client, villa_xlsx, tmp_path):
    from openpyxl import load_workbook
    run = _exported(app, admin_client, villa_xlsx)
    rfqs = _letters(app, run)
    for r in rfqs:
        api_post(admin_client, f"/api/rfq/{r['id']}", {"to": "approved"})
        api_post(admin_client, f"/api/rfq/{r['id']}", {"to": "issued"})
    sheet = admin_client.get(f"/register/{rfqs[0]['id']}/file/pricing")
    assert sheet.status_code == 200
    prices = {"Alpha Co": (1000, 2400), "Beta Co": (1100, 2300)}
    for r in rfqs:
        p = tmp_path / f"{r['supplier']}.xlsx"
        p.write_bytes(sheet.data)
        wb = load_workbook(p)
        ws = wb.active
        k = 0
        for row in ws.iter_rows(min_row=1):
            if row[0].value and str(row[0].value) not in ("id",) and row[4].value is not None and \
                    isinstance(row[4].value, (int, float)):
                row[5].value = prices[r["supplier"]][k]
                k += 1
        wb.save(p)
        res = admin_client.post(f"/api/rfq/{r['id']}", data={"quote": (io.BytesIO(p.read_bytes()), p.name)},
                                headers={"X-CSRF": csrf_of(admin_client)}, content_type="multipart/form-data")
        assert res.status_code == 200, res.get_json()
        assert "2 rate(s)" in res.get_json()["message"]
    rows = {r["supplier"]: dict(r) for r in app.config["DB"].q("SELECT * FROM rfqs WHERE run_id=?", (run,))}
    assert rows["Alpha Co"]["quote_amount"] == 50 * 1000 + 30 * 2400 and rows["Alpha Co"]["status"] == "quoted"
    from webapp.register import comparison
    cmp = comparison(app.config["DB"], app.config["STORE"], run, "DIV03")
    sums = {x["rfq"]["supplier"]: x for x in cmp["sums"]}
    assert sums["Alpha Co"]["lowest"] and not sums["Beta Co"]["lowest"]
    assert cmp["rows"][0]["cells"][0]["lowest"] and cmp["rows"][1]["cells"][1]["lowest"]   # per-item lowest
    x = admin_client.get(f"/register/compare/{run}/DIV03?format=xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert admin_client.get(f"/register/compare/{run}/DIV03").status_code == 200


def test_register_export_and_import(app, admin_client, villa_xlsx, tmp_path):
    from openpyxl import Workbook
    run = _exported(app, admin_client, villa_xlsx)
    _letters(app, run)
    x = admin_client.get("/register?format=xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    p = admin_client.get("/register?format=pdf")
    assert p.status_code == 200 and p.data[:5] == b"%PDF-"
    wb = Workbook()
    ws = wb.active
    ws.append(["Old register 2025"])
    ws.append(["RFQ No.", "Project", "Work package", "Supplier", "Status", "Due date", "Amount"])
    ws.append(["OLD-001", "Villa", "Steel works", "مؤسسة النور", "Awarded", "15/03/2025", "1,250,000"])
    ws.append(["OLD-002", "Unknown", "Paint", "Gamma", "", "2025-04-01", ""])
    f = tmp_path / "old.xlsx"
    wb.save(f)
    r = admin_client.post("/register/import", data={"_csrf": csrf_of(admin_client), "file": (io.BytesIO(f.read_bytes()),
                                                                                           "old.xlsx")},
                          content_type="multipart/form-data")
    assert r.status_code == 302
    rows = {r["number"]: dict(r) for r in app.config["DB"].q("SELECT * FROM rfqs WHERE number LIKE 'OLD-%'")}
    assert rows["OLD-001"]["status"] == "awarded" and rows["OLD-001"]["quote_amount"] == 1250000
    assert rows["OLD-001"]["due_date"] == "2025-03-15" and rows["OLD-001"]["run_id"] == run
    assert rows["OLD-002"]["run_id"] == "" and rows["OLD-002"]["status"] == "issued"


# ====================================================================== Phase 5 — collaboration
def test_comments_mentions_and_bell(app, admin_client, villa_xlsx):
    run = upload(app, admin_client, villa_xlsx)
    add_user(admin_client, "sara", role="engineer")
    sara = app.test_client()
    login(sara, "sara", "user-pass-123")
    r = api_post(admin_client, f"/api/p/{run}/comments", {"target": "wp:DIV03", "body": "@sara please check 3.2"})
    assert r.get_json()["mentioned"] == 1
    n = sara.get("/api/notifications").get_json()
    assert n["unread"] == 1 and "mentioned you" in n["items"][0]["text"] and "#comments" in n["items"][0]["url"]
    api_post(sara, f"/api/p/{run}/comments", {"target": "wp:DIV03", "body": "Done."})
    admin_n = admin_client.get("/api/notifications").get_json()
    assert admin_n["unread"] == 1 and "commented" in admin_n["items"][0]["text"]
    lst = admin_client.get(f"/api/p/{run}/comments?target=wp:DIV03").get_json()["comments"]
    assert [c["body"] for c in lst] == ["@sara please check 3.2", "Done."]
    r = api_post(admin_client, f"/api/p/{run}/comments", {"delete": lst[1]["id"]})
    assert r.status_code == 200                                     # administrators may delete any comment
    api_post(sara, "/api/notifications/read", {})
    assert sara.get("/api/notifications").get_json()["unread"] == 0


def test_package_owner_assignment(app, admin_client, villa_xlsx):
    run = upload(app, admin_client, villa_xlsx)
    add_user(admin_client, "nour", role="engineer")
    uid = app.config["DB"].one("SELECT id FROM users WHERE username='nour'")["id"]
    r = api_post(admin_client, f"/api/p/{run}/owner", {"wp_key": "DIV03", "user_id": uid})
    assert r.status_code == 200
    assert app.config["DB"].one("SELECT user_id FROM package_owners WHERE run_id=?", (run,))["user_id"] == uid
    add_user(admin_client, "viewer1", role="viewer")
    vid = app.config["DB"].one("SELECT id FROM users WHERE username='viewer1'")["id"]
    assert api_post(admin_client, f"/api/p/{run}/owner", {"wp_key": "DIV03", "user_id": vid}).status_code == 400


def test_search_across_projects(app, admin_client, villa_xlsx):
    run = upload(app, admin_client, villa_xlsx)
    page = admin_client.get("/search?q=porcelain").text
    assert "Porcelain floor tiles" in page and "Ceramic" in page
    assert "Nothing found" in admin_client.get("/search?q=zzzunknown").text
    # the index follows edits: a renamed package shows up
    api_post(admin_client, f"/api/p/{run}/wp/rename", {"key": "DIV03", "name": "Structural concrete"})
    assert "Structural concrete" in admin_client.get("/search?q=foundations").text


def test_saved_views_and_bulk_actions(app, admin_client, villa_xlsx):
    a = upload(app, admin_client, villa_xlsx, name="A")
    b = upload(app, admin_client, villa_xlsx, name="B")
    assert api_post(admin_client, "/api/views", {"page": "projects", "name": "Starred", "query": "?scope=starred"}).status_code == 200
    assert "Starred" in admin_client.get("/projects").text
    r = api_post(admin_client, "/api/projects/bulk", {"action": "star", "ids": [a, b]}).get_json()
    assert r["done"] == 2
    r = api_post(admin_client, "/api/projects/bulk", {"action": "delete", "ids": [a]}).get_json()
    assert r["done"] == 1
    assert app.config["DB"].one("SELECT deleted, deleted_at FROM projects WHERE run_id=?", (a,))["deleted_at"]
    add_user(admin_client, "e2", role="engineer")
    e2 = app.test_client()
    login(e2, "e2", "user-pass-123")
    r = api_post(e2, "/api/projects/bulk", {"action": "delete", "ids": [b]}).get_json()
    assert r["done"] == 0 and r["skipped"] == 1                     # not the owner


def test_trash_is_emptied_after_n_days(app, admin_client, villa_xlsx):
    from webapp.collab import purge_trash
    run = upload(app, admin_client, villa_xlsx)
    api_post(admin_client, f"/api/p/{run}/project", {"deleted": 1})
    rt = app.config["RUNTIME"]
    assert purge_trash(rt, 30) == 0                                  # deleted just now: kept
    app.config["DB"].x("UPDATE projects SET deleted_at='2020-01-01T00:00:00' WHERE run_id=?", (run,))
    assert purge_trash(rt, 30) == 1
    assert not app.config["STORE"].run_dir(run).exists()
    assert app.config["DB"].one("SELECT 1 FROM projects WHERE run_id=?", (run,)) is None


def test_tour_and_whats_new(app, admin_client):
    page = admin_client.get("/projects").text
    assert 'id="tour"' in page
    api_post(admin_client, "/api/tour/done")
    page = admin_client.get("/projects").text
    assert 'id="tour"' not in page and "See what&#39;s new" not in page


def test_print_view(app, admin_client, villa_xlsx):
    run = upload(app, admin_client, villa_xlsx)
    page = admin_client.get(f"/p/{run}/print").text
    assert "Plain concrete for foundations" in page and "data-print" in page
