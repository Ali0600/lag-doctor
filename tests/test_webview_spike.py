"""The window spike's policy and plumbing (scripts/webview_spike.py, M0 of docs/LAUNCHER-PLAN.md).

The window itself runs only in the spike: by the owner, by an agent with --auto, and in the Window spike workflow.
These hold the parts a regression could break without a window to show it: the origin and navigation rules the
window's security rests on, the COM objects the spike hands WebView2 (driven here through their own vtables, as
WebView2 calls them), the socket reader behind the no-server check (proved on sockets this test opens), and the
environment-variable fact the hardening relies on.
"""
from __future__ import annotations

import ctypes
import os
import socket
import sys
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import webview_spike as ws  # noqa: E402


# ------------------------------------------------------------------------------------------------- policy --

@pytest.mark.parametrize("source", [
    "https://app.lagdoctor.example/index.html",
    "https://app.lagdoctor.example/",
    "https://app.lagdoctor.example/other.html?x=1#y",
])
def test_the_apps_own_documents_pass_the_origin_check(source):
    assert ws.origin_ok(source)


@pytest.mark.parametrize("source", [
    None, "", "about:blank", "null",
    "http://app.lagdoctor.example/index.html",                 # not https
    "https://app.lagdoctor.example.evil.test/index.html",      # the name inside another host
    "https://evil.test/app.lagdoctor.example/index.html",
    "https://evil.test/#https://app.lagdoctor.example/",
    "https://app.lagdoctor.example@evil.test/",                # user info: the host is evil.test
    "https://user@app.lagdoctor.example/",                     # user info on the right host
    "https://user:pw@app.lagdoctor.example/",
    "https://app.lagdoctor.example:443/",                      # an explicit port is not the origin a page sends
    "https://app.lagdoctor.example:99999/",                    # not even a port
    "file:///C:/Windows/win.ini",
    "https://lagdoctor.example/",
    "https://evil.app.lagdoctor.example/",                     # a host under the app's name
    "https://app.lagdoctor.example./",                         # the name with a trailing dot
    "blob:https://app.lagdoctor.example/x",
])
def test_anything_else_fails_the_origin_check(source):
    assert not ws.origin_ok(source)


def test_only_the_first_navigation_and_only_to_the_entry_page_is_allowed():
    assert ws.navigation_allowed(ws.ENTRY, first=True)
    assert not ws.navigation_allowed(ws.ENTRY, first=False)          # a reload or a link back to it, later
    assert not ws.navigation_allowed(ws.APP_ORIGIN + "/other.html", first=True)
    assert not ws.navigation_allowed(ws.ENTRY + "?next=https://evil.test/", first=True)
    assert not ws.navigation_allowed(ws.ENTRY + "#x", first=True)
    assert not ws.navigation_allowed("https://evil.test/index.html", first=True)
    assert not ws.navigation_allowed(None, first=True)


def test_percentile_is_nearest_rank():
    assert ws.percentile([], 50) is None
    assert ws.percentile([7.0], 95) == 7.0
    values = [float(v) for v in range(100, 0, -1)]                  # 100 down to 1, unsorted on purpose
    assert ws.percentile(values, 50) == 50.0
    assert ws.percentile(values, 95) == 95.0
    assert ws.percentile(values, 100) == 100.0


def test_the_runtime_version_is_four_numbers():
    assert ws.parse_runtime_version("157.0.4341.42") == (157, 0, 4341, 42)
    for bad in ("157.0.4341", "157.0.4341.42.1", "x.0.4341.42", "", "157.0.4341.42 "):
        assert ws.parse_runtime_version(bad) is None


def test_every_interface_the_spike_calls_has_one_iid_and_the_iids_are_distinct():
    calls = [name for name in ws.SLOTS if not name.endswith("EventArgs")]
    assert all(name in ws.IIDS for name in calls)
    assert len(set(ws.IIDS.values())) == len(ws.IIDS)
    for slots in ws.SLOTS.values():                                   # IUnknown holds 0-2 everywhere
        assert min(slots.values()) >= 3
        assert len(set(slots.values())) == len(slots)


