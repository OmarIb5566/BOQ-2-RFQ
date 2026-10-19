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
from .knowledge.rules import MISC, FlowchartRules
from .llm.client import OllamaClient
from .llm.prompts import system_prompt
from .models import Session
from .planning.planner import Planner
from .render.snapshots import render_all
from .text_utils import safe_filename

EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb"}
PDF_EXT = {".pdf"}


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
    def analyze(self, file_path: Path, original_name: Optional[str] = None) -> Session:
        file_path = Path(file_path)
        name = original_name or file_path.name
        ext = Path(name).suffix.lower()
        if ext not in EXCEL_EXT | PDF_EXT:
            raise ValueError(f"Unsupported file type '{ext}'. Upload an Excel (.xlsx/.xls/.xlsm/.xlsb) or PDF BOQ.")
        self._session_log = []
        run_id = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{safe_filename(Path(name).stem, 40).replace(' ', '_')}"
        run_dir = self.settings.workspace / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        llm_ok, msg = (self.llm.status() if self.settings.use_llm else (False, "LLM disabled"))
        self.log(f"LLM: {msg}")
        if not llm_ok:
            self.log("Running in KEYWORD-ONLY mode (no LLM). Results will be less accurate.")
        use_llm = self.settings.use_llm and llm_ok

        self.progress(0.05, "Reading the BOQ …")
        if ext in PDF_EXT:
            src = run_dir / "source.pdf"
            shutil.copy2(file_path, src)
            doc = read_pdf(src, run_id, name, self.settings, self.log)
        else:
            src = prepare_workbook(file_path, run_dir, self.log)
            hdr = header_from_llm(self.llm, system_prompt(self.rules)) if use_llm else None
            doc = read_excel(src, run_id, name, llm_header=hdr, log=self.log)
        for w in doc.warnings:
            self.log("⚠ " + w)
        if not doc.items:
            raise RuntimeError("No BOQ items could be detected. " + " ".join(doc.warnings))

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
        it = session.document.item_map()[item_uid]
        it.category = category if category not in ("", "NONE") else None
        it.method, it.confidence, it.reason = "manual", 1.0, "set by user"
        return self.replan(session)

    def replan(self, session: Session) -> Session:
        session.plan = self.planner.build(session.document, previous=session.plan)
        session.status = "planned"
        self.save(session)
        return session

    def item_category_options(self, session: Session, section_uid: str) -> list[str]:
        sec = session.document.section_map()[section_uid]
        mode = self.rules.mode(sec.division)
        if mode == "split":
            return self.rules.split_ids + self.rules.special_ids + [MISC]
        if mode == "as_is":
            return ["NONE"] + self.rules.special_ids
        return ["NONE"]

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

    # --------------------------------------------------------------- persistence
    def save(self, session: Session) -> Path:
        p = self.settings.workspace / session.run_id / "session.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(session.model_dump_json(indent=1), encoding="utf-8")
        return p

    def load(self, run_id: str) -> Session:
        p = self.settings.workspace / run_id / "session.json"
        return Session.model_validate_json(p.read_text(encoding="utf-8"))
