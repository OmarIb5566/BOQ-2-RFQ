"""Phase 0 security: headers, CSP, CORS, cross-site requests, no inline scripts, nothing loaded from other sites."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import ROOT, api_post, csrf_of


def test_html_pages_get_strict_csp_with_fresh_nonce(admin_client):
    a = admin_client.get("/projects")
    b = admin_client.get("/projects")
    csp = a.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]       # scripts: nonce only
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in a.text                                            # the theme script carries it
    assert nonce != re.search(r"'nonce-([^']+)'", b.headers["Content-Security-Policy"]).group(1)


def test_security_headers_on_every_response(admin_client):
    for url in ("/projects", "/login", "/static/app.css", "/api/status"):
        h = admin_client.get(url).headers
        assert h["X-Content-Type-Options"] == "nosniff", url
        assert h["X-Frame-Options"] == "DENY", url
        assert h["Referrer-Policy"] == "same-origin", url
    assert admin_client.get("/projects").headers["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" not in admin_client.get("/login").headers     # plain HTTP by default


def test_https_mode_sets_secure_cookie_and_hsts(make_app):
    app = make_app(BOQ_HTTPS="1")
    assert app.config["SESSION_COOKIE_SECURE"] is True
    r = app.test_client().get("/login", base_url="https://boq.example")
    assert "Strict-Transport-Security" in r.headers
    assert "Secure" in r.headers.get("Set-Cookie", "")


def test_no_cors_even_when_another_site_asks(admin_client):
    evil = {"Origin": "https://evil.example"}
    for r in (admin_client.get("/api/jobs", headers=evil),
              admin_client.options("/api/jobs", headers={**evil, "Access-Control-Request-Method": "POST"})):
        assert not [k for k in r.headers.keys() if k.lower().startswith("access-control-")]


def test_cross_site_post_refused_even_with_valid_csrf(admin_client):
    r = api_post(admin_client, "/api/p/x/project", {"name": "n"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and "another site" in r.get_json()["error"]
    r = admin_client.post("/logout", data={"_csrf": csrf_of(admin_client)}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_same_origin_post_allowed(admin_client):
    r = admin_client.post("/logout", data={"_csrf": csrf_of(admin_client)}, headers={"Origin": "http://localhost"})
    assert r.status_code == 302


def test_post_without_csrf_token_refused(admin_client):
    assert admin_client.post("/logout").status_code == 400
    r = admin_client.post("/api/p/x/project", json={}, headers={"X-CSRF": "wrong"})
    assert r.status_code == 400


TEMPLATES = sorted((ROOT / "webapp" / "templates").rglob("*.html"))


def test_templates_have_no_inline_event_handlers_or_unnonced_scripts():
    for t in TEMPLATES:
        s = t.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", s), f"inline handler in {t.name}"
        for tag in re.findall(r"<script\b[^>]*>", s):
            assert "src=" in tag or "nonce=" in tag or 'type="application/json"' in tag, f"{t.name}: {tag}"


def test_nothing_is_loaded_from_other_sites():
    files = TEMPLATES + sorted((ROOT / "webapp" / "static").rglob("*.js")) + sorted(
        (ROOT / "webapp" / "static").rglob("*.css"))
    for f in files:
        s = f.read_text(encoding="utf-8")
        s = s.replace("http://www.w3.org/2000/svg", "")          # an SVG namespace, not a download
        assert not re.search(r"(?:src|href)\s*=\s*[\"']?(?:https?:)?//", s), f.name
        assert not re.search(r"url\(\s*[\"']?(?:https?:)?//", s), f.name
        assert not re.search(r"@import\s+[\"']?(?:url\()?\s*[\"']?(?:https?:)?//", s), f.name


def test_branding_assets_present(admin_client):
    page = admin_client.get("/projects").text
    for asset in ("brand/rowad-logo.png", "brand/rowad-logo-dark.png", "brand/rowad-mark-32.png",
                  "brand/rowad-mark-180.png"):
        assert asset in page, asset
        assert admin_client.get(f"/static/{asset}").status_code == 200
    admin_client.post("/logout", data={"_csrf": csrf_of(admin_client)})
    assert 'alt="Rowad Modern Engineering"' in admin_client.get("/login").text


def test_file_route_stays_inside_project(admin_client, app):
    from conftest import wait_job  # noqa: F401
    store = app.config["STORE"]
    run = "20260101-000000-x"
    (store.run_dir(run)).mkdir(parents=True)
    app.config["DB"].upsert_project(run, name="x", owner_id=1, shared=1, stage="ready")
    (Path(app.config["SETTINGS"].workspace).parent / "secret.txt").write_text("no", encoding="utf-8")
    assert admin_client.get(f"/p/{run}/file/../../secret.txt").status_code == 404
    assert admin_client.get(f"/p/{run}/file/..%2F..%2Fsecret.txt").status_code == 404
