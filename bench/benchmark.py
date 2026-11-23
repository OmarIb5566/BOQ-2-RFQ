"""Benchmark the packaging against packages people made by hand (real historical projects).

    .venv\\Scripts\\python bench\\benchmark.py --config baseline --config keywords --out bench\\results

Ground truth = a folder per project with the FULL BOQ(s) and the packages a person cut from them (one BOQ file per
package). For every human package H the harness finds its items in the app's result and reports:
    found     share of H's items matched to an item of the full BOQ: same text (+qty), else the same item number
              with ≥ 35 % of the words in common, else ≥ 60 % of the words in common (the hand-made packages were
              often cut from a slightly different revision of the BOQ: other thicknesses / quantities)
    together  share of H's (found) items that the app put in ONE work package — the package holding most of them
    purity    share of that app package's items that belong to H (low = the app mixed other work in)
Special cases are evaluated twice: all approved and all denied (the human decisions are not recorded).
Everything runs in a scratch folder (never workspace/). Client data stays local: only numbers are written out.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from boq_agent.agent import BOQAgent  # noqa: E402
from boq_agent.config import Settings  # noqa: E402
from boq_agent.text_utils import norm  # noqa: E402

# project -> (full BOQ files, {human package name: package BOQ file})
PROJECTS = {
    "Project 1": (["ETS-A-ARCH-BOQ.xls", "ETS-A-Civil-BOQ.xlsx"],
                  {"Metal doors & windows": "Metal Doors & Windows BOQ.pdf", "Metal works": "Metal Works BOQ.pdf",
                   "RC concrete": "RM Conc BOQ.pdf", "Signage": "Signages BOQ.pdf",
                   "Wooden doors": "Wooden Doors BOQ.pdf"}),
    "Project 2": (["BOQ All QTY.xlsx"],
                  {"Blockwork": "Blockwork/Blockwork BOQ Bamboo Project.xlsx",
                   "Marble": "Marble/Marble BOQ Bamboo Project.xlsx",
                   "Waterproofing": "Waterproofing/Insulation BOQ Bamboo Project.xlsx",
                   "Earthwork": "Earthwork BOQ Bamboo Project.pdf"}),
}
FOLDERS = {"Project 1": "Project 1 Packages", "Project 2": "Project 2 Packages"}

CONFIGS = {
    "keywords": {"use_llm": False},
    "baseline": {"model": "gemma4:e4b"},
    "qwen3-14b": {"model": "qwen3:14b"},
}


def key(it) -> str:
    return " ".join(norm(it.description or it.full_description or "").split())[:300]


def qkey(it) -> str:
    try:
        q = f"{float(str(it.quantity).replace(',', '')):.2f}"
    except ValueError:
        q = str(it.quantity).strip()
    return key(it) + "|" + q


def words(it) -> set[str]:
    return {w for w in key(it).split() if len(w) > 1}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def read_items(path: Path, scratch: Path) -> list:
    s = Settings(workspace=scratch, use_llm=False, excel_renderer="html")
    return BOQAgent(s, log=lambda m: None).analyze([path], [path.name]).document.items


def evaluate(session, human: dict[str, list]) -> dict:
    items = session.document.items
    by_q, by_t = {}, {}
    for it in items:
        by_q.setdefault(qkey(it), []).append(it.uid)
        by_t.setdefault(key(it), []).append(it.uid)
    wp_of = {u: wp.key for wp in session.plan.work_packages for u in wp.item_uids}
    size = Counter(wp_of.values())
    out = {}
    for name, hitems in human.items():
        used, uids = set(), []
        for h in hitems:
            cand = [u for u in by_q.get(qkey(h), []) if u not in used] or [u for u in by_t.get(key(h), [])
                                                                             if u not in used]
            if not cand:
                hw, hno = words(h), (h.item_no or "").strip().lower()
                best, score = None, 0.0
                for it in items:
                    if it.uid in used:
                        continue
                    j = jaccard(hw, words(it))
                    same_no = hno and (it.item_no or "").strip().lower() == hno
                    if (same_no and j >= 0.35) or j >= 0.6:
                        sc = j + (1 if same_no else 0)
                        if sc > score:
                            best, score = it.uid, sc
                cand = [best] if best else []
            if cand:
                used.add(cand[0])
                uids.append(cand[0])
        if not uids:
            out[name] = {"items": len(hitems), "found": 0.0, "together": None, "purity": None, "package": None}
            continue
        pk = Counter(wp_of.get(u) for u in uids)
        best, n = pk.most_common(1)[0]
        out[name] = {"items": len(hitems), "found": round(len(uids) / len(hitems), 3),
                     "together": round(n / len(uids), 3), "purity": round(n / size[best], 3) if best else None,
                     "package": best, "split_over": len(pk)}
    return out


def run(cfg_name: str, data_root: Path, scratch: Path, log=print) -> dict:
    over = CONFIGS[cfg_name]
    results = {"config": cfg_name, "settings": over, "projects": {}}
    for proj, (full, pkgs) in PROJECTS.items():
        folder = data_root / FOLDERS[proj]
        files = [folder / f for f in full]
        if not all(f.exists() for f in files):
            log(f"{proj}: files missing — skipped")
            continue
        human = {name: read_items(folder / f, scratch / "human") for name, f in pkgs.items() if (folder / f).exists()}
        base = Settings(workspace=scratch / cfg_name, excel_renderer="html", force=True)
        st = replace(base, **over)
        agent = BOQAgent(st, log=lambda m: None)
        t0 = time.time()
        s = agent.analyze(files, [f.name for f in files])
        secs = round(time.time() - t0, 1)
        ids = [p.proposal_id for p in s.plan.proposals]
        res = {"seconds": secs, "llm_calls": agent.llm.calls, "items": len(s.document.items),
               "packages": len(s.plan.work_packages), "special_cases": len(ids)}
        agent.decide_many(s, ids, "approved")
        res["approved"] = evaluate(s, human)
        agent.decide_many(s, ids, "denied")
        res["denied"] = evaluate(s, human)
        results["projects"][proj] = res
        log(f"{cfg_name} · {proj}: {secs}s, {res['items']} items, {res['packages']} packages")
    return results


def summary(results: dict) -> dict:
    """Mean of 'together' and 'purity' over all human packages, per decision mode."""
    out = {}
    for mode in ("approved", "denied"):
        tog, pur = [], []
        for p in results["projects"].values():
            for v in p[mode].values():
                if v["together"] is not None:
                    tog.append(v["together"])
                    pur.append(v["purity"])
        out[mode] = {"together": round(sum(tog) / len(tog), 3) if tog else None,
                     "purity": round(sum(pur) / len(pur), 3) if pur else None, "n": len(tog)}
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", choices=list(CONFIGS), required=True)
    ap.add_argument("--data", default=str(ROOT))
    ap.add_argument("--out", default=str(ROOT / "bench" / "results"))
    ap.add_argument("--scratch", default="")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="boq-bench-"))
    try:
        for c in args.config:
            r = run(c, Path(args.data), scratch)
            r["summary"] = summary(r)
            (out / f"{c}.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
            print(c, json.dumps(r["summary"]))
    finally:
        if not args.scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
