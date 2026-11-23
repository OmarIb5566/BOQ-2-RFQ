"""Phase 0 upload checks: real file type, PDF page / image pixel limits, zips, Word templates (XXE, size)."""
from __future__ import annotations

import io
import zipfile

import pytest

from boq_agent.ingest import sniff
from boq_agent.ingest.intake import collect
from conftest import api_post, csrf_of, make_pdf, make_png, make_xlsx, run_id_from, wait_job


# ---------------------------------------------------------------------- content sniffing
@pytest.mark.parametrize("head,kind", [
    (b"PK\x03\x04rest", "zip"), (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"), (b"%PDF-1.7", "pdf"),
    (b"\n\n%PDF-1.4", "pdf"), (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff\xe0", "jpeg"),
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"BM", "bmp"), (b"RIFF\x00\x00\x00\x00WEBP", "webp"),
    (b"\xef\xbb\xbf<html>", "markup"), (b"MZ\x90\x00", ""), (b"", ""),
])
def test_kind_of(head, kind):
    assert sniff.kind_of(head) == kind


@pytest.mark.parametrize("name,head,ok", [
    ("boq.xlsx", b"PK\x03\x04", True), ("boq.xlsx", b"%PDF-1.7", False), ("boq.xlsx", b"MZ\x90", False),
    ("old.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", True), ("export.xls", b"<html><table>", True),
    ("specs.pdf", b"%PDF-1.5", True), ("specs.pdf", b"PK\x03\x04", False), ("scan.jpg", b"\x89PNG\r\n\x1a\n", False),
    ("tool.exe", b"MZ", False), ("noext", b"PK\x03\x04", False),
])
def test_check_head(name, head, ok):
    assert (sniff.check_head(head, name) == "") is ok


def test_check_file_limits(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "s.pdf", pages=5)
    png = make_png(tmp_path / "d.png", 100, 100)
    assert sniff.check_file(pdf, "s.pdf") == "" and sniff.check_file(png, "d.png") == ""
    monkeypatch.setattr(sniff, "MAX_PDF_PAGES", 4)
    assert "5 pages" in sniff.check_file(pdf, "s.pdf")
    monkeypatch.setattr(sniff, "MAX_IMAGE_MP", 0.005)                       # 5 000 pixels
    assert "100×100" in sniff.check_file(png, "d.png")
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.7\n garbage")
    assert "damaged" in sniff.check_file(bad, "broken.pdf")


def test_image_sizes_read_from_headers_only(tmp_path):
    import struct
    import zlib

    import pymupdf as fitz
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 123, 45), False)
    pix.clear_with(90)
    for fmt in ("png", "jpg"):
        p = tmp_path / f"i.{fmt}"
        p.write_bytes(pix.tobytes(fmt if fmt == "png" else "jpeg"))
        assert sniff.image_size(p) == (123, 45), fmt
    # a PNG "bomb": the header claims 60000 x 60000 pixels (3.6 gigapixels) in a file of a few bytes
    ihdr = struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    bomb = tmp_path / "bomb.png"
    bomb.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b""))
    assert "60000×60000" in sniff.check_file(bomb, "bomb.png")
    bmp = tmp_path / "i.bmp"
    bmp.write_bytes(b"BM" + bytes(16) + struct.pack("<ii", 640, -480) + bytes(30))
    assert sniff.image_size(bmp) == (640, 480)
    tif = tmp_path / "i.tif"
    ifd = (struct.pack("<H", 2) + struct.pack("<HHI4s", 256, 3, 1, struct.pack("<H", 800) + bytes(2))
           + struct.pack("<HHI4s", 257, 4, 1, struct.pack("<I", 600)) + bytes(4))
    tif.write_bytes(b"II*\x00" + struct.pack("<I", 8) + ifd)
    assert sniff.image_size(tif) == (800, 600)


# ---------------------------------------------------------------------- intake (runs in the background job)
def test_collect_skips_fakes_and_oversize(tmp_path, monkeypatch):
    good = make_xlsx(tmp_path / "boq.xlsx")
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"MZ this is a program")
    big = make_pdf(tmp_path / "big.pdf", pages=3)
    monkeypatch.setattr(sniff, "MAX_PDF_PAGES", 2)
    log = []
    out = collect([(good, "boq.xlsx"), (fake, "fake.pdf"), (big, "big.pdf")], tmp_path / "x", log.append)
    assert [c.name for c in out] == ["boq.xlsx"]
    assert any("fake.pdf" in m for m in log) and any("3 pages" in m for m in log)


def test_zip_entries_are_checked(tmp_path):
    zp = tmp_path / "all.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.write(make_xlsx(tmp_path / "a.xlsx"), "BOQ/a.xlsx")
        z.writestr("BOQ/evil.xlsx", b"MZ not a workbook")
        z.writestr("../escape.pdf", b"%PDF-1.4")
        z.writestr("notes.txt", b"ignored")
    log = []
    out = collect([(zp, "all.zip")], tmp_path / "x", log.append)
    assert [c.name for c in out] == ["all.zip/BOQ/a.xlsx"]
    assert not (tmp_path / "x" / "zip1" / "BOQ" / "evil.xlsx").exists()
    assert not (tmp_path / "escape.pdf").exists() and not (tmp_path / "x" / "escape.pdf").exists()


