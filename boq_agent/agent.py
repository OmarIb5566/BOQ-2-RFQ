"""The BOQ work-packaging agent (orchestrator).

    analyze()  : sort uploads (BOQ / spec / drawing) -> ingest BOQs -> index specs & drawings (vision model only
                 for scans) -> [LLM] divisions -> [LLM] item categories (+ reflection) -> plan -> match docs
    decide()   : user approves / denies a special-case proposal -> re-plan (+ re-match docs)
    export()   : per work package: BOQ cut-out + Specs PDF + Drawings PDF, RFQ hand-off JSON, zip
Specs and drawings are optional: BOQ only, BOQ + specs, BOQ + drawings or all three.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .analysis.classifier import Classifier
from .config import Settings
from .excel_com import prepare_workbook
from .ingest.excel_reader import header_from_llm, read_excel
from .ingest.pdf_reader import read_pdf
from .knowledge.rules import FlowchartRules
from .llm.client import OllamaClient
from .llm.prompts import system_prompt
from .models import Session
from .planning.planner import Planner
from .render.snapshots import render_all
from .text_utils import safe_filename

from .docs.common import IMG_EXT, Cache, load_doc_rules
from .ingest.intake import EXCEL_EXT, PDF_EXT


class BOQAgent:
    def __init__(self, settings: Optional[Settings] = None, log: Optional[Callable[[str], None]] = None,
                 progress: Optional[Callable[[float, str], None]] = None):
        self.settings = settings or Settings()
        self.settings.ensure_dirs()
        self._log_fn = log or print
        self.progress = progress or (lambda p, m: None)
        self.rules = FlowchartRules(self.settings.rules_path)
        self.llm = OllamaClient(self.settings.ollama_url, self.settings.model, self.settings.temperature,
                                self.settings.num_ctx, self.settings.request_timeout)
        self.planner = Planner(self.rules)
        self._session_log: list[str] = []

    def log(self, msg: str) -> None:
        self._session_log.append(f"{dt.datetime.now():%H:%M:%S} {msg}")
        self._log_fn(msg)

    # ------------------------------------------------------------------ analyze
    def analyze(self, files, original_names=None, run_id: Optional[str] = None) -> Session:
        """files: one path or a list of paths (Excel / PDF BOQs and/or .zip archives of them).
        All BOQs found are packaged together as ONE project."""
        from .ingest.intake import collect, pdf_has_boq_table
        from .models import BOQDocument, SourceFile

        paths = [Path(f) for f in (files if isinstance(files, (list, tuple)) else [files])]
        names = list(original_names) if isinstance(original_names, (list, tuple)) else (
            [original_names] if original_names else [])
        names = [n or p.name for n, p in zip(names + [None] * len(paths), paths)]
        for n in names:
            if Path(n).suffix.lower() not in EXCEL_EXT | PDF_EXT | IMG_EXT | {".zip"}:
                raise ValueError(f"Unsupported file type '{Path(n).suffix}'. Upload Excel / PDF BOQs (plus optional "
                                 f"PDF / image specifications and drawings) or a .zip of them.")
        self._session_log = []
        if len(names) == 1:
            project = Path(names[0]).stem
        else:
            project = f"{Path(names[0]).stem} +{len(names) - 1}"
        run_id = run_id or f"{dt.datetime.now():%Y%m%d-%H%M%S}-{safe_filename(project, 40).replace(' ', '_')}"
        run_dir = self.settings.workspace / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        llm_ok, msg = (self.llm.status() if self.settings.use_llm else (False, "LLM disabled"))
        self.log(f"LLM: {msg}")
        if not llm_ok:
            self.log("Running in KEYWORD-ONLY mode (no LLM). Results will be less accurate.")
        use_llm = self.settings.use_llm and llm_ok

        self.progress(0.02, "Unpacking …")
        cands = collect(list(zip(paths, names)), run_dir / "uploads", self.log)
        if not cands:
            raise RuntimeError("No Excel or PDF files were found in the upload.")
        multi = len(cands) > 1
        from .docs.sort import _by_text, sort_file

        drules = load_doc_rules(str(self.settings.doc_rules_path))
        kinds: dict[int, tuple[str, str]] = {}
        for k, c in enumerate(cands):
            if not multi:
                kinds[k] = ("drawing", "image file") if c.path.suffix.lower() in IMG_EXT else ("boq", "only file")
            else:
                self.progress(0.02 + 0.02 * k / len(cands), f"Sorting {k + 1}/{len(cands)}: {Path(c.name).name} …")
                kinds[k] = sort_file(c.path, c.name, drules, lambda p: pdf_has_boq_table(p, self.settings))
        doc_found: list[tuple[Path, str, str, str]] = []      # specs & drawings
        n_boq = sum(1 for v in kinds.values() if v[0] == "boq")
        hdr = header_from_llm(self.llm, system_prompt(self.rules)) if use_llm else None
        doc = BOQDocument(doc_id=run_id, filename=project if multi else cands[0].name, source_path="", work_path="",
                          kind="excel")
        skipped: list[str] = []
        n = 0
        for k, c in enumerate(cands, 1):
            kind, why = kinds[k - 1]
            if kind != "boq":
                doc_found.append((c.path, c.name, kind, why))
                continue
            self.progress(0.04 + 0.08 * k / len(cands), f"Reading BOQ {k}/{len(cands)}: {Path(c.name).name} …")
            ext = c.path.suffix.lower()
            fid = f"F{n + 1}"
            fdir = run_dir / fid
            fdir.mkdir(parents=True, exist_ok=True)
            try:
                if ext in EXCEL_EXT:
                    src = prepare_workbook(c.path, fdir, self.log,
                                           stem=f"{fid}-{safe_filename(Path(c.name).stem, 60)}")
                    part = read_excel(src, run_id, c.name, llm_header=hdr, log=self.log, file_id=fid)
                    kind = "excel"
                else:
                    src = fdir / f"{fid}-{safe_filename(Path(c.name).stem, 60)}.pdf"
                    shutil.copy2(c.path, src)
                    part = read_pdf(src, run_id, c.name, self.settings, self.log, file_id=fid)
                    kind = "pdf"
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{c.name} (could not be read: {e})")
                shutil.rmtree(fdir, ignore_errors=True)
                continue
            if not part.items:
                shutil.rmtree(fdir, ignore_errors=True)
                if ext in PDF_EXT and multi:          # a PDF without BOQ items: a spec or a drawing after all
                    import pymupdf as fitz
                    doc_found.append((c.path, c.name, *_by_text(fitz.open(c.path))))
                else:
                    skipped.append(f"{c.name} (no BOQ items found)")
                continue
            n += 1
            f = SourceFile(id=fid, filename=c.name, source_path=str(c.path), work_path=str(src), kind=kind)
            doc.files.append(f)
            doc.rows += part.rows
            doc.sections += part.sections
            doc.items += part.items
            doc.scopes += part.scopes
            doc.warnings += [f"{f.label}: {w}" if multi else w for w in part.warnings]
            self.log(f"{fid} = {c.name}: {len(part.sections)} section(s), {len(part.items)} item(s).")
        for sk in skipped:
            doc.warnings.append("Skipped: " + sk)
        for w in doc.warnings:
            self.log("⚠ " + w)
        if not doc.items:
            hint = " Only specifications / drawings were found — upload the BOQ with them." if doc_found and not n_boq else ""
            raise RuntimeError("No BOQ items could be detected." + hint + " " + " ".join(doc.warnings))
        first = doc.files[0]
        doc.source_path, doc.work_path, doc.kind = first.source_path, first.work_path, first.kind
        if len(doc.files) == 1:
            doc.filename = first.filename

        settings = self.settings
        if not use_llm:
            settings = Settings(**{**self.settings.__dict__, "use_llm": False})

        # specs & drawings: indexed now, so every vision-model call happens before the text model's work
        from .docs.index import build_index, register_files
        pdocs = register_files(doc_found, run_dir)
        if pdocs.files:
            build_index(pdocs, settings, self.rules, self._cache(), self.log, self.progress)
            st = pdocs.stats
            self.log(f"Specs & drawings: {len(pdocs.sections)} section(s), {len(pdocs.sheets)} sheet(s) in "
                     f"{st['index_seconds']}s ({st['sheets_text_layer']} sheet(s) read from the text layer, "
                     f"{st['vision_calls']} vision call(s), {st['cache_hits']} cache hit(s)).")
        clf = Classifier(self.rules, settings, self.llm, self.log, self.progress)
        self.progress(0.15, "Identifying BOQ divisions (أبواب) …")
        clf.assign_divisions(doc)
        self.progress(0.25, "Classifying items against the flowchart …")
        clf.classify_items(doc)

        session = Session(run_id=run_id, created=dt.datetime.now().isoformat(timespec="seconds"), document=doc,
                          documents=pdocs)
        session.plan = self.planner.build(doc)
        session.status = "planned"
        self._apply_names(session)
        self.match_documents(session, use_llm, verbose=True)
        self.log(f"Plan: {len(session.plan.work_packages)} work package(s), "
                 f"{len(session.plan.proposals)} special case(s) awaiting approval.")
        if use_llm:
            self.log(f"LLM calls: {self.llm.calls}, {self.llm.seconds:.0f}s total.")
        session.log = list(self._session_log)
        self.save(session)
        self.progress(1.0, "Analysis complete")
        return session

    # -------------------------------------------------------------- user edits
    def decide(self, session: Session, proposal_id: str, decision: str) -> Session:
        return self.decide_many(session, [proposal_id], decision)

    def decide_many(self, session: Session, proposal_ids: list[str], decision: str) -> Session:
        """Several decisions, one re-plan (e.g. 'approve all' on a project with dozens of cross-BOQ cases)."""
        ids = set(proposal_ids)
        for p in session.plan.proposals:
            if p.proposal_id in ids:
                p.decision = decision
        return self.replan(session)

    def set_section_division(self, session: Session, section_uid: str, division: Optional[str]) -> Session:
        sec = session.document.section_map()[section_uid]
        sec.division, sec.division_confidence, sec.division_method = division, 1.0, "manual"
        llm_ok = self.settings.use_llm and self.llm.status()[0]
        settings = self.settings if llm_ok else Settings(**{**self.settings.__dict__, "use_llm": False})
        Classifier(self.rules, settings, self.llm, self.log, self.progress).classify_items(session.document, [section_uid])
        return self.replan(session)

    def set_item_category(self, session: Session, item_uid: str, category: Optional[str]) -> Session:
        """A correction applies to the item and to all its size / colour / code variants."""
        items = session.document.item_map()
        it = items[item_uid]
        group = [x for x in items.values() if it.variant_group and x.variant_group == it.variant_group] or [it]
        for x in group:
            x.category = category if category not in ("", "NONE") else None
            x.method, x.confidence = "manual", 1.0
            x.reason = "set by user" if x is it else f"set by user on variant {it.item_no or it.uid}"
        return self.replan(session)

    def replan(self, session: Session) -> Session:
        session.plan = self.planner.build(session.document, previous=session.plan)
        session.status = "planned"
        self._apply_names(session)
        self.match_documents(session)
        self.save(session)
        return session

    def _apply_names(self, session: Session) -> None:
        for wp in session.plan.work_packages:
            if session.wp_names.get(wp.key):
                wp.name_en = session.wp_names[wp.key]

    def rename_wp(self, session: Session, wp_key: str, name: str) -> Session:
        """A user's own name for a work package (used in its files and RFQ letters; kept across re-plans).
        An empty name restores the flowchart name."""
        name = " ".join((name or "").split())[:120]
        if name:
            session.wp_names[wp_key] = name
        else:
            session.wp_names.pop(wp_key, None)
        return self.replan(session)

    # ---------------------------------------------------------- specs & drawings
    def _cache(self) -> Cache:
        return Cache(self.settings.workspace / "_cache", force=self.settings.force)

    def match_documents(self, session: Session, use_llm: Optional[bool] = None, verbose: bool = False) -> None:
        """Attach spec sections & drawing sheets to the current work packages (codes / keywords first,
        the text model only for leftovers, cached)."""
        docs = session.documents
        if not (docs.sections or docs.sheets):
            return
        import time

        from .docs.match import Matcher

        if use_llm is None:
            use_llm = self.settings.use_llm and self.llm.status()[0]
        t = time.time()
        n0 = len(self.llm.log)
        Matcher(self.rules, load_doc_rules(str(self.settings.doc_rules_path)), self.llm if use_llm else None,
                self._cache(), self.log).run(session)
        calls = self.llm.log[n0:]
        docs.stats.update({"match_seconds": round(time.time() - t, 1), "match_llm_calls": len(calls),
                           "match_llm_seconds": round(sum(c["s"] for c in calls), 1)})
        n_wp = len({l.wp_key for l in docs.links})
        (self.log if verbose else self._session_log.append)(f"Documents matched: {len(docs.links)} link(s) to {n_wp} package(s) "
                 f"{docs.stats.get('links_by_method', {})}; {len(docs.stats.get('unlinked', []))} not linked; "
                 f"{len(calls)} model call(s).")

    def set_doc_kind(self, session: Session, file_id: str, kind: str) -> Session:
        """The user re-sorts an uploaded file (spec / drawing / ignored)."""
        from .docs.index import build_index

        docs = session.documents
        for f in docs.files:
            if f.id == file_id and f.path:
                f.kind, f.note = kind, "set by user"
        llm_ok = self.settings.use_llm and self.llm.status()[0]
        settings = self.settings if llm_ok else Settings(**{**self.settings.__dict__, "use_llm": False})
        build_index(docs, settings, self.rules, self._cache(), self.log, self.progress)
        return self.replan(session)

    def set_doc_links(self, session: Session, wp_key: str, kind: str, refs: list[str]) -> Session:
        """The user's choice of spec sections / sheets for a package (kept across re-plans)."""
        docs = session.documents
        now = {l.ref for l in docs.links_for(wp_key, kind)}
        want = set(refs)
        ov = docs.overrides.setdefault(wp_key, {"add": [], "remove": []})
        for r in want - now:
            ov["remove"] = [x for x in ov["remove"] if x != r]
            ov["add"].append(r)
        for r in now - want:
            ov["add"] = [x for x in ov["add"] if x != r]
            ov["remove"].append(r)
        self.match_documents(session)
        session.status = "planned"          # the exported PDFs no longer match: export again
        self.save(session)
        return session

    def item_category_options(self, session: Session, section_uid: str) -> list[str]:
        sec = session.document.section_map()[section_uid]
        opts = self.rules.allowed_categories(sec.division)
        return (["NONE"] + [o for o in opts if o != "NONE"]) if self.rules.mode(sec.division) != "split" else opts

    # ------------------------------------------------------------------ export
    def export(self, session: Session, approve_pending: bool = False) -> Session:
        pending = [p for p in session.plan.proposals if p.decision == "pending"]
        if pending and not approve_pending:
            raise RuntimeError(f"{len(pending)} special case(s) still need your approval or denial.")
        for p in pending:
            p.decision = "approved"
        self.replan(session)
        out_dir = self.settings.workspace / session.run_id / "work_packages"
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        self.progress(0.1, "Cutting work packages out of the BOQ …")
        files = render_all(session.document, session.plan, out_dir, self.settings, self.log)
        for wp in session.plan.work_packages:
            if wp.wp_id in files:
                wp.pdf_file = str(files[wp.wp_id])
        if session.documents.links:
            from .docs.export import build_wp_docs
            from .render.snapshots import _stamp

            self.progress(0.8, "Attaching specifications and drawings …")
            for wp in session.plan.work_packages:
                made = build_wp_docs(session.documents, wp, out_dir, lambda p, w: _stamp(p, w, self.settings), self.log)
                wp.spec_pdf = str(made["spec"]) if "spec" in made else None
                wp.drawings_pdf = str(made["drawing"]) if "drawing" in made else None
            self.log(f"Attached specs to {sum(1 for w in session.plan.work_packages if w.spec_pdf)} and drawings to "
                     f"{sum(1 for w in session.plan.work_packages if w.drawings_pdf)} work package(s).")

        from .rfq.interface import build_rfq_packages
        handoff = [p.model_dump() for p in build_rfq_packages(session)]
        (out_dir / "work_packages.json").write_text(json.dumps(handoff, ensure_ascii=False, indent=2), encoding="utf-8")

        zip_path = self.settings.workspace / session.run_id / f"{safe_filename(Path(session.document.filename).stem)} - work packages.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out_dir.glob("*.pdf")):
                z.write(f, f.name)
            z.write(out_dir / "work_packages.json", "work_packages.json")
        session.output_dir, session.zip_file, session.status = str(out_dir), str(zip_path), "exported"
        self.log(f"Exported {len(files)} work package PDF(s) → {out_dir}")
        session.log.extend(self._session_log[-(len(files) + 3):])
        self.save(session)
        self.progress(1.0, "Done")
        return session

    # --------------------------------------------------------------------- RFQ
    def rfq_prefill(self, session: Session, template: Optional[Path] = None) -> dict:
        """Template fields + values detected from the BOQ. Nothing is committed here."""
        from .rfq.builder import prefill
        from .rfq.detect import load_field_config

        llm = self.llm if (self.settings.use_llm and self.llm.status()[0]) else None
        return prefill(session, Path(template or self.settings.rfq_template), load_field_config(self.settings.rfq_fields),
                       llm, system_prompt(self.rules), self.log)

    def generate_rfqs(self, session: Session, project_values: dict[str, str], packages: list[dict],
                      supplier_fields: list[str], template: Optional[Path] = None) -> Session:
        """Called only after the user approved every auto-filled value."""
        from .rfq.builder import generate
        from .rfq.detect import load_field_config

        if session.status != "exported":
            raise RuntimeError("Export the work packages first — each RFQ attaches its BOQ cut-out.")
        out_dir = self.settings.workspace / session.run_id / "rfq"
        session.rfq = generate(session, Path(template or self.settings.rfq_template), project_values, packages,
                               supplier_fields, out_dir, self.log, load_field_config(self.settings.rfq_fields))
        self.log(f"Generated {len(session.rfq['files'])} RFQ(s) → {out_dir}")
        self.save(session)
        return session

    # --------------------------------------------------------------- persistence
    def save(self, session: Session) -> Path:
        p = self.settings.workspace / session.run_id / "session.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(session.model_dump_json(), encoding="utf-8")     # compact: big projects save 3x faster
        tmp.replace(p)                                                   # atomic: a crash never leaves half a file
        return p

    def load(self, run_id: str) -> Session:
        p = self.settings.workspace / run_id / "session.json"
        return Session.model_validate_json(p.read_text(encoding="utf-8"))
