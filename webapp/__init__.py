"""BOQ → RFQ web application (Flask).

    create_app()  ->  Flask app with: accounts (auth.py), projects & actions (views.py),
                      a project store with per-project locks (store.py) and one background job queue (jobs.py).
Run it with  server.py  (Waitress, multi-threaded) — see run_app.bat.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path

from flask import Flask, render_template

from boq_agent.config import Settings

from .db import DB
from .jobs import Jobs
from .store import Store


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings()
    settings.ensure_dirs()
    app = Flask(__name__, instance_path=str(Path(settings.workspace).resolve().parent / "instance"))
    os.makedirs(app.instance_path, exist_ok=True)

    key_file = Path(app.instance_path) / "secret.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32), encoding="utf-8")
    app.config.update(
        SECRET_KEY=key_file.read_text(encoding="utf-8").strip(),
        MAX_CONTENT_LENGTH=4 * 1024 ** 3,                 # 4 GB per upload (drawing sets are large)
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        TEMPLATES_AUTO_RELOAD=True,
    )
    db = DB(Path(app.instance_path) / "boq2rfq.db")
    store = Store(settings, db)
    app.config.update(SETTINGS=settings, DB=db, STORE=store)
    app.config["JOBS"] = Jobs(db, app)

    from . import auth, views
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)

    def import_runs(owner_id: int) -> int:
        with app.test_request_context():
            return views.import_runs(owner_id)
    app.config["IMPORT_RUNS"] = import_runs

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
        return render_template("error.html", code=413, msg="The upload is too large (4 GB at most)."), 413

    return app
