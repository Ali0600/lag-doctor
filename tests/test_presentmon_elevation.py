"""winapi.elevation: ETW rights of this token, read live and compared with whoami; the UAC relaunch with a fake.

The real ShellExecuteExW is never called: an autouse fixture replaces it with one that fails the test,
and each relaunch test installs a fake that starts an ordinary (unelevated) child process instead.
"""
from __future__ import annotations

import _winapi
import csv
import io
import subprocess
import sys
import uuid

import pytest

from lagdoctor.winapi import elevation

BASE_PY = getattr(sys, "_base_executable", sys.executable)
ADMIN, PLU = elevation.ADMINISTRATORS_SID, elevation.PERFORMANCE_LOG_USERS_SID


@pytest.fixture(autouse=True)
def no_real_uac(monkeypatch):
    def refuse(_info):
        raise AssertionError("a test reached the real ShellExecuteExW")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", refuse)


def whoami_groups() -> dict[str, str]:
    """SID -> attributes for every group in this token, read by whoami (it enumerates TokenGroups)."""
    out = subprocess.run(["whoami", "/groups", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30).stdout
    return {row[2]: row[3] for row in csv.reader(io.StringIO(out)) if len(row) >= 4}


def enabled(groups: dict[str, str], sid: str) -> bool:
    attrs = groups.get(sid, "")
    return "Enabled group" in attrs and "deny only" not in attrs.lower()


# ------------------------------------------------------------------- live, read-only ---

def test_is_elevated_matches_the_tokens_integrity_level():
    levels = [int(sid.rsplit("-", 1)[1]) for sid in whoami_groups() if sid.startswith("S-1-16-")]
    assert levels, "whoami listed no mandatory label"
    assert elevation.is_elevated() == (max(levels) >= 0x3000)          # High (0x3000) or above = elevated


def test_group_membership_matches_whoami():
    groups = whoami_groups()
    if not any("Enabled group" in a for a in groups.values()):
        pytest.skip("whoami's attribute names are not English on this PC")
    assert elevation.in_performance_log_users() == enabled(groups, PLU)
    assert elevation.can_trace() == (enabled(groups, ADMIN) or enabled(groups, PLU))


def test_a_uac_filtered_admin_is_not_an_administrator_for_tracing():
    # This laptop's own case (PM 6.3): Administrators is in the token but deny-only until elevated.
    attrs = whoami_groups().get(ADMIN, "")
    if "deny only" not in attrs.lower():
        pytest.skip("this token's Administrators SID is not deny-only")
    assert elevation._is_member(ADMIN) is False


@pytest.mark.parametrize("members,expect", [(set(), False), ({ADMIN}, True), ({PLU}, True), ({ADMIN, PLU}, True)])
def test_can_trace_is_administrators_or_performance_log_users(monkeypatch, members, expect):
    monkeypatch.setattr(elevation, "_is_member", lambda sid: sid in members)
    assert elevation.can_trace() is expect
    assert elevation.in_performance_log_users() is (PLU in members)


# --------------------------------------------------------------- relaunch, with a fake ---

class FakeShellExecute:
    """Records the SHELLEXECUTEINFOW it gets and starts a plain child that exits with `code`."""

    def __init__(self, code: int = 0, error: int | None = None, handle: bool = True) -> None:
        self.code, self.error, self.give_handle = code, error, handle
        self.seen: dict = {}
        self.handle = None
        self.children: list[subprocess.Popen] = []

    def __call__(self, ref) -> bool:
        info = ref._obj
        self.seen = {name: getattr(info, name) for name, _ in info._fields_}
        if self.error is not None:
            import ctypes
            ctypes.set_last_error(self.error)
            return False
        child = subprocess.Popen([BASE_PY, "-I", "-c", f"import sys, time; time.sleep(0.3); sys.exit({self.code})"])
        self.children.append(child)
        if self.give_handle:
            me = _winapi.GetCurrentProcess()
            self.handle = _winapi.DuplicateHandle(me, child._handle, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS)
            info.hProcess = self.handle
        return True

    def reap(self) -> None:
        for c in self.children:
            c.wait(10)


def test_relaunch_waits_for_the_child_and_returns_its_exit_code(monkeypatch, tmp_path):
    fake = FakeShellExecute(code=7)
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    closed = []
    real_close = elevation._kernel32.CloseHandle
    monkeypatch.setattr(elevation._kernel32, "CloseHandle", lambda h: closed.append(h) or real_close(h))
    argv = [sys.executable, "-m", "lagdoctor", "watch", "--capture-only", "--out", r"C:\a folder\cap.ldcap.json.gz",
            'quote"inside']
    try:
        assert elevation.relaunch_elevated(argv, str(tmp_path)) == 7
    finally:
        fake.reap()
    s = fake.seen
    assert s["cbSize"] == 112 and s["lpVerb"] == "runas" and s["lpFile"] == sys.executable
    assert s["lpParameters"] == subprocess.list2cmdline(argv[1:]) and s["lpDirectory"] == str(tmp_path)
    assert s["fMask"] & elevation.SEE_MASK_NOCLOSEPROCESS and s["fMask"] & elevation.SEE_MASK_NO_CONSOLE
    assert s["nShow"] == elevation.SW_SHOWNORMAL
    assert closed == [fake.handle]                                     # the process handle is not leaked


def test_declining_the_prompt_raises_permission_error(monkeypatch, tmp_path):
    monkeypatch.setattr(elevation, "_ShellExecuteExW", FakeShellExecute(error=elevation.ERROR_CANCELLED))
    with pytest.raises(PermissionError) as info:
        elevation.relaunch_elevated([sys.executable, "-V"], str(tmp_path))
    assert info.value.winerror == 1223


def test_other_launch_failures_raise_oserror_with_the_windows_code(monkeypatch, tmp_path):
    monkeypatch.setattr(elevation, "_ShellExecuteExW", FakeShellExecute(error=2))
    with pytest.raises(OSError) as info:
        elevation.relaunch_elevated([sys.executable, "-V"], str(tmp_path))
    assert info.value.winerror == 2 and not isinstance(info.value, PermissionError)


def test_a_launch_that_returns_no_process_handle_is_an_error(monkeypatch, tmp_path):
    fake = FakeShellExecute(handle=False)
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    try:
        with pytest.raises(OSError, match="no process handle"):
            elevation.relaunch_elevated([sys.executable, "-V"], str(tmp_path))
    finally:
        fake.reap()


@pytest.mark.parametrize("argv,cwd", [
    (["python.exe", "-m", "lagdoctor"], "C:\\"),           # a bare name would be searched on PATH, elevated
    ([sys.executable], "relative\\folder"),
    ([sys.executable, "bad\0arg"], "C:\\"),
    ([sys.executable, "--steam-log", ""], "C:\\"),         # an empty argument: never launched
    ([sys.executable, "line\nbreak"], "C:\\"),              # a control character: never launched either
    ([sys.executable, "tab\there"], "C:\\"),
    ([sys.executable], "C:\\new\nline"),
    ([], "C:\\"),
    (["C:\\no\\such\\program.exe"], "C:\\"),
    ([sys.executable], "C:\\no\\such\\folder"),
])
def test_an_ambiguous_launch_is_refused_before_any_prompt(monkeypatch, argv, cwd):
    fake = FakeShellExecute()
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    with pytest.raises(ValueError):
        elevation.relaunch_elevated(argv, cwd)
    assert fake.seen == {}


def test_launch_returns_at_once_and_wait_gives_the_exit_code(monkeypatch, tmp_path):
    fake = FakeShellExecute(code=3)
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    closed = []
    real_close = elevation._kernel32.CloseHandle
    # Records every close but passes each handle value to Windows once: a second close could hit a reused value.
    monkeypatch.setattr(elevation._kernel32, "CloseHandle",
                        lambda h: closed.append(h) or (real_close(h) if closed.count(h) == 1 else True))
    try:
        child = elevation.launch_elevated([sys.executable, "-V"], str(tmp_path))
        assert child.wait(timeout_s=0) is None                     # still running: a timed wait says so
        assert child.wait() == 3 and child.wait() == 3             # the handle stays usable until closed
        child.close()
        child.close()                                              # closing twice closes once
        assert closed == [fake.handle]
        with pytest.raises(ValueError):
            child.wait()
    finally:
        fake.reap()


def test_a_ctrl_c_while_waiting_leaves_the_child_and_its_handle_alone(monkeypatch, tmp_path):
    fake = FakeShellExecute(code=5)
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    real_wait = elevation._kernel32.WaitForSingleObject
    calls = []

    def interrupted_once(handle, ms):
        calls.append(ms)
        if len(calls) == 1:
            raise KeyboardInterrupt                                 # Ctrl+C arrives during one wait slice
        return real_wait(handle, ms)
    monkeypatch.setattr(elevation._kernel32, "WaitForSingleObject", interrupted_once)
    try:
        child = elevation.launch_elevated([sys.executable, "-V"], str(tmp_path))
        with pytest.raises(KeyboardInterrupt):
            child.wait()
        assert child.wait() == 5                                    # waiting again still works
        child.close()
    finally:
        fake.reap()


def test_token_user_sid_matches_whoami():
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30).stdout
    rows = [r for r in csv.reader(io.StringIO(out)) if len(r) >= 2]
    assert rows, f"whoami printed no user: {out!r}"
    assert elevation.token_user_sid() == rows[0][1] and rows[0][1].startswith("S-1-5-")


