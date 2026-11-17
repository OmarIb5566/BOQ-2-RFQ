"""Per work package: one Specs PDF (whole pages of its sections) and one Drawings PDF (whole sheets).

Pages are copied at vector quality (insert_pdf), in upload order, and stamped in the top margin like the
BOQ cut-outs. Image sheets were converted to one-page PDFs when indexed.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from ..models import ProjectDocs, WorkPackage
from ..text_utils import safe_filename


def _pages_by_file(docs: ProjectDocs, wp: WorkPackage, kind: str) -> "OrderedDict[str, list[int]]":
    refs = docs.ref_map()
    out: "OrderedDict[str, list[int]]" = OrderedDict()
    order = {f.id: i for i, f in enumerate(docs.files)}
    links = sorted(docs.links_for(wp.key, kind), key=lambda l: (order.get(refs[l.ref].file_id, 0), l.ref))
    for l in links:
        r = refs.get(l.ref)
        if r is None:
            continue
        pages = r.pages if kind == "spec" else [r.page]
        lst = out.setdefault(r.file_id, [])
        lst += [p for p in pages if p not in lst]
    return out


def build_wp_docs(docs: ProjectDocs, wp: WorkPackage, out_dir: Path, stamp: Callable[[Path, WorkPackage], None],
                  log: Callable[[str], None] = print) -> dict[str, Path]:
    fmap = docs.file_map()
    made = {}
    for kind, suffix in (("spec", "Specs"), ("drawing", "Drawings")):
        parts = _pages_by_file(docs, wp, kind)
        if not parts:
            continue
        out = fitz.open()
        for fid, pages in parts.items():
            f = fmap.get(fid)
            if f is None or not Path(f.path).exists():
                continue
            src = fitz.open(f.path)
            for a, b in _runs(sorted(pages) if kind == "spec" else pages):
                if a < len(src):
                    out.insert_pdf(src, from_page=a, to_page=min(b, len(src) - 1))
            src.close()
        if len(out) == 0:
            continue
        path = Path(out_dir) / f"{safe_filename(f'{wp.wp_id} {wp.name_en}', 70)} - {suffix}.pdf"
        out.save(path, garbage=3, deflate=True)
        out.close()
        stamp(path, wp)
        made[kind] = path
    return made


def _runs(nums: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out
