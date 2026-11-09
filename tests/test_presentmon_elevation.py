"""winapi.elevation: ETW rights of this token, read live and compared with whoami; the UAC relaunch with a fake.

The real ShellExecuteExW is never called: an autouse fixture replaces it with one that fails the test,
and each relaunch test installs a fake that starts an ordinary (unelevated) child process instead.
"""
from __future__ import annotations

import _winapi
import csv
import ctypes
import io
import os
import subprocess
import sys
import uuid
from ctypes import wintypes

import pytest

from lagdoctor.winapi import elevation

BASE_PY = getattr(sys, "_base_executable", sys.executable)
ADMIN, PLU = elevation.ADMINISTRATORS_SID, elevation.PERFORMANCE_LOG_USERS_SID
ME, OTHER, INTERACTIVE = "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1002", "S-1-5-4"
USERS, EVERYONE, AUTHENTICATED = "S-1-5-32-545", "S-1-1-0", "S-1-5-11"
DENY_ONLY = elevation.SE_GROUP_USE_FOR_DENY_ONLY             # as UAC leaves Administrators in a normal window
ON = elevation.SE_GROUP_MANDATORY | elevation.SE_GROUP_ENABLED_BY_DEFAULT | elevation.SE_GROUP_ENABLED


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


def english_whoami(groups: dict[str, str]) -> None:
    if not any("Enabled group" in a for a in groups.values()):
        pytest.skip("whoami's attribute names are not English on this PC")


def whoami_user_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30).stdout
    sid = out.strip().split(",")[-1].strip('"')
    assert sid.startswith("S-1-5-"), f"whoami printed no user SID: {out!r}"
    return sid


def whoami_logon_sid() -> str:
    out = subprocess.run(["whoami", "/logonid"], capture_output=True, text=True, timeout=30).stdout.strip()
    assert out.startswith("S-1-5-5-"), f"whoami /logonid printed {out!r}"
    return out


@pytest.fixture
def psid():
    """Makes real SIDs (ConvertStringSidToSidW) for staged Windows buffers to point at; frees them afterwards."""
    made: list[ctypes.c_void_p] = []

    def make(text: str) -> int:
        p = ctypes.c_void_p()
        assert elevation._advapi32.ConvertStringSidToSidW(text, ctypes.byref(p)), text
        made.append(p)
        return p.value
    yield make
    for p in made:
        elevation._kernel32.LocalFree(p)


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


# ------------------------------------------------------------------ token groups ---

WHOAMI_BITS = {"Mandatory group": elevation.SE_GROUP_MANDATORY,
               "Enabled by default": elevation.SE_GROUP_ENABLED_BY_DEFAULT,
               "Enabled group": elevation.SE_GROUP_ENABLED,
               "Group used for deny only": elevation.SE_GROUP_USE_FOR_DENY_ONLY}


def test_token_groups_match_whoami():
    # Live, read-only: whoami enumerates this token's groups itself. It leaves out the logon session's SID, which
    # /logonid prints, and the machine's "None" group (RID 513), which a built-in Administrator's token carries:
    # measured on CI's runner (2026-11-07), where the token held 14 groups and whoami listed the other 12. Every
    # attribute name whoami shows must match its bit, and every bit its name.
    listed = whoami_groups()
    english_whoami(listed)
    assert {EVERYONE, AUTHENTICATED} <= set(listed), f"whoami listed too little to compare: {listed}"
    groups = elevation._token_groups()
    logon = whoami_logon_sid()
    machine_none = whoami_user_sid().rsplit("-", 1)[0] + "-513"
    assert set(listed) | {logon} <= set(groups), f"missing from the token read: {(set(listed) | {logon}) - set(groups)}"
    assert set(groups) - set(listed) - {logon} <= {machine_none}, f"unlisted: {set(groups) - set(listed) - {logon}}"
    for sid, attrs in listed.items():
        for name, bit in WHOAMI_BITS.items():
            assert (name in attrs) == bool(groups[sid] & bit), f"{sid} {attrs!r}: {name!r} against 0x{groups[sid]:x}"


