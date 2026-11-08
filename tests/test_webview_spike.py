"""The window spike's policy and plumbing (scripts/webview_spike.py, M0 of docs/LAUNCHER-PLAN.md).

The window itself runs only in the spike: by the owner, by an agent with --auto, and in the Window spike workflow.
These hold the parts a regression could break without a window to show it: the origin and navigation rules the
window's security rests on, the COM objects the spike hands WebView2 (driven here through their own vtables, as
WebView2 calls them), the no-server check (its socket reader proved on sockets this test opens, the rule that tells a
server from WebView2's own clients, and the whole check run against a process holding a client or a server), and the
environment-variable fact the hardening relies on.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
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


def test_the_socket_reader_gives_the_bound_address_and_whether_the_program_named_a_udp_port():
    made = []

    def new(family, kind):
        s = socket.socket(family, kind)
        made.append(s)
        return s
    try:
        lst = new(socket.AF_INET6, socket.SOCK_STREAM)
        lst.bind(("::1", 0))
        lst.listen(1)
        u4 = new(socket.AF_INET, socket.SOCK_DGRAM)
        u4.bind(("127.0.0.1", 0))
        named = new(socket.AF_INET6, socket.SOCK_DGRAM)
        named.bind(("::1", u4.getsockname()[1]))                       # IPv6 ports are a space of their own
        client = new(socket.AF_INET, socket.SOCK_DGRAM)
        client.connect(("127.0.0.1", u4.getsockname()[1]))             # bound by Windows; sends nothing
        assert client.getsockname()[0] == "127.0.0.1"                  # what it sends from
        seen = {(x["proto"], x["port"]): x for x in ws.open_sockets({os.getpid()})}
        assert seen[("tcp6", lst.getsockname()[1])]["addr"] == "::1"
        for proto, s, addr, named_port in (("udp4", u4, "127.0.0.1", False), ("udp6", named, "::1", True),
                                           ("udp4", client, "0.0.0.0", False)):    # the client: as bound, not sent
            x = seen[(proto, s.getsockname()[1])]
            assert (x["addr"], x["specific_port"]) == (addr, named_port)
            assert x["specific_port"] is named_port                    # a bool: the rule tells False from unread
    finally:
        for s in made:
            s.close()


def _row(proto="udp4", addr="0.0.0.0", specific=False, state="established", pid=200, port=50000):
    x = {"proto": proto, "pid": pid, "port": port, "addr": addr}
    if proto.startswith("tcp"):
        x.update(state=state, remote_port=443)
    else:
        x.update(state="bound", specific_port=specific)
    return x


@pytest.mark.parametrize("row, kind, why", [
    (_row(), "utility", None),                         # the shape all 70 of WebView2's UDP sockets had on the runner
    (_row("udp6", "::"), "utility", None),
    (_row(), "browser", "UDP in the browser process"),
    (_row(), "renderer", "UDP in the renderer process"),
    (_row(), "GPU", "UDP in the GPU process"),
    (_row(), None, "UDP in a process WebView2 does not list"),
    (_row(addr="127.0.0.1"), "utility", "UDP bound to 127.0.0.1"),
    (_row("udp6", "::1"), "utility", "UDP bound to ::1"),
    (_row(addr="192.168.0.10"), "utility", "UDP bound to 192.168.0.10"),
    (_row(specific=True), "utility", "UDP on a port its program named"),
    (_row("udp6", "::", specific=True), "utility", "UDP on a port its program named"),
    (_row(specific=None), "utility", "UDP on a port its program named"),       # unread is a server's
    (_row("tcp4", "127.0.0.1", state="listen"), "utility", "listening"),
    (_row("tcp6", "::", state="listen"), "browser", "listening"),
    (_row("tcp4", "192.168.0.10"), "utility", None),
    (_row("tcp4", "192.168.0.10", state="state 3"), "renderer", None),        # a connection, whoever holds it
])
def test_a_webview2_socket_is_a_server_unless_it_has_a_clients_shape(row, kind, why):
    assert ws.server_reason(row, kind) == why


def test_sort_sockets_keeps_the_spikes_own_apart_and_gives_each_server_its_reason():
    own = 100
    found = [_row(pid=own, port=40000),                # a control socket: left out
             _row(pid=own, port=40001),                # any other of the spike's own, even in a client's shape
             _row(pid=200, port=50000),
             _row("tcp4", "192.168.0.10", pid=200, port=50001),
             _row("tcp4", "127.0.0.1", state="listen", pid=300, port=50002),
             _row(pid=300, port=50003)]
    mine, servers, outgoing = ws.sort_sockets(found, own, {200: "utility", 300: "browser"}, {40000})
    assert [x["port"] for x in mine] == [40001]
    assert [(x["port"], x["kind"], x["why"]) for x in servers] == [(50002, "browser", "listening"),
                                                                    (50003, "browser", "UDP in the browser process")]
    assert [(x["port"], x["kind"]) for x in outgoing] == [(50000, "utility"), (50001, "utility")]


# The whole check, in a clean process (pytest's own may hold sockets other tests left): that process plays WebView2's
# browser, and a child it starts holds the sockets under test, as the process kind the case names. "unlisted" leaves
# the process list unread (no WebView2 environment); "blind" and "flagless" break the reader for the control only, and
# "lenient" breaks the rule, so that only the control can still fail the check.
CHECK_RUNNER = r'''
import json, os, subprocess, sys, tempfile
from pathlib import Path
import webview_spike as ws

HOLDER = """
import os, socket, sys
mode = sys.argv[1]
held = []
if mode != "nothing":
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM if mode == "tcp-listen" else socket.SOCK_DGRAM)
    if mode == "udp-client":
        s.connect(("127.0.0.1", 9))
    else:
        s.bind(("127.0.0.1", 0))
        if mode == "tcp-listen":
            s.listen(1)
    held.append(s)
