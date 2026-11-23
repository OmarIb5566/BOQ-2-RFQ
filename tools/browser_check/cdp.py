"""Tiny Chrome DevTools Protocol driver for Edge (no Playwright: package downloads are blocked on this network).
Uses the `websockets` package already installed in the project environments."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class Browser:
    def __init__(self, profile: Path, port: int = 9333):
        shutil.rmtree(profile, ignore_errors=True)
        self.proc = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={port}",
                                      f"--user-data-dir={profile}", "--no-first-run", "--disable-extensions",
                                      "--remote-allow-origins=*", "--disable-features=msEdgeSidebarV2",
                                      "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = None
        for _ in range(100):
            try:
                url = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1))[
                    "webSocketDebuggerUrl"]
                break
            except OSError:
                time.sleep(0.2)
        self.ws = connect(url, max_size=None, open_timeout=10)
        self.n = 0
        self.events: list[dict] = []

    def _send(self, method, params=None, session=None) -> int:
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        return self.n

    def _pump(self, timeout: float):
        msg = json.loads(self.ws.recv(timeout=timeout))
        if "method" in msg:
            if msg["method"] == "Page.javascriptDialogOpening":
                self._send("Page.handleJavaScriptDialog", {"accept": True}, msg.get("sessionId"))
            self.events.append(msg)
        return msg

    def call(self, method, params=None, session=None, timeout=60):
        mid = self._send(method, params, session)
        end = time.time() + timeout
        while True:
            msg = self._pump(max(0.1, end - time.time()))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def wait_event(self, name, session, timeout=30, since=0):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events[since:]:
                if e["method"] == name and e.get("sessionId") == session:
                    return e
            try:
                self._pump(0.2)
            except TimeoutError:
                pass
        raise TimeoutError(name)

    def idle(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                self._pump(max(0.05, end - time.time()))
            except TimeoutError:
                pass

    def page(self, width=1366, height=860, mobile=False, scale=1, context=None):
        ctx = context or self.call("Target.createBrowserContext")["browserContextId"]
        tid = self.call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx})["targetId"]
        sid = self.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        for d in ("Page", "Runtime", "Log", "Network", "DOM"):
            self.call(f"{d}.enable", session=sid)
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                         "deviceScaleFactor": scale, "mobile": mobile}, sid)
        if mobile:
            self.call("Emulation.setTouchEmulationEnabled", {"enabled": True}, sid)
        return Page(self, sid, ctx)

    def close(self):
        try:
            self.call("Browser.close", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.proc.wait(timeout=10)


class Page:
    def __init__(self, b: Browser, sid: str, ctx: str):
        self.b, self.sid, self.ctx = b, sid, ctx

    def goto(self, url):
        mark = len(self.b.events)
        self.b.call("Page.navigate", {"url": url}, self.sid)
        self.b.wait_event("Page.loadEventFired", self.sid, since=mark)
        self.b.idle(0.4)

    def js(self, expr):
        r = self.b.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                        self.sid)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))
        return r.get("result", {}).get("value")

    def act_and_wait(self, expr, timeout=60):
        """Run JS that navigates (click / submit) and wait for the next page load."""
        mark = len(self.b.events)
        self.js(expr)
        self.b.wait_event("Page.loadEventFired", self.sid, timeout=timeout, since=mark)
        self.b.idle(0.4)

    def url(self):
        return self.js("location.href")

    def set_files(self, selector, paths):
        root = self.b.call("DOM.getDocument", session=self.sid)["root"]["nodeId"]
        node = self.b.call("DOM.querySelector", {"nodeId": root, "selector": selector}, self.sid)["nodeId"]
        self.b.call("DOM.setFileInputFiles", {"files": [str(p) for p in paths], "nodeId": node}, self.sid)

    def shot(self, path, full=False):
        self.b.call("Page.bringToFront", session=self.sid)
        params = {"format": "png"}
        if full:
            m = self.b.call("Page.getLayoutMetrics", session=self.sid)["cssContentSize"]
            params.update(captureBeyondViewport=True,
                          clip={"x": 0, "y": 0, "width": m["width"], "height": min(m["height"], 6000), "scale": 1})
        data = self.b.call("Page.captureScreenshot", params, self.sid)["data"]
        Path(path).write_bytes(base64.b64decode(data))
