# BOQ2RFQ — How the system works

BOQ2RFQ reads a Bill of Quantities (Excel or PDF, Arabic or English) and cuts it into **work packages (WPs)** following the company packaging flowchart. It asks the user to approve or deny every flowchart special case (the ovals). It then exports one PDF snapshot per work package, showing the original BOQ rows with **all columns**, plus a JSON hand-off file for the future RFQ-drafting module.

It runs fully locally. The reasoning model is **Gemma (`gemma4:e4b`) through Ollama**, prompted to act as a senior construction manager / QS who is fluent in Arabic construction terminology.

---

## 1. Running it

| How | Command |
|---|---|
| Web UI (Streamlit) | double-click `run_app.bat` (it creates `.venv` on the first run), or `.venv\Scripts\streamlit run BOQ2RFQ.py` |
| Command line | `.venv\Scripts\python cli.py "BOQ.xlsx"` (asks Approve/Deny for each special case) |
| | `… --approve-all` / `--deny-all` (non-interactive), `--no-llm` (keyword-only), `--renderer excel\|html`, `--model <name>` |

**Requirements**
- **Ollama** running with `gemma4:e4b` pulled (`ollama pull gemma4:e4b`). Without it the agent still runs in a less accurate *keyword-only* mode.
- **Microsoft Excel** (optional, Windows). Used to convert `.xls`/`.xlsb` and to print snapshots that look exactly like the original. Without Excel, `.xls` is read values-only (xlrd) and snapshots use a built-in HTML renderer.
- **Tesseract OCR** with Arabic data (optional). Used only for scanned PDFs that have no text layer.

**Settings** (`boq_agent/config.py`, several can be set as environment variables):

| Setting | Default | Env var |
|---|---|---|
| Ollama URL | `http://localhost:11434` | `OLLAMA_URL` |
| Model | `gemma4:e4b` | `BOQ_LLM_MODEL` |
| Temperature / context | `0.0` / 16 384 tokens | — |
| Items per LLM call | 10 | — (UI slider) |
| Review threshold | 0.70 confidence | — |
| Workspace folder | `./workspace` | `BOQ_WORKSPACE` |
| Excel renderer | `auto` (Excel if available, else HTML) | `BOQ_EXCEL_RENDERER` |
| Tesseract folder | `C:\Program Files\Tesseract-OCR` | `TESSERACT_DIR` |

---

## 2. The pipeline at a glance

```
 Upload BOQ (.xlsx/.xls/.xlsm/.xlsb/.pdf)
        │
        ▼
 ① INGEST ─────────── Excel reader / PDF reader → rows with columns (item no, description, unit, qty, rate, total)
        │
        ▼
 ② STRUCTURE ──────── row roles → sections (أبواب / Divisions) → priced items with parent + heading context
        │
        ▼
 ③ DIVISIONS ──────── each section → CSI division (explicit "Division 03" → LLM → keywords)
        │
        ▼
 ④ CLASSIFY ITEMS ─── keyword pre-screen → LLM batches → LLM review of doubtful items → context rules
        │
        ▼
 ⑤ PLAN ───────────── deterministic flowchart engine → work packages + special-case proposals
        │
        ▼
 ⑥ USER REVIEW ────── Approve / Deny each special case; optionally correct divisions or item categories
        │
        ▼
 ⑦ EXPORT ─────────── one PDF cut-out per WP + work_packages.json (RFQ hand-off) + ZIP
```

The split of responsibilities is deliberate:

- **The LLM only answers narrow, schema-constrained questions.** For example: "which division is this section?" or "which of these categories is this item?". Every answer must be valid JSON whose values come from a fixed list (an enum), so the model cannot invent packages.
- **Packaging itself is deterministic.** The flowchart engine (`planning/planner.py`) applies the rules in `flowchart_rules.json` the same way every time. The model never decides how packages are grouped.
- **The user has the final say** on every special case, and can correct any division or item category in the UI.

---

## 3. The flowchart as data — `boq_agent/knowledge/flowchart_rules.json`