print(os.getpid(), flush=True)
sys.stdin.read()
"""
mode, kind, sabotage, out = sys.argv[1:5]
holder = subprocess.Popen([sys.executable, "-c", HOLDER, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          text=True)
try:
    holder_pid = int(holder.stdout.readline())
    spike = ws.Spike("socket-test", Path(tempfile.mkdtemp()), False, lambda line: None)
    spike.browser_pid = os.getpid()
    if kind != "unlisted":
        spike.process_kinds = lambda: ({os.getpid(): "browser", holder_pid: kind}, None)
    reader = ws.open_sockets
    if sabotage == "blind":
        ws.open_sockets = lambda pids: [x for x in reader(pids) if x["pid"] != os.getpid()]
    elif sabotage == "flagless":
        ws.open_sockets = lambda pids: [dict(x, specific_port=False) if "specific_port" in x else x
                                        for x in reader(pids)]
    elif sabotage == "lenient":
        ws.server_reason = lambda x, kind: None
    spike.check_sockets("sockets-1", "in a test")
    r = spike.results[-1]
    Path(out).write_text(json.dumps({"status": r.status, "measured": r.measured, "detail": r.detail},
                                    default=str), encoding="utf-8")
finally:
    holder.stdin.close()
    holder.wait(10)
'''


@pytest.mark.parametrize("mode, kind, sabotage, status, why", [
    ("nothing", "utility", "none", "PASS", None),
    ("udp-client", "utility", "none", "PASS", None),
    ("udp-client", "renderer", "none", "FAIL", "UDP in the renderer process"),
    ("udp-loopback", "utility", "none", "FAIL", "UDP bound to 127.0.0.1"),
    ("tcp-listen", "utility", "none", "FAIL", "listening"),
    ("nothing", "unlisted", "none", "FAIL", None),
    ("nothing", "utility", "blind", "FAIL", None),
    ("nothing", "utility", "flagless", "FAIL", None),
    ("nothing", "utility", "lenient", "FAIL", None),
])
def test_the_check_fails_on_a_server_in_a_webview2_process_and_counts_its_clients(tmp_path, mode, kind, sabotage,
                                                                                  status, why):
    out = tmp_path / "result.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "scripts"), str(ROOT)]), TEMP=str(tmp_path),
               TMP=str(tmp_path))
    subprocess.run([sys.executable, "-c", CHECK_RUNNER, mode, kind, sabotage, str(out)], cwd=ROOT, env=env,
                   timeout=60, check=True)
    r = json.loads(out.read_text(encoding="utf-8"))
    d = r["detail"]
    assert r["status"] == status, r["measured"]
    assert d["mine"] == []
    assert [x["why"] for x in d["servers"]] == ([why] if why else [])
    clients = [x for x in d["outgoing"] if x["proto"] == "udp4"]
    expect_client = mode == "udp-client" and kind == "utility"
    assert [(x["kind"], x["addr"]) for x in clients] == ([("utility", "0.0.0.0")] if expect_client else [])
    if sabotage == "none":
        assert d["control"] == {"read": 6, "wrong": []}
    else:                                              # the control catches a reader that cannot see it
        assert d["control"]["wrong"] and "were NOT all read back" in r["measured"]
    if kind == "unlisted":
        assert "did NOT name the browser process (no environment)" in r["measured"]


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
TYPED = "typed all 11 actions (41 of 41 input events taken)"


@pytest.fixture(autouse=True)
def _no_real_input(monkeypatch):
    """No test in this file may type, click, move the pointer, take the foreground, post to a window or set a timer on
    the PC that runs it: each of these raises, and a test that needs one replaces it itself."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached real input or a real window: replace the call in the test")
    for name in ("SendInput", "SetCursorPos", "SetForegroundWindow", "ShowWindow", "EndMenu", "PostMessageW",
                 "SetTimer"):
        monkeypatch.setattr(ws, name, refuse)


