"""BOQ → Work Packages → RFQs (Streamlit UI).

Run:  .venv\\Scripts\\streamlit run BOQ2RFQ.py      (or double-click run_app.bat)
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from boq_agent.agent import BOQAgent
from boq_agent.config import Settings
from boq_agent.knowledge.rules import MISC
from boq_agent.text_utils import truncate

st.set_page_config(page_title="BOQ → RFQ", page_icon="📐", layout="wide")
st.markdown(
    """<style>
    #MainMenu, footer, .stDeployButton {visibility: hidden;}
    .block-container {padding-top: 2rem; max-width: 1200px;}
    .rtl {direction: rtl; text-align: right;}
    .steps {display: flex; gap: .5rem; margin: .25rem 0 1.25rem 0; flex-wrap: wrap;}
    .step {padding: .35rem .9rem; border-radius: 999px; font-size: .85rem;
           border: 1px solid rgba(128,128,128,.35); opacity: .55;}
    .step.done {opacity: .9;}
    .step.now {opacity: 1; font-weight: 600; border-color: #ff4b4b; color: #ff4b4b;}
    .muted {opacity: .65; font-size: .85rem;}
    </style>""",
    unsafe_allow_html=True,
)

settings = Settings()


def make_agent(progress_widget=None) -> BOQAgent:
    def _progress(p: float, msg: str):
        if progress_widget is not None:
            progress_widget.progress(min(max(p, 0.0), 1.0), text=msg)

    return BOQAgent(settings, log=lambda m: None, progress=_progress)


def reset() -> None:
    for k in list(st.session_state):
        del st.session_state[k]


# ------------------------------------------------------------------ header + step tracker
session = st.session_state.get("session")
pending = [p for p in session.plan.proposals if p.decision == "pending"] if session else []
if session is None:
    step = 0
elif pending:
    step = 1
elif session.status != "exported":
    step = 2
else:
    step = 3

head_l, head_r = st.columns([5, 1])
head_l.title("📐 BOQ → RFQ")
if session is not None and head_r.button("↺ New project", width="stretch"):
    reset()
    st.rerun()
labels = ["1 · Upload BOQs", "2 · Decide special cases", "3 · Export work packages", "4 · Send RFQs"]
st.markdown("<div class='steps'>" + "".join(
    f"<span class='step {'now' if i == step else 'done' if i < step else ''}'>{'✓ ' if i < step else ''}{t}</span>"
    for i, t in enumerate(labels)) + "</div>", unsafe_allow_html=True)

tab_wp, tab_rfq = st.tabs(["Work packages", "RFQs"])
if session is None:
    with tab_rfq:
        st.info("Upload and analyze a BOQ first.")

# ======================================================================== WORK PACKAGES
with tab_wp:
    if session is None:
        st.markdown("Upload the project's BOQs — one or several Excel / PDF files (Arabic or English), "
                    "or a .zip with all of them. They are packaged together as one project.")
        ups = st.file_uploader("BOQ files", type=["xlsx", "xlsm", "xls", "xlsb", "pdf", "zip"],
                               accept_multiple_files=True, label_visibility="collapsed")
        if ups and st.button("Analyze", type="primary"):
            if "engine_ok" not in st.session_state:
                st.session_state.engine_ok = BOQAgent(settings).llm.status()[0]
            if not st.session_state.engine_ok:
                st.warning("The analysis engine is offline, so results will be less accurate. "
                           "Start it and analyze again for the best result.")
            tmp_dir = Path(tempfile.mkdtemp())
            paths = []
            for i, up in enumerate(ups):
                dst = tmp_dir / str(i) / Path(up.name).name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(up.getbuffer())
                paths.append(dst)
            prog = st.progress(0.0, text="Reading …")
            try:
                st.session_state.session = make_agent(prog).analyze(paths, [u.name for u in ups])
                st.rerun()
            except Exception as e:  # noqa: BLE001
                prog.empty()
                st.error(f"The upload could not be read: {e}")
        st.stop()

    agent = make_agent()
    doc = session.document
    items = doc.item_map()
    secs = doc.section_map()

    files = doc.file_list()
    fmap = doc.file_map()
    skipped = [w[len("Skipped: "):] for w in doc.warnings if w.startswith("Skipped: ")]
    if doc.multi_file or skipped:
        n_items = {f.id: 0 for f in files}
        for sec in doc.sections:
            n_items[sec.file_id] = n_items.get(sec.file_id, 0) + len(sec.item_uids)
        with st.expander(f"**{len(files)} BOQ(s) in this project**" + (f" · {len(skipped)} file(s) skipped" if skipped else "")):
            for f in files:
                st.markdown(f"📄 {f.filename} <span class='muted'>· {n_items.get(f.id, 0)} items</span>",
                            unsafe_allow_html=True)
            for sk in skipped:
                st.markdown(f"<span class='muted'>⤫ {sk}</span>", unsafe_allow_html=True)
    else:
        st.markdown(f"**{doc.filename}**")
    for w in doc.warnings:
        if not w.startswith("Skipped: "):
            st.caption(f"⚠️ {w}")
    m1, m2, m3 = st.columns(3)
    m1.metric("BOQ items", len(doc.items))
    m2.metric("Work packages", len(session.plan.work_packages))
    m3.metric("Decisions needed", len(pending))

    # ------------------------------------------------------------ special cases
    if session.plan.proposals:
        st.subheader("Special cases")
        st.caption("These items could be grouped differently. Choose for each one.")
        for p in session.plan.proposals:
            icon = {"pending": "🟠", "approved": "🟢", "denied": "⚪"}[p.decision]
            with st.container(border=True):
                st.markdown(f"**{icon} {p.title_en}**")
                st.write(p.message_en)
                st.markdown(f"<div class='rtl muted'>{p.message_ar}</div>", unsafe_allow_html=True)
                with st.expander(f"{len(p.item_uids)} item(s)"):
                    st.dataframe(pd.DataFrame([{
                        "Item": items[u].item_no, "Description": truncate(items[u].full_description, 160),
                        "Unit": items[u].unit, "Qty": items[u].quantity} for u in p.item_uids]),
                        hide_index=True, width="stretch")
                b1, b2 = st.columns(2)
                if b1.button(f"✓ {p.approve_label}", key=f"a_{p.proposal_id}", width="stretch",
                             type="primary" if p.decision == "approved" else "secondary"):
                    st.session_state.session = agent.decide(session, p.proposal_id, "approved")
                    st.rerun()
                if b2.button(f"✗ Keep as {p.deny_label}", key=f"d_{p.proposal_id}", width="stretch",
                             type="primary" if p.decision == "denied" else "secondary"):
                    st.session_state.session = agent.decide(session, p.proposal_id, "denied")
                    st.rerun()

    # ------------------------------------------------------------ work packages
    st.subheader("Work packages")
    for n in session.plan.notes:
        st.caption(f"ℹ️ {n}")
    st.dataframe(pd.DataFrame([{
        "WP": wp.wp_id, "Work package": wp.name_en, "الباقة": wp.name_ar,
        **({"From BOQ": ", ".join(fmap[f].label for f in wp.source_files if f in fmap)} if doc.multi_file else {}),
        "Items": len(wp.item_uids),
        "Item nos.": ", ".join(i for i in (items[u].item_no for u in wp.item_uids[:12]) if i)
                     + (" …" if len(wp.item_uids) > 12 else "")}
        for wp in session.plan.work_packages]), hide_index=True, width="stretch")

    with st.expander("Adjust packaging (optional)"):
        st.markdown("**Divisions**")
        div_opts = ["—"] + [f"Div {c} · {d['name_en']}" for c, d in agent.rules.divisions.items()]
        for s in doc.sections:
            cur = f"Div {s.division} · {agent.rules.division_name(s.division)}" if s.division else "—"
            col_a, col_b = st.columns([3, 2])
            where = f"{fmap[s.file_id].label} · " if doc.multi_file and s.file_id in fmap else ""
            col_a.markdown(f"{truncate(s.title, 80)}  \n<span class='muted'>{where}{len(s.item_uids)} items</span>",
                           unsafe_allow_html=True)
            new = col_b.selectbox("Division", div_opts, index=div_opts.index(cur) if cur in div_opts else 0,
                                  key=f"div_{s.uid}", label_visibility="collapsed")
            if new != cur:
                with st.spinner("Updating …"):
                    st.session_state.session = agent.set_section_division(
                        session, s.uid, None if new == "—" else new.split()[1])
                st.rerun()

        st.markdown("**Items** — change the package of any item, then apply.")
        names = {"NONE": "Stays with its section", MISC: "Other items of the division"}
        for cid in agent.rules.categories:
            names[cid] = agent.rules.wp_names(cid)[0]
        back = {v: k for k, v in names.items()}
        df = pd.DataFrame([{
            "uid": it.uid, "Item": it.item_no, "Section": truncate(secs[it.section_uid].title, 30),
            "Description": truncate(it.full_description, 120), "Package": names.get(it.category or "NONE", it.category)}
            for it in doc.items])
        edited = st.data_editor(df, hide_index=True, width="stretch", key="item_editor",
                                disabled=[c for c in df.columns if c != "Package"],
                                column_config={"uid": None,
                                               "Package": st.column_config.SelectboxColumn(options=list(names.values()))})
        if st.button("Apply changes"):
            for _, r in edited[edited["Package"] != df["Package"]].iterrows():
                cat = back.get(r["Package"], "NONE")
                session = agent.set_item_category(session, r["uid"], None if cat == "NONE" else cat)
            st.session_state.session = session
            st.rerun()

    # ------------------------------------------------------------ export
    st.subheader("Export")
    if session.status == "exported" and session.zip_file and Path(session.zip_file).exists():
        z = Path(session.zip_file)
        c1, c2 = st.columns([1, 2])
        c1.download_button("⬇ Download all PDFs (ZIP)", z.read_bytes(), file_name=z.name, mime="application/zip",
                           type="primary", width="stretch")
        c2.success("Work packages exported. Next: the **RFQs** tab.")
        with st.expander("Individual PDFs"):
            cols = st.columns(3)
            for i, wp in enumerate(session.plan.work_packages):
                if wp.pdf_file and Path(wp.pdf_file).exists():
                    f = Path(wp.pdf_file)
                    cols[i % 3].download_button(f"{wp.wp_id} · {truncate(wp.name_en, 34)}", f.read_bytes(),
                                                file_name=f.name, mime="application/pdf", key=f"dl_{wp.wp_id}",
                                                width="stretch")
        export_label = "Export again"
    else:
        export_label = "Export work-package PDFs"
    if pending:
        st.caption(f"Decide the {len(pending)} special case(s) above first.")
    if st.button(export_label, type="secondary" if session.status == "exported" else "primary", disabled=bool(pending)):
        prog = st.progress(0.0, text="Cutting out the work packages …")
        try:
            st.session_state.session = make_agent(prog).export(session)
        except Exception as e:  # noqa: BLE001
            st.error(f"Export failed: {e}")
            st.stop()
        st.rerun()


# ======================================================================== RFQs
def _set_state(key: str, value: str) -> None:
    st.session_state[key] = value


def _sorted_rows(rows: list[dict], order: dict[str, int]) -> list[dict]:
    return sorted(rows, key=lambda r: (order.get(r["Work package"], 10_000), r["Supplier"] == ""))


def _apply_supplier_edits(key: str, order: dict[str, int]) -> None:
    """Merge the table edits into the stored rows, then keep each package's suppliers together."""
    rows = [dict(r) for r in st.session_state.rfq_rows]
    ch = st.session_state[key]
    for i, upd in ch.get("edited_rows", {}).items():
        rows[int(i)].update({k: (v or "") for k, v in upd.items()})
    for i in sorted((int(i) for i in ch.get("deleted_rows", [])), reverse=True):
        rows.pop(i)
    for add in ch.get("added_rows", []):
        rows.append({"Work package": add.get("Work package") or "", "Supplier": add.get("Supplier") or ""})
    st.session_state.rfq_rows = _sorted_rows(rows, order)       # rows still without a package stay at the end
    st.session_state.rfq_ver = st.session_state.get("rfq_ver", 0) + 1


def render_rfq(session, agent: BOQAgent) -> None:
    from boq_agent.rfq.detect import load_field_config
    from boq_agent.rfq.template import preview

    if session.status != "exported":
        st.info("Export the work packages first — each RFQ is sent with its BOQ cut-out.")
        return

    template = settings.rfq_template
    custom = st.session_state.get("rfq_custom_tpl")
    if custom and Path(custom).exists():
        template = Path(custom)
    key = (session.run_id, str(template))
    if st.session_state.get("rfq_key") != key:
        with st.spinner("Preparing the RFQ …"):
            st.session_state.rfq_pre = agent.rfq_prefill(session, template)
        st.session_state.rfq_key = key
        for k in [k for k in st.session_state if str(k).startswith(("rfq_v_", "rfq_ok_", "rfq_diff_", "rfq_rows", "rfq_ver"))]:
            del st.session_state[k]
    pre = st.session_state.rfq_pre
    date_fmt = load_field_config(settings.rfq_fields).get("date_format", "%d %B %Y")

    # ---------------------------------------------------------------- project details
    st.subheader("Project details")
    values: dict[str, str] = {}
    to_confirm: list[str] = []
    with st.container(border=True):
        for p in pre["project"]:
            s = p.spec
            if s.type == "date" and s.detect.startswith("same_as:"):
                src = s.detect.split(":", 1)[1]
                if st.checkbox(f"Use a different date for “{s.label}”", key=f"rfq_diff_{s.name}"):
                    d = st.date_input(s.label, value=None, key=f"rfq_v_{s.name}", format="DD/MM/YYYY")
                    values[s.name] = d.strftime(date_fmt) if d else ""
                else:
                    values[s.name] = values.get(src, "")
                continue
            c1, c2 = st.columns([1, 3])
            c1.markdown(f"<div style='padding-top:.45rem'>{s.label}</div>", unsafe_allow_html=True)
            if s.type == "date":
                d = c2.date_input(s.label, value=None, key=f"rfq_v_{s.name}", format="DD/MM/YYYY",
                                  label_visibility="collapsed")
                values[s.name] = d.strftime(date_fmt) if d else ""
                continue
            k = f"rfq_v_{s.name}"
            if k not in st.session_state:
                st.session_state[k] = p.value
            v = c2.text_input(s.label, key=k, label_visibility="collapsed").strip()
            values[s.name] = v
            if p.auto and v == p.value:
                cc1, cc2 = c2.columns([3, 1])
                cc1.caption(f"Found in the BOQ ({p.source})" if p.source else "Found in the BOQ")
                if not cc2.checkbox("Confirm", key=f"rfq_ok_{s.name}"):
                    to_confirm.append(s.label)
            elif not p.auto and p.suggestions and not v:
                c2.caption("Possibly, from the BOQ:")
                for i, (sv, _) in enumerate(p.suggestions):
                    c2.button(truncate(sv, 80), key=f"rfq_sug_{s.name}_{i}", on_click=_set_state, args=(k, sv))

    # ---------------------------------------------------------------- suppliers
    st.subheader("Suppliers")
    st.caption("One RFQ per row. Add a row for each extra supplier; delete a row to skip it.")
    wps = {wp.wp_id: wp for wp in session.plan.work_packages}
    label = {pk["wp_id"]: f"{pk['wp_id']} · {wps[pk['wp_id']].name_en}" for pk in pre["packages"]}
    by_label = {v: k for k, v in label.items()}
    order = {v: i for i, v in enumerate(label.values())}
    if "rfq_rows" not in st.session_state:
        st.session_state.rfq_rows = [{"Work package": label[pk["wp_id"]], "Supplier": ""} for pk in pre["packages"]]
    ed_key = f"rfq_sup_{st.session_state.get('rfq_ver', 0)}"
    st.data_editor(pd.DataFrame(st.session_state.rfq_rows, columns=["Work package", "Supplier"]),
                   key=ed_key, num_rows="dynamic", hide_index=True, width="stretch",
                   on_change=_apply_supplier_edits, args=(ed_key, order),
                   column_config={
                       "Work package": st.column_config.SelectboxColumn(options=list(label.values()), required=True,
                                                                        width="large"),
                       "Supplier": st.column_config.TextColumn(width="large")})

    pkg_values = {pk["wp_id"]: pk["values"] for pk in pre["packages"]}
    grouped: dict[str, list[str]] = {}
    for r in st.session_state.rfq_rows:
        wid = by_label.get(r["Work package"])
        if wid:
            grouped.setdefault(wid, []).append(r["Supplier"].strip())
    packages = [{"wp_id": wid, "values": pkg_values[wid], "suppliers": [s for s in sups if s]}
                for wid, sups in sorted(grouped.items(), key=lambda kv: order[label[kv[0]]])]
    sup_fields = pre["supplier_fields"]

    # ---------------------------------------------------------------- generate
    st.subheader("Generate")
    missing = [pre_p.spec.label for pre_p in pre["project"] if not values.get(pre_p.spec.name)]
    no_sup = sum(1 for pk in packages if not pk["suppliers"])
    if to_confirm:
        st.warning("Confirm the values found in the BOQ: " + ", ".join(to_confirm))
    notes = []
    if missing:
        notes.append("empty: " + ", ".join(dict.fromkeys(missing)))
    if no_sup:
        notes.append(f"{no_sup} work package(s) without a supplier name")
    if notes:
        st.caption("These will stay blank in the letter — " + "; ".join(notes) + ".")
    if packages:
        first = packages[0]
        sup = first["suppliers"][0] if first["suppliers"] else ""
        with st.expander(f"Preview · {label[first['wp_id']]}" + (f" · {sup}" if sup else "")):
            st.text("\n".join(t for t in preview(template, {**values, **first["values"],
                                                             **{f: sup for f in sup_fields}}) if t.strip()))
    n_letters = sum(max(1, len(pk["suppliers"])) for pk in packages)
    if st.button(f"Generate {n_letters} RFQ(s)", type="primary", disabled=bool(to_confirm) or not packages):
        with st.spinner("Writing the RFQs …"):
            st.session_state.session = agent.generate_rfqs(session, values, packages, sup_fields, template)
        st.rerun()

    if session.rfq.get("zip") and Path(session.rfq["zip"]).exists():
        z = Path(session.rfq["zip"])
        c1, c2 = st.columns([1, 2])
        c1.download_button("⬇ Download RFQs (ZIP)", z.read_bytes(), file_name=z.name, mime="application/zip",
                           type="primary", width="stretch", key="dl_rfq_zip")
        c2.success(f"{len(session.rfq['files'])} RFQ(s) ready — each with its BOQ cut-out.")

    with st.expander("Use a different RFQ template"):
        up = st.file_uploader("Word template (.docx)", type=["docx"], key="rfq_tpl", label_visibility="collapsed")
        if up is not None:
            dst = settings.workspace / session.run_id / "rfq_template.docx"
            dst.write_bytes(up.getbuffer())
            if st.session_state.get("rfq_custom_tpl") != str(dst) or st.button("Reload template"):
                st.session_state.rfq_custom_tpl = str(dst)
                st.session_state.pop("rfq_key", None)
                st.rerun()
        elif custom:
            st.caption(f"Using: {Path(custom).name}")
            if st.button("Back to the company template"):
                st.session_state.pop("rfq_custom_tpl", None)
                st.session_state.pop("rfq_key", None)
                st.rerun()


with tab_rfq:
    render_rfq(session, agent)
