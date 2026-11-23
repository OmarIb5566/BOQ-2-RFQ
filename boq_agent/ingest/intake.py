"""Upload intake: several BOQ files and/or .zip archives → the list of BOQ files to read.

Zips are unpacked safely (no paths outside the target folder, no symlinks, size / count limits).
Excel, PDF and image files are kept (images = scanned drawing sheets); everything else is ignored. The files are
then sorted into BOQs / specifications / drawings by docs/sort.py (PDFs are pre-screened for a BOQ table).
"""
from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .sniff import HEAD_BYTES, check_file, check_head

EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb"}
PDF_EXT = {".pdf"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
BOQ_EXT = EXCEL_EXT | PDF_EXT
UPLOAD_EXT = BOQ_EXT | IMG_EXT
MAX_FILES = 500
MAX_UNZIPPED = 2 * 1024 ** 3        # 2 GB


@dataclass
class Candidate:
    path: Path        # local file to read
    name: str         # display name ("Project.zip/Structural/BOQ.xlsx")


def _head(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(HEAD_BYTES)


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
            if ext not in UPLOAD_EXT:
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
            err = check_file(target, f"{display}/{name}")
            if err:
                log(f"Skipped — {err}")
                target.unlink(missing_ok=True)
                continue
            out.append(Candidate(target, f"{display}/{name}"))
    return out


def collect(uploads: list[tuple[Path, str]], dest: Path, log: Callable[[str], None] = print) -> list[Candidate]:
    """uploads = [(local path, original name)]. Returns BOQ candidate files in a stable order."""
    out: list[Candidate] = []
    for i, (path, name) in enumerate(uploads, 1):
        ext = Path(name).suffix.lower()
        if ext == ".zip":
            err = check_head(_head(path), Path(name).name)
            if err:
                log(f"Skipped — {err}")
                continue
            try:
                out += _unzip(path, Path(name).name, dest / f"zip{i}", log)
            except zipfile.BadZipFile as e:
                log(f"Skipped — {Path(name).name}: damaged zip ({e})")
        elif ext in UPLOAD_EXT and not _ignored((Path(name).name,)):
            err = check_file(path, Path(name).name)
            if err:
                log(f"Skipped — {err}")
                continue
            out.append(Candidate(path, Path(name).name))
        else:
            log(f"Ignored (not an Excel / PDF / image file): {name}")
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