def test_the_host_names_every_owner_key_as_the_page_does():
    assert ws.KEY_STROKES == {"ctrl+r": ((0x11,), 0x52), "ctrl+p": ((0x11,), 0x50), "ctrl+f": ((0x11,), 0x46),
                              "ctrl+s": ((0x11,), 0x53), "ctrl+shift+i": ((0x11, 0x10), 0x49),
                              "ctrl+plus": ((0x11,), 0xBB), "ctrl+minus": ((0x11,), 0xBD),
                              "alt+arrowleft": ((0x12,), 0x25)}            # WinUser.h's values, written out
    assert (ws.VK_ADD, ws.VK_SUBTRACT, ws.SC_KEYMENU, ws.PROBE_KEY) == (0x6B, 0x6D, 0xF100, 0x51)
    assert set(ws.KEY_STROKES) == set(ws.OWNER_KEYS)
    for combo, (mods, vk) in ws.KEY_STROKES.items():
        assert ws.host_combo(vk, 0x11 in mods, 0x10 in mods, 0x12 in mods) == combo
    assert ws.host_combo(0x6B, True, False, False) == "ctrl+plus"         # the number pad's + and -
    assert ws.host_combo(0x6D, True, False, False) == "ctrl+minus"
    assert ws.host_combo(0xBB, True, True, False) == "ctrl+plus"          # Shift held to type +: left out
    assert ws.host_combo(0x25, True, False, True) == "alt+arrowleft"      # AltGr: a non-US right Alt is Ctrl+Alt
    assert ws.host_combo(0x25, True, False, False) == "ctrl+arrowleft"    # the control: no Alt, no Alt+Left
    assert ws.host_combo(0x52, False, False, False) == "r"                # R without Ctrl: no owner key
    assert ws.host_combo(0x74, True, False, False) is None                # F5: no line of step 2 names it


def test_the_pages_own_naming_follows_the_same_rules():
    js = ws.APP_JS.split("function combo(e) {")[1].split("\n}\n")[0]
    for rule in ("k = 'plus'", "k = 'minus'", "k !== 'plus' && k !== 'minus'", "e.altKey) return 'alt+arrowleft'"):
        assert rule in js
    assert js.index("mods.push('ctrl')") < js.index("mods.push('shift')") < js.index("mods.push('alt')")


def test_the_page_ticks_its_lines_never_takes_its_own_tick_back_and_returns_what_the_step_reads():
    js = ws.APP_JS
    mark = js.split("function markDone(item, by) {")[1].split("\n}\n")[0]
    assert "li.dataset.item === item && li.className !== 'page'" in mark     # a page tick is never turned into 'window'
    items = {item for item, _ in ws.KEYS_CHECKLIST}
    for item in ("right-click", "ctrl-wheel"):
        assert item in items and f"markDone('{item}', 'page')" in js          # each listener ticks a line that exists
    assert "markDone(c, 'page')" in js.split("addEventListener('keydown'")[1].split("});")[0]
    assert "keyseen: (m) => { if (keysWatch) markDone(String(m.item), 'window'); }" in js
    assert "if (b.textContent === m.button) { b.click(); break; }" in js.split("press: (m) =>")[1].split("\n")[0]
    end = js.split("async keys_end() {")[1].split("\n  },")[0]
    for field in ("keys: keysSeen", "key_times: keyTimes", "context_menus: contextMenus", "click_times: clickTimes",
                  "ctrl_wheels: ctrlWheels", "lines"):
        assert field in end                                                  # every field owner_keys reads by name


def test_step_2_lists_every_key_the_wheel_and_a_right_click_with_alt_left_last():
    ids = [item for item, _ in ws.KEYS_CHECKLIST]
    assert set(ids) == set(ws.OWNER_KEYS) | {"ctrl-wheel", "right-click"} and len(ids) == len(set(ids))
    assert ids[-1] == "alt+arrowleft"                                  # a menu mode it starts can take nothing after it


def test_a_ctrl_key_the_window_saw_counts_even_when_the_page_never_got_it():
    ok, text = ws.keys_verdict(NOTHING, False, FOUR, {"ctrl+shift+i", "ctrl+plus", "alt+arrowleft"}, 1, "the effects")
    assert ok is True                                                  # 6 of the 7 Ctrl keys, and a right-click
    assert "6 of 7 Ctrl keys seen by the page or the window (not seen: Ctrl+Minus)" in text
    assert "Ctrl+Shift+I, Ctrl+Plus, Alt+Left reached the window but not the page" in text and "the effects" in text
    assert ws.keys_verdict(NOTHING, False, FOUR, set(), 1, "")[0] is None        # the control: the page's four alone


