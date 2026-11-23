"""The RFQ register: one numbered RFQ per work package × supplier, with a lifecycle, due dates and quotes.

    draft → approved → issued ⇄ clarification → quoted → evaluated → awarded / not awarded
                                 ↘ declined                        (cancelled from most states)
Who may move an RFQ is decided in perms.RFQ_FLOW.

Pricing sheets: at export, every work package gets "Pricing - <WP>.xlsx" (item, description, unit, qty and an empty
rate column; column A holds the item id, hidden). A supplier who returns that sheet filled in can be compared
automatically: record the quote with the sheet → the bid comparison workbook puts every supplier's rates side by
side, with the lowest highlighted. Everything is written with openpyxl / PyMuPDF — no Microsoft Office needed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import zipfile
from pathlib import Path
from typing import Optional

from boq_agent.text_utils import safe_filename

from .db import new_id, now

STATUSES = ["draft", "approved", "issued", "clarification", "quoted", "evaluated", "awarded", "not_awarded",
            "declined", "cancelled"]
STATUS_LABEL = {"draft": "Draft", "approved": "Approved", "issued": "Issued", "clarification": "Clarification",
                "quoted": "Quoted", "evaluated": "Evaluated", "awarded": "Awarded", "not_awarded": "Not awarded",
                "declined": "Declined", "cancelled": "Cancelled"}
OPEN = ("issued", "clarification")              # waiting for the supplier: these can be overdue
ACTIVE = ("draft", "approved", "issued", "clarification", "quoted", "evaluated")


def today() -> str:
    return dt.date.today().isoformat()


def is_overdue(r: dict) -> bool:
    return bool(r.get("due_date")) and r["status"] in OPEN and r["due_date"] < today()


def parse_date(v) -> Optional[str]:
    if v is None or v == "":
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    v = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(v[:20], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def new_number(db) -> str:
    year = dt.date.today().year
    return f"RFQ-{year}-{db.next_number(f'rfq-{year}'):04d}"


def create(db, run_id: str, wp_key: str, wp_id: str, package: str, supplier: str, user_id: Optional[int],
           due_date: Optional[str] = None, letter: str = "", status: str = "draft", number: str = "",
           amount: Optional[float] = None, notes: str = "") -> str:
    rid = new_id()
    db.x("INSERT INTO rfqs (id, number, run_id, wp_key, wp_id, package, supplier, status, due_date, letter, "
         "quote_amount, notes, created_by, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (rid, number or new_number(db), run_id, wp_key, wp_id, package[:300], supplier[:200], status, due_date,
          letter, amount, notes[:2000], user_id, now(), now()))
    event(db, rid, user_id, "created", None, status)
    return rid


def event(db, rfq_id: str, user_id, action: str, fr: Optional[str], to: Optional[str], note: str = "") -> None:
    db.x("INSERT INTO rfq_events (id, rfq_id, ts, user_id, action, from_status, to_status, note) VALUES (?,?,?,?,?,?,?,?)",
         (new_id(), rfq_id, now(), user_id, action, fr, to, note[:1000]))


def move(db, rfq: dict, to: str, user: dict, note: str = "") -> str:
    """Change the status if the user's role allows this step; returns '' or the reason it is refused."""
    from .perms import RFQ_FLOW
    allowed = RFQ_FLOW.get((rfq["status"], to))
    if allowed is None:
        return f"An RFQ cannot go from {STATUS_LABEL[rfq['status']]} to {STATUS_LABEL.get(to, to)}."
    if user["role"] not in allowed:
        return f"Your role cannot move an RFQ to {STATUS_LABEL[to]}."
    extra = ", issued_at=?" if to == "issued" and not rfq.get("issued_at") else ""
    args = (to, now(), *((now(),) if extra else ()), rfq["id"], rfq["version"])
    n = db.conn().execute(f"UPDATE rfqs SET status=?, updated=?{extra}, version=version+1 WHERE id=? AND version=?",
                          args).rowcount
    if not n:
        return "Someone else changed this RFQ meanwhile — reload the page."
    event(db, rfq["id"], user["id"], "status", rfq["status"], to, note)
    return ""


