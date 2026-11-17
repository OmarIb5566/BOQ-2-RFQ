/* BOQ → RFQ — small, dependency-free client. Every action is a JSON POST; the page then refreshes only
   the parts that changed (#projhead + #main), so long pages never reload or lose their scroll position. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const CSRF = document.querySelector('meta[name="csrf"]')?.content || "";
  const RUN = document.body.dataset.run || "";

  // ------------------------------------------------------------------ toasts
  function toast(msg, kind = "", ms = 4200) {
    const box = $("#toasts");
    if (!box || !msg) return;
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.innerHTML = `<div class="grow"></div><button class="btn ghost sm" style="color:inherit" aria-label="Close">✕</button>`;
    t.firstChild.textContent = msg;
    t.lastChild.onclick = () => t.remove();
    box.appendChild(t);
    setTimeout(() => t.remove(), ms);
  }
  window.toast = toast;

  // ------------------------------------------------------------------ API
  async function post(url, data, btn) {
    const isForm = data instanceof FormData;
    if (btn) { btn.disabled = true; btn.dataset.label = btn.innerHTML; btn.innerHTML = `<span class="spin"></span> ${btn.textContent.trim()}`; }
    try {
      const r = await fetch(url, {
        method: "POST", headers: isForm ? { "X-CSRF": CSRF } : { "Content-Type": "application/json", "X-CSRF": CSRF },
        body: isForm ? data : JSON.stringify(data || {}),
      });
      let j = {};
      try { j = await r.json(); } catch { j = { ok: false, error: `Server error (${r.status}).` }; }
      if (!j.ok) { toast(j.error || "Something went wrong.", "bad", 7000); return j; }
      if (j.message && !j.silent) toast(j.message, j.job ? "" : "ok");
      if (j.job) watchJobs(true);
      return j;
    } catch (e) {
      toast("Cannot reach the server — is it still running?", "bad", 7000);
      return { ok: false };
    } finally {
      if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = btn.dataset.label; }
    }
  }
  window.post = post;

  // refresh the project header + main area from the server, keeping scroll and open <details>
  async function refresh() {
    const open = $$("details[open][id]").map(d => d.id);
    const filters = $$("[data-filter]").map(i => [i.dataset.filter, i.value]);
    const y = window.scrollY;
    const r = await fetch(location.href, { headers: { "X-Partial": "1" } });
    if (!r.ok) return location.reload();
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    for (const id of ["projhead", "main"]) {
      const a = document.getElementById(id), b = doc.getElementById(id);
      if (a && b) a.replaceWith(b);
    }
    open.forEach(id => { const d = document.getElementById(id); if (d) { d.open = true; d.dispatchEvent(new Event("toggle")); } });
    filters.forEach(([k, v]) => { const i = $(`[data-filter="${k}"]`); if (i) { i.value = v; applyFilter(i); } });
    window.scrollTo(0, y);
    init(document);
  }
  window.refresh = refresh;

  // ------------------------------------------------------------------ declarative actions
  // <button data-post="/api/…" data-body='{"a":1}' data-confirm="Sure?" data-then="refresh|reload|go:URL">
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-post]");
    if (b) {
      e.preventDefault();
      if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
      const j = await post(b.dataset.post, b.dataset.body ? JSON.parse(b.dataset.body) : {}, b);
      if (j.ok) after(b.dataset.then || "refresh", j);
      return;
    }
    const row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a,button,input,select,label")) {
      if (e.ctrlKey || e.metaKey) window.open(row.dataset.href); else location.href = row.dataset.href;
    }
  });
  function after(what, j) {
    if (j.url) return (location.href = j.url);
    if (what === "reload") return location.reload();
    if (what.startsWith("go:")) return (location.href = what.slice(3));
    if (what === "none") return;
    if (!j.job) refresh();
  }

  // ------------------------------------------------------------------ inline rename
  // <span class="editable" data-rename="/api/…" data-key="name" data-extra='{"key":"DIV04"}'>Name</span>
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-rename]");
    if (!el || el.querySelector("input")) return;
    const old = el.textContent.trim();
    const inp = document.createElement("input");
    inp.type = "text"; inp.value = old; inp.className = "sm"; inp.style.minWidth = "280px";
    el.textContent = ""; el.appendChild(inp); inp.focus(); inp.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const v = inp.value.trim();
      el.textContent = save && v ? v : old;
      if (!save || v === old) return;
      const body = { ...(el.dataset.extra ? JSON.parse(el.dataset.extra) : {}), [el.dataset.field || "name"]: v };
      const j = await post(el.dataset.rename, body);
      if (!j.ok) el.textContent = old; else if (el.dataset.then !== "none") refresh();
    };
    inp.addEventListener("keydown", (k) => { if (k.key === "Enter") finish(true); if (k.key === "Escape") finish(false); });
    inp.addEventListener("blur", () => finish(true));
  });

  // ------------------------------------------------------------------ table filter
  // <input data-filter="#tbl"> + optional <select data-filter-col="#tbl" data-col="cat">
  function applyFilter(inp) {
    const tbl = $(inp.dataset.filter);
    if (!tbl) return;
    const words = inp.value.toLowerCase().split(/\s+/).filter(Boolean);
    const sels = $$(`[data-filter-col="${inp.dataset.filter}"]`).filter(s => s.value);
    let shown = 0;
    $$("tbody tr", tbl).forEach(tr => {
      const txt = (tr.dataset.text || tr.textContent).toLowerCase();
      const ok = words.every(w => txt.includes(w)) && sels.every(s => (tr.dataset[s.dataset.col] || "") === s.value);
      tr.classList.toggle("hidden", !ok);
      if (ok) shown++;
    });
    const c = $(`[data-count="${inp.dataset.filter}"]`);
    if (c) c.textContent = shown;
  }
  document.addEventListener("input", (e) => {
    if (e.target.matches("[data-filter]")) applyFilter(e.target);
  });
  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-filter-col]")) {
      const inp = $(`[data-filter="${e.target.dataset.filterCol}"]`);
      if (inp) applyFilter(inp);
    }
  });
  document.addEventListener("keydown", (e) => {   // "/" jumps to the search box
    if (e.key === "/" && !e.target.matches("input,textarea,select")) {
      const s = $("[data-filter], input[type=search]");
      if (s) { e.preventDefault(); s.focus(); }
    }
  });

  // ------------------------------------------------------------------ category picker (one <select> on demand)
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".cat-btn");
    if (!b) return;
    const names = JSON.parse($("#cat-names").textContent);
    const sel = document.createElement("select");
    sel.className = "sm";
    for (const [k, v] of names) {
      const o = document.createElement("option"); o.value = k; o.textContent = v; if (k === b.dataset.cat) o.selected = true;
      sel.appendChild(o);
    }
    b.replaceWith(sel); sel.focus();
    const restore = () => { if (sel.isConnected) sel.replaceWith(b); };
    sel.addEventListener("blur", () => setTimeout(restore, 150));
    sel.addEventListener("change", async () => {
      sel.disabled = true;
      const j = await post(`/api/p/${RUN}/item`, { uid: b.dataset.uid, category: sel.value });
      if (j.ok) refresh(); else restore();
    });
  });

  // ------------------------------------------------------------------ lazy <details>
  function initLazy(root) {
    $$("details[data-lazy]", root).forEach(d => {
      if (d.dataset.bound) return;
      d.dataset.bound = "1";
      d.addEventListener("toggle", async () => {
        if (!d.open || d.dataset.loaded) return;
        d.dataset.loaded = "1";
        const box = $(".lazy-body", d);
        box.innerHTML = '<span class="spin"></span>';
        box.innerHTML = await (await fetch(d.dataset.lazy)).text();
      });
    });
  }

  // ------------------------------------------------------------------ background jobs
  let watching = null, since = null;
  async function pollJobs() {
    try {
      const r = await fetch("/api/jobs" + (since ? `?since=${encodeURIComponent(since)}` : ""));
      if (r.status === 401) return;
      const j = await r.json();
      since = j.now;
      const n = j.active.length;
      const qb = $("#queue-n");
      if (qb) { qb.textContent = n; qb.classList.toggle("hidden", !n); }
      const list = $("#queue-list");
      if (list) list.innerHTML = n ? j.active.map(a => `<a href="/p/${a.run_id}/"><b>${esc(a.label)}</b> · ${esc(a.pname || "")}<br>
          <span class="muted small">${esc(a.message || "")}</span><div class="bar"><i style="width:${Math.round(a.progress * 100)}%"></i></div></a>`).join("")
          : '<div class="muted small" style="padding:.5rem .7rem">No jobs running.</div>';
      // job banner on a project page
      const mine = RUN ? j.active.find(a => a.run_id === RUN) : null;
      const jb = $("#jobbar");
      if (jb) {
        if (mine) {
          jb.classList.remove("hidden");
          $(".jb-label", jb).textContent = mine.label;
          $(".jb-msg", jb).textContent = mine.message || "";
          $(".bar > i", jb).style.width = Math.round(mine.progress * 100) + "%";
        } else if (!jb.classList.contains("hidden")) {
          jb.classList.add("hidden");
        }
      }
      // project list: live progress of running analyses
      j.active.forEach(a => {
        const row = $(`[data-jobrow="${CSS.escape(a.run_id)}"]`);
        if (!row) return;
        $(".bar > i", row).style.width = Math.round(a.progress * 100) + "%";
        $(".jr-msg", row).textContent = `${a.label} — ${a.message || ""}`;
      });
      // a page waiting for a job that is no longer running: show the result
      if (document.body.dataset.pending === "1" && !mine) { setTimeout(() => location.reload(), 600); return; }
      if (!RUN && (j.finished || []).length && $("[data-jobrow]")) setTimeout(() => location.reload(), 800);
      for (const f of j.finished || []) {
        if (f.status === "done") toast(`✓ ${f.label}: ${f.message}`, "ok", 6000);
        else toast(`✗ ${f.label} failed: ${f.error}`, "bad", 12000);
        if (RUN && f.run_id === RUN) {
          if (document.body.dataset.pending === "1") location.reload(); else refresh();
        }
      }
      if (!n && watching && !document.body.dataset.alwaysPoll) { clearInterval(watching); watching = null; }
      else if (n && !watching) watching = setInterval(pollJobs, 2000);
    } catch { /* server restarting */ }
  }
  function watchJobs(now) {
    if (!watching) watching = setInterval(pollJobs, 2000);
    if (now) setTimeout(pollJobs, 400);
  }
  window.watchJobs = watchJobs;
  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ------------------------------------------------------------------ engine status
  async function status() {
    try {
      const j = await (await fetch("/api/status")).json();
      const el = $("#engine");
      if (!el || !j.ok) return;
      const d = $(".dot", el), l = $(".lbl", el);
      d.className = "dot " + (j.llm ? "ok" : "warn");
      l.textContent = j.llm ? "AI ready" : "AI offline — keyword mode";
      el.title = `${j.llm_msg}\nVision: ${j.vision_msg}`;
    } catch { /* ignore */ }
  }

  // ------------------------------------------------------------------ upload drop-zone
  function initDrop() {
    const zone = $("#drop"), input = $("#files"), list = $("#file-list");
    if (!zone) return;
    let files = [];
    const kind = (n) => /\.(xlsx|xlsm|xls|xlsb)$/i.test(n) ? ["xls", "XLS"] : /\.pdf$/i.test(n) ? ["pdf", "PDF"]
      : /\.zip$/i.test(n) ? ["zip", "ZIP"] : ["img", "IMG"];
    const render = () => {
      list.innerHTML = "";
      files.forEach((f, i) => {
        const [c, t] = kind(f.name);
        const row = document.createElement("div");
        row.className = "file-row";
        row.innerHTML = `<span class="ico ${c}">${t}</span><span class="grow"></span><span class="muted small">${(f.size / 1048576).toFixed(1)} MB</span><button type="button" class="btn ghost sm">Remove</button>`;
        row.children[1].textContent = f.name;
        row.lastChild.onclick = () => { files.splice(i, 1); render(); };
        list.appendChild(row);
      });
      const dt = new DataTransfer(); files.forEach(f => dt.items.add(f)); input.files = dt.files;
      $("#go").disabled = !files.length;
      const nm = $("#name");
      if (nm && !nm.dataset.touched && files.length) nm.value = files[0].name.replace(/\.[^.]+$/, "") + (files.length > 1 ? ` +${files.length - 1}` : "");
    };
    const add = (fl) => { for (const f of fl) if (!files.some(x => x.name === f.name && x.size === f.size)) files.push(f); render(); };
    zone.onclick = () => input.click();
    input.onchange = () => add(input.files);
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
    zone.addEventListener("drop", (e) => add(e.dataTransfer.files));
    $("#name")?.addEventListener("input", (e) => (e.target.dataset.touched = "1"));
    $("#upload-form").addEventListener("submit", () => { const b = $("#go"); b.disabled = true; b.innerHTML = '<span class="spin"></span> Uploading …'; });
  }

  // ------------------------------------------------------------------ RFQ form (autosave)
  function initRfq(root) {
    const form = $("#rfq-form", root);
    if (!form || form.dataset.bound) return;
    form.dataset.bound = "1";
    let timer = null;
    const state = () => {
      const values = {}, confirmed = [], diff = {}, suppliers = {}, skip = [];
      $$("[data-field]", form).forEach(i => (values[i.dataset.field] = i.value));
      $$("[data-confirm-field]", form).forEach(i => i.checked && confirmed.push(i.dataset.confirmField));
      $$("[data-diff]", form).forEach(i => (diff[i.dataset.diff] = i.checked));
      $$("[data-wp]", form).forEach(row => {
        suppliers[row.dataset.wp] = $$(".chip", row).map(c => c.dataset.v);
        if (!$("[data-include]", row).checked) skip.push(row.dataset.wp);
      });
      return { values, confirmed, diff_date: diff, suppliers, skip };
    };
    const save = () => {
      clearTimeout(timer);
      const st = $("#save-state");
      if (st) st.textContent = "Saving …";
      timer = setTimeout(async () => {
        const j = await post(`/api/p/${RUN}/rfq/draft`, state());
        if (st) st.textContent = j.ok ? "All changes saved" : "Not saved";
        updateRfqChecks();
      }, 500);
    };
    form.addEventListener("input", save);
    form.addEventListener("change", (e) => {
      if (e.target.matches("[data-diff]")) $(`#diff-${CSS.escape(e.target.dataset.diff)}`)?.classList.toggle("hidden", !e.target.checked);
      save();
    });
    form.addEventListener("click", (e) => {
      const sug = e.target.closest("[data-suggest]");
      if (sug) { const i = $(`[data-field="${CSS.escape(sug.dataset.for)}"]`, form); i.value = sug.dataset.suggest; save(); }
      const x = e.target.closest(".chip button");
      if (x) { x.parentElement.remove(); save(); }
      const all = e.target.closest("[data-all-include]");
      if (all) { $$("[data-include]", form).forEach(c => (c.checked = all.dataset.allInclude === "1")); save(); }
    });
    form.addEventListener("keydown", (e) => {
      if (!e.target.matches(".chip-input")) return;
      if ((e.key === "Enter" || e.key === ",") && e.target.value.trim()) {
        e.preventDefault();
        addChip(e.target, e.target.value.trim()); e.target.value = ""; save();
      } else if (e.key === "Backspace" && !e.target.value) {
        const last = $$(".chip", e.target.parentElement).pop(); if (last) { last.remove(); save(); }
      }
    });
    form.addEventListener("focusout", (e) => {
      if (e.target.matches(".chip-input") && e.target.value.trim()) { addChip(e.target, e.target.value.trim()); e.target.value = ""; save(); }
    });
    // "same supplier for all selected"
    $("#bulk-sup-add", root)?.addEventListener("click", () => {
      const v = $("#bulk-sup").value.trim();
      if (!v) return;
      $$("[data-wp]", form).forEach(row => { if ($("[data-include]", row).checked) addChip($(".chip-input", row), v); });
      $("#bulk-sup").value = ""; save();
    });
    updateRfqChecks();
  }
  function addChip(input, v) {
    if ($$(".chip", input.parentElement).some(c => c.dataset.v.toLowerCase() === v.toLowerCase())) return;
    const c = document.createElement("span");
    c.className = "chip"; c.dataset.v = v;
    c.innerHTML = '<span></span><button type="button" title="Remove">×</button>';
    c.firstChild.textContent = v;
    input.before(c);
  }
  function updateRfqChecks() {
    const form = $("#rfq-form");
    if (!form) return;
    const need = $$("[data-confirm-field]", form).filter(c => {
      const i = $(`[data-field="${CSS.escape(c.dataset.confirmField)}"]`, form);
      const same = i && i.value === c.dataset.detected;
      c.closest(".confirm-box")?.classList.toggle("hidden", !same);
      return same && !c.checked;
    });
    const n = $$("[data-wp]", form).reduce((a, row) => a + ($("[data-include]", row).checked ? Math.max(1, $$(".chip", row).length) : 0), 0);
    const g = $("#gen-btn");
    if (g) {
      g.disabled = !!need.length || !n || g.dataset.blocked === "1";
      g.textContent = `Generate ${n} RFQ letter${n === 1 ? "" : "s"}`;
    }
    const w = $("#need-confirm");
    if (w) { w.classList.toggle("hidden", !need.length); w.querySelector("b").textContent = need.map(c => c.dataset.label).join(", "); }
  }

  // ------------------------------------------------------------------ letter preview
  document.addEventListener("change", async (e) => {
    if (!e.target.matches("#preview-wp")) return;
    showPreview(e.target.value);
  });
  async function showPreview(key) {
    const box = $("#letter");
    if (!box) return;
    const j = await post(`/api/p/${RUN}/rfq/preview`, { wp_key: key });
    if (!j.ok) return;
    box.innerHTML = "";
    j.lines.forEach(t => { const p = document.createElement("p"); p.textContent = t; box.appendChild(p); });
  }
  window.showPreview = showPreview;

  // ------------------------------------------------------------------ misc
  document.addEventListener("submit", async (e) => {    // <form data-ajax> -> multipart POST, then refresh
    const f = e.target;
    if (!f.matches("[data-ajax]")) return;
    e.preventDefault();
    const j = await post(f.action, new FormData(f), $("button[type=submit]", f));
    if (j.ok) after(f.dataset.then || "refresh", j);
  });
  document.addEventListener("click", (e) => {     // close menus on outside click
    $$("details.menu[open]").forEach(m => { if (!m.contains(e.target)) m.open = false; });
  });
  $("#theme-toggle")?.addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const nx = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = nx;
    try { localStorage.setItem("theme", nx); } catch { /* private mode */ }
  });

  function init(root) {
    initLazy(root);
    initRfq(root);
    $$("[data-filter]", root).forEach(i => {
      if (i.value || $$(`[data-filter-col="${i.dataset.filter}"]`).some(s => s.value)) applyFilter(i);
    });
    if (root === document) {
      const pv = $("#preview-wp");
      if (pv) showPreview(pv.value);
    }
  }
  init(document);
  initDrop();
  status();
  setInterval(status, 60000);
  pollJobs();
  if (document.body.dataset.alwaysPoll) watchJobs();
})();
