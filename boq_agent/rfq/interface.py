"""Hand-off contract between the work-packaging agent and the (future) RFQ drafting module.

Every export writes `work_packages/work_packages.json` = list[RFQPackage]. The RFQ module should
only depend on this file (or on `build_rfq_packages(session)`), never on BOQ parsing internals.

Planned RFQ module (not implemented yet):
  1. Load RFQPackage list (+ the WP snapshot PDF as attachment).
  2. Pick vendors per WP from the vendor register (e.g. 'Vendors Workpackages.xlsx' /
     'Unifier Vendor List.xlsx') keyed by `wp_key` / `division`.
  3. Draft bilingual RFQ letters with the same local LLM (scope = `lines`, special notes = `rule_ids`),
     user reviews, then export .docx/.pdf/email.
"""
from __future__ import annotations

from typing import Optional, Protocol

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


class RFQDraft(BaseModel):
    wp_id: str
    vendor: Optional[str] = None
    subject: str = ""
    body_en: str = ""
    body_ar: str = ""
    attachments: list[str] = Field(default_factory=list)


class RFQDrafter(Protocol):
    def draft(self, package: RFQPackage, vendor: Optional[dict] = None) -> RFQDraft: ...


def build_rfq_packages(session: Session) -> list[RFQPackage]:
    doc = session.document
    items = doc.item_map()
    rows = doc.row_map()
    out = []
    for wp in session.plan.work_packages:
        lines = []
        for iu in wp.item_uids:
            it = items[iu]
            ref = rows[it.row_uids[0]].ref
            src = f"{ref.sheet}!{ref.row}" if ref.sheet else f"page {(ref.page or 0) + 1}"
            lines.append(RFQLine(item_no=it.item_no, description=it.full_description, unit=it.unit,
                                 quantity=it.quantity, source=src))
        out.append(RFQPackage(project_file=doc.filename, run_id=session.run_id, wp_id=wp.wp_id, wp_key=wp.key,
                              name_en=wp.name_en, name_ar=wp.name_ar, division=wp.division,
                              division_name=wp.division_name, snapshot_pdf=wp.pdf_file,
                              special_rules=wp.rule_ids, lines=lines))
    return out


class LLMRFQDrafter:
    """Placeholder for the next module."""

    def __init__(self, llm=None, vendor_register: Optional[str] = None):
        self.llm, self.vendor_register = llm, vendor_register

    def draft(self, package: RFQPackage, vendor: Optional[dict] = None) -> RFQDraft:
        raise NotImplementedError("RFQ drafting is the next module — the input contract (RFQPackage) is ready.")
