"""lag-doctor's window, driven for real: the page on a demo library through the host's own script hook
(tests/app_harness.py), plus what the page's files must hold (its CSP, the vendored uPlot and its licence, the icon).

The window opens for a few seconds without taking the focus, in a child process, and closes itself.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lagdoctor.app import ASSETS, ICON
from lagdoctor.app import main as app_main
from lagdoctor.app import window as app_window
from lagdoctor.winapi import webview2 as wv
from tests import app_demo

ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_S = 240
# uPlot 1.6.32 from npm (registry.npmjs.org/uplot/-/uplot-1.6.32.tgz, its published SHA-512 checked 2026-11-08):
# the three files lagdoctor/app/assets/vendor/ holds, byte for byte.
UPLOT = {"uPlot.iife.min.js": "19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f",
         "uPlot.min.css": "df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04",
         "uPlot-LICENSE.txt": "8f989229699b4fe2f1a0432d0e9edc338a8a911e250e2d1b01ecd770a5f5b1bd"}


# ------------------------------------------------------------------------------------------- the real page --

@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    home = tmp_path_factory.mktemp("app-page-home")
    built = app_demo.build(home)
    out = home / "harness.json"
    env = dict(os.environ, LAGDOCTOR_HOME=str(home))
    done = subprocess.run([sys.executable, "-m", "tests.app_harness", str(home), str(out)], cwd=ROOT, env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=CHILD_TIMEOUT_S)
    facts = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    facts["_exit"], facts["_stderr"], facts["_built"] = done.returncode, done.stderr[-3000:], built
    return facts


def _ok(run: dict) -> None:
    assert run["_exit"] == 0 and run.get("finished") and "error" not in run, (
        run.get("error"), run["_stderr"], run.get("log"))


@pytest.mark.needs("webview2")
def test_the_window_opens_on_the_library_and_lists_each_recorded_game_once(run):
    _ok(run)
    lib = run["library"]
    assert lib["names"] == [app_demo.MARKUP_TITLE, "Demo Racer", "starplanner"]     # the check-up is left out
    assert lib["recent"] == 0 and lib["strip"]                       # 3 games: no Recent row repeating them
    assert run["told"] == [] and run["fatal"] == []


@pytest.mark.needs("webview2")
def test_a_game_and_a_process_named_like_markup_appear_as_text(run):
    # The demo game's title is '<img src=x onerror=...><b>Bold</b> Quest': as text it shows those characters; as
    # markup it would make an <img> and a <b> (and the CSP would still stop the handler). No screen made either.
    _ok(run)
    for screen in ("library", "game", "results"):
        assert run[screen]["made"] == 0 and run[screen]["pwned"] is False, screen
    assert run["game"]["name"] == app_demo.MARKUP_TITLE and run["results"]["game"] == app_demo.MARKUP_TITLE


@pytest.mark.needs("webview2")
def test_results_draw_every_frame_handed_over_in_the_shared_buffer(run):
    _ok(run)
    expected = next(f["frames"] for f in run["_built"].values() if f["demo"].title == app_demo.MARKUP_TITLE)
    r = run["results"]
    assert r["frames_drawn"] == expected and expected > 1000
    assert r["verdict"] == "Stutter" and r["findings"] >= 1 and r["events"] >= 1 and r["charts"] >= 2


@pytest.mark.needs("webview2")
def test_the_fix_command_shows_on_the_last_recording_only(run):
    _ok(run)
    assert run["results"]["commands"] and all(c.startswith("lagdoctor fix ") for c in run["results"]["commands"])
    assert run["game"]["last"] == 1 and run["racer_results_commands"] == 0


@pytest.mark.needs("webview2")
def test_a_lag_moment_opens_with_its_charts(run):
    _ok(run)
    assert run["moment"]["title"] and run["moment"]["charts"] >= 2


@pytest.mark.needs("webview2")
def test_compare_puts_two_recordings_side_by_side_and_back_returns_to_the_game(run):
    _ok(run)
    rows = {r[0]: r[1:] for r in run["compare"]}
    assert rows["Verdict"] == ["No lag found", "Stutter"] and rows["Refresh rate"] == ["144 Hz", "144 Hz"]
    assert run["back"] == "Demo Racer"


@pytest.mark.needs("webview2")
def test_settings_name_the_sessions_folder_and_the_lag_key(run):
    _ok(run)
    assert run["settings"]["folder"].endswith("sessions") and "Ctrl+Alt+L" in run["settings"]["text"]


@pytest.mark.needs("webview2")
def test_the_page_cannot_leave_open_windows_or_ask_for_what_it_may_not(run):
    _ok(run)
    assert run["window_open"] is True                              # window.open returned null: no new window
    assert run["href_after"] == "https://app.lagdoctor.example/index.html"
    assert run["refused"] == {"new window": 1, "navigation": 1, "malformed message": 3}


@pytest.mark.needs("webview2")
def test_a_crashed_page_loads_again_and_an_ended_browser_process_gets_a_new_environment(run):
    _ok(run)
    assert run["crash_call"] == "0x00000000" and run["after_renderer_crash"] == "https://app.lagdoctor.example/index.html"
    after = run["after_browser_exit"]
    assert run["end_browser"] == 0 and after["generation"] == 2 and after["new_pid"] != after["old_pid"]
    assert {"ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled"} <= set(after["options_asked_again"])
    assert after["env_restored"] is True and after["names"] == run["library"]["names"]
    assert run["readies"] == 3 and run["fatal"] == []


@pytest.mark.needs("webview2")
def test_every_setting_reads_back_and_the_runtime_read_the_options(run):
    _ok(run)
    assert set(run["settings_readback"]) == {prop for _, prop, _ in app_window.SETTINGS}
    assert set(run["settings_readback"].values()) == {"ok"}
    assert {"ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled"} <= set(run["options_asked"])
    assert run["env_restored"] is True


# ------------------------------------------------------------------------------------------- the page's files --

def test_the_page_loads_nothing_from_outside_and_runs_no_inline_script():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html).group(1)
    rules = {part.split()[0]: part.split()[1:] for part in " ".join(csp.split()).split(";") if part.strip()}
    assert rules["default-src"] == ["'none'"] and rules["script-src"] == ["'self'"] and rules["style-src"] == ["'self'"]
    assert rules["worker-src"] == ["'none'"] and rules["connect-src"] == ["'none'"] and rules["frame-src"] == ["'none'"]
    assert "unsafe" not in csp and "http" not in csp
    assert "<script>" not in html and not re.search(r"\son\w+=", html)
    assert re.findall(r'src="([^"]+)"', html) == ["vendor/uPlot.iife.min.js", "app.js"]


def test_the_pages_script_makes_text_nodes_and_never_parses_markup():
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "createContextualFragment",
                 "eval(", "new Function", "setTimeout('", 'setTimeout("'):
        assert sink not in js, sink
    assert "document.createTextNode(String(c))" in js


@pytest.mark.parametrize("name, digest", sorted(UPLOT.items()))
def test_the_vendored_uplot_is_the_pinned_release_with_its_licence_beside_it(name, digest):
    data = (ASSETS / "vendor" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    if name == "uPlot-LICENSE.txt":
        assert b"The MIT License" in data and b"Copyright (c) 2022 Leon Sorokin" in data


def test_the_assets_folder_holds_only_the_page_and_its_vendored_files():
    files = sorted(p.relative_to(ASSETS).as_posix() for p in ASSETS.rglob("*") if p.is_file())
    assert files == ["app.css", "app.js", "index.html", *sorted(f"vendor/{n}" for n in UPLOT)]


def test_the_committed_icon_is_what_make_icon_draws():
    from scripts import make_icon
    assert ICON.read_bytes() == make_icon.ico_bytes()
    assert ICON.parent == ASSETS.parent                            # beside the page's folder, never mapped with it


# ----------------------------------------------------------------------------------------- the window's rules --

def test_the_app_refuses_elevated_and_without_a_new_enough_runtime():
    assert "administrator" in app_main.refusal(True, {"build": 9999})
    assert "WebView2 Runtime" in app_main.refusal(False, None)
    assert "newer WebView2 Runtime" in app_main.refusal(False, {"build": app_main.MIN_BUILD - 1, "version": "x"})
    assert app_main.refusal(False, {"build": app_main.MIN_BUILD}) is None
    assert app_main.refusal(None, {"build": app_main.MIN_BUILD}) is None      # elevation unknown: not refused


def test_one_instance_per_home_and_a_tests_home_is_never_the_owners(tmp_path):
    a, b = app_main.instance_tag(tmp_path / "a"), app_main.instance_tag(tmp_path / "b")
    assert a != b and a == app_main.instance_tag(tmp_path / "a") and re.fullmatch(r"[0-9a-f]{12}", a)


def test_webview2s_own_variables_are_set_empty_while_it_starts_and_put_back_after(monkeypatch):
    for name in app_window.HARDENED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--remote-debugging-port=9222")
    before = app_window.harden_env()
    try:
        assert {n: wv.env_state(n) for n in app_window.HARDENED_ENV} == app_window.HARDENED_ENV
        assert wv.env_state("WEBVIEW2_USER_DATA_FOLDER") == ""             # present and empty, not absent
    finally:
        assert app_window.restore_env(before)
    assert wv.env_state("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") == "--remote-debugging-port=9222"
    assert wv.env_state("WEBVIEW2_USER_DATA_FOLDER") is None


def test_an_access_violation_in_a_handler_closes_the_window_but_an_ordinary_error_does_not():
    w = app_window.AppWindow.__new__(app_window.AppWindow)
    w.fatal, w.stop_requested, w.log = [], False, lambda text: None
    call = w.guard("a handler", lambda a, b: (_ for _ in ()).throw(ValueError("bad")))
    assert call(0, 0, 0) == wv.E_FAIL and not w.stop_requested
    call = w.guard("a handler", lambda a, b: (_ for _ in ()).throw(OSError("exception: access violation reading 0x0")))
    assert call(0, 0, 0) == wv.E_FAIL and w.stop_requested and w.fatal
    assert not app_window.is_access_violation(wv.ComError("x", wv.E_FAIL))


def test_a_page_cannot_pile_up_work():
    w = app_window.AppWindow.__new__(app_window.AppWindow)
    import queue
    w.jobs, w.workers, w.hwnd = queue.Queue(), [object()] * app_window.WORKERS, 0
    for _ in range(app_window.MAX_QUEUED):
        assert w.submit(lambda: None, lambda r, e: None)
    assert not w.submit(lambda: None, lambda r, e: None)


def _window(tmp_path) -> app_window.AppWindow:
    got = []
    w = app_window.AppWindow(runtime={"version": "1.0.0.0"}, create_fn=None, data_folder=tmp_path / "webview",
                             assets=ASSETS, icon=None, class_name="LagDoctorAppTest", on_message=got.append,
                             on_failed=lambda why: None, log=lambda text: None)
    w.got = got
    return w


def _fake_message_args(source: str, text: str):
    """A stand-in ICoreWebView2WebMessageReceivedEventArgs: Source and the message's JSON, through its vtable."""
    def get_str(value):
        def get(this, out):
            out[0] = wv.co_wstr(value)
            return wv.S_OK
        return wv.GET_STR(get)
    fns = [wv.QI_PROTO(lambda this, riid, ppv: wv.E_NOINTERFACE), wv.REF_PROTO(lambda this: 1),
           wv.REF_PROTO(lambda this: 1), get_str(source), get_str(text)]
    vtbl = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    cell = ctypes.c_void_p(ctypes.addressof(vtbl))
    return ctypes.addressof(cell), (fns, vtbl, cell)