def token_groups_buffer(psid, entries: list[tuple[str, int]], count: int | None = None) -> ctypes.Array:
    """A TOKEN_GROUPS laid out as GetTokenInformation returns it, for (SID text, attribute bits) entries;
    `count` overrides its GroupCount."""
    class Groups(ctypes.Structure):
        _fields_ = [("GroupCount", wintypes.DWORD), ("Groups", elevation.SID_AND_ATTRIBUTES * len(entries))]
    staged = Groups()
    staged.GroupCount = len(entries) if count is None else count
    for i, (sid, bits) in enumerate(entries):
        staged.Groups[i].Sid, staged.Groups[i].Attributes = psid(sid), bits
    return ctypes.create_string_buffer(bytes(staged), ctypes.sizeof(staged))


def test_token_groups_reads_every_entry_with_its_bits(monkeypatch, psid):
    buf = token_groups_buffer(psid, [(EVERYONE, ON), (ADMIN, DENY_ONLY), (PLU, 0), (EVERYONE, 0x8)])
    asked = []
    monkeypatch.setattr(elevation, "_token_information", lambda cls: asked.append(cls) or buf)
    assert elevation._token_groups() == {EVERYONE: ON | 0x8, ADMIN: DENY_ONLY, PLU: 0}   # a repeated SID keeps both
    assert asked == [elevation.TOKEN_GROUPS_CLASS] == [2]                                  # TokenGroups (winnt.h)


def test_a_group_count_the_buffer_cannot_hold_is_an_error_not_a_read_past_it(monkeypatch, psid):
    buf = token_groups_buffer(psid, [(EVERYONE, ON)], count=2)
    monkeypatch.setattr(elevation, "_token_information", lambda cls: buf)
    with pytest.raises(OSError, match="does not fit"):
        elevation._token_groups()


@pytest.mark.parametrize("members,expect", [(set(), False), ({ADMIN}, True), ({PLU}, True), ({ADMIN, PLU}, True)])
def test_can_trace_is_administrators_or_performance_log_users(monkeypatch, members, expect):
    monkeypatch.setattr(elevation, "_is_member", lambda sid: sid in members)
    assert elevation.can_trace() is expect
    assert elevation.in_performance_log_users() is (PLU in members)


# ----------------------------------------------------------------- group members ---

def powershell_members(group_sid: str) -> list[str]:
    """The group's member SIDs as PowerShell's Get-LocalGroupMember reads them (another path to the same SAM)."""
    ps = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                         f"Get-LocalGroupMember -SID {group_sid} | ForEach-Object {{ $_.SID.Value }}"],
                        capture_output=True, text=True, timeout=60)
    if ps.returncode != 0:
        pytest.skip(f"Get-LocalGroupMember failed here: {ps.stderr.strip()[:200]}")
    return sorted(line.strip() for line in ps.stdout.splitlines() if line.strip())


@pytest.mark.parametrize("group", ["S-1-5-32-544", "S-1-5-32-545"])     # Administrators; Users
def test_group_members_match_powershell_for_groups_that_have_members(group):
    # Both groups have members on every PC (its first account; INTERACTIVE and Authenticated Users in Users),
    # so an empty answer cannot pass by matching an empty group.
    expect = powershell_members(group)
    assert expect, f"PowerShell lists no members of {group}"
    assert sorted(elevation.group_member_sids(group)) == expect


def test_members_that_are_groups_are_listed_not_expanded():
    assert {"S-1-5-4", "S-1-5-11"} <= set(elevation.group_member_sids("S-1-5-32-545"))   # INTERACTIVE, Authenticated Users


def test_a_group_that_does_not_exist_raises():
    with pytest.raises(OSError):
        elevation.group_member_sids("S-1-5-32-999")


