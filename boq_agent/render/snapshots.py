"""Work-package snapshots ("cut-outs") as PDF.

* Excel source  -> Microsoft Excel hides every row that is not part of the WP and prints the sheet
                   (keeps the original formatting, logos, print titles and ALL columns).
                   Fallback without Excel: an HTML re-rendering of the same rows/columns.
* PDF source    -> the original page regions (header band + item rows, full table width) are
                   cropped and stacked onto new pages at vector quality.
"""
from __future__ import annotations

import html
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Callable

import pymupdf as fitz
from openpyxl.utils import get_column_letter

from ..config import Settings
from ..models import BOQDocument, Plan, WorkPackage
from ..planning.planner import snapshot_rows
from ..text_utils import fmt_value, safe_filename

Log = Callable[[str], None]


def _runs(nums: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(set(nums)):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def wp_filename(wp: WorkPackage) -> str:
    return safe_filename(f"{wp.wp_id} {wp.name_en}") + ".pdf"


def _stamp(pdf_path: Path, wp: WorkPackage, settings: Settings) -> None:
    """Small WP label in the top margin of every page."""
    doc = fitz.open(pdf_path)
    arch = fitz.Archive(settings.font_dir)
    css = f"@font-face {{font-family: f; src: url({settings.font_file});}} * {{font-family: f; font-size: 7pt; color: #555;}}"
    label = html.escape(f"{wp.wp_id} | {wp.name_en}" + (f" | {wp.name_ar}" if wp.name_ar else ""))
    for i, page in enumerate(doc):
        r = page.rect
        page.insert_htmlbox(fitz.Rect(18, 3, r.width - 18, 16),
                            f"<div>{label} — page {i + 1}/{len(doc)}</div>", css=css, archive=arch)
    tmp = pdf_path.with_suffix(".tmp.pdf")
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    shutil.move(tmp, pdf_path)


# =============================================================================== Excel
def _excel_jobs(doc: BOQDocument, plan: Plan) -> list[tuple[WorkPackage, "OrderedDict[str, list[int]]"]]:
    rmap = doc.row_map()
    jobs = []
    for wp in plan.work_packages:
        per_sheet: OrderedDict[str, set[int]] = OrderedDict()
        for uid in snapshot_rows(doc, wp):
            ref = rmap[uid].ref
            rows = per_sheet.setdefault(ref.sheet, set())
            rows.update(range(ref.row, (ref.span_to or ref.row) + 1))
        jobs.append((wp, OrderedDict((k, sorted(v)) for k, v in per_sheet.items())))
    return jobs


def _header_rows(doc: BOQDocument, sheet: str) -> list[int]:
    rmap = doc.row_map()
    sc = doc.scope(sheet)
    return sorted(rmap[u].ref.row for u in sc.header_row_uids) if sc else []


def render_excel_com(doc: BOQDocument, plan: Plan, out_dir: Path, settings: Settings, log: Log) -> dict[str, Path]:
    from ..excel_com import excel_app

    results: dict[str, Path] = {}
    tmp_dir = out_dir / "_parts"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with excel_app() as xl:
        wb = xl.Workbooks.Open(str(Path(doc.work_path).resolve()), UpdateLinks=0, ReadOnly=True)
        for wp, sheets in _excel_jobs(doc, plan):
            parts = []
            for k, (sheet, rows) in enumerate(sheets.items()):
                sc = doc.scope(sheet)
                ws = wb.Worksheets(sheet)
                hdr = _header_rows(doc, sheet)
                last = max([sc.last_row] + rows)
                orig_hidden = set(sc.hidden_rows)
                show = (set(range(min(hdr), max(hdr) + 1)) if hdr else set()) | set(rows)
                ws.Rows(f"1:{last}").Hidden = True
                for a, b in _runs([r for r in show if r not in orig_hidden]):
                    ws.Rows(f"{a}:{b}").Hidden = False
                try:
                    xl.PrintCommunication = False
                except Exception:  # noqa: BLE001
                    pass
                ps = ws.PageSetup
                ps.PrintArea = f"${get_column_letter(sc.first_col)}$1:${get_column_letter(sc.last_col)}${last}"
                if hdr:
                    ps.PrintTitleRows = f"${min(hdr)}:${max(hdr)}"
                ps.Zoom = False
                ps.FitToPagesWide = 1
                ps.FitToPagesTall = False
                try:
                    xl.PrintCommunication = True
                except Exception:  # noqa: BLE001
                    pass
                ws.ResetAllPageBreaks()
                part = tmp_dir / f"{wp.wp_id}_{k}.pdf"
                ws.ExportAsFixedFormat(0, str(part.resolve()), 0, False, False)
                parts.append(part)
                ws.Rows(f"1:{last}").Hidden = False
                for a, b in _runs(sorted(orig_hidden)):
                    ws.Rows(f"{a}:{b}").Hidden = True
            out = out_dir / wp_filename(wp)
            merged = fitz.open()
            for p in parts:
                with fitz.open(p) as d:
                    merged.insert_pdf(d)
            merged.save(out)
            merged.close()
            _stamp(out, wp, settings)
            results[wp.wp_id] = out
            log(f"{wp.wp_id}: {out.name} ({sum(len(r) for r in sheets.values())} rows) — Excel")
        wb.Close(SaveChanges=False)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return results


def render_excel_html(doc: BOQDocument, plan: Plan, out_dir: Path, settings: Settings, log: Log) -> dict[str, Path]:
    import openpyxl

    wb = openpyxl.load_workbook(doc.work_path, data_only=True)
    arch = fitz.Archive(settings.font_dir)
    results = {}
    for wp, sheets in _excel_jobs(doc, plan):
        parts = [f"<h3>{html.escape(wp.wp_id)} — {html.escape(wp.name_en)} {html.escape(wp.name_ar)}</h3>"]
        max_cols = 1
        for sheet, rows in sheets.items():
            sc = doc.scope(sheet)
            ws = wb[sheet]
            hdr = _header_rows(doc, sheet)
            show = sorted(((set(range(min(hdr), max(hdr) + 1)) if hdr else set()) | set(rows)) - set(sc.hidden_rows))
            cols = [c for c in range(sc.first_col, sc.last_col + 1) if c not in set(sc.hidden_cols)]
            max_cols = max(max_cols, len(cols))
            parts.append(_sheet_table_html(ws, show, cols, set(hdr), sc.rtl or _looks_rtl(ws, show, cols)))
        out = out_dir / wp_filename(wp)
        css = (f"@font-face {{font-family: f; src: url({settings.font_file});}} "
               "* {font-family: f;} h3 {font-size: 11pt; margin: 0 0 6px 0;} "
               "table {border-collapse: collapse; width: 100%; margin-bottom: 10px;} "
               "td {border: 0.5px solid #777; padding: 2px 3px; font-size: 7pt; vertical-align: top;} "
               "tr.h td {background: #e8e8e8; font-weight: bold; text-align: center;}")
        story = fitz.Story(html="".join(parts), user_css=css, archive=arch)
        paper = fitz.paper_rect("a4-l" if max_cols > 5 else "a4")
        where = paper + (24, 30, -24, -24)
        writer = fitz.DocumentWriter(str(out))
        more = 1
        while more:
            dev = writer.begin_page(paper)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
        writer.close()
        _stamp(out, wp, settings)
        results[wp.wp_id] = out
        log(f"{wp.wp_id}: {out.name} — HTML renderer")
    return results


def _looks_rtl(ws, rows, cols) -> bool:
    from ..text_utils import is_arabic
    sample = " ".join(str(ws.cell(r, c).value or "") for r in rows[:30] for c in cols)
    return is_arabic(sample)


def _sheet_table_html(ws, rows: list[int], cols: list[int], header_rows: set[int], rtl: bool) -> str:
    shown = set(rows)
    colset = set(cols)
    merge_top: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for mr in ws.merged_cells.ranges:
        first_vis_row = next((r for r in range(mr.min_row, mr.max_row + 1) if r in shown), None)
        first_vis_col = next((c for c in range(mr.min_col, mr.max_col + 1) if c in colset), None)
        if first_vis_row is None or first_vis_col is None:
            continue
        rs = sum(1 for r in range(mr.min_row, mr.max_row + 1) if r in shown)
        cs = sum(1 for c in range(mr.min_col, mr.max_col + 1) if c in colset)
        merge_top[(first_vis_row, first_vis_col)] = (rs, cs, mr.min_row, mr.min_col)
        for r in range(mr.min_row, mr.max_row + 1):
            for c in range(mr.min_col, mr.max_col + 1):
                if (r, c) != (first_vis_row, first_vis_col):
                    covered.add((r, c))
    widths = []
    for c in cols:
        w = ws.column_dimensions[get_column_letter(c)].width
        widths.append(w if w else 8.43)
    tot = sum(widths) or 1
    out = [f'<table dir="{"rtl" if rtl else "ltr"}"><colgroup>']
    out += [f'<col width="{100 * w / tot:.1f}%">' for w in widths]
    out.append("</colgroup>")
    for r in rows:
        out.append(f'<tr class="{"h" if r in header_rows else ""}">')
        for c in cols:
            if (r, c) in covered:
                continue
            attrs = ""
            src_r, src_c = r, c
            if (r, c) in merge_top:
                rs, cs, src_r, src_c = merge_top[(r, c)]
                attrs = f' rowspan="{rs}" colspan="{cs}"'
            cell = ws.cell(src_r, src_c)
            style = []
            if cell.font is not None and cell.font.b:
                style.append("font-weight:bold")
            fill = cell.fill
            if fill is not None and fill.fill_type == "solid":
                rgb = getattr(fill.fgColor, "rgb", None)
                if isinstance(rgb, str) and len(rgb) == 8 and rgb[2:] not in ("000000",):
                    style.append(f"background:#{rgb[2:]}")
            al = cell.alignment.horizontal if cell.alignment is not None else None
            if al in ("center", "centerContinuous"):
                style.append("text-align:center")
            elif al in ("left", "right"):
                style.append(f"text-align:{al}")
            text = html.escape(fmt_value(cell.value)).replace("\n", "<br>")
            out.append(f'<td{attrs} style="{";".join(style)}">{text}</td>')
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


# ================================================================================= PDF
def render_pdf_crops(doc: BOQDocument, plan: Plan, out_dir: Path, settings: Settings, log: Log) -> dict[str, Path]:
    src = fitz.open(doc.work_path)
    rmap = doc.row_map()
    order = {r.uid: i for i, r in enumerate(doc.rows)}
    scope = doc.scope("pdf")
    content_rows = [r for r in doc.rows if r.role not in ("header", "blank")]
    pos = {r.uid: i for i, r in enumerate(content_rows)}
    results = {}
    for wp in plan.work_packages:
        uids = [u for u in snapshot_rows(doc, wp) if u in pos]
        uids.sort(key=lambda u: order[u])
        # contiguous runs (in reading order, same page) -> one clip each
        clips: list[tuple[int, float, float]] = []
        prev_pos = None
        for u in uids:
            r = rmap[u]
            pg, (x0, y0, x1, y1) = r.ref.page, r.ref.bbox
            if clips and prev_pos is not None and pos[u] == prev_pos + 1 and clips[-1][0] == pg:
                p, a, b = clips[-1]
                clips[-1] = (p, min(a, y0), max(b, y1))
            else:
                clips.append((pg, y0, y1))
            prev_pos = pos[u]
        out = out_dir / wp_filename(wp)
        _compose(src, clips, scope, out)
        _stamp(out, wp, settings)
        results[wp.wp_id] = out
        log(f"{wp.wp_id}: {out.name} ({len(clips)} region(s)) — PDF crop")
    return results


def _compose(src, clips, scope, out: Path) -> None:
    dst = fitz.open()
    W, H = src[0].rect.width, src[0].rect.height
    margin, top = 20, 22
    page, y = None, 0.0
    last_hdr_page = None

    def extent(pg):
        ext = scope.page_extents.get(pg) or scope.page_extents.get(str(pg))
        return ext or [0, src[pg].rect.width]

    def header_for(pg):
        hb = scope.page_headers.get(pg) or scope.page_headers.get(str(pg))
        if hb:
            return pg, hb
        keys = sorted(int(k) for k in scope.page_headers)
        prior = [k for k in keys if k <= pg] or keys
        if not prior:
            return None
        k = prior[-1]
        return k, scope.page_headers.get(k) or scope.page_headers.get(str(k))

    def new_page(pg):
        nonlocal page, y, last_hdr_page
        page = dst.new_page(width=W, height=H)
        y = top
        h = header_for(pg)
        if h:
            hp, hb = h
            x0, x1 = extent(hp)
            clip = fitz.Rect(x0, hb[1] - 3, x1, hb[3] + 3)
            place(hp, clip)
            last_hdr_page = hp

    def place(pg, clip):
        nonlocal y
        scale = min(1.0, (W - 2 * margin) / clip.width)
        h = clip.height * scale
        page.show_pdf_page(fitz.Rect(margin, y, margin + clip.width * scale, y + h), src, pg, clip=clip)
        y += h

    for pg, y0, y1 in clips:
        x0, x1 = extent(pg)
        clip = fitz.Rect(x0, y0 - 2, x1, y1 + 2)
        while clip.height > 0:
            if page is None:
                new_page(pg)
            scale = min(1.0, (W - 2 * margin) / clip.width)
            avail = (H - margin - y) / scale
            if avail < min(clip.height, 20):
                new_page(pg)
                continue
            part = fitz.Rect(clip.x0, clip.y0, clip.x1, clip.y0 + min(clip.height, avail))
            place(pg, part)
            clip = fitz.Rect(clip.x0, part.y1, clip.x1, clip.y1)
    if page is None:
        dst.new_page(width=W, height=H)
    dst.save(out, garbage=3, deflate=True)
    dst.close()


# ============================================================================ dispatch
def render_all(doc: BOQDocument, plan: Plan, out_dir: Path, settings: Settings, log: Log) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if doc.kind == "pdf":
        return render_pdf_crops(doc, plan, out_dir, settings, log)
    mode = settings.excel_renderer
    if mode in ("auto", "excel"):
        try:
            return render_excel_com(doc, plan, out_dir, settings, log)
        except Exception as e:  # noqa: BLE001
            if mode == "excel":
                raise
            log(f"Excel rendering unavailable ({e}); using the built-in HTML renderer.")
    return render_excel_html(doc, plan, out_dir, settings, log)
