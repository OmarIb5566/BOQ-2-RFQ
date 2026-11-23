"""Who may do what — one place for every permission decision.

Global roles (set by an administrator):
    admin     everything: users, settings, every project, the admin console
    manager   every project (view + edit + membership), approves RFQs, sees the job queue; no user administration
    engineer  creates projects; works on own, team-shared and member projects
    viewer    reads team-shared and member projects, comments; changes nothing

Per project: the owner, plus members added as 'editor' or 'viewer'. "Shared with the team" lets every engineer edit
and every viewer read the project.
"""
from __future__ import annotations

from typing import Optional

ROLES = ["admin", "manager", "engineer", "viewer"]
ROLE_LABEL = {"admin": "Administrator", "manager": "Manager", "engineer": "Engineer", "viewer": "Viewer"}
MEMBER_ROLES = ["editor", "viewer"]


def is_admin(user: Optional[dict]) -> bool:
    return bool(user) and user["role"] == "admin"


def sees_all(user: Optional[dict]) -> bool:
    return bool(user) and user["role"] in ("admin", "manager")


def member_role(db, run_id: str, user_id: int) -> Optional[str]:
    r = db.one("SELECT role FROM project_members WHERE run_id=? AND user_id=?", (run_id, user_id))
    return r["role"] if r else None


def can_view(db, user: dict, row: dict) -> bool:
    if sees_all(user) or row["owner_id"] == user["id"] or row["shared"]:
        return True
    return member_role(db, row["run_id"], user["id"]) is not None


def can_edit(db, user: dict, row: dict) -> bool:
    if user["role"] == "viewer":
        return False
    if sees_all(user) or row["owner_id"] == user["id"]:
        return True
    m = member_role(db, row["run_id"], user["id"])
    if m is not None:
        return m == "editor"
    return bool(row["shared"])


def can_manage(user: dict, row: dict) -> bool:
    """Rename, share, delete, membership."""
    return sees_all(user) or (row["owner_id"] == user["id"] and user["role"] != "viewer")


def can_create(user: dict) -> bool:
    return user["role"] in ("admin", "manager", "engineer")


def visible_sql(user: dict, alias: str = "p") -> tuple[str, tuple]:
    """WHERE fragment limiting a projects query to what the user may see."""
    if sees_all(user):
        return "1=1", ()
    return (f"({alias}.owner_id=? OR {alias}.shared=1 OR {alias}.run_id IN "
            "(SELECT run_id FROM project_members WHERE user_id=?))", (user["id"], user["id"]))


# RFQ register transitions: (from, to) -> roles allowed
RFQ_FLOW = {
    ("draft", "approved"): {"admin", "manager"},
    ("approved", "issued"): {"admin", "manager", "engineer"},
    ("issued", "clarification"): {"admin", "manager", "engineer"},
    ("clarification", "issued"): {"admin", "manager", "engineer"},
    ("issued", "quoted"): {"admin", "manager", "engineer"},
    ("clarification", "quoted"): {"admin", "manager", "engineer"},
    ("issued", "declined"): {"admin", "manager", "engineer"},
    ("clarification", "declined"): {"admin", "manager", "engineer"},
    ("quoted", "evaluated"): {"admin", "manager", "engineer"},
    ("evaluated", "awarded"): {"admin", "manager"},
    ("evaluated", "not_awarded"): {"admin", "manager"},
    ("draft", "cancelled"): {"admin", "manager", "engineer"},
    ("approved", "cancelled"): {"admin", "manager"},
    ("issued", "cancelled"): {"admin", "manager"},
    ("clarification", "cancelled"): {"admin", "manager"},
    ("quoted", "cancelled"): {"admin", "manager"},
    ("approved", "draft"): {"admin", "manager"},
}


def rfq_moves(user: dict, status: str) -> list[str]:
    return [to for (fr, to), roles in RFQ_FLOW.items() if fr == status and user["role"] in roles]