def test_a_member_list_that_never_finishes_raises_instead_of_looping(monkeypatch):
    calls = []

    def more_forever(server, name, level, buf, pref, read, total, resume):
        calls.append(name)
        return elevation.ERROR_MORE_DATA                 # nothing read, buffer left NULL
    monkeypatch.setattr(elevation, "_NetLocalGroupGetMembers", more_forever)
    with pytest.raises(OSError, match="did not finish"):
        elevation.group_member_sids("S-1-5-32-545")
    assert len(calls) == 1


def test_an_error_from_the_member_list_raises(monkeypatch):
    calls = []

    def not_found(*args):
        calls.append(args)
        return 2220                                              # NERR_GroupNotFound (lmerr.h)
    monkeypatch.setattr(elevation, "_NetLocalGroupGetMembers", not_found)
    with pytest.raises(OSError) as info:
        elevation.group_member_sids(USERS)
    assert info.value.winerror == 2220 and len(calls) == 1      # Windows' own code, after one call


@pytest.fixture
def freed(monkeypatch):
    """Records each buffer group_member_sids frees instead of freeing it: a staged buffer is not Windows'."""
    out: list[int] = []
    monkeypatch.setattr(elevation, "_NetApiBufferFree", lambda buf: out.append(buf.value) or 0)
    return out


def members_page(psid, *sids: str) -> ctypes.Array:
    """A LOCALGROUP_MEMBERS_INFO_0 array of real SIDs, as NetLocalGroupGetMembers hands one back."""
    page = (elevation.LOCALGROUP_MEMBERS_INFO_0 * len(sids))()
    for i, sid in enumerate(sids):
        page[i].lgrmi0_sid = psid(sid)
    return page


def test_a_member_list_in_two_pages_is_read_whole_and_in_order(monkeypatch, psid, freed):
    pages = [members_page(psid, ME, INTERACTIVE), members_page(psid, AUTHENTICATED)]
    seen = []

    def paged(server, name, level, buf, pref, read, total, resume):
        seen.append((level, pref, resume._obj.value))
        page = pages[len(seen) - 1]                              # a third call fails the test (IndexError)
        buf._obj.value, read._obj.value, total._obj.value = ctypes.addressof(page), len(page), 3
        if len(seen) == 1:
            resume._obj.value = 4242                             # where Windows goes on from next time
            return elevation.ERROR_MORE_DATA
        return elevation.NERR_SUCCESS
    monkeypatch.setattr(elevation, "_NetLocalGroupGetMembers", paged)
    assert elevation.group_member_sids(USERS) == [ME, INTERACTIVE, AUTHENTICATED]
    assert seen == [(0, elevation.MAX_PREFERRED_LENGTH, 0), (0, elevation.MAX_PREFERRED_LENGTH, 4242)]
    assert freed == [ctypes.addressof(p) for p in pages]         # each page's buffer, once, after reading it


def test_a_member_list_that_keeps_saying_more_stops_after_max_member_pages(monkeypatch, psid, freed):
    page = members_page(psid, INTERACTIVE)
    calls = []

    def more_forever(server, name, level, buf, pref, read, total, resume):
        calls.append(1)
        if len(calls) > elevation.MAX_MEMBER_PAGES:
            raise AssertionError("asked for a page after MAX_MEMBER_PAGES of them")   # fails instead of hanging
        buf._obj.value, read._obj.value = ctypes.addressof(page), len(page)
        return elevation.ERROR_MORE_DATA                         # entries every time, and never the end
    monkeypatch.setattr(elevation, "_NetLocalGroupGetMembers", more_forever)
    with pytest.raises(OSError, match="did not finish"):
        elevation.group_member_sids(USERS)
    assert len(calls) == elevation.MAX_MEMBER_PAGES == 1000
    assert freed == [ctypes.addressof(page)] * 1000


