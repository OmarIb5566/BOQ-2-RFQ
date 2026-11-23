"""Arabic interface: every text the interface shows has a translation, placeholders are kept, pages switch to RTL."""
from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import ROOT, api_post, csrf_of

CATALOG = ROOT / "webapp" / "translations" / "ar.json"


def test_catalog_is_valid_json_with_arabic_values():
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    for k, v in data.items():
        if k.startswith("__"):
            continue
        assert v.strip(), k
        assert re.search(r"[؀-ۿ]", v) or v in ("Excel", "PDF", "Microsoft Excel", "Tesseract OCR"), k


def test_every_interface_text_is_translated():
    from webapp.i18n import catalog, message_ids
    catalog.cache_clear()
    missing = sorted(i for i in message_ids() if i not in catalog("ar"))
    assert not missing, f"{len(missing)} text(s) without an Arabic translation, e.g. {missing[:10]}"


def test_placeholders_survive_translation():
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    for k, v in data.items():
        assert set(re.findall(r"\{\w+\}", k)) == set(re.findall(r"\{\w+\}", v)), k


def test_t_formats_and_falls_back():
    from webapp.i18n import t
    assert t("Decide {n} case(s)", "ar", n=3) == "قرّر في 3 حالة"
    assert t("Some text nobody translated", "ar") == "Some text nobody translated"
    assert t("Projects", "en") == "Projects"


def test_language_switch_per_user_and_rtl(app, admin_client):
    assert 'dir="ltr"' in admin_client.get("/projects").text
    r = admin_client.post("/lang/ar", data={"_csrf": csrf_of(admin_client), "next": "/projects"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/projects")
    page = admin_client.get("/projects").text
    assert 'dir="rtl"' in page and "المشاريع" in page and "مشروع جديد" in page
    assert app.config["DB"].one("SELECT lang FROM users WHERE username='admin'")["lang"] == "ar"
    i18n = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', page, re.S).group(1)
    assert json.loads(i18n)["Reload"] == "إعادة التحميل"


def test_signed_out_pages_follow_the_cookie(app):
    c = app.test_client()
    c.post("/lang/ar", data={"_csrf": csrf_of(c)})
    assert 'dir="rtl"' in c.get("/setup").text


def test_api_messages_are_translated(app, admin_client):
    admin_client.post("/lang/ar", data={"_csrf": csrf_of(admin_client)})
    r = api_post(admin_client, "/api/p/nope/project", {"name": "x"})
    assert r.status_code == 404
