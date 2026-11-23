"""BOQ revisions: a revised BOQ becomes a new project linked to the previous one (its history stays).

carry_forward()  the user's work on the previous revision is applied to the new one:
                   special-case decisions (same case id), item categories set by hand (same item text),
                   section divisions set by hand (same section title), package names, the user's choice of spec
                   sections / sheets per package, and the RFQ form (values, suppliers, template)
compare()        item by item: new / changed (qty, unit, rate, package) / removed / unchanged, plus per package —
                   and which packages already had RFQs issued (an addendum is needed)
Unchanged inputs cost almost nothing to re-run: identical AI questions are answered from the cache (fleet.py)
and drawings / specs already read are reused (content-addressed cache of the engine).
"""
from __future__ import annotations

from typing import Optional

from boq_agent.text_utils import norm


def item_key(it) -> str:
    return " ".join(norm(it.full_description or "").split())[:500]


def full_key(it) -> str:
    return f"{(it.item_no or '').strip().lower()}|{item_key(it)}"


def carry_forward(runtime, src_run: str, session, agent) -> str:
    store = runtime.store
    try:
        src = store.load(src_run)
    except Exception:  # noqa: BLE001
        return ""
    counts = {"decisions": 0, "items": 0, "sections": 0, "names": 0, "documents": 0, "rfq": 0}

    # 1. section divisions set by hand (same section title) — re-classifies that section's items
    old_secs = {}
    for sec in src.document.sections:
        if sec.division_method == "manual":
            old_secs[" ".join(norm(sec.title).split())] = sec.division
    for sec in session.document.sections:
        d = old_secs.get(" ".join(norm(sec.title).split()), "__none__")
        if d != "__none__" and sec.division != d:
            agent.set_section_division(session, sec.uid, d)
            counts["sections"] += 1

    # 2. item categories set by hand (same item text; with the item number when the text repeats)
    by_full, by_text = {}, {}
    for it in src.document.items:
        if it.method == "manual":
            by_full[full_key(it)] = it.category
            by_text.setdefault(item_key(it), []).append(it.category)
    for it in session.document.items:
        if full_key(it) in by_full:
            cat = by_full[full_key(it)]
        elif len(set(by_text.get(item_key(it), []))) == 1:
            cat = by_text[item_key(it)][0]
        else:
            continue
        it.category, it.method, it.confidence = cat, "manual", 1.0
        it.reason = "set by user in the previous revision"
        counts["items"] += 1

    # 3. package names and document choices (by package key)
    for k, v in src.wp_names.items():
        if v and not session.wp_names.get(k):
            session.wp_names[k] = v
            counts["names"] += 1
    refs = set(session.documents.ref_map()) if session.documents.files else set()
    for wp_key, ov in (src.documents.overrides or {}).items():
        add = [r for r in ov.get("add", []) if r in refs]
        rem = [r for r in ov.get("remove", []) if r in refs]
        if add or rem:
            session.documents.overrides[wp_key] = {"add": add, "remove": rem}
            counts["documents"] += len(add) + len(rem)

    # re-plan with the corrections, THEN apply the special-case decisions (ids depend on the plan)
    agent.replan(session)
    old_dec = {p.proposal_id: p.decision for p in src.plan.proposals if p.decision != "pending"}
    ids_by = {"approved": [], "denied": []}
    for p in session.plan.proposals:
        d = old_dec.get(p.proposal_id)
        if d in ids_by and p.decision == "pending":
            ids_by[d].append(p.proposal_id)
    for d, ids in ids_by.items():
        if ids:
            agent.decide_many(session, ids, d)
            counts["decisions"] += len(ids)

    # 4. the RFQ form
    if src.rfq_draft:
        keep = {k: v for k, v in src.rfq_draft.items() if k in ("values", "confirmed", "diff_date", "suppliers", "skip",
                                                                    "template")}
        if keep:
            session.rfq_draft.update(keep)
            counts["rfq"] = 1
    agent.save(session)
    parts = [f"{v} {k}" for k, v in counts.items() if v and k != "rfq"] + (["RFQ form"] if counts["rfq"] else [])
    return ("carried over: " + ", ".join(parts)) if parts else ""


def compare(old, new, issued_keys: Optional[set] = None) -> dict:
    """old/new: engine sessions. Returns {'items': [...], 'packages': [...], 'counts': {...}}."""
    issued_keys = issued_keys or set()

    def wp_map(s):
        m = {}
        for wp in s.plan.work_packages:
            for u in wp.item_uids:
                m[u] = wp
        return m
    wo, wn = wp_map(old), wp_map(new)

    def index(items):
        full, text = {}, {}
        for it in items:
            full.setdefault(full_key(it), []).append(it)
            text.setdefault(item_key(it), []).append(it)
        return full, text
    of, ot = index(old.document.items)
    used_old: set[str] = set()
    rows = []
    for it in new.document.items:
        cand = [x for x in of.get(full_key(it), []) if x.uid not in used_old] or \
               [x for x in ot.get(item_key(it), []) if x.uid not in used_old]
        o = cand[0] if cand else None
        if o is None:
            # same item number, different text = a changed description
            same_no = [x for x in old.document.items if x.uid not in used_old and it.item_no and
                       x.item_no == it.item_no and x.unit == it.unit]
            o = same_no[0] if len(same_no) == 1 else None
        nw = wn.get(it.uid)
        if o is None:
            rows.append({"status": "new", "new": it, "old": None, "changes": [], "wp_new": nw, "wp_old": None})
            continue
        used_old.add(o.uid)
        ow = wo.get(o.uid)
        changes = []
        if item_key(o) != item_key(it):
            changes.append("description")
        for f, label in (("quantity", "qty"), ("unit", "unit"), ("rate", "rate")):
            if str(getattr(o, f) or "").strip() != str(getattr(it, f) or "").strip():
                changes.append(label)
        if (ow.key if ow else None) != (nw.key if nw else None):
            changes.append("package")
        rows.append({"status": "changed" if changes else "unchanged", "new": it, "old": o, "changes": changes,
                     "wp_new": nw, "wp_old": ow})
    for it in old.document.items:
        if it.uid not in used_old:
            rows.append({"status": "removed", "new": None, "old": it, "changes": [], "wp_new": None,
                         "wp_old": wo.get(it.uid)})
    pk: dict[str, dict] = {}
    for r in rows:
        for side in ("wp_new", "wp_old"):
            wp = r[side]
            if wp is None:
                continue
            p = pk.setdefault(wp.key, {"key": wp.key, "name": wp.name_en, "new": 0, "changed": 0, "removed": 0,
                                       "unchanged": 0, "issued": wp.key in issued_keys})
            if side == "wp_new" or r["status"] == "removed":
                p[r["status"]] += 1
                break
    for p in pk.values():
        p["touched"] = bool(p["new"] or p["changed"] or p["removed"])
        p["addendum"] = p["issued"] and p["touched"]
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("new", "changed", "removed", "unchanged")}
    order = {"new": 0, "changed": 1, "removed": 2, "unchanged": 3}
    rows.sort(key=lambda r: order[r["status"]])
    return {"items": rows, "packages": sorted(pk.values(), key=lambda p: (not p["touched"], p["name"])),
            "counts": counts}
