"""Drive lag-doctor's real window through the host's own script hook (ExecuteScript), in a child process.

tests/test_app_page.py runs this on a demo library (tests/app_demo.py) and reads what it wrote. The window opens
without taking the focus and closes itself; nothing is typed or clicked outside the page: each step runs script in the
page (a click is the page's own element.click()) and waits for the page to show the result. WebDriver for WebView2
has been broken since runtime 133 (research note, section 3), so the app's own hook is the driver.

    python -m tests.app_harness <home> <out.json> [<shots folder>]    # LAGDOCTOR_HOME must name <home> too

With a shots folder, each screen is also saved as a PNG there, through WebView2's own CapturePreview: what the page
drew, without the window's frame (the README's pictures come from this, on the demo library).
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from lagdoctor.app.main import App
from lagdoctor.winapi import webview2 as wv

STEP_TIMEOUT_S = 30.0
RUN_TIMEOUT_S = 150.0
POLL_MS = 100
# Test-only: DevTools' Page.crash ends the page's renderer, as a crash would (WebView2.h 1.0.4314.35:
# ICoreWebView2::CallDevToolsProtocolMethod is slot 36; its completion handler's IID below). The app never calls it.
CALL_DEVTOOLS_SLOT = 36
DEVTOOLS_DONE = "ICoreWebView2CallDevToolsProtocolMethodCompletedHandler"
wv.IID.setdefault(DEVTOOLS_DONE, wv.guid("5C4889F0-5EF6-4C5A-952C-D8F1B92D0574"))
# Test-only too: ICoreWebView2::CapturePreview is slot 30, PNG is format 0, and its handler's Invoke takes one HRESULT.
CAPTURE_PREVIEW_SLOT = 30
SHOT_DONE = "ICoreWebView2CapturePreviewCompletedHandler"
wv.IID.setdefault(SHOT_DONE, wv.guid("697E05E9-3D8F-45FA-96F4-8FFE1EDEDAF5"))
DONE_HR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long)
STGM_WRITE, STGM_SHARE_DENY_WRITE, STGM_CREATE, FILE_ATTRIBUTE_NORMAL = 0x1, 0x20, 0x1000, 0x80
_shlwapi = ctypes.WinDLL("shlwapi")
_shlwapi.SHCreateStreamOnFileEx.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int,
                                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
_shlwapi.SHCreateStreamOnFileEx.restype = ctypes.c_long


def js(code: str) -> tuple:
    return ("js", code)


def until(code: str, seconds: float = STEP_TIMEOUT_S) -> tuple:
    return ("until", code, seconds)


def sleep(ms: int) -> tuple:
    return ("sleep", ms)


def shot(name: str) -> tuple:
    """Save what the page shows now as <shots>/<name>.png (nothing without a shots folder)."""
    return ("shot", name)


def until_ready(n: int, seconds: float = STEP_TIMEOUT_S) -> tuple:
    """Wait until the page has said it is ready n times (each load of it says so once)."""
    return ("ready", n, seconds)


CLICK_GAME = ("[...document.querySelectorAll('#games .game')].find((b) => "
              "b.querySelector('.name').textContent.includes({name})).click(); true")
MADE = "document.querySelectorAll('#view img, #view b, #view script, #view iframe').length"


def script(facts: dict, app: App):
    """The steps, as a generator: each yield is an operation, and what it sends back is the step's answer."""
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["library"] = yield js(f"""(() => ({{
        names: [...document.querySelectorAll('#games .game .name')].map((n) => n.textContent),
        recent: document.querySelectorAll('.recent .game').length,
        strip: document.querySelector('.strip').textContent,
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield shot("library")

    yield js(CLICK_GAME.format(name=json.dumps("Quest")))
    yield until("!!document.getElementById('game-name')")
    facts["game"] = yield js(f"""(() => ({{
        name: document.getElementById('game-name').textContent,
        rows: document.querySelectorAll('tr[data-session]').length,
        last: document.querySelectorAll('.last-tag').length, made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield shot("game")

    yield js("document.querySelector('tr[data-session]').click(); true")
    yield until("!!document.querySelector('.verdict') && !!document.querySelector('[data-chart=frames] canvas')")
    facts["results"] = yield js(f"""(() => ({{
        game: document.querySelector('#view h1').textContent,
        verdict: document.querySelector('.verdict .badge').textContent,
        findings: document.querySelectorAll('.finding').length,
        frames_drawn: charts.length ? charts[0].data[0].length : -1,
        charts: document.querySelectorAll('#view canvas').length,
        events: document.querySelectorAll('tr[data-event]').length,
        commands: [...document.querySelectorAll('.fix code')].map((c) => c.textContent),
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield shot("results")
    yield js("document.getElementById('view').scrollTop = document.querySelector('[data-chart=frames]').offsetTop - 70; "
             "true")
    yield sleep(300)
    yield shot("results-charts")

    yield js("document.querySelector('tr[data-event]').click(); true")
    yield until("!!document.querySelector('#moment .moment')")
    facts["moment"] = yield js("""(() => ({
        title: document.querySelector('#moment h3').textContent,
        charts: document.querySelectorAll('#moment canvas').length,
        causes: document.querySelector('#moment p').textContent}))()""")
    yield sleep(300)
    yield shot("moment")

    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    yield js(CLICK_GAME.format(name=json.dumps("Demo Racer")))
    yield until("document.querySelectorAll('tr[data-session] input').length >= 2")
    facts["racer_results_commands"] = yield js("document.querySelectorAll('.last-tag').length")
    yield js("""const boxes = document.querySelectorAll('tr[data-session] input');
        boxes[0].click(); boxes[1].click(); document.getElementById('compare').click(); true""")
    yield until("!!document.getElementById('compare-table')")
    facts["compare"] = yield js("""[...document.querySelectorAll('#view table tbody tr')].map(
        (tr) => [...tr.children].map((td) => td.textContent))""")
    yield shot("compare")
    yield js("document.getElementById('back').click(); true")
    yield until("!!document.getElementById('game-name')")
    facts["back"] = yield js("document.getElementById('game-name').textContent")

    yield js("document.querySelector('[data-go=settings]').click(); true")
    yield until("[...document.querySelectorAll('#view h1')].some((h) => h.textContent === 'Settings')")
    facts["settings"] = yield js("""(() => ({
        folder: document.querySelector('#view .row code') ? document.querySelector('#view .row code').textContent : null,
        text: document.getElementById('view').textContent}))()""")
    yield shot("settings")

    # The policy, from inside the page: a new window, a navigation away and malformed messages are turned away.
    facts["window_open"] = yield js("window.open('https://example.com/') === null")
    yield js("location.href = 'https://example.com/'; true")
    yield sleep(1500)
    facts["href_after"] = yield js("location.href")
    yield js("""chrome.webview.postMessage({type: 'request', id: 9001, kind: 'results', session: '..\\\\..\\\\x'});
        chrome.webview.postMessage({type: 'request', id: 9002, kind: 'unknown'});
        chrome.webview.postMessage({type: 'request', id: 9003, kind: 'library', extra: 1}); true""")
    yield sleep(800)
    facts["refused"] = dict(app.window.refused)
    facts["settings_readback"] = dict(app.window.settings_readback)
    facts["options_asked"] = sorted(set(app.window.options_asked[0])) if app.window.options_asked else []
    facts["env_restored"] = app.window.env_restored

    # Recovery. The renderer crashes: the window loads its page again.
    yield ("crash_renderer",)
    yield until_ready(2)
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["after_renderer_crash"] = yield js("location.href")
    # The browser process ends: once BrowserProcessExited says the data folder is free, a new environment, with the
    # options object again, and the page again.
    pid = app.window.browser_pid
    yield ("end_browser",)
    yield until_ready(3, 60.0)
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["after_browser_exit"] = {"generation": app.window.generation, "old_pid": pid,
                                   "new_pid": app.window.browser_pid,
                                   "options_asked_again": sorted(set(app.window.options_asked[-1])),
                                   "env_restored": app.window.env_restored,
                                   "names": (yield js("[...document.querySelectorAll('#games .game .name')]"
                                                      ".map((n) => n.textContent)"))}


class Driver:
    def __init__(self, app: App, facts: dict, shots: Path | None = None) -> None:
        self.app = app
        self.shots = shots
        self.facts = facts
        self.gen = None
        self.deadline = 0.0
        self.readies = 0

    def start(self, app: App) -> None:
        self.readies += 1
        self.facts["readies"] = self.readies
        if self.gen is None:
            self.gen = script(self.facts, app)
            self.resume(None)

    def resume(self, value) -> None:
        try:
            op = self.gen.send(value)
        except StopIteration:
            self.finish()
            return
        except Exception:  # noqa: BLE001
            self.facts["error"] = traceback.format_exc()
            self.finish()
            return
        self.facts["steps"] = self.facts.get("steps", 0) + 1
        w = self.app.window
        if op[0] == "js":
            w.execute_script(op[1], lambda hr, res: self.resume(self.decode(hr, res)))
        elif op[0] == "sleep":
            w.after(op[1], lambda: self.resume(None))
        elif op[0] == "shot":
            self.take_shot(op[1])
        elif op[0] == "ready":
            self.deadline = time.monotonic() + op[2]
            self.wait_ready(op[1])
        elif op[0] == "crash_renderer":
            done = w.handler(DEVTOOLS_DONE, wv.DONE_STR, lambda hr, res: None)
            hr = wv.vcall(w.webview.ptr, CALL_DEVTOOLS_SLOT, (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p),
                          "Page.crash", "{}", done.ptr)
            self.facts["crash_call"] = f"0x{hr & 0xFFFFFFFF:08X}"
            w.after(50, lambda: self.resume(None))
        elif op[0] == "end_browser":
            done = subprocess.run(["taskkill", "/F", "/PID", str(w.browser_pid)], capture_output=True,
                                  timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.facts["end_browser"] = done.returncode
            w.after(50, lambda: self.resume(None))
        else:
            self.deadline = time.monotonic() + op[2]
            self.poll(op[1])

    def decode(self, hr: int, res: str | None):
        if hr < 0:
            return {"hresult": f"0x{hr & 0xFFFFFFFF:08X}"}
        try:
            return json.loads(res) if res else None
        except ValueError:
            return res

    def take_shot(self, name: str) -> None:
        if self.shots is None:
            self.resume(None)
            return
        w = self.app.window
        stream = ctypes.c_void_p()
        path = self.shots / f"{name}.png"
        hr = _shlwapi.SHCreateStreamOnFileEx(str(path), STGM_CREATE | STGM_WRITE | STGM_SHARE_DENY_WRITE,
                                             FILE_ATTRIBUTE_NORMAL, 1, None, ctypes.byref(stream))
        if hr < 0:
            self.facts.setdefault("shots_failed", []).append(f"{name}: 0x{hr & 0xFFFFFFFF:08X}")
            self.resume(None)
            return

        def done(hr, _):
            wv.vcall(stream.value, 2, (), restype=ctypes.c_ulong)          # Release: the file is complete
            if hr < 0:
                self.facts.setdefault("shots_failed", []).append(f"{name}: 0x{hr & 0xFFFFFFFF:08X}")
            w.call_soon(lambda: self.resume(None))
        h = wv.Handler(SHOT_DONE, DONE_HR, done, guard=lambda iface, fn: (lambda this, hr: (fn(hr, None), 0)[1]))
        w.keep.append(h)
        hr = wv.vcall(w.webview.ptr, CAPTURE_PREVIEW_SLOT, (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p), 0,
                      stream.value, h.ptr)
        if hr < 0:
            wv.vcall(stream.value, 2, (), restype=ctypes.c_ulong)
            self.facts.setdefault("shots_failed", []).append(f"{name}: 0x{hr & 0xFFFFFFFF:08X}")
            self.resume(None)

    def wait_ready(self, n: int) -> None:
        if self.readies >= n:
            self.resume(True)
        elif time.monotonic() > self.deadline:
            self.facts["error"] = f"the page was not ready a {n}th time"
            self.finish()
        else:
            self.app.window.after(POLL_MS, lambda: self.wait_ready(n))

    def poll(self, code: str) -> None:
        def check(hr, res):
            if self.decode(hr, res) is True:
                self.resume(True)
            elif time.monotonic() > self.deadline:
                self.facts["error"] = f"timed out waiting for: {code}"
                self.finish()
            else:
                self.app.window.after(POLL_MS, lambda: self.poll(code))
        self.app.window.execute_script(f"(() => {{ try {{ return !!({code}); }} catch (e) {{ return false; }} }})()",
                                       check)

    def finish(self) -> None:
        self.facts["finished"] = True
        self.app.window.begin_close()


def main(argv: list[str]) -> int:
    home, out = Path(argv[0]), Path(argv[1])
    shots = Path(argv[2]) if len(argv) > 2 else None
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    if Path(os.environ.get("LAGDOCTOR_HOME", "")).resolve() != home.resolve():
        print("LAGDOCTOR_HOME must name the same home", file=sys.stderr)
        return 2
    facts: dict = {"log": [], "told": [], "opened": []}
    runtime = wv.find_runtime()
    if runtime is None:
        print("no WebView2 Runtime", file=sys.stderr)
        return 3
    create = wv.environment_creator(runtime["dll"])
    wv.SetProcessDpiAwarenessContext(wv.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    wv.CoInitializeEx(None, wv.COINIT_APARTMENTTHREADED)
    app = App(home=home, runtime=runtime, create_fn=create, log=facts["log"].append, tell=facts["told"].append,
              activate=False, opener=facts["opened"].append)
    driver = Driver(app, facts, shots)
    app.on_ready = driver.start

    def give_up():
        if not app.window.closing:
            facts["error"] = f"the run took over {RUN_TIMEOUT_S:.0f} s"
            app.window.begin_close()
    app.window.create_window()
    app.window.after(int(RUN_TIMEOUT_S * 1000), give_up)
    try:
        app.window.create_environment()
        app.window.loop()
    finally:
        facts["fatal"] = app.window.fatal
        app.window.destroy_icons()
        out.write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
    return 0 if facts.get("finished") and "error" not in facts else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