def test_the_page_loads_no_inline_script_and_writes_text_only_through_textcontent():
    head = ws.INDEX_HTML.split("</head>")[0]
    assert "Content-Security-Policy" in head and "script-src 'self'" in head and "default-src 'none'" in head
    assert "<script>" not in ws.INDEX_HTML and "onclick" not in ws.INDEX_HTML
    assert ws.APP_JS.count("innerHTML") == 1 and "the attack, on purpose" in ws.APP_JS   # the CSP probe alone


# ------------------------------------------------------------------------------------------------ COM objects --

def qi(ptr: int, iface: str) -> tuple[int, int]:
    out = ctypes.c_void_p()
    hr = ws.vcall(ptr, 0, (ctypes.POINTER(ws.GUID), ctypes.POINTER(ctypes.c_void_p)),
                  ctypes.byref(ws.IID[iface]), ctypes.byref(out))
    return hr, out.value or 0


def test_a_handler_answers_for_its_own_interface_and_iunknown_only():
    h = ws.Handler("ICoreWebView2WebMessageReceivedEventHandler", ws.EVENT, lambda a, b: None)
    assert qi(h.ptr, "ICoreWebView2WebMessageReceivedEventHandler") == (ws.S_OK, h.ptr)
    assert qi(h.ptr, "IUnknown") == (ws.S_OK, h.ptr)
    assert qi(h.ptr, "ICoreWebView2NavigationStartingEventHandler") == (ws.E_NOINTERFACE, 0)
    assert h.refs == 3                                                 # 1, plus the two successful queries


def test_a_handlers_invoke_reaches_python_with_its_arguments():
    seen = []
    h = ws.Handler("ICoreWebView2WebMessageReceivedEventHandler", ws.EVENT, lambda a, b: seen.append((a, b)))
    hr = ws.vcall(h.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0x1234, 0x5678)
    assert hr == ws.S_OK and seen == [(0x1234, 0x5678)]
    assert ws.vcall(h.ptr, 1, (), restype=ctypes.c_ulong) == 2       # AddRef
    assert ws.vcall(h.ptr, 2, (), restype=ctypes.c_ulong) == 1       # Release


def test_a_guarded_handler_logs_its_exception_and_returns_e_fail(tmp_path):
    lines = []
    spike = ws.Spike("main", tmp_path, False, lines.append)

    def boom(a, b):
        raise RuntimeError("planted failure in a handler")
    h = spike.handler("ICoreWebView2WebMessageReceivedEventHandler", ws.EVENT, boom)
    hr = ws.vcall(h.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, 0)
    assert hr == ws.E_FAIL
    assert any("planted failure in a handler" in line for line in lines)
    assert spike.stop_requested is False                              # an ordinary exception does not stop the run
    assert h in spike.keep                                            # and the handler stays referenced


