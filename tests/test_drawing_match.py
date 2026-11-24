"""Drawing sheets go to the packages they show — not general notes to everyone, not title-block text."""
from __future__ import annotations

from pathlib import Path

import pytest

from boq_agent.docs.common import load_doc_rules
from boq_agent.docs.drawings import boilerplate, is_general_sheet, is_note
from boq_agent.docs.index import _number_from_name, _titles_from_drawing_list
from boq_agent.models import DrawingSheet

RULES = Path(__file__).resolve().parents[1] / "boq_agent" / "knowledge" / "document_rules.json"


@pytest.fixture(scope="module")
def drules():
    return load_doc_rules(str(RULES))


@pytest.mark.parametrize("line", [
    "1-WALLS ARE DIMENSIONED TO THE UNFINISHED FACE OF THE CMU WALL, FACE OF CONCRETE OR",
    "LARGE DISTANCES OR HEIGHTS. REINFORCED MASONRY MAY BE USED TOGETHER WITH THE APPROPRIATE",
    "FIRE SEALED WITH 2 HRS FIRE PROOFING MATERIAL AFTER INSTALLATION OF SERVICES",
    "EQUIPMENTS FINAL DIMENSIONS AND REQUIREMENTS.",
    "the ceiling tiles.",
    "4-COORDINATE PLACEMENT OF ALL CEILING ELEMENTS WITH MECHANICAL, ELECTRICAL",
])
def test_note_lines_are_not_legend_entries(line):
    assert is_note(line)


@pytest.mark.parametrize("line", ["BLACK GRANITE TILE SKIRTING", "LIGHT GREY PAINT",
                                  "ACRYLIC PAINT ON CEMENT PLASTER (COLOR RAL 9010)", "GYPSUM BOARD FALSE CEILING"])
def test_legend_entries_are_kept(line):
    assert not is_note(line)


def _lines(*texts):
    return [(t, 10.0, (0, i * 12, 200, i * 12 + 10)) for i, t in enumerate(texts)]


def test_general_notes_sheet_is_recognised():
    assert is_general_sheet(_lines("GENERAL NOTES", "1-ALL DRAWINGS SHALL BE …", "SYMBOLS", "ABBREVIATIONS"))
    # a plan with a small notes box in its title block is still a plan
    assert not is_general_sheet(_lines("GROUND FLOOR PLAN", "GENERAL NOTES :", "1- Do not scale"))


def test_title_block_line_on_every_sheet_is_boilerplate():
    pages = [_lines(f"ROOM {i}", "the ceiling tiles.", "BLACK GRANITE TILE SKIRTING") for i in range(6)]
    boiler = boilerplate(pages, 0.5, keep=lambda n: "ceiling" in n or "granite" in n)
    assert "the ceiling tiles." in boiler                      # disclaimer fragment: ignored
    assert "black granite tile skirting" not in boiler         # a legend repeated on every plan: kept


def test_trade_from_combined_number_field(drules):
    assert drules.discipline("", "E2405-TND-001-ADE-10300-0") == "AR"
    assert drules.discipline("", "E2405-TND-001-SDE-10300-0") == "ST"


def test_number_from_file_name_beats_a_level_tag(drules):
    sh = DrawingSheet(uid="D9:p1", file_id="D9", page=0, number="B.B-3.4F-UTI-002", title="FIRST FLOOR PLAN", kind="plan")
    _number_from_name(sh, "PDF.zip/PDF/E2405-TND-001-ADE-10300-0.pdf", drules,
                      _lines("B.B-3.4F-UTI-002", "DRAWING NO.", "E2405-TND-001-ADE-10300-0"))
    assert sh.number == "E2405-TND-001-ADE-10300-0" and sh.discipline == "AR"


def test_missing_title_comes_from_the_drawing_list(drules):
    sheets = [DrawingSheet(uid="D12:p1", file_id="D12", page=0, number="E2405-TND-001-ADE-10401-0")]
    # the list page is stored rotated: one row = same x, increasing y
    lst = [("E2405-TND-001-ADE-10400-0", 10, (1175, 584, 1206, 1015)), ("SECOND FLOOR PLAN", 10, (1174, 1238, 1206, 1580)),
           ("E2405-TND-001-ADE-10401-0", 10, (1130, 584, 1161, 1015)),
           ("SECOND FLOOR PLAN-PARTIALS", 10, (1129, 1156, 1161, 1661)), ("1:100", 10, (1130, 1900, 1161, 1990))]
    _titles_from_drawing_list(sheets, {"D2": [lst]}, drules)
    assert sheets[0].title == "SECOND FLOOR PLAN-PARTIALS" and sheets[0].kind == "plan"
