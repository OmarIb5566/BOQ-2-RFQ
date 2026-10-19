"""Minimal Ollama chat client with JSON-schema constrained output."""
from __future__ import annotations

import json
import re
import time
from typing import Optional

import requests


class LLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, url: str, model: str, temperature: float = 0.0, num_ctx: int = 16384, timeout: int = 900):
        self.url = url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.calls = 0
        self.seconds = 0.0

    def status(self) -> tuple[bool, str]:
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            return False, f"Ollama not reachable at {self.url}: {e}"
        names = [m.get("name", "") for m in r.json().get("models", [])]
        if self.model not in names and f"{self.model}:latest" not in names:
            return False, f"Model '{self.model}' is not pulled. Run: ollama pull {self.model}"
        return True, f"{self.model} ready"

    def chat_json(self, system: str, user: str, schema: dict, think: bool = False,
                  images: Optional[list[str]] = None, retries: int = 2) -> dict:
        msg = {"role": "user", "content": user}
        if images:
            msg["images"] = images
        payload = {
            "model": self.model,
            "stream": False,
            "think": think,
            "format": schema,
            "keep_alive": "30m",
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx},
            "messages": [{"role": "system", "content": system}, msg],
        }
        last = None
        for attempt in range(retries + 1):
            t0 = time.time()
            try:
                r = requests.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
                r.raise_for_status()
                content = r.json()["message"]["content"]
                self.calls += 1
                self.seconds += time.time() - t0
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