def test_nothing_happened_counts_only_with_six_ctrl_keys_and_a_right_click():
    six = SEVEN - {"ctrl+minus"}
    assert ws.keys_verdict(NOTHING, False, six, set(), 1, "")[0] is True
    assert ws.keys_verdict(NOTHING, False, six - {"ctrl+plus"}, set(), 1, "")[0] is None    # five Ctrl keys
    five = SEVEN - {"ctrl+plus", "ctrl+minus"}
    assert ws.keys_verdict(NOTHING, False, five, {ws.ALT_LEFT}, 1, "")[0] is None   # Alt+Left is never the sixth
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


def test_any_navigation_zoom_or_browser_window_during_step_2_is_a_change():
    assert not ws.keys_changed(1.25, 1.25, 0, [], [])                  # the control: nothing acted
    assert ws.keys_changed(1.25, 1.25, 0, [{"uri": ws.ENTRY, "allowed": False}], [])    # refused, but a key acted
    assert ws.keys_changed(1.25, 1.5, 0, [], []) and ws.keys_changed(1.25, 1.25, 1, [], [])
    assert ws.keys_changed(1.25, 1.25, 0, [], [{"class": "Chrome_WidgetWin_1"}])
    assert ws.keys_changed(1.25, None, 0, [], [])                      # the view gone: not "nothing"


def test_the_alt_left_story_reads_only_what_came_after_it():
    window = [{"t": 1.0, "combo": "ctrl+r"}, {"t": 5.0, "combo": "alt+arrowleft"}]
    page = [("ctrl+r", 1.0), ("q", 5.5)]
    earlier = [{"t": 2.0, "msg": "menu loop started"}, {"t": 2.5, "msg": "menu loop ended"}]   # a lone Alt before it
    later = [{"t": 5.1, "msg": "system command", "command": "keyboard menu"}, {"t": 5.12, "msg": "menu loop started"},
             {"t": 6.0, "msg": ws.VIEW_LOST_FOCUS}]
    text = ws.alt_left_story(window, page, earlier + later)
    assert text.startswith("Alt+Left reached the window but not the page")
    assert "menu mode started 0.10 s after it;" in text                # not the earlier loop, and not its end
    assert "the view lost the keyboard" in text
    assert "after it, 1 key reached the page, 0 the view's key event and 0 the window or its menu" in text
    text = ws.alt_left_story(window, page, earlier)                    # the control: nothing after Alt+Left
    assert "no menu mode followed" in text and "lost the keyboard" not in text
    assert ws.alt_left_story([], [("ctrl+r", 1.0)], later) == "Alt+Left was seen by neither the window nor the page"
    assert ws.alt_left_story([], [("alt+arrowleft", 3.0)], []).startswith("Alt+Left reached the page but not")


def test_the_alt_left_story_when_both_sides_saw_it_or_it_was_pressed_twice():
    window = [{"t": 5.0, "combo": "alt+arrowleft"}]
    both = ws.alt_left_story(window, [("alt+arrowleft", 5.03)], [{"t": 5.01, "msg": ws.VIEW_LOST_FOCUS}])
    assert both.startswith("Alt+Left reached the window and the page")
    assert "the view lost the keyboard" in both                        # timed from the first sighting, not the last
    assert both.endswith("nothing was pressed after it, so this does not show whether the page kept the keyboard")
    loop = [{"t": 5.2, "msg": "menu loop started"}, {"t": 6.0, "msg": "menu loop ended"}]
    assert "menu mode started 0.20 s after it and ended" in ws.alt_left_story(window, [], loop)
    spanning = [{"t": 4.0, "msg": "menu loop started"}, {"t": 6.0, "msg": "menu loop ended"}]   # began before it
    assert "no menu mode followed" in ws.alt_left_story(window, [], spanning)
    twice = window + [{"t": 7.0, "combo": "alt+arrowleft"}]
    text = ws.alt_left_story(twice, [], [{"t": 7.3, "msg": "menu loop started"}])
    assert text.startswith("Alt+Left (2 presses) reached the window") and "started 0.30 s after it" in text
    keys = [{"t": 7.5, "combo": "ctrl+r"}]                             # a key after the last press: the view saw it
    window_menu = [{"t": 7.6, "msg": "menu got a character"}]
    assert ("after it, 0 keys reached the page, 1 the view's key event and 1 the window or its menu"
            in ws.alt_left_story(twice + keys, [], window_menu))


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


