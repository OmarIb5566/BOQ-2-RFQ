"""One documented settings file for the whole installation: boq2rfq.toml (see boq2rfq.example.toml).

Every key is the name of an environment variable, grouped in sections only for readability:

    [ai]
    OLLAMA_URL = "http://127.0.0.1:11434"

Environment variables that are already set win over the file. The file is looked for at $BOQ_CONFIG, then
<project folder>/boq2rfq.toml. It is read once, before the settings are created.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOADED: dict[str, str] = {}          # key -> value taken from the file (for the admin console)
SOURCE: list[str] = []


def config_path() -> Path:
    return Path(os.getenv("BOQ_CONFIG") or PROJECT_ROOT / "boq2rfq.toml")


def load(path: Path | None = None) -> dict[str, str]:
    p = Path(path) if path else config_path()
    if not p.exists():
        return {}
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    flat: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                flat[k2] = v2
        else:
            flat[k] = v
    out = {}
    for k, v in flat.items():
        k = str(k).upper()
        if not k.replace("_", "").isalnum():
            continue
        val = ("1" if v else "0") if isinstance(v, bool) else str(v)
        if k not in os.environ:
            os.environ[k] = val
            out[k] = val
    LOADED.update(out)
    SOURCE[:] = [str(p)]
    return out


load()
