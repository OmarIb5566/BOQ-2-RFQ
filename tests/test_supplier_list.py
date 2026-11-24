"""Supplier list (Excel / CSV) -> supplier names next to the matching work packages on the RFQ page."""
from __future__ import annotations

import io

import openpyxl
import pytest

from boq_agent.rfq.suppliers import read_supplier_list
from conftest import api_post, csrf_of, run_id_from, wait_job


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    wb.save(path)
    return path


def test_supplier_and_trade_columns(tmp_path):
    p = _xlsx(tmp_path / "a.xlsx", [["Our approved vendors"], [],
                                    ["No.", "Supplier Name", "Trade / Scope"],
                                    [1, "Al Bonyan", "Masonry, Plaster"],
                                    [2, "Nile Paints", "Painting"],
                                    [3, "Delta Blocks", ""]])          # blank trade = same group as above
    lst = read_supplier_list(p)
    assert ("Al Bonyan", "Masonry") in lst.pairs and ("Al Bonyan", "Plaster") in lst.pairs
    assert ("Delta Blocks", "Painting") in lst.pairs
    assert lst.layout == "supplier and trade columns"


def test_tick_matrix(tmp_path):
    p = _xlsx(tmp_path / "b.xlsx", [["Supplier", "Masonry", "Painting Works", "Doors"],
                                    ["Al Bonyan", "x", "", ""], ["Nile Paints", "", "✓", "yes"]])
    assert set(read_supplier_list(p).pairs) == {("Al Bonyan", "Masonry"), ("Nile Paints", "Painting Works"),
                                                ("Nile Paints", "Doors")}


def test_column_per_trade(tmp_path):
    p = _xlsx(tmp_path / "c.xlsx", [["Masonry", "Painting Works", "Doors"],
                                    ["Al Bonyan", "Nile Paints", "Wood Art"], ["Delta Blocks", "", "Alu Tech"]])
    pairs = set(read_supplier_list(p).pairs)
    assert ("Delta Blocks", "Masonry") in pairs and ("Alu Tech", "Doors") in pairs and len(pairs) == 5


def test_arabic_csv(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("المورد,الأعمال\nشركة البنيان,مباني، محارة\n", encoding="utf-8-sig")
    assert ("شركة البنيان", "مباني") in read_supplier_list(p).pairs


@pytest.fixture
def project(app, admin_client, villa_xlsx):
    r = admin_client.post("/new", data={"_csrf": csrf_of(admin_client), "name": "Villa", "shared": "on",
                                        "files": [(io.BytesIO(villa_xlsx.read_bytes()), "villa.xlsx")]},
                          content_type="multipart/form-data")
    run = run_id_from(r.headers["Location"])
    assert wait_job(app, run)["status"] == "done"
    return run


def _upload(client, run, data: bytes, name: str):
    return client.post(f"/api/p/{run}/rfq/suppliers-file", data={"suppliers": (io.BytesIO(data), name)},
                       headers={"X-CSRF": csrf_of(client)}, content_type="multipart/form-data").get_json()


def test_upload_fills_matching_packages_and_undo(app, admin_client, project, tmp_path):
    store = app.config["STORE"]
    s = store.load(project)
    wp = {w.name_en: w.key for w in s.plan.work_packages}
    masonry = next(k for n, k in wp.items() if "Masonry" in n)
    paint = next(k for n, k in wp.items() if "Paint" in n)
    # a name typed by hand stays
    api_post(admin_client, f"/api/p/{project}/rfq/draft", {"suppliers": {masonry: ["Typed By Hand"]}})
    f = _xlsx(tmp_path / "list.xlsx", [["Supplier", "Packages"], ["Al Bonyan", "block work, masonry"],
                                       ["Nile Paints", "Acrylic paint"], ["Sky Elevators", "Elevators"]])
    j = _upload(admin_client, project, f.read_bytes(), "list.xlsx")
    assert j["ok"] and j["job"], j
    assert wait_job(app, project)["status"] == "done"
    d = store.load(project).rfq_draft
    assert d["suppliers"][masonry] == ["Typed By Hand", "Al Bonyan"]
    assert d["suppliers"][paint] == ["Nile Paints"]
    assert "Elevators" in d["supplier_import"]["unmatched"]          # no such package in this BOQ
    page = admin_client.get(f"/p/{project}/rfq").get_data(as_text=True)
    assert "Al Bonyan" in page and "Not matched to any package" in page
    j = api_post(admin_client, f"/api/p/{project}/rfq/suppliers-undo").get_json()
    assert j["ok"], j
    d = store.load(project).rfq_draft
    assert d["suppliers"][masonry] == ["Typed By Hand"] and not d["suppliers"].get(paint)


def test_upload_refuses_a_fake_file(admin_client, project):
    j = _upload(admin_client, project, b"MZ\x90\x00 not excel", "list.xlsx")
    assert not j["ok"] and "not a real" in j["error"]
    assert not _upload(admin_client, project, b"x", "list.pdf")["ok"]


def test_example_file(admin_client, project):
    r = admin_client.get(f"/p/{project}/rfq/suppliers-example.xlsx")
    assert r.status_code == 200
    ws = openpyxl.load_workbook(io.BytesIO(r.data)).active
    assert ws["A1"].value == "Supplier" and ws["B2"].value