def test_the_key_handler_keeps_each_press_with_the_keys_held_and_ticks_the_page_only_during_step_2(tmp_path,
                                                                                                    monkeypatch):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    handler = spike.handler("ICoreWebView2AcceleratorKeyPressedEventHandler", ws.EVENT, spike.on_accelerator)
    held = {0x11}                                                      # VK_CONTROL
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
    n = len(spike.accel_keys)
    key(0, 0x52, 0x40130001)                                           # held down: a repeat, counted and not kept
    assert len(spike.accel_keys) == n and spike.accel_repeats == 1 and len(spike.calls) == 1
    assert key(1, 0x52, 0xC0130001)["combo"] is None and len(spike.calls) == 1      # let go: kept, no press
    held.add(0x10)                                                     # VK_SHIFT
    assert key(0, 0x49)["combo"] == "ctrl+shift+i" and len(spike.calls) == 2
    held.clear()
    alt = key(2, 0x25, 0x214B0001)                                     # a system key: Alt only in lParam (bit 29)
    assert alt["combo"] == "alt+arrowleft" and alt["alt"] and not alt["ctrl"] and len(spike.calls) == 3
    held.update({0x11, 0x12})                                          # AltGr: Ctrl and Alt, as a plain key-down
    assert key(0, 0x25, 0x014B0001)["combo"] == "alt+arrowleft" and len(spike.calls) == 4    # Alt from GetKeyState
    held.discard(0x12)
    assert key(0, 0x25, 0x014B0001)["combo"] == "ctrl+arrowleft" and len(spike.calls) == 4   # the control: no Alt
    sent = []
    spike.to_page = lambda obj: sent.append(obj) or True
    while spike.calls:
        spike.calls.popleft()()
    assert [m["item"] for m in sent] == ["ctrl+r", "ctrl+shift+i", "alt+arrowleft", "alt+arrowleft"]


def test_the_window_keeps_its_focus_and_menu_messages_and_still_passes_each_on(tmp_path):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    for msg, wp, lp in ((ws.WM_SYSCOMMAND, ws.SC_KEYMENU, 0), (ws.WM_ENTERMENULOOP, 0, 0), (ws.WM_INITMENU, 7, 0),
                        (ws.WM_INITMENUPOPUP, 8, 0x10000), (ws.WM_KILLFOCUS, 0, 0), (ws.WM_ACTIVATE, 0, 0),
                        (ws.WM_ACTIVATE, 0x10001, 0), (ws.WM_ACTIVATE, 2, 0), (ws.WM_SYSKEYUP, 0x12, 0),
                        (ws.WM_SYSCOMMAND, ws.SC_MOVE, 0)):
        assert spike.on_message(0, msg, wp, lp) is None                # None: DefWindowProc still gets it
    assert spike.menu_open                                             # a menu loop started and has not ended
    assert spike.on_message(0, ws.WM_EXITMENULOOP, 0, 0) is None and not spike.menu_open
    assert [e["msg"] for e in spike.focus_events] == [
        "system command", "menu loop started", "a menu became active (nothing shown yet)",
        "a drop-down menu was about to be shown", "window lost focus", "window deactivated",
        "window activated",                                            # LOWORD only: HIWORD says minimized
        "window activated by a click", "window system key up", "system command", "menu loop ended"]
    assert [e.get("command") for e in spike.focus_events if e["msg"] == "system command"] == ["keyboard menu",
                                                                                              "0xF010"]
    assert spike.focus_events[3]["window_menu"] is True
    assert spike.on_message(0, ws.WM_SETFOCUS, 0, 0) == 0              # kept, and its own branch still answers it
    assert spike.focus_events[-1]["msg"] == "window got focus"
    assert spike.on_message(0, ws.WM_SIZE, 0, 0) == 0 and len(spike.focus_events) == 12   # the control: not kept
    spike.note_view_focus(False)
    assert spike.focus_events[-1]["msg"] == ws.VIEW_LOST_FOCUS         # what alt_left_story reads, from one name
    assert "the view lost the keyboard" in ws.alt_left_story([{"t": 0.0, "combo": "alt+arrowleft"}], [],
                                                             spike.focus_events[-1:])


def test_a_full_record_still_follows_menu_mode(tmp_path):
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    spike.focus_events = [{"t": 0.0, "msg": "window got focus"}] * ws.FOCUS_CAP
    assert spike.on_message(0, ws.WM_ENTERMENULOOP, 0, 0) is None
    assert spike.menu_open and len(spike.focus_events) == ws.FOCUS_CAP  # not kept, but followed: type_keys ends it
    spike.on_message(0, ws.WM_EXITMENULOOP, 0, 0)
    assert not spike.menu_open


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


# ------------------------------------------------------------------------------- step 2, driven as the script does --

