"""winapi/process.py, live on this machine. Only a child process these tests start is ever changed.

Everything else is only read: our own process, and Microsoft Defender's service process (found
through the service manager), whose refusal is the point. Readouts that do not go through the code
under test: the child reports its own class and create time, WMI reports both, whoami reports our SID.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path

import psutil
import pytest

from lagdoctor.fixes.base import GONE
from lagdoctor.fixes.journal import Journal
from lagdoctor.fixes.priority import BackgroundPriorityFix, Refused
from lagdoctor.winapi import process

BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)   # not the venv launcher (docs/learnings.md)
CHILD_SCRIPT = Path(__file__).resolve().parent / "fixtures" / "settings" / "child.py"
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def _elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _must_not_open(pid, access):
    raise AssertionError(f"process {pid} was opened (access 0x{access:x})")


class Child:
    """A Python child that answers questions about itself (tests/fixtures/settings/child.py)."""

    def __init__(self) -> None:
        self.proc = subprocess.Popen([BASE_PYTHON, "-I", str(CHILD_SCRIPT)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        self.pid = self.proc.pid

    def ask(self, cmd: str) -> int:
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()
        line: list[str] = []
        reader = threading.Thread(target=lambda: line.append(self.proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(10)
        assert line and line[0].strip(), f"the child did not answer {cmd!r}"
        return int(line[0])

    def exit(self) -> None:
        """End the child, keeping Popen's process handle open so Windows cannot give its PID to another
        process while the test still uses it."""
        self.proc.stdin.close()
        self.proc.wait(10)
        assert not self.proc._handle.closed, "Popen closed its handle: the PID could be reused"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        for pipe in (self.proc.stdin, self.proc.stdout):
            if not pipe.closed:
                pipe.close()


@pytest.fixture
def child():
    c = Child()
    try:
        assert c.ask("pid") == c.pid, "Popen's pid is not the process running the script"
        yield c
    finally:
        c.close()


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.json", lock_timeout=2)


@pytest.fixture
def opens(monkeypatch):
    """Every PID passed to the real OpenProcess from here on (the call still goes to Windows)."""
    seen: list[int] = []
    real = process._k32.OpenProcess

    def spy(access, inherit, pid):
        seen.append(pid)
        return real(access, inherit, pid)
    monkeypatch.setattr(process._k32, "OpenProcess", spy)
    return seen


def _wmi(pid: int) -> tuple[int, int]:
    """(create time in FILETIME ticks, base priority) from WMI's Win32_Process."""
    script = (f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
              "'{0} {1}' -f $p.CreationDate.ToFileTimeUtc(), $p.Priority")
    out = subprocess.run([str(SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"), "-NoProfile",
                          "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    ctime, prio = out.stdout.split()
    return int(ctime), int(prio)


def _defender_pid() -> int:
    pid = next((p for p, names in process.service_pids().items() if "WinDefend" in names), None)
    if pid is None:
        pytest.skip("Microsoft Defender (service WinDefend) is not running here")
    return pid


# ------------------------------------------------------------ live: child --

def test_priority_class_matches_what_the_child_reports(child):
    assert process.priority_class(child.pid) == child.ask("prio")


def test_creation_time_matches_the_child_and_wmi(child):
    ctime = process.creation_time(child.pid)
    assert ctime == child.ask("ctime")
    wmi_ctime, _ = _wmi(child.pid)
    assert abs(ctime - wmi_ctime) < 10        # WMI keeps whole microseconds (10 ticks)


def test_lowering_and_restoring_a_child(child):
    ctime = process.creation_time(child.pid)
    process.set_priority_class(child.pid, process.BELOW_NORMAL, create_time=ctime)
    assert child.ask("prio") == process.BELOW_NORMAL
    assert process.priority_class(child.pid, create_time=ctime) == process.BELOW_NORMAL
    assert _wmi(child.pid)[1] == 6            # WMI's base priority of the BELOW_NORMAL class
    process.set_priority_class(child.pid, process.NORMAL, create_time=ctime)
    assert child.ask("prio") == process.NORMAL
    assert _wmi(child.pid)[1] == 8


@pytest.mark.skipif(_elevated(), reason="elevated, REALTIME would really be granted")
def test_a_realtime_request_that_comes_back_high_is_reported(child):
    with pytest.raises(process.PriorityNotApplied) as err:
        process.set_priority_class(child.pid, process.REALTIME)
    assert (err.value.wanted, err.value.got) == (process.REALTIME, process.HIGH)
    assert child.ask("prio") == process.HIGH       # the call said yes; only the readback told the truth
    process.set_priority_class(child.pid, process.NORMAL)
    assert child.ask("prio") == process.NORMAL


def _reused_pid_calls(pid: int, ctime: int) -> list:
    return [lambda: process.priority_class(pid, create_time=ctime),
            lambda: process.set_priority_class(pid, process.IDLE, create_time=ctime),
            lambda: process.same_user(pid, create_time=ctime),
            lambda: process.session_id(pid, create_time=ctime),
            lambda: process.is_service_or_protected(pid, create_time=ctime)]


def test_a_reused_pid_is_refused_before_it_is_opened(child, opens):
    # The journaled PID now names another process (it could be the game): the snapshot tells, no handle.
    ctime = child.ask("ctime")                     # the child's own answer: nothing of ours opened it
    before = child.ask("prio")
    for call in _reused_pid_calls(child.pid, ctime + 1):
        with pytest.raises(process.ProcessGone):
            call()
    assert child.pid not in opens
    assert child.ask("prio") == before             # nothing was set
    assert process.priority_class(child.pid, create_time=ctime) == before
    assert child.pid in opens                      # the spy does see an open: its silence above meant something


def test_a_reused_pid_is_refused_on_the_handle_already_held(child, monkeypatch):
    # The PID can be reused between the snapshot and the open: the handle's own create time still decides.
    ctime = process.creation_time(child.pid)
    before = child.ask("prio")
    monkeypatch.setattr(process, "is_running", lambda pid, create_time: True)    # the snapshot said yes
    for call in _reused_pid_calls(child.pid, ctime + 1):
        with pytest.raises(process.ProcessGone):
            call()
    assert child.ask("prio") == before             # nothing was set
    assert process.priority_class(child.pid, create_time=ctime) == before


def test_running_name_is_the_childs_image_name_and_needs_its_create_time(child, opens):
    ctime = child.ask("ctime")
    assert process.running_name(child.pid, ctime).casefold() == Path(BASE_PYTHON).name.casefold()
    assert process.is_running(child.pid, ctime) is True
    assert process.running_name(child.pid, ctime + 1) is None
    assert process.is_running(child.pid, ctime - 1) is False
    assert opens == []
    for bad in (None, 0, -1, True, 1.5):
        with pytest.raises(ValueError):
            process.running_name(child.pid, bad)
    with pytest.raises(ValueError):
        process.running_name(0, ctime)


def test_running_name_is_none_once_the_child_exited_although_its_handle_is_held(child):
    ctime = child.ask("ctime")
    child.exit()
    assert process.running_name(child.pid, ctime) is None


def test_an_exited_process_is_gone_while_a_handle_still_holds_its_pid(child):
    ctime = process.creation_time(child.pid)
    child.exit()
    calls = [lambda: process.priority_class(child.pid),
             lambda: process.priority_class(child.pid, create_time=ctime),
             lambda: process.creation_time(child.pid),
             lambda: process.same_user(child.pid, create_time=ctime)]
    for call in calls:
        with pytest.raises(process.ProcessGone):
            call()


def test_a_pid_that_does_not_exist_is_gone():
    pid = 0xFFFFFFFC
    assert pid not in psutil.pids()
    with pytest.raises(process.ProcessGone) as err:
        process.priority_class(pid)
    assert err.value.winerror == process.ERROR_INVALID_PARAMETER


def test_our_own_process_and_the_child_are_the_same_user(child):
    assert process.same_user(os.getpid()) is True
    assert process.same_user(child.pid) is True


def test_the_child_is_neither_a_service_nor_protected(child):
    assert process.is_service_or_protected(child.pid) is False


def test_user_sid_matches_whoami():
    out = subprocess.run([str(SYSTEM32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True,
                         text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    sid = out.stdout.strip().split(",")[-1].strip('"')
    assert sid.startswith("S-1-5-")
    assert process.user_sid(os.getpid()) == sid


def test_session_id_matches_process_id_to_session_id():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD()
    assert k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    assert process.session_id(os.getpid()) == session.value != 0


# --------------------------------------------------- live: services, Defender --

def test_service_pids_match_the_service_manager_through_psutil():
    found = process.service_pids()
    checked = 0
    for name in ("Schedule", "EventLog", "WinDefend", "WSearch"):
        try:
            pid = psutil.win_service_get(name).pid()      # QueryServiceStatusEx, no process handle (WT 10)
        except psutil.Error:
            continue
        if pid:
            assert name in found.get(pid, []), f"{name} runs in {pid}; service_pids says {found.get(pid)}"
            checked += 1
    assert checked >= 2                                  # Task Scheduler and Event Log always run
    assert os.getpid() not in found


def test_defender_is_recognised_without_being_opened(monkeypatch):
    pid = _defender_pid()
    monkeypatch.setattr(process, "_open", _must_not_open)
    assert process.is_service_or_protected(pid) is True


@pytest.mark.skipif(_elevated(), reason="the refusal is what an unelevated user gets")
def test_defender_refuses_even_a_limited_query(monkeypatch):
    # WT 9.2: unelevated, MsMpEng.exe refuses PROCESS_QUERY_LIMITED_INFORMATION with error 5.
    # This makes only that read-only attempt.
    pid = _defender_pid()
    with pytest.raises(PermissionError):
        process.session_id(pid)
    monkeypatch.setattr(process, "service_pids", lambda: {})
    assert process.is_service_or_protected(pid) is True   # the refusal alone is enough


# ------------------------------------------------------ logic, nothing opened --

@pytest.mark.parametrize("bad", [0, -4, 2**32, True, 4.0, "4", None])
def test_bad_pids_are_rejected_before_anything_is_opened(monkeypatch, bad):
    monkeypatch.setattr(process, "_open", _must_not_open)
    for call in (process.priority_class, process.creation_time, process.same_user, process.user_sid,
                 process.session_id, process.is_service_or_protected):
        with pytest.raises(ValueError):
            call(bad)
    with pytest.raises(ValueError):
        process.set_priority_class(bad, process.NORMAL)


@pytest.mark.parametrize("bad", [0, -1, 2**63, True, 1.5, "1"])
def test_bad_create_times_are_rejected_before_anything_is_opened(monkeypatch, bad):
    monkeypatch.setattr(process, "_open", _must_not_open)
    monkeypatch.setattr(process, "service_pids", lambda: {})
    with pytest.raises(ValueError):
        process.priority_class(4242, create_time=bad)
    with pytest.raises(ValueError):
        process.is_service_or_protected(4242, create_time=bad)


@pytest.mark.parametrize("cls", [0, 0x00100000, 0x00200000, 0x21, -1, True, None, "normal"])
def test_only_the_six_priority_classes_can_be_set(monkeypatch, cls):
    monkeypatch.setattr(process, "_open", _must_not_open)
    with pytest.raises(ValueError):
        process.set_priority_class(4242, cls)


def test_a_service_pid_counts_without_being_opened(monkeypatch):
    monkeypatch.setattr(process, "service_pids", lambda: {4242: ["Fake"]})
    monkeypatch.setattr(process, "_open", _must_not_open)
    assert process.is_service_or_protected(4242) is True


def test_a_session_zero_process_counts_as_a_service(monkeypatch):
    monkeypatch.setattr(process, "service_pids", lambda: {})
    monkeypatch.setattr(process, "session_id", lambda pid, create_time=None: 0)
    assert process.is_service_or_protected(4242) is True
    monkeypatch.setattr(process, "session_id", lambda pid, create_time=None: 1)
    assert process.is_service_or_protected(4242) is False


def test_a_refused_query_counts_as_protected(monkeypatch):
    def denied(pid, create_time=None):
        raise ctypes.WinError(process.ERROR_ACCESS_DENIED)
    monkeypatch.setattr(process, "service_pids", lambda: {})
    monkeypatch.setattr(process, "session_id", denied)
    assert process.is_service_or_protected(4242) is True


def test_a_gone_process_is_not_called_protected(monkeypatch):
    def gone(pid, create_time=None):
        raise process.ProcessGone(3, "gone")
    monkeypatch.setattr(process, "service_pids", lambda: {})
    monkeypatch.setattr(process, "session_id", gone)
    with pytest.raises(process.ProcessGone):
        process.is_service_or_protected(4242)


class FakeServiceManager:
    """Stands in for advapi32's service calls; it bounds itself so a broken loop cannot run away."""

    def __init__(self, count: int, more: bool) -> None:
        self.count, self.more = count, more
        self.calls = 0
        self.closed = False

    def OpenSCManagerW(self, machine, database, access):
        return 1

    def CloseServiceHandle(self, handle):
        self.closed = True
        return 1

    def EnumServicesStatusExW(self, scm, level, kind, state, buf, size, need, count, resume, group):
        self.calls += 1
        assert self.calls <= 1000, "the service loop did not stop"
        count._obj.value = self.count          # byref() keeps its target in _obj
        need._obj.value = size
        if self.more:
            ctypes.set_last_error(process.ERROR_MORE_DATA)
            return 0
        return 1


def test_service_pids_refuses_a_count_larger_than_its_buffer(monkeypatch):
    fake = FakeServiceManager(count=10**6, more=False)
    monkeypatch.setattr(process, "_adv", fake)
    with pytest.raises(OSError, match="entries"):
        process.service_pids()
    assert fake.closed


def test_service_pids_stops_when_the_list_never_ends(monkeypatch):
    fake = FakeServiceManager(count=0, more=True)
    monkeypatch.setattr(process, "_adv", fake)
    with pytest.raises(OSError, match="did not end"):
        process.service_pids()
    assert fake.calls == 64 and fake.closed


def test_a_token_that_keeps_asking_for_more_room_is_refused(monkeypatch):
    sizes: list[int] = []

    class FakeTokenApi:
        def GetTokenInformation(self, token, cls, buf, size, need):
            sizes.append(size)
            assert len(sizes) < 50, "the token buffer kept growing"
            need._obj.value = size + 1000
            ctypes.set_last_error(process.ERROR_INSUFFICIENT_BUFFER)
            return 0

    monkeypatch.setattr(process, "_adv", FakeTokenApi())
    with pytest.raises(OSError):
        process._token_sid(None)
    assert sizes == [256, 1256, 2256, 3256]          # the next ask (4256) is past the 4096-byte cap


def test_same_user_is_false_for_another_sid(monkeypatch, child):
    real = process.user_sid
    monkeypatch.setattr(process, "user_sid",
                        lambda pid, create_time=None: "S-1-5-18" if pid == child.pid else real(pid, create_time))
    assert process.same_user(child.pid) is False
    assert process.same_user(os.getpid()) is True


# ---------------------------------------- live: background_priority on a child --

def _params(child: Child, **extra) -> dict:
    """As the report suggests it: the child by (pid, create time) and image name; a stand-in game PID."""
    return {"pid": child.pid, "create_time": child.ask("ctime"), "class": "below_normal",
            "exclude": [os.getppid()], "name": Path(BASE_PYTHON).name, **extra}


def test_background_priority_lowers_a_child_and_undo_restores_it(child, journal):
    process.set_priority_class(child.pid, process.NORMAL)     # a known start, whatever pytest itself runs at
    fix = BackgroundPriorityFix()
    plan = fix.plan(_params(child))
    assert (plan.before, plan.after, plan.session_only) == ("normal", "below_normal", True)
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied", res.message
    assert child.ask("prio") == process.BELOW_NORMAL
    [entry] = journal.undoable()
    assert entry.session_only
    res = fix.undo(entry, journal)
    assert res.ok and res.state == "undone", res.message
    assert child.ask("prio") == process.NORMAL


def test_background_priority_undo_after_the_child_exited_settles_without_writing(child, journal):
    process.set_priority_class(child.pid, process.NORMAL)
    fix = BackgroundPriorityFix()
    assert fix.apply(fix.plan(_params(child)), journal).ok
    child.exit()
    [entry] = journal.undoable()
    assert fix.read(entry.params) == GONE
    res = fix.undo(entry, journal)
    assert res.ok and "no longer exists" in res.message
    assert journal.get(entry.id).state == "undone"


def test_background_priority_refuses_the_game_without_opening_it(child, monkeypatch):
    params = _params(child, exclude=[child.pid])      # the child plays the game here
    monkeypatch.setattr(process, "_open", _must_not_open)
    with pytest.raises(Refused, match="game"):
        BackgroundPriorityFix().plan(params)
    same_name = _params(child, game_name=Path(BASE_PYTHON).name.upper())   # the game's same-named sibling
    with pytest.raises(Refused, match="game"):
        BackgroundPriorityFix().plan(same_name)


def test_background_priority_never_opens_a_reused_pid(child, journal, opens):
    # A journal entry outlived its process and the PID went to another one: plan, read and a forced undo all
    # settle it from the snapshot without a handle (DESIGN Principle 1: that process may be the game).
    params = _params(child)
    params["create_time"] += 1
    fix = BackgroundPriorityFix()
    assert fix.read(params) == GONE
    assert fix.plan(params).before == GONE
    entry = journal.begin("background_priority", "x", params, before="normal", after="below_normal",
                          session_only=True)
    journal.set_state(entry.id, "applied")
    res = fix.undo(journal.get(entry.id), journal, force=True)
    assert res.ok and journal.get(entry.id).state == "undone"
    assert child.pid not in opens
    assert fix.ended(params) is True
    assert child.pid not in opens


def test_background_priority_refuses_a_process_with_another_name_without_opening_it(child, opens):
    with pytest.raises(Refused, match="not Discord.exe"):
        BackgroundPriorityFix().plan(_params(child, name="Discord.exe"))
    assert child.pid not in opens


def test_background_priority_refuses_defender(monkeypatch):
    pid = _defender_pid()
    monkeypatch.setattr(process, "_open", _must_not_open)
    with pytest.raises(Refused, match="WinDefend"):
        BackgroundPriorityFix().plan({"pid": pid, "create_time": 1, "class": "below_normal",
                                      "exclude": [os.getppid()], "name": "MsMpEng.exe"})