# ---------------------------------------------------------------------- created with the RFQ letters
def register_letters(runtime, run_id: str, session, args: dict, user_id) -> int:
    db = runtime.db
    keys = {wp.wp_id: wp for wp in session.plan.work_packages}
    files = [str(f) for f in session.rfq.get("files", [])]
    due = parse_date(args.get("due_date"))
    n = 0
    for pk in args.get("packages", []):
        wp = keys.get(pk["wp_id"])
        if wp is None:
            continue
        for sup in (pk.get("suppliers") or [""]):
            exists = db.one("SELECT id, letter FROM rfqs WHERE run_id=? AND wp_key=? AND supplier=? AND deleted=0 "
                            "AND status NOT IN ('cancelled')", (run_id, wp.key, sup))
            letter = _find_letter(files, wp.wp_id, sup)
            if exists:
                if letter and letter != exists["letter"]:
                    db.x("UPDATE rfqs SET letter=?, updated=? WHERE id=?", (letter, now(), exists["id"]))
                continue
            create(db, run_id, wp.key, wp.wp_id, wp.name_en, sup, user_id, due_date=due, letter=letter)
            n += 1
    return n


def _find_letter(files: list[str], wp_id: str, supplier: str) -> str:
    sup = safe_filename(supplier, 60).lower() if supplier else ""
    for f in files:
        name = Path(f).name.lower()
        if wp_id.lower() in name and (not sup or sup[:20] in name):
            return f
    return ""


# ---------------------------------------------------------------------- pricing sheets (export)
def pricing_dir(session, run_dir: Path) -> Path:
    base = Path(session.output_dir) if session.output_dir else Path(run_dir) / "work_packages"
    return base / "pricing"


def write_pricing_sheets(session, run_dir: Path) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation
    items = session.document.item_map()
    out = pricing_dir(session, run_dir)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for wp in session.plan.work_packages:
        wb = Workbook()
        ws = wb.active
        ws.title = "Pricing"
        ws["B1"] = f"{wp.wp_id} · {wp.name_en}"
        ws["B1"].font = Font(bold=True, size=13)
        ws["B2"] = "Supplier:"
        ws["C2"] = ""
        ws["C2"].fill = PatternFill("solid", fgColor="FFF3DF")
        ws["B3"] = "Fill in the yellow cells (unit rates). Do not change other columns or the order of the rows."
        ws["B3"].font = Font(italic=True, color="666666")
        head = ["id", "Item", "Description", "Unit", "Qty", "Unit rate", "Total", "Notes"]
        ws.append([])
        ws.append(head)
        r0 = ws.max_row
        for c in ws[r0]:
            c.font = Font(bold=True)
        for uid in wp.item_uids:
            it = items.get(uid)
            if it is None:
                continue
            qty = _num(it.quantity)
            ws.append([uid, it.item_no, it.full_description[:2000], it.unit, qty if qty is not None else it.quantity,
                       None, None, None])
            r = ws.max_row
            ws.cell(r, 7).value = f'=IF(AND(ISNUMBER(E{r}),ISNUMBER(F{r})),E{r}*F{r},"")'
            ws.cell(r, 6).fill = PatternFill("solid", fgColor="FFF3DF")
            ws.cell(r, 3).alignment = Alignment(wrap_text=True, vertical="top")
        last = ws.max_row
        ws.append([None, None, "TOTAL", None, None, None, f"=SUM(G{r0 + 1}:G{last})"])
        ws.cell(ws.max_row, 3).font = Font(bold=True)
        ws.column_dimensions["A"].hidden = True
        for col, w in zip("BCDEFGH", (10, 70, 8, 10, 12, 14, 24)):
            ws.column_dimensions[col].width = w
        dv = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"F{r0 + 1}:F{last}")
        ws.freeze_panes = ws.cell(r0 + 1, 2)
        f = out / f"Pricing - {safe_filename(wp.wp_id + ' ' + wp.name_en, 90)}.xlsx"
        wb.save(f)
        made.append(f)
    if session.zip_file and Path(session.zip_file).exists():
        with zipfile.ZipFile(session.zip_file, "a") as z:
            have = set(z.namelist())
            for f in made:
                arc = f"pricing/{f.name}"
                if arc not in have:
                    z.write(f, arc)
    return len(made)


