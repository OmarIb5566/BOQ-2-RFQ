"""Minimal Ollama chat client with JSON-schema constrained output."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests


class LLMError(RuntimeError):
    pass


_STATUS: dict[tuple[str, str], tuple[float, tuple[bool, str]]] = {}
_STATUS_TTL = 30.0      # s — the UI asks on every edit; Ollama's model list rarely changes
_http = threading.local()


def _session() -> requests.Session:
    """One keep-alive HTTP session per thread (a new TCP connection per call costs time on every request)."""
    s = getattr(_http, "s", None)
    if s is None:
        s = _http.s = requests.Session()
    return s


def normalize_url(url: str) -> str:
    """'localhost' -> '127.0.0.1'. On Windows 'localhost' tries IPv6 first; Ollama listens on IPv4 only, so
    every new connection waited ~2 s for the fallback (measured: 2.1 s vs 0.08 s per call)."""
    parts = urlsplit(url.rstrip("/"))
    if parts.hostname == "localhost":
        netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


class OllamaClient:
    def __init__(self, url: str, model: str, temperature: float = 0.0, num_ctx: int = 16384, timeout: int = 900):
        self.url = normalize_url(url)
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.calls = 0
        self.seconds = 0.0
        self.log: list[dict] = []      # one entry per call: op, model, tokens in/out, seconds, images
        self.keep_alive: str | int = "30m"   # how long Ollama keeps the model loaded after a call

    def status(self) -> tuple[bool, str]:
        hit = _STATUS.get((self.url, self.model))
        if hit and time.time() - hit[0] < _STATUS_TTL:
            return hit[1]
        res = self._status()
        _STATUS[(self.url, self.model)] = (time.time(), res)
        return res

    def _status(self) -> tuple[bool, str]:
        try:
            r = _session().get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return False, f"Ollama not reachable at {self.url}: {e}"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if self.model not in names and f"{self.model}:latest" not in names:
            return False, f"Model '{self.model}' is not pulled. Run: ollama pull {self.model}"
        return True, f"{self.model} ready"

    def chat_json(self, system: str, user: str, schema: dict, think: bool = False,
                  images: Optional[list[str]] = None, retries: int = 2, op: str = "",
                  max_tokens: Optional[int] = None) -> dict:
        msg = {"role": "user", "content": user}
        if images:
            msg["images"] = images
        payload = {
            "model": self.model,
            "stream": False,
            "think": think,
            "format": schema,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx,
                        **({"num_predict": max_tokens} if max_tokens else {})},
            "messages": ([{"role": "system", "content": system}] if system else []) + [msg],
        }
        last = None
        for attempt in range(retries + 1):
            t0 = time.time()
            try:
                r = _session().post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
                r.raise_for_status()
                js = r.json()
                content = js["message"]["content"]
                dt = time.time() - t0
                self.calls += 1
                self.seconds += dt
                self.log.append({"op": op, "model": self.model, "in": js.get("prompt_eval_count", 0),
                                 "out": js.get("eval_count", 0), "s": round(dt, 2),
                                 "load_s": round(js.get("load_duration", 0) / 1e9, 2), "images": len(images or [])})
                return _parse_json(content)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1 + attempt)
        raise LLMError(f"LLM call failed after {retries + 1} attempts: {last}")


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise
