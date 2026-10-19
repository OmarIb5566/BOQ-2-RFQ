"""System prompts. The flowchart part is generated from flowchart_rules.json."""
from __future__ import annotations

from ..knowledge.rules import FlowchartRules

PERSONA = """You are a senior Construction Manager and Quantity Surveyor with 20+ years of experience on \
Egyptian and Gulf building projects. You read Bills of Quantities (BOQ / جداول الكميات / المقايسة) written in \
Arabic, English or both, and you are fluent in Arabic construction terminology, for example:
باب/الباب = BOQ section (division); بند = item; بيان الأعمال = description; الفئة/سعر الوحدة = rate; الجملة/الإجمالي = amount;
م² = m2; م³ = m3; م.ط = linear metre; بالعدد/عدد = number; مقطوعية = lump sum;
خرسانة = concrete; حديد تسليح = rebar; مباني/طوب/بلوك = masonry; حفر/ردم/إحلال = earthwork;
عزل = insulation/waterproofing; مشمعات بيتومينية = bituminous membranes; فواصل تمدد = expansion joints;
أعمال الحدادة/الأعمال المعدنية = metal works; صاج = sheet steel; درابزين = handrail; سلم بحاري = ship ladder; فتحات كشف = access hatch;
نجارة = carpentry (mostly wooden doors); حلق = door frame; ضلفة = door leaf; ألوميتال = aluminium joinery; شبابيك = windows; لوفر/كاسرات = louvers;
بياض/محارة = plaster; دهانات بلاستيك = acrylic/emulsion paint; إيبوكسي = epoxy; رخام = marble; جرانيت = granite; حجر = stone;
سيراميك/بورسلين = ceramic/porcelain; وزر = skirting; أسقف معلقة/جبسون بورد = suspended ceiling/gypsum board; باركيه = parquet; قرميد = roof tiles.
Your job is procurement packaging: cutting the BOQ into work packages (WP) that will each be sent to \
specialist subcontractors as a Request For Quotation (RFQ). Judge every item by WHAT IS SUPPLIED/INSTALLED \
and its MAIN MATERIAL and LOCATION – not by incidental words (e.g. 'aluminium louvers with wood texture' are \
METAL louvers; 'plaster under ceramic tiles' is PLASTER). Scope listed after 'the item includes / including / \
شاملاً / ويشمل' is ancillary work priced inside the main item, never its category (e.g. 'reinforced concrete … the \
item includes formwork, movement joints, joint filler' is CONCRETE, not expansion joints). Be precise, consistent \
and conservative."""


def system_prompt(rules: FlowchartRules) -> str:
    return PERSONA + "\n\n" + rules.render_prompt() + "\n\nAlways answer with JSON only, matching the requested schema."


DIVISION_TASK = """Identify the CSI MasterFormat division of each BOQ section below. Use the section title (often \
'الباب ...' or 'Division ..'), the sheet name, the sub-headings AND the sample items. Egyptian BOQ naming hints: \
أعمال الحفر والردم→31, الخرسانة→03, المباني→04, الأعمال المعدنية/الحدادة→05, أعمال الأخشاب (pergolas, wood cladding)→06, \
العزل→07, الأبواب والشبابيك/النجارة (doors)/الألوميتال→08, التشطيبات/البياض/الدهانات/الأرضيات/التكسيات/الأسقف→09, \
الأعمال الخاصة/اللوحات الإرشادية/الإكسسوارات→10, الأعمال الصحية→22, التكييف→23, الكهرباء→26, الحريق→21, الموقع العام→32. \
Use "00" if the section is not a work section (e.g. summary, notes) or you cannot tell.

Allowed division codes: {codes}

Sections:
{sections}"""


ITEM_TASK = """BOQ section: "{section}" — Division {division} {division_name}.
Classify EVERY item below independently into exactly ONE category id from this list:
{menu}

Rules: use the PARENT text (the main description) together with the ITEM text (the sub-item). \
Give a confidence 0..1 and a very short reason (max 12 words, English).

Items:
{items}"""


REVIEW_TASK = """Double-check this single BOQ item. A first pass classified it as "{first}" (confidence {conf:.2f}); \
a keyword pre-screen suggested "{hint}" (keywords can be misleading). Think about the material, the location \
(internal/external) and the trade that would execute it, then give the final category. Ignore anything the \
description merely lists as included ancillary scope.

Section: "{section}" — Division {division} {division_name}
Allowed categories:
{menu}

Item:
{item}"""


def division_schema(codes: list[str], ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section_id": {"type": "string", "enum": ids},
                        "division": {"type": "string", "enum": codes},
                        "confidence": {"type": "number"},
                    },
                    "required": ["section_id", "division", "confidence"],
                },
            }
        },
        "required": ["results"],
    }


def items_schema(cats: list[str], ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string", "enum": ids},
                        "category": {"type": "string", "enum": cats},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["item_id", "category", "confidence", "reason"],
                },
            }
        },
        "required": ["results"],
    }


def review_schema(cats: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": cats},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["category", "confidence", "reason"],
    }


HEADER_TASK = """Below are the first rows of a BOQ spreadsheet (row number, then cells as COLUMN=value). \
Find the table header row(s) and which column holds each field. Return column letters; use "" if a field does not exist.

{rows}"""

HEADER_SCHEMA = {
    "type": "object",
    "properties": {
        "header_first_row": {"type": "integer"},
        "header_last_row": {"type": "integer"},
        "item_no": {"type": "string"},
        "description": {"type": "string"},
        "unit": {"type": "string"},
        "quantity": {"type": "string"},
        "rate": {"type": "string"},
        "total": {"type": "string"},
    },
    "required": ["header_first_row", "header_last_row", "item_no", "description", "unit", "quantity", "rate", "total"],
}