def test_joined_counts_a_member_group_that_is_enabled_in_this_token(monkeypatch):
    # Live: Users lists INTERACTIVE on every PC, and an interactive sign-in's token has it enabled.
    groups = whoami_groups()
    english_whoami(groups)
    if not enabled(groups, INTERACTIVE):
        pytest.skip(f"INTERACTIVE is not enabled in this token (whoami: {groups.get(INTERACTIVE)!r})")
    assert elevation._joined(USERS) is True
    monkeypatch.setattr(elevation, "token_user_sid", lambda: OTHER)     # now no listed member is "this account",
    assert elevation._joined(USERS) is True                             # so a member enabled here was counted


def test_joined_agrees_with_powershell_for_performance_log_users():
    # Live: the members by PowerShell, the token by whoami. False on a PC where nobody joined (this one).
    me, groups = whoami_user_sid(), whoami_groups()
    english_whoami(groups)
    listed = powershell_members(PLU)
    assert elevation._joined(PLU) == (me in listed or any(enabled(groups, s) for s in listed))


@pytest.mark.parametrize("can,groups,members,token,expect", [
    (True, {}, [], set(), "now"),
    (True, {PLU: DENY_ONLY}, [ME], set(), "now"),                    # a token that may trace comes first
    (False, {PLU: DENY_ONLY}, [ME], set(), "filtered"),              # in the token but off: no sign-out helps
    (False, {PLU: ON | DENY_ONLY}, [ME], set(), "filtered"),         # deny-only outweighs an enabled bit
    (False, {PLU: ON}, [ME], set(), "after_sign_in"),                # on in the token is not "switched off"
    (False, {ADMIN: DENY_ONLY}, [ME], set(), "after_sign_in"),       # this account joined after this sign-in began
    (False, {}, [INTERACTIVE], {INTERACTIVE}, "after_sign_in"),     # through a group this token has
    (False, {}, [OTHER], set(), "no"),                              # someone else is a member
    (False, {}, [], set(), "no"),
])
def test_trace_rights_tells_a_pending_sign_in_apart(monkeypatch, can, groups, members, token, expect):
    monkeypatch.setattr(elevation, "can_trace", lambda: can)
    monkeypatch.setattr(elevation, "_token_groups", lambda: dict(groups))
    monkeypatch.setattr(elevation, "token_user_sid", lambda: ME)
    monkeypatch.setattr(elevation, "_is_member", lambda sid: sid in token)
    monkeypatch.setattr(elevation, "group_member_sids", lambda sid: list(members) if sid == PLU else [OTHER])
    assert elevation.trace_rights() == expect


def test_a_switched_off_group_is_told_without_reading_the_member_list(monkeypatch):
    # The token alone says it, so a member list that cannot be read does not turn "filtered" into "unknown".
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(elevation, "_token_groups", lambda: {PLU: DENY_ONLY})

    def refuse(sid):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(elevation, "group_member_sids", refuse)
    assert elevation.trace_rights() == "filtered"


@pytest.mark.parametrize("fails", ["can_trace", "_token_groups", "token_user_sid", "group_member_sids", "_is_member"])
def test_trace_rights_that_cannot_be_read_are_unknown_not_no(monkeypatch, fails):
    monkeypatch.setattr(elevation, "can_trace", lambda: False)
    monkeypatch.setattr(elevation, "_token_groups", lambda: {})
    monkeypatch.setattr(elevation, "token_user_sid", lambda: ME)
    monkeypatch.setattr(elevation, "group_member_sids", lambda sid: [OTHER])
    monkeypatch.setattr(elevation, "_is_member", lambda sid: False)

    def refuse(*args):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(elevation, fails, refuse)                # each reading in turn is the one that fails
    assert elevation.trace_rights() == "unknown"


def test_trace_rights_agree_with_whoami_and_powershell():
    # Live, read-only: the token by whoami, the group's members by PowerShell.
    me, groups = whoami_user_sid(), whoami_groups()
    english_whoami(groups)
    if enabled(groups, ADMIN) or enabled(groups, PLU):
        expect = "now"
    elif PLU in groups:
        expect = "filtered"
    else:
        listed = powershell_members(PLU)
        expect = "after_sign_in" if (me in listed or any(enabled(groups, s) for s in listed)) else "no"
    assert elevation.trace_rights() == expect


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


