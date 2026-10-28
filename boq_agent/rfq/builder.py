"""RFQ drafting from the company Word template.

    prefill()  : template fields + what could be detected (never committed — the user approves)
    generate() : one .docx per work package × supplier (+ the WP's BOQ cut-out), zipped
"""
from __future__ import annotations

import datetime as dt
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..models import Session, WorkPackage
from ..text_utils import safe_filename, truncate
from .detect import detect_project_fields
from .template import discover_fields, fill_template


@dataclass
class FieldSpec:
    name: str                       # exactly as in the template, e.g. "Package Name / Scope of Works"
    scope: str = "project"          # project | package | supplier
    type: str = "text"              # text | date
    detect: str = ""                # wp_name | project_name | project_location | owner | same_as:<Field>
    help: str = ""
    label: str = ""                 # what the user sees (defaults to the field name)


@dataclass
class ProjectPrefill:
    spec: FieldSpec
    value: str = ""
    auto: bool = False              # detected with (close to) full certainty -> needs approval
    evidence: str = ""
    source: str = ""
    note: str = ""
    suggestions: list[tuple[str, str]] = field(default_factory=list)


def field_specs(template: Path, cfg: dict) -> list[FieldSpec]:
    known = {k.lower(): v for k, v in cfg.get("fields", {}).items()}
    out = []
    for name in discover_fields(template):
        c = known.get(name.lower(), {})
        out.append(FieldSpec(name=name, scope=c.get("scope", "project"), type=c.get("type", "text"),
                             detect=c.get("detect", ""), help=c.get("help", ""), label=c.get("label", name)))
    return out


def package_name(session: Session, wp: WorkPackage) -> str:
    """The WP name used in the RFQ (Div 10 per-description WPs get their full, untruncated description)."""
    if wp.key.startswith("ITEM:"):
        it = session.document.item_map()[wp.item_uids[0]]
        return truncate(it.parent_text or it.description, 140)
    return wp.name_en


def prefill(session: Session, template: Path, cfg: dict, llm=None, system: str = "",
            log: Callable[[str], None] = print) -> dict:
    specs = field_specs(template, cfg)
    det = detect_project_fields(session.document, cfg, llm, system, log) if any(
        s.detect in ("project_name", "project_location", "owner") for s in specs) else {}
    project = []
    for s in specs:
        if s.scope != "project":
            continue
        p = ProjectPrefill(spec=s)
        d = det.get(s.detect)
        if d is not None:
            p.value, p.auto, p.evidence, p.note, p.suggestions = d.value, d.auto, d.evidence, d.note, d.suggestions
            p.source = d.source
        project.append(p)
    pkg_specs = [s for s in specs if s.scope == "package"]
    packages = [{"wp_id": wp.wp_id, "wp_name": wp.name_en,
                 "values": {s.name: (package_name(session, wp) if s.detect == "wp_name" else "") for s in pkg_specs},
                 "pdf": wp.pdf_file}
                for wp in session.plan.work_packages]
    return {"specs": specs, "project": project, "packages": packages,
            "supplier_fields": [s.name for s in specs if s.scope == "supplier"]}


def generate(session: Session, template: Path, project_values: dict[str, str], packages: list[dict],
             supplier_fields: list[str], out_dir: Path, log: Callable[[str], None] = print) -> dict:
    """packages: [{"wp_id", "values": {field: value}, "suppliers": [names]}] — values already approved
    by the user. Returns a summary dict (also stored on the session)."""
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    wps = {wp.wp_id: wp for wp in session.plan.work_packages}
    specs = discover_fields(template)
    files, unfilled = [], set()
    for pk in packages:
        wp = wps[pk["wp_id"]]
        folder = out_dir / safe_filename(f"{wp.wp_id} {wp.name_en}", 70)
        folder.mkdir(parents=True, exist_ok=True)
        if wp.pdf_file and Path(wp.pdf_file).exists():
            shutil.copy2(wp.pdf_file, folder / f"BOQ - {Path(wp.pdf_file).name}")
        suppliers = [s.strip() for s in pk.get("suppliers", []) if s.strip()] or [""]
        for sup in suppliers:
            values = {**project_values, **pk.get("values", {}), **{name: sup for name in supplier_fields}}
            out = folder / (safe_filename(f"RFQ - {wp.wp_id} {wp.name_en}" + (f" - {sup}" if sup else ""), 120) + ".docx")
            fill_template(template, out, values)
            unfilled.update(f for f in specs if not str(values.get(f, "")).strip())
            files.append(str(out))
            log(f"RFQ: {out.relative_to(out_dir)}")
    zip_path = out_dir.parent / f"{safe_filename(Path(session.document.filename).stem)} - RFQs.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out_dir.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out_dir))
    return {"created": dt.datetime.now().isoformat(timespec="seconds"), "template": str(template),
            "project_values": project_values, "packages": packages, "files": files, "rfq_dir": str(out_dir),
            "zip": str(zip_path), "left_as_placeholder": sorted(unfilled)}


def spec_dicts(specs: list[FieldSpec]) -> list[dict]:
    return [asdict(s) for s in specs]