def test_fake_zip_and_damaged_zip(tmp_path):
    fake = tmp_path / "f.zip"
    fake.write_bytes(b"%PDF-1.4 not a zip")
    broken = tmp_path / "b.zip"
    broken.write_bytes(b"PK\x03\x04" + b"\x00" * 50)
    log = []
    assert collect([(fake, "f.zip"), (broken, "b.zip")], tmp_path / "x", log.append) == []
    assert len(log) == 2


# ---------------------------------------------------------------------- the upload page
def _upload(client, files, **form):
    data = {"_csrf": csrf_of(client), "name": form.get("name", "Test"), "shared": "on",
            "files": [(io.BytesIO(b), n) for n, b in files]}
    return client.post("/new", data=data, content_type="multipart/form-data")


def test_upload_rejects_a_renamed_file_before_anything_is_stored(app, admin_client):
    r = _upload(admin_client, [("boq.xlsx", b"MZ\x90\x00 program")])
    assert r.status_code == 302 and r.headers["Location"].endswith("/new")
    assert "not a real .xlsx" in admin_client.get("/new").text
    assert app.config["DB"].one("SELECT COUNT(*) n FROM projects")["n"] == 0
    ws = app.config["SETTINGS"].workspace
    assert not [p for p in ws.iterdir() if not p.name.startswith("_")]


def test_upload_real_boq_is_analysed_in_the_background(app, admin_client, villa_xlsx):
    r = _upload(admin_client, [("villa.xlsx", villa_xlsx.read_bytes())], name="Villa")
    assert r.status_code == 302
    run = run_id_from(r.headers["Location"])
    job = wait_job(app, run)
    assert job["status"] == "done", job["error"]
    row = app.config["DB"].one("SELECT * FROM projects WHERE run_id=?", (run,))
    assert row["stage"] in ("decisions", "ready") and "11 items" in row["summary"]


def test_oversized_request_is_refused(make_app):
    app = make_app(BOQ_MAX_UPLOAD_MB="1")
    c = app.test_client()
    from conftest import create_admin
    create_admin(app, c)
    r = _upload(c, [("big.pdf", b"%PDF-1.4" + b"0" * (2 * 1024 * 1024))])
    assert r.status_code == 413 and "1 MB at most" in r.text


# ---------------------------------------------------------------------- Word templates
def _docx(document_xml: bytes, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document_xml)
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def test_template_xxe_is_not_resolved(tmp_path):
    from boq_agent.rfq.template import discover_fields
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    xml = (f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///{secret.as_posix()}">]>'
           f'<w:document {W}><w:body><w:p><w:r><w:t>(Project Name)</w:t></w:r></w:p>'
           f'<w:p><w:r><w:t>(&x;)</w:t></w:r></w:p></w:body></w:document>')
    p = tmp_path / "t.docx"
    p.write_bytes(_docx(xml.encode()))
    fields = discover_fields(p)
    assert "Project Name" in fields
    assert not any("TOPSECRET" in f for f in fields)


def test_template_zip_bomb_refused(tmp_path):
    from boq_agent.rfq.template import discover_fields
    p = tmp_path / "bomb.docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", f"<w:document {W}/>")
        z.writestr("word/media/big.bin", b"\x00" * (101 * 1024 * 1024))
    p.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="too large"):
        discover_fields(p)


def _project(app, admin_client, villa_xlsx) -> str:
    r = _upload(admin_client, [("villa.xlsx", villa_xlsx.read_bytes())], name="Villa")
    run = run_id_from(r.headers["Location"])
    assert wait_job(app, run)["status"] == "done"
    return run


def test_template_upload_checks(app, admin_client, villa_xlsx):
    run = _project(app, admin_client, villa_xlsx)

    def up(name, data):
        return admin_client.post(f"/api/p/{run}/rfq/template", data={"template": (io.BytesIO(data), name)},
                                 headers={"X-CSRF": csrf_of(admin_client)}, content_type="multipart/form-data")
    r = up("t.docx", b"MZ not a document")
    assert r.status_code == 400 and "not a real Word" in r.get_json()["error"]
    r = up("t.docx", b"PK\x03\x04 broken zip")
    assert r.status_code == 400 and "cannot be read" in r.get_json()["error"]
    good = _docx(f'<w:document {W}><w:body><w:p><w:r><w:t>(Supplier Name)</w:t></w:r></w:p></w:body></w:document>'.encode())
    r = up("t.docx", good)
    assert r.status_code == 200 and "1 field" in r.get_json()["message"]


def test_api_still_works_with_csrf_header(app, admin_client, villa_xlsx):
    run = _project(app, admin_client, villa_xlsx)
    r = api_post(admin_client, f"/api/p/{run}/project", {"name": "Renamed villa"})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert app.config["DB"].one("SELECT name FROM projects WHERE run_id=?", (run,))["name"] == "Renamed villa"
