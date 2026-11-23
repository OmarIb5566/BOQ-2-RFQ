"""Shared test fixtures. Everything runs in pytest's temporary folders — never in workspace/ or instance/.

    pytest                      # all tests (from the project folder, with .venv-dev)
    UPDATE_GOLDEN=1 pytest tests/test_golden.py     # accept a deliberate change of the packaging results
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECURITY_ENV = ("BOQ_HTTPS", "BOQ_BEHIND_PROXY", "BOQ_IDLE_MINUTES", "BOQ_REMEMBER_DAYS", "BOQ_MAX_UPLOAD_MB")
ADMIN_PW = "admin-pass-123"


# ---------------------------------------------------------------------- sample inputs (made up, no client data)
VILLA_ROWS = [
    ["Project: Test Villa"], [],
    ["Item", "Description", "Unit", "Qty", "Rate", "Total"],
    ["", "Division 03 - Concrete", "", "", "", ""],
    ["3.1", "Plain concrete for foundations", "m3", 50, 1000, 50000],
    ["3.2", "Reinforced concrete for columns", "m3", 30, 2500, 75000],
    ["", "Division 04 - Masonry", "", "", "", ""],
    ["4.1", "Cement block walls 20 cm", "m2", 400, 200, 80000],
    ["", "Division 08 - Openings", "", "", "", ""],
    ["8.1", "Wooden door 90x210 cm", "no", 12, 4000, 48000],
    ["8.2", "Aluminium window 120x150 cm", "no", 20, 3000, 60000],
    ["", "Division 09 - Finishes", "", "", "", ""],
    ["9.1", "Internal plaster to walls", "m2", 900, 80, 72000],
    ["9.2", "Acrylic paint to walls", "m2", 900, 60, 54000],
    ["9.3", "Porcelain floor tiles 60x60", "m2", 300, 350, 105000],
    ["9.4", "Marble flooring Galala", "m2", 80, 900, 72000],
    ["", "Division 07 - Thermal and Moisture Protection", "", "", "", ""],
    ["7.1", "Bituminous waterproofing to roof", "m2", 200, 150, 30000],
    ["7.2", "Cement tiles for roof finish 40x40", "m2", 200, 120, 24000],
]


def make_xlsx(path: Path, rows=VILLA_ROWS) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BOQ"
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def make_pdf(path: Path, pages: int = 1, text: str = "Specification") -> Path:
    import pymupdf as fitz
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"{text} page {i + 1}")
    doc.save(path)
    return path


def make_png(path: Path, w: int = 64, h: int = 48) -> Path:
    import pymupdf as fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.clear_with(200)
    path.write_bytes(pix.tobytes("png"))
    return path


@pytest.fixture
def villa_xlsx(tmp_path) -> Path:
    return make_xlsx(tmp_path / "villa.xlsx")


# ---------------------------------------------------------------------- the web app
@pytest.fixture
def settings(tmp_path):
    from boq_agent.config import Settings
    return Settings(workspace=tmp_path / "data" / "workspace", use_llm=False, excel_renderer="html")


@pytest.fixture
def make_app(settings, monkeypatch):
    """make_app(**env) -> a fresh app on a temporary workspace (env = security settings for this app)."""
    for k in SECURITY_ENV:
        monkeypatch.delenv(k, raising=False)

    def _make(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        from webapp import create_app
        app = create_app(settings)
        app.config["TESTING"] = True
        return app
    return _make


@pytest.fixture
def app(make_app):
    return make_app()


def csrf_of(client) -> str:
    with client.session_transaction() as s:
        if "csrf" not in s:
            client.get("/login")
    with client.session_transaction() as s:
        return s["csrf"]


def create_admin(app, client, username="admin", password=ADMIN_PW):
    tok = app.config["SETUP_TOKEN"]
    client.get(f"/setup?token={tok}")
    r = client.post("/setup", data=dict(_csrf=csrf_of(client), full_name="Admin User", username=username,
                                        password=password, password2=password))
    assert r.status_code == 302, r.text
    return r


def login(client, username, password, remember=False):
    data = dict(_csrf=csrf_of(client), username=username, password=password)
    if remember:
        data["remember"] = "on"
    return client.post("/login", data=data)


@pytest.fixture
def admin_client(app):
    c = app.test_client()
    create_admin(app, c)
    return c


def add_user(admin_client, username, password="user-pass-123", role="user"):
    r = admin_client.post("/admin/users", data=dict(_csrf=csrf_of(admin_client), action="add", username=username,
                                                    full_name=username.title(), password=password, role=role))
    assert r.status_code == 302


def api_post(client, url, json=None, **kw):
    return client.post(url, json=json or {}, headers={"X-CSRF": csrf_of(client), **kw.pop("headers", {})}, **kw)


def wait_job(app, run_id: str, timeout: float = 120) -> dict:
    """Wait until no job is queued / running for the project; returns the last job."""
    db = app.config["DB"]
    end = time.time() + timeout
    while time.time() < end:
        if not db.one("SELECT 1 FROM jobs WHERE run_id=? AND status IN ('queued','running')", (run_id,)):
            return dict(db.one("SELECT * FROM jobs WHERE run_id=? ORDER BY created DESC LIMIT 1", (run_id,)))
        time.sleep(0.2)
    raise TimeoutError(f"job for {run_id} still running after {timeout}s")


def run_id_from(location: str) -> str:
    m = re.search(r"/p/([^/]+)/", location)
    assert m, location
    return m.group(1)
