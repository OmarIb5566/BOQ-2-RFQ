"""Browser check: a scratch data folder, the real server + inline worker, headless Edge over DevTools.
English + Arabic, desktop + mobile, light + dark. Reports console errors, CSP violations, requests to other sites and
pages that scroll sideways; screenshots go to a temporary folder (printed at the end).

    python tools/browser_check/browser_check.py .        (needs Microsoft Edge + `websockets`)
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from cdp import Browser  # noqa: E402

ROOT = Path(sys.argv[1])
import tempfile  # noqa: E402
_TMP = Path(tempfile.mkdtemp(prefix="boq-browser-check-"))
OUT = _TMP / "shots"
DATA = _TMP / "data"
shutil.rmtree(OUT, ignore_errors=True); OUT.mkdir()
shutil.rmtree(DATA, ignore_errors=True)
sys.path.insert(0, str(ROOT / "tests"))
from conftest import make_xlsx  # noqa: E402

boq = make_xlsx(_TMP / "Villa BOQ.xlsx")
PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
env = {**{k: v for k, v in os.environ.items() if not k.startswith("BOQ_")}, "BOQ_WORKSPACE": str(DATA / "workspace"),
       "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
srv = subprocess.Popen([sys.executable, "server.py", "--port", str(PORT), "--no-browser"], cwd=ROOT, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
b = None
sideways = []


def check_width(page, url):
    w = page.js("[document.documentElement.scrollWidth, window.innerWidth]")
    if w[0] > w[1] + 1:
        sideways.append(f"{url}: {w[0]}px > {w[1]}px")


try:
    setup = None
    for _ in range(100):
        m = re.search(r"(http://127\.0\.0\.1:\d+/setup\?token=\S+)", srv.stdout.readline())
        if m:
            setup = m.group(1); break
    assert setup
    time.sleep(1)
    b = Browser(_TMP / "edge-profile", port=9334)
    p = b.page()
    p.goto(setup)
    p.act_and_wait("""(() => { const f = document.querySelector('form'); f.full_name.value = 'Omar Admin';
        f.username.value = 'omar'; f.password.value = 'phase-all-pass'; f.password2.value = 'phase-all-pass';
        f.querySelector('button').click(); })()""")
    p.shot(OUT / "d01-projects-tour.png")
    p.js("document.querySelector('[data-tour-done]')?.click()"); b.idle(1)
    p.goto(BASE + "/new"); p.set_files("#files", [boq]); b.idle(0.5)
    p.act_and_wait("document.getElementById('go').click()")
    run = re.search(r"/p/([^/]+)/", p.url()).group(1)
    for _ in range(90):
        if re.search(r"/p/[^/]+/(cases|packages|rfq)$", p.url()):
            break
        b.idle(1); p.goto(f"{BASE}/p/{run}/")
    print("analysed:", p.url())
    # approve all cases, export (pricing sheets), so the register pages have content later
    p.goto(f"{BASE}/p/{run}/cases")
    p.js("""fetch(location.pathname.replace('/p/', '/api/p/').replace('/cases', '/decide'), {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF': document.querySelector('meta[name=csrf]').content},
        body: JSON.stringify({ids: [...document.querySelectorAll('[data-post*="/decide"]')].flatMap(x => JSON.parse(x.dataset.body || '{}').ids || []), decision: 'approved'})})""")
    b.idle(2)
    p.goto(f"{BASE}/p/{run}/export")
    p.js("document.querySelector(\"[data-post$='/export']\")?.click()"); b.idle(12)
    pages = [("projects", "/projects"), ("packages", f"/p/{run}/packages"), ("cases", f"/p/{run}/cases"),
             ("items", f"/p/{run}/items"), ("export", f"/p/{run}/export"), ("rfq", f"/p/{run}/rfq"),
             ("activity", f"/p/{run}/activity"), ("package", f"/p/{run}/package/DIV03"),
             ("members", f"/p/{run}/members"), ("revisions", f"/p/{run}/revisions"), ("print", f"/p/{run}/print"),
             ("search", "/search?q=concrete"), ("register", "/register"), ("help", "/help"),
             ("admin-health", "/admin/health"), ("admin-queue", "/admin/queue"), ("admin-models", "/admin/models"),
             ("admin-audit", "/admin/audit"), ("admin-usage", "/admin/usage"), ("admin-settings", "/admin/settings")]
    for name, url in pages:
        p.goto(BASE + url); b.idle(0.3)
        p.shot(OUT / f"en-{name}.png", full=True)
        check_width(p, url)
    # a comment with the package's comment box
    p.goto(f"{BASE}/p/{run}/package/DIV03")
    p.js("""(() => { const box = document.querySelector('[data-comments]'); box.querySelector('textarea').value = 'Please check the slab thickness @omar';
        box.querySelector('form button').click(); })()""")
    b.idle(2)
    p.shot(OUT / "en-package-comment.png", full=True)
    print("comments shown:", p.js("document.querySelectorAll('.comment').length"))
    # Arabic
    p.act_and_wait("""(() => { const f = [...document.querySelectorAll('form')].find(x => x.action.includes('/lang/ar')); f.submit(); })()""")
    for name, url in pages[:8] + [("register", "/register"), ("admin-health", "/admin/health"), ("search", "/search?q=خرسانه")]:
        p.goto(BASE + url); b.idle(0.3)
        p.shot(OUT / f"ar-{name}.png", full=True)
        check_width(p, url)
    print("rtl:", p.js("document.documentElement.dir"))
    p.js("document.documentElement.dataset.theme='dark'"); b.idle(0.3); p.shot(OUT / "ar-dark-search.png")
    # mobile (same session, Arabic + English)
    m = b.page(390, 844, mobile=True, scale=2, context=p.ctx)
    for name, url in [("projects", "/projects"), ("packages", f"/p/{run}/packages"), ("register", "/register"),
                      ("admin", "/admin/health"), ("package", f"/p/{run}/package/DIV03")]:
        m.goto(BASE + url); b.idle(0.3)
        m.shot(OUT / f"m-ar-{name}.png", full=True)
        check_width(m, "mobile " + url)
    # second user for presence + conflict
    q = b.page()
    q.goto(BASE + "/login"); q.shot(OUT / "login.png")
finally:
    events = b.events if b else []
    if b:
        b.close()
    srv.terminate()
    out = srv.communicate(timeout=20)[0]

problems, external = [], set()
for e in events:
    mth, prm = e["method"], e.get("params", {})
    if mth == "Runtime.consoleAPICalled" and prm.get("type") in ("error", "warning", "assert"):
        problems.append("console: " + " ".join(str(a.get("value", a.get("description", ""))) for a in prm["args"]))
    elif mth == "Runtime.exceptionThrown":
        problems.append("exception: " + prm["exceptionDetails"].get("exception", {}).get("description", "")[:300])
    elif mth == "Log.entryAdded" and prm["entry"]["level"] in ("error", "warning"):
        en = prm["entry"]
        problems.append(f"log[{en.get('source')}]: {en.get('text', '')[:250]} {en.get('url', '')}")
    elif mth == "Network.requestWillBeSent":
        u = prm["request"]["url"]
        if not u.startswith((BASE, "data:", "blob:", "about:")):
            external.add(u)
print("SERVER TRACEBACK:", "Traceback" in out)
if "Traceback" in out:
    print(out[-3000:])
print("EXTERNAL REQUESTS:", sorted(external) or "none")
print("SIDEWAYS SCROLL:", sideways or "none")
print("PROBLEMS:", len(problems))
for x in problems:
    print("  ", x)
print("SHOTS:", len(list(OUT.iterdir())), OUT)