The company flowchart is stored as a machine-readable rules file. It drives three things:
1. the planner's behaviour;
2. the menus of allowed answers given to the LLM;
3. the agent's **system prompt**, which is generated from this file. You can see the generated prompt in the UI tab *Flowchart rules (agent instructions)*.

To change packaging behaviour, edit this file. Normally no code change is needed.

### 3.1 Rectangles — how each division is packaged (`divisions[*].mode`)

| Mode | Meaning | Divisions |
|---|---|---|
| `as_is` | The division is one work package, exactly as written in the BOQ | 01–04, 06, 07, 11–14, 21–23, 26–28, 31–33 |
| `split` | The division is split into the listed work-package categories | 05 Metals, 08 Openings, 09 Finishes |
| `per_item` | Every BOQ description becomes its own work package (subject to approval, see R6) | 10 Specialties |

Each division also has Arabic and English **keywords** (e.g. `خرسانه`, `concrete`) for the keyword fallback.

### 3.2 Categories — the work packages inside split divisions (`categories`)

| Division | Normal categories (rectangles) |
|---|---|
| 05 Metals | Handrails — Steel/Saj · Handrails — Wood · Handrails — Aluminium · Glass Handrails · Access Hatches & Covers · Ship Ladders |
| 08 Openings | Wooden Doors Only · Metal Doors Only · Aluminium Doors & Windows |
| 09 Finishes | Plaster · Epoxy Paint · Acrylic Paint · Internal Marble/Granite/Stone · Wooden Floors/Ceilings · Suspended Ceiling/Gypsum Board · Ceramic/Porcelain (incl. skirting) |

Each category has:
- a **definition**, which is shown to the LLM;
- **keywords** for the pre-screen;
- optionally **`requires_any`**: words that must also appear. For example, *roof tiles* needs a tile word **and** a roof word (`سطح`, `roof`, `terrace`).

Items of a split division that match no category go to a **"Div NN — other items"** package (category `MISC`).

### 3.3 Ovals — special cases that need the user's approval (`special_rules`)

Special categories (`"special": true`) are looked for in **every** division, whatever its mode. Each rule says what happens on **Approve** and on **Deny**:

| Rule | Detected items | Approve | Deny |
|---|---|---|---|
| **R1** External marble | Marble/granite/stone used outside (facades, copings, external sills) | Bundled into the **Div 04 Masonry** WP. If the BOQ has no masonry package, it is issued alone and a note explains why. | Separate **External Marble / Stone Cladding** WP |
| **R2** Roof tiles | Tiles/pavers forming the roof finish (incl. بلاطات أسمنتية لزوم السطح, قرميد) | Separate **Roof Tiles** WP | Stays in its own section's package (e.g. Div 07 Insulation) |
| **R3** Expansion joints | Expansion / movement / settlement joints and joint covers | Separate **Expansion Joints** WP | Stays in its own section's package |
| **R4** Wooden louvers | Louvers of real wood | Grouped with **Wooden Doors Only** (if present) | Separate **Wooden Louvers** WP |
| **R5** Metal louvers | Aluminium (even with wood texture), steel or sheet-metal louvers | **Metal Louvers** WP under Div 05 Metals | **Louvers** WP under Div 08 Openings |
| **R6** Div 10 per item | Div 10 Specialties with ≥ 2 BOQ descriptions | One WP **per BOQ description** (sub-items such as 21.1, 21.2 stay with their parent 21) | Div 10 kept as **one** WP |

Each rule carries bilingual title and message templates. These become the notification the user sees, e.g. *"1 external marble/stone item(s) from Division 04 : Masonry will be GROUPED with the Div 4 Masonry work package … Approve to bundle them with masonry, or Deny …"* together with the Arabic text.

### 3.4 Context rules — fixed overrides from the heading an item sits under (`context_rules`)

