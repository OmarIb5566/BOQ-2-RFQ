"""Index the uploaded specs & drawings once per run (before the BOQ is classified, so all vision-model work
is grouped and the text model is loaded only afterwards)."""
from __future__ import annotations

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
            if not real:
                f.kind, f.note = "ignored", "no drawing sheet in it (vision model: not a drawing)"
                continue
            docs.sheets += sheets
            n_vlm = sum(s.source == "vlm" for s in sheets)
            log(f"{f.id} = {f.filename}: {len(real)} drawing sheet(s)" + (f", {n_vlm} read by the vision model" if n_vlm else ""))
    t_dwg = time.time() - t_dwg

    vstats = vision.client.log if vision else []
    docs.stats = {
        "index_seconds": round(time.time() - t0, 1), "spec_seconds": round(t_spec, 1), "drawing_seconds": round(t_dwg, 1),
        "vision_calls": len(vstats), "vision_seconds": round(sum(c["s"] for c in vstats), 1),
        "vision_tokens_in": sum(c["in"] for c in vstats), "vision_tokens_out": sum(c["out"] for c in vstats),
        "cache_hits": cache.hits - hits0,
        "sheets_text_layer": sum(s.source == "text" for s in docs.sheets),
        "sheets_vision": sum(s.source == "vlm" for s in docs.sheets),
    }
    return docs
