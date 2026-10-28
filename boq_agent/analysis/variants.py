"""General rule: the same product in different sizes / dimensions / colours / patterns / codes is ONE work package.

Two neighbouring items of a section are variants when what remains of their text — after removing numbers,
dimensions, units, item codes, colour / pattern / finish words and 'includes …' clauses — is (almost) the same:
  * under the same parent description: their own lines are compared ("Walls 120 mm, code WT1" ~ "Walls 250 mm, code WT2");
  * without a parent: the full descriptions are compared (interlock PC.01 … PC.08 in different colours);
  * short lines under the same heading also count when they share most words ("250mm wall sign" ~ "450x200 ceiling sign").
Items under DIFFERENT parents are never variants (fountain excavation WF-01 ≠ fountain waterproofing WF-01).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional

from ..models import BOQDocument, Item
from ..text_utils import norm, strip_inclusions

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_DIM = re.compile(r"[x×*/\\]")
_UNITS = {"mm", "cm", "m", "m2", "m3", "مم", "مللي", "مللى", "ملم", "سم", "م", "م2", "م3", "مط", "ط", "inch", "in", "kg", "كجم",
          "thick", "thickness", "height", "width", "length", "depth", "diameter", "dia", "size", "overall",
          "سمك", "بسمك", "ارتفاع", "بارتفاع", "عرض", "بعرض", "طول", "بطول", "قطر", "بقطر", "مقاس", "بمقاس"}
_COLOURS = {"احمر", "رمادي", "رمادى", "ابيض", "اسود", "بيج", "اصفر", "اخضر", "ازرق", "بني", "بنى", "فاتح", "غامق", "غامقه", "فاتحه",
            "red", "grey", "gray", "white", "black", "beige", "yellow", "green", "blue", "brown", "light", "dark", "silver",
            "herringbone", "running", "bond", "stack", "basket", "weave", "custom", "random", "antique", "polished",
            "flamed", "honed", "matt", "matte", "glossy", "splitface", "smooth", "rough"}
_STOP = {"و", "من", "في", "على", "علي", "الى", "إلى", "مع", "او", "أو", "the", "of", "for", "and", "with", "to", "a", "in",
         "بالمتر", "المسطح", "المربع", "الطولي", "الطولى", "المكعب", "بالعدد", "توريد", "تركيب", "وتركيب", "supply", "install",
         "ditto", "ديتو", "لزوم"}


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, a: str) -> str:
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def skeleton(text: str, ignore: set[str], code_re: Optional[re.Pattern]) -> set[str]:
    t = strip_inclusions(norm(text))
    if code_re is not None:
        t = code_re.sub(" ", t)
    t = _NUM.sub(" ", t)
    t = _DIM.sub(" ", t)
    toks = re.findall(r"[^\W\d_]+", t)
    return {w for w in toks if len(w) > 1 and w not in _UNITS and w not in _COLOURS and w not in _STOP and w not in ignore}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _overlap(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def _unit_family(u: str) -> str:
    n = norm(u).replace(" ", "").replace(".", "")
    if any(k in n for k in ("3", "مكعب")):
        return "vol"
    if any(k in n for k in ("2", "مسطح", "مربع")):
        return "area"
    if n in ("m", "م", "مط", "م/ط", "lm", "ml") or "طول" in n:
        return "len"
    return n or "-"


def variant_groups(doc: BOQDocument, cfg: dict) -> dict[str, str]:
    """item uid -> leader uid (first item of its variant group), only for groups of 2+ items."""
    ignore = {norm(w) for w in cfg.get("ignore_words", [])}
    code_re = re.compile(cfg["code_family_re"], re.I) if cfg.get("code_family_re") else None
    thr = float(cfg.get("min_similarity", 0.75))
    items = doc.item_map()
    uf = _UF()
    for s in doc.sections:
        its: list[Item] = [items[u] for u in s.item_uids]
        prev: Optional[Item] = None
        for it in its:
            uf.find(it.uid)
            if prev is not None and _unit_family(prev.unit) == _unit_family(it.unit):
                # same parent = same description text (a parent repeated after a page break counts as one)
                pp = norm(prev.parent_text) if prev.parent_row_uids else None
                cp = norm(it.parent_text) if it.parent_row_uids else None
                same = False
                if pp and pp == cp:                                   # siblings under one description
                    a, b = skeleton(prev.description, ignore, code_re), skeleton(it.description, ignore, code_re)
                    same = (not a and not b) or a == b or _jaccard(a, b) >= thr
                elif not pp and not cp:
                    a, b = skeleton(prev.full_description, ignore, code_re), skeleton(it.full_description, ignore, code_re)
                    same = _jaccard(a, b) >= thr
                    if (not same and prev.headings and prev.headings == it.headings
                            and len(norm(prev.headings[-1])) >= 40      # the heading itself describes the product
                            and len(norm(prev.description)) < 60 and len(norm(it.description)) < 60):
                        same = _overlap(a, b) >= 0.5                  # short size lines under that heading
                if same:
                    uf.union(prev.uid, it.uid)
            prev = it
    members: dict[str, list[str]] = defaultdict(list)
    order = {it.uid: i for i, it in enumerate(doc.items)}
    for it in doc.items:
        members[uf.find(it.uid)].append(it.uid)
    out: dict[str, str] = {}
    for group in members.values():
        if len(group) > 1:
            leader = min(group, key=order.get)
            for u in group:
                out[u] = leader
    return out


def consolidate(doc: BOQDocument, groups: dict[str, str], log=print) -> int:
    """Give every item of a variant group the group's category. A manual choice or a context rule wins;
    otherwise the confidence-weighted majority. Returns the number of items changed."""
    items = doc.item_map()
    by_leader: dict[str, list[Item]] = defaultdict(list)
    for u, leader in groups.items():
        by_leader[leader].append(items[u])
    changed = 0
    for leader, its in by_leader.items():
        for it in its:
            it.variant_group = leader
        cats = {it.category for it in its}
        if len(cats) == 1:
            continue
        score: dict[Optional[str], float] = defaultdict(float)
        for it in its:
            w = 100.0 if it.method == "manual" else 50.0 if it.method == "context-rule" else (
                0.5 if it.method == "section" else max(it.confidence, 0.05))
            score[it.category] += w
        winner = max(score, key=lambda c: (score[c], c is not None))
        ref = items[leader]
        for it in its:
            if it.category != winner:
                it.category = winner
                it.method = it.method if it.method.endswith("+variant") else f"{it.method}+variant"
                it.reason = f"same product as item {ref.item_no or ref.uid} (size / colour / code variant)"
                changed += 1
        log(f"Variants of item {ref.item_no or ref.uid} ({len(its)} items) kept together → {winner or 'section default'}")
    return changed