def test_the_prompt_belongs_to_the_window_it_is_given_and_to_none_by_default(monkeypatch, tmp_path):
    for kw, want in (({}, None), ({"hwnd": None}, None), ({"hwnd": 0x10A2C}, 0x10A2C)):
        fake = FakeShellExecute()
        monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
        try:
            elevation.launch_elevated([sys.executable, "-V"], str(tmp_path), **kw).close()
        finally:
            fake.reap()
        assert fake.seen["hwnd"] == want, (kw, fake.seen["hwnd"])


def test_a_handle_handed_to_another_process_is_one_it_can_wait_on(monkeypatch, tmp_path):
    fake = FakeShellExecute(code=9)
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    # The recorder the window started: a process of its own, whose handle (from starting it) may take a copy.
    recorder = subprocess.Popen([BASE_PY, "-I", "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    try:
        child = elevation.launch_elevated([sys.executable, "-V"], str(tmp_path))
        pid = child.pid
        value = child.hand_over(int(recorder._handle))
        with pytest.raises(ValueError):                     # its own copy is closed
            child.wait(0)
        with pytest.raises(ValueError):
            child.hand_over(int(recorder._handle))
        here = _winapi.GetCurrentProcess()                  # the copy is the recorder's: take it back from there
        back = _winapi.DuplicateHandle(int(recorder._handle), value, here, 0, False,
                                       _winapi.DUPLICATE_SAME_ACCESS | _winapi.DUPLICATE_CLOSE_SOURCE)
        adopted = elevation.ElevatedChild.adopt(back)
        try:
            assert adopted.pid == pid and adopted.wait(10) == 9
        finally:
            adopted.close()
    finally:
        recorder.stdin.close()
        recorder.wait(10)
        fake.reap()


@pytest.mark.parametrize("value", [0, -1, True, 1 << 64, "0x1a4", None])
def test_adopt_refuses_what_is_no_handle_value(value):
    with pytest.raises(ValueError):
        elevation.ElevatedChild.adopt(value)


def test_adopt_refuses_a_handle_that_is_not_a_process_and_leaves_it_open():
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateEventW.restype = ctypes.c_void_p
    k32.SetEvent.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    event = k32.CreateEventW(None, True, False, None)
    try:
        with pytest.raises(ValueError, match="not a process"):
            elevation.ElevatedChild.adopt(event)
        assert k32.SetEvent(event)                          # still open: it may be one of this process's own
    finally:
        k32.CloseHandle(event)


@pytest.mark.parametrize("hwnd", [0, -1, True, 1.5, "0x10a2c", 1 << 64])
def test_a_bad_window_handle_is_refused_before_any_prompt(monkeypatch, tmp_path, hwnd):
    fake = FakeShellExecute()
    monkeypatch.setattr(elevation, "_ShellExecuteExW", fake)
    with pytest.raises(ValueError):
        elevation.launch_elevated([sys.executable, "-V"], str(tmp_path), hwnd=hwnd)
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


def test_a_zero_wait_on_the_elevated_child_is_a_poll_and_names_its_pid(monkeypatch):
    # The relay's launch polls its process between connection tries: a 0 s wait must not block a 250 ms slice.
    asked = []

    def wait(handle, ms):
        asked.append(ms)
        return elevation.WAIT_TIMEOUT
    monkeypatch.setattr(elevation._kernel32, "WaitForSingleObject", wait)
    me = _winapi.GetCurrentProcess()
    info = elevation.SHELLEXECUTEINFOW()
    info.hProcess = _winapi.DuplicateHandle(me, me, me, 0, False, _winapi.DUPLICATE_SAME_ACCESS)
    child = elevation.ElevatedChild(info)
    try:
        assert child.pid == os.getpid()
        assert child.wait(0) is None and asked == [0]
        assert child.wait(0.3) is None and asked[1] == 250 and 0 <= asked[-1] <= 250
    finally:
        child.close()