def _keys_step(tmp_path, *, rehearse=False, probe=False):
    """owner_keys driven as the script driver drives it, each wait answered by hand: no window, no timer, no page."""
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    spike.rehearse, spike.keys_probe, spike.instance = rehearse, probe, "page-1"
    timers, added = [], []
    spike.after = lambda ms, fn: timers.append((ms, fn))
    spike.add = lambda id_, name, ok, measured, **detail: added.append((id_, ok, measured, detail))
    step = spike.owner_keys()
    assert step.send(None).test == "keys_begin"
    owner = step.send({"type": "report", "ok": True, "instance": "page-1"})
    assert isinstance(owner, ws.Owner) and spike.keys_watch            # the window's keys tick the page meanwhile
    return spike, step, owner, timers, added


def _window_saw(spike, *combos):
    for combo in combos:
        spike.accel_keys.append({"t": spike.now(), "kind": 0, "vk": 0, "lp": 0, "ctrl": True, "shift": False,
                                 "alt": combo == ws.ALT_LEFT, "combo": combo})


def _end_keys_step(spike, step, answer, keys, right_clicks=1, lines=None, instance="page-1"):
    end = step.send(answer)
    assert end.test == "keys_end" and not spike.keys_watch
    with pytest.raises(StopIteration):
        step.send({"type": "report", "instance": instance, "keys": list(keys),
                   "key_times": [100 * i for i in range(len(keys))], "context_menus": right_clicks,
                   "click_times": [250], "ctrl_wheels": 1, "lines": lines or {"ctrl+r": "page"}})


def test_step_2_counts_the_ctrl_keys_only_the_window_saw_and_keeps_its_whole_record(tmp_path):
    spike, step, owner, timers, added = _keys_step(tmp_path)
    assert owner.checklist == ws.KEYS_CHECKLIST and owner.auto and not timers      # a person answers; nothing typed
    _window_saw(spike, "ctrl+shift+i", "ctrl+plus", "ctrl+minus", ws.ALT_LEFT)
    _end_keys_step(spike, step, NOTHING, ["ctrl+r", "ctrl+p", "ctrl+f", "ctrl+s"])
    (id_, ok, measured, detail), = added
    assert id_ == "keys" and ok is True and "7 of 7 Ctrl keys seen" in measured
    times = [t for _, t in detail["page_keys"]]
    assert [round(b - a, 3) for a, b in zip(times, times[1:])] == [0.1, 0.1, 0.1]   # page ms, on the spike's clock
    assert detail["right_clicks"] == [round(detail["t_begin"] + 0.25, 3)]
    assert detail["lines"] == {"ctrl+r": "page"} and detail["window_key_count"] == 4
    spike, step, owner, timers, added = _keys_step(tmp_path)           # the control: the window saw nothing
    _end_keys_step(spike, step, NOTHING, ["ctrl+r", "ctrl+p", "ctrl+f", "ctrl+s"])
    assert added[0][1] is None and "too few" in added[0][2]


def test_skip_keeps_step_2s_record_and_names_what_the_spike_saw(tmp_path):
    spike, step, owner, timers, added = _keys_step(tmp_path)
    _window_saw(spike, ws.ALT_LEFT)
    spike.nav_events.append({"uri": ws.ENTRY, "allowed": False, "user": True})
    _end_keys_step(spike, step, "Skip", ["ctrl+r"])
    (id_, ok, measured, detail), = added
    assert ok is None and measured.startswith("skipped; ") and "1 navigation, 0 allowed" in measured
    assert "Alt+Left reached the window but not the page" in measured
    assert detail["window_key_count"] == 1 and detail["page_keys"][0][0] == "ctrl+r"


def test_a_refused_navigation_during_step_2_fails_it(tmp_path):
    spike, step, owner, timers, added = _keys_step(tmp_path)
    _window_saw(spike, "ctrl+shift+i", "ctrl+plus", "ctrl+minus")
    spike.nav_events.append({"uri": ws.ENTRY, "allowed": False, "user": True})       # a reload, refused
    _end_keys_step(spike, step, NOTHING, ["ctrl+r", "ctrl+p", "ctrl+f", "ctrl+s"])
    assert added[0][1] is False


def test_the_typed_rehearsal_answers_by_typing_and_adds_a_check_of_its_own(tmp_path):
    spike, step, owner, timers, added = _keys_step(tmp_path, rehearse=True, probe=True)
    assert not owner.auto and timers == [(1500, spike.type_keys)]      # typing answers, not the page's own click
    _window_saw(spike, *ws.COUNTED_KEYS, ws.ALT_LEFT)
    spike.typing = TYPED
    lines = {item: "page" for item, _ in ws.KEYS_CHECKLIST}
    _end_keys_step(spike, step, NOTHING, ["ctrl+r", "q"], lines=lines)
    assert [a[0] for a in added] == ["keys", "keys-typed"] and added[1][1] is True
    assert "the letter typed after Alt+Left reached the page" in added[1][2] and TYPED in added[0][2]