def test_a_message_from_another_origin_never_reaches_the_app_and_one_from_its_page_does(tmp_path):
    w = _window(tmp_path)
    handler = w.handler("ICoreWebView2WebMessageReceivedEventHandler", wv.EVENT, w.on_web_message)
    request = json.dumps({"type": "request", "id": 5, "kind": "library"})
    forged, keep1 = _fake_message_args("https://evil.test/index.html", request)
    assert wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, forged) == wv.S_OK
    assert w.refused == {"message from another origin": 1} and not w.calls
    bad, keep2 = _fake_message_args(bridge_entry(), json.dumps({"type": "request", "id": 5, "kind": "nope"}))
    assert wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, bad) == wv.S_OK
    assert w.refused["malformed message"] == 1 and not w.calls
    genuine, keep3 = _fake_message_args(bridge_entry(), request)                        # the control
    assert wv.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, genuine) == wv.S_OK
    assert len(w.calls) == 1
    w.calls.popleft()()
    assert [(r.kind, r.id) for r in w.got] == [("library", 5)]


def bridge_entry() -> str:
    from lagdoctor.app import bridge
    return bridge.ENTRY


def test_the_views_settings_are_exactly_these():
    # Written out here, not read from the module: a setting turned on by mistake would still read back as set.
    assert {prop: value for _, prop, value in app_window.SETTINGS} == {
        "IsScriptEnabled": True, "IsWebMessageEnabled": True, "AreDefaultScriptDialogsEnabled": False,
        "IsStatusBarEnabled": False, "AreDevToolsEnabled": False, "AreDefaultContextMenusEnabled": False,
        "AreHostObjectsAllowed": False, "IsZoomControlEnabled": False, "IsBuiltInErrorPageEnabled": False,
        "AreBrowserAcceleratorKeysEnabled": False, "IsPasswordAutosaveEnabled": False,
        "IsGeneralAutofillEnabled": False, "IsPinchZoomEnabled": False, "IsSwipeNavigationEnabled": False,
        "IsReputationCheckingRequired": False}
    assert app_window.OPTION_BOOLS == {"AllowSingleSignOnUsingOSPrimaryAccount": False,
                                       "ExclusiveUserDataFolderAccess": True, "IsCustomCrashReportingEnabled": True}
    assert app_window.HARDENED_ENV == {"WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER": "",
                                       "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "",
                                       "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE": "0",
                                       "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": "", "WEBVIEW2_USER_DATA_FOLDER": ""}