def test_a_named_mutex_is_held_by_one_handle_at_a_time():
    name = f"Local\\lagdoctor-test-{uuid.uuid4().hex}"
    first = elevation.hold_named_mutex(name)
    try:
        assert first
        assert elevation.hold_named_mutex(name) is None             # ERROR_ALREADY_EXISTS: held elsewhere
    finally:
        elevation.release_named_mutex(first)
    again = elevation.hold_named_mutex(name)                       # gone with its last handle
    assert again
    elevation.release_named_mutex(again)


@pytest.mark.parametrize("err,expect", [(elevation.ERROR_ACCESS_DENIED, None), (6, OSError)])
def test_a_mutex_this_token_may_not_open_counts_as_held(monkeypatch, err, expect):
    import ctypes

    def denied(attrs, owner, name):
        ctypes.set_last_error(err)                                 # e.g. an elevated child's copy
        return None
    monkeypatch.setattr(elevation, "_CreateMutexW", denied)
    if expect is None:
        assert elevation.hold_named_mutex("Local\\lagdoctor-test-denied") is None
    else:
        with pytest.raises(OSError) as info:
            elevation.hold_named_mutex("Local\\lagdoctor-test-denied")
        assert info.value.winerror == err


@pytest.mark.parametrize("which", ["program", "folder"])
def test_a_relative_name_is_refused_even_when_it_exists_here(monkeypatch, tmp_path, which):
    (tmp_path / "prog.exe").write_bytes(b"")
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path)
    fake = FakeShellExecute()
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    argv, cwd = (["prog.exe"], str(tmp_path)) if which == "program" else ([sys.executable], "work")
    with pytest.raises(ValueError, match="absolute"):
        elevation.relaunch_elevated(argv, cwd)
    assert fake.seen == {}