def test_the_typed_check_fails_on_an_unseen_key_or_line_or_a_change_and_is_not_run_when_nothing_was_typed():
    lines = {item: "page" for item, _ in ws.KEYS_CHECKLIST}
    ok, text = ws.typed_verdict(TYPED, FOUR, SEVEN, 1, 1, lines, False, True)
    assert ok is True and "7 of 7 Ctrl keys seen" in text and "reached the page" in text
    assert ws.typed_verdict(TYPED, FOUR, set(), 1, 1, lines, False, True)[0] is False      # 3 Ctrl keys unseen
    assert ws.typed_verdict(TYPED, SEVEN, set(), 0, 1, lines, False, True)[0] is False     # no right-click
    assert ws.typed_verdict(TYPED, SEVEN, set(), 1, 0, lines, False, True)[0] is False     # no wheel turn
    assert ws.typed_verdict(TYPED, SEVEN, set(), 1, 1, {**lines, "ctrl-wheel": ""}, False, True)[0] is False
    assert ws.typed_verdict(TYPED, SEVEN, set(), 1, 1, lines, True, True)[0] is False      # something acted
    ok, text = ws.typed_verdict(TYPED, SEVEN, set(), 1, 1, {**lines, "alt+arrowleft": ""}, False, False)
    assert ok is True and "did not reach the page" in text             # Alt+Left and the letter: reported only
    stopped = "typed 0 of 11 actions: the window was not in front before ctrl+r"
    assert ws.typed_verdict(stopped, SEVEN, SEVEN, 1, 1, lines, False, True) == (None, "not typed: " + stopped)
    assert ws.typed_verdict(None, SEVEN, SEVEN, 1, 1, lines, False, True)[0] is None


# --------------------------------------------------------------------------- typing, on GitHub's runner only --

def _typing_spike(tmp_path, monkeypatch, on_runner: bool):
    """A spike whose type_keys runs at once and touches nothing real: no window, pointer or input."""
    spike = ws.Spike("main", tmp_path, False, lambda line: None)
    spike.keys_probe, spike.hwnd = True, 0x1234
    monkeypatch.setattr(ws.guard, "on_throwaway_runner", lambda environ=None: on_runner)
    rig = {"front": spike.hwnd, "lost_after": None, "take": {}, "raise_at": None, "menu_on_alt": False,
           "typed": [], "sent": [], "pointer": [], "end_menu": 0}

    def get_cursor(p):
        p._obj.x, p._obj.y = 7, 9                                       # where the pointer was
        return 1

    def end_menu():
        rig["end_menu"] += 1
        spike.menu_open = False
        return 1

    def send(items):
        batch = len(rig["typed"])
        if batch == rig["raise_at"]:
            raise OSError("planted failure in SendInput")
        rig["typed"].append([(i.u.ki.wVk, i.u.ki.dwFlags) if i.type == ws.INPUT_KEYBOARD else
                             ("mouse", i.u.mi.dwFlags, i.u.mi.mouseData) for i in items])
        if rig["menu_on_alt"] and items[0].type == ws.INPUT_KEYBOARD and items[0].u.ki.wVk == 0x12:
            spike.menu_open = True                                      # as if Alt+Left started Windows' menu mode
        if len(rig["typed"]) == rig["lost_after"]:
            rig["front"] = 0x9999                                       # another window comes to the front
        return rig["take"].get(batch, len(items))
    for name in ("ShowWindow", "SetForegroundWindow", "ClientToScreen"):
        monkeypatch.setattr(ws, name, lambda *a: 1)
    monkeypatch.setattr(ws, "GetCursorPos", get_cursor)
    monkeypatch.setattr(ws, "SetCursorPos", lambda x, y: rig["pointer"].append((x, y)) or 1)
    monkeypatch.setattr(ws, "EndMenu", end_menu)
    monkeypatch.setattr(ws, "send_input", send)
    monkeypatch.setattr(ws, "GetForegroundWindow", lambda: rig["front"])
    spike.client_rect = lambda: wintypes.RECT(0, 0, 200, 100)
    spike.after = lambda ms, fn: fn()
    spike.to_page = lambda obj: rig["sent"].append(obj) or True
    return spike, rig


def test_type_keys_refuses_off_githubs_runner_even_with_its_flag_set(tmp_path, monkeypatch):
    spike, rig = _typing_spike(tmp_path, monkeypatch, on_runner=False)
    spike.type_keys()
    assert rig["typed"] == [] and rig["pointer"] == []                 # a flag alone types nothing
    assert spike.typing.startswith("not typed")
    assert rig["sent"] == [{"type": "press", "button": NOTHING}]       # and the step is still answered


