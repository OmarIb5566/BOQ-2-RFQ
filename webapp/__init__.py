"""BOQ → RFQ web application (Flask).

    create_app()  ->  Flask app: accounts (auth.py), projects & actions (views.py), the admin console (admin.py),
                      collaboration (collab_views.py), the RFQ register (register_views.py) …  — see BLUEPRINTS.
The settings, database, project store and job queue come from runtime.py and are shared with worker.py.
Run it with  server.py  (Waitress, multi-threaded).
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import secrets
from pathlib import Path

from flask import Flask, render_template

from boq_agent.config import Settings

from . import security
from .runtime import build_runtime, worker_mode

# Screens and APIs, one module each (a new feature adds a module here instead of growing views.py).
BLUEPRINTS = ["auth", "views", "admin", "collab_views", "register_views", "revisions_views", "fleet_views",
              "help_views"]


def create_app(settings: Settings | None = None, start_worker: bool | None = None) -> Flask:
    rt = build_runtime(settings)
    settings = rt.settings
    app = Flask(__name__, instance_path=str(rt.instance_path))

    key_file = Path(app.instance_path) / "secret.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    max_mb = int(os.getenv("BOQ_MAX_UPLOAD_MB", "4096"))
    app.config.update(
        SECRET_KEY=key_file.read_text(encoding="utf-8").strip(),
        MAX_CONTENT_LENGTH=max_mb * 1024 ** 2,            # 4 GB per upload by default (drawing sets are large)
        MAX_UPLOAD_MB=max_mb,
        TEMPLATES_AUTO_RELOAD=True,
    )
    security.configure(app)
    app.config["PERMANENT_SESSION_LIFETIME"] = dt.timedelta(seconds=app.config["REMEMBER_SECONDS"])
    db, store = rt.db, rt.store
    app.config.update(SETTINGS=settings, DB=db, STORE=store, JOBS=rt.jobs, RUNTIME=rt, WORKER_MODE=worker_mode())

    from . import i18n
    i18n.init_app(app)
    for name in BLUEPRINTS:
        mod = importlib.import_module(f".{name}", __name__)
        app.register_blueprint(mod.bp)

    if start_worker is None:
        start_worker = worker_mode() == "inline"
    if start_worker:
        rt.jobs.start_worker(rt)

    views = importlib.import_module(".views", __name__)

    def import_runs(owner_id: int) -> int:
        with app.test_request_context():
            return views.import_runs(owner_id)
    app.config["IMPORT_RUNS"] = import_runs

    # first run: a one-time set-up token, so only whoever can see the server window creates the administrator
    token_file = Path(app.instance_path) / "setup.token"
    if not db.one("SELECT 1 FROM users LIMIT 1"):
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(18), encoding="utf-8")
        app.config["SETUP_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
    else:
        token_file.unlink(missing_ok=True)

    def clear_setup_token() -> None:
        app.config["SETUP_TOKEN"] = ""
        token_file.unlink(missing_ok=True)
    app.config["CLEAR_SETUP_TOKEN"] = clear_setup_token

    # runs created outside the web app (command line) appear for the first administrator
    admin = db.one("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1")
    if admin:
        import_runs(admin["id"])

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("error.html", code=403, msg="This project is not shared with you."), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("error.html", code=404, msg="That page or project does not exist."), 404

    @app.errorhandler(413)
    def too_big(e):
        mb = app.config["MAX_UPLOAD_MB"]
        limit = f"{mb / 1024:g} GB" if mb >= 1024 else f"{mb} MB"
        return render_template("error.html", code=413, msg=f"The upload is too large ({limit} at most)."), 413

    return app


def setup_url(app: Flask, base: str) -> str:
    """The first-run link (with its one-time token) while no administrator exists yet, else ''."""
    tok = app.config.get("SETUP_TOKEN")
    return f"{base}/setup?token={tok}" if tok else ""
