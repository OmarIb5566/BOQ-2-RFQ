"""Help page, "What's new" and the first-run tour."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, render_template

from .auth import login_required

bp = Blueprint("help_views", __name__)

APP_VERSION = "2.0"

# newest first; shown once to each user after an update (users.seen_version)
WHATS_NEW = [
    ("2.0", [
        "Rowad branding, Arabic interface (account menu › العربية).",
        "Roles: administrator, manager, engineer, viewer — plus members per project.",
        "Admin console: system health, job queue, audit log, AI models, usage.",
        "See who else has a project open; changes made by someone else are never overwritten silently.",
        "Comments with @mentions, package owners and a notification bell.",
        "Upload a revised BOQ: your decisions and corrections carry over; see what changed between revisions.",
        "RFQ register: numbered RFQs with a status, due dates, quotes, and a bid comparison workbook.",
        "Search all projects by words or by meaning; saved filter views; bulk actions; trash with automatic emptying.",
        "Security: one-time set-up link, sign-in throttling, idle sign-out, upload checks.",
    ]),
]

TOUR = [
    ("New project", "Upload the BOQ — Excel or PDF, Arabic or English — and its specs and drawings if you have them."),
    ("Special cases", "Approve or deny each flowchart special case (marble with masonry? one package for several BOQs?)."),
    ("Work packages", "Check the packages; move an item to another category if needed — the app remembers your choice."),
    ("Export & RFQs", "Export the package PDFs, then draft the RFQ letters; they enter the RFQ register."),
    ("RFQ register", "Track each RFQ: issue, record quotes, compare bids, award."),
]


@bp.app_context_processor
def _tour_ctx():
    u = g.get("user")
    if not u:
        return {}
    return {"show_tour": not u.get("tour_done"), "tour_steps": TOUR,
            "show_whats_new": (u.get("seen_version") or "") != APP_VERSION and bool(u.get("tour_done")),
            "app_version": APP_VERSION}


@bp.route("/help")
@login_required
def help_page():
    return render_template("help.html", tour=TOUR)


@bp.route("/whats-new")
@login_required
def whats_new():
    current_app.config["DB"].x("UPDATE users SET seen_version=? WHERE id=?", (APP_VERSION, g.user["id"]))
    return render_template("whats_new.html", notes=WHATS_NEW)


@bp.route("/api/tour/done", methods=["POST"])
@login_required
def tour_done():
    current_app.config["DB"].x("UPDATE users SET tour_done=1, seen_version=? WHERE id=?", (APP_VERSION, g.user["id"]))
    return jsonify(ok=True)
