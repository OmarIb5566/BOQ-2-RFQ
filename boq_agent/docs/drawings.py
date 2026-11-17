"""Drawing files (PDF sheets or images) -> one DrawingSheet per sheet.

CAD-exported PDFs keep a vector text layer, so the sheet is read as data first:
* drawing number: the strongest 'XX-AR-…-1201'-style token (room tags such as UN2-G-14 score low),
  else the short number next to the 'Drawing No' label;
* title: a short, large-font line naming a sheet type (… PLAN / ELEVATIONS / SECTION / DETAIL / SCHEDULE);
* legend: the sheet's descriptive lines (material / finish legends, schedules, notes) after removing the
  boilerplate repeated on most sheets of the set (general notes, consultant address, copyright …).
Only a sheet WITHOUT a text layer (a scan, an image) goes to the vision model.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Optional

from ..models import DrawingSheet
from ..text_utils import keyword_hits, nfkc, norm
from .common import DocRules

_TOKEN = re.compile(r"(?<![\w])[A-Za-z0-9]{1,8}(?:[-_.][A-Za-z0-9]{1,8}){1,7}(?![\w])")
_NUM_ONLY = re.compile(r"^[A-Z]{0,3}[-.]?\d{2,5}[A-Z]?$")
MIN_TEXT = 150          # characters of text layer below which a sheet is treated as a scan
# sentences of general notes / disclaimers (not legend entries)
_PROSE = re.compile(r"\b(shall|must|should|is to|are to|to be|do not|prior to|unless|notified|responsible|reproduced|"
                    r"property of|confidential|permission|in conjunction|etc|i\.e|e-mail|p\. ?o\. box)\b|^\d{1,2}\.\s")


def _lines(page) -> list[tuple[str, float, tuple]]:
    """(text, font size, bbox) per text line of the page."""
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            t = nfkc("".join(s["text"] for s in ln["spans"])).strip()
            if t:
                out.append((re.sub(r"\s+", " ", t), max(s["size"] for s in ln["spans"]), tuple(ln["bbox"])))
    return out


def _number_score(tok: str, rules: DocRules) -> float:
    parts = [p for p in re.split(r"[-_.]", tok) if p]
    if len(parts) < 3 or not re.search(r"[A-Za-z]", tok) or not re.search(r"\d", tok):
        return 0
    up = {p.upper() for p in parts}
    disc = any(p in v["codes"] and len(p) > 1 for v in rules.disciplines.values() for p in up)
    return 2 * len(parts) + 4 * disc + 3 * ("DWG" in up) + 2 * bool(re.fullmatch(r"\d{3,4}", parts[-1])) + len(tok) / 6


def _find_number(lines, rules: DocRules) -> str:
    cnt = Counter(m.group(0).strip("-_.") for t, _, _ in lines for m in _TOKEN.finditer(t))
    best, best_s = "", 0.0
    for tok, n in cnt.items():
        s = _number_score(tok, rules) - (3 if n > 2 else 0)      # a drawing number is printed once or twice
        if s > best_s:
            best, best_s = tok, s
    if best_s >= 14:
        return best.lstrip("-").upper()
    # short number next to the 'Drawing No' label (any orientation: nearest centre)
    labels = [bb for t, _, bb in lines if keyword_hits(norm(t), rules.number_labels)]
    for lb in labels:
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        near = sorted((abs((bb[0] + bb[2]) / 2 - cx) + abs((bb[1] + bb[3]) / 2 - cy), t)
                      for t, _, bb in lines if _NUM_ONLY.match(t.upper()))
        if near and near[0][0] < 200:
            return near[0][1].upper()
    return best.upper() if best_s >= 10 else ""


def _find_title(lines, rules: DocRules, boiler: set[str]) -> str:
    kind_words = [w for v in rules.kinds.values() for w in v["keywords"]]
    cands = []
    for t, size, _ in lines:
        n = norm(t)
        if n in boiler or len(t) > 70 or len(t.split()) > 9 or not keyword_hits(n, kind_words):
            continue
        if re.search(r"\b(refer|see|are to|shall|unless|as per)\b", n):
            continue
        cands.append((size, len(t), t))
    return max(cands)[2] if cands else ""


def _legend(lines, boiler: set[str], limit: int = 80) -> list[str]:
    seen, out = set(), []
    for t, _, _ in lines:
        n = norm(t)
        if n in boiler or n in seen or len(t) < 8 or len(re.findall(r"[^\W\d_]{2,}", t)) < 3:
            continue
        if _PROSE.search(n) or _TOKEN.fullmatch(t.strip("-_. ")):
            continue
        seen.add(n)
        out.append(t[:200])
    return sorted(out, key=len, reverse=True)[:limit]


def raw_pages(pdf) -> list[list[tuple[str, float, tuple]]]:
    return [_lines(p) for p in pdf]


def boilerplate(all_pages: list[list[tuple]], share: float, keep: Callable[[str], bool] = lambda n: False) -> set[str]:
    """Lines printed on most sheets of the drawing set (general notes, address, copyright …).
    A repeated line that names a material (keep(norm_line) is True) is a shared legend, not boilerplate."""
    pages = [p for p in all_pages if p]
    if len(pages) < 3:
        return set()
    cnt = Counter(n for p in pages for n in {norm(t) for t, _, _ in p})
    return {n for n, c in cnt.items() if c >= max(3, share * len(pages)) and not (keep(n) and not _PROSE.search(n))}


def index_drawing_pages(pdf, file_id: str, raw: list[list[tuple]], boiler: set[str], rules: DocRules,
                        vision=None, log: Callable[[str], None] = print) -> list[DrawingSheet]:
    sheets = []
    for pno, lines in enumerate(raw):
        chars = sum(len(t) for t, _, _ in lines)
        sh = DrawingSheet(uid=f"{file_id}:p{pno + 1}", file_id=file_id, page=pno)
        if chars >= MIN_TEXT:
            sh.number = _find_number(lines, rules)
            sh.title = _find_title(lines, rules, boiler)
            sh.legend = _legend(lines, boiler)
        elif vision is not None:
            r = vision.read_sheet(pdf[pno])
            sh.source, sh.review = "vlm", True
            if r is None:
                sh.title = "(scanned sheet — not read)"
            elif not r["is_drawing"]:
                sh.kind = "not a drawing"
                sh.title = r["title"] or "(not a drawing sheet)"
            else:
                sh.number, sh.title, sh.legend = r["number"], r["title"], r["legend"]
                if r["discipline"]:
                    sh.discipline = rules.discipline(r["discipline"], "")
        else:
            sh.review, sh.title = True, "(scanned sheet — vision model off)"
        if sh.kind != "not a drawing":
            sh.kind = rules.sheet_kind(sh.title, sh.number)
            sh.discipline = sh.discipline or rules.discipline(sh.title, sh.number, " ".join(sh.legend[:20]))
        sheets.append(sh)
    return sheets


def image_to_pdf(path) -> Optional[bytes]:
    """An image file as a one-page PDF (so it can be read, cropped and attached like a PDF sheet)."""
    import pymupdf as fitz

    try:
        img = fitz.open(path)
        return img.convert_to_pdf()
    except Exception:  # noqa: BLE001
        return None
