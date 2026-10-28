"""The BOQ work-packaging agent (orchestrator).

    analyze()  : ingest -> structure -> [LLM] divisions -> [LLM] item categories (+ reflection) -> plan
    decide()   : user approves / denies a special-case proposal -> re-plan
    export()   : PDF cut-out per work package + RFQ hand-off JSON + zip
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
    def analyze(self, files, original_names=None) -> Session:
        """files: one path or a list of paths (Excel / PDF BOQs and/or .zip archives of them).
        All BOQs found are packaged together as ONE project."""
        from .ingest.intake import collect, pdf_has_boq_table
        from .models import BOQDocument, SourceFile

        paths = [Path(f) for f in (files if isinstance(files, (list, tuple)) else [files])]
        names = list(original_names) if isinstance(original_names, (list, tuple)) else (
            [original_names] if original_names else [])
        names = [n or p.name for n, p in zip(names + [None] * len(paths), paths)]
        for n in names:
            if Path(n).suffix.lower() not in EXCEL_EXT | PDF_EXT | {".zip"}:
                raise ValueError(f"Unsupported file type '{Path(n).suffix}'. Upload Excel / PDF BOQs or a .zip of them.")
        self._session_log = []
        if len(names) == 1:
            project = Path(names[0]).stem
        else:
            project = f"{Path(names[0]).stem} +{len(names) - 1}"
        run_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{safe_filename(project, 40).replace(' ', '_')}"
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
        hdr = header_from_llm(self.llm, system_prompt(self.rules)) if use_llm else None
        doc = BOQDocument(doc_id=run_id, filename=project if multi else cands[0].name, source_path="", work_path="",
                          kind="excel")
        skipped: list[str] = []
        n = 0
        for k, c in enumerate(cands, 1):
            self.progress(0.02 + 0.10 * k / len(cands), f"Reading {k}/{len(cands)}: {Path(c.name).name} …")
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
                    if multi and not pdf_has_boq_table(c.path, self.settings):
                        skipped.append(f"{c.name} (no BOQ table — drawing / specification?)")
                        shutil.rmtree(fdir, ignore_errors=True)
                        continue
                    src = fdir / f"{fid}-{safe_filename(Path(c.name).stem, 60)}.pdf"
                    shutil.copy2(c.path, src)
                    part = read_pdf(src, run_id, c.name, self.settings, self.log, file_id=fid)
                    kind = "pdf"
            except Exception as e:  # noqa: BLE001
                skipped.append(f"{c.name} (could not be read: {e})")
                shutil.rmtree(fdir, ignore_errors=True)
                continue
            if not part.items:
                skipped.append(f"{c.name} (no BOQ items found)")
                shutil.rmtree(fdir, ignore_errors=True)
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
            raise RuntimeError("No BOQ items could be detected. " + " ".join(doc.warnings))
        first = doc.files[0]
        doc.source_path, doc.work_path, doc.kind = first.source_path, first.work_path, first.kind
        if len(doc.files) == 1:
            doc.filename = first.filename

        settings = self.settings
        if not use_llm:
            settings = Settings(**{**self.settings.__dict__, "use_llm": False})
        clf = Classifier(self.rules, settings, self.llm, self.log, self.progress)
        self.progress(0.15, "Identifying BOQ divisions (أبواب) …")
        clf.assign_divisions(doc)
        self.progress(0.25, "Classifying items against the flowchart …")
        clf.classify_items(doc)

        session = Session(run_id=run_id, created=dt.datetime.now().isoformat(timespec="seconds"), document=doc)
        session.plan = self.planner.build(doc)
        session.status = "planned"
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
        for p in session.plan.proposals:
            if p.proposal_id == proposal_id:
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
        session.log.extend(self._session_log[-(len(files) + 2):])
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

        if session.status != "exported":
            raise RuntimeError("Export the work packages first — each RFQ attaches its BOQ cut-out.")
        out_dir = self.settings.workspace / session.run_id / "rfq"
        session.rfq = generate(session, Path(template or self.settings.rfq_template), project_values, packages,
                               supplier_fields, out_dir, self.log)
        self.log(f"Generated {len(session.rfq['files'])} RFQ(s) → {out_dir}")
        self.save(session)
        return session

    # --------------------------------------------------------------- persistence
    def save(self, session: Session) -> Path:
        p = self.settings.workspace / session.run_id / "session.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(session.model_dump_json(indent=1), encoding="utf-8")
        return p

    def load(self, run_id: str) -> Session:
        p = self.settings.workspace / run_id / "session.json"
        return Session.model_validate_json(p.read_text(encoding="utf-8"))