def test_the_app_never_opens_devtools():
    # OpenDevToolsWindow opens DevTools even with DevTools turned off (M0): the app has no way to call it.
    assert all("OpenDevToolsWindow" not in methods for methods in wv.SLOTS.values())
    for p in sorted((ROOT / "lagdoctor").rglob("*.py")):
        assert "OpenDevToolsWindow" not in p.read_text(encoding="utf-8").replace(
            "never calls OpenDevToolsWindow", "").replace("never call OpenDevToolsWindow", ""), p


def test_only_a_folder_holding_its_capture_is_a_recording_the_page_can_ask_for(tmp_path):
    from lagdoctor.app import library
    from lagdoctor.app.main import App, Gone
    from lagdoctor.report import summary
    sessions = tmp_path / "sessions"
    gone = sessions / "20261104-194500-gone"           # its summary says a recording, but the capture was deleted
    gone.mkdir(parents=True)
    (gone / summary.SUMMARY_NAME).write_text(json.dumps({"schema": summary.SCHEMA, "kind": "watch"}), encoding="utf-8")
    check = sessions / "20261105-090000-check-up"
    check.mkdir()
    entries = library.scan(sessions)
    assert {e.id for e in entries} == {gone.name, check.name} and not any(e.is_recording for e in entries)
    app = App.__new__(App)
    app.sessions, app.entries = sessions, []
    import threading
    app.lock = threading.Lock()
    for sid in (gone.name, check.name, "20261106-000000-never"):
        with pytest.raises(Gone):
            app.entry(sid)


