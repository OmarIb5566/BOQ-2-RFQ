"""Golden test: the packaging result for a fixed sample BOQ (keyword mode, no LLM) must not change by accident.

Later phases change the engine; if a change to the packages is intended, accept it with
    UPDATE_GOLDEN=1 pytest tests/test_golden.py
and review the diff of tests/golden/villa.json in the commit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from boq_agent.agent import BOQAgent
from boq_agent.knowledge.rules import FlowchartRules

GOLDEN = Path(__file__).parent / "golden" / "villa.json"


def summarize(session) -> dict:
    items = session.document.item_map()
    return {
        "items": len(session.document.items),
        "work_packages": {wp.key: {"name": wp.name_en,
                                   "items": sorted(items[u].item_no for u in wp.item_uids)}
                          for wp in session.plan.work_packages},
        "proposals": sorted(p.proposal_id for p in session.plan.proposals),
    }


def test_villa_packages_match_golden(settings, villa_xlsx):
    agent = BOQAgent(settings, log=lambda m: None)
    got = summarize(agent.analyze([villa_xlsx], ["villa.xlsx"]))
    if os.getenv("UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert got == want


def test_decisions_replan_deterministically(settings, villa_xlsx):
    agent = BOQAgent(settings, log=lambda m: None)
    s = agent.analyze([villa_xlsx], ["villa.xlsx"])
    before = summarize(s)
    ids = [p.proposal_id for p in s.plan.proposals]
    assert ids, "the sample BOQ should trigger at least one special case"
    agent.decide_many(s, ids, "approved")
    approved = summarize(s)
    agent.decide_many(s, ids, "denied")
    denied = summarize(s)
    agent.decide_many(s, ids, "approved")
    assert summarize(s) == approved                       # same decisions -> same packages
    assert approved != denied
    assert before["items"] == approved["items"] == denied["items"]


def test_every_item_is_packaged_exactly_once(settings, villa_xlsx):
    s = BOQAgent(settings, log=lambda m: None).analyze([villa_xlsx], ["villa.xlsx"])
    uids = [u for wp in s.plan.work_packages for u in wp.item_uids]
    assert sorted(uids) == sorted(i.uid for i in s.document.items)


def test_flowchart_rules_file_is_consistent(settings):
    raw = json.loads(Path(settings.rules_path).read_text(encoding="utf-8"))
    FlowchartRules(settings.rules_path)                    # loads without error
    modes = {d.get("mode") for d in raw["divisions"].values()} if isinstance(raw["divisions"], dict) else {
        d.get("mode") for d in raw["divisions"]}
    assert modes <= {"as_is", "split", "per_item"}
