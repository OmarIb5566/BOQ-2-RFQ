"""Smoke tests of the real entry points (unit tests do not catch start-up crashes). Temporary folders only."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import ROOT, make_xlsx


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path):
    e = {k: v for k, v in os.environ.items() if not k.startswith("BOQ_")}
    e.update(BOQ_WORKSPACE=str(tmp_path / "data" / "workspace"), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return e


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def test_server_starts_and_prints_the_one_time_setup_link(env, tmp_path):
    port = free_port()
    p = subprocess.Popen([sys.executable, "server.py", "--port", str(port), "--no-browser"], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        opener = urllib.request.build_opener(NoRedirect)
        status, loc = None, ""
        for _ in range(100):
            try:
                r = opener.open(f"http://127.0.0.1:{port}/", timeout=2)
                status = r.status
                break
            except urllib.error.HTTPError as e:
                status, loc = e.code, e.headers.get("Location", "")
                break
            except OSError:
                if p.poll() is not None:
                    break
                time.sleep(0.2)
        assert status == 302 and loc.endswith("/setup"), (status, loc)
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(f"http://127.0.0.1:{port}/setup", timeout=5)
        assert e.value.code == 403                                            # locked without the token
        token = (tmp_path / "data" / "instance" / "setup.token").read_text(encoding="utf-8").strip()
        r = opener.open(f"http://127.0.0.1:{port}/setup?token={token}", timeout=5)
        assert r.status == 200 and "Content-Security-Policy" in r.headers
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0]
    assert f"http://127.0.0.1:{port}/setup?token=" in out, out
    assert "Traceback" not in out, out


def test_cli_runs_keyword_mode(env, tmp_path):
    boq = make_xlsx(tmp_path / "villa.xlsx")
    r = subprocess.run([sys.executable, "cli.py", str(boq), "--no-llm", "--approve-all", "--renderer", "html"],
                       cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    runs = [d for d in (tmp_path / "data" / "workspace").iterdir() if (d / "session.json").exists()]
    assert len(runs) == 1
