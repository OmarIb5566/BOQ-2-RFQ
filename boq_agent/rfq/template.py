"""The company RFQ Word template: field discovery and verbatim filling.

Every "(…)" or "[…]" in the template is a field. Filling replaces only the placeholder characters —
all other text, numbering, styles and page setup stay exactly as in the template. Word often splits a
placeholder over several runs ("(" + "Package" + " Name" + ")"); the value is written into the run where
the placeholder starts, so it keeps that run's formatting (bold title, 14 pt, …).
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

FIELD_RE = re.compile(r"\(([^()\[\]\n]{1,80})\)|\[([^()\[\]\n]{1,80})\]")
_PART_RE = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


def field_name(raw: str) -> str:
    return re.sub(r"\s+", " ", raw).strip()


def _paragraph_texts(root) -> list[list]:
    """Per paragraph: its <w:t> elements in reading order (runs of nested text-box paragraphs excluded)."""
    out = []
    for p in root.iter(f"{W}p"):
        ts = [t for t in p.iter(f"{W}t") if next(t.iterancestors(f"{W}p")) is p]
        if ts:
            out.append(ts)
    return out


def _parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def discover_fields(template: Path) -> list[str]:
    """Field names in order of first appearance."""
    seen: dict[str, None] = {}
    for name, data in _parts(Path(template)).items():
        if not _PART_RE.match(name):
            continue
        for ts in _paragraph_texts(etree.fromstring(data)):
            for m in FIELD_RE.finditer("".join(t.text or "" for t in ts)):
                seen.setdefault(field_name(m.group(1) or m.group(2)), None)
    return list(seen)


def preview(template: Path, values: dict[str, str]) -> list[str]:
    """Body paragraphs as plain text with the values substituted (for the on-screen review)."""
    vals = {k.lower(): v for k, v in values.items() if v and str(v).strip()}
    root = etree.fromstring(_parts(Path(template))["word/document.xml"])
    out = []
    for p in root.iter(f"{W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{W}t"))
        out.append(FIELD_RE.sub(lambda m: vals.get(field_name(m.group(1) or m.group(2)).lower(), m.group(0)), text))
    return out


def _fill_paragraph(ts: list, values: dict[str, str]) -> int:
    text = "".join(t.text or "" for t in ts)
    spans = []           # (start, end, value)
    for m in FIELD_RE.finditer(text):
        v = values.get(field_name(m.group(1) or m.group(2)).lower())
        if v:
            spans.append((m.start(), m.end(), v))
    if not spans:
        return 0
    bounds, pos = [], 0  # char range of each <w:t>
    for t in ts:
        n = len(t.text or "")
        bounds.append((pos, pos + n))
        pos += n
    for start, end, value in reversed(spans):   # right-to-left keeps earlier offsets valid
        first = next(i for i, (a, b) in enumerate(bounds) if a <= start < b)
        last = next(i for i, (a, b) in enumerate(bounds) if a < end <= b)
        for i in range(first, last + 1):
            a, b = bounds[i]
            s = ts[i].text or ""
            keep_before = s[: max(0, start - a)] if i == first else ""
            keep_after = s[end - a:] if i == last else ""
            ts[i].text = keep_before + (value if i == first else "") + keep_after
            ts[i].set(XML_SPACE, "preserve")
        # later <w:t>s of this paragraph are untouched; recompute bounds for the next (earlier) span
        pos, bounds = 0, []
        for t in ts:
            n = len(t.text or "")
            bounds.append((pos, pos + n))
            pos += n
    return len(spans)


def fill_template(template: Path, out: Path, values: dict[str, str]) -> int:
    """Write a copy of the template with fields replaced. Blank values leave the placeholder as-is.
    Returns the number of placeholders filled."""
    vals = {k.lower(): v for k, v in values.items() if v and str(v).strip()}
    parts = _parts(Path(template))
    filled = 0
    for name, data in list(parts.items()):
        if not _PART_RE.match(name):
            continue
        root = etree.fromstring(data)
        n = sum(_fill_paragraph(ts, vals) for ts in _paragraph_texts(root))
        if n:
            parts[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            filled += n
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(Path(template)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():          # same entry order / names as the template
            z.writestr(info, parts[info.filename])
    return filled
