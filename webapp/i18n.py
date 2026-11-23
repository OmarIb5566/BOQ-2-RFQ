"""Interface language: English or Arabic (right-to-left).

    {{ _("Projects") }}                     in templates
    {{ _("{n} item(s)", n=3) }}             with values
    t("Saved.")                             in Python (flash / API messages)

Translations live in webapp/translations/<lang>.json as {"English text": "translation"}; an English text without a
translation is shown in English (never an error). The language comes from the user's choice (account menu), else
the `lang` cookie, else BOQ_DEFAULT_LANG (default "en"). tests/test_i18n.py lists any text still untranslated.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from flask import Blueprint, Flask, current_app, g, jsonify, make_response, redirect, request

LANGS = {"en": {"name": "English", "dir": "ltr"}, "ar": {"name": "العربية", "dir": "rtl"}}
TRANSLATIONS = Path(__file__).parent / "translations"

bp = Blueprint("i18n", __name__)


@lru_cache(maxsize=8)
def catalog(lang: str) -> dict[str, str]:
    p = TRANSLATIONS / f"{lang}.json"
    if lang == "en" or not p.exists():
        return {}
    return {k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items() if not k.startswith("__")}


def current_lang() -> str:
    try:
        u = g.get("user")
    except RuntimeError:            # outside a request (jobs, worker)
        return "en"
    if u and u.get("lang") in LANGS:
        return u["lang"]
    c = request.cookies.get("lang") if request else None
    if c in LANGS:
        return c
    d = os.getenv("BOQ_DEFAULT_LANG", "en")
    return d if d in LANGS else "en"


def t(text: str, lang: str | None = None, **kw) -> str:
    out = catalog(lang or current_lang()).get(text, text)
    if kw:
        try:
            out = out.format(**kw)
        except (KeyError, IndexError, ValueError):
            out = text.format(**kw)
    return out


def js_catalog(lang: str) -> dict[str, str]:
    """The translations app.js needs (keys marked in the catalog with the 'js:' prefix are client-side texts)."""
    cat = catalog(lang)
    return {k[3:]: v for k, v in cat.items() if k.startswith("js:")}


def init_app(app: Flask) -> None:
    @app.context_processor
    def _ctx():
        lang = current_lang()
        return {"_": lambda s, **kw: t(s, lang, **kw), "t_summary": t_summary, "lang": lang, "dir": LANGS[lang]["dir"], "LANGS": LANGS,
                "js_i18n": js_catalog(lang)}
    app.register_blueprint(bp)


@bp.route("/lang/<code>", methods=["POST"])
def set_lang(code):
    if code not in LANGS:
        code = "en"
    u = g.get("user")
    if u:
        current_app.config["DB"].x("UPDATE users SET lang=? WHERE id=?", (code, u["id"]))
    nxt = request.form.get("next") or request.args.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
        nxt = "/"
    resp = make_response(jsonify(ok=True) if request.is_json else redirect(nxt))
    resp.set_cookie("lang", code, max_age=365 * 86400, samesite="Lax", httponly=True,
                    secure=current_app.config.get("SESSION_COOKIE_SECURE", False))
    return resp


def message_ids() -> set[str]:
    """Every English text the interface can show: _("…") in the templates + texts the code passes through _()."""
    import re
    pat = re.compile(r"""_\(\s*(?:"([^"]*)"|'([^']*)')""")
    ids: set[str] = set()
    for p in (Path(__file__).parent / "templates").rglob("*.html"):
        for m in pat.finditer(p.read_text(encoding="utf-8")):
            ids.add(m.group(1) if m.group(1) is not None else m.group(2))
    from . import fleet, help_views, perms, register
    from .store import STAGES
    from .views import ACTION_TEXT, TABS
    for t_, d in help_views.TOUR:
        ids |= {t_, d}
    for _v, items in help_views.WHATS_NEW:
        ids |= set(items)
    ids |= set(perms.ROLE_LABEL.values()) | set(register.STATUS_LABEL.values()) | set(fleet.ROLE_HELP.values())
    ids |= {lb for _k, lb in STAGES} | {lb for _k, lb in TABS} | set(ACTION_TEXT.values())
    ids |= {r.capitalize() for r in fleet.ROLES} | set(register.STATUSES) | set(perms.MEMBER_ROLES)
    ids |= {"done", "failed", "running", "queued", "interrupted", "cancelled", "new", "changed", "removed",
            "unchanged", "approved", "denied", "pending", "Engine", "Server", "Set in the admin console", "Database",
            "Background worker", "Disk (workspace)", "Microsoft Excel", "Tesseract OCR", "Sign-in"}
    ids |= {f"js:{s}" for s in JS_TEXTS}
    ids |= {k for _p, k in SUMMARY_WORDS} | {"just now", "{n} min ago", "{n} h ago", "{n} d ago"}
    return {i for i in ids if i.strip()}


# texts shown by app.js (looked up as "js:<text>" in the catalogue)
JS_TEXTS = [
    "Something went wrong.", "Cannot reach the server — is it still running?", "Reload",
    "{name} is here too", "{n} others are here", "This project was changed by someone else.", "Show the changes",
    "Nothing new.", "No comments yet. Type @username to notify someone.", "Delete", "Delete this comment?",
    "Name of this view (the current filters are saved):", "Note for the history (optional):",
    "AI ready", "AI offline — keyword mode",
]


SUMMARY_WORDS = [(r"(\d+) BOQs?\b", "{n} BOQ(s)"), (r"(\d+) items\b", "{n} items"), (r"(\d+) packages\b", "{n} packages"),
                 (r"(\d+) spec files?\b", "{n} spec file(s)"), (r"(\d+) sheets\b", "{n} sheets"),
                 (r"(\d+) file\(s\) uploaded", "{n} file(s) uploaded")]


def t_summary(text: str) -> str:
    """'3 BOQs · 412 items · 18 packages' (stored in English) shown in the user's language."""
    import re as _re
    if current_lang() == "en" or not text:
        return text
    out = text
    for pat, key in SUMMARY_WORDS:
        out = _re.sub(pat, lambda m, k=key: t(k, n=m.group(1)), out)
    return out