def test_the_environment_options_answer_on_three_faces_with_lag_doctors_values():
    opts = ws.EnvOptions("157.0.4341.42")
    faces = {}
    for iface in ("ICoreWebView2EnvironmentOptions", "ICoreWebView2EnvironmentOptions2",
                  "ICoreWebView2EnvironmentOptions3", "IUnknown"):
        hr, ptr = qi(opts.ptr, iface)
        assert hr == ws.S_OK and ptr
        faces[iface] = ptr
    assert faces["IUnknown"] == faces["ICoreWebView2EnvironmentOptions"] == opts.ptr
    assert len({faces[i] for i in faces}) == 3
    assert qi(faces["ICoreWebView2EnvironmentOptions3"], "ICoreWebView2Settings") == (ws.E_NOINTERFACE, 0)

    def get_str(ptr, iface, method):
        p = ctypes.c_void_p()
        assert ws.vcall(ptr, ws.SLOTS[iface][method], (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(p)) == ws.S_OK
        return ws.take_wstr(p)

    def get_bool(ptr, iface, method):
        v = wintypes.BOOL(7)
        assert ws.vcall(ptr, ws.SLOTS[iface][method], (ctypes.POINTER(wintypes.BOOL),), ctypes.byref(v)) == ws.S_OK
        return v.value

    o1, o2, o3 = (faces["ICoreWebView2EnvironmentOptions"], faces["ICoreWebView2EnvironmentOptions2"],
                  faces["ICoreWebView2EnvironmentOptions3"])
    assert get_str(o1, "ICoreWebView2EnvironmentOptions", "get_AdditionalBrowserArguments") == ""
    assert get_str(o1, "ICoreWebView2EnvironmentOptions", "get_TargetCompatibleBrowserVersion") == "157.0.4341.42"
    assert get_bool(o1, "ICoreWebView2EnvironmentOptions", "get_AllowSingleSignOnUsingOSPrimaryAccount") == 0
    assert get_bool(o2, "ICoreWebView2EnvironmentOptions2", "get_ExclusiveUserDataFolderAccess") == 1
    assert get_bool(o3, "ICoreWebView2EnvironmentOptions3", "get_IsCustomCrashReportingEnabled") == 1
    assert opts.asked == ["AdditionalBrowserArguments", "TargetCompatibleBrowserVersion",
                          "AllowSingleSignOnUsingOSPrimaryAccount", "ExclusiveUserDataFolderAccess",
                          "IsCustomCrashReportingEnabled"]


# ----------------------------------------------------------------------------------- sockets and processes --

def test_the_socket_reader_sees_sockets_this_process_opens_with_their_states_and_not_after_they_close():
    made = []
    for family, kind, host, proto, state in ((socket.AF_INET, socket.SOCK_STREAM, "127.0.0.1", "tcp4", "listen"),
                                             (socket.AF_INET6, socket.SOCK_STREAM, "::1", "tcp6", "listen"),
                                             (socket.AF_INET, socket.SOCK_DGRAM, "127.0.0.1", "udp4", "bound"),
                                             (socket.AF_INET6, socket.SOCK_DGRAM, "::1", "udp6", "bound")):
        s = socket.socket(family, kind)
        s.bind((host, 0))
        if kind == socket.SOCK_STREAM:
            s.listen(1)
        made.append((proto, state, s))
    client = socket.create_connection(("127.0.0.1", made[0][2].getsockname()[1]), timeout=5)
    server, _ = made[0][2].accept()
    made += [("tcp4", "established", client), ("tcp4", "established", server)]
    expected = {(proto, s.getsockname()[1], state) for proto, state, s in made}
    try:
        seen = {(x["proto"], x["port"], x["state"]) for x in ws.open_sockets({os.getpid()})}
        assert expected <= seen                                       # each family's row layout and state read right
        others = {(x["proto"], x["port"]) for x in ws.open_sockets({4})}   # the System process: never ours
        assert not {(p, port) for p, port, _ in expected} & others
    finally:
        for _, _, s in made:
            s.close()
    after = {(x["proto"], x["port"], x["state"]) for x in ws.open_sockets({os.getpid()})}
    assert not {e for e in expected if e[2] in ("listen", "bound")} & after


def _proc(pid, ppid, created, name="msedgewebview2.exe"):
    return SimpleNamespace(pid=pid, ppid=ppid, create_time=created, name=name)


def test_the_process_tree_takes_children_younger_than_their_parent_only():
    snap = {p.pid: p for p in (
        _proc(10, 1, 100, "python.exe"),
        _proc(20, 10, 200),            # the browser
        _proc(21, 20, 210),            # its renderer
        _proc(22, 21, 220),            # a grandchild
        _proc(23, 20, 150),            # an older process holding a reused parent PID: not a child
        _proc(30, 99, 300),            # unrelated
    )}
    assert set(ws.process_tree(snap, 20)) == {20, 21, 22}
    assert ws.process_tree(snap, 77) == {}


# ---------------------------------------------------------------------------------------- the environment --

def test_env_state_tells_an_absent_variable_from_an_empty_one_and_os_environ_cannot_make_an_empty_one():
    name = "LAGDOCTOR_SPIKE_TEST_ENV"
    try:
        ws.SetEnvironmentVariableW(name, "")
        assert ws.env_state(name) == ""                       # present, empty: what WebView2 must see
        ws.SetEnvironmentVariableW(name, None)
        assert ws.env_state(name) is None
        os.environ[name] = ""                                 # CPython's _wputenv("NAME=") removes it instead
        assert ws.env_state(name) is None
        ws.SetEnvironmentVariableW(name, "0")
        assert ws.env_state(name) == "0"
    finally:
        ws.SetEnvironmentVariableW(name, None)
        os.environ.pop(name, None)


def test_harden_then_restore_leaves_every_variable_as_it_was(monkeypatch):
    names = list(ws.HARDENED_ENV)
    original = {name: ws.env_state(name) for name in names}
    try:
        ws.SetEnvironmentVariableW(names[0], None)                    # absent before: must be absent again
        ws.SetEnvironmentVariableW(names[1], "")                      # empty before: must stay present and empty
        ws.SetEnvironmentVariableW(names[2], "user value")            # set before: must come back
        before = {name: ws.env_state(name) for name in names}
        hardened = ws.harden_env()
        assert hardened["ok"] and hardened["reader_ok"] and hardened["os_environ_empty_is"] == "removed"
        assert hardened["before"] == before
        assert {name: ws.env_state(name) for name in names} == ws.HARDENED_ENV
        assert ws.restore_env(hardened["before"])
        assert {name: ws.env_state(name) for name in names} == before
        assert before[names[0]] is None and before[names[1]] == "" and before[names[2]] == "user value"
    finally:
        for name, value in original.items():
            ws.SetEnvironmentVariableW(name, value)


# ------------------------------------------------------------------------------------- the host's own logic --

def _fake_message_args(source: str, message: dict):
    """A stand-in ICoreWebView2WebMessageReceivedEventArgs: Source and the message as JSON, through its vtable."""
    def get_str(text):
        def get(this, out):
            out[0] = ws.co_wstr(text)
            return ws.S_OK
        return ws.GET_STR(get)
    fns = [ws.QI_PROTO(lambda this, riid, ppv: ws.E_NOINTERFACE), ws.REF_PROTO(lambda this: 1),
           ws.REF_PROTO(lambda this: 1), get_str(source), get_str(__import__("json").dumps(message)),
           get_str(__import__("json").dumps(message))]
    vtbl = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    cell = ctypes.c_void_p(ctypes.addressof(vtbl))
    return ctypes.addressof(cell), (fns, vtbl, cell)


def test_a_message_from_another_origin_is_refused_by_the_real_handler_and_one_from_the_app_is_not(tmp_path):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    handler = spike.handler("ICoreWebView2WebMessageReceivedEventHandler", ws.EVENT, spike.on_web_message)
    got = []
    spike.pending_pages[7] = got.append
    forged, keep1 = _fake_message_args("https://evil.test/index.html", {"type": "report", "token": 7})
    assert ws.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, forged) == ws.S_OK
    assert spike.msgs_bad_origin == 1 and 7 in spike.pending_pages and not spike.calls
    genuine, keep2 = _fake_message_args(ws.ENTRY, {"type": "report", "token": 7})    # the control
    assert ws.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, genuine) == ws.S_OK
    assert spike.msgs_bad_origin == 1 and 7 not in spike.pending_pages and len(spike.calls) == 1
    spike.calls.popleft()()
    assert got == [{"type": "report", "token": 7}]


