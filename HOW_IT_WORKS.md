# BOQ2RFQ — How the system works

BOQ2RFQ reads a Bill of Quantities (Excel or PDF, Arabic or English) and cuts it into **work packages (WPs)** following the company packaging flowchart. It asks the user to approve or deny every flowchart special case (the ovals). It then exports one PDF snapshot per work package, showing the original BOQ rows with **all columns**, plus a JSON hand-off file. Finally it drafts the **RFQ letters** for those packages from the company Word template (§6).

It runs fully locally. The reasoning model is **Gemma (`gemma4:e4b`) through Ollama**, prompted to act as a senior construction manager / QS who is fluent in Arabic construction terminology.

---

## 1. Running it

| How | Command |
|---|---|
| Web UI (Streamlit) | double-click `run_app.bat` (it creates `.venv` on the first run), or `.venv\Scripts\streamlit run BOQ2RFQ.py` |
| Command line | `.venv\Scripts\python cli.py "BOQ.xlsx"` (asks Approve/Deny for each special case) |
| | several BOQs of one project: `cli.py Structural.xlsx Architectural.pdf Landscape.xlsx`, or a zip: `cli.py "Project BOQs.zip"` |
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
        │
        ▼
 ⑧ RFQ DRAFTING ───── company Word template → fields auto-filled only when certain → user approves → .docx per supplier
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
3. the agent's **system prompt**, which is generated from this file (`rules.render_prompt()`).

To change packaging behaviour, edit this file. Normally no code change is needed.

### 3.1 Rectangles — how each division is packaged (`divisions[*].mode`)

| Mode | Meaning | Divisions |
|---|---|---|
| `as_is` | The division is one work package, exactly as written in the BOQ | 01–04, 06, 07, 11–14, 21–23, 26–28, 31, 33 |
| `split` | The division is split into the listed work-package categories | 05 Metals, 08 Openings, 09 Finishes, 32 Landscape |
| `per_item` | Every BOQ description becomes its own work package (subject to approval, see R6) | 10 Specialties |

Each division also has Arabic and English **keywords** (e.g. `خرسانه`, `concrete`) for the keyword fallback.

### 3.2 Categories — the work packages inside split divisions (`categories`)

| Division | Normal categories (rectangles) |
|---|---|
| 05 Metals | Handrails — Steel/Saj · Handrails — Wood · Handrails — Aluminium · Glass Handrails · Access Hatches & Covers · Ship Ladders |
| 08 Openings | Wooden Doors Only · Metal Doors Only · Aluminium Doors & Windows |
| 09 Finishes | Plaster · Epoxy Paint · Acrylic Paint · Internal Marble/Granite/Stone · Wooden Floors/Ceilings · Suspended Ceiling/Gypsum Board · Ceramic/Porcelain (incl. skirting) |
| 32 Landscape — **Hardscape** | Interlock · Dakshum (حجر دقشوم) · Crushed Gravel · Cast-in-Place Concrete Paving · Fountains (everything built for a fountain: its concrete, waterproofing, mosaic, stone, copings, nozzles; not its excavation) · Tartan Sports Flooring · **Hardscape** (every other landscape item: granite/stone paving, kerbs, benches, play equipment, car stoppers, barriers …) |
| 32 Landscape — **Softscape** | Mobilization Furniture (أعمال الفرش: caravans / temporary site offices) · Planting (أعمال الزراعة) |

**Landscape** packages are only used inside landscape sections, and building packages are never offered there. The external-marble rule (R1) does not apply in landscape: it is for the building's cladding only, so granite paving stays in landscape. A landscape section is recognised by:
- its title or sheet name (لاندسكيب, landscape, hardscape, softscape, تنسيق الموقع …); or
- at least two landscape section codes in its headings (old MasterFormat `قسم 027xx–029xx`, or `32 xx xx`).

The settings live in `landscape_detection`. "الموقع العام" on its own is *not* a signal yet.

Each category has:
- a **definition**, which is shown to the LLM;
- **keywords** for the pre-screen;
- optionally **`requires_any`**: words that must also appear. For example, *roof tiles* needs a tile word **and** a roof word (`سطح`, `roof`, `terrace`).

