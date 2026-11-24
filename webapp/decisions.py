"""Special-case decisions remembered across projects.

Every Approve / Deny a user clicks is stored (decision_memory). When ANY project is analysed, each open special case
gets the most recent decision taken for
    1. the same kind of case — same rule, same "approve into" and "keep in" packages
       (e.g. earthwork moved into Div 31 out of Div 21 Fire Suppression), else
    2. the same rule (e.g. any earthwork case), else it stays open.
The decision is applied, but marked on the Special cases tab ("Remembered: approved in Project X on …") so the user
checks it; changing it is one click and is remembered in turn. A revised BOQ takes its own project's previous
decisions first (revisions.py); this memory only fills what is still open. Admin › AI models can switch it off.
"""
from __future__ import annotations

from typing import Optional

from .db import new_id, now


def case_key(p) -> str:
    return f"{p.rule_id}|{p.approve_key}|{p.deny_key}"


def enabled(db) -> bool:
    from .fleet import config
    return bool(config(db).get("decisions_on", True))


def remember(db, user_id: Optional[int], run_id: str, proposals: list, decision: str) -> int:
    if decision not in ("approved", "denied"):
        return 0
    for p in proposals:
        db.x("INSERT INTO decision_memory (id, ts, user_id, run_id, rule_id, case_key, decision, title, approve_label, "
             "deny_label) VALUES (?,?,?,?,?,?,?,?,?,?)",
             (new_id(), now(), user_id, run_id, p.rule_id, case_key(p), decision, p.title_en[:300],
              (p.approve_label or "")[:200], (p.deny_label or "")[:200]))
    return len(proposals)


def overview(db) -> list[dict]:
    """One row per kind of case: its current (latest) decision, how often it was decided, last project and person."""
    rows = db.q("""SELECT m.*, p.name pname, u.full_name who,
                   (SELECT COUNT(*) FROM decision_memory x WHERE x.case_key=m.case_key) n,
                   (SELECT COUNT(*) FROM decision_memory x WHERE x.case_key=m.case_key AND x.decision='approved') n_app
                   FROM decision_memory m LEFT JOIN projects p ON p.run_id=m.run_id LEFT JOIN users u ON u.id=m.user_id
                   WHERE m.rowid = (SELECT y.rowid FROM decision_memory y WHERE y.case_key=m.case_key
                                    ORDER BY y.ts DESC, y.rowid DESC LIMIT 1)
                   ORDER BY m.rule_id, m.title""")
    return [dict(r) for r in rows]


def set_default(db, user_id: Optional[int], key: str, decision: str) -> bool:
    """Change what future projects get for this kind of case (a new latest entry; the history is kept)."""
    r = db.one("SELECT * FROM decision_memory WHERE case_key=? ORDER BY ts DESC, rowid DESC LIMIT 1", (key,))
    if r is None or decision not in ("approved", "denied"):
        return False
    db.x("INSERT INTO decision_memory (id, ts, user_id, run_id, rule_id, case_key, decision, title, approve_label, "
         "deny_label, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
         (new_id(), now(), user_id, "", r["rule_id"], key, decision, r["title"], r["approve_label"], r["deny_label"],
          "set by hand"))
    return True


def forget_key(db, key: str) -> int:
    n = db.one("SELECT COUNT(*) n FROM decision_memory WHERE case_key=?", (key,))["n"]
    db.x("DELETE FROM decision_memory WHERE case_key=?", (key,))
    return n


def _latest(db, col: str, value: str, exclude_run: str):
    return db.one(f"SELECT m.*, p.name pname FROM decision_memory m LEFT JOIN projects p ON p.run_id=m.run_id "
                  f"WHERE m.{col}=? AND m.run_id<>? ORDER BY m.ts DESC, m.rowid DESC LIMIT 1", (value, exclude_run))


def apply(db, agent, session, run_id: str) -> int:
    """Decide the open cases from earlier decisions (one re-plan per decision). Returns how many were decided."""
    if not enabled(db):
        return 0
    picks: dict[str, list[str]] = {"approved": [], "denied": []}
    notes = {}
    for p in session.plan.proposals:
        if p.decision != "pending":
            continue
        r = _latest(db, "case_key", case_key(p), run_id)
        match = "same case"
        if r is None:
            r = _latest(db, "rule_id", p.rule_id, run_id)
            match = "same rule"
        if r is None or r["decision"] not in picks:
            continue
        picks[r["decision"]].append(p.proposal_id)
        notes[p.proposal_id] = {"decision": r["decision"], "from_run": r["run_id"],
                                "from_name": r["pname"] or ("Remembered decisions" if not r["run_id"] else ""),
                                "ts": r["ts"], "match": match}
    n = 0
    for d, ids in picks.items():
        if ids:
            agent.decide_many(session, ids, d)
            n += len(ids)
    if notes:
        # ids can change after a re-plan only for cases that disappeared; keep notes of those still present
        present = {p.proposal_id for p in session.plan.proposals}
        session.remembered = {k: v for k, v in notes.items() if k in present}
        agent.save(session)
    return n


def forget(session, proposal_ids) -> None:
    """A user decided these cases by hand: they are no longer 'remembered'."""
    for i in proposal_ids:
        session.remembered.pop(i, None)
