"""Index the uploaded specs & drawings once per run (before the BOQ is classified, so all vision-model work
is grouped and the text model is loaded only afterwards)."""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from ..models import DocFile, ProjectDocs, SpecSection
from ..text_utils import keyword_hits, safe_filename
from .common import IMG_EXT, Cache, file_sha, key_of, load_doc_rules
from .drawings import boilerplate, image_to_pdf, index_drawing_pages, raw_pages
from .specs import index_spec
from .vision import Vision

INDEX_VERSION = "i1"
# bump when the way drawing sheets are read changes: saved projects re-read their drawings at the next re-plan
DRAWING_RULES = "d2"


def _copy(path: Path, name: str, fdir: Path) -> Path:
    fdir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(Path(name).stem, 60)
    if Path(name).suffix.lower() in IMG_EXT:
        data = image_to_pdf(path)
        if data is None:
            raise ValueError("unreadable image")
        dst = fdir / f"{stem}.pdf"
        dst.write_bytes(data)
        return dst
    dst = fdir / f"{stem}.pdf"
    shutil.copy2(path, dst)
    return dst


def _number_from_name(sh, filename: str, drules, lines=()) -> None:
    """One sheet per file (the usual way a drawing set is issued): a file name that is a drawing number
    (E2405-TND-001-ADE-10300-0.pdf) is more reliable than a token read off the sheet (a level or room tag)."""
    from .drawings import _number_score

    stem = Path(filename).stem.strip()
    if " " in stem or _number_score(stem, drules) < 14 or stem.upper() == (sh.number or "").upper():
        return
    printed = any(stem.upper() in t.upper().replace(" ", "") for t, _, _ in lines)
    if not printed and _number_score(stem, drules) <= _number_score(sh.number, drules):
        return
    sh.number = stem.upper()
    if sh.kind not in ("general", "not a drawing") and not sh.kind:
        sh.kind = drules.sheet_kind(sh.title, sh.number)
    sh.discipline = drules.discipline(sh.title, sh.number) or sh.discipline


def _titles_from_drawing_list(sheets, raws, drules) -> None:
    """A drawing list (or any table) printing 'NUMBER  TITLE' on one row names every sheet of the set:
    it fills the title (and so the sheet type) of sheets whose own title could not be read."""
    numbers = {s.number.upper(): s for s in sheets if s.number}
    missing = {n for n, s in numbers.items() if not s.title or not s.kind}
    if not missing:
        return
    found: dict[str, str] = {}
    for pages in raws.values():
        for lines in pages:
            for t, _, bb in lines:
                n = t.strip().upper().replace(" ", "")
                if n not in missing or n in found:
                    continue
                # the page may be stored rotated: a vertical number means the row runs along y
                vert = (bb[3] - bb[1]) > 2 * (bb[2] - bb[0])
                a0, a1, c0, c1 = (1, 3, 0, 2) if vert else (0, 2, 1, 3)    # along-row / across-row axes
                mid, h = (bb[c0] + bb[c1]) / 2, max(bb[c1] - bb[c0], 1)
                row = sorted((b2[a0], t2) for t2, _, b2 in lines
                             if b2[a0] > bb[a1] and abs((b2[c0] + b2[c1]) / 2 - mid) < h * 0.6)
                if row and not re.fullmatch(r"[\d:./\- ]+|N\.?T\.?S\.?|--", row[0][1].strip(), re.I):
                    found[n] = row[0][1].strip()
    for n, title in found.items():
        s = numbers[n]
        s.title = s.title or title
        if not s.kind:
            s.kind = drules.sheet_kind(title, s.number)


def register_files(found: list[tuple[Path, str, str, str]], run_dir: Path) -> ProjectDocs:
    """found = [(local path, display name, kind 'spec'|'drawing', why)] -> files copied into the run folder.
    The same file uploaded twice (same content) is kept once."""
    docs = ProjectDocs()
    seen: dict[str, str] = {}
    for n, (path, name, kind, why) in enumerate(found, 1):
        fid = f"D{n}"
        sha = file_sha(path)
        f = DocFile(id=fid, filename=name, path="", kind=kind, sha=sha, note=why)
        if sha in seen:
            f.kind, f.note = "ignored", f"same file as {seen[sha]}"
            docs.files.append(f)
            continue
        seen[sha] = fid
        try:
            f.path = str(_copy(path, name, run_dir / fid))
            f.pages = len(fitz.open(f.path))
        except Exception as e:  # noqa: BLE001
            f.kind, f.note = "ignored", f"could not be read: {e}"
        docs.files.append(f)
    return docs


