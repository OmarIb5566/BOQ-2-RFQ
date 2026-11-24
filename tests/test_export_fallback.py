"""Export never fails because Microsoft Excel could not open a workbook: it retries, then uses the built-in renderer."""
from __future__ import annotations

import contextlib
import types

import pytest

from conftest import make_xlsx


class _Books:
    def __init__(self):
        self.calls = 0

    def Open(self, *a, **kw):  # noqa: N802  (Excel's name)
        self.calls += 1
        return None            # what Excel does behind a hidden prompt: no error, no workbook


@pytest.fixture
def planned(settings, tmp_path):
    from boq_agent.agent import BOQAgent as Agent
    agent = Agent(settings)
    s = agent.analyze([make_xlsx(tmp_path / "villa.xlsx")])
    return agent, s


def test_excel_returning_nothing_falls_back(planned, monkeypatch):
    agent, s = planned
    books = _Books()
    started = []

    @contextlib.contextmanager
    def fake_excel():
        started.append(1)
        yield types.SimpleNamespace(Workbooks=books)

    import boq_agent.excel_com as xc
    monkeypatch.setattr(xc, "excel_app", fake_excel)
    agent.settings.excel_renderer = "auto"
    for p in s.plan.proposals:
        p.decision = "approved"
    logs = []
    agent.log = logs.append
    agent.export(s)
    out = agent.settings.workspace / s.run_id / "work_packages"
    assert len(list(out.glob("*.pdf"))) == len(s.plan.work_packages) > 0
    assert books.calls == 2 and len(started) == 2          # one retry with a fresh Excel, then the fallback
    assert any("built-in renderer" in m for m in logs)


def test_excel_only_mode_reports_clearly(planned, monkeypatch):
    agent, s = planned

    @contextlib.contextmanager
    def fake_excel():
        yield types.SimpleNamespace(Workbooks=_Books())

    import boq_agent.excel_com as xc
    monkeypatch.setattr(xc, "excel_app", fake_excel)
    agent.settings.excel_renderer = "excel"
    for p in s.plan.proposals:
        p.decision = "approved"
    with pytest.raises(RuntimeError, match="could not open"):
        agent.export(s)
