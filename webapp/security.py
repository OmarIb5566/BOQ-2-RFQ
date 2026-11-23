"""Request hardening for the web app.

* Security headers on every response; HTML pages get a strict Content-Security-Policy whose scripts must carry a
  per-request nonce (no inline event handlers, nothing loaded from other sites).
* State-changing requests whose Origin header names another site are refused (on top of the CSRF token).
* No CORS headers are ever sent: other sites cannot read this app's responses with the user's cookies.
* Sign-in throttling: too many wrong passwords for one user name, or from one address, lock sign-in for a while.
* Idle sign-out: a session that has not been used for a while ends (background polling does not count as use).

Settings (environment variables, read at start-up):
    BOQ_IDLE_MINUTES      sign out after this many idle minutes (default 60; "keep me signed in": 7 days)
    BOQ_REMEMBER_DAYS     how long "keep me signed in" lasts (default 7)
    BOQ_HTTPS=1           the app is reached over HTTPS (Secure cookies + HSTS)
    BOQ_BEHIND_PROXY=1    trust X-Forwarded-For/-Proto/-Host from ONE reverse proxy in front of the app
"""
from __future__ import annotations

import os
import secrets
import time
from urllib.parse import urlsplit

from flask import Flask, abort, g, jsonify, request

LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_PER_USER = 5          # wrong passwords per user name in the window
LOGIN_MAX_PER_IP = 20           # wrong passwords from one address in the window

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def configure(app: Flask) -> None:
    https = env_flag("BOQ_HTTPS")
    app.config.update(
        IDLE_SECONDS=int(float(os.getenv("BOQ_IDLE_MINUTES", "60")) * 60),
        REMEMBER_SECONDS=int(float(os.getenv("BOQ_REMEMBER_DAYS", "7")) * 86400),
        SESSION_COOKIE_NAME="boq2rfq_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=https,
        HTTPS=https,
    )
    if env_flag("BOQ_BEHIND_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.before_request
    def _nonce_and_origin():
        g.csp_nonce = secrets.token_urlsafe(16)
        if request.method in UNSAFE and not same_origin():
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Request from another site refused."), 403
            abort(403)
        return None

    @app.context_processor
    def _ctx():
        return {"csp_nonce": g.get("csp_nonce", "")}

    @app.after_request
    def _headers(resp):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        if app.config.get("HTTPS"):
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if resp.mimetype == "text/html":
            nonce = g.get("csp_nonce", "")
            h["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self' 'unsafe-inline'; "         # style="" attributes in the templates
                "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
            )
            if getattr(g, "user", None):
                h.setdefault("Cache-Control", "no-store")     # signed-in pages are not kept in shared caches
        for k in [k for k in h.keys() if k.lower().startswith("access-control-")]:
            del h[k]                                           # never CORS
        return resp


def same_origin() -> bool:
    """True unless the browser says the request comes from another site (Origin, else Referer)."""
    src = request.headers.get("Origin") or request.headers.get("Referer")
    if not src:
        return True                      # non-browser clients; the CSRF token still applies
    if src == "null":
        return False
    return urlsplit(src).netloc.lower() == request.host.lower()


# ---------------------------------------------------------------------- sign-in throttling
def login_blocked(db, username: str, ip: str) -> int:
    """Seconds until sign-in is allowed again for this user name / address (0 = allowed)."""
    since = time.time() - LOGIN_WINDOW_S
    wait = 0
    for col, val, limit in (("username", username.lower(), LOGIN_MAX_PER_USER), ("ip", ip, LOGIN_MAX_PER_IP)):
        rows = db.q(f"SELECT ts FROM login_failures WHERE {col}=? AND ts>? ORDER BY ts DESC", (val, since))
        if len(rows) >= limit:
            wait = max(wait, int(rows[limit - 1]["ts"] + LOGIN_WINDOW_S - time.time()) + 1)
    return wait


def login_failed(db, username: str, ip: str) -> None:
    db.x("INSERT INTO login_failures (username, ip, ts) VALUES (?,?,?)", (username.lower(), ip, time.time()))
    db.x("DELETE FROM login_failures WHERE ts<?", (time.time() - 2 * LOGIN_WINDOW_S,))


def login_succeeded(db, username: str) -> None:
    db.x("DELETE FROM login_failures WHERE username=?", (username.lower(),))


def is_background() -> bool:
    """Polling requests (job progress, engine status) do not count as the user being active."""
    return request.headers.get("X-Background") == "1"