def test_an_access_violation_in_a_handler_fails_and_stops_the_run_but_other_errors_do_not(tmp_path):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)

    def raises(exc):
        def fn(a, b):
            raise exc
        return spike.handler("ICoreWebView2WebMessageReceivedEventHandler", ws.EVENT, fn)
    for harmless in (RuntimeError("an ordinary bug"), ws.ComError("ICoreWebView2::Navigate", -2147467259)):
        assert ws.vcall(raises(harmless).ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, 0) == ws.E_FAIL
    assert spike.fatal_events == [] and spike.stop_requested is False
    fault = OSError("exception: access violation reading 0xFFFFFFFFFFFFFFFF")   # what ctypes raises for one
    assert ws.vcall(raises(fault).ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, 0) == ws.E_FAIL
    assert len(spike.fatal_events) == 1 and spike.stop_requested is True


def test_the_worker_tally_counts_only_posts_made_while_a_move_loop_ran(tmp_path):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    spike.worker_posts = {"window": [(1, 0.5), (2, 1.5), (3, 2.5), (4, 3.5)],
                          "thread": [(1, 0.5), (2, 1.5), (3, 2.5), (4, 3.5)]}
    spike.worker_got = {"window": {1, 2, 3, 4}, "thread": {1, 4}}
    tally = spike.worker_tally([[1.0, 3.0]])                          # posts 2 and 3 fell inside the loop
    assert tally["window"] == {"in_loop": 2, "in_loop_arrived": 2, "outside": 2, "outside_arrived": 2}
    assert tally["thread"] == {"in_loop": 2, "in_loop_arrived": 0, "outside": 2, "outside_arrived": 2}
    assert spike.worker_tally([[1.0, None]])["thread"]["in_loop"] == 3      # a loop still running counts to now


