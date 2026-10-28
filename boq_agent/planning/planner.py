"""Deterministic flowchart engine: classified items -> work packages + special-case proposals."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from typing import Optional

from ..knowledge.rules import MISC, FlowchartRules
from ..models import BOQDocument, Item, Plan, Proposal, WorkPackage
from ..text_utils import truncate


class Planner:
    def __init__(self, rules: FlowchartRules):
        self.rules = rules

    # ----------------------------------------------------------------- keys
    def _default_key(self, it: Item, doc_secs) -> str:
        """Where the item goes when NO special case is applied (= the 'deny' world)."""
        s = doc_secs[it.section_uid]
        mode = self.rules.mode(s.division)
        if mode == "split":
            if it.category in self.rules.split_ids:
                return it.category
            return f"{MISC}{s.division}"
        if mode == "per_item":
            # one WP per BOQ *description*: sub-items (AR 21.1, 21.2 …) stay with their parent (AR 21.0),
            # and sizes / colours / codes of one product stay together (variant group)
            return f"ITEM:{it.parent_row_uids[0] if it.parent_row_uids else (it.variant_group or it.uid)}"
        return f"DIV{s.division}" if s.division else f"SEC:{s.uid}"

    def _resolve(self, spec: dict, it: Item, default: str, present: set[str]) -> Optional[str]:
        t = spec.get("type")
        if t == "merge_into":
            target = spec["target"]
            if spec.get("only_if_target_exists") and target not in present:
                # e.g. no explicit earthwork package in this BOQ → create one from these items
                return self._resolve(spec["else"], it, default, present) if spec.get("else") else None
            return target
        if t == "standalone":
            return spec["target"]
        if t == "section_default":
            return default
        return None

    # ----------------------------------------------------------------- build
    def build(self, doc: BOQDocument, previous: Optional[Plan] = None) -> Plan:
        decisions = {p.proposal_id: p.decision for p in (previous.proposals if previous else [])}
        secs = doc.section_map()
        self._fmap = doc.file_map() if doc.multi_file else {}
        items = [it for s in doc.sections for it in (doc.item_map()[u] for u in s.item_uids)]
        specials = set(self.rules.special_ids)
        plan = Plan()

        default = {it.uid: self._default_key(it, secs) for it in items}
        present = {default[it.uid] for it in items if it.category not in specials}
        assign: dict[str, str] = {}
        rule_tags: dict[str, set[str]] = defaultdict(set)

        # 1) special categories (ovals)
        groups: dict[tuple, list[Item]] = OrderedDict()
        for it in items:
            if it.category not in specials:
                assign[it.uid] = default[it.uid]
                continue
            rule = self.rules.rule_for_category(it.category)
            if rule is None:
                assign[it.uid] = it.category
                continue
            fallback = it.category
            approve = self._resolve(rule["approve"], it, default[it.uid], present)
            deny = self._resolve(rule["deny"], it, default[it.uid], present) or fallback
            if approve is None:
                approve = deny
                note = (f"{rule['title_en']}: target package not present in this BOQ, so "
                        f"{self.rules.wp_names(fallback)[0]} is issued as its own package.")
                if note not in plan.notes:
                    plan.notes.append(note)
            if approve == deny:
                assign[it.uid] = approve
                continue
            groups.setdefault((rule["id"], approve, deny), []).append(it)

        for (rule_id, approve, deny), its in groups.items():
            rule = next(r for r in self.rules.special_rules if r["id"] == rule_id)
            pid = f"{rule_id}|{approve}|{deny}"
            decision = decisions.get(pid, "pending")
            fmt = dict(n=len(its), sources=self._sources(its, secs), from_wp=self.rules.wp_names(deny)[0],
                       to_wp=self.rules.wp_names(approve)[0])
            plan.proposals.append(Proposal(
                proposal_id=pid, rule_id=rule_id,
                title_en=rule["title_en"], title_ar=rule.get("title_ar", ""),
                message_en=rule["message_en"].format(**fmt), message_ar=rule.get("message_ar", "").format(**fmt),
                item_uids=[it.uid for it in its], approve_key=approve, deny_key=deny,
                approve_label=self.rules.wp_names(approve)[0], deny_label=self.rules.wp_names(deny)[0],
                decision=decision))
            for it in its:
                assign[it.uid] = deny if decision == "denied" else approve
                rule_tags[assign[it.uid]].add(rule_id)

        # 2) per-item divisions (Div 10 oval)
        per_item_divs: dict[str, list[Item]] = OrderedDict()
        for it in items:
            if assign[it.uid].startswith("ITEM:"):
                per_item_divs.setdefault(secs[it.section_uid].division or "", []).append(it)
        for div, its in per_item_divs.items():
            rule = self.rules.rule_for_division(div)
            n_groups = len({assign[it.uid] for it in its})
            if rule is None or n_groups < 2:
                continue
            pid = f"{rule['id']}|ITEMS|DIV{div}"
            decision = decisions.get(pid, "pending")
            fmt = dict(n=n_groups, sources=self._sources(its, secs), from_wp="")
            plan.proposals.append(Proposal(
                proposal_id=pid, rule_id=rule["id"], title_en=rule["title_en"], title_ar=rule.get("title_ar", ""),
                message_en=rule["message_en"].format(**fmt), message_ar=rule.get("message_ar", "").format(**fmt),
                item_uids=[it.uid for it in its], approve_key="ITEMS", deny_key=f"DIV{div}",
                approve_label=f"{n_groups} separate work packages", deny_label=self.rules.wp_names(f"DIV{div}")[0],
                decision=decision))
            for it in its:
                if decision == "denied":
                    assign[it.uid] = f"DIV{div}"
                rule_tags[assign[it.uid]].add(rule["id"])

        # 3) the same package coming from several BOQs (multi-file projects): one RFQ, or one per BOQ?
        if doc.multi_file:
            self._cross_boq(items, assign, default, secs, decisions, plan, rule_tags)

        # 4) work packages in BOQ order
        by_key: dict[str, list[Item]] = OrderedDict()
        for it in items:
            by_key.setdefault(assign[it.uid], []).append(it)
        for n, (key, its) in enumerate(by_key.items(), 1):
            name_en, name_ar, div = self._names(key, its, secs)
            wp = WorkPackage(
                wp_id=f"WP-{n:02d}", key=key, name_en=name_en, name_ar=name_ar, division=div,
                division_name=self.rules.division_name(div),
                item_uids=[it.uid for it in its],
                source_sections=list(OrderedDict.fromkeys(it.section_uid for it in its)),
                source_files=list(OrderedDict.fromkeys(secs[it.section_uid].file_id for it in its)),
                rule_ids=sorted(rule_tags.get(key, set())))
            low = [it for it in its if it.method.startswith("llm") and it.confidence < 0.6]
            if low:
                wp.notes.append(f"{len(low)} item(s) classified with low confidence — please review.")
            plan.work_packages.append(wp)
        return plan

    def _cross_boq(self, items, assign, default, secs, decisions, plan, rule_tags) -> None:
        """A package whose own items (not ones moved in by a flowchart oval — that oval was already approved)
        come from 2+ BOQ files → Approve: ONE work package / RFQ;  Deny: one package per BOQ."""
        members: dict[str, list[Item]] = OrderedDict()
        for it in items:
            if not assign[it.uid].startswith("ITEM:"):
                members.setdefault(assign[it.uid], []).append(it)
        for key, its in members.items():
            files = list(OrderedDict.fromkeys(secs[it.section_uid].file_id for it in its
                                              if assign[it.uid] == default[it.uid]))
            if len(files) < 2:
                continue
            pid = f"XBOQ|{key}"
            decision = decisions.get(pid, "pending")
            name_en, name_ar, _ = self._names(key, its, secs)
            labels = ", ".join(self._fmap[f].label for f in files)
            n = len(files)
            plan.proposals.append(Proposal(
                proposal_id=pid, rule_id="XBOQ_MERGE",
                title_en=f"{name_en} → one package for {n} BOQs",
                title_ar=f"{name_ar or name_en} ← باقة واحدة لعدد {n} مقايسات",
                message_en=f"'{name_en}' appears in {n} BOQs ({labels}). Approve to issue ONE work package and ONE RFQ "
                           f"for all of them, or Deny to keep a separate package per BOQ.",
                message_ar=f"الباقة '{name_ar or name_en}' موجودة في عدد {n} مقايسات ({labels}). وافق لإصدار باقة عمل "
                           f"وطلب عرض سعر واحد لها جميعاً، أو ارفض لإبقاء باقة منفصلة لكل مقايسة.",
                item_uids=[it.uid for it in its], approve_key=key, deny_key=f"{key}@*",
                approve_label=f"One {name_en} package", deny_label=f"{n} separate packages (one per BOQ)",
                decision=decision))
            if decision == "denied":
                tags = rule_tags.pop(key, set())
                for it in its:
                    f = secs[it.section_uid].file_id
                    assign[it.uid] = f"{key}@{f if f in files else files[0]}"   # moved-in items join their own BOQ's part
                    rule_tags[assign[it.uid]] |= tags
            else:
                rule_tags[key].add("XBOQ_MERGE")

    # --------------------------------------------------------------- helpers
    def _names(self, key: str, its: list[Item], secs) -> tuple[str, str, str]:
        if "@" in key:                                   # per-BOQ part of a package (cross-BOQ merge denied)
            base, fid = key.rsplit("@", 1)
            en, ar, div = self._names(base, its, secs)
            label = self._fmap[fid].label if fid in getattr(self, "_fmap", {}) else fid
            return f"{en} — {label}", (f"{ar} — {label}" if ar else ""), div
        if key.startswith("ITEM:"):
            it = its[0]
            s = secs[it.section_uid]
            label = it.parent_text or (it.headings[-1] if len(its) > 1 and it.headings else it.description)
            no = it.item_no if len(its) == 1 or not it.item_no else it.item_no.rsplit(".", 1)[0]
            return (f"Div {s.division}: {truncate(label, 45)}" + (f" ({no})" if no else ""), "", s.division or "")
        if key.startswith("SEC:"):
            s = secs[key[4:]]
            return (truncate(s.title, 80), "", "")
        return self.rules.wp_names(key)

    def _sources(self, its: list[Item], secs) -> str:
        fmap = getattr(self, "_fmap", {})

        def title(s) -> str:
            t = truncate(s.title, 50)
            return f"{t} [{fmap[s.file_id].label}]" if s.file_id in fmap else t

        titles = list(OrderedDict.fromkeys(title(secs[it.section_uid]) for it in its))
        return "; ".join(titles[:3]) + (" …" if len(titles) > 3 else "")


def snapshot_rows(doc: BOQDocument, wp: WorkPackage) -> list[str]:
    """Ordered row uids (excluding table headers) that make up the WP cut-out."""
    order = {r.uid: i for i, r in enumerate(doc.rows)}
    items = doc.item_map()
    secs = doc.section_map()
    uids: set[str] = set()
    wp_items = set(wp.item_uids)
    for iu in wp.item_uids:
        it = items[iu]
        uids.update(it.row_uids)
        uids.update(it.context_row_uids)
    for su in wp.source_sections:
        s = secs[su]
        uids.update(s.title_row_uids)
        if set(s.item_uids) <= wp_items:
            uids.update(s.subtotal_row_uids)
    return sorted(uids, key=lambda u: order.get(u, 0))
