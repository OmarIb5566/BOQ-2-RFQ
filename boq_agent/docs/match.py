"""Spec sections & drawing sheets -> work packages.

Evidence, strongest first; each link records its method, a human-readable reason and a confidence:
  spec section                                   drawing sheet
  1. code   — the BOQ cites the section number     1. drawing ref — the BOQ cites the drawing number
  2. title  — the section title names a package's  2. legend — a legend / note line names a package's
             category (keywords of the flowchart)            material (keywords of the flowchart)
  3. division — same CSI division as the package   3. sheet type — general sheets a trade always needs
                                                              (document_rules.json: layout_sheets)
  4. llm — only for what none of the above placed: one short question, answers restricted to the
           package keys (cannot invent packages), cached by content.
User corrections (overrides) are applied last and survive re-planning (stored per work-package key).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..knowledge.rules import MISC, FlowchartRules
from ..models import DocLink, DrawingSheet, ProjectDocs, Session, SpecSection, WorkPackage
from ..text_utils import keyword_hits, norm, truncate
from .common import Cache, DocRules, key_of

PROMPT_VERSION = "m1"
GENERAL_KINDS = ("general", "cover")
_CITED_CODE = re.compile(r"(?:section|sec\.?|قسم|القسم)\s*[:\-]?\s*(\d{2})\s?(\d{2})\s?(\d{2})(?!\d)|"
                         r"(?:section|sec\.?|قسم|القسم)\s*[:\-]?\s*(\d{5})(?!\d)|"
                         r"(?<!\d)(\d{2}) (\d{2}) (\d{2})(?!\d)", re.I)
_CITED_DWG = re.compile(r"(?:dwg|drawing|detail|sheet|لوحه|لوحة)\s*(?:no\.?|number|رقم)?\s*[:#]?\s*([A-Za-z]{0,4}[-_.]?[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+){0,5})", re.I)


def _alnum(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


@dataclass
class WPProfile:
    wp: WorkPackage
    division: str
    cats: set[str] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)      # cited section codes: 4-digit groups ("0420") + old 5-digit
    dwg_refs: set[str] = field(default_factory=set)   # cited drawing numbers (alphanumeric only)
    sample: str = ""


def profiles(session: Session, rules: FlowchartRules) -> list[WPProfile]:
    items = session.document.item_map()
    out = []
    for wp in session.plan.work_packages:
        base = wp.key.split("@", 1)[0]
        pr = WPProfile(wp=wp, division=wp.division or "")
        if base in rules.categories:
            pr.cats.add(base)
        if base.startswith("ITEM:MF"):
            pr.codes.add(base[len("ITEM:MF"):])
        texts = []
        for u in wp.item_uids:
            it = items[u]
            if it.category and it.category not in ("NONE", MISC):
                pr.cats.add(it.category)
                pr.cats.add(rules.package_alias.get(it.category, it.category))
            texts += [*it.headings[-2:], it.parent_text, it.description]
        blob = "\n".join(dict.fromkeys(t for t in texts if t))
        for m in _CITED_CODE.finditer(blob):
            g = m.groups()
            if g[0]:
                pr.codes.add(g[0] + g[1])
            elif g[3]:
                pr.codes.add(g[3])
            elif g[4] and 1 <= int(g[4]) <= 49:
                pr.codes.add(g[4] + g[5])
        for m in _CITED_DWG.finditer(blob):
            ref = _alnum(m.group(1))
            if len(ref) >= 3 and re.search(r"\d", ref):
                pr.dwg_refs.add(ref)
        pr.sample = "; ".join(truncate(items[u].full_description, 70) for u in wp.item_uids[:3])
        out.append(pr)
    return out


class Matcher:
    def __init__(self, rules: FlowchartRules, drules: DocRules, llm=None, cache: Optional[Cache] = None,
                 log: Callable[[str], None] = print):
        self.rules = rules
        self.dr = drules
        self.llm = llm
        self.cache = cache
        self.log = log
        self.llm_calls = 0

    # ------------------------------------------------------------------ helpers
    def _holders(self, prs: list[WPProfile], cat: str) -> list[WPProfile]:
        alias = self.rules.package_alias.get(cat, cat)
        return [p for p in prs if cat in p.cats or alias in p.cats]

    def _cats_in(self, text_norm: str) -> list[str]:
        """Every category a line names ('acrylic paint on cement plaster' -> paint AND plaster)."""
        hits: dict[str, list[str]] = {}
        for cid, c in self.rules.categories.items():
            h = keyword_hits(text_norm, c.keywords)
            if h and (not c.requires_any or keyword_hits(text_norm, c.requires_any)):
                hits[cid] = h
        # a more specific category wins over a general one whose words it contains:
        # 'glass handrail' -> Glass Handrails only, not also steel Handrails (matched on 'handrail')
        def covered(cid: str) -> bool:
            return all(any(w != x and w in x for o, xs in hits.items() if o != cid for x in xs) for w in hits[cid])

        return [c for c in hits if not covered(c)] or list(hits)

    def _line_targets(self, line: str, prs: list[WPProfile], by_division: bool = True) -> list[tuple[WPProfile, str]]:
        """Packages a legend / title line points to, with the category or division it named.
        Words in brackets are qualifiers (colour, texture, brand: 'METAL LOUVERS (W/ WOODEN LOOK COAT)'),
        so the text outside the brackets is matched first."""
        full = norm(line)
        primary = re.sub(r"\([^)]*\)?", " ", full)
        for text in (primary, full):
            cats = self._cats_in(text)
            if cats:     # the line names a trade: only its packages (none if this BOQ has no such package)
                return [(p, self.rules.categories[c].name_en) for c in cats for p in self._holders(prs, c)]
        if not by_division:
            return []
        for text in (primary, full):
            div = self.rules.division_hint(text)
            if div and self.rules.mode(div) == "as_is":
                return [(p, f"Div {div} {self.rules.division_name(div)}") for p in prs if p.division == div]
        return []

    # ------------------------------------------------------------------ specs
    def _spec(self, s: SpecSection, prs: list[WPProfile]) -> list[tuple[WPProfile, str, str, float]]:
        if not s.code:
            return []
        hits = [p for p in prs if s.code[:4] in p.codes or s.code in p.codes]
        if hits:
            return [(p, "code", f"the BOQ cites Section {s.code_display}", 1.0) for p in hits]
        cat = self.rules.keyword_hint(norm(s.title))
        if cat:
            hold = self._holders(prs, cat)
            name = self.rules.categories[cat].name_en
            if hold:
                return [(p, "title", f"section title “{s.title}” → {name}", 0.9) for p in hold]
            return []        # names a trade that has no package in this BOQ -> leftover (LLM / user)
        same = [p for p in prs if p.division == s.division]
        main = [p for p in same if p.wp.key.split("@")[0].startswith(("DIV", MISC))]
        same = main or same          # a general section goes to the division's own package, not to its ovals
        if same:
            conf = 0.85 if len(same) == 1 else 0.6
            return [(p, "division", f"Div {s.division} section ({self.rules.division_name(s.division)})", conf)
                    for p in same]
        return []

    # ------------------------------------------------------------------ drawings
    def _sheet(self, sh: DrawingSheet, prs: list[WPProfile]) -> list[tuple[WPProfile, str, str, float]]:
        out = []
        num = _alnum(sh.number)
        if num:
            for p in prs:
                if any(num == r or (len(r) >= 4 and num.endswith(r)) for r in p.dwg_refs):
                    out.append((p, "drawing ref", f"the BOQ cites drawing {sh.number}", 1.0))
        if sh.kind in GENERAL_KINDS:
            return out       # notes / symbols / abbreviations / cover: project-wide, only when the BOQ cites it
        # on plans / sections the text is mostly room names ("STERILIZING AND FILLING"): there, only a named
        # material counts; a whole division is inferred only from the title or from a legend / schedule sheet
        listing = sh.kind in ("legend", "schedule", "detail")
        for k, line in enumerate([sh.title, *sh.legend]):
            if not line:
                continue
            for p, what in self._line_targets(line, prs, by_division=listing or k == 0):
                out.append((p, "legend", f"“{truncate(line, 70)}” → {what}", 0.85))
        for r in self.dr.layout:
            if sh.discipline not in r.get("disciplines", []):
                continue
            if "*" not in r.get("kinds", []) and sh.kind not in r.get("kinds", []):
                continue
            targets = [p for p in prs if p.division in r.get("divisions", [])]
            for c in r.get("categories", []):
                targets += self._holders(prs, c)
            for p in targets:
                out.append((p, "sheet type", f"{self.dr.disciplines.get(sh.discipline, {}).get('name', sh.discipline)} "
                                             f"{sh.kind or 'sheet'}: {r.get('why', '')}", 0.6))
        return out

    # ------------------------------------------------------------------ LLM for leftovers
    def _ask(self, what: str, text: str, prs: list[WPProfile]) -> list[tuple[WPProfile, str, str, float]]:
        if self.llm is None or not prs:
            return []
        keys = [p.wp.key for p in prs]
        menu = "\n".join(f"- {p.wp.key}: {p.wp.name_en} (Div {p.division}) e.g. {p.sample}" for p in prs)
        k = key_of(PROMPT_VERSION, what, text, menu, self.llm.model)
        ans = self.cache.get("doc-match", k) if self.cache else None
        if ans is None:
            schema = {"type": "object", "properties": {
                "packages": {"type": "array", "items": {"type": "string", "enum": keys + ["NONE"]}, "maxItems": 4},
                "reason": {"type": "string"}}, "required": ["packages", "reason"]}
            system = ("You are a senior construction procurement engineer. You decide which work packages a "
                      "specification section or drawing sheet must be sent with, so each supplier receives the "
                      "documents they need to price. Answer with the package keys only; NONE if it serves none.")
            user = (f"{what}:\n{text}\n\nWork packages:\n{menu}\n\n"
                    "Which packages need this document? Pick only packages whose scope it describes or shows.")
            try:
                ans = self.llm.chat_json(system, user, schema, op="doc-match", max_tokens=300)
            except Exception as e:  # noqa: BLE001
                self.log(f"Document matching question failed: {e}")
                return []
            self.llm_calls += 1
            if self.cache:
                self.cache.put("doc-match", k, ans)
        by = {p.wp.key: p for p in prs}
        reason = truncate(ans.get("reason", ""), 160)
        return [(by[x], "llm", reason or "suggested by the model", 0.55) for x in ans.get("packages", []) if x in by]

    # ------------------------------------------------------------------ all
    def run(self, session: Session) -> ProjectDocs:
        docs = session.documents
        prs = profiles(session, self.rules)
        best: dict[tuple[str, str], DocLink] = {}

        def add(ref: str, kind: str, found):
            for p, method, reason, conf in found:
                k = (p.wp.key, ref)
                cur = best.get(k)
                if cur is None or conf > cur.confidence:
                    best[k] = DocLink(wp_key=p.wp.key, ref=ref, kind=kind, method=method, reason=reason,
                                      confidence=conf)

        fmap = docs.file_map()
        for s in docs.sections:
            if fmap.get(s.file_id) and fmap[s.file_id].kind != "spec":
                continue
            found = self._spec(s, prs)
            if not found and s.code:
                found = self._ask("Specification section", f"Section {s.code_display} {s.title}. {s.intro}", prs)
            add(s.uid, "spec", found)
        for sh in docs.sheets:
            if (fmap.get(sh.file_id) and fmap[sh.file_id].kind != "drawing") or sh.kind == "not a drawing":
                continue
            found = self._sheet(sh, prs)
            if not found and (sh.title or sh.legend) and sh.kind not in GENERAL_KINDS:
                body = f"{sh.number} {sh.title} ({sh.kind or 'sheet'}, {sh.discipline or 'trade unknown'})\n" + \
                       "\n".join(sh.legend[:15])
                found = self._ask("Drawing sheet", body, prs)
            add(sh.uid, "drawing", found)

        # user corrections
        valid = {p.wp.key for p in prs}
        refs = docs.ref_map()
        for wkey, ov in docs.overrides.items():
            if wkey not in valid:
                continue
            for r in ov.get("remove", []):
                best.pop((wkey, r), None)
            for r in ov.get("add", []):
                if r in refs:
                    kind = "spec" if isinstance(refs[r], SpecSection) else "drawing"
                    best[(wkey, r)] = DocLink(wp_key=wkey, ref=r, kind=kind, method="manual", reason="added by user")
        order = {p.wp.key: i for i, p in enumerate(prs)}
        pos = {r: i for i, r in enumerate(refs)}
        docs.links = sorted(best.values(), key=lambda l: (order.get(l.wp_key, 999), l.kind, pos.get(l.ref, 0)))
        by_method = defaultdict(int)
        for l in docs.links:
            by_method[l.method] += 1
        docs.stats["links_by_method"] = dict(by_method)
        linked = {l.ref for l in docs.links}
        docs.stats["unlinked"] = [r for r in refs if r not in linked and not (
            isinstance(refs[r], DrawingSheet) and refs[r].kind == "not a drawing")]
        return docs