def _run(work, name, age, finished=True, data=True):
    d = work / name
    (d / "webview-data" / "EBWebView").mkdir(parents=True)
    if not data:
        __import__("shutil").rmtree(d / "webview-data")
    (d / ws.RUN_MARKER).write_text("made by scripts/webview_spike.py\n", "utf-8")
    (d / "spike.log").write_text("log\n", "utf-8")
    if finished:
        (d / "results.json").write_text("{}", "utf-8")
    stamp = 1_000_000 - age
    os.utime(d / ws.RUN_MARKER, (stamp, stamp))
    return d


def test_pruning_keeps_new_runs_live_runs_evidence_and_anything_the_spike_did_not_make(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "KEEP_RUNS", 3)
    monkeypatch.setattr(ws, "KEEP_EVIDENCE", 5)
    now = 1_000_000
    runs = [_run(tmp_path, f"run-{i}", age=1000 * (10 - i)) for i in range(7)]       # run-0 oldest, run-6 newest
    live = _run(tmp_path, "run-live", age=60, finished=False)                       # no results yet, a minute old
    stranger = tmp_path / "not-a-run"
    (stranger / "webview-data").mkdir(parents=True)
    ws.prune_runs(tmp_path, now=now)
    # 8 marked runs; the newest 2 (KEEP_RUNS - 1) stay whole: run-6 and run-live
    assert (live / "webview-data").is_dir() and (runs[6] / "webview-data").is_dir()
    for d in runs[4:6]:                                     # older, finished: data gone, evidence kept
        assert not (d / "webview-data").exists() and (d / "results.json").is_file() and (d / "spike.log").is_file()
    for d in runs[:4]:                                      # past the newest KEEP_EVIDENCE - 1 = 4: gone entirely
        assert not d.exists()
    assert (stranger / "webview-data").is_dir()             # no marker: never touched


def test_pruning_never_touches_an_unfinished_run_younger_than_the_grace_period(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "KEEP_RUNS", 1)
    monkeypatch.setattr(ws, "KEEP_EVIDENCE", 1)
    young = _run(tmp_path, "run-young", age=60, finished=False)
    old = _run(tmp_path, "run-old", age=ws.LIVE_GRACE_S + 60, finished=False)
    ws.prune_runs(tmp_path, now=1_000_000)
    assert (young / "webview-data").is_dir() and young.is_dir()
    assert not old.exists()                                 # unfinished but long dead: pruned


# ----------------------------------------------------------------------------------------------- step 2's keys --
# The owner's first two runs never counted step 2: the first saw no right-click, the second only four keys, and Alt+Left
# reached the page in neither (docs/research/desktop-app.md, section 11). These hold the rebuilt step's rules.

NOTHING, SOMETHING = "Nothing happened", "Something happened"
FOUR = {"ctrl+r", "ctrl+p", "ctrl+f", "ctrl+s"}
SEVEN = FOUR | {"ctrl+shift+i", "ctrl+plus", "ctrl+minus"}


def test_the_host_names_every_owner_key_as_the_page_does():
    assert set(ws.KEY_STROKES) == set(ws.OWNER_KEYS)                  # each key the owner presses can be typed, and back
    for combo, (mods, vk) in ws.KEY_STROKES.items():
        assert ws.host_combo(vk, ws.VK_CONTROL in mods, ws.VK_SHIFT in mods, ws.VK_MENU in mods) == combo
    assert ws.host_combo(ws.VK_ADD, True, False, False) == "ctrl+plus"            # the number pad's + and -
    assert ws.host_combo(ws.VK_SUBTRACT, True, False, False) == "ctrl+minus"
    assert ws.host_combo(ws.VK_OEM_PLUS, True, True, False) == "ctrl+plus"        # Shift held to type +: left out
    assert ws.host_combo(ws.VK_LEFT, True, False, True) == "alt+arrowleft"         # AltGr: a non-US right Alt
    assert ws.host_combo(ws.VK_LEFT, False, False, False) == "arrowleft"          # the control: no Alt, no Alt+Left
    assert ws.host_combo(0x52, False, False, False) == "r"                        # R without Ctrl: no owner key
    assert ws.host_combo(0x74, True, False, False) is None                        # F5: no line of step 2 names it


