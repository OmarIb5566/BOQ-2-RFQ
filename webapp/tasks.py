"""The background tasks, by name. Loaded by the web app AND by worker.py, so both always run the same code.

Each task is fn(ctx) -> result message. It must not use Flask (no request, no url_for): it may run in another
process. Add a task with @task("name") and submit it with jobs.submit("name", label, run_id, user_id, args).
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

REGISTRY: dict[str, Callable] = {}
AFTER_ANALYSIS: list[Callable] = []        # hooks run after a project was (re-)analysed: fn(runtime, run_id, session)


def task(name: str):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


@dataclass
class JobContext:
    job_id: str
    run_id: str
    user_id: Optional[int]
    args: dict
    progress: Callable[[float, str], None]
    runtime: object = field(repr=False)

    @property
    def store(self):
        return self.runtime.store

    @property
    def db(self):
        return self.runtime.db


def on_failed(runtime, job: dict, error: str) -> None:
    if job["kind"] in ("analyze", "duplicate", "revision") and job.get("run_id"):
        runtime.db.upsert_project(job["run_id"], stage="failed", error=error[:1000])


def engine_settings(runtime, use_ai: bool = True):
    """The engine settings for one job: the base settings + the models chosen by the administrator."""
    from boq_agent.config import Settings
    base = runtime.settings
    over = {}
    models = runtime.db.setting("models", {}) or {}
    if models.get("text"):
        over["model"] = models["text"]
    if models.get("vision"):
        over["vision_model"] = models["vision"]
    if not use_ai:
        over["use_llm"] = False
    return Settings(**{**base.__dict__, **over}) if over else base


def after_analysis(runtime, run_id: str, session) -> None:
    for hook in AFTER_ANALYSIS:
        try:
            hook(runtime, run_id, session)
        except Exception:  # noqa: BLE001  (an index must never fail the analysis)
            import traceback
            traceback.print_exc()


# ---------------------------------------------------------------------- analysis
@task("analyze")
def analyze(ctx: JobContext) -> str:
    from boq_agent.agent import BOQAgent
    store, db = ctx.store, ctx.db
    man = json.loads((store.run_dir(ctx.run_id) / "_incoming" / "manifest.json").read_text(encoding="utf-8"))
    settings = engine_settings(ctx.runtime, man.get("use_ai", True))
    agent = BOQAgent(settings, log=lambda m: None, progress=ctx.progress)
    from .fleet import attach_fleet
    attach_fleet(agent, ctx)
    with store.lock(ctx.run_id):
        s = agent.analyze([Path(f["path"]) for f in man["files"]], [f["name"] for f in man["files"]],
                          run_id=ctx.run_id)
        carried = ""
        if man.get("carry_from"):
            from .revisions import carry_forward
            carried = carry_forward(ctx.runtime, man["carry_from"], s, agent)
            agent.save(s)
        from .decisions import apply as apply_remembered
        n_mem = apply_remembered(db, agent, s, ctx.run_id)       # open cases only: a revision's own decisions win
        if n_mem:
            carried = (carried + " · " if carried else "") + f"{n_mem} special case(s) decided as before"
        db.upsert_project(ctx.run_id, error="")
        store.saved(ctx.run_id, ctx.user_id, s)
    after_analysis(ctx.runtime, ctx.run_id, s)
    msg = f"{len(s.document.items)} items → {len(s.plan.work_packages)} work packages"
    return msg + (f" · {carried}" if carried else "")


@task("division")
def division(ctx: JobContext) -> str:
    with ctx.store.edit(ctx.run_id, ctx.user_id) as (agent, s):
        agent.progress = ctx.progress
        from .fleet import attach_fleet
        attach_fleet(agent, ctx)
        agent.set_section_division(s, ctx.args["section_uid"], ctx.args.get("division"))
    after_analysis(ctx.runtime, ctx.run_id, s)
    return "Section re-classified"


@task("docs")
def docs_kind(ctx: JobContext) -> str:
    with ctx.store.edit(ctx.run_id, ctx.user_id) as (agent, s):
        agent.progress = ctx.progress
        agent.set_doc_kind(s, ctx.args["file_id"], ctx.args["kind"])
    return "Documents re-read and matched"


@task("export")
def export(ctx: JobContext) -> str:
    with ctx.store.edit(ctx.run_id, ctx.user_id) as (agent, s):
        agent.progress = ctx.progress
        agent.export(s)
        from .register import write_pricing_sheets
        write_pricing_sheets(s, ctx.store.run_dir(ctx.run_id))
    return f"{len(s.plan.work_packages)} work packages exported"


@task("rfq-suppliers")
def rfq_suppliers(ctx: JobContext) -> str:
    """A supplier list (who does which trade) -> supplier names next to the matching work packages.
    Only ADDS names; what was added is remembered so it can be undone."""
    from boq_agent.rfq.suppliers import assign, match_trades, read_supplier_list
    from .db import now

    ctx.progress(0.1, "Reading the supplier list …")
    try:
        lst = read_supplier_list(Path(ctx.args["file"]))
    except Exception as e:  # noqa: BLE001  (damaged / unreadable file)
        raise ValueError(f"The supplier list cannot be read ({type(e).__name__}).") from e
    if not lst.pairs:
        raise ValueError("No supplier with a trade was found in the file. Use two columns: Supplier | Packages.")
    with ctx.store.edit(ctx.run_id, ctx.user_id) as (agent, s):
        llm = agent.llm if agent.settings.use_llm and agent.llm.status()[0] else None
        ctx.progress(0.3, f"Matching {len(lst.trades)} trade(s) to the work packages …")
        matches = match_trades(s, agent.rules, lst.trades, llm, agent.log)
        per = assign(lst, matches)
        sups = s.rfq_draft.setdefault("suppliers", {})
        added: dict[str, list[str]] = {}
        for key, names in per.items():
            cur = sups.setdefault(key, [])
            have = {x.casefold() for x in cur}
            new = [n for n in names if n.casefold() not in have]
            if new:
                cur += new
                added[key] = new
        s.rfq_draft["supplier_import"] = {
            "file": ctx.args.get("name", ""), "ts": now(), "layout": lst.layout,
            "suppliers": len(lst.suppliers), "trades": len(lst.trades), "added": added,
            "unmatched": [t for t in lst.trades if t not in matches][:50],
            "by_ai": sum(1 for v in matches.values() if any(h == "AI" for _, h in v))}
        agent.save(s)
    n = sum(len(v) for v in added.values())
    return f"{n} supplier name(s) added to {len(added)} package(s)"


@task("rfq-detect")
def rfq_detect(ctx: JobContext) -> str:
    from dataclasses import asdict
    ctx.progress(0.2, "Reading the BOQ title area, headers and footers …")
    with ctx.store.edit(ctx.run_id, ctx.user_id) as (agent, s):
        tpl = Path(ctx.args["template"])
        pre = agent.rfq_prefill(s, tpl)
        s.rfq_draft["prefill"] = {"project": [asdict(p) for p in pre["project"]],
                                  "supplier_fields": pre["supplier_fields"]}
        s.rfq_draft["prefill_template"] = str(tpl)
        vals = s.rfq_draft.setdefault("values", {})
        for p in pre["project"]:
            if p.value and not vals.get(p.spec.name):
                vals[p.spec.name] = p.value
        agent.save(s)
    return "Project details detected"


@task("rfq")
def rfq_generate(ctx: JobContext) -> str:
    a = ctx.args
    ctx.progress(0.1, "Writing the letters …")
    with ctx.store.edit(ctx.run_id, ctx.user_id) as (agent, s):
        agent.generate_rfqs(s, a["values"], a["packages"], a["supplier_fields"], Path(a["template"]))
    from .register import register_letters
    n = register_letters(ctx.runtime, ctx.run_id, s, a, ctx.user_id)
    return f"{len(s.rfq.get('files', []))} RFQ letter(s) written" + (f" · {n} added to the RFQ register" if n else "")


@task("duplicate")
def duplicate(ctx: JobContext) -> str:
    from boq_agent.models import Session
    store = ctx.store
    src_id, new_id = ctx.args["src"], ctx.run_id
    src, dst = store.run_dir(src_id), store.run_dir(new_id)
    ctx.progress(0.1, "Copying the project files …")
    with store.lock(src_id):
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
            "work_packages", "rfq", "*.zip", "session.tmp"))
    text = (dst / "session.json").read_text(encoding="utf-8")
    for a, b in ((str(src), str(dst)), (str(src).replace("\\", "/"), str(dst).replace("\\", "/"))):
        text = text.replace(json.dumps(a)[1:-1], json.dumps(b)[1:-1])
    s = Session.model_validate_json(text)
    s.run_id = new_id
    s.status, s.output_dir, s.zip_file, s.rfq = "planned", None, None, {}
    for wp in s.plan.work_packages:
        wp.pdf_file = wp.spec_pdf = wp.drawings_pdf = None
    store.agent().save(s)
    store.saved(new_id, ctx.user_id, s)
    after_analysis(ctx.runtime, new_id, s)
    return "Copy ready"


@task("index")
def index_project(ctx: JobContext) -> str:
    """Rebuild a project's search index and meaning vectors (used after imports / restores)."""
    s = ctx.store.load(ctx.run_id)
    after_analysis(ctx.runtime, ctx.run_id, s)
    return "Search index rebuilt"


@task("purge-trash")
def purge_trash(ctx: JobContext) -> str:
    from .collab import purge_trash as purge
    n = purge(ctx.runtime, int(ctx.args.get("days", 30)))
    return f"{n} project(s) deleted for good"


@task("noop")
def noop(ctx: JobContext) -> str:
    """Used by tests and the admin console's 'test the worker' button."""
    import time
    steps = int(ctx.args.get("steps", 3))
    for i in range(steps):
        ctx.progress(i / steps, f"Step {i + 1}/{steps}")
        time.sleep(float(ctx.args.get("sleep", 0.05)))
    if ctx.args.get("fail"):
        raise RuntimeError(ctx.args["fail"])
    if ctx.args.get("transient"):
        import requests
        raise requests.ConnectionError("model server not reachable")
    return "ok"
