"""Format-independent BOQ structure detection.

Readers (Excel / PDF) turn the source into `Row`s carrying `fields` (item_no, unit, quantity,
rate, total) and `text` (description).  This module then:
  * finds the table header,
  * decides each row's role (section title / heading / parent description / leaf item / subtotal),
  * groups rows into `Section`s (أبواب) and priced `Item`s with their parent + heading context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..models import Item, Row, Section
from ..text_utils import has_text, nfkc, norm, parse_number

# --------------------------------------------------------------------------- header
HEADER_KEYWORDS: dict[str, list[str]] = {
    "item_no": ["رقم البند", "رقم", "البند", "بند", "م", "مسلسل", "item", "item no", "item no.", "no", "no.", "ref", "ref.", "s.n", "sn", "code", "كود"],
    "description": ["بيان الاعمال", "البيان", "بيان", "الوصف", "وصف", "وصف البند", "وصف الاعمال", "description", "description of work", "particulars"],
    "unit": ["الوحده", "وحده", "الوحدات", "unit", "uom", "u.o.m"],
    "quantity": ["الكميه", "كميه", "الكميات", "qty", "qty.", "quantity", "quantities"],
    "rate": ["سعر الوحده", "الفئه", "فئه", "السعر", "سعر", "rate", "unit price", "unit rate", "price"],
    "total": ["السعر الاجمالي", "الاجمالي", "اجمالي", "الجمله", "جمله", "القيمه", "المبلغ", "total", "amount", "total price", "total amount"],
}
HEADER_KEYWORDS = {f: [norm(k) for k in kws] for f, kws in HEADER_KEYWORDS.items()}
CURRENCY_TOKENS = [norm(t) for t in ["egp", "l.e", "le", "جنيه", "بالجنيه", "قرش", "رقما", "كتابه", "usd", "sar", "aed", "دولار", "ريال"]]
FIELDS = ["item_no", "description", "unit", "quantity", "rate", "total"]


def match_header_cell(text: str) -> Optional[str]:
    n = norm(text)
    if not n or len(n) > 45:
        return None
    best, best_len = None, 0
    for f, kws in HEADER_KEYWORDS.items():
        for kw in kws:
            if n == kw:
                if len(kw) + 100 > best_len:
                    best, best_len = f, len(kw) + 100
            elif len(kw) > 2 and kw in n and len(kw) > best_len:
                best, best_len = f, len(kw)
    return best


def is_currency_cell(text: str) -> bool:
    n = norm(text)
    return bool(n) and len(n) < 30 and any(re.search(rf"(^|\W){re.escape(t)}($|\W)", n) for t in CURRENCY_TOKENS)


@dataclass
class HeaderInfo:
    best_row: int
    first_row: int
    last_row: int
    field_columns: dict[str, int] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


def detect_header(grid: list[tuple[int, list[tuple[int, str]]]], max_scan: int = 60) -> Optional[HeaderInfo]:
    """grid: [(row_number, [(column, text), ...]), ...] in order. Returns header info or None."""
    scan = grid[:max_scan]
    scored = []
    for idx, (rnum, cells) in enumerate(scan):
        fields: dict[str, int] = {}
        labels: dict[str, str] = {}
        for col, text in cells:
            f = match_header_cell(text)
            if f and f not in fields:
                fields[f] = col
                labels[f] = text
        score = len(fields)
        if score >= 3 or (score >= 2 and "description" in fields and ("quantity" in fields or "unit" in fields)):
            scored.append((score, -idx, idx, fields, labels))
    if not scored:
        return None
    scored.sort(reverse=True)
    _, _, best_idx, fields, labels = scored[0]
    best_row = scan[best_idx][0]

    def qualifies(i: int) -> bool:
        if i < 0 or i >= len(scan):
            return False
        cells = scan[i][1]
        if any(parse_number(t) not in (None, 0) for _, t in cells if not has_text(t)):
            return False
        return any(match_header_cell(t) or is_currency_cell(t) for _, t in cells)

    first_idx = best_idx
    for i in range(best_idx - 1, best_idx - 4, -1):
        if qualifies(i):
            first_idx = i
    last_idx = best_idx
    for i in range(best_idx + 1, best_idx + 3):
        if qualifies(i):
            last_idx = i
        elif scan[i:i + 1] and scan[i][1]:
            break
    for i in range(first_idx, last_idx + 1):
        for col, text in scan[i][1]:
            f = match_header_cell(text)
            if f and f not in fields and col not in fields.values():
                fields[f] = col
                labels[f] = text
    return HeaderInfo(best_row=best_row, first_row=scan[first_idx][0], last_row=scan[last_idx][0],
                      field_columns=fields, labels=labels)


# ------------------------------------------------------------------------ patterns
SECTION_RE = re.compile(
    r"^\W*(ال)?باب\s+\S+|^\W*(division|div\.?)\s*[:\-]?\s*\d+|^\W*bill\s*(no\.?|number|nr\.?)?\s*[:\-]?\s*\d+",
    re.I)
NOISE_RE = re.compile(
    r"(اجمالي|الاجمالي|مجموع|المجموع|ملخص|المرحل|مرحل|ما قبله|صفحه\s*(رقم)?\s*\d|صفحه\s*\d+\s*من|"
    r"\bpage\b|\btotal\b|sub-?total|carried|brought|summary|issue date|rev\.?\s*no|تاريخ الاصدار)", re.I)
PAGE_NOISE_RE = re.compile(
    r"(صفحه|\bpage\b|ما قبله|مرحل|carried forward|brought forward|issue date|rev\.?\s*no|تاريخ الاصدار)", re.I)
L1_RE = re.compile(
    r"^\W*(section|sec\.?)\s*[:\-]?\s*\d|^\W*\d{2}\s?\d{2}\s?\d{2}\b|"
    r"^\W*(قسم|اولا|ثانيا|ثالثا|رابعا|خامسا|سادسا|سابعا|ثامنا|تاسعا|عاشرا)\b", re.I)
LETTER_SUB_RE = re.compile(r"^\(?([ء-ي]|[a-zA-Z])\)?[\-\.)]?$")
LUMP_TOKENS = {"rate only", "بالسعر فقط", "lump sum", "ls", "l.s", "مقطوعيه", "مقطوعية", "مقطوعي", "item", "lot"}


def norm_item_no(raw: str) -> str:
    s = nfkc(raw).upper()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^([A-Z]+)\.", r"\1", s)
    if not re.search(r"\d", s):
        return ""
    if parse_number(s) == 0:
        return ""
    return s


def parent_base(key: str) -> str:
    return re.sub(r"(\.0+)+$", "", key)


def is_child(key: str, base: Optional[str]) -> bool:
    if not key or not base:
        return False
    return key != base and any(key.startswith(base + sep) for sep in (".", "-", "/", "_"))


def _clean_unit(u: str) -> str:
    u = (u or "").strip()
    return u if (u and has_text(u) and len(u) <= 20 and not re.search(r"\d{3,}", u)) else ""


def _is_leaf(row: Row) -> bool:
    unit = _clean_unit(row.fields.get("unit", ""))
    qraw = row.fields.get("quantity", "")
    qty = parse_number(qraw)
    if unit and (has_text(row.text) or row.fields.get("item_no")):
        return True
    if norm(qraw) in LUMP_TOKENS:
        return True
    return qty not in (None, 0) and (has_text(row.text) or bool(norm_item_no(row.fields.get("item_no", ""))))


# ------------------------------------------------------------------------ builder
@dataclass
class _Parent:
    base: Optional[str]
    rows: list[str]
    texts: list[str]
    has_children: bool = False
    raw_no: str = ""

    @property
    def text(self) -> str:
        return " ".join(t for t in self.texts if t)


def build_structure(rows: list[Row], scope_prefix: str, default_title: str, sheet: Optional[str],
                    start_index: int = 0) -> tuple[list[Section], list[Item]]:
    """Assign roles to `rows` (in reading order) and build sections + items."""
    leaf_like = [r for r in rows if _is_leaf(r)]
    numbered = sum(1 for r in leaf_like if norm_item_no(r.fields.get("item_no", "")))
    numbering_used = bool(leaf_like) and numbered / len(leaf_like) >= 0.5

    sections: list[Section] = []
    items: list[Item] = []
    counter = {"s": start_index, "i": 0}

    def new_section(title: str, title_rows: list[str]) -> Section:
        counter["s"] += 1
        return Section(uid=f"{scope_prefix}S{counter['s']}", title=title.strip() or default_title,
                       sheet=sheet, title_row_uids=title_rows)

    cur = new_section(default_title, [])
    parent: Optional[_Parent] = None
    headings: list[tuple[int, str, str]] = []   # (level, uid, text)
    last_kind = None
    last_leaf: Optional[Item] = None

    def close_section():
        if cur.item_uids:
            sections.append(cur)

    for row in rows:
        n = norm(row.text)
        raw_no = (row.fields.get("item_no") or "").strip()
        key = norm_item_no(raw_no)
        leaf = _is_leaf(row)
        has_desc = has_text(row.text)

        if not leaf and not has_desc and not key:
            row.role = "blank"
            continue

        if not leaf and SECTION_RE.search(n) and len(n) < 160 and not NOISE_RE.search(n.split(":")[0]):
            parent, headings, last_kind = None, [], "section"
            row.role = "section"
            if not cur.item_uids and not cur.title_row_uids:
                cur.title, cur.title_row_uids = row.text.strip(), [row.uid]
            else:
                close_section()
                cur = new_section(row.text, [row.uid])
            continue

        if not leaf and not key and NOISE_RE.search(n) and len(n) < 120:
            row.role = "subtotal"
            if not PAGE_NOISE_RE.search(n):
                cur.subtotal_row_uids.append(row.uid)
            continue

        if leaf:
            letter_sub = bool(LETTER_SUB_RE.match(raw_no)) if raw_no else False
            ctx_rows: list[str] = []
            ptext = ""
            if parent and (not key or parent.base is None or is_child(key, parent.base) or letter_sub):
                ctx_rows, ptext = list(parent.rows), parent.text
                parent.has_children = True
            else:
                parent = None
                if not key and last_leaf is not None and last_kind == "leaf" and len(n) < 35:
                    ptext = last_leaf.parent_text or last_leaf.description   # context for the LLM only
            counter["i"] += 1
            item = Item(
                uid=f"{scope_prefix}I{counter['i']}",
                section_uid=cur.uid,
                item_no=raw_no or (parent.raw_no if parent and ctx_rows else ""),
                description=row.text.strip(),
                parent_text=ptext,
                headings=[h[2] for h in headings],
                unit=row.fields.get("unit", "").strip(),
                quantity=row.fields.get("quantity", "").strip(),
                rate=row.fields.get("rate", "").strip(),
                total=row.fields.get("total", "").strip(),
                row_uids=[row.uid],
                context_row_uids=[h[1] for h in headings] + ctx_rows,
                parent_row_uids=ctx_rows,
            )
            row.role = "leaf"
            items.append(item)
            cur.item_uids.append(item.uid)
            last_leaf, last_kind = item, "leaf"
            continue

        # ---- descriptive (non-priced) row
        if key and not LETTER_SUB_RE.match(raw_no):
            parent = _Parent(base=parent_base(key), rows=[row.uid], texts=[row.text], raw_no=raw_no)
            row.role, last_kind = "parent", "parent"
            continue
        is_l1 = bool(L1_RE.search(n)) and len(n) < 100
        if (parent is not None and not parent.has_children and last_kind in ("parent", "continuation")
                and not is_l1 and (parent.base is not None or not numbering_used)):
            parent.rows.append(row.uid)
            parent.texts.append(row.text)
            row.role, last_kind = "continuation", "continuation"
            continue
        if not is_l1 and (len(n) > 100 or not numbering_used):
            parent = _Parent(base=None, rows=[row.uid], texts=[row.text])
            row.role, last_kind = "parent", "parent"
            continue
        level = 1 if L1_RE.search(n) else 2
        while headings and headings[-1][0] >= level:
            headings.pop()
        headings.append((level, row.uid, row.text.strip()))
        parent = None
        row.role, last_kind = "heading", "heading"

    close_section()
    _attach_orphans(rows, items)
    return sections, items


def _attach_orphans(rows: list[Row], items: list[Item]) -> None:
    """Descriptive rows that ended up in no item (e.g. the wrapped tail of a description printed
    below its quantity line) are attached to the preceding item so snapshots never lose text."""
    used = set()
    for it in items:
        used.update(it.row_uids)
        used.update(it.context_row_uids)
    by_first = {it.row_uids[0]: it for it in items}
    last: Optional[Item] = None
    for row in rows:
        if row.uid in by_first:
            last = by_first[row.uid]
            continue
        if row.role in ("section", "subtotal"):
            last = None
            continue
        if row.role in ("parent", "continuation") and row.uid not in used and last is not None:
            last.row_uids.append(row.uid)
            used.add(row.uid)