Items of a split division that match no category go to a **"Div NN — other items"** package (category `MISC`). A division can rename it with `misc`; Landscape calls it **Hardscape**.

### 3.3 Ovals — special cases that need the user's approval (`special_rules`)

Special categories (`"special": true`) are looked for in every division, whatever its mode, unless the division limits them. Landscape only knows the Earthwork oval (`only_specials`), and the earthwork division itself cannot pull its own items (`exclude_specials`). Each rule says what happens on **Approve** and on **Deny**:

| Rule | Detected items | Approve | Deny |
|---|---|---|---|
| **R1** External marble | Marble/granite/stone used outside (facades, copings, external sills) | Bundled into the **Div 04 Masonry** WP. If the BOQ has no masonry package, it is issued alone and a note explains why. | Separate **External Marble / Stone Cladding** WP |
| **R2** Roof tiles | Tiles/pavers forming the roof finish (incl. بلاطات أسمنتية لزوم السطح, قرميد) | Separate **Roof Tiles** WP | Stays in its own section's package (e.g. Div 07 Insulation) |
| **R3** Expansion joints | Expansion / movement / settlement joints and joint covers | Separate **Expansion Joints** WP | Stays in its own section's package |
| **R4** Wooden louvers | Louvers of real wood | Grouped with **Wooden Doors Only** (if present) | Separate **Wooden Louvers** WP |
| **R5** Metal louvers | Aluminium (even with wood texture), steel or sheet-metal louvers | **Metal Louvers** WP under Div 05 Metals | **Louvers** WP under Div 08 Openings |
| **R6** Div 10 per item | Div 10 Specialties with ≥ 2 BOQ descriptions | One WP **per BOQ description** (sub-items such as 21.1, 21.2 stay with their parent 21, and size/colour variants stay together, see 3.5) | Div 10 kept as **one** WP |
| **XBOQ** Same package in several BOQs (multi-BOQ projects only) | A package whose own items come from 2+ uploaded BOQs (e.g. *Div 03: Concrete* in the Structural **and** the Landscape BOQ). Items moved in by another oval (e.g. earthwork pulled in by R7) do not count, because that oval was already approved | **One** work package and **one** RFQ for all BOQs | One package per BOQ, e.g. *Div 03: Concrete — Structural* and *Div 03: Concrete — Landscape* |
| **R7** Earthwork | Excavation, backfilling, soil replacement, compaction found **outside** the earthwork section, in the structural and/or landscape parts (incl. excavation for fountains / pump rooms) | Moved into the **Div 31 Earthwork** WP. If the BOQ has no earthwork section, an Earthwork WP is **created** from these items. | Stays in the structural / landscape package where the BOQ put it |

Each rule carries bilingual title and message templates. These become the notification the user sees, e.g. *"1 external marble/stone item(s) from Division 04 : Masonry will be GROUPED with the Div 4 Masonry work package … Approve to bundle them with masonry, or Deny …"* together with the Arabic text.

### 3.4 Context rules — fixed overrides from the heading an item sits under (`context_rules`)

| Rule | Condition | Effect |
|---|---|---|
| **C1** External-cladding context | The item sits under a heading (or a short, title-like parent such as `AR 20.0 التكسية الخارجية بالحجر`) containing an **external** word (خارجي، واجهات، external, facade…) **and** a **cladding** word (تكسية، تجاليد، cladding…; common misspellings such as تكيسة included) | The item is classed as **External Marble / Stone Cladding**, and then follows R1 like any other external-marble item |
| **C2** Fountain items (Landscape only) | The item's **own text or its description** (not the headings, which can run on past the fountains) names a fountain: نافورة، نوافير، fountain, water wall, or a water-feature code `WF-01` … | The item goes to **Fountains**: its concrete, waterproofing, mosaic, stone, copings, nozzles. Fountain **excavation** keeps the Earthwork oval |

