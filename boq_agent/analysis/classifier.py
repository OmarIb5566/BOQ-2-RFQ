"""LLM reasoning steps: section -> CSI division, item -> flowchart work-package category."""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..config import Settings
from ..knowledge.rules import MISC, NONE, FlowchartRules
from ..llm import prompts as P
from ..llm.client import LLMError, OllamaClient
from ..models import BOQDocument, Item, Section
from ..text_utils import norm, truncate
from .variants import consolidate, variant_groups

_EXPLICIT_DIV = re.compile(r"\b(?:division|divi?|div\.?)\s*[:\-]?\s*0?(\d{1,2})\b", re.I)


class Classifier:
    def __init__(self, rules: FlowchartRules, settings: Settings, llm: Optional[OllamaClient],
                 log: Callable[[str], None] = print, progress: Callable[[float, str], None] = lambda p, m: None):
        self.rules = rules
        self.settings = settings
        self.llm = llm if settings.use_llm else None
        self.log = log
        self.progress = progress
        self.system = P.system_prompt(rules)

    # ------------------------------------------------------------ divisions
    def split_mixed_sections(self, doc: BOQDocument) -> None:
        """A sheet without 'الباب …' / 'Division NN' titles whose item groups name different trades
        (أعمال الحفر والردم / أعمال الخرسانة / أعمال العزل …) is split into one section per trade group,
        so concrete in a structural BOQ is not packaged as earthwork."""
        from ..ingest.structure import SECTION_RE

        items = doc.item_map()
        out: list[Section] = []
        for s in doc.sections:
            if SECTION_RE.search(norm(s.title)) or _EXPLICIT_DIV.search(norm(s.title)):
                out.append(s)
                continue
            runs: list[tuple[Optional[str], str, list[str]]] = []      # (division hint, group label, item uids)
            cur: Optional[str] = None
            for u in s.item_uids:
                it = items[u]
                label = (it.parent_text.splitlines()[0] if it.parent_text else "") or (it.headings[-1] if it.headings else "")
                label = truncate(label.strip(), 60)
                hint = self.rules.division_hint(label) if label else None
                hint = hint or cur                                      # unlabelled lines continue the current group
                if runs and hint == runs[-1][0]:
                    runs[-1][2].append(u)
                else:
                    runs.append((hint, label, [u]))
                cur = hint
            if len({h for h, _, _ in runs if h}) < 2:
                out.append(s)
                continue
            for k, (hint, label, uids) in enumerate(runs, 1):
                out.append(Section(uid=f"{s.uid}.{k}", title=f"{s.title.strip()} — {label}" if label else s.title,
                                   sheet=s.sheet, file_id=s.file_id, title_row_uids=list(s.title_row_uids),
                                   subtotal_row_uids=list(s.subtotal_row_uids) if k == len(runs) else [],
                                   item_uids=uids))
                for u in uids:
                    items[u].section_uid = f"{s.uid}.{k}"
            self.log(f"Section '{truncate(s.title, 40)}' has no division titles — split into {len(runs)} trade group(s).")
        doc.sections = out

    def assign_divisions(self, doc: BOQDocument) -> None:
        self.split_mixed_sections(doc)
        items = doc.item_map()
        pending: list[Section] = []
        for s in doc.sections:
            m = _EXPLICIT_DIV.search(norm(s.title)) or _EXPLICIT_DIV.search(norm(s.sheet or ""))
            if m and m.group(1).zfill(2) in self.rules.divisions:
                s.division, s.division_confidence, s.division_method = m.group(1).zfill(2), 1.0, "explicit"
            else:
                pending.append(s)
        if not pending:
            self._detect_landscape(doc)
            return
        codes = list(self.rules.divisions) + ["00"]
        if self.llm:
            for i in range(0, len(pending), 6):
                chunk = pending[i:i + 6]
                ids = [f"S{k + 1}" for k in range(len(chunk))]
                blocks = []
                for sid, s in zip(ids, chunk):
                    sample = [items[u] for u in s.item_uids[:8]]
                    heads = sorted({h for it in sample for h in it.headings})[:4]
                    lines = [f"[{sid}] title: {s.title!r}; sheet: {s.sheet or '-'}"]
                    if heads:
                        lines.append("   sub-headings: " + " | ".join(truncate(h, 60) for h in heads))
                    for it in sample:
                        lines.append("   - " + truncate(it.full_description, 160))
                    blocks.append("\n".join(lines))
                menu = ", ".join(f"{c}={d['name_en']}" for c, d in self.rules.divisions.items()) + ", 00=unknown"
                try:
                    ans = self.llm.chat_json(self.system, P.DIVISION_TASK.format(codes=menu, sections="\n".join(blocks)),
                                             P.division_schema(codes, ids))
                    for r in ans.get("results", []):
                        if r.get("section_id") in ids:
                            s = chunk[ids.index(r["section_id"])]
                            if r["division"] != "00":
                                s.division = r["division"]
                                s.division_confidence = float(r.get("confidence", 0.7))
                                s.division_method = "llm"
                except LLMError as e:
                    self.log(f"Division step: LLM unavailable ({e}); using keywords.")
        for s in pending:
            if not s.division:
                hint = self.rules.division_hint(s.title + " " + (s.sheet or ""))
                if hint:
                    s.division, s.division_confidence, s.division_method = hint, 0.5, "keyword"
        self._detect_landscape(doc)
        for s in doc.sections:
            self.log(f"Section '{truncate(s.title, 60)}' → Div {s.division or '??'} "
                     f"{self.rules.division_name(s.division)} ({s.division_method}, {s.division_confidence:.2f})")

    def _detect_landscape(self, doc: BOQDocument) -> None:
        """Sections that are landscape by title / sheet name or by landscape section codes → Div 32
        (an explicit 'Division NN' in the title still wins unless the title itself says landscape)."""
        if "32" not in self.rules.divisions:
            return
        items = doc.item_map()
        for s in doc.sections:
            if s.division == "32":
                continue
            ctx = [h for u in s.item_uids for h in items[u].headings] + [items[u].parent_text for u in s.item_uids]
            by_title = self.rules.is_landscape(s.title, s.sheet or "", [])
            if (s.division_method != "explicit" or by_title) and self.rules.is_landscape(s.title, s.sheet or "", ctx):
                s.division, s.division_confidence, s.division_method = "32", 1.0, "landscape"

    # ----------------------------------------------------------------- items
    def allowed_categories(self, section: Section) -> list[str]:
        return self.rules.allowed_categories(section.division)

    def classify_items(self, doc: BOQDocument, sections: Optional[list[str]] = None) -> None:
        items = doc.item_map()
        work: list[tuple[Section, list[Item], list[str]]] = []
        for s in doc.sections:
            if sections is not None and s.uid not in sections:
                continue
            mode = self.rules.mode(s.division)
            allowed = self.allowed_categories(s)
            to_llm = []
            for uid in s.item_uids:
                it = items[uid]
                it.category, it.confidence, it.reason = None, 0.0, ""
                if mode == "per_item":
                    it.method = "per_item"
                    continue
                cands = [c for c in allowed if c not in (MISC, NONE)]
                it.keyword_hint = self.rules.keyword_hint(it.full_description + " " + " ".join(it.headings), cands)
                if mode == "as_is" and it.keyword_hint is None:
                    # cheap pre-screen: nothing suggests a special case -> stays with its section
                    it.method, it.confidence = "section", 1.0
                    continue
                to_llm.append(it)
            if to_llm:
                work.append((s, to_llm, allowed))

        total = sum(len(w[1]) for w in work) or 1
        done = 0
        for s, its, allowed in work:
            for i in range(0, len(its), self.settings.batch_size):
                batch = its[i:i + self.settings.batch_size]
                self._classify_batch(s, batch, allowed)
                done += len(batch)
                self.progress(done / total, f"Classifying items … {done}/{total}")
        self._review(doc, work)
        self._apply_context_rules(doc, sections)
        self._keep_variants_together(doc)

    def _keep_variants_together(self, doc: BOQDocument) -> None:
        """General rule: one product in different sizes / colours / codes stays in one work package."""
        for it in doc.items:
            it.variant_group = None
        groups = variant_groups(doc, self.rules.variants)
        n = consolidate(doc, groups, self.log)
        if groups:
            self.log(f"Variant groups: {len(set(groups.values()))} group(s), {len(groups)} items; {n} re-aligned.")

    def _apply_context_rules(self, doc: BOQDocument, sections: Optional[list[str]]) -> None:
        """Deterministic overrides from the group an item sits under (final say over the LLM)."""
        items = doc.item_map()
        for s in doc.sections:
            if sections is not None and s.uid not in sections:
                continue
            allowed = self.allowed_categories(s)
            for uid in s.item_uids:
                it = items[uid]
                hit = self.rules.context_override(it.headings, it.parent_text, it.description, it.category)
                if hit is None or hit[0] not in allowed:
                    continue
                self.log(f"{it.item_no or it.uid}: {it.category or 'section default'} → {hit[0]} (context rule {hit[1]})")
                it.category, it.confidence, it.method = hit[0], 1.0, "context-rule"
                it.reason = f"heading/group context ({hit[1]})"

    def _classify_batch(self, s: Section, batch: list[Item], allowed: list[str]) -> None:
        if not self.llm:
            for it in batch:
                self._keyword_fallback(it, allowed)
            return
        ids = [f"I{k + 1}" for k in range(len(batch))]
        text = "\n".join(f"[{iid}] {it.llm_text()}" for iid, it in zip(ids, batch))
        prompt = P.ITEM_TASK.format(section=truncate(s.title, 100), division=s.division or "??",
                                    division_name=self.rules.division_name(s.division),
                                    menu=self.rules.category_menu(allowed, s.division), items=text)
        try:
            ans = self.llm.chat_json(self.system, prompt, P.items_schema(allowed, ids))
        except LLMError as e:
            self.log(f"LLM batch failed ({e}); keyword fallback for {len(batch)} item(s).")
            for it in batch:
                self._keyword_fallback(it, allowed)
            return
        got = {}
        for r in ans.get("results", []):
            if r.get("item_id") in ids and r.get("category") in allowed:
                got[r["item_id"]] = r
        for iid, it in zip(ids, batch):
            r = got.get(iid)
            if r is None:
                it.method, it.confidence = "llm-missing", 0.0
                it.category = it.keyword_hint
                continue
            it.category = None if r["category"] == NONE else r["category"]
            it.confidence = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
            it.reason = truncate(r.get("reason", ""), 120)
            it.method = "llm"

    def _keyword_fallback(self, it: Item, allowed: list[str]) -> None:
        it.category = it.keyword_hint if it.keyword_hint in allowed else (MISC if MISC in allowed else None)
        it.confidence, it.method = 0.4, "keyword"

    def _review(self, doc: BOQDocument, work) -> None:
        """Reflection step: re-examine doubtful items one by one with thinking enabled."""
        if not self.llm:
            return
        sec = doc.section_map()
        doubtful = []
        for s, its, allowed in work:
            for it in its:
                disagree = it.keyword_hint is not None and it.category != it.keyword_hint
                if it.method == "llm-missing" or it.confidence < self.settings.review_threshold or disagree:
                    doubtful.append((s, it, allowed))
        if doubtful:
            self.log(f"Reviewing {len(doubtful)} doubtful item(s) …")
        for k, (s, it, allowed) in enumerate(doubtful, 1):
            self.progress(k / len(doubtful), f"Reviewing doubtful items … {k}/{len(doubtful)}")
            prompt = P.REVIEW_TASK.format(first=it.category or NONE, conf=it.confidence, hint=it.keyword_hint or "-",
                                          section=truncate(sec[it.section_uid].title, 100),
                                          division=s.division or "??", division_name=self.rules.division_name(s.division),
                                          menu=self.rules.category_menu(allowed, s.division), item=it.llm_text(500, 600))
            try:
                ans = self.llm.chat_json(self.system, prompt, P.review_schema(allowed), think=True)
            except LLMError as e:
                self.log(f"Review failed for {it.item_no or it.uid}: {e}")
                continue
            if ans.get("category") in allowed:
                it.category = None if ans["category"] == NONE else ans["category"]
                it.confidence = max(0.0, min(1.0, float(ans.get("confidence", 0.6))))
                it.reason = truncate(ans.get("reason", ""), 120)
                it.method = "llm-review"
