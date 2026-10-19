"""Loads the machine-readable flowchart and renders it as agent instructions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..text_utils import keyword_hits, norm, strip_inclusions

MISC = "MISC"
NONE = "NONE"   # LLM answer meaning "not a special case – stays with its section"


@dataclass
class Category:
    id: str
    division: str
    name_en: str
    name_ar: str
    definition: str
    keywords: list[str]
    requires_any: list[str]
    special: bool


class FlowchartRules:
    def __init__(self, path: Path):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.raw = data
        self.divisions: dict[str, dict] = data["divisions"]
        self.default_mode: str = data.get("default_mode", "as_is")
        self.categories: dict[str, Category] = {}
        for cid, c in data["categories"].items():
            self.categories[cid] = Category(
                id=cid,
                division=c["division"],
                name_en=c["name_en"],
                name_ar=c.get("name_ar", ""),
                definition=c.get("definition", ""),
                keywords=[norm(k) for k in c.get("keywords", [])],
                requires_any=[norm(k) for k in c.get("requires_any", [])],
                special=bool(c.get("special")),
            )
        self.special_rules: list[dict] = data.get("special_rules", [])
        self.extra_wps: dict[str, dict] = data.get("extra_work_packages", {})
        self.context_rules: list[dict] = []
        for r in data.get("context_rules", []):
            r = dict(r)
            for k in ("non_stone_context", "stone_words"):
                r[k] = [norm(w) for w in r.get(k, [])]
            r["context_all_groups"] = [[norm(w) for w in g] for g in r.get("context_all_groups", [])]
            self.context_rules.append(r)
        self._div_kw = {code: [norm(k) for k in d.get("keywords", [])] for code, d in self.divisions.items()}

    # ------------------------------------------------------------------ lookups
    def mode(self, division: Optional[str]) -> str:
        if division and division in self.divisions:
            return self.divisions[division].get("mode", self.default_mode)
        return self.default_mode

    def division_name(self, code: Optional[str], lang: str = "en") -> str:
        d = self.divisions.get(code or "")
        if not d:
            return "Unclassified" if lang == "en" else "غير مصنف"
        return d["name_en"] if lang == "en" else d.get("name_ar", d["name_en"])

    @property
    def special_ids(self) -> list[str]:
        return [c.id for c in self.categories.values() if c.special]

    @property
    def split_ids(self) -> list[str]:
        """All normal (rectangle) categories of every split division."""
        return [c.id for c in self.categories.values() if not c.special and self.mode(c.division) == "split"]

    def rule_for_category(self, cat: Optional[str]) -> Optional[dict]:
        return next((r for r in self.special_rules if r.get("category") == cat), None)

    def rule_for_division(self, div: str) -> Optional[dict]:
        return next((r for r in self.special_rules if r.get("division") == div), None)

    def wp_names(self, key: str) -> tuple[str, str, str]:
        """(name_en, name_ar, division) for a planner key."""
        if key.startswith("DIV"):
            code = key[3:]
            return (f"Div {code}: {self.division_name(code)}", self.division_name(code, "ar"), code)
        if key.startswith(MISC):
            code = key[len(MISC):]
            return (f"Div {code}: {self.division_name(code)} — other items",
                    f"{self.division_name(code, 'ar')} — بنود أخرى", code)
        if key in self.categories:
            c = self.categories[key]
            return c.name_en, c.name_ar, c.division
        if key in self.extra_wps:
            e = self.extra_wps[key]
            return e["name_en"], e.get("name_ar", ""), e.get("division", "")
        return key, "", ""

    # ------------------------------------------------------------- heuristics
    def keyword_hint(self, text: str, candidates: Optional[list[str]] = None) -> Optional[str]:
        """Cheap keyword pre-screen. Returns the best matching category id or None.
        Matches only the primary scope, so 'includes … movement joints' does not flag expansion joints."""
        t = strip_inclusions(norm(text))
        best, best_score = None, 0
        for cid in candidates or list(self.categories):
            c = self.categories[cid]
            hits = keyword_hits(t, c.keywords)
            if not hits:
                continue
            if c.requires_any and not keyword_hits(t, c.requires_any):
                continue
            score = sum(len(h) for h in hits) + (5 if c.requires_any else 0)
            if score > best_score:
                best, best_score = cid, score
        return best

    def context_override(self, headings: list[str], parent_text: str, item_text: str,
                         category: Optional[str]) -> Optional[tuple[str, str]]:
        """(category, rule id) forced by the group an item sits under, or None.
        Context = headings + a short, title-like parent (never the item's own text)."""
        for r in self.context_rules:
            target = r["set_category"]
            if category == target or (category in self.special_ids and category not in r.get("stone_categories", [])):
                continue   # already there, or another flowchart oval keeps it
            ctx = [norm(h) for h in headings]
            if parent_text and len(norm(parent_text)) <= r.get("max_parent_len", 90):
                ctx.append(norm(parent_text))
            hit = next((c for c in ctx if all(keyword_hits(c, g) for g in r["context_all_groups"])), None)
            if hit is None:
                continue
            if keyword_hits(hit, r["non_stone_context"]):
                # e.g. 'external cladding — aluminium panels': only real stone items count as external stone
                stone = category in r.get("stone_categories", []) or keyword_hits(norm(item_text), r["stone_words"])
                if not stone:
                    continue
            return target, r["id"]
        return None

    def division_hint(self, text: str) -> Optional[str]:
        t = norm(text)
        best, best_score = None, 0
        for code, kws in self._div_kw.items():
            score = sum(len(h) for h in keyword_hits(t, kws))
            if score > best_score:
                best, best_score = code, score
        return best

    # ------------------------------------------------------------ prompt text
    def render_prompt(self) -> str:
        """The flowchart as natural-language instructions for the agent's system prompt."""
        as_is = [f"Div {k} {v['name_en']}" for k, v in self.divisions.items() if v.get("mode") == "as_is"]
        lines = [
            "COMPANY WORK-PACKAGE FLOWCHART (follow strictly):",
            "A) Packages kept AS WRITTEN in the BOQ (one work package per division): " + "; ".join(as_is) + ".",
        ]
        for code, d in self.divisions.items():
            if d.get("mode") == "split":
                cats = [c for c in self.categories.values() if c.division == code and not c.special]
                lines.append(f"B) Div {code} {d['name_en']} ({d.get('name_ar', '')}) is SPLIT into separate work packages: "
                             + "; ".join(f"{c.name_en} ({c.name_ar})" for c in cats) + ".")
            elif d.get("mode") == "per_item":
                lines.append(f"C) Div {code} {d['name_en']} ({d.get('name_ar', '')}): EACH BOQ item is its own work package.")
        lines.append("SPECIAL CASES (ovals in the flowchart – exceptions where part of one package is grouped with "
                     "or separated from another; the user must approve them):")
        for r in self.special_rules:
            lines.append(f"- {r['title_en']}: {r.get('flowchart', '')}")
        lines.append("Items of Div 9 marble/granite/stone are 'Internal' ONLY when used inside the building; "
                     "anything on facades/exterior is External Marble.")
        for r in self.context_rules:
            lines.append("- " + r.get("note", ""))
        return "\n".join(lines)

    def category_menu(self, ids: list[str]) -> str:
        out = []
        for cid in ids:
            if cid == MISC:
                out.append(f"- {MISC}: belongs to this division but matches none of the listed packages")
            elif cid == NONE:
                out.append(f"- {NONE}: normal item of this section (not one of the special cases above)")
            else:
                c = self.categories[cid]
                out.append(f"- {cid}: {c.name_en} / {c.name_ar} — {c.definition}")
        return "\n".join(out)