Notes on C1:
- **What counts as context:** only the headings and a short parent. The item's own text is never used.
- **Non-stone headings:** if the heading names a non-stone cladding (aluminium/ACP, glass, GRC, HPL, wood, metal), only stone items (marble/granite/stone, or items the LLM called internal stone) are flipped to external.
- **Other ovals win:** items the LLM placed in another special case (roof tiles, expansion joints, louvers) keep that oval.
- **Why it exists:** a granite skirting under "التكسية الخارجية بالحجر" is external stone, even though "skirting" sounds internal.

### 3.5 Variants — one product in several sizes is one package (`variants`, `analysis/variants.py`)

General rule: the same product in different **sizes, dimensions, thicknesses, colours, patterns or codes** always lands in one work package. Examples: hollow blocks 120 / 250 mm, interlock PC.01–PC.08 in grey / red, acrylic paint dark grey / light grey / white, signs 250 mm / 450×200 mm, fountain mosaic WF-01…09.

**How variants are recognised.** Neighbouring items in a section are compared after removing numbers, dimensions, units, item codes, colour / pattern / finish words and "includes …" clauses. They are variants when the remaining text is (almost) the same:
- **Under the same parent description:** their own lines are compared. A parent repeated after a page break counts as the same parent.
- **Without a parent:** the full descriptions are compared.
- **Short size lines under a heading that describes the product** (e.g. "20mm thick, Aluminum signage …") are compared word by word.

Items under **different** parents are never variants. For example, fountain excavation WF-01 and fountain waterproofing WF-01 stay apart.

**What happens to a group.** After classification, every item in it gets the group's category:
- a manual choice wins;
- then a context rule;
- otherwise the confidence-weighted majority.

The log reports each group, and the items show `+variant` in *By*. A correction made in the UI on one variant applies to the whole group. In Div 10, a variant group is one work package.

---

## 4. Step by step

### ① Ingest