def test_the_pages_own_naming_follows_the_same_rules():
    js = ws.APP_JS.split("function combo(e) {")[1].split("\n}\n")[0]
    for rule in ("k = 'plus'", "k = 'minus'", "k !== 'plus' && k !== 'minus'", "e.altKey) return 'alt+arrowleft'"):
        assert rule in js
    assert js.index("mods.push('ctrl')") < js.index("mods.push('shift')") < js.index("mods.push('alt')")


def test_step_2_lists_every_key_the_wheel_and_a_right_click_with_alt_left_last():
    ids = [item for item, _ in ws.KEYS_CHECKLIST]
    assert set(ids) == set(ws.OWNER_KEYS) | {"ctrl-wheel", "right-click"} and len(ids) == len(set(ids))
    assert ids[-1] == "alt+arrowleft"                                  # a menu mode it starts can take nothing after it


def test_a_key_the_window_saw_counts_as_pressed_even_when_the_page_never_got_it():
    ok, text = ws.keys_verdict(NOTHING, False, FOUR, {"ctrl+shift+i", "ctrl+plus", "alt+arrowleft"}, 1, "the effects")
    assert ok is True                                                  # 7 of 8 pressed, and a right-click
    assert "7 of 8 keys pressed (missing: Ctrl+Minus)" in text
    assert "Ctrl+Shift+I, Ctrl+Plus, Alt+Left reached the window but not the page" in text and "the effects" in text
    assert ws.keys_verdict(NOTHING, False, FOUR, set(), 1, "")[0] is None        # the control: the page's four alone


def test_nothing_happened_counts_only_with_six_keys_and_a_right_click():
    six = SEVEN - {"ctrl+minus"}
    assert ws.keys_verdict(NOTHING, False, six, set(), 1, "")[0] is True
    assert ws.keys_verdict(NOTHING, False, six - {"ctrl+plus"}, set(), 1, "")[0] is None    # five keys
    ok, text = ws.keys_verdict(NOTHING, False, set(ws.OWNER_KEYS), set(ws.OWNER_KEYS), 0, "")
    assert ok is None and text.startswith("too few") and "0 right-clicks" in text      # all eight, no right-click
    assert ws.keys_verdict(NOTHING, False, SEVEN, set(), 0, "")[0] is None        # the owner's first run
    assert ws.keys_verdict(NOTHING, False, FOUR, set(), 4, "")[0] is None         # and their second


def test_something_happened_or_a_change_the_spike_saw_fails_even_with_too_few_tries():
    assert ws.keys_verdict(SOMETHING, False, set(), set(), 0, "")[0] is False
    assert ws.keys_verdict(SOMETHING, False, set(ws.OWNER_KEYS), set(), 2, "")[0] is False
    assert ws.keys_verdict(NOTHING, True, set(), set(), 0, "")[0] is False        # the zoom moved, say
    assert ws.keys_verdict(NOTHING, True, set(ws.OWNER_KEYS), set(), 2, "")[0] is False
    assert ws.keys_verdict("Skip", False, set(ws.OWNER_KEYS), set(), 2, "") == (None, "skipped")


def test_the_alt_left_story_reads_only_what_came_after_it():
    window = [{"t": 1.0, "combo": "ctrl+r"}, {"t": 5.0, "combo": "alt+arrowleft"}]
    page = [("ctrl+r", 1.0), ("x", 5.5)]
    earlier = [{"t": 2.0, "msg": "menu loop started"}, {"t": 2.5, "msg": "menu loop ended"}]   # a lone Alt before it
    later = [{"t": 5.1, "msg": "system command", "command": "keyboard menu"}, {"t": 5.12, "msg": "menu loop started"},
             {"t": 6.0, "msg": "view lost focus"}]
    text = ws.alt_left_story(window, page, earlier + later)
    assert text.startswith("Alt+Left reached the window but not the page")
    assert "menu mode started 0.10 s later;" in text                   # not the earlier loop, and not its end
    assert "the view lost the keyboard" in text and "1 key reached the page after it" in text
    text = ws.alt_left_story(window, page, earlier)                    # the control: nothing after Alt+Left
    assert "no menu mode followed" in text and "lost the keyboard" not in text
    assert ws.alt_left_story([], [("ctrl+r", 1.0)], later) == "Alt+Left was seen by neither the window nor the page"
    assert ws.alt_left_story([], [("alt+arrowleft", 3.0)], []).startswith("Alt+Left reached the page but not")