def pricing_sheet_for(session, run_dir: Path, wp_key: str) -> Optional[Path]:
    wp = next((w for w in session.plan.work_packages if w.key == wp_key), None)
    if wp is None:
        return None
    f = pricing_dir(session, run_dir) / f"Pricing - {safe_filename(wp.wp_id + ' ' + wp.name_en, 90)}.xlsx"
    return f if f.exists() else None


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[^\d.\-]", "", str(v).replace(",", ""))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- quotes
def read_priced_sheet(path: Path) -> dict:
    """{'rates': {item uid: {'rate', 'total'}}, 'total': float, 'missing': n} from a returned pricing sheet."""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rates, total, missing, head_row = {}, 0.0, 0, None
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        vals = list(row) + [None] * 8
        if head_row is None:
            if str(vals[0] or "").strip().lower() == "id" and str(vals[5] or "").strip().lower().startswith("unit rate"):
                head_row = i
            continue
        uid = str(vals[0] or "").strip()
        if not uid:
            continue
        qty, rate = _num(vals[4]), _num(vals[5])
        if rate is None:
            missing += 1
            rates[uid] = {"rate": None, "total": None, "note": str(vals[7] or "")}
            continue
        t = _num(vals[6])
        if t is None and qty is not None:
            t = qty * rate
        rates[uid] = {"rate": rate, "total": t, "note": str(vals[7] or "")}
        total += t or 0.0
    wb.close()
    if head_row is None:
        raise ValueError("This is not a pricing sheet from this app (no 'id … Unit rate' header row).")
    return {"rates": rates, "total": round(total, 2), "missing": missing}


def quote_dir(run_dir: Path) -> Path:
    return Path(run_dir) / "quotes"


def save_quote(db, store, rfq: dict, upload, amount: Optional[float], user: dict) -> str:
    """Store a supplier's quote (the priced sheet, or any PDF/Excel with an amount). Returns a message."""
    from boq_agent.ingest.sniff import check_head
    ext = Path(upload.filename).suffix.lower()
    if ext not in (".xlsx", ".pdf", ".xlsm"):
        raise ValueError("Upload the priced sheet (.xlsx) or the quotation as PDF.")
    head = upload.stream.read(2048)
    upload.stream.seek(0)
    if check_head(head, f"quote{ext}"):
        raise ValueError("That file is not a real " + ext + " file.")
    d = quote_dir(store.run_dir(rfq["run_id"])) if rfq["run_id"] else Path(store.settings.workspace) / "_quotes"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"{rfq['number']} - {safe_filename(rfq['supplier'] or 'supplier', 60)}{ext}"
    upload.save(dst)
    parsed = None
    if ext in (".xlsx", ".xlsm"):
        try:
            parsed = read_priced_sheet(dst)
        except ValueError:
            parsed = None
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"The workbook cannot be read ({type(e).__name__}).") from e
    if parsed:
        dst.with_suffix(".json").write_text(json.dumps(parsed), encoding="utf-8")
        if amount is None:
            amount = parsed["total"]
    rel = str(dst)
    db.x("UPDATE rfqs SET quote_file=?, quote_amount=?, updated=?, version=version+1 WHERE id=?",
         (rel, amount, now(), rfq["id"]))
    event(db, rfq["id"], user["id"], "quote", None, None,
          f"{dst.name}" + (f" · {amount:,.2f}" if amount is not None else "") +
          (f" · {parsed['missing']} item(s) without a rate" if parsed and parsed["missing"] else ""))
    if rfq["status"] in ("issued", "clarification"):
        move(db, {**rfq, "version": rfq["version"] + 1}, "quoted", user, "quote received")
    return "Quote recorded" + (f" — {len(parsed['rates']) - parsed['missing']} rate(s) read from the pricing sheet."
                               if parsed else ".")