def build_index(docs: ProjectDocs, settings, frules, cache: Cache, log: Callable[[str], None] = print,
                progress: Callable[[float, str], None] = lambda p, m: None) -> ProjectDocs:
    """(Re)builds the sections / sheets of every file from its current kind. Cheap on a re-run: cached."""
    t0 = time.time()
    docs.sections, docs.sheets = [], []
    drules = load_doc_rules(str(settings.doc_rules_path))
    vision = Vision(settings, cache, log) if settings.use_llm else None
    hits0 = cache.hits

    # ---- specifications (each file on its own; cached by content)
    t_spec = time.time()
    specs = [f for f in docs.files if f.kind == "spec"]
    for k, f in enumerate(specs, 1):
        progress(0.12 + 0.03 * k / len(specs), f"Reading specification {k}/{len(specs)}: {Path(f.filename).name} …")
        ck = key_of(INDEX_VERSION, f.sha, settings.ocr_languages, settings.vision_model if vision else "")
        hit = cache.get("spec-index", ck)
        if hit is not None:
            secs = [SpecSection(**{**s, "uid": s["uid"].replace("{fid}", f.id), "file_id": f.id}) for s in hit]
        else:
            secs = index_spec(fitz.open(f.path), f.id, settings, vision, log)
            cache.put("spec-index", ck, [{**s.model_dump(), "uid": s.uid.replace(f.id + ":", "{fid}:")} for s in secs])
        docs.sections += secs
        coded = [s for s in secs if s.code]
        log(f"{f.id} = {f.filename}: specification, {len(coded)} section(s)"
            + (f" ({', '.join(s.code_display for s in coded[:6])}{' …' if len(coded) > 6 else ''})" if coded else ""))
    t_spec = time.time() - t_spec

    # ---- drawings (the whole set together: boilerplate = lines repeated on most sheets)
    t_dwg = time.time()
    dwgs = [f for f in docs.files if f.kind == "drawing"]
    if dwgs:
        progress(0.15, f"Reading {len(dwgs)} drawing file(s) …")
        pdfs = {f.id: fitz.open(f.path) for f in dwgs}
        raws = {fid: raw_pages(p) for fid, p in pdfs.items()}
        mat_kw = [k for c in frules.categories.values() for k in c.keywords]
        boiler = boilerplate([pg for r in raws.values() for pg in r], drules.boilerplate_share,
                             keep=lambda s: bool(keyword_hits(s, mat_kw)))
        for k, f in enumerate(dwgs, 1):
            progress(0.15 + 0.05 * k / len(dwgs), f"Reading drawings {k}/{len(dwgs)}: {Path(f.filename).name} …")
            sheets = index_drawing_pages(pdfs[f.id], f.id, raws[f.id], boiler, drules, vision, log)
            real = [s for s in sheets if s.kind != "not a drawing"]
            if len(real) == 1:
                _number_from_name(real[0], f.filename, drules, raws[f.id][real[0].page] if raws[f.id] else [])
            if not real:
                f.kind, f.note = "ignored", "no drawing sheet in it (vision model: not a drawing)"
                continue
            docs.sheets += sheets
            n_vlm = sum(s.source == "vlm" for s in sheets)
            log(f"{f.id} = {f.filename}: {len(real)} drawing sheet(s)" + (f", {n_vlm} read by the vision model" if n_vlm else ""))
        _titles_from_drawing_list(docs.sheets, raws, drules)
    t_dwg = time.time() - t_dwg

    vstats = vision.client.log if vision else []
    docs.stats = {
        "drawing_rules": DRAWING_RULES,
        "index_seconds": round(time.time() - t0, 1), "spec_seconds": round(t_spec, 1), "drawing_seconds": round(t_dwg, 1),
        "vision_calls": len(vstats), "vision_seconds": round(sum(c["s"] for c in vstats), 1),
        "vision_tokens_in": sum(c["in"] for c in vstats), "vision_tokens_out": sum(c["out"] for c in vstats),
        "cache_hits": cache.hits - hits0,
        "sheets_text_layer": sum(s.source == "text" for s in docs.sheets),
        "sheets_vision": sum(s.source == "vlm" for s in docs.sheets),
    }
    return docs
