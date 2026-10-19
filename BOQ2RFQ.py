"""BOQ → Work Packages agent (Streamlit UI).

Run:  .venv\\Scripts\\streamlit run BOQ2RFQ.py      (or double-click run_app.bat)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from agent import BOQAgent
from config import Settings
from knowledge.rules import MISC
from text_utils import truncate

st.set_page_config(page_title="BOQ → Work Packages", page_icon="📐", layout="wide")
st.markdown(
    """<style>
    .rtl {direction: rtl; text-align: right; font-size: 0.95rem;}
    div[data-testid="stExpander"] details summary p {font-weight: 600;}
    </style>""",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------ sidebar / settings
with st.sidebar:
    st.header("⚙️ Settings")
    model = st.text_input("Local LLM (Ollama model)", value=Settings().model)
    url = st.text_input("Ollama URL", value=Settings().ollama_url)
    use_llm = st.toggle("Use LLM reasoning", value=True,
                        help="Off = keyword-only mode (fast, no Ollama needed, less accurate).")
    renderer = st.selectbox("Excel snapshot renderer", ["auto", "excel", "html"],
                            help="auto = Microsoft Excel if installed (exact look of the BOQ), else built-in HTML renderer.")
    batch = st.slider("Items per LLM call", 4, 20, 10)

settings = Settings()
settings.model, settings.ollama_url, settings.use_llm = model, url, use_llm
settings.excel_renderer, settings.batch_size = renderer, batch

with st.sidebar:
    ok, msg = BOQAgent(settings).llm.status() if use_llm else (False, "LLM disabled")
    (st.success if ok else st.warning)(msg)
    st.caption("Flowchart rules: `boq_agent/knowledge/flowchart_rules.json`")


def make_agent(progress_widget=None, log_box=None) -> BOQAgent:
    logs: list[str] = []

    def _log(m: str):
        logs.append(m)
        if log_box is not None:
            log_box.code("\n".join(logs[-14:]), language=None)

    def _progress(p: float, m: str):
        if progress_widget is not None:
            progress_widget.progress(min(max(p, 0.0), 1.0), text=m)

    return BOQAgent(settings, log=_log, progress=_progress)


# ----------------------------------------------------------------------- header
st.title("📐 BOQ → Work Packages")
st.caption("Upload a BOQ (Excel or PDF, Arabic or English). The agent — acting as a construction manager — cuts it "
           "into work packages following the company flowchart, asks you about special cases, and exports one PDF "
           "snapshot per work package.")

tab_main, tab_rules, tab_rfq = st.tabs(["Work packages", "Flowchart rules (agent instructions)", "RFQ drafting (next module)"])

with tab_rules:
    agent0 = BOQAgent(settings)
    from llm.prompts import system_prompt
    st.markdown("The flowchart is converted into the agent's system prompt below. Rectangles = normal packaging, "
                "ovals = special cases that require your approval.")
    st.code(system_prompt(agent0.rules), language=None)

with tab_rfq:
    st.info("Planned next module: draft RFQs for each exported work package (vendor selection from the vendor register, "
            "bilingual RFQ letter, attachments). Each export already writes `work_packages.json` — the hand-off contract "
            "defined in `boq_agent/rfq/interface.py`.")

with tab_main:
    up = st.file_uploader("BOQ file", type=["xlsx", "xlsm", "xls", "xlsb", "pdf"])
    if up is not None and st.button("🔍 Analyze BOQ", type="primary"):
        tmp = Path(tempfile.mkdtemp()) / up.name
        tmp.write_bytes(up.getbuffer())
        prog = st.progress(0.0, text="Starting …")
        with st.status("Agent working …", expanded=True) as status:
            log_box = st.empty()
            agent = make_agent(prog, log_box)
            try:
                st.session_state.session = agent.analyze(tmp, up.name)
                status.update(label="Analysis complete", state="complete", expanded=False)
            except Exception as e:  # noqa: BLE001
                status.update(label="Analysis failed", state="error")
                st.error(str(e))
                st.session_state.pop("session", None)

    session = st.session_state.get("session")
    if session is None:
        st.stop()

    agent = make_agent()
    doc = session.document
    items = doc.item_map()
    secs = doc.section_map()

    for w in doc.warnings:
        st.warning(w)
    for n in session.plan.notes:
        st.info(n)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sections (أبواب)", len(doc.sections))
    c2.metric("BOQ items", len(doc.items))
    c3.metric("Work packages", len(session.plan.work_packages))
    pending = [p for p in session.plan.proposals if p.decision == "pending"]
    c4.metric("Special cases pending", len(pending))

    # ------------------------------------------------------------ special cases
    if session.plan.proposals:
        st.subheader("🔔 Special cases — your approval is needed")
        for p in session.plan.proposals:
            icon = {"pending": "🟠", "approved": "🟢", "denied": "🔴"}[p.decision]
            with st.container(border=True):
                st.markdown(f"**{icon} {p.title_en}**  ·  <span class='rtl'>{p.title_ar}</span>", unsafe_allow_html=True)
                st.write(p.message_en)
                st.markdown(f"<div class='rtl'>{p.message_ar}</div>", unsafe_allow_html=True)
                with st.expander(f"Affected items ({len(p.item_uids)})"):
                    st.dataframe(pd.DataFrame([{
                        "Item": items[u].item_no, "Section": truncate(secs[items[u].section_uid].title, 40),
                        "Description": truncate(items[u].full_description, 160), "Unit": items[u].unit,
                        "Qty": items[u].quantity, "Why": items[u].reason} for u in p.item_uids]),
                        hide_index=True, use_container_width=True)
                b1, b2, b3 = st.columns([1, 1, 4])
                if b1.button("✅ Approve", key=f"a_{p.proposal_id}", disabled=p.decision == "approved"):
                    st.session_state.session = agent.decide(session, p.proposal_id, "approved")
                    st.rerun()
                if b2.button("❌ Deny", key=f"d_{p.proposal_id}", disabled=p.decision == "denied"):
                    st.session_state.session = agent.decide(session, p.proposal_id, "denied")
                    st.rerun()
                b3.caption(f"Approve → **{p.approve_label}**   ·   Deny → **{p.deny_label}**")

    # ------------------------------------------------------------ plan preview
    st.subheader("📦 Work packages")
    rows = []
    for wp in session.plan.work_packages:
        rows.append({"WP": wp.wp_id, "Work package": wp.name_en, "الباقة": wp.name_ar,
                     "Division": f"{wp.division} {wp.division_name}" if wp.division else "",
                     "Items": len(wp.item_uids),
                     "Item nos.": ", ".join(i for i in (items[u].item_no for u in wp.item_uids[:12]) if i),
                     "Special rules": ", ".join(wp.rule_ids), "Notes": " ".join(wp.notes)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    # ------------------------------------------------------------ review / corrections
    with st.expander("🛠 Review & correct the agent (divisions and item classification)"):
        st.markdown("**Section → CSI division**")
        div_opts = ["(unknown)"] + [f"{c} {d['name_en']}" for c, d in agent.rules.divisions.items()]
        for s in doc.sections:
            cur = f"{s.division} {agent.rules.division_name(s.division)}" if s.division else "(unknown)"
            col_a, col_b = st.columns([3, 2])
            col_a.markdown(f"{truncate(s.title, 80)}  \n<small>{len(s.item_uids)} items · {s.division_method} "
                           f"{s.division_confidence:.2f}</small>", unsafe_allow_html=True)
            new = col_b.selectbox("Division", div_opts, index=div_opts.index(cur) if cur in div_opts else 0,
                                  key=f"div_{s.uid}", label_visibility="collapsed")
            if new != cur:
                code = None if new == "(unknown)" else new.split()[0]
                with st.spinner("Re-classifying this section …"):
                    st.session_state.session = agent.set_section_division(session, s.uid, code)
                st.rerun()

        st.markdown("**Item → work-package category** (edit the *Category* column, then press *Apply*)")
        df = pd.DataFrame([{
            "uid": it.uid, "Item": it.item_no, "Section": truncate(secs[it.section_uid].title, 30),
            "Description": truncate(it.full_description, 120), "Category": it.category or "NONE",
            "Conf.": round(it.confidence, 2), "By": it.method, "Reason": it.reason} for it in doc.items])
        all_cats = ["NONE", MISC] + list(agent.rules.categories)
        edited = st.data_editor(df, hide_index=True, use_container_width=True, disabled=[c for c in df.columns if c != "Category"],
                                column_config={"uid": None, "Category": st.column_config.SelectboxColumn(options=all_cats)},
                                key="item_editor")
        if st.button("Apply category changes"):
            changed = edited[edited["Category"] != df["Category"]]
            for _, r in changed.iterrows():
                session = agent.set_item_category(session, r["uid"], None if r["Category"] == "NONE" else r["Category"])
            st.session_state.session = session
            st.rerun()

    with st.expander("🧾 Agent log"):
        st.code("\n".join(session.log), language=None)

    # ------------------------------------------------------------ export
    st.subheader("📄 Generate work-package PDFs")
    if pending:
        st.warning(f"{len(pending)} special case(s) above still need Approve / Deny.")
    if st.button("✂️ Cut out work packages", type="primary", disabled=bool(pending)):
        prog = st.progress(0.0, text="Rendering …")
        with st.status("Rendering snapshots …", expanded=True) as status:
            log_box = st.empty()
            ag = make_agent(prog, log_box)
            try:
                st.session_state.session = ag.export(session)
                status.update(label="Done", state="complete", expanded=False)
            except Exception as e:  # noqa: BLE001
                status.update(label="Export failed", state="error")
                st.exception(e)
        st.rerun()

    if session.status == "exported" and session.zip_file:
        z = Path(session.zip_file)
        st.success(f"Exported {len(session.plan.work_packages)} work package(s) to `{session.output_dir}`")
        st.download_button("⬇️ Download all (ZIP)", z.read_bytes(), file_name=z.name, mime="application/zip", type="primary")
        cols = st.columns(3)
        for i, wp in enumerate(session.plan.work_packages):
            if wp.pdf_file and Path(wp.pdf_file).exists():
                f = Path(wp.pdf_file)
                cols[i % 3].download_button(f"{wp.wp_id} · {truncate(wp.name_en, 38)}", f.read_bytes(),
                                            file_name=f.name, mime="application/pdf", key=f"dl_{wp.wp_id}",
                                            use_container_width=True)