def load_quote(rfq: dict) -> Optional[dict]:
    if not rfq.get("quote_file"):
        return None
    j = Path(rfq["quote_file"]).with_suffix(".json")
    if j.exists():
        return json.loads(j.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------------- bid comparison
def comparison(db, store, run_id: str, wp_key: str) -> dict:
    s = store.load(run_id)
    wp = next((w for w in s.plan.work_packages if w.key == wp_key), None)
    if wp is None:
        raise ValueError("That work package no longer exists.")
    items = s.document.item_map()
    rfqs = [dict(r) for r in db.q("SELECT * FROM rfqs WHERE run_id=? AND wp_key=? AND deleted=0 AND status NOT IN "
                                  "('cancelled','declined') ORDER BY supplier", (run_id, wp_key))]
    bidders = [r for r in rfqs if r["quote_amount"] is not None or load_quote(r)]
    quotes = {r["id"]: (load_quote(r) or {}).get("rates", {}) for r in bidders}
    rows = []
    for uid in wp.item_uids:
        it = items.get(uid)
        if it is None:
            continue
        cells = []
        for r in bidders:
            q = quotes[r["id"]].get(uid) or {}
            cells.append({"rate": q.get("rate"), "total": q.get("total"), "note": q.get("note", "")})
        totals = [c["total"] for c in cells if c["total"] is not None]
        low = min(totals) if totals else None
        for c in cells:
            c["lowest"] = low is not None and c["total"] == low and len(totals) > 1
        rows.append({"item": it, "cells": cells})
    sums = []
    for i, r in enumerate(bidders):
        t = sum(row["cells"][i]["total"] or 0 for row in rows)
        miss = sum(1 for row in rows if row["cells"][i]["rate"] is None)
        sums.append({"rfq": r, "items_total": round(t, 2), "missing": miss,
                     "amount": r["quote_amount"] if r["quote_amount"] is not None else round(t, 2)})
    if sums:
        best = min(x["amount"] for x in sums if x["amount"] is not None)
        lowest_amount = min(x["amount"] for x in sums)
        for x in sums:
            x["lowest"] = x["amount"] == best
            x["vs_lowest"] = round((x["amount"] / lowest_amount - 1) * 100, 1) if lowest_amount else None
    return {"wp": wp, "bidders": bidders, "rows": rows, "sums": sums, "project": s}


def comparison_xlsx(cmp: dict, path: Path, project_name: str) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    green = PatternFill("solid", fgColor="D9F2E3")
    grey = PatternFill("solid", fgColor="EEEEEE")
    wb = Workbook()
    ws = wb.active
    ws.title = "Comparison"
    wp = cmp["wp"]
    ws["A1"] = f"Bid comparison — {wp.wp_id} {wp.name_en}"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"{project_name} · {dt.date.today():%d %B %Y}"
    head = ["Item", "Description", "Unit", "Qty"]
    for r in cmp["bidders"]:
        head += [f"{r['supplier'] or r['number']} — rate", f"{r['supplier'] or r['number']} — total"]
    ws.append([])
    ws.append(head)
    hr = ws.max_row
    for c in ws[hr]:
        c.font = Font(bold=True)
        c.fill = grey
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for row in cmp["rows"]:
        it = row["item"]
        vals = [it.item_no, it.full_description[:1500], it.unit, _num(it.quantity) or it.quantity]
        for c in row["cells"]:
            vals += [c["rate"], c["total"]]
        ws.append(vals)
        r = ws.max_row
        ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")
        for i, c in enumerate(row["cells"]):
            col = 5 + 2 * i
            if c["lowest"]:
                ws.cell(r, col + 1).fill = green
            if c["rate"] is None:
                ws.cell(r, col).value = "—"
    ws.append([])
    tot = ["", "TOTAL (priced items)", "", ""]
    amt = ["", "QUOTED AMOUNT", "", ""]
    gap = ["", "vs. lowest", "", ""]
    miss = ["", "Items without a rate", "", ""]
    for s in cmp["sums"]:
        tot += ["", s["items_total"]]
        amt += ["", s["amount"]]
        gap += ["", f"+{s['vs_lowest']}%" if s.get("vs_lowest") else ("lowest" if s.get("lowest") else "")]
        miss += ["", s["missing"]]
    for line in (tot, amt, gap, miss):
        ws.append(line)
        ws.cell(ws.max_row, 2).font = Font(bold=True)
    amt_row = ws.max_row - 2
    for i, s in enumerate(cmp["sums"]):
        if s.get("lowest"):
            ws.cell(amt_row, 6 + 2 * i).fill = green
    widths = [10, 60, 8, 10] + [16, 18] * len(cmp["bidders"])
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(hr + 1, 5)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ---------------------------------------------------------------------- register export / import
COLUMNS = [("number", "RFQ no."), ("project", "Project"), ("wp_id", "WP"), ("package", "Work package"),
           ("supplier", "Supplier"), ("status", "Status"), ("due_date", "Due date"), ("issued_at", "Issued"),
           ("quote_amount", "Quoted amount"), ("assigned", "Assigned to"), ("updated", "Last change"), ("notes", "Notes")]


def export_xlsx(rows: list[dict], path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "RFQ register"
    ws.append([h for _, h in COLUMNS])
    for c in ws[1]:
        c.font = Font(bold=True)
    red = PatternFill("solid", fgColor="FDECEA")
    for r in rows:
        ws.append([STATUS_LABEL.get(r.get(k), r.get(k)) if k == "status" else r.get(k) for k, _ in COLUMNS])
        if is_overdue(r):
            for c in ws[ws.max_row]:
                c.fill = red
    for col, w in zip("ABCDEFGHIJKL", (16, 28, 8, 34, 26, 14, 12, 18, 16, 18, 18, 30)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
    return path


def export_pdf(rows: list[dict], path: Path, title: str) -> Path:
    """A landscape A4 table; Arabic text is shaped and laid out right-to-left by PyMuPDF's HTML engine."""
    import html

    import pymupdf as fitz
    cols = [("number", "RFQ no."), ("project", "Project"), ("package", "Work package"), ("supplier", "Supplier"),
            ("status", "Status"), ("due_date", "Due"), ("quote_amount", "Amount")]
    trs = []
    for r in rows:
        cells = []
        for k, _ in cols:
            v = r.get(k)
            if k == "status":
                v = STATUS_LABEL.get(v, v)
            if k == "quote_amount" and v is not None:
                v = f"{v:,.2f}"
            style = ' style="color:#b42318;font-weight:bold"' if k == "due_date" and is_overdue(r) else ""
            cells.append(f'<td dir="auto"{style}>{html.escape(str(v if v is not None else ""))}</td>')
        trs.append("<tr>" + "".join(cells) + "</tr>")
    body = (f"<h2>{html.escape(title)}</h2><p style='color:#666'>{dt.datetime.now():%d %B %Y %H:%M} · "
            f"{len(rows)} RFQ(s)</p><table><tr>" + "".join(f"<th>{h}</th>" for _, h in cols) + "</tr>"
            + "".join(trs) + "</table>")
    css = ("body{font-family:sans-serif;font-size:9pt} table{border-collapse:collapse;width:100%} "
           "th,td{border:1px solid #ccc;padding:3px 4px;text-align:left;vertical-align:top} th{background:#eee}")
    story = fitz.Story(html=body, user_css=css)
    rect = fitz.paper_rect("a4-l")
    where = rect + (30, 30, -30, -30)
    writer = fitz.DocumentWriter(str(path))
    more = True
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return path


IMPORT_HEADERS = {"number": ["rfq no.", "rfq no", "rfq number", "number", "رقم"], "project": ["project", "المشروع"],
                  "package": ["work package", "package", "scope", "الحزمة", "البند"],
                  "supplier": ["supplier", "vendor", "المورد"], "status": ["status", "الحالة"],
                  "due_date": ["due date", "due", "deadline", "submission deadline", "تاريخ التسليم"],
                  "quote_amount": ["quoted amount", "amount", "quote", "value", "القيمة"], "notes": ["notes", "ملاحظات"]}


def import_xlsx(db, path: Path, user: dict, projects_by_name: dict[str, str]) -> tuple[int, list[str]]:
    """Existing registers (any Excel with recognisable column titles) → RFQs. Returns (created, problems)."""
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    head_i, cols = None, {}
    for i, row in enumerate(rows[:15]):
        names = [str(c or "").strip().lower() for c in row]
        found = {k: names.index(a) for k, alts in IMPORT_HEADERS.items() for a in alts if a in names}
        if "supplier" in found and ("package" in found or "number" in found):
            head_i, cols = i, found
            break
    if head_i is None:
        raise ValueError("No header row with at least 'Supplier' and 'Work package' (or 'RFQ no.') was found.")
    rev = {v.lower(): k for k, v in STATUS_LABEL.items()}
    made, problems = 0, []
    for n, row in enumerate(rows[head_i + 1:], head_i + 2):
        get = lambda k: (row[cols[k]] if k in cols and cols[k] < len(row) else None)  # noqa: E731
        sup = str(get("supplier") or "").strip()
        pkg = str(get("package") or "").strip()
        if not sup and not pkg:
            continue
        number = str(get("number") or "").strip()
        if number and db.one("SELECT 1 FROM rfqs WHERE number=?", (number,)):
            problems.append(f"row {n}: {number} already in the register — skipped")
            continue
        status = rev.get(str(get("status") or "").strip().lower(), "issued")
        amount = _num(get("quote_amount"))
        run = projects_by_name.get(str(get("project") or "").strip().lower(), "")
        create(db, run, "", "", pkg, sup, user["id"], due_date=parse_date(get("due_date")), status=status,
               number=number, amount=amount, notes=str(get("notes") or "")[:2000])
        made += 1
    return made, problems
