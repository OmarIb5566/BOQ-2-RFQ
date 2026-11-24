"""Supplier list (Excel / CSV) -> supplier names per work package, for the RFQ letters.

The list is whatever the procurement team already keeps. Three layouts are understood:
  1. one row per supplier:   Supplier | Packages / Trade / Scope   ("Masonry, Plaster" -> two trades)
  2. a tick matrix:          Supplier | Masonry | Plaster | …       (x / ✓ / yes / 1 under the trades it does)
  3. one column per trade:   Masonry | Plaster | …                  (supplier names listed under each trade)
A row without a header is read as layout 1 (first column = supplier, second = trades).

Each trade text is then matched to THIS project's work packages:
  name     — the trade names the package ("Masonry" -> "Div 04: Masonry", or "WP-02")
  keywords — the same trade keywords the packaging uses ("block work" -> masonry)
  llm      — only for what is left; the answer is restricted to the package keys (cannot invent packages)
"""
from __future__ import annotations

import csv
import io
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..text_utils import norm, truncate

SUPPLIER_WORDS = ["supplier", "suppliers", "vendor", "vendors", "subcontractor", "sub contractor", "contractor",
                  "company", "company name", "name", "manufacturer", "مورد", "المورد", "الموردين", "الشركه", "شركه",
                  "اسم", "الاسم", "مقاول", "مقاول الباطن"]
TRADE_WORDS = ["package", "packages", "trade", "trades", "scope", "scope of work", "category", "categories", "work",
               "works", "speciality", "specialty", "activity", "activities", "division", "discipline", "service",
               "services", "products", "items", "البند", "البنود", "الاعمال", "اعمال", "التخصص", "النشاط", "الباقه",
               "الحزمه", "نوع العمل"]
TICK = re.compile(r"^\s*(x|✓|✔|☑|yes|y|1|true|نعم|●|•|\*)\s*$", re.I)
SPLIT = re.compile(r"\s*(?:[;,،\n|]|\s\+\s|\band\b|\s&\s|\sو\s)\s*", re.I)
MAX_ROWS = 5000


@dataclass
class SupplierList:
    pairs: list[tuple[str, str]] = field(default_factory=list)     # (supplier, trade text)
    layout: str = ""

    @property
    def suppliers(self) -> list[str]:
        return list(OrderedDict.fromkeys(s for s, _ in self.pairs))

    @property
    def trades(self) -> list[str]:
        return list(OrderedDict.fromkeys(t for _, t in self.pairs))


