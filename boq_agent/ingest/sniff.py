"""Upload checks by content, not by name: does the file really look like what its extension says?

    check_head(head, name)  -> "" when fine, else a reason       (cheap: the first bytes only — used in the web request)
    check_file(path, name)  -> "" when fine, else a reason       (also opens PDFs / images — used in the background job)

The limits can be changed with environment variables (BOQ_MAX_PDF_PAGES, BOQ_MAX_IMAGE_MP).
"""
from __future__ import annotations

import os
from pathlib import Path

HEAD_BYTES = 2048

MAX_PDF_PAGES = int(os.getenv("BOQ_MAX_PDF_PAGES", "3000"))
MAX_IMAGE_MP = float(os.getenv("BOQ_MAX_IMAGE_MP", "200"))     # megapixels; A0 scanned at 300 dpi ≈ 140 MP


def kind_of(head: bytes) -> str:
    """The real format of a file from its first bytes ('' = unknown)."""
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"                                   # .xlsx / .xlsm / .xlsb / .docx / .zip
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"                                   # old .xls / .doc
    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head.startswith(b"BM"):
        return "bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    t = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if t.startswith((b"<", b"\xff\xfe<", b"\xfe\xff\x00<")):
        return "markup"                                # HTML / XML "spreadsheets" saved as .xls by other systems
    return ""


EXPECTED: dict[str, set[str]] = {
    ".xlsx": {"zip"}, ".xlsm": {"zip"}, ".xlsb": {"zip"},
    ".xls": {"ole", "zip", "markup"},                  # many systems export HTML/XML or real .xlsx with a .xls name
    ".pdf": {"pdf"},
    ".png": {"png"}, ".jpg": {"jpeg"}, ".jpeg": {"jpeg"}, ".tif": {"tiff"}, ".tiff": {"tiff"},
    ".bmp": {"bmp"}, ".webp": {"webp"},
    ".zip": {"zip"}, ".docx": {"zip"},
}


def check_head(head: bytes, name: str) -> str:
    ext = Path(name).suffix.lower()
    want = EXPECTED.get(ext)
    if want is None:
        return f"{name}: file type {ext or '(none)'} is not accepted"
    got = kind_of(head)
    if got not in want:
        return f"{name}: the content is not a real {ext} file" + (f" (it looks like {got})" if got else "")
    return ""


def check_file(path: Path, name: str) -> str:
    """Content check plus size limits that need the file to be opened (PDF pages, image pixels)."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
    except OSError as e:
        return f"{name}: cannot be read ({e})"
    err = check_head(head, name)
    if err:
        return err
    ext = path.suffix.lower() if path.suffix else Path(name).suffix.lower()
    if ext == ".pdf":
        import pymupdf as fitz
        try:
            with fitz.open(path) as doc:
                n = doc.page_count
        except Exception as e:  # noqa: BLE001
            return f"{name}: damaged PDF ({e})"
        if n > MAX_PDF_PAGES:
            return f"{name}: {n} pages — more than the limit of {MAX_PDF_PAGES}"
    elif ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"):
        try:
            w, h = image_size(path)
        except Exception as e:  # noqa: BLE001
            return f"{name}: damaged image ({e})"
        if w * h > MAX_IMAGE_MP * 1e6:
            return f"{name}: {w}×{h} pixels — more than the limit of {MAX_IMAGE_MP:g} megapixels"
    return ""


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) in pixels read from the file header only — nothing is decoded, so an image "bomb"
    (a small file that expands to gigabytes of pixels) is caught before any library tries to render it."""
    import struct

    with open(path, "rb") as f:
        head = f.read(64)
        kind = kind_of(head)
        if kind == "png":
            if head[12:16] != b"IHDR":
                raise ValueError("no PNG header")
            w, h = struct.unpack(">II", head[16:24])
        elif kind == "bmp":
            w, h = struct.unpack("<ii", head[18:26])
            w, h = abs(w), abs(h)
        elif kind == "webp":
            chunk = head[12:16]
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(head[24:27], "little")
                h = 1 + int.from_bytes(head[27:30], "little")
            elif chunk == b"VP8L":
                b = int.from_bytes(head[21:25], "little")
                w, h = 1 + (b & 0x3FFF), 1 + ((b >> 14) & 0x3FFF)
            elif chunk == b"VP8 ":
                w = int.from_bytes(head[26:28], "little") & 0x3FFF
                h = int.from_bytes(head[28:30], "little") & 0x3FFF
            else:
                raise ValueError("unknown WebP layout")
        elif kind == "jpeg":
            f.seek(2)
            while True:
                m = f.read(2)
                if len(m) < 2 or m[0] != 0xFF:
                    raise ValueError("no JPEG frame header")
                marker = m[1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    continue
                seg = struct.unpack(">H", f.read(2))[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">xHH", f.read(5))
                    break
                f.seek(seg - 2, 1)
        elif kind == "tiff":
            end = "<" if head[:2] == b"II" else ">"
            f.seek(struct.unpack(end + "I", head[4:8])[0])
            n = struct.unpack(end + "H", f.read(2))[0]
            w = h = 0
            for _ in range(n):
                tag, typ, _cnt, val = struct.unpack(end + "HHI4s", f.read(12))
                v = struct.unpack(end + ("H" if typ == 3 else "I"), val[:2] if typ == 3 else val)[0]
                if tag == 256:
                    w = v
                elif tag == 257:
                    h = v
        else:
            raise ValueError("unknown image format")
    if w <= 0 or h <= 0:
        raise ValueError("no size")
    return int(w), int(h)