| Rule | Condition | Effect |
|---|---|---|
| **C1** External-cladding context | The item sits under a heading (or a short, title-like parent such as `AR 20.0 التكسية الخارجية بالحجر`) containing an **external** word (خارجي، واجهات، external, facade…) **and** a **cladding** word (تكسية، تجاليد، cladding…; common misspellings such as تكيسة included) | The item is classed as **External Marble / Stone Cladding**, and then follows R1 like any other external-marble item |

Notes on C1:
- **What counts as context:** only the headings and a short parent. The item's own text is never used.
- **Non-stone headings:** if the heading names a non-stone cladding (aluminium/ACP, glass, GRC, HPL, wood, metal), only stone items (marble/granite/stone, or items the LLM called internal stone) are flipped to external.
- **Other ovals win:** items the LLM placed in another special case (roof tiles, expansion joints, louvers) keep that oval.
- **Why it exists:** a granite skirting under "التكسية الخارجية بالحجر" is external stone, even though "skirting" sounds internal.

---

## 4. Step by step

### ① Ingest

**Excel** (`ingest/excel_reader.py`, `excel_com.py`)
1. **Copy and convert.** The upload is copied into the run folder. `.xls`/`.xlsb` are converted to `.xlsx` by Excel; if Excel is unavailable, xlrd reads `.xls` values only.
2. **Sheets read.** Only **visible** sheets are read. Summary/collection sheets (`Bill Summ.`, `Summary`, `ملخص`, `اجمالي`, `خلاصة`…) are skipped, because they repeat division totals.
3. **What counts as content.** The sheet's **print area** limits the columns. **Hidden rows and columns** are left out of the analysis and out of the snapshots.
4. **Header detection.** The first 80 rows are scanned for a row that matches Arabic/English header words, e.g. `رقم البند / بيان الأعمال / الوحدة / الكمية / الفئة / الجملة` or `Item / Description / Unit / Qty / Rate / Amount`.
   - A header needs 3 fields, or a description plus unit/quantity.
   - Multi-row headers are detected, and the sheet's *print title rows* are used as the header band when they contain the header.
   - **If no header is found**, the LLM is shown the top rows and asked for the column letters. Its answer is rejected unless unit/quantity sit in a different column from the description. That stops summary or notes sheets being mistaken for BOQ tables.
5. **Building rows.** Every remaining row becomes a `Row` with:
   - its fields (item no, unit, qty, rate, total);
   - its descriptive text, made from all non-mapped text columns;
   - its source reference (`sheet!row`, including vertical merges).

**PDF** (`ingest/pdf_reader.py`)
1. **Reading words.** Words are rebuilt from **individual character positions**. PDFs often store text like `2عددAR` as one run even though the glyphs sit in different columns. The reader splits on spaces and on large horizontal gaps.
2. **Scanned pages.** Pages with almost no text layer are OCR'd with Tesseract (`ara+eng`, 300 dpi).
3. **Reversed Arabic.** If the Arabic is stored in visual (reversed) order, it is detected by checking common words and flipped back.
4. **Lines and columns.** Words are clustered into visual lines. A header is found on each page, and columns are mapped using the header labels plus the table's **vertical ruling lines**. Wrapped description lines are merged.
5. **Position kept.** Every row keeps its page and bounding box, so it can be cropped later.

### ② Structure (`ingest/structure.py`, the same for Excel and PDF)

Each row gets a **role**:

| Role | Detected by |
|---|---|
| `section` | `الباب …`, `Division 03`, `Bill No. 2` → starts a new section (باب) |
| `heading` | Group titles. Level 1: `Section 03 30 00`, `قسم`, `أولاً/ثانياً…`. Level 2: other short titles such as *Plain Concrete* |
| `parent` | A numbered row with no unit/quantity (e.g. `AR 9.0`, `3`) holding the main description. Continuation lines are appended to it |
| `leaf` | A **priced line**: it has a unit, a lump-sum token (`Rate only`, `مقطوعية`, `LS`), or a non-zero quantity |
| `subtotal` | `Total / اجمالي / مجموع / Carried to Summary / ما قبله …`. Page furniture such as `صفحة` or `Issue date` is recognised and ignored |

