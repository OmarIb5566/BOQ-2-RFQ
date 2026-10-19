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
    def assign_divisions(self, doc: BOQDocument) -> None:
        items = doc.item_map()
        pending: list[Section] = []
        for s in doc.sections:
            m = _EXPLICIT_DIV.search(norm(s.title)) or _EXPLICIT_DIV.search(norm(s.sheet or ""))
            if m and m.group(1).zfill(2) in self.rules.divisions:
                s.division, s.division_confidence, s.division_method = m.group(1).zfill(2), 1.0, "explicit"
            else:
                pending.append(s)
        if not pending:
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
        for s in doc.sections:
            self.log(f"Section '{truncate(s.title, 60)}' → Div {s.division or '??'} "
                     f"{self.rules.division_name(s.division)} ({s.division_method}, {s.division_confidence:.2f})")

    # ----------------------------------------------------------------- items
    def allowed_categories(self, section: Section) -> list[str]:
        mode = self.rules.mode(section.division)
        if mode == "split":
            return self.rules.split_ids + self.rules.special_ids + [MISC]
        if mode == "as_is":
            return self.rules.special_ids + [NONE]
        return []

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
                                    menu=self.rules.category_menu(allowed), items=text)
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
                                          menu=self.rules.category_menu(allowed), item=it.llm_text(500, 600))
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