def _fake_key_args(kind: int, vk: int, lparam: int):
    """A stand-in ICoreWebView2AcceleratorKeyPressedEventArgs: KeyEventKind, VirtualKey and KeyEventLParam."""
    int_out = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    u32_out = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))

    def getter(value, proto):
        def get(this, out):
            out[0] = value
            return ws.S_OK
        return proto(get)
    fns = [ws.QI_PROTO(lambda this, riid, ppv: ws.E_NOINTERFACE), ws.REF_PROTO(lambda this: 1),
           ws.REF_PROTO(lambda this: 1), getter(kind, int_out), getter(vk, u32_out),
           getter(ctypes.c_int(lparam).value, int_out)]
    assert [ws.SLOTS["ICoreWebView2AcceleratorKeyPressedEventArgs"][m]
            for m in ("get_KeyEventKind", "get_VirtualKey", "get_KeyEventLParam")] == [3, 4, 5]
    vtbl = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
    cell = ctypes.c_void_p(ctypes.addressof(vtbl))
    return ctypes.addressof(cell), (fns, vtbl, cell)


def test_the_key_handler_keeps_every_key_and_ticks_the_page_only_for_a_first_press_during_step_2(tmp_path,
                                                                                                 monkeypatch):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    handler = spike.handler("ICoreWebView2AcceleratorKeyPressedEventHandler", ws.EVENT, spike.on_accelerator)
    held = {ws.VK_CONTROL}
    monkeypatch.setattr(ws, "GetKeyState", lambda vk: -32768 if vk in held else 0)   # the high bit: held down
    kept = []

    def key(kind, vk, lparam=0x00130001):
        args, keep = _fake_key_args(kind, vk, lparam)
        kept.append(keep)
        assert ws.vcall(handler.ptr, 3, (ctypes.c_void_p, ctypes.c_void_p), 0, args) == ws.S_OK   # nothing raised
        return spike.accel_keys[-1]
    assert key(0, 0x52)["combo"] == "ctrl+r" and not spike.calls       # outside step 2: kept, no tick
    spike.keys_watch = True
    assert key(0, 0x52)["combo"] == "ctrl+r" and len(spike.calls) == 1
    assert key(0, 0x52, 0x40130001)["combo"] is None and len(spike.calls) == 1      # held: a repeat, no new press
    assert key(1, 0x52, 0xC0130001)["combo"] is None and len(spike.calls) == 1      # let go
    held.clear()
    alt = key(2, ws.VK_LEFT, 0x214B0001)                               # a system key, Alt in its lParam (bit 29)
    assert alt["combo"] == "alt+arrowleft" and alt["alt"] and not alt["ctrl"] and len(spike.calls) == 2
    sent = []
    spike.to_page = lambda obj: sent.append(obj) or True
    while spike.calls:
        spike.calls.popleft()()
    assert sent == [{"type": "keyseen", "item": "ctrl+r"}, {"type": "keyseen", "item": "alt+arrowleft"}]


def test_the_window_keeps_its_focus_and_menu_messages_and_still_passes_each_on(tmp_path):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    for msg, wp in ((ws.WM_SYSCOMMAND, ws.SC_KEYMENU), (ws.WM_ENTERMENULOOP, 0), (ws.WM_KILLFOCUS, 0),
                    (ws.WM_ACTIVATE, 1), (ws.WM_SYSKEYUP, ws.VK_MENU), (ws.WM_SYSCOMMAND, ws.SC_MOVE)):
        assert spike.on_message(0, msg, wp, 0) is None                 # None: DefWindowProc still gets it
    assert spike.menu_open                                             # a menu loop started and has not ended
    assert spike.on_message(0, ws.WM_EXITMENULOOP, 0, 0) is None and not spike.menu_open
    assert [e["msg"] for e in spike.focus_events] == ["system command", "menu loop started", "window lost focus",
                                                     "window activation", "window system key up", "system command",
                                                     "menu loop ended"]
    assert [e.get("command") for e in spike.focus_events if e["msg"] == "system command"] == ["keyboard menu",
                                                                                              "0xF010"]
    assert spike.on_message(0, ws.WM_SETFOCUS, 0, 0) == 0              # kept, and its own branch still answers it
    assert spike.focus_events[-1]["msg"] == "window got focus"
    assert spike.on_message(0, ws.WM_SIZE, 0, 0) == 0 and len(spike.focus_events) == 8    # the control: not kept