- **Items.** Every leaf becomes an **Item** carrying its own text, the **parent description** (when its number is a child of the parent's, e.g. 3.2 under 3, or a lettered sub-item), the stack of headings above it, unit, quantity, rate and total.
- **Orphan rows.** Descriptive rows that belong to no item, such as the wrapped tail of a description printed under its quantity line, are attached to the preceding item, so no text is lost from the snapshots.

### ③ Divisions (`analysis/classifier.py → assign_divisions`)

Each section gets a CSI MasterFormat division code:
1. **Explicit:** `Division 03`, `Div 9`, `Divi 08` in the section title or sheet name → confidence 1.0.
2. **LLM:** the remaining sections are sent in groups of 6, each with its title, sheet name, sub-headings and up to 8 sample items. The prompt includes Egyptian naming hints (أعمال الحفر والردم→31, المباني→04, النجارة→08 …). `00` = not a work section.
3. **Keywords:** used if the LLM is unavailable or unsure.

The section's division decides its packaging mode (`as_is`, `split` or `per_item`).

### ④ Item classification (`analysis/classifier.py → classify_items`)

1. **Allowed answers per section.**
   - *split* sections: all normal categories + all special categories + `MISC`.
   - *as_is* sections: the special categories + `NONE` (= "normal item, stays with its section").
   - *per_item* sections: nothing to classify.
2. **Keyword pre-screen.** The item text (parent + item + headings) is normalised (Arabic letter forms, diacritics, digits, case) and matched against category keywords.
   - **Ancillary scope is ignored.** Anything after *"the item includes / including / شاملاً / ويشمل / بما في ذلك …"* is dropped before matching. So "reinforced concrete … the item includes formwork, movement joints, joint filler" is **not** flagged as an expansion joint.
   - **Cost saver.** In *as_is* sections, items with no keyword hit skip the LLM and stay with their section.
3. **LLM classification.** Items are sent in batches of 10, with the section, the division and the category menu (with definitions). The model must return one category id per item from the allowed list, plus a confidence and a short reason.
4. **Reflection / review.** An item gets a second look, one item at a time with Gemma's *thinking* enabled, when:
   - the batch answer was missing; or
   - confidence was below 0.70; or
   - the LLM disagreed with the keyword hint.

   The review prompt tells the model to judge by material, location (internal/external) and executing trade, and to ignore included ancillary scope.
5. **Context rules.** C1 is applied last and overrides the LLM.
6. **No-LLM fallback.** Each item gets its keyword hint, or `MISC`, at confidence 0.4.

Every item records `category`, `confidence`, `method` (`llm`, `llm-review`, `keyword`, `section`, `context-rule`, `manual`) and `reason`, all visible in the UI.

### ⑤ Planning — the flowchart engine (`planning/planner.py`)

1. **Default package ("Deny" world).** Every item first gets the package it would land in if no special case were applied:
   - *split* division → its category, or `Div NN — other items`;
   - *as_is* division → `Div NN`;
   - *per_item* division → one key per BOQ description.
2. **Special cases.** Items in a special category are resolved with their rule's Approve and Deny targets.
   - When the two targets differ, a **Proposal** is created, one per rule and source/target pair, listing the affected items and the bilingual message.
   - `merge_into` targets are used only if that package actually exists in this BOQ (e.g. marble merges into masonry only when there is a masonry package). Otherwise the item is issued standalone and a plan note explains why.
3. **Div 10.** A per-item proposal is created when Div 10 has at least 2 descriptions.
4. **Undecided proposals.** While pending, they are **previewed as approved**. Exporting requires every one to be approved or denied (the CLI's `--approve-all`/`--deny-all` decide them in bulk).
5. **Numbering and notes.** Work packages are numbered `WP-01, WP-02 …` in BOQ order (first appearance). Each WP records the special rules that shaped it. WPs with LLM answers below 0.6 confidence get a "please review" note.

Re-planning (after any decision or correction) keeps all earlier decisions.

### ⑥ User review (Streamlit UI)

- **Special cases panel:** one card per proposal, in English and Arabic, with the affected items (number, section, description, unit, qty, the model's reason) and **Approve / Deny** buttons. Each button shows what it leads to (e.g. *Approve → Div 04: Masonry · Deny → External Marble / Stone Cladding*).
- **Work-package table:** WP id, English/Arabic names, division, item count, item numbers, special rules, notes.
- **Review & correct:**
  - change a section's division (that section is re-classified);
  - change any item's category in an editable table (set to `manual`, confidence 1.0).
- **Agent log:** every step, LLM call count and time.
- **Flowchart rules tab:** the exact system prompt generated from the rules file.

### ⑦ Export — the cut-outs (`render/snapshots.py`, `planning/planner.py → snapshot_rows`)

**Rows in each WP snapshot**
- the table **header rows** (all columns);
- the **section title** row(s);
- the **headings and parent description** above each item;
- the item's own rows;
- the section **subtotal** row, but only when the whole section is in this WP.

**Excel source (Microsoft Excel available)**
1. Excel opens the converted workbook read-only and hides every row not in the WP. Rows hidden in the original stay hidden.
2. The header rows become print titles, repeated on every page.
3. The sheet is printed fit to one page wide, to PDF.
4. The workbook is restored and closed without saving. The original file is never modified.

The result keeps the original look: logos, fonts, colours, borders, merged cells, page header/footer.

**Excel source without Excel:** an HTML re-rendering of the same rows and columns, keeping merges, bold, fills, alignment and RTL direction. Pages are A4, landscape when there are more than 5 columns.

**PDF source:** the original page regions (header band + item rows, full table width) are cropped at **vector quality** and stacked onto new pages. The header band is repeated on each output page.

**Every PDF** is stamped in the top margin: `WP-03 | Glass Handrails | درابزين زجاج — page 1/1`.

**Output folder** (`workspace/<run_id>/`):

```
source.xlsx / source.pdf          the copy that was analysed (converted if needed)
session.json                      full state: rows, sections, items + classifications, plan, decisions, log
work_packages/WP-01 ….pdf         one cut-out per work package
work_packages/work_packages.json  RFQ hand-off (see §6)
<BOQ name> - work packages.zip    all PDFs + the JSON (the UI's download button)
```

---

## 5. The agent's prompts (`llm/prompts.py`)

- **Persona.** Senior Construction Manager & QS (20+ years, Egyptian/Gulf projects) with an Arabic↔English glossary of BOQ terms, e.g. باب = section; بند = item; بياض/محارة = plaster; حلق/ضلفة = door frame/leaf; لوفر/كاسرات = louvers; وزرة = skirting; قرميد = roof tiles.
- **Job and principles.** Procurement packaging for RFQs. Judge by **what is supplied/installed, its main material and location**, not by incidental words: "aluminium louvers with wood texture" are metal louvers, and scope listed after "includes / شاملاً" is ancillary.
- **Flowchart.** Rendered from `flowchart_rules.json` (rectangles, splits, ovals, context rules).
- **Output format.** JSON only, constrained by a schema whose category values are the allowed ids.

Task prompts: `DIVISION_TASK`, `ITEM_TASK`, `REVIEW_TASK` (single item, with thinking) and `HEADER_TASK` (fallback header detection).

---

## 6. Hand-off to the future RFQ module (`rfq/interface.py`)

Every export writes `work_packages/work_packages.json`, a list of `RFQPackage`:

```json
{
  "project_file": "BOQ All QTY.xlsx",
  "run_id": "20261014-153157-BOQ_All_QTY",
  "wp_id": "WP-02",
  "wp_key": "DIV04",                       // stable flowchart key, use it for vendor mapping
  "name_en": "Div 04: Masonry", "name_ar": "أعمال المباني",
  "division": "04", "division_name": "Masonry",
  "snapshot_pdf": ".../WP-02 Div 04 Masonry.pdf",
  "special_rules": ["R1_EXTERNAL_MARBLE"],  // ovals that shaped this package
  "lines": [{"item_no": "1.1", "description": "<parent> — <item>", "unit": "m2", "quantity": "3,095", "source": "Div04 !5"}]
}
```

The RFQ module should depend only on this contract, never on the BOQ parsing internals. Planned flow (stubs `RFQDraft`, `RFQDrafter`, `LLMRFQDrafter` already exist):
1. Load the packages.
2. Pick vendors per `wp_key`/`division` from the vendor register.
3. Draft bilingual RFQ letters with the same local LLM, with the snapshot PDF attached.
4. User review.
5. Export.

---

## 7. Customising the behaviour

| You want to… | Edit |
|---|---|
| Catch a new Arabic/English term for an existing package | add it to that category's `keywords` (and `requires_any` if it must co-occur with a location word) |
| Change a definition the LLM sees | the category's `definition` |
| Add a new work package to a split division | add a category with that `division` |
| Change a division from "as written" to "split" (or vice-versa) | the division's `mode` |
| Add a new oval / exception | add a category with `"special": true` and a `special_rules` entry with `approve`/`deny` (`merge_into` with `only_if_target_exists`, `standalone`, `section_default`, `per_item`, `as_is`) plus bilingual messages (`{n}`, `{sources}`, `{from_wp}` placeholders) |
| Add a fixed "heading decides the package" rule | add an entry to `context_rules` |

The system prompt, the LLM answer menus, the planner and the UI all pick up the changes on the next run.

---

## 8. Code map

| File | Responsibility |
|---|---|
| `BOQ2RFQ.py` | Streamlit UI |
| `cli.py` | Command-line runner |
| `boq_agent/agent.py` | Orchestrator: `analyze()` → `decide()` / corrections → `export()`, session save/load |
| `boq_agent/config.py` | Settings |
| `boq_agent/models.py` | Shared data model (`Row`, `Item`, `Section`, `WorkPackage`, `Proposal`, `Plan`, `Session`), all JSON-serialisable |
| `boq_agent/ingest/excel_reader.py` | Excel → rows (sheets, print area, hidden rows/cols, header detection, summary-sheet skip) |
| `boq_agent/ingest/pdf_reader.py` | PDF → rows (character-level words, OCR, RTL fix, column mapping) |
| `boq_agent/ingest/structure.py` | Row roles, sections, parent/child items |
| `boq_agent/excel_com.py` | Excel automation (conversion, rendering) and xlrd fallback |
| `boq_agent/knowledge/flowchart_rules.json` | **The flowchart as data** |
| `boq_agent/knowledge/rules.py` | Loads the rules; keyword pre-screen; context rules; renders the flowchart prompt |
| `boq_agent/llm/client.py` | Ollama client with JSON-schema output and retries |
| `boq_agent/llm/prompts.py` | Persona, task prompts, answer schemas |
| `boq_agent/analysis/classifier.py` | Division assignment and item classification (batch → review → context rules) |
| `boq_agent/planning/planner.py` | Flowchart engine and snapshot row selection |
| `boq_agent/render/snapshots.py` | PDF cut-outs (Excel COM / HTML / PDF crop) |
| `boq_agent/rfq/interface.py` | RFQ hand-off contract and stubs for the next module |
| `boq_agent/text_utils.py` | Arabic/English normalisation, keyword matching, inclusion-clause stripping, formatting |

---

## 9. Known limits and judgement calls

- **The LLM can vary at the margins.** Temperature is 0, but borderline items (e.g. stone skirting, cement-board copings) can still change between model versions. Check the *By / Reason* columns in *Review & correct*, and fix any item there. Manual corrections always win.
- **Not every BOQ layout is covered.** Header and structure detection handle the usual Egyptian/Gulf layouts. A BOQ with no recognisable header on a sheet is reported as a warning and that sheet is skipped.
- **Scanned PDFs depend on OCR quality.** Low-resolution scans may mis-map columns.
- **Without Microsoft Excel, snapshots lose some fidelity.** The HTML renderer reproduces structure and basic styling, but not logos or page headers/footers.
