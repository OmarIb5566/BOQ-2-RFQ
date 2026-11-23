"""Every page renders (English and Arabic) for every role — catches template errors and broken links early."""
from __future__ import annotations

import io

import pytest

from conftest import add_user, api_post, csrf_of, login, run_id_from, wait_job


@pytest.fixture
def project(app, admin_client, villa_xlsx):
    r = admin_client.post("/new", data={"_csrf": csrf_of(admin_client), "name": "Villa", "shared": "on",
                                        "files": [(io.BytesIO(villa_xlsx.read_bytes()), "villa.xlsx")]},
                          content_type="multipart/form-data")
    run = run_id_from(r.headers["Location"])
    assert wait_job(app, run)["status"] == "done"
    return run


def pages(run: str, wp_key: str) -> list[str]:
    tabs = ["overview", "cases", "packages", "items", "export", "rfq", "activity"]
    return (["/projects", "/projects?trash=1", "/new", "/search", "/search?q=concrete", "/search?q=paint&mode=meaning",
             "/register", "/register?open=1&overdue=1", "/help", "/whats-new", "/account",
             f"/p/{run}/members", f"/p/{run}/revisions", f"/p/{run}/revise", f"/p/{run}/print",
             f"/p/{run}/items?view=sections", f"/p/{run}/package/{wp_key}", f"/register/compare/{run}/{wp_key}"]
            + [f"/p/{run}/{t}" for t in tabs])


ADMIN_PAGES = ["/admin/health", "/admin/queue", "/admin/audit", "/admin/settings", "/admin/models", "/admin/usage",
               "/admin/users"]


@pytest.mark.parametrize("lang", ["en", "ar"])
def test_every_page_renders_for_admin(app, admin_client, project, lang):
    api_post(admin_client, f"/lang/{lang}")
    admin_client.post(f"/lang/{lang}", data={"_csrf": csrf_of(admin_client)})
    s = app.config["STORE"].load(project)
    wp_key = s.plan.work_packages[0].key
    for url in pages(project, wp_key) + ADMIN_PAGES:
        r = admin_client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:500])
        assert f'lang="{lang}"' in r.text, url
        if lang == "ar":
            assert 'dir="rtl"' in r.text, url


@pytest.mark.parametrize("role", ["manager", "engineer", "viewer"])
def test_pages_per_role(app, admin_client, project, role):
    add_user(admin_client, f"u_{role}", role=role)
    c = app.test_client()
    login(c, f"u_{role}", "user-pass-123")
    s = app.config["STORE"].load(project)
    for url in pages(project, s.plan.work_packages[0].key):
        r = c.get(url)
        if url.endswith("/revise") or url == "/new":
            assert r.status_code == (403 if role == "viewer" else 200), (role, url)
        else:
            assert r.status_code == 200, (role, url, r.status_code)
    for url in ADMIN_PAGES:
        r = c.get(url)
        allowed = role == "manager" and url in ("/admin/health", "/admin/queue", "/admin/usage")
        assert r.status_code == (200 if allowed else 403), (role, url, r.status_code)
