"""Machine-readable hand-off of the exported work packages.

Every export writes `work_packages/work_packages.json` = list[RFQPackage], for any downstream tool
(vendor selection, ERP import …). The RFQ letters themselves are drafted from the company Word
template by `rfq/builder.py`.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from ..models import Session


class RFQLine(BaseModel):
    item_no: str = ""
    description: str
    unit: str = ""
    quantity: str = ""
    source: str = ""            # "Sheet!row" or "page N"


class RFQPackage(BaseModel):
    project_file: str
    run_id: str
    wp_id: str
    wp_key: str                 # stable flowchart key, e.g. "WOOD_DOORS", "DIV03" -> use for vendor mapping
    name_en: str
    name_ar: str = ""
    division: str = ""
    division_name: str = ""
    snapshot_pdf: Optional[str] = None
    special_rules: list[str] = Field(default_factory=list)
    lines: list[RFQLine] = Field(default_factory=list)


def build_rfq_packages(session: Session) -> list[RFQPackage]:
    doc = session.document
    items = doc.item_map()
    rows = doc.row_map()
    fmap = doc.file_map()
    out = []
    for wp in session.plan.work_packages:
        lines = []
        for iu in wp.item_uids:
            it = items[iu]
            ref = rows[it.row_uids[0]].ref
            src = f"{ref.sheet}!{ref.row}" if ref.sheet else f"page {(ref.page or 0) + 1}"
            if doc.multi_file and ref.file in fmap:
                src = f"{fmap[ref.file].filename} | {src}"
            lines.append(RFQLine(item_no=it.item_no, description=it.full_description, unit=it.unit,
                                 quantity=it.quantity, source=src))
        out.append(RFQPackage(project_file=doc.filename, run_id=session.run_id, wp_id=wp.wp_id, wp_key=wp.key,
                              name_en=wp.name_en, name_ar=wp.name_ar, division=wp.division,
                              division_name=wp.division_name, snapshot_pdf=wp.pdf_file,
                              special_rules=wp.rule_ids, lines=lines))
    return out