**One project, one or many BOQs** (`ingest/intake.py`, `agent.analyze`). A project is often split into several BOQs (structural, architectural, landscape …). You can upload several Excel / PDF files at once, and/or `.zip` archives of them (folders inside are fine).
- **Zips** are unpacked safely: no paths outside the target folder, no symlinks, at most 500 files / 2 GB. Office lock files (`~$…`), hidden files and `__MACOSX` are ignored, and so is anything that is not Excel or PDF.
- **PDFs in a multi-file upload are pre-screened.** Only files with a BOQ table header on their first pages are read, so drawings and specifications in a project zip are skipped quickly (scanned pages are OCR'd one at a time, only until a header is found).
- **Each BOQ is read on its own** (as below) with its own id `F1, F2 …`. Every row, sheet and section remembers its file.
- **All BOQs are then packaged together** as one project, so flowchart rules work across files. For example, earthwork from the structural BOQ and from the landscape BOQ ends up in one Earthwork package.
- **Skipped files are listed**, with the reason (no BOQ table, unreadable …).


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
- **Sibling-numbered lines.** Some BOQs number a description and its priced lines at the same level, e.g. description `2.8.1 اعمال الحفر` followed by lines `2.8.1 … 2.8.9`. Short priced lines that directly follow a description at its own level belong to it.
- **Group titles.** A numbered description that gets no priced lines of its own (e.g. `2.8 قسم 02815 النوافير`) is a group **heading** for what follows. It is no longer an orphan stuck to the previous item.
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
5. **Context rules.** C1 is applied after the LLM and overrides it.
6. **Variants.** Size/colour/code variants are aligned to one category (3.5).
7. **No-LLM fallback.** Each item gets its keyword hint, or `MISC`, at confidence 0.4.

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

The app has two tabs, **Work packages** and **RFQs**, and a step tracker across the top: *Upload BOQ → Decide special cases → Export work packages → Send RFQs*. **↺ New BOQ** starts over. Settings (model, renderer …) come from `config.py` / environment variables and are not shown in the app. The only engine message is a warning when the analysis engine is offline.

- **Special cases:** one card per proposal, in English and Arabic, with its items (number, description, unit, qty). The two buttons name the outcome, e.g. *✓ Div 04: Masonry* / *✗ Keep as External Marble / Stone Cladding*. The chosen one is highlighted.
- **Work-package table:** WP id, English/Arabic names, item count, item numbers.
- **Adjust packaging (optional):**
  - change a section's division (that section is re-classified);
  - move any item to another package, picked by package name; the change also applies to the item's size/colour variants.

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

**Packages spanning several BOQs.** A work package can hold items from several BOQ files. Each file's rows are rendered with that file's own method (Excel, HTML fallback or PDF crop), and the parts are joined, in project order, into **one PDF** per package.

**Every PDF** is stamped in the top margin: `WP-03 | Glass Handrails | درابزين زجاج — page 1/1`.

**Output folder** (`workspace/<run_id>/`):

```
uploads/                          unpacked zip contents
F1/, F2/ …                        per BOQ file: the copy that was analysed (source.xlsx / source.pdf, converted if needed)
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

## 6. RFQ drafting (`rfq/`) — the *RFQs* tab

After the work packages are exported, the agent drafts one RFQ letter per work package and supplier from the company Word template, `boq_agent/rfq/templates/RFQ Templet.docx`. A different `.docx` can be uploaded under *Use a different RFQ template*.

The tab has three parts:
- **Project details:** project name, location, owner and the submission deadline. The closing-paragraph deadline follows it unless *Use a different date* is ticked.
- **Suppliers:** **one table**, one row per work package and supplier. Each package starts with one row; add a row for each extra supplier, and rows are kept sorted under their package. Delete a row to skip that package. The package name in the letter is the work-package name.
- **Generate:** a preview of the first letter, then one `.docx` per row.

### 6.1 Template fields

Every `( … )` or `[ … ]` in the template is a field. The template's own wording **is** the list of fields, so a new placeholder added in Word becomes a new field automatically.

When a letter is written, only the placeholder characters are replaced; every other word, the numbering, styles and page setup stay as in the template. Each value takes the formatting of its placeholder, e.g. bold in the opening sentence, 14 pt bold in the title. A field left empty keeps its placeholder text in the letter, and the tab lists such fields before you generate.

What each field means is set in `boq_agent/rfq/fields.json`:

| Field in the template | Scope | Filled from |
|---|---|---|
| `(Package Name / Scope of Works)` | per work package | the work-package name |
| `(Project Name)` | project | auto-detected from the BOQ when certain (see 6.2) |
| `(Project Location)` | project | auto-detected when certain |
| `(Owner Name)` | project | auto-detected when certain |
| `(Supplier Name)` | per letter | typed by the user; one letter per supplier |
| `(Date)` | project | date picker (quotation deadline in *Project Details*) |
| `[Submission Deadline]` | project | "Same as Date" (default) or its own date |

### 6.2 When a value is auto-filled

A project field is filled in by the agent **only when all four checks pass**:
1. **Explicit label.** The value is introduced by a label in the BOQ's title area, page header or page footer: `Project:`, `Client:`, `Owner:`, `Location:`, `اسم المشروع`, `مشروع …`, `المالك:`, `صاحب العمل:`, `الموقع:` …
2. **Verbatim.** The value is copied exactly from the file.
3. **Consistent.** Every sheet or page that states it gives the same value.
4. **Confirmed by the LLM.** The model reads the same lines independently and extracts the same value. If the LLM is off, the fill is marked "label match only".

Anything less certain is shown as a **suggestion** with a *Use* button and is never filled silently. That covers an unlabelled title line, values that differ between sheets, and cases where the label and the LLM disagree.

Results on the BOQs tested so far:

| BOQ | Auto-filled | Suggested only |
|---|---|---|
| Project 2 (`BOQ All QTY.xlsx`) | Project Name = *Bamboo, Family House* (all 8 page headers + LLM) | — |
| Terrace Landscape | Project Name = *Terrace Compound Infrastructure*; Owner = *شركة التعمير والإسكان للإستثمار العقارى* | — |
| ETS-A (Arabic) | — (the label gives *محطة تبادل الطاقة - ETS-A* but the LLM read another line) | both readings; location *العاصمة الادارية الجديدة - الحي الحكومي* |
| BL-01, trial (PDF) | — (no labels in the headers) | the LLM's readings |

### 6.3 Approval before anything is written

- **Confirm each detected value.** Every value found in the BOQ shows where it was found and a *Confirm* tick.
- **Generate stays locked** until every detected value has been confirmed or replaced. A value you type or pick yourself counts as yours and needs no further approval.
- **Preview first.** A preview shows the first letter with the values in place before you generate.
- **Audit trail.** The approved values, file list and time are saved in `session.json` (`rfq`).

### 6.4 Output

`workspace/<run_id>/rfq/<WP folder>/` holds:
- `RFQ - WP-02 Div 04 Masonry - <Supplier>.docx`, one per supplier;
- `BOQ - WP-02 ….pdf`, the package's BOQ cut-out (the template's "BOQ" attachment).

Everything is zipped into `<BOQ name> - RFQs.zip`. Drawings and specifications are not part of the BOQ, so you attach them yourself.

### 6.5 Machine-readable hand-off (`rfq/interface.py`)

Every export also writes `work_packages/work_packages.json`, a list of `RFQPackage`, for any downstream tool (vendor selection, ERP import):

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

Downstream tools should depend only on this contract, never on the BOQ parsing internals.

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
| Add a field to the RFQ letter | type it in the Word template as `(Field Name)` or `[Field Name]`; optionally describe it in `rfq/fields.json` (scope, date type, detection) |
| Teach RFQ detection a new label (e.g. `الاستشاري:`) | the `labels` lists in `rfq/fields.json` |

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
| `boq_agent/ingest/intake.py` | Multi-file / zip intake, safe unzip, BOQ pre-screen for PDFs |
| `boq_agent/ingest/structure.py` | Row roles, sections, parent/child items |
| `boq_agent/excel_com.py` | Excel automation (conversion, rendering) and xlrd fallback |
| `boq_agent/knowledge/flowchart_rules.json` | **The flowchart as data** |
| `boq_agent/knowledge/rules.py` | Loads the rules; keyword pre-screen; context rules; renders the flowchart prompt |
| `boq_agent/llm/client.py` | Ollama client with JSON-schema output and retries |
| `boq_agent/llm/prompts.py` | Persona, task prompts, answer schemas |
| `boq_agent/analysis/classifier.py` | Division assignment (incl. landscape detection) and item classification (batch → review → context rules → variants) |
| `boq_agent/analysis/variants.py` | Groups size / colour / code variants and keeps each group in one package |
| `boq_agent/planning/planner.py` | Flowchart engine and snapshot row selection |
| `boq_agent/render/snapshots.py` | PDF cut-outs (Excel COM / HTML / PDF crop) |
| `boq_agent/rfq/templates/RFQ Templet.docx` | The company RFQ template |
| `boq_agent/rfq/fields.json` | What each template field means, its scope, and the labels used for detection |
| `boq_agent/rfq/template.py` | Field discovery and verbatim filling of the Word template |
| `boq_agent/rfq/detect.py` | Reads the BOQ title area / headers / footers and auto-fills only near-certain values |
| `boq_agent/rfq/builder.py` | Prefill (nothing committed) and generation of the letters + ZIP |
| `boq_agent/rfq/interface.py` | `work_packages.json` hand-off contract |
| `boq_agent/text_utils.py` | Arabic/English normalisation, keyword matching, inclusion-clause stripping, formatting |

---

## 9. Known limits and judgement calls

- **The LLM can vary at the margins.** Temperature is 0, but borderline items (e.g. stone skirting, cement-board copings) can still change between model versions. Check the packages and fix any item in *Adjust packaging*; the method and reason per item are kept in `session.json`. Manual corrections always win.
- **Not every BOQ layout is covered.** Header and structure detection handle the usual Egyptian/Gulf layouts. A BOQ with no recognisable header on a sheet is reported as a warning and that sheet is skipped.
- **Scanned PDFs depend on OCR quality.** Low-resolution scans may mis-map columns.
- **Without Microsoft Excel, snapshots lose some fidelity.** The HTML renderer reproduces structure and basic styling, but not logos or page headers/footers.