def test_type_keys_types_each_action_in_order_and_puts_everything_back(tmp_path, monkeypatch):
    spike, rig = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    spike.type_keys()
    up, ext = ws.KEYEVENTF_KEYUP, ws.KEYEVENTF_EXTENDEDKEY
    typed = rig["typed"]
    assert typed[0] == [(0x11, 0), (0x52, 0), (0x52, up), (0x11, up)]                       # Ctrl+R, released
    assert typed[4] == [(0x11, 0), (0x10, 0), (0x49, 0), (0x49, up), (0x10, up), (0x11, up)]   # last held, first let go
    assert typed[7] == [(0x11, 0), ("mouse", ws.MOUSEEVENTF_WHEEL, 120), (0x11, up)]       # one notch, Ctrl held
    assert typed[8] == [("mouse", ws.MOUSEEVENTF_RIGHTDOWN, 0), ("mouse", ws.MOUSEEVENTF_RIGHTUP, 0)]
    assert typed[9] == [(0x12, 0), (0x25, ext), (0x25, ext | up), (0x12, up)]              # Left on the arrow cluster
    assert typed[10] == [(0x51, 0), (0x51, up)] and len(typed) == 11                       # then a letter
    assert rig["pointer"] == [(100, 50), (100, 50), (7, 9)]           # onto the page twice, then back where it was
    assert rig["end_menu"] == 0 and spike.typing.startswith("typed all 11 actions (41 of 41")
    assert rig["sent"] == [{"type": "press", "button": NOTHING}]


def test_type_keys_ends_a_menu_mode_alt_left_started(tmp_path, monkeypatch):
    spike, rig = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    rig["menu_on_alt"] = True
    spike.type_keys()
    assert rig["end_menu"] == 1 and "the spike then ended Windows' menu mode" in spike.typing
    assert rig["pointer"][-1] == (7, 9) and rig["sent"] == [{"type": "press", "button": NOTHING}]


def test_type_keys_stops_once_the_window_is_not_in_front_or_windows_takes_part_of_a_batch(tmp_path, monkeypatch):
    spike, rig = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    rig["lost_after"] = 3
    spike.type_keys()
    assert len(rig["typed"]) == 3 and "typed 3 of 11 actions: the window was not in front before ctrl+s" in spike.typing
    assert rig["pointer"] == [(7, 9)] and rig["sent"] == [{"type": "press", "button": NOTHING}]
    spike, rig = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    rig["take"] = {4: 2}                                               # Ctrl+Shift+I: only Ctrl and Shift went down
    spike.type_keys()
    assert rig["typed"][5] == [(0x10, ws.KEYEVENTF_KEYUP), (0x11, ws.KEYEVENTF_KEYUP)] and len(rig["typed"]) == 6
    assert "Windows took 2 of the 6 input events for ctrl+shift+i" in spike.typing
    assert rig["sent"] == [{"type": "press", "button": NOTHING}]


def test_type_keys_still_answers_and_puts_the_pointer_back_when_a_step_raises(tmp_path, monkeypatch):
    spike, rig = _typing_spike(tmp_path, monkeypatch, on_runner=True)
    rig["raise_at"] = 1
    spike.type_keys()
    assert len(rig["typed"]) == 1 and "typed 1 of 11 actions: ctrl+p raised" in spike.typing
    assert rig["pointer"] == [(7, 9)] and rig["sent"] == [{"type": "press", "button": NOTHING}]


def test_releases_let_go_of_exactly_what_a_cut_short_batch_left_down():
    keys = ws.stroke((0x11, 0x10), 0x49)                               # Ctrl, Shift, I down; I, Shift, Ctrl up

    def named(items):
        return [(i.u.ki.wVk, i.u.ki.dwFlags) if i.type == ws.INPUT_KEYBOARD else i.u.mi.dwFlags for i in items]
    up = ws.KEYEVENTF_KEYUP
    assert named(ws.releases(keys, 2)) == [(0x10, up), (0x11, up)]     # Shift first, then Ctrl
    assert named(ws.releases(keys, 4)) == [(0x10, up), (0x11, up)]     # I went down and up: not held
    assert ws.releases(keys, 0) == [] and ws.releases(keys, 6) == []
    click = [ws.mouse_input(ws.MOUSEEVENTF_RIGHTDOWN), ws.mouse_input(ws.MOUSEEVENTF_RIGHTUP)]
    assert named(ws.releases(click, 1)) == [ws.MOUSEEVENTF_RIGHTUP] and ws.releases(click, 2) == []
