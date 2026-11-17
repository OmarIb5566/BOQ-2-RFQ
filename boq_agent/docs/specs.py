"""A specification PDF -> its sections, using the document's own numbering (no model on digital PDFs).

A page belongs to a section when
* a heading 'SECTION 04 20 00' (+ title on the same or next line) starts on it, or
* its running header names the section ('Section 31 23 00: Excavation & Filling'), or
* it follows a page of that section (continuation pages without a header).
Old 5-digit MasterFormat codes ('SECTION 04200 UNIT MASONRY') and Arabic 'القسم' are recognised.
A page listing many section numbers (a table of contents) starts nothing.
Scanned pages: Tesseract OCR of the page; if that is not available, the vision model reads the header band.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional

from ..models import SpecSection
from ..text_utils import nfkc

_CODE = r"(\d{2})\s?(\d{2})\s?(\d{2})(?!\d)|(\d{5})(?!\d)"
_HEAD_RE = re.compile(rf"^\s*(?:section|القسم|قسم)\s*[:\-–]?\s*(?:{_CODE})\s*[:\-–]?\s*(.*)$", re.I)
_ANY_RE = re.compile(rf"(?:section|القسم|قسم)\s*[:\-–]?\s*(?:{_CODE})\s*[:\-–]?\s*([^\n|]*)", re.I)
_INTRO_RE = re.compile(r"(section\s+includes?|summary|scope(?:\s+of\s+work)?)\s*[:.]?\s*(.{20,400}?)(?:\n\s*\n|\s[B-Z]\.\s|$)",
                       re.I | re.S)


def _code(m: re.Match, base: int = 1) -> str:
    g = m.groups()
    return g[base - 1] + g[base] + g[base + 1] if g[base - 1] else g[base + 2]


def _clean_title(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip(" :-–|")
    t = re.sub(r"\bpage\s*-?\s*\d+\s*(of\s*\d+)?.*$", "", t, flags=re.I).strip(" :-–|")
    return t[:120]


def _page_text(page, settings, vision, log) -> tuple[str, str]:
    t = page.get_text()
    if len(t.strip()) >= 40:
        return t, "text"
    try:      # scanned page: OCR (same Tesseract set-up as the BOQ reader)
        tp = page.get_textpage_ocr(language=settings.ocr_languages, dpi=200, full=True,
                                   tessdata=os.path.join(settings.tesseract_dir, "tessdata"))
        t = page.get_text(textpage=tp)
        if len(t.strip()) >= 40:
            return t, "ocr"
    except Exception:  # noqa: BLE001
        pass
    if vision is not None:
        h = vision.read_header(page)
        if h:
            return h, "vlm"
    return t, "text"


def index_spec(pdf, file_id: str, settings, vision=None, log: Callable[[str], None] = print) -> list[SpecSection]:
    pages = []
    for page in pdf:
        text, src = _page_text(page, settings, vision, log)
        text = nfkc(text)
        lines = [ln.strip() for ln in text.split("\n")]
        starts = []          # (code, title) of headings that start on this page
        for i, ln in enumerate(lines):
            m = _HEAD_RE.match(ln)
            if not m:
                continue
            title = _clean_title(m.group(5))
            if not title:    # title on the next non-empty line
                title = next((_clean_title(x) for x in lines[i + 1:i + 4] if x.strip()), "")
            starts.append((_code(m), title))
        distinct = {c for c, _ in starts}
        if len(distinct) > 3:            # table of contents / index page
            starts = []
        header = None                    # running header: first section reference near the top of the page
        top = "\n".join(lines[:12])
        mh = _ANY_RE.search(top)
        if mh:
            header = (_code(mh), _clean_title(mh.group(5)))
        pages.append((starts, header, text, src))

    sections: list[SpecSection] = []
    cur: Optional[SpecSection] = None
    for pno, (starts, header, text, src) in enumerate(pages):
        cur_code = cur.code if cur else None
        new = next((st for st in starts if st[0] != cur_code), None)     # a heading of another section
        if new is None and not starts and header and header[0] != cur_code:
            new = header                                               # running header of another section
        if new:
            code, title = new
            cur = next((s for s in sections if s.code == code and s.pages and s.pages[-1] == pno - 1), None)
            if cur is None:
                cur = SpecSection(uid=f"{file_id}:s{len(sections) + 1}", file_id=file_id, code=code,
                                  title=title, division=code[:2], source=src)
                sections.append(cur)
            if not cur.title and title:
                cur.title = title
        if cur is None:                  # front matter before the first section
            cur = SpecSection(uid=f"{file_id}:s{len(sections) + 1}", file_id=file_id, title="General / front matter",
                              source=src)
            sections.append(cur)
        cur.pages.append(pno)
        if not cur.intro:
            m = _INTRO_RE.search(text)
            if m:
                cur.intro = re.sub(r"\s+", " ", m.group(2)).strip()[:300]
    for s in sections:
        if s.code and (not s.title or s.title.isdigit()):
            s.title = mf_title(s.code) or s.title
    return sections


def mf_title(code: str) -> str:
    """MasterFormat title for a code, when the document itself does not print one."""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "knowledge" / "masterformat_titles.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))["titles"].get(code[:4], "")
    except Exception:  # noqa: BLE001
        return ""
