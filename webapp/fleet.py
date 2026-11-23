"""The AI fleet: which local model does which job, and how calls to them are made.

Roles                     default model       used for
    text                  gemma4:e4b          divisions, item categories, review, document matching
    vision                qwen2.5vl:7b        drawing sheets / spec pages without a text layer
    embed                 bge-m3              meaning vectors: search by meaning, memory of similar items
    verify                qwen3:14b           independent second opinion on doubtful items (off by default)

Profiles
    staged     one GPU: before a role's first call in a job, the models of the other roles are unloaded, so only
               one model sits in GPU memory at a time (slower switches, no out-of-memory)
    resident   several GPUs / a big GPU: every model stays loaded (keep_alive = -1)

Every call goes through FleetClient:
    * a fallback chain per role (the next model is used when one is not installed or keeps failing)
    * at most N parallel calls per role (BOQ_PARALLEL_<ROLE>, default 1)
    * a response cache keyed by the EXACT request (model, prompts, schema, options, images) — temperature is 0, so a
      re-run of unchanged inputs costs nothing
    * one row per call in the llm_calls table (role, model, tokens, seconds, cached, errors)

Only local models are offered: names ending in ':cloud' or served from another host are never selectable.
The administrator changes the models on Admin › AI models (stored in app_settings['models']).
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
import threading
import time
from typing import Optional

from boq_agent.llm.client import LLMError, OllamaClient, _session, normalize_url

from .db import new_id, now
from .tasks import AFTER_ANALYSIS

ROLES = ["text", "vision", "embed", "verify"]
DEFAULTS = {"text": os.getenv("BOQ_LLM_MODEL", "gemma4:e4b"), "vision": os.getenv("BOQ_VISION_MODEL", "qwen2.5vl:7b"),
            "embed": os.getenv("BOQ_EMBED_MODEL", "bge-m3"), "verify": os.getenv("BOQ_VERIFY_MODEL", "qwen3:14b")}
ROLE_HELP = {
    "text": "Reads the BOQ: divisions, item categories, review of doubtful items, document matching.",
    "vision": "Reads drawing sheets and spec pages that have no text layer (scans, photos).",
    "embed": "Turns item descriptions into meaning vectors (search by meaning, memory of similar items).",
    "verify": "Gives an independent second opinion on doubtful items (when switched on).",
}


def config(db) -> dict:
    c = {"profile": os.getenv("BOQ_MODEL_PROFILE", "staged"), "cache_on": True, "memory_on": True,
         "verify_on": False, "fallbacks": {}}
    c.update({r: DEFAULTS[r] for r in ROLES})
    c.update(db.setting("models", {}) or {})
    return c


# ---------------------------------------------------------------------- model server
_TAGS: dict = {}


def installed(url: str, ttl: float = 30) -> list[dict]:
    """Local models on the model server (cloud / remote models removed)."""
    url = normalize_url(url)
    hit = _TAGS.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        r = _session().get(f"{url}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m for m in r.json().get("models", []) if is_local(m)]
    except Exception:  # noqa: BLE001
        models = []
    _TAGS[url] = (time.time(), models)
    return models


def is_local(m: dict) -> bool:
    name = m.get("name", "")
    return not (name.endswith(":cloud") or "-cloud" in name.split(":")[-1] or m.get("remote_host")
                or m.get("remote_model"))


def installed_names(url: str) -> set[str]:
    names = set()
    for m in installed(url):
        n = m.get("name", "")
        names.add(n)
        if n.endswith(":latest"):
            names.add(n[:-7])
    return names


def fleet_status(url: Optional[str] = None, db=None) -> list[dict]:
    from flask import current_app
    db = db or current_app.config["DB"]
    url = url or current_app.config["SETTINGS"].ollama_url
    c = config(db)
    names = installed_names(url)
    out = []
    for r in ROLES:
        chain = [c[r], *c.get("fallbacks", {}).get(r, [])]
        have = [m for m in chain if m in names]
        optional = r == "verify" and not c.get("verify_on")
        ok = True if have else (None if optional else False)
        if not names:
            detail = f"model server not reachable at {normalize_url(url)}"
            ok = None if optional else False
        elif have:
            detail = f"{have[0]}" + (" (fallback)" if have[0] != c[r] else "") + (
                " · not switched on" if optional else "")
        else:
            detail = f"{c[r]} is not installed — run: ollama pull {c[r]}"
        out.append({"role": r, "ok": ok, "detail": detail})
    return out


_SEMS: dict[str, threading.BoundedSemaphore] = {}
_SEM_LOCK = threading.Lock()


def role_semaphore(role: str) -> threading.BoundedSemaphore:
    with _SEM_LOCK:
        if role not in _SEMS:
            _SEMS[role] = threading.BoundedSemaphore(max(1, int(os.getenv(f"BOQ_PARALLEL_{role.upper()}", "1"))))
        return _SEMS[role]


def unload_others(url: str, keep: str) -> list[str]:
    """Staged profile: free the GPU for `keep` by unloading every other loaded model."""
    url = normalize_url(url)
    try:
        loaded = [m.get("name") for m in _session().get(f"{url}/api/ps", timeout=5).json().get("models", [])]
    except Exception:  # noqa: BLE001
        return []
    gone = []
    for m in loaded:
        if m and m != keep and m != f"{keep}:latest":
            try:
                _session().post(f"{url}/api/generate", json={"model": m, "keep_alive": 0}, timeout=30)
                gone.append(m)
            except Exception:  # noqa: BLE001
                pass
    return gone


class FleetClient(OllamaClient):
    """OllamaClient with fallbacks, a per-role parallel cap, an exact-request cache and a persistent call log."""

    def __init__(self, url: str, chain: list[str], role: str, db=None, cache=None, job_id: str = "",
                 run_id: str = "", profile: str = "staged", temperature: float = 0.0, num_ctx: int = 16384,
                 timeout: int = 900):
        super().__init__(url, chain[0], temperature, num_ctx, timeout)
        self.chain = [m for m in dict.fromkeys(chain) if m]
        self.role, self.db, self.cache = role, db, cache
        self.job_id, self.run_id, self.profile = job_id, run_id, profile
        self.keep_alive = -1 if profile == "resident" else "5m"
        self._staged = False
        self.cache_hits = 0

    def status(self) -> tuple[bool, str]:
        names = installed_names(self.url)
        if not names:
            return super().status()
        for m in self.chain:
            if m in names:
                self.model = m
                return True, f"{m} ready" + (" (fallback)" if m != self.chain[0] else "")
        return False, f"None of {', '.join(self.chain)} is installed. Run: ollama pull {self.chain[0]}"

    def _key(self, system, user, schema, think, images, max_tokens) -> str:
        img = [hashlib.sha256(i.encode() if isinstance(i, str) else i).hexdigest() for i in images or []]
        raw = json.dumps([self.model, system, user, schema, bool(think), img, max_tokens, self.temperature,
                          self.num_ctx], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def chat_json(self, system: str, user: str, schema: dict, think: bool = False,
                  images: Optional[list[str]] = None, retries: int = 2, op: str = "",
                  max_tokens: Optional[int] = None) -> dict:
        names = installed_names(self.url)
        chain = [m for m in self.chain if not names or m in names] or self.chain
        last: Optional[Exception] = None
        for model in chain:
            self.model = model
            key = self._key(system, user, schema, think, images, max_tokens)
            if self.cache is not None:
                hit = self.cache.get("llm", key)
                if hit is not None:
                    self.cache_hits += 1
                    self._record(op, 0, 0, 0.0, cached=True)
                    return hit
            if self.profile == "staged" and not self._staged:
                unload_others(self.url, model)
                self._staged = True
            n0 = len(self.log)
            t0 = time.time()
            try:
                with role_semaphore(self.role):
                    ans = super().chat_json(system, user, schema, think, images, retries, op, max_tokens)
            except LLMError as e:
                last = e
                self._record(op, 0, 0, time.time() - t0, ok=False, error=str(e)[:300])
                continue
            entry = self.log[n0] if len(self.log) > n0 else {}
            self._record(op, entry.get("in", 0), entry.get("out", 0), entry.get("s", time.time() - t0))
            if self.cache is not None:
                self.cache.put("llm", key, ans)
            return ans
        raise LLMError(f"All models for '{self.role}' failed: {last}")

    def _record(self, op, tin, tout, secs, cached=False, ok=True, error=""):
        if self.db is None:
            return
        try:
            self.db.x("INSERT INTO llm_calls (id, ts, job_id, run_id, role, model, op, tokens_in, tokens_out, seconds, "
                      "cached, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (new_id(), now(), self.job_id, self.run_id, self.role, self.model, op or "", int(tin or 0),
                       int(tout or 0), round(float(secs or 0), 3), int(cached), int(ok), error))
        except Exception:  # noqa: BLE001  (the log must never break a call)
            pass


def client_for(role: str, settings, db, ctx=None, cache=None) -> FleetClient:
    c = config(db)
    chain = [c[role], *c.get("fallbacks", {}).get(role, [])]
    return FleetClient(settings.ollama_url, chain, role, db=db, cache=cache,
                       job_id=getattr(ctx, "job_id", ""), run_id=getattr(ctx, "run_id", ""),
                       profile=c["profile"], temperature=settings.temperature, num_ctx=settings.num_ctx,
                       timeout=settings.request_timeout)


def attach_fleet(agent, ctx) -> None:
    """Give an engine run the fleet: the fleet client for the text role + the classification hooks."""
    rt = ctx.runtime
    c = config(rt.db)
    from boq_agent.docs.common import Cache
    cache = Cache(rt.settings.workspace / "_cache", force=agent.settings.force) if c.get("cache_on", True) else None
    agent.llm = client_for("text", agent.settings, rt.db, ctx, cache)
    if c.get("memory_on", True):
        agent.classify_hooks.append(lambda a, doc, clf, secs: apply_memory(rt.db, doc, clf, secs, a.log))
    if c.get("verify_on"):
        verifier = client_for("verify", agent.settings, rt.db, ctx, cache)
        agent.classify_hooks.append(lambda a, doc, clf, secs: second_opinion(verifier, doc, clf, secs, a.log))


# ---------------------------------------------------------------------- memory of user corrections
def norm_text(s: str) -> str:
    from boq_agent.text_utils import norm
    return " ".join(norm(s or "").split())[:600]


def remember_correction(db, user_id, run_id: str, item, category: Optional[str]) -> None:
    """A user put an item in a category: next time the SAME text is classified, the user's answer is used."""
    text = item.full_description
    db.x("INSERT INTO corrections (id, ts, user_id, run_id, kind, text, norm, value) VALUES (?,?,?,?,?,?,?,?)",
         (new_id(), now(), user_id, run_id, "item_category", text[:2000], norm_text(text), category or ""))


