"""Shared helpers: the document reference data and a content-addressed cache."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..text_utils import keyword_hits, norm

from ..ingest.intake import IMG_EXT  # noqa: E402,F401  (one list of image types)


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def key_of(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class Cache:
    """JSON results keyed by content hash + every setting that changes the answer.
    Lives in <workspace>/_cache/<namespace>/ so a re-uploaded file costs nothing."""

    def __init__(self, root: Path, force: bool = False):
        self.root = Path(root)
        self.force = force
        self.hits = 0

    def get(self, ns: str, key: str) -> Optional[Any]:
        if self.force:
            return None
        p = self.root / ns / f"{key}.json"
        if p.exists():
            try:
                self.hits += 1
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None
        return None

    def put(self, ns: str, key: str, value: Any) -> None:
        p = self.root / ns / f"{key}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


class DocRules:
    def __init__(self, path: Path):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        self.raw = d
        self.name_hints = {k: [norm(w) for w in v] for k, v in d.get("file_name_hints", {}).items()}
        self.kinds = {k: {"keywords": [norm(w) for w in v.get("keywords", [])],
                          "codes": [c.upper() for c in v.get("codes", [])]} for k, v in d.get("sheet_kinds", {}).items()}
        self.disciplines = {k: {"name": v.get("name", k), "codes": [c.upper() for c in v.get("codes", [])],
                                "keywords": [norm(w) for w in v.get("keywords", [])]}
                            for k, v in d.get("disciplines", {}).items()}
        self.layout = d.get("layout_sheets", [])
        self.number_labels = [norm(w) for w in d.get("drawing_number_labels", [])]
        self.title_labels = [norm(w) for w in d.get("sheet_title_labels", [])]
        self.boilerplate_share = float(d.get("boilerplate_share", 0.5))

    def name_hint(self, filename: str) -> Optional[str]:
        """'spec' / 'drawing' / 'boq' from the file name, or None. Word-like matching ('plan' ≠ 'planning')."""
        t = " " + re.sub(r"[_\-.()\[\]]+", " ", norm(Path(filename).stem)) + " "
        for kind in ("boq", "spec", "drawing"):
            for w in self.name_hints.get(kind, []):
                if re.search(rf"(^|[^\w]){re.escape(w)}($|[^\w])", t):
                    return kind
        return None

    def sheet_kind(self, title: str, number: str) -> str:
        t = norm(title)
        for k, v in self.kinds.items():
            if keyword_hits(t, v["keywords"]):
                return k
        toks = [x.upper() for x in re.split(r"[-_.\s/]+", number or "") if x]
        for k, v in self.kinds.items():
            if any(tok in v["codes"] for tok in toks[1:] or toks):
                return k
        return ""

    def discipline(self, title: str, number: str, text: str = "") -> str:
        toks = [x.upper() for x in re.split(r"[-_.\s/]+", number or "") if x]
        for i, tok in enumerate(toks[:3]):
            for k, v in self.disciplines.items():
                # one-letter codes (A-101, S-201) only count as the first part of the number
                if tok in v["codes"] and (len(tok) > 1 or i == 0):
                    return k
        t = norm(f"{title} {text[:4000]}")
        best, n_best = "", 0
        for k, v in self.disciplines.items():
            n = len(keyword_hits(t, v["keywords"]))
            if n > n_best:
                best, n_best = k, n
        return best


@lru_cache(maxsize=4)
def load_doc_rules(path: str) -> DocRules:
    return DocRules(Path(path))
