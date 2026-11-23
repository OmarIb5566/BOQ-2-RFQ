"""Phase 0 engine-side fixes: Excel macros force-disabled, Ollama reached via 127.0.0.1 on kept-alive sessions."""
from __future__ import annotations

import sys
import threading
import types

import pytest

from boq_agent.llm import client as llm


# ---------------------------------------------------------------------- Excel automation
class _FakeExcel:
    def __init__(self, log):
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "Workbooks", [])

    def __setattr__(self, k, v):
        self._log.append((k, v))
        object.__setattr__(self, k, v)

    def Quit(self):
        pass


def test_macros_disabled_before_anything_else(monkeypatch):
    log = []
    fake_client = types.SimpleNamespace(DispatchEx=lambda name: _FakeExcel(log))
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: None,
                                                                        CoUninitialize=lambda: None))
    monkeypatch.setitem(sys.modules, "win32com", types.SimpleNamespace(client=fake_client))
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    from boq_agent.excel_com import excel_app
    with excel_app():
        pass
    assert log[0] == ("AutomationSecurity", 3)          # first property set, before any workbook can be opened


@pytest.mark.skipif(sys.platform != "win32", reason="Microsoft Excel automation is Windows only")
def test_real_excel_accepts_force_disable():
    from boq_agent.excel_com import excel_app
    try:
        cm = excel_app()
        xl = cm.__enter__()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Microsoft Excel not available: {e}")
    try:
        assert xl.AutomationSecurity == 3
    finally:
        cm.__exit__(None, None, None)


# ---------------------------------------------------------------------- Ollama client
@pytest.mark.parametrize("url,want", [
    ("http://localhost:11434", "http://127.0.0.1:11434"), ("http://localhost:11434/", "http://127.0.0.1:11434"),
    ("http://LOCALHOST", "http://127.0.0.1"), ("http://gpu-01:11434", "http://gpu-01:11434"),
    ("http://10.0.0.5:11434", "http://10.0.0.5:11434"),
])
def test_normalize_url(url, want):
    assert llm.normalize_url(url) == want


def test_default_url_is_ipv4():
    from boq_agent.config import Settings
    assert "localhost" not in Settings().ollama_url or "OLLAMA_URL" in __import__("os").environ


def test_one_kept_alive_session_per_thread():
    a, b = llm._session(), llm._session()
    assert a is b
    other = []
    t = threading.Thread(target=lambda: other.append(llm._session()))
    t.start()
    t.join()
    assert other[0] is not a


def test_client_uses_the_session(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": '{"a": 1}'}, "prompt_eval_count": 3, "eval_count": 2}

    class Sess:
        def post(self, url, **kw):
            calls.append(url)
            return Resp()
    monkeypatch.setattr(llm, "_session", lambda: Sess())
    c = llm.OllamaClient("http://localhost:11434", "m")
    assert c.chat_json("", "hi", {"type": "object"}) == {"a": 1}
    assert calls == ["http://127.0.0.1:11434/api/chat"]