def apply_memory(db, doc, clf, sections, log=print) -> int:
    items = doc.item_map()
    n = 0
    for s in doc.sections:
        if sections is not None and s.uid not in sections:
            continue
        allowed = set(clf.allowed_categories(s))
        for uid in s.item_uids:
            it = items[uid]
            if it.method in ("per_item", "manual"):
                continue
            r = db.one("SELECT value, ts FROM corrections WHERE kind='item_category' AND norm=? ORDER BY ts DESC "
                       "LIMIT 1", (norm_text(it.full_description),))
            if r is None:
                continue
            val = r["value"] or None
            if val is not None and val not in allowed:
                continue
            if it.category != val:
                it.category = val
                n += 1
            it.method, it.confidence = "memory", 0.97
            it.reason = f"A user chose this for the same text on {r['ts'][:10]}."
    if n:
        log(f"Memory: {n} item(s) set from earlier user corrections of the same text.")
    return n


# ---------------------------------------------------------------------- second opinion
def second_opinion(verifier: FleetClient, doc, clf, sections, log=print) -> dict:
    """Ask a DIFFERENT model about the doubtful items; disagreements are flagged for the user (never silently
    changed): confidence is lowered and the reason names the other model's answer."""
    import copy

    from boq_agent.analysis.classifier import Classifier
    ok, _ = verifier.status()
    if not ok:
        return {"checked": 0}
    items = doc.item_map()
    thr = clf.settings.review_threshold
    from boq_agent.config import Settings
    vs = Settings(**{**clf.settings.__dict__, "use_llm": True})     # the verifier always asks its model
    other = Classifier(clf.rules, vs, verifier, lambda m: None)
    checked = disputed = 0
    for s in doc.sections:
        if sections is not None and s.uid not in sections:
            continue
        doubt = [items[u] for u in s.item_uids if items[u].method.startswith("llm") and items[u].confidence < thr]
        if not doubt:
            continue
        allowed = clf.allowed_categories(s)
        copies = [copy.deepcopy(it) for it in doubt]
        for i in range(0, len(copies), clf.settings.batch_size):
            other._classify_batch(s, copies[i:i + clf.settings.batch_size], allowed)
        for it, cp in zip(doubt, copies):
            checked += 1
            if cp.method == "llm-missing":
                continue
            if (cp.category or None) != (it.category or None):
                disputed += 1
                it.confidence = min(it.confidence, 0.5)
                it.method = it.method + "+disputed"
                it.reason = f"Second opinion ({verifier.model}): {cp.category or 'stays in its section'}. " + it.reason
    if checked:
        log(f"Second opinion: {checked} doubtful item(s) checked, {disputed} disputed (flagged for review).")
    return {"checked": checked, "disputed": disputed}


