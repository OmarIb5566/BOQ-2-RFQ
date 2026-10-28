"""PDF BOQ reader -> BOQDocument.

Words are taken from the text layer (or Tesseract OCR for scanned pages), clustered into visual
lines, mapped to columns using the header labels and the table's vertical ruling lines, and fed to
the shared structure builder. Every row keeps its page + bbox so it can be cropped later.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

import pymupdf as fitz

from ..models import BOQDocument, Row, SourceRef, TableScope
from ..text_utils import has_text, is_arabic, nfkc, norm
from .structure import FIELDS, SECTION_RE, build_structure, detect_header

_COMMON_AR = {norm(w) for w in [
    "توريد", "تركيب", "اعمال", "الاعمال", "لزوم", "طبقا", "شامل", "جميع", "حسب", "المواصفات", "بالمتر",
    "المسطح", "الطولي", "المكعب", "عدد", "من", "في", "علي", "الي", "مع", "او", "الباب", "البند", "الكميه",
    "الوحده", "السعر", "بيان", "المهندس", "المشرف", "كامل", "نهوا", "يلزم", "ما", "كل"]}


class _W:
    __slots__ = ("x0", "y0", "x1", "y1", "t")

    def __init__(self, x0, y0, x1, y1, t):
        self.x0, self.y0, self.x1, self.y1, self.t = x0, y0, x1, y1, t

    @property
    def xc(self):
        return (self.x0 + self.x1) / 2

    @property
    def yc(self):
        return (self.y0 + self.y1) / 2


def _words_from_chars(page, textpage=None) -> list[_W]:
    """Build words from character boxes. PDF text runs often glue together glyphs that sit in
    different table columns (e.g. '2عددAR'), so we split on spaces AND on large horizontal gaps."""
    d = page.get_text("rawdict", textpage=textpage) if textpage else page.get_text("rawdict")
    out: list[_W] = []
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            for sp in ln.get("spans", []):
                thr = 0.45 * max(sp.get("size", 8), 4)
                cur: list[tuple] = []

                def flush():
                    if cur:
                        t = nfkc("".join(c[0] for c in cur)).strip()
                        if t:
                            out.append(_W(min(c[1] for c in cur), min(c[2] for c in cur),
                                          max(c[3] for c in cur), max(c[4] for c in cur), t))
                    cur.clear()

                for ch in sp.get("chars", []):
                    c, bb = ch["c"], ch["bbox"]
                    if c.isspace():
                        flush()
                        continue
                    if cur:
                        px0, px1 = min(x[1] for x in cur), max(x[3] for x in cur)
                        if max(bb[0] - px1, px0 - bb[2]) > thr:
                            flush()
                    cur.append((c, bb[0], bb[1], bb[2], bb[3]))
                flush()
    return out


def _page_words(page, settings, log) -> list[_W]:
    words = _words_from_chars(page)
    if sum(len(w.t) for w in words) < 40:
        tessdata = os.path.join(settings.tesseract_dir, "tessdata")
        try:
            tp = page.get_textpage_ocr(language=settings.ocr_languages, dpi=300, full=True, tessdata=tessdata)
            words = _words_from_chars(page, tp)
            log(f"Page {page.number + 1}: no text layer — OCR applied ({len(words)} words).")
        except Exception as e:  # noqa: BLE001
            log(f"Page {page.number + 1}: OCR failed ({e}).")
    return words


def _fix_reversed(pages: list[list[_W]]) -> bool:
    normal = reversed_ = 0
    for words in pages:
        for w in words:
            if is_arabic(w.t):
                n = norm(w.t)
                normal += n in _COMMON_AR
                reversed_ += n[::-1] in _COMMON_AR
    if reversed_ >= 5 and reversed_ > 1.5 * normal:
        for words in pages:
            for w in words:
                if is_arabic(w.t):
                    w.t = w.t[::-1]
        return True
    return False


def _lines(words: list[_W]) -> list[list[_W]]:
    if not words:
        return []
    words = sorted(words, key=lambda w: (w.yc, w.x0))
    lines, cur, cur_y, cur_h = [], [words[0]], words[0].yc, words[0].y1 - words[0].y0
    for w in words[1:]:
        tol = max(2.0, 0.45 * max(cur_h, w.y1 - w.y0))
        if abs(w.yc - cur_y) <= tol:
            cur.append(w)
            cur_y = sum(x.yc for x in cur) / len(cur)
        else:
            lines.append(cur)
            cur, cur_y, cur_h = [w], w.yc, w.y1 - w.y0
    lines.append(cur)
    return lines


def _phrases(line: list[_W]) -> list[tuple[float, float, str]]:
    ws = sorted(line, key=lambda w: w.x0)
    out, grp = [], [ws[0]]
    for w in ws[1:]:
        h = max(w.y1 - w.y0, 4)
        if w.x0 - grp[-1].x1 < 0.6 * h:
            grp.append(w)
        else:
            out.append(grp)
            grp = [w]
    out.append(grp)
    res = []
    for g in out:
        res.append((g[0].x0, g[-1].x1, _join(g)))
    return res


def _join(ws: list[_W]) -> str:
    text = " ".join(w.t for w in ws)
    if is_arabic(text):
        return " ".join(w.t for w in sorted(ws, key=lambda w: -w.x1))
    return " ".join(w.t for w in sorted(ws, key=lambda w: w.x0))


def _vertical_rules(page) -> list[float]:
    xs: list[tuple[float, float]] = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return []
    for d in drawings:
        for it in d.get("items", []):
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) < 1.0 and abs(p1.y - p2.y) > 8:
                    xs.append((p1.x, abs(p1.y - p2.y)))
            elif it[0] == "re":
                r = it[1]
                if r.width < 2.0 and r.height > 8:
                    xs.append(((r.x0 + r.x1) / 2, r.height))
                elif r.height > 8:
                    xs.append((r.x0, r.height))
                    xs.append((r.x1, r.height))
    if not xs:
        return []
    xs.sort()
    merged: list[list[float]] = []
    for x, h in xs:
        if merged and abs(x - merged[-1][0]) < 2.5:
            merged[-1][1] += h
        else:
            merged.append([x, h])
    return [x for x, cover in merged if cover > 60]


def _intervals(field_x: dict[str, float], rules: list[float], width: float) -> list[tuple[float, float, str]]:
    """Column intervals -> field name ('' = unmapped/description-ish)."""
    if len(rules) >= 3:
        bounds = sorted(set([0.0] + rules + [width]))
        out = []
        for a, b in zip(bounds, bounds[1:]):
            name = next((f for f, x in field_x.items() if a <= x < b), "")
            out.append((a, b, name))
        return out
    cs = sorted(field_x.items(), key=lambda kv: kv[1])
    out = []
    for i, (f, x) in enumerate(cs):
        a = (cs[i - 1][1] + x) / 2 if i else 0.0
        b = (x + cs[i + 1][1]) / 2 if i + 1 < len(cs) else width
        out.append((a, b, f))
    return out


def read_pdf(path: Path, doc_id: str, source_name: str, settings, log: Callable[[str], None] = print,
             file_id: str = "") -> BOQDocument:
    pre = f"{file_id}." if file_id else ""
    pdf = fitz.open(path)
    doc = BOQDocument(doc_id=doc_id, filename=source_name, source_path=str(path), work_path=str(path), kind="pdf")
    pages = [_page_words(p, settings, log) for p in pdf]
    if _fix_reversed(pages):
        log("Arabic text in this PDF is stored in visual order — reversed to logical order.")

    scope = TableScope(name="pdf", kind="pdf", file=file_id)
    rows: list[Row] = []
    intervals: Optional[list[tuple[float, float, str]]] = None
    initial_title = ""
    found_any_header = False

    for pno, words in enumerate(pages):
        page = pdf[pno]
        lines = _lines(words)
        if not lines:
            continue
        grid = [(i, [((a + b) / 2, t) for a, b, t in _phrases(ln)]) for i, ln in enumerate(lines)]
        header = detect_header([(i, [(int(x), t) for x, t in cells]) for i, cells in grid], max_scan=40)
        start = 0
        if header is not None:
            found_any_header = True
            field_x = {f: float(c) for f, c in header.field_columns.items()}
            if "description" not in field_x:
                header = None
            else:
                intervals = _intervals(field_x, _vertical_rules(page), page.rect.width)
                h_lines = lines[header.first_row:header.last_row + 1]
                hb = [min(w.x0 for ln in h_lines for w in ln), min(w.y0 for ln in h_lines for w in ln),
                      max(w.x1 for ln in h_lines for w in ln), max(w.y1 for ln in h_lines for w in ln)]
                scope.page_headers[pno] = hb
                if not scope.header_labels:
                    scope.header_labels = dict(header.labels)
                    scope.field_columns = {f: i for i, f in enumerate(FIELDS) if f in field_x}
                hu = f"{pre}P{pno}H"
                doc.rows.append(Row(uid=hu, ref=SourceRef(file=file_id, page=pno, bbox=hb), text=" ".join(
                    t for ln in h_lines for t in [_join(ln)]), role="header"))
                scope.header_row_uids.append(hu)
                for ln in h_lines:
                    for _, _, t in _phrases(ln):
                        if SECTION_RE.search(norm(t)) and not initial_title:
                            initial_title = t
                start = header.last_row + 1
        if intervals is None:
            continue

        rules = _vertical_rules(page)
        data_words = [w for ln in lines[start:] for w in ln]
        if data_words:
            x0 = min(rules) if len(rules) >= 2 else min(w.x0 for w in data_words)
            x1 = max(rules) if len(rules) >= 2 else max(w.x1 for w in data_words)
            if pno in scope.page_headers:
                x0, x1 = min(x0, scope.page_headers[pno][0]), max(x1, scope.page_headers[pno][2])
            scope.page_extents[pno] = [max(0, x0 - 3), min(page.rect.width, x1 + 3)]

        prev: Optional[Row] = None
        prev_y1 = None
        for li, ln in enumerate(lines[start:], start=start):
            buckets: dict[str, list[_W]] = {}
            for w in ln:
                name = next((f for a, b, f in intervals if a <= w.xc < b), "") or "description"
                buckets.setdefault(name, []).append(w)
            fields = {f: _join(ws) for f, ws in buckets.items() if f != "description"}
            desc = _join(buckets.get("description", []))
            # text in numeric columns that is really text (e.g. a title spilling over) -> description
            for f in ("quantity", "rate", "total"):
                if f in fields and has_text(fields[f]) and len(fields[f]) > 12:
                    desc = (desc + " " + fields.pop(f)).strip()
            bbox = [min(w.x0 for w in ln), min(w.y0 for w in ln), max(w.x1 for w in ln), max(w.y1 for w in ln)]
            h = bbox[3] - bbox[1]
            desc_only = not any(v.strip() for v in fields.values())
            if (prev is not None and desc_only and not any(v.strip() for v in prev.fields.values())
                    and prev.text and prev_y1 is not None and bbox[1] - prev_y1 < 0.6 * max(h, 6)
                    and not SECTION_RE.search(norm(desc)) and not SECTION_RE.search(norm(prev.text))):
                prev.text = (prev.text + " " + desc).strip()
                pb = prev.ref.bbox
                prev.ref.bbox = [min(pb[0], bbox[0]), min(pb[1], bbox[1]), max(pb[2], bbox[2]), max(pb[3], bbox[3])]
                prev.cells = [prev.text]
                prev_y1 = bbox[3]
                continue
            row = Row(uid=f"{pre}P{pno}L{li}", ref=SourceRef(file=file_id, page=pno, bbox=bbox),
                      cells=[fields.get(f, "") if f != "description" else desc for f in FIELDS],
                      fields=fields, text=desc)
            rows.append(row)
            prev, prev_y1 = row, bbox[3]

    if not found_any_header:
        doc.warnings.append("No BOQ table header (Item / Description / Unit / Qty …) was found in the PDF.")
        return doc
    sections, items = build_structure(rows, f"{pre}P", initial_title or Path(source_name).stem, None)
    for sec in sections:
        sec.file_id = file_id
    doc.rows.extend(rows)
    doc.sections, doc.items = sections, items
    doc.scopes.append(scope)
    log(f"PDF: {len(pdf)} page(s), {len(rows)} text rows, {len(sections)} section(s), {len(items)} item(s).")
    if not items:
        doc.warnings.append("No priced BOQ items were detected in the PDF.")
    return doc