class _FakeWindow:
    def __init__(self, fail_buffer=False):
        self.posted, self.fail_buffer = [], fail_buffer

    def post(self, obj):
        self.posted.append(obj)
        return True

    def post_buffer(self, data, meta):
        if self.fail_buffer:
            raise wv.ComError("ICoreWebView2Environment12::CreateSharedBuffer", wv.E_OUTOFMEMORY)
        return 7


def _app(window, opened):
    from lagdoctor.app.main import App
    app = App.__new__(App)
    app.window, app.opener, app.log, app.generic_error = window, opened.append, lambda text: None, "generic"
    return app


def test_a_file_the_page_asks_to_open_is_opened_on_the_ui_thread_never_by_a_worker(tmp_path):
    # ShellExecute (os.startfile) wants COM, which the UI thread has (STA) and the workers do not.
    from lagdoctor.app import bridge
    from lagdoctor.app.main import Open
    opened: list[str] = []
    app = _app(_FakeWindow(), opened)
    app.sessions = tmp_path
    result = app.do_open_folder()                       # what a worker runs
    assert opened == [] and result == Open(tmp_path)
    app.answer(bridge.Request("open_folder", 4), result, None)    # what the UI thread runs
    assert opened == [str(tmp_path)] and app.window.posted == [bridge.reply(4, {})]


def test_frame_times_that_cannot_be_handed_over_are_said_on_the_chart_not_shown_as_none_recorded():
    from lagdoctor.app import bridge
    app = _app(_FakeWindow(fail_buffer=True), [])
    view = {"frames": {"missing": ""}, "game": "x"}
    app.answer(bridge.Request("results", 5), (view, (b"\0" * 16, {"kind": "frames", "count": 1})), None)
    data = app.window.posted[0]["data"]
    assert data["frames_token"] is None and data["frames"]["missing"].startswith("The frame times could not be handed")
    ok = _app(_FakeWindow(), [])
    ok.answer(bridge.Request("results", 6), (view, (b"\0" * 16, {"kind": "frames", "count": 1})), None)
    assert ok.window.posted[0]["data"]["frames_token"] == 7 and ok.window.posted[0]["data"]["frames"]["missing"] == ""