# ------------------------------------------------------------------------------------------ reading
def _rows(path: Path) -> list[list[list[str]]]:
    """The non-empty rows of every sheet, as text."""
    ext = path.suffix.lower()
    if ext == ".csv":
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1256", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t") if text.strip() else csv.excel
        return [[[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]]
    if ext == ".xls":
        import xlrd
        book = xlrd.open_workbook(str(path))
        return [[[_txt(sh.cell_value(r, c)) for c in range(sh.ncols)] for r in range(min(sh.nrows, MAX_ROWS))
                 if any(_txt(sh.cell_value(r, c)) for c in range(sh.ncols))] for sh in book.sheets()]
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            cells = [_txt(v) for v in r]
            if any(cells):
                rows.append(cells)
            if len(rows) >= MAX_ROWS:
                break
        out.append(rows)
    wb.close()
    return out


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", " ", str(v)).strip()


def _is(cell: str, words: list[str]) -> bool:
    n = norm(cell).strip(" :.#")
    # exact, or a header that starts with the word ("Supplier name", "Trade / scope"); NOT one that ends with it:
    # "Painting Works" is a trade name (a column-per-trade list), not a "works" header
    return bool(n) and (n in words or any(n.startswith(w + " ") for w in words if len(w) > 3))


def _header(rows: list[list[str]]) -> tuple[int, Optional[int], Optional[int]]:
    """(header row index, supplier column, trade column) in the first rows; -1 when there is no header."""
    for i, r in enumerate(rows[:10]):
        sup = next((c for c, v in enumerate(r) if _is(v, SUPPLIER_WORDS)), None)
        trd = next((c for c, v in enumerate(r) if c != sup and _is(v, TRADE_WORDS)), None)
        if sup is not None or trd is not None:
            return i, sup, trd
    return -1, None, None


def read_supplier_list(path: Path) -> SupplierList:
    out = SupplierList()
    for rows in _rows(Path(path)):
        if not rows:
            continue
        h, sup, trd = _header(rows)
        body = rows[h + 1:]
        width = max(len(r) for r in rows)
        cell = lambda r, c: r[c] if c is not None and c < len(r) else ""   # noqa: E731
        if h >= 0 and sup is not None and trd is not None:                       # 1. supplier | trades
            last = ""
            for r in body:
                name, trades = cell(r, sup), cell(r, trd) or last     # a blank trade continues the group above
                last = trades
                for t in SPLIT.split(trades):
                    if name and t:
                        out.pairs.append((name, t))
            out.layout = out.layout or "supplier and trade columns"
        elif h >= 0 and sup is not None:                                          # 2. tick matrix
            heads = rows[h]
            for r in body:
                name = cell(r, sup)
                for c in range(width):
                    if c != sup and heads[c:c + 1] and heads[c] and TICK.match(cell(r, c)):
                        out.pairs.append((name, heads[c]))
            out.layout = out.layout or "tick matrix"
        elif h >= 0 and trd is not None:                                          # trade | name | name …
            for r in body:
                for c in range(width):
                    if c != trd and cell(r, c) and cell(r, trd):
                        out.pairs.append((cell(r, c), cell(r, trd)))
            out.layout = out.layout or "trade column with supplier names"
        elif sum(len([v for v in r if v]) > 2 for r in rows[:8]) >= 2:           # 3. a column per trade
            heads = rows[0]
            for r in rows[1:]:
                for c in range(width):
                    v = cell(r, c)
                    if v and heads[c:c + 1] and heads[c] and not TICK.match(v):
                        out.pairs.append((v, heads[c]))
            out.layout = out.layout or "a column per trade"
        else:                                                                      # no header: name | trades
            for r in rows:
                vals = [v for v in r if v]
                if len(vals) >= 2:
                    for t in SPLIT.split(vals[1]):
                        if t:
                            out.pairs.append((vals[0], t))
            out.layout = out.layout or "two columns without headers"
    # tidy: drop numbers-only "names" and duplicates
    seen, pairs = set(), []
    for s, t in out.pairs:
        s, t = s.strip(" -–:"), t.strip(" -–:")
        if not s or not t or re.fullmatch(r"[\d\s.,/-]+", s):
            continue
        k = (norm(s), norm(t))
        if k not in seen:
            seen.add(k)
            pairs.append((s, t))
    out.pairs = pairs
    return out


# ----------------------------------------------------------------------------------------- matching
PROMPT_VERSION = "s1"


def match_trades(session, rules, trades: list[str], llm=None, log: Callable[[str], None] = print
                 ) -> dict[str, list[tuple[str, str]]]:
    """trade text -> [(work package key, how)] for this project's packages."""
    from ..docs.common import load_doc_rules
    from ..docs.match import Matcher, profiles

    prs = profiles(session, rules)
    m = Matcher(rules, load_doc_rules(str(Path(__file__).resolve().parents[1] / "knowledge" / "document_rules.json")))
    by_id = {p.wp.wp_id.upper(): p for p in prs}
    out: dict[str, list[tuple[str, str]]] = {}
    left = []
    for t in trades:
        n = norm(t)
        hits: list[tuple[str, str]] = []
        wid = re.search(r"\bwp[-\s]?(\d{1,3})\b", n)
        if wid and f"WP-{int(wid.group(1)):02d}" in by_id:
            hits = [(by_id[f"WP-{int(wid.group(1)):02d}"].wp.key, "package number")]
        if not hits and len(n) >= 4:
            for p in prs:
                names = [norm(p.wp.name_en), norm(p.wp.name_ar or ""), norm(re.sub(r"^div \d+\s*:?\s*", "", p.wp.name_en, flags=re.I))]
                if any(x and (n == x or (len(n) >= 5 and n in x.split(" — ")[0]) or (len(x) >= 5 and x in n)) for x in names):
                    hits.append((p.wp.key, "package name"))
        if not hits:
            hits = list(OrderedDict.fromkeys((p.wp.key, "trade keywords") for p, _ in m._line_targets(t, prs)))
        if hits:
            out[t] = hits
        else:
            left.append(t)
    if left and llm is not None and prs:
        out.update(_ask(left, prs, llm, log))
    return out


def _ask(trades: list[str], prs, llm, log) -> dict[str, list[tuple[str, str]]]:
    keys = [p.wp.key for p in prs]
    menu = "\n".join(f"- {p.wp.key}: {p.wp.name_en}" + (f" (Div {p.division})" if p.division else "")
                     + (f" e.g. {p.sample}" if p.sample else "") for p in prs)
    schema = {"type": "object", "properties": {"matches": {"type": "array", "items": {
        "type": "object", "properties": {"trade": {"type": "string"},
                                         "packages": {"type": "array", "items": {"type": "string", "enum": keys + ["NONE"]},
                                                      "maxItems": 6}},
        "required": ["trade", "packages"]}}}, "required": ["matches"]}
    system = ("You are a senior construction procurement engineer. A supplier list says which trades each supplier "
              "works in. For each trade, pick the work packages of this project a supplier of that trade can price. "
              "Answer with package keys only; NONE when no package fits. Never guess a loosely related package.")
    out: dict[str, list[tuple[str, str]]] = {}
    for i in range(0, len(trades), 40):
        chunk = trades[i:i + 40]
        user = "Trades:\n" + "\n".join(f"- {truncate(t, 120)}" for t in chunk) + f"\n\nWork packages:\n{menu}"
        try:
            ans = llm.chat_json(system, user, schema, op="supplier-match", max_tokens=1500)
        except Exception as e:  # noqa: BLE001
            log(f"Supplier matching question failed: {e}")
            continue
        wanted = {norm(truncate(t, 120)): t for t in chunk}
        for row in ans.get("matches", []):
            t = wanted.get(norm(row.get("trade", "")))
            ks = [k for k in row.get("packages", []) if k in keys]
            if t and ks:
                out[t] = [(k, "AI") for k in ks]
    return out


def assign(lst: SupplierList, matches: dict[str, list[tuple[str, str]]]) -> "OrderedDict[str, list[str]]":
    """work package key -> supplier names (list order)."""
    per: OrderedDict[str, list[str]] = OrderedDict()
    for s, t in lst.pairs:
        for k, _ in matches.get(t, []):
            if s not in per.setdefault(k, []):
                per[k].append(s)
    return per