def test_a_rehearsal_clicks_the_first_button_itself_unless_the_step_answers_by_typing():
    w = ws.Owner("Step 2", "text", [NOTHING, "Skip"], checklist=ws.KEYS_CHECKLIST)
    prompt = ws.owner_prompt(w, "id1", rehearse=True)
    assert prompt["auto"] == NOTHING and prompt["checklist"][-1] == ["alt+arrowleft", ws.KEYS_CHECKLIST[-1][1]]
    assert "auto" not in ws.owner_prompt(ws.Owner("Step 2", "text", [NOTHING], auto=False), "id2", rehearse=True)
    assert "auto" not in ws.owner_prompt(w, "id3", rehearse=False)     # a person answers


def test_only_a_ci_rehearsal_on_githubs_own_runner_types_keys():
    github = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}
    assert ws.types_keys("main", True, True, github)
    assert not ws.types_keys("main", True, True, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"})
    assert not ws.types_keys("main", True, True, {})                   # a PC someone uses
    assert not ws.types_keys("main", False, True, github)              # the automated checks: no step 2 to type into
    assert not ws.types_keys("main", True, False, github)
    assert not ws.types_keys("droprefs", True, True, github)


def _typing_spike(tmp_path, monkeypatch, on_runner: bool):
    """A spike whose type_keys runs at once and touches nothing real: no window, pointer or input."""
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    spike.keys_probe, spike.hwnd = True, 0x1234
    monkeypatch.setattr(ws.guard, "on_throwaway_runner", lambda environ=None: on_runner)
    for name in ("ShowWindow", "SetForegroundWindow", "SetCursorPos", "ClientToScreen", "GetClientRect", "EndMenu"):
        monkeypatch.setattr(ws, name, lambda *a: 1)
    monkeypatch.setattr(ws, "GetCursorPos", lambda p: 0)
    front = {"hwnd": spike.hwnd, "lost_after": None}
    typed, sent = [], []

    def send(items):
        typed.append([(i.u.ki.wVk if i.type == ws.INPUT_KEYBOARD else -i.u.mi.dwFlags) for i in items])
        if len(typed) == front["lost_after"]:
            front["hwnd"] = 0x9999                                     # another window comes to the front
        return len(items)
    monkeypatch.setattr(ws, "send_input", send)
    monkeypatch.setattr(ws, "GetForegroundWindow", lambda: front["hwnd"])
    spike.after = lambda ms, fn: fn()
    spike.to_page = lambda obj: sent.append(obj) or True
    return spike, front, typed, sent


def test_type_keys_refuses_off_githubs_runner_even_with_its_flag_set(tmp_path, monkeypatch):
    spike, _, typed, sent = _typing_spike(tmp_path, monkeypatch, on_runner=False)
    spike.type_keys()
    assert typed == [] and spike.typing.startswith("not typed")        # a wrong flag alone types nothing
    assert sent == [{"type": "press", "button": NOTHING}]              # and the step is still answered


def test_type_keys_types_each_action_in_order_and_stops_once_the_window_is_not_in_front(tmp_path, monkeypatch):
    spike, front, typed, sent = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    spike.type_keys()
    ctrl, shift, alt = ws.VK_CONTROL, ws.VK_SHIFT, ws.VK_MENU
    assert typed[0] == [ctrl, 0x52, 0x52, ctrl] and typed[4] == [ctrl, shift, 0x49, 0x49, shift, ctrl]
    assert typed[7] == [ctrl, -ws.MOUSEEVENTF_WHEEL, ctrl]
    assert typed[8] == [-ws.MOUSEEVENTF_RIGHTDOWN, -ws.MOUSEEVENTF_RIGHTUP]
    assert typed[9] == [alt, ws.VK_LEFT, ws.VK_LEFT, alt] and typed[10] == [0x58, 0x58]   # Alt+Left, then a letter
    assert len(typed) == 11 and spike.typing.startswith("typed all 11 actions")
    assert sent == [{"type": "press", "button": NOTHING}]
    spike, front, typed, sent = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    front["lost_after"] = 3
    spike.type_keys()
    assert len(typed) == 3 and "typed 3 of 11 actions: the window was not in front before ctrl+s" in spike.typing
    assert sent == [{"type": "press", "button": NOTHING}]