# ---------------------------------------------------------------------- meaning vectors
def embed(settings, db, texts: list[str], batch: int = 64) -> Optional[list[list[float]]]:
    c = config(db)
    model = c["embed"]
    if model not in installed_names(settings.ollama_url):
        return None
    url = normalize_url(settings.ollama_url)
    out: list[list[float]] = []
    t0 = time.time()
    for i in range(0, len(texts), batch):
        r = _session().post(f"{url}/api/embed", json={"model": model, "input": texts[i:i + batch],
                                                     "keep_alive": -1 if c["profile"] == "resident" else "5m"},
                            timeout=300)
        r.raise_for_status()
        out += r.json()["embeddings"]
    db.x("INSERT INTO llm_calls (id, ts, role, model, op, seconds) VALUES (?,?,?,?,?,?)",
         (new_id(), now(), "embed", model, f"embed {len(texts)}", round(time.time() - t0, 3)))
    return out


def pack(v: list[float]) -> bytes:
    a = array.array("f", v)
    n = math.sqrt(sum(x * x for x in a)) or 1.0
    return array.array("f", (x / n for x in a)).tobytes()


def unpack(b: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(b)
    return a


def index_vectors(runtime, run_id: str, session) -> int:
    db = runtime.db
    items = session.document.items
    if not items:
        return 0
    try:
        vecs = embed(runtime.settings, db, [it.full_description[:1000] for it in items])
    except Exception:  # noqa: BLE001  (the embedding model is optional)
        vecs = None
    if not vecs:
        return 0
    model = config(db)["embed"]
    with db.tx() as c:
        c.execute("DELETE FROM item_vectors WHERE run_id=?", (run_id,))
        c.executemany("INSERT INTO item_vectors (run_id, uid, model, vector) VALUES (?,?,?,?)",
                      [(run_id, it.uid, model, pack(v)) for it, v in zip(items, vecs)])
    return len(vecs)


AFTER_ANALYSIS.append(index_vectors)


def semantic_search(runtime, run_ids: list[str], query: str, limit: int = 30) -> Optional[list[tuple[float, str, str]]]:
    """[(score, run_id, uid)] by meaning, best first; None when the embedding model is not available."""
    db = runtime.db
    try:
        q = embed(runtime.settings, db, [query])
    except Exception:  # noqa: BLE001
        q = None
    if not q:
        return None
    qv = unpack(pack(q[0]))
    model = config(db)["embed"]
    scored = []
    for chunk in range(0, len(run_ids), 400):
        ids = run_ids[chunk:chunk + 400]
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        for r in db.q(f"SELECT run_id, uid, vector FROM item_vectors WHERE model=? AND run_id IN ({marks})",
                      (model, *ids)):
            v = unpack(r["vector"])
            if len(v) != len(qv):
                continue
            scored.append((sum(a * b for a, b in zip(qv, v)), r["run_id"], r["uid"]))
    scored.sort(reverse=True)
    return scored[:limit]
