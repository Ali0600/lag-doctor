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
import types
from pathlib import Path

from lagdoctor.app import library
from lagdoctor.app.main import App
from lagdoctor.winapi import webview2 as wv
from tests import app_demo, fake_recorder

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


def activate(on: bool) -> tuple:
    """Tell the window it is (or is no longer) the active one, through its own WM_ACTIVATE branch, which the
    harness calls itself: posted, the message would also reach DefWindowProc, which gives the window the focus."""
    return ("activate", on)


def wait(predicate, seconds: float = STEP_TIMEOUT_S) -> tuple:
    """Wait until predicate() is true, on the window's thread (for what only Python can read, such as the view's
    own IsVisible and IsSuspended)."""
    return ("wait", predicate, seconds)


def window_title(app: App) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(ctypes.c_void_p(app.window.hwnd), buf, 256)
    return buf.value


def view_state(app: App) -> dict:
    """The view as WebView2 itself reports it, beside what the window believes."""
    w = app.window
    return {"visible": w.controller.get_bool("get_IsVisible"), "suspended": w.webview3.get_bool("get_IsSuspended"),
            "hidden": w.view_hidden, "recording": w.recording}


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
    facts["racer_game"] = yield js(f"""(() => ({{
        notes: [...document.querySelectorAll('tr[data-session] .note')].map((n) => n.textContent),
        made: {MADE}, pwned: window.__pwned === 1}}))()""")
    yield js("""const boxes = document.querySelectorAll('tr[data-session] input');
        boxes[0].click(); boxes[1].click(); document.getElementById('compare').click(); true""")
    yield until("!!document.getElementById('compare-table')")
    facts["compare"] = yield js("""[...document.querySelectorAll('#view table tbody tr')].map(
        (tr) => [...tr.children].map((td) => td.textContent))""")
    facts["compare_differs"] = yield js("""[...new Set([...document.querySelectorAll('#view td.differs')].map(
        (td) => td.parentElement.children[0].textContent))]""")
    facts["compare_made"] = yield js(f"({{made: {MADE}, pwned: window.__pwned === 1}})")
    yield shot("compare")
    yield js("document.getElementById('back').click(); true")
    yield until("!!document.getElementById('game-name')")
    facts["back"] = yield js("document.getElementById('game-name').textContent")
    # The newest Demo Racer recording, the one with a note, on Results.
    yield js("document.querySelector('tr[data-session]').click(); true")
    yield until("!!document.querySelector('.verdict')")
    facts["racer_results"] = yield js(f"""(() => ({{
        game: document.querySelector('#view h1').textContent,
        note: document.getElementById('note') ? document.getElementById('note').textContent : null,
        made: {MADE}, pwned: window.__pwned === 1}}))()""")

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

    # Recording from the app (M4), with tests/fake_recorder.py in the recorder's place and a harmless process in the
    # relay's (App's recorder and launcher): the window is told it is the active one, then Record.
    rig = app.harness
    yield activate(True)
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("!!document.getElementById('record')")
    yield js("document.getElementById('record').click(); true")
    yield until("!!document.getElementById('rec-clock') && !!document.getElementById('rec-stop')")
    facts["rec_running"] = yield js(f"""(() => ({{
        title: document.getElementById('rec-title').textContent,
        game: document.getElementById('rec-game').textContent,
        clock: document.getElementById('rec-clock').textContent,
        detail: document.getElementById('rec-detail').textContent,
        bar: !document.getElementById('rec').hidden, made: {MADE}, pwned: window.__pwned === 1}}))()""")
    facts["rec_view_shown"] = view_state(app)
    yield shot("recording")
    yield activate(False)                              # the player goes back to the game
    yield wait(lambda: app.window.suspended is True)
    facts["rec_view_hidden"] = view_state(app)
    facts["rec_title_hidden"] = window_title(app)
    yield activate(True)                               # and comes back to the window
    yield wait(lambda: view_state(app)["visible"] and not view_state(app)["suspended"])
    facts["rec_view_back"] = view_state(app)
    yield until("!!document.getElementById('rec-stop')")
    # The page crashes while the view is hidden: it loads again hidden, is suspended again, and is back on the
    # running screen once the window is shown.
    readies = facts["readies"]
    yield activate(False)
    yield wait(lambda: view_state(app)["suspended"])
    yield ("crash_renderer",)
    yield until_ready(readies + 1)
    yield wait(lambda: view_state(app)["suspended"] and not view_state(app)["visible"])
    facts["rec_after_page_crash"] = view_state(app)
    yield activate(True)
    yield wait(lambda: view_state(app)["visible"] and not view_state(app)["suspended"])
    yield until("!!document.getElementById('rec-stop')")
    facts["rec_after_page_crash_screen"] = yield js("document.getElementById('rec-title').textContent")
    # The browser process ends while the view is hidden: the new environment's view is hidden and suspended too.
    yield activate(False)
    yield wait(lambda: view_state(app)["suspended"])
    yield ("end_browser",)
    yield until_ready(readies + 2, 60.0)
    yield wait(lambda: view_state(app)["suspended"] and not view_state(app)["visible"], 30.0)
    facts["rec_after_browser_exit"] = view_state(app)
    yield activate(True)
    yield wait(lambda: view_state(app)["visible"] and not view_state(app)["suspended"])
    yield until("!!document.getElementById('rec-stop')")
    # The player goes to the Library and the recording ends meanwhile (stopped from there): the bar keeps a way back to
    # the end, and the Library is asked again, since it now holds the new recording.
    yield js("document.querySelector('[data-go=library]').click(); true")
    yield until("document.querySelectorAll('#games .game').length > 0")
    asks_before = len(rig.library_asks)
    yield js("ask('stop_recording'); true")
    yield until("!document.getElementById('rec').hidden && document.getElementById('rec').textContent === "
                "'Recording ended'")
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["rec_left_for_the_library"] = {"library_asked_again": len(rig.library_asks) - asks_before,
                                         "bar": (yield js("document.getElementById('rec').textContent"))}
    yield js("document.getElementById('rec').click(); true")
    yield until("!!document.getElementById('rec-results')")
    facts["rec_done"] = yield js("""(() => ({title: document.getElementById('rec-title').textContent,
        bar: !document.getElementById('rec').hidden,
        notes: [...document.querySelectorAll('#rec-notes li')].map((n) => n.textContent)}))()""")
    yield shot("recording-done")
    # A view of the recording older than the one shown changes nothing (a reply and a push that crossed).
    facts["rec_older_view"] = yield js("""(() => {
        recordingPushed(Object.assign({}, rec, {seq: rec.seq - 1, phase: 'recording', active: true,
                                               title: 'Recording', action: 'stop'}));
        return {title: document.getElementById('rec-title').textContent, bar: !document.getElementById('rec').hidden};
    })()""")
    facts["rec_title_done"] = window_title(app)
    yield js("document.getElementById('rec-results').click(); true")
    yield until("!!document.querySelector('.verdict')")
    facts["rec_results"] = yield js("document.querySelector('#view h1').textContent")
    facts["rec_played"], facts["rec_flashes"] = list(rig.played), list(rig.flashes)
    facts["rec_prompts"] = [{"hwnd_is_the_window": c["hwnd"] == app.window.hwnd, "argv": c["argv"]}
                            for c in rig.prompts]
    yield wait(lambda: rig.report.is_file())
    facts["rec_report"] = json.loads(rig.report.read_text(encoding="utf-8"))

    # Recovery. The renderer crashes: the window loads its page again.
    yield ("crash_renderer",)
    yield until_ready(facts["readies"] + 1)
    yield until("document.querySelectorAll('#games .game').length > 0")
    facts["after_renderer_crash"] = yield js("location.href")
    # The page loaded again after the recording's end: the bar still offers the way back to it, once (its answer to
    # the page's own ask('recording') may come after the library has drawn, so wait for it).
    yield until("!document.getElementById('rec').hidden")
    facts["bar_after_reload"] = yield js("document.getElementById('rec').textContent")
    # The browser process ends: once BrowserProcessExited says the data folder is free, a new environment, with the
    # options object again, and the page again.
    pid = app.window.browser_pid
    yield ("end_browser",)
    yield until_ready(facts["readies"] + 1, 60.0)
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
        elif op[0] == "activate":
            w.on_window_message(w.hwnd, wv.WM_ACTIVATE, 1 if op[1] else wv.WA_INACTIVE, 0)
            w.after(50, lambda: self.resume(None))
        elif op[0] == "wait":
            self.deadline = time.monotonic() + op[2]
            self.wait_for(op[1])
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

    def wait_for(self, predicate) -> None:
        try:
            ok = bool(predicate())
        except Exception:  # noqa: BLE001 - a failed read is a failed wait, said in full
            self.facts["error"] = traceback.format_exc()
            self.finish()
            return
        if ok:
            self.resume(True)
        elif time.monotonic() > self.deadline:
            self.facts["error"] = "timed out waiting for a condition only Python can read"
            self.finish()
        else:
            self.app.window.after(POLL_MS, lambda: self.wait_for(predicate))

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
    # The recording's stand-ins: the recorder records nothing and saves no file, the prompt is a harmless process of
    # this harness's own, and the sounds and the taskbar flash are only noted.
    rig = types.SimpleNamespace(played=[], flashes=[], prompts=[], relays=[], report=home / "fake-recorder-report.json",
                                library_asks=[])
    recordings = [e for e in library.scan(home / "sessions") if e.is_recording]
    # The markup-named game the tests use, else (the README's pictures library) the newest recording's game.
    picked = next((e for e in recordings if e.game == app_demo.MARKUP_TITLE), recordings[0])
    plan = home / "fake-recorder-plan.json"
    plan.write_text(json.dumps({"elevate": True, "game": picked.game, "marks": 2, "session": picked.id,
                                "end": "stop", "report": str(rig.report)}), encoding="utf-8")
    app = App(home=home, runtime=runtime, create_fn=create, log=facts["log"].append, tell=facts["told"].append,
              activate=False, opener=facts["opened"].append,
              recorder=lambda: [str(fake_recorder.BASE_PY), "-B", "-E", "-s", "-m", "tests.fake_recorder", str(plan)],
              launcher=fake_recorder.stand_in_prompt(rig.prompts, rig.relays), play=rig.played.append,
              flash=rig.flashes.append)
    app.harness = rig
    real_library = app.do_library
    app.do_library = lambda: (rig.library_asks.append(1), real_library())[1]     # counts the page's asks
    app.window.notify_state = lambda: wv.QUNS_ACCEPTS_NOTIFICATIONS   # the flash's own wait is tested apart
    from lagdoctor.winapi import elevation

    def no_real_prompt(*args, **kwargs):           # whatever slips, this run never raises Windows' UAC prompt
        raise AssertionError("the harness reached the real UAC prompt")
    elevation.launch_elevated = no_real_prompt
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
        for p in rig.relays:
            p.kill()
        if app.rec is not None and app.rec.proc is not None and app.rec.proc.poll() is None:
            app.rec.proc.kill()                         # a run that failed mid-recording leaves no recorder behind
        out.write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
    return 0 if facts.get("finished") and "error" not in facts else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
