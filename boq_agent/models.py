"""Data model shared by every stage (and by the future RFQ module).

Everything is pydantic so a whole `Session` round-trips to JSON on disk.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

RowRole = Literal["header", "section", "heading", "parent", "continuation", "leaf", "subtotal", "blank"]
Decision = Literal["pending", "approved", "denied"]


class SourceFile(BaseModel):
    """One uploaded BOQ file of the project."""
    id: str                               # "F1", "F2" … ("" = legacy single-file session)
    filename: str                         # as uploaded (incl. folder inside a zip)
    source_path: str
    work_path: str                        # file actually read/rendered (.xls converted to .xlsx …)
    kind: Literal["excel", "pdf"]

    @property
    def label(self) -> str:
        from pathlib import PurePath
        return PurePath(self.filename).stem


class SourceRef(BaseModel):
    file: str = ""                        # SourceFile.id
    sheet: Optional[str] = None           # Excel sheet name
    row: Optional[int] = None             # Excel 1-based row
    span_to: Optional[int] = None         # Excel: last row covered by vertical merges starting on this row
    page: Optional[int] = None            # PDF 0-based page
    bbox: Optional[list[float]] = None    # PDF x0, y0, x1, y1


class Row(BaseModel):
    uid: str
    ref: SourceRef
    cells: list[str] = Field(default_factory=list)       # display text per column, left -> right
    fields: dict[str, str] = Field(default_factory=dict)  # item_no / unit / quantity / rate / total
    text: str = ""                                        # descriptive text of the row
    role: RowRole = "blank"


class Item(BaseModel):
    """One priced BOQ line (a leaf with unit/quantity) plus its context."""
    uid: str
    section_uid: str
    item_no: str = ""
    description: str = ""
    parent_text: str = ""                 # description of the parent item (e.g. "AR 9.0" for "AR 9.1")
    headings: list[str] = Field(default_factory=list)
    unit: str = ""
    quantity: str = ""
    rate: str = ""
    total: str = ""
    row_uids: list[str] = Field(default_factory=list)          # the leaf rows
    context_row_uids: list[str] = Field(default_factory=list)  # parent + heading rows shown with it
    parent_row_uids: list[str] = Field(default_factory=list)   # the parent description rows only

    # classification
    category: Optional[str] = None        # flowchart category id, "MISC", or None (= stays with its section)
    confidence: float = 0.0
    method: str = ""                      # llm | llm-review | keyword | section | per_item | manual
    keyword_hint: Optional[str] = None
    reason: str = ""
    variant_group: Optional[str] = None   # uid of the first item of its size/colour/code variant group
    umbrella: Optional[str] = None        # product group (same purpose / same product in other sizes) for item-by-item divisions

    def llm_text(self, max_parent: int = 260, max_desc: int = 320) -> str:
        from .text_utils import truncate
        parts = []
        if self.headings:
            parts.append(f"(group: {truncate(self.headings[-1], 90)})")
        if self.parent_text:
            parts.append(f"PARENT: {truncate(self.parent_text, max_parent)} ||")
        parts.append(f"ITEM: {truncate(self.description, max_desc)}")
        if self.unit:
            parts.append(f"[unit: {self.unit}]")
        return " ".join(parts)

    @property
    def full_description(self) -> str:
        return f"{self.parent_text} — {self.description}" if self.parent_text else self.description


class Section(BaseModel):
    """A BOQ 'باب' / Division / Bill."""
    uid: str
    title: str
    sheet: Optional[str] = None
    file_id: str = ""                     # SourceFile.id
    title_row_uids: list[str] = Field(default_factory=list)
    subtotal_row_uids: list[str] = Field(default_factory=list)
    item_uids: list[str] = Field(default_factory=list)
    division: Optional[str] = None        # CSI code e.g. "08"; None = unknown
    division_confidence: float = 0.0
    division_method: str = ""


class TableScope(BaseModel):
    """One table area: an Excel sheet, or the whole PDF."""
    name: str
    kind: Literal["excel", "pdf"]
    file: str = ""                        # SourceFile.id
    header_row_uids: list[str] = Field(default_factory=list)
    header_labels: dict[str, str] = Field(default_factory=dict)   # field -> header text
    field_columns: dict[str, int] = Field(default_factory=dict)   # field -> column index
    # Excel
    first_col: int = 1
    last_col: int = 1
    last_row: int = 1
    hidden_rows: list[int] = Field(default_factory=list)
    hidden_cols: list[int] = Field(default_factory=list)
    rtl: bool = False
    # PDF: page -> header band bbox, page -> table x-extent
    page_headers: dict[int, list[float]] = Field(default_factory=dict)
    page_extents: dict[int, list[float]] = Field(default_factory=dict)


class BOQDocument(BaseModel):
    doc_id: str
    filename: str
    source_path: str
    work_path: str                        # file actually read/rendered (e.g. .xls converted to .xlsx)
    kind: Literal["excel", "pdf"]
    files: list[SourceFile] = Field(default_factory=list)   # all BOQs of the project (empty in old sessions)
    scopes: list[TableScope] = Field(default_factory=list)
    rows: list[Row] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def row_map(self) -> dict[str, Row]:
        return {r.uid: r for r in self.rows}

    def item_map(self) -> dict[str, Item]:
        return {i.uid: i for i in self.items}

    def section_map(self) -> dict[str, Section]:
        return {s.uid: s for s in self.sections}

    def scope(self, name: str, file: str = "") -> Optional[TableScope]:
        return next((s for s in self.scopes if s.name == name and s.file == file), None)

    def file_list(self) -> list[SourceFile]:
        """The project's BOQ files (old single-file sessions → one file with id "")."""
        return self.files or [SourceFile(id="", filename=self.filename, source_path=self.source_path,
                                         work_path=self.work_path, kind=self.kind)]

    def file_map(self) -> dict[str, SourceFile]:
        return {f.id: f for f in self.file_list()}

    @property
    def multi_file(self) -> bool:
        return len(self.files) > 1


