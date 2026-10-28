"""Upload intake: several BOQ files and/or .zip archives → the list of BOQ files to read.

Zips are unpacked safely (no paths outside the target folder, no symlinks, size / count limits).
Everything that is not an Excel or PDF file is ignored; PDFs are pre-screened for a BOQ table so that
drawings and specifications in a project zip are skipped quickly instead of being read / OCR'd in full.
"""
from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb"}
PDF_EXT = {".pdf"}
BOQ_EXT = EXCEL_EXT | PDF_EXT
MAX_FILES = 500
MAX_UNZIPPED = 2 * 1024 ** 3        # 2 GB


@dataclass
class Candidate:
    path: Path        # local file to read
    name: str         # display name ("Project.zip/Structural/BOQ.xlsx")


def _ignored(parts: tuple[str, ...]) -> bool:
    return any(p.startswith(("~$", ".", "__MACOSX")) for p in parts)


def _unzip(zpath: Path, display: str, dest: Path, log: Callable[[str], None]) -> list[Candidate]:
    out: list[Candidate] = []
    total = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = PurePosixPath(info.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or _ignored(name.parts):
                continue
            if stat.S_ISLNK(info.external_attr >> 16):          # symlink entries
                continue
            ext = name.suffix.lower()
            if ext == ".zip":
                log(f"Nested zip ignored: {display}/{name}")
                continue
            if ext not in BOQ_EXT:
                continue
            total += info.file_size
            if len(out) >= MAX_FILES or total > MAX_UNZIPPED:
                log(f"{display}: too many / too large files — the rest was not unpacked.")
                break
            target = (dest / Path(*name.parts)).resolve()
            if dest.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            out.append(Candidate(target, f"{display}/{name}"))
    return out


def collect(uploads: list[tuple[Path, str]], dest: Path, log: Callable[[str], None] = print) -> list[Candidate]:
    """uploads = [(local path, original name)]. Returns BOQ candidate files in a stable order."""
    out: list[Candidate] = []
    for i, (path, name) in enumerate(uploads, 1):
        ext = Path(name).suffix.lower()
        if ext == ".zip":
            out += _unzip(path, Path(name).name, dest / f"zip{i}", log)
        elif ext in BOQ_EXT and not _ignored((Path(name).name,)):
            out.append(Candidate(path, Path(name).name))
        else:
            log(f"Ignored (not a BOQ file type): {name}")
    return out


def pdf_has_boq_table(path: Path, settings, max_pages: int = 3) -> Optional[bool]:
    """Cheap pre-screen: is there a BOQ table header (Item / Description / Unit / Qty …) on the first pages?
    Scanned pages are OCR'd one at a time, only until a header is found."""
    import os

    import pymupdf as fitz

    from .pdf_reader import _lines, _phrases, _words_from_chars
    from .structure import detect_header

    try:
        pdf = fitz.open(path)
    except Exception:  # noqa: BLE001
        return False
    for pno in range(min(max_pages, len(pdf))):
        page = pdf[pno]
        words = _words_from_chars(page)
        if sum(len(w.t) for w in words) < 40:
            try:
                tp = page.get_textpage_ocr(language=settings.ocr_languages, dpi=200, full=True,
                                           tessdata=os.path.join(settings.tesseract_dir, "tessdata"))
                words = _words_from_chars(page, tp)
            except Exception:  # noqa: BLE001
                continue
        lines = _lines(words)
        grid = [(i, [(int((a + b) / 2), t) for a, b, t in _phrases(ln)]) for i, ln in enumerate(lines)]
        if detect_header(grid, max_scan=60) is not None:
            return True
        # Arabic text stored in visual order: try the reversed words too
        rev = [(i, [(x, t[::-1]) for x, t in cells]) for i, cells in grid]
        if detect_header(rev, max_scan=60) is not None:
            return True
    return False
