"""Command-line runner.

    python cli.py "BOQ.xlsx"                 # interactive approve/deny for special cases
    python cli.py "BOQ.pdf" --approve-all    # non-interactive
    python cli.py "BOQ.xls" --deny-all --no-llm
    python cli.py Structural.xlsx Architectural.pdf Landscape.xlsx --approve-all   # one project, several BOQs
    python cli.py "Project BOQs.zip" --approve-all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from boq_agent.agent import BOQAgent
from boq_agent.config import Settings


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Cut a BOQ into work-package PDFs following the company flowchart.")
    ap.add_argument("files", type=Path, nargs="+", help="BOQ files (Excel / PDF) and/or .zip archives of them")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve-all", action="store_true")
    g.add_argument("--deny-all", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="keyword-only mode (no Ollama)")
    ap.add_argument("--renderer", choices=["auto", "excel", "html"], default="auto")
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    s = Settings()
    s.use_llm = not args.no_llm
    s.excel_renderer = args.renderer
    if args.model:
        s.model = args.model
    agent = BOQAgent(s, log=lambda m: print("  ·", m))
    session = agent.analyze(args.files)

    doc = session.document
    items = doc.item_map()
    print("\nWORK PACKAGES (preview, special cases shown as approved):")
    for wp in session.plan.work_packages:
        print(f"  {wp.wp_id}  {wp.name_en}  [{len(wp.item_uids)} items]")
        for iu in wp.item_uids:
            it = items[iu]
            print(f"        {it.item_no:9} {it.category or '-':24} {it.confidence:.2f} {it.method:10} {it.full_description[:70]}")

    for p in session.plan.proposals:
        print(f"\n⚠ SPECIAL CASE: {p.title_en}\n  {p.message_en}\n  {p.message_ar}")
        for iu in p.item_uids:
            print(f"     - {items[iu].item_no} {items[iu].full_description[:90]}")
        if args.approve_all:
            d = "approved"
        elif args.deny_all:
            d = "denied"
        else:
            d = "denied" if input("  Approve? [Y/n] ").strip().lower().startswith("n") else "approved"
        agent.decide(session, p.proposal_id, d)
        print(f"  -> {d}")

    session = agent.export(session)
    print("\nOUTPUT:")
    for wp in session.plan.work_packages:
        print(f"  {wp.wp_id}  {wp.name_en:55}  {Path(wp.pdf_file).name if wp.pdf_file else '-'}")
    print(f"\nZIP: {session.zip_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
