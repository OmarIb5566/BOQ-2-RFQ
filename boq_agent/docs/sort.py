"""What is each uploaded file?  BOQ / specification / drawing — decided cheaply, without reading it in full.

Order of evidence (cheapest first):
1. Excel -> BOQ;  image -> drawing (the vision model confirms it later).
2. A PDF page larger than A3 -> drawing (no BOQ / spec is printed on A1).
3. A BOQ table header on the first pages -> BOQ  (the existing pre-screen).
4. The file name ('… Specs …', '… DWG …', 'مواصفات' …).
5. The text: 'SECTION 04 20 00' / 'PART 1 GENERAL' -> spec; a title block (Drawing No, Scale, Drawn …) -> drawing.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..ingest.intake import EXCEL_EXT
from ..text_utils import norm
from .common import IMG_EXT, DocRules

A3_LONG = 1250          # pt; A3 is 1191 pt on its long side
_SPEC_RE = re.compile(r"\b(section\s+\d{2}\s?\d{2}\s?\d{2}|section\s+\d{5}|part\s+1\b|end\s+of\s+section|"
                      r"section\s+includes|related\s+documents|القسم\s+\d)", re.I)
_TITLE_BLOCK = ["drawing no", "dwg no", "drawn", "checked", "approved", "scale", "rev", "sheet", "project no",
                "رقم اللوحه", "مقياس الرسم", "رسم", "مراجعه"]


def sort_file(path: Path, name: str, rules: DocRules, has_boq_table) -> tuple[str, str]:
    """-> (kind, why) with kind in boq | spec | drawing.  has_boq_table(path) -> bool is the BOQ pre-screen."""
    ext = Path(name).suffix.lower()
    if ext in EXCEL_EXT:
        return "boq", "spreadsheet"
    if ext in IMG_EXT:
        return "drawing", "image file"
    import pymupdf as fitz

    try:
        pdf = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        return "boq", f"could not open ({e})"          # let the BOQ reader report the problem
    if len(pdf) == 0:
        return "boq", "empty PDF"
    big = max(max(p.rect.width, p.rect.height) for p in pdf.pages(0, min(3, len(pdf))))
    if big > A3_LONG:
        return "drawing", f"sheet size {big / 72 * 25.4:.0f} mm (larger than A3)"
    hint = rules.name_hint(name)
    if hint == "boq" or has_boq_table(path):
        return "boq", "BOQ table found" if hint != "boq" else "file name"
    if hint in ("spec", "drawing"):
        return hint, "file name"
    return _by_text(pdf)


def _by_text(pdf) -> tuple[str, str]:
    spec_hits = tb_hits = chars = 0
    landscape = 0
    n = min(4, len(pdf))
    for p in pdf.pages(0, n):
        t = p.get_text()
        chars += len(t)
        spec_hits += len(_SPEC_RE.findall(t))
        tn = norm(t)
        tb_hits += sum(1 for w in _TITLE_BLOCK if w in tn)
        landscape += p.rect.width > p.rect.height
    if spec_hits >= 2:
        return "spec", "specification headings (SECTION / PART 1 …)"
    if tb_hits >= 4 or landscape > n / 2:
        return "drawing", "title block / landscape sheets"
    if chars / max(n, 1) > 1200:
        return "spec", "running text"
    return "drawing", "little text (scan?)"