class WorkPackage(BaseModel):
    wp_id: str                            # "WP-01"
    key: str                              # planner key, e.g. "DIV03", "WOOD_DOORS", "ITEM:x"
    name_en: str
    name_ar: str = ""
    division: str = ""
    division_name: str = ""
    item_uids: list[str] = Field(default_factory=list)
    source_sections: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)  # SourceFile ids the items come from
    rule_ids: list[str] = Field(default_factory=list)       # special rules that shaped this WP
    notes: list[str] = Field(default_factory=list)
    pdf_file: Optional[str] = None
    spec_pdf: Optional[str] = None        # the package's specification sections (whole pages)
    drawings_pdf: Optional[str] = None    # the package's drawing sheets (whole sheets)

    @property
    def display_name(self) -> str:
        return f"{self.wp_id} — {self.name_en}" + (f" ({self.name_ar})" if self.name_ar else "")


class Proposal(BaseModel):
    """A flowchart special case (oval) that needs the user's approval."""
    proposal_id: str
    rule_id: str
    title_en: str
    title_ar: str = ""
    message_en: str
    message_ar: str = ""
    item_uids: list[str] = Field(default_factory=list)
    approve_key: str
    deny_key: str                         # "ITEMS" = per-item keys (Div 10 case) handled by planner
    approve_label: str = ""
    deny_label: str = ""
    decision: Decision = "pending"


class Plan(BaseModel):
    work_packages: list[WorkPackage] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- specs & drawings
DocKind = Literal["spec", "drawing", "ignored"]


class DocFile(BaseModel):
    """An uploaded specification or drawing file (the BOQs are SourceFile)."""
    id: str                               # "D1", "D2" …
    filename: str
    path: str                             # local copy that is read / cut
    kind: DocKind
    pages: int = 0
    sha: str = ""                         # content hash (cache key)
    note: str = ""                        # why it was sorted this way / problems


class SpecSection(BaseModel):
    """One specification section, e.g. 'SECTION 04 20 00 UNIT MASONRY', pages 1-19."""
    uid: str                              # "D1:s1"
    file_id: str
    code: str = ""                        # digits only: "042000" (old 5-digit codes: "04200")
    title: str = ""
    division: str = ""                    # "04"
    pages: list[int] = Field(default_factory=list)   # 0-based
    intro: str = ""                       # the 'section includes' sentence
    source: str = "text"                  # text | ocr | vlm

    @property
    def code_display(self) -> str:
        c = self.code
        return f"{c[:2]} {c[2:4]} {c[4:6]}" if len(c) == 6 else c

    @property
    def label(self) -> str:
        return (f"Section {self.code_display} " if self.code else "") + (self.title or "(untitled)")


class DrawingSheet(BaseModel):
    """One drawing sheet (a PDF page or an image)."""
    uid: str                              # "D2:p3"
    file_id: str
    page: int = 0
    number: str = ""                      # drawing number from the title block
    title: str = ""
    discipline: str = ""                  # AR / ST / ME / EL / PL / FF / LS / CV / ID
    kind: str = ""                        # plan / elevation / section / detail / schedule / legend / site / cover
    legend: list[str] = Field(default_factory=list)  # material / finish / legend lines (boilerplate removed)
    source: str = "text"                  # text | vlm
    review: bool = False                  # low-confidence reading (VLM only, illegible …)

    @property
    def label(self) -> str:
        return " · ".join(x for x in (self.number, self.title) if x) or f"page {self.page + 1}"


class DocLink(BaseModel):
    """A spec section or drawing sheet attached to a work package."""
    wp_key: str                           # WorkPackage.key (stable across re-plans)
    ref: str                              # SpecSection.uid or DrawingSheet.uid
    kind: Literal["spec", "drawing"]
    method: str                           # code | title | division | legend | sheet type | drawing ref | llm | manual
    reason: str = ""
    confidence: float = 1.0


class ProjectDocs(BaseModel):
    files: list[DocFile] = Field(default_factory=list)
    sections: list[SpecSection] = Field(default_factory=list)
    sheets: list[DrawingSheet] = Field(default_factory=list)
    links: list[DocLink] = Field(default_factory=list)
    # user corrections per work package key: {"add": [refs], "remove": [refs]} — survive re-plans
    overrides: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    stats: dict = Field(default_factory=dict)

    def file_map(self) -> dict[str, DocFile]:
        return {f.id: f for f in self.files}

    def ref_map(self) -> dict[str, "SpecSection | DrawingSheet"]:
        return {**{s.uid: s for s in self.sections}, **{s.uid: s for s in self.sheets}}

    def links_for(self, wp_key: str, kind: Optional[str] = None) -> list[DocLink]:
        return [l for l in self.links if l.wp_key == wp_key and (kind is None or l.kind == kind)]


class Session(BaseModel):
    run_id: str
    created: str
    status: Literal["analyzed", "planned", "exported"] = "analyzed"
    document: BOQDocument
    plan: Plan = Field(default_factory=Plan)
    output_dir: Optional[str] = None
    zip_file: Optional[str] = None
    rfq: dict = Field(default_factory=dict)   # last RFQ generation: approved values, files, zip
    documents: ProjectDocs = Field(default_factory=ProjectDocs)   # specs & drawings (optional uploads)
    wp_names: dict[str, str] = Field(default_factory=dict)        # user renames, per work-package key
    rfq_draft: dict = Field(default_factory=dict)                 # RFQ form as last edited (kept between visits)
    log: list[str] = Field(default_factory=list)
