"""scripts/etw_access.py: reading who may enable each ETW provider (the E1 probe in
docs/research/install-and-rights.md).

The verdicts come from Windows' own access check (authz.dll) on descriptors built from SDDL text, and that check
is compared with AccessCheck on this process's own token. A verdict for "you" is asserted only where it does not
depend on whether the tests run elevated (no Administrators entry decides it). The live read is checked to be
refused without admin rights, and the UAC relaunch is faked: no test may raise a real prompt.
"""
from __future__ import annotations

import ctypes
import json
import re
import sys
import winreg
from ctypes import wintypes
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import etw_access  # noqa: E402

from lagdoctor.winapi import elevation  # noqa: E402

YOU, LS, SY = etw_access.YOU, etw_access.LOCAL_SERVICE, etw_access.SYSTEM
ENABLE, REALTIME = etw_access.TRACELOG_GUID_ENABLE, etw_access.REALTIME_SESSION
KP, KN, PCL, DXGI = ("Microsoft-Windows-Kernel-Process", "Microsoft-Windows-Kernel-Network", "Nvidia_PCL",
                     "Microsoft-Windows-DXGI")
# The default descriptor as Microsoft's own blog dumped it (docs/research/install-and-rights.md, section 1).
DEFAULT = ("O:BAG:BAD:(A;;0x800;;;WD)(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)(A;;0x120fff;;;NS)(A;;0x120fff;;;BA)"
           "(A;;0xee5;;;LU)(A;;LC;;;MU)(A;;0x800;;;AC)")
# A provider that grants only SYSTEM, LocalService and Administrators (as Kernel-Process reportedly does).
RESTRICTED = "O:BAG:SYD:(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)(A;;0x120fff;;;BA)"
# The same without Administrators: it refuses you whether or not the tests run elevated.
SERVICES_ONLY = "O:BAG:SYD:(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)"


@pytest.fixture(autouse=True)
def no_real_uac(monkeypatch):
    def refuse(_info):
        raise AssertionError("a test reached the real ShellExecuteExW")
    monkeypatch.setattr(elevation, "_ShellExecuteExW", refuse)


def verdicts(sddl: str, right: int = ENABLE) -> dict[str, bool]:
    sd = etw_access.descriptor_from_sddl(sddl)
    with etw_access.Authz() as authz:
        return {client: authz.granted(client, sd, right) for client in etw_access.CLIENTS}


# ------------------------------------------------------------------ Windows' own verdicts ---

@pytest.mark.parametrize("sddl,expect", [
    ("O:BAG:BAD:(A;;0x80;;;LU)", True),                    # the group you would join
    ("O:BAG:BAD:(A;;0x80;;;AU)", True),                    # Authenticated Users: in every signed-in token
    ("O:BAG:BAD:(D;;0x80;;;AU)(A;;0x80;;;LU)", False),     # a deny to a group the token holds comes first
    ("O:BAG:BAD:(A;IO;0x80;;;LU)", False),                 # inherit-only: for objects below, not for this GUID
    ("O:BAG:BAD:(A;;0x80;;;SY)", False),                   # SYSTEM's grant is not yours
    ("O:BAG:SYD:(D;;0x80;;;LU)(A;;0xee5;;;LU)", False),    # a deny first wins
    ("O:BAG:SYD:(A;;0xee5;;;LU)(D;;0x80;;;LU)", True),     # an allow first wins: Windows reads entries in order
    ("O:BAG:SYD:(A;;0x80;;;WD)", True),                    # granted to Everyone
    ("O:BAG:SYD:(A;;0x7f;;;LU)", False),                   # every right but enable
    ("O:BAG:SYD:", False),                                 # an empty DACL grants nothing
    ("O:BAG:SY", True),                                    # no DACL at all grants everything
])
def test_the_verdict_follows_windows_rules(sddl, expect):
    assert verdicts(sddl)[YOU] is expect


@pytest.mark.parametrize("sddl,expect", [
    ("O:BAG:BAD:(A;;0x80;;;LS)", True),
    ("O:BAG:BAD:(A;;0x80;;;SU)", True),                    # SERVICE
    ("O:BAG:BAD:(A;;0x80;;;S-1-2-0)", True),               # LOCAL
    ("O:BAG:BAD:(A;;0x80;;;IU)", False),                   # INTERACTIVE: a service is not signed in at the PC
    ("O:BAG:BAD:(A;;0x80;;;SY)", False),
    ("O:BAG:BAD:(A;;0x80;;;BA)", False),                   # LocalService is no administrator
])
def test_the_local_service_model(sddl, expect):
    assert verdicts(sddl)[LS] is expect


@pytest.mark.parametrize("sddl,expect", [
    ("O:BAG:BAD:(A;;0x80;;;SY)", True),
    ("O:BAG:BAD:(A;;0x80;;;BA)", True),                    # a SYSTEM token holds Administrators, enabled
    ("O:BAG:BAD:(A;;0x80;;;LS)", False),
    ("O:BAG:BAD:(A;;0x80;;;IU)", False),
])
def test_the_system_model(sddl, expect):
    assert verdicts(sddl)[SY] is expect


def test_the_check_for_you_adds_the_group_whatever_this_account_already_holds():
    nobody = "S-1-5-21-1-2-3-4242"                        # a SID no token here holds
    sd = etw_access.descriptor_from_sddl(f"O:BAG:BAD:(A;;0x80;;;{nobody})")
    with etw_access.Authz({"plus": (None, (nobody,)), "as is": (None, ())}) as authz:
        assert authz.granted("plus", sd, ENABLE) is True
        assert authz.granted("as is", sd, ENABLE) is False


def test_under_the_default_descriptor_everyone_lag_doctor_needs_may_trace():
    assert verdicts(DEFAULT, ENABLE) == {YOU: True, LS: True, SY: True}
    assert verdicts(DEFAULT, REALTIME) == {YOU: True, LS: True, SY: True}


def test_a_real_time_session_needs_both_starting_and_reading_it():
    starts_only = "O:BAG:BAD:(A;;0xa0;;;WD)"              # enable, and start a session, but not read one
    assert verdicts(starts_only, ENABLE) == {YOU: True, LS: True, SY: True}
    assert verdicts(starts_only, REALTIME) == {YOU: False, LS: False, SY: False}


def test_a_provider_with_its_own_descriptor_can_shut_the_group_out():
    assert verdicts(SERVICES_ONLY)[YOU] is False
    r = verdicts(RESTRICTED)
    assert r[LS] is True and r[SY] is True


class GENERIC_MAPPING(ctypes.Structure):
    _fields_ = [("GenericRead", wintypes.DWORD), ("GenericWrite", wintypes.DWORD),
                ("GenericExecute", wintypes.DWORD), ("GenericAll", wintypes.DWORD)]


assert ctypes.sizeof(GENERIC_MAPPING) == 16              # winnt.h
TOKEN_QUERY, TOKEN_DUPLICATE, SECURITY_IDENTIFICATION = 0x8, 0x2, 1   # winnt.h
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.DuplicateToken.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
_adv.DuplicateToken.restype = wintypes.BOOL
_adv.AccessCheck.argtypes = [ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(GENERIC_MAPPING),
                             ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(wintypes.BOOL)]
_adv.AccessCheck.restype = wintypes.BOOL
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL


@pytest.fixture(scope="module")
def my_token():
    """An identification-level copy of this process's own token (our own process only, read-only)."""
    primary = wintypes.HANDLE()
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(primary)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        token = wintypes.HANDLE()
        if not _adv.DuplicateToken(primary, SECURITY_IDENTIFICATION, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(primary)
    yield token
    _k32.CloseHandle(token)


def access_check(token, sddl: str, right: int) -> bool:
    """Win32 AccessCheck; a generic mapping of zeros is enough for the specific rights asked here."""
    sd = etw_access.descriptor_from_sddl(sddl)
    buf = ctypes.create_string_buffer(sd, len(sd))
    privileges, size = ctypes.create_string_buffer(256), wintypes.DWORD(256)
    granted, status = wintypes.DWORD(), wintypes.BOOL()
    if not _adv.AccessCheck(buf, token, right, ctypes.byref(GENERIC_MAPPING()), privileges, ctypes.byref(size),
                            ctypes.byref(granted), ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    return bool(status.value)


OBJECT_TYPE, INHERITED_TYPE = "bf967aba-0de6-11d0-a285-00aa003049e2", "4828cc14-1437-45bc-9b07-ad6f015e5f28"
# Where an access check's details show: inherit-only and object entries, deny-only groups (an administrator's
# filtered token), OWNER RIGHTS, a right granted by half. Left out: integrity labels, conditional entries and
# denies for an object type, where authz.dll and AccessCheck were measured to differ (etw_access.not_weighed turns
# those rows into "?").
CROSS_CHECK = [
    ("O:BAG:BAD:(A;;0x80;;;AU)", ENABLE), ("O:BAG:BAD:(A;;0x80;;;LU)", ENABLE),
    ("O:BAG:BAD:(D;;0x80;;;AU)(A;;0x80;;;WD)", ENABLE), ("O:BAG:BAD:(A;IO;0x80;;;WD)", ENABLE),
    ("O:BAG:BAD:(D;;0x80;;;BA)(A;;0x80;;;WD)", ENABLE), ("O:BAG:BAD:(A;;0x80;;;BA)", ENABLE),
    (f"O:BAG:BAD:(OD;;0x80;;{INHERITED_TYPE};WD)(A;;0x80;;;WD)", ENABLE),
    (f"O:BAG:BAD:(OA;;0x80;{OBJECT_TYPE};;WD)", ENABLE), ("O:BAG:BAD:(A;;0x80;;;IU)", ENABLE),
    ("O:BAG:BAD:(A;;0x80;;;BU)", ENABLE), ("O:BAG:BAD:(A;;0x80;;;OW)", ENABLE), ("O:BAG:BAD:", ENABLE),
    ("O:BAG:BA", ENABLE), ("O:BAG:BAD:(A;;0x20;;;WD)", REALTIME), ("O:BAG:BAD:(A;;0x420;;;WD)", REALTIME),
    (DEFAULT, ENABLE), (DEFAULT, REALTIME), (SERVICES_ONLY, ENABLE),
]


def test_the_check_for_you_without_the_group_agrees_with_access_check_on_this_token(my_token):
    pairs = {}
    with etw_access.Authz({"as is": (None, ())}) as authz:
        for sddl, right in CROSS_CHECK:
            pairs[(sddl, right)] = (authz.granted("as is", etw_access.descriptor_from_sddl(sddl), right),
                                    access_check(my_token, sddl, right))
    assert [case for case, (ours, theirs) in pairs.items() if ours is not theirs] == [], pairs
    assert {theirs for _ours, theirs in pairs.values()} == {True, False}     # both answers occur: not vacuous


@pytest.mark.parametrize("sddl", [
    "O:BAG:BAD:(A;;0x80;;;WD)S:(ML;;NW;;;HI)",                              # an integrity label
    'O:BAG:BAD:(XD;;0x80;;;WD;(@User.lagdoctor == "x"))(A;;0x80;;;WD)',     # a conditional deny
    f"O:BAG:BAD:(OD;;0x80;{OBJECT_TYPE};;WD)(A;;0x80;;;WD)",               # a deny for an object type
])
def test_a_descriptor_this_check_does_not_weigh_as_the_kernel_does_reads_as_unknown(sddl):
    judged = etw_access.add_verdicts(saved({DXGI: sddl}))
    row = judged["providers"][DXGI]
    assert "rights" not in row and "does not weigh" in row["check_error"]
    text = etw_access.summary(judged)
    assert table_row(text, DXGI)[3].startswith("could not check: ")
    assert verdict_lines(text)[1] == "A LocalService service would get: frame timing ?; the game-server tracker yes"


def test_every_authz_handle_is_freed_also_when_making_one_fails(monkeypatch):
    makers = ("AuthzInitializeResourceManager", "AuthzInitializeContextFromToken", "AuthzInitializeContextFromSid",
              "AuthzAddSidsToContext")
    freers = ("AuthzFreeContext", "AuthzFreeResourceManager")
    real = {name: getattr(etw_access._authz, name) for name in (*makers, *freers)}
    made, freed = [], []

    def making(name):
        def call(*args):
            ok = real[name](*args)
            if ok:
                made.append(args[-1]._obj.value)              # the handle the out parameter received
            return ok
        return call

    def freeing(name):
        def call(handle):
            freed.append(handle.value if isinstance(handle, ctypes.c_void_p) else handle)
            return real[name](handle)
        return call

    for name in makers:
        monkeypatch.setattr(etw_access._authz, name, making(name))
    for name in freers:
        monkeypatch.setattr(etw_access._authz, name, freeing(name))
    with etw_access.Authz() as authz:
        assert authz.granted(YOU, etw_access.descriptor_from_sddl(DEFAULT), ENABLE)
    assert len(made) == 1 + 2 * len(etw_access.CLIENTS)     # the manager; per client a context and its copy + groups
    assert sorted(freed) == sorted(made)

    made.clear()
    freed.clear()
    copies = []

    def second_copy_fails(*args):                            # LocalService's groups cannot be added
        copies.append(args)
        if len(copies) == 2:
            ctypes.set_last_error(8)                         # ERROR_NOT_ENOUGH_MEMORY
            return 0
        return making("AuthzAddSidsToContext")(*args)
    monkeypatch.setattr(etw_access._authz, "AuthzAddSidsToContext", second_copy_fails)
    with pytest.raises(OSError):
        etw_access.Authz()
    assert len(made) == 4 and sorted(freed) == sorted(made)


# ------------------------------------------------------------------ what the elevated copy reads ---

def test_the_dacl_walk_reads_every_entry_in_order():
    entries = etw_access.aces(etw_access.descriptor_from_sddl(DEFAULT))
    assert [(e["type"], e["flags"], e["mask"], e["sid"]) for e in entries] == [
        ("allow", 0, 0x800, "S-1-1-0"), ("allow", 0, 0x120FFF, "S-1-5-18"), ("allow", 0, 0x120FFF, "S-1-5-19"),
        ("allow", 0, 0x120FFF, "S-1-5-20"), ("allow", 0, 0x120FFF, "S-1-5-32-544"), ("allow", 0, 0xEE5, "S-1-5-32-559"),
        ("allow", 0, 0x4, "S-1-5-32-558"), ("allow", 0, 0x800, "S-1-15-2-1")]


def test_the_listing_keeps_the_flags_and_finds_the_sid_behind_object_types():
    sddl = (f"O:BAG:BAD:(A;IO;0x80;;;LU)(OA;;0x80;{OBJECT_TYPE};;WD)(OD;;0x80;{OBJECT_TYPE};{INHERITED_TYPE};AU)"
            '(XA;;0x80;;;BU;(@User.lagdoctor == "x"))')
    assert etw_access.aces(etw_access.descriptor_from_sddl(sddl)) == [
        {"type": "allow", "flags": 0x8, "mask": 0x80, "sid": "S-1-5-32-559"},
        {"type": "allow-object", "flags": 0, "mask": 0x80, "sid": "S-1-1-0"},
        {"type": "deny-object", "flags": 0, "mask": 0x80, "sid": "S-1-5-11"},
        {"type": "allow-callback", "flags": 0, "mask": 0x80, "sid": "S-1-5-32-545"}]
    assert etw_access.aces(etw_access.descriptor_from_sddl("O:BAG:SY")) is None


def test_the_saved_sddl_reads_back_with_any_integrity_label():
    assert "(A;;0xee5;;;LU)" in etw_access.sddl_of(etw_access.descriptor_from_sddl(DEFAULT))
    labelled = etw_access.sddl_of(etw_access.descriptor_from_sddl("O:BAG:BAD:(A;;0x80;;;WD)S:(ML;;NW;;;HI)"))
    assert labelled.startswith("O:BAG:BAD:(A;;") and labelled.endswith("S:(ML;;NW;;;HI)")    # 0x80 reads back as LO


def test_reading_a_live_descriptor_needs_admin_rights():
    if elevation.is_elevated():
        pytest.skip("this test runs elevated")
    with pytest.raises(PermissionError):
        etw_access.descriptor(etw_access.DEFAULT_GUID)


def test_the_elevated_copy_saves_what_it_read_and_no_verdicts(monkeypatch):
    def fake_descriptor(guid):
        if guid == etw_access.PROVIDERS[PCL][0]:
            raise OSError(87, "The parameter is incorrect")
        return etw_access.descriptor_from_sddl(SERVICES_ONLY if guid == etw_access.PROVIDERS[KP][0] else DEFAULT)
    monkeypatch.setattr(etw_access, "descriptor", fake_descriptor)
    monkeypatch.setattr(etw_access, "own_descriptors", lambda: {etw_access.PROVIDERS[KP][0]})
    rows = etw_access.read_all()["providers"]
    assert list(rows) == [etw_access.DEFAULT_ROW, *etw_access.PROVIDERS]
    assert set(rows[KP]) == {"guid", "own_descriptor", "sddl", "aces"} and rows[KP]["own_descriptor"] is True
    assert rows[KP]["sddl"] == etw_access.sddl_of(etw_access.descriptor_from_sddl(SERVICES_ONLY))
    assert [e["sid"] for e in rows[KP]["aces"]] == ["S-1-5-18", "S-1-5-19"]
    assert set(rows[PCL]) == {"guid", "own_descriptor", "error"} and "87" in rows[PCL]["error"]
    assert rows[KN]["own_descriptor"] is False
    monkeypatch.setattr(etw_access, "own_descriptors", lambda: None)
    assert all(r["own_descriptor"] is None for r in etw_access.read_all()["providers"].values())


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_a_list_of_own_descriptors_cut_short_is_unknown_not_a_no(monkeypatch):
    names = ["{22FB2CD6-0E7B-422B-A0C7-2FAD1FD0E716}", "0811c1af-7a07-4a06-82ed-869455cdf713"]
    monkeypatch.setattr(winreg, "OpenKey", lambda _root, _path: _Key())
    monkeypatch.setattr(winreg, "QueryInfoKey", lambda _key: (0, len(names), 0))
    monkeypatch.setattr(winreg, "EnumValue", lambda _key, i: (names[i], b"", winreg.REG_BINARY))
    assert etw_access.own_descriptors() == {"22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716", etw_access.DEFAULT_GUID}

    def cut_short(_key, i):
        if i:
            raise OSError(259, "No more data is available")
        return names[i], b"", winreg.REG_BINARY
    monkeypatch.setattr(winreg, "EnumValue", cut_short)
    assert etw_access.own_descriptors() is None


def test_the_needed_for_column_rests_on_the_flags_lag_doctor_starts_presentmon_with():
    from lagdoctor.capture import netflows, presentmon
    flags = set(presentmon.FLAGS)
    # PresentMon 2.6.0 enables Intel-PresentMon only with --track_frame_type, Nvidia_PCL only with
    # --track_pc_latency, Win32k and Dwm-Core only while it tracks the display, Direct3D12 never from its console.
    assert etw_access.PROVIDERS["Intel-PresentMon"][1] == (
        etw_access.FRAME_TIMING if "--track_frame_type" in flags else etw_access.UNUSED)
    assert etw_access.PROVIDERS[PCL][1] == (etw_access.FRAME_TIMING if "--track_pc_latency" in flags
                                            else etw_access.UNUSED)
    assert not flags & {"--no_track_display", "--v1_metrics"}, "a provider marked frame timing would be unused"
    assert etw_access.PROVIDERS["Microsoft-Windows-Direct3D12"][1] == etw_access.UNUSED
    assert etw_access.PROVIDERS[KN] == (netflows.KERNEL_NETWORK, etw_access.TRACKER)


# ------------------------------------------------------------------ the summary ---

def saved(changes: dict | None = None) -> dict:
    """A result as the elevated copy saves it: every row under the default descriptor, except `changes`
    (row name -> SDDL text, or the row's fields)."""
    rows = {}
    for name, guid in [(etw_access.DEFAULT_ROW, etw_access.DEFAULT_GUID),
                       *((n, g) for n, (g, _need) in etw_access.PROVIDERS.items())]:
        change = (changes or {}).get(name, DEFAULT)
        rows[name] = {"guid": guid, "own_descriptor": False,
                      **({"sddl": change} if isinstance(change, str) else change)}
    return {"build": 26200, "elevated": True, "providers": rows}


def table_row(text: str, name: str) -> list[str]:
    """The cells of the summary's row for `name` (cells stand two or more spaces apart)."""
    for line in text.splitlines():
        cells = re.split(r" {2,}", line.strip())
        if cells[0] == name:
            return cells
    raise AssertionError(f"no row for {name!r} in:\n{text}")


def verdict_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(("In Performance Log Users", "A LocalService"))]


def test_the_summary_says_what_each_row_is_needed_for_and_who_gets_it():
    text = etw_access.summary(etw_access.add_verdicts(saved({KP: {"sddl": SERVICES_ONLY, "own_descriptor": True},
                                                             KN: SERVICES_ONLY})))
    assert table_row(text, "provider") == ["provider", "own", "needed for", YOU, LS, SY]
    assert table_row(text, etw_access.DEFAULT_ROW) == [etw_access.DEFAULT_ROW, "no", "every session", "Y", "Y", "Y"]
    assert table_row(text, KP) == [KP, "yes", "frame timing (may be refused)", "-", "Y", "Y"]
    assert table_row(text, KN) == [KN, "no", "game-server tracker", "-", "Y", "Y"]
    assert table_row(text, DXGI)[2] == "frame timing"
    assert table_row(text, "Microsoft-Windows-Direct3D12")[2] == "unused"
    assert verdict_lines(text) == [
        "In Performance Log Users, you would get: frame timing yes (without process start and stop events); "
        "the game-server tracker no",
        "A LocalService service would get: frame timing yes; the game-server tracker yes"]


def test_a_row_that_could_not_be_read_leaves_its_verdicts_open():
    unreadable = {"own_descriptor": None, "error": "[WinError 87] The parameter is incorrect"}
    text = etw_access.summary(etw_access.add_verdicts(saved({PCL: unreadable})))
    cells = table_row(text, PCL)
    assert cells == [PCL, "?", "frame timing", "could not read: [WinError 87] The parameter is incorrect"]
    assert verdict_lines(text) == [
        "In Performance Log Users, you would get: frame timing ?; the game-server tracker yes",
        "A LocalService service would get: frame timing ?; the game-server tracker yes"]


def test_one_refusal_settles_a_verdict_an_unreadable_row_leaves_open():
    text = etw_access.summary(etw_access.add_verdicts(saved({PCL: {"error": "unreadable"}, DXGI: SERVICES_ONLY})))
    assert verdict_lines(text)[0] == ("In Performance Log Users, you would get: frame timing no; "
                                      "the game-server tracker yes")


def test_without_the_session_right_neither_recording_works():
    enable_only = "O:BAG:SYD:(A;;0x80;;;WD)(A;;0x120fff;;;SY)(A;;0x120fff;;;LS)"    # you may enable, not start
    text = etw_access.summary(etw_access.add_verdicts(saved({etw_access.DEFAULT_ROW: enable_only})))
    assert table_row(text, etw_access.DEFAULT_ROW)[3:] == ["-", "Y", "Y"]          # the default row shows sessions
    assert verdict_lines(text)[0] == ("In Performance Log Users, you would get: frame timing no; "
                                      "the game-server tracker no")


# ------------------------------------------------------------------ the two windows ---

@pytest.fixture
def unelevated(monkeypatch):
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)


def fake_child(result: dict | None, code: int = 0, launched: list | None = None):
    """A stand-in for the elevated copy: saves `result` (unless None) where it was told, returns `code`."""
    def relaunch(argv, cwd):
        if launched is not None:
            launched.append((argv, cwd))
        if result is not None:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(result), encoding="utf-8")
        return code
    return relaunch


def no_prompt(argv, cwd):
    pytest.fail(f"raised the UAC prompt for {argv}")


def test_without_admin_rights_it_reads_in_one_elevated_copy_and_judges_here(unelevated, monkeypatch, tmp_path, capsys):
    out = tmp_path / "result.json"
    launched = []
    result = saved({KP: {"sddl": SERVICES_ONLY, "own_descriptor": True},
                    PCL: {"own_descriptor": None, "error": "[WinError 87] The parameter is incorrect"}})
    monkeypatch.setattr(elevation, "relaunch_elevated", fake_child(result, launched=launched))
    assert etw_access.main(["--out", str(out)]) == 0
    assert len(launched) == 1
    argv, cwd = launched[0]
    assert argv[-1] == "--child" and all(Path(p).is_absolute() for p in (argv[0], argv[1], cwd))
    assert Path(argv[argv.index("--out") + 1]) == out.resolve()
    text = capsys.readouterr().out
    assert table_row(text, KP) == [KP, "yes", "frame timing (may be refused)", "-", "Y", "Y"]
    assert table_row(text, PCL)[3].startswith("could not read")
    assert "In Performance Log Users, you would get: frame timing ?; the game-server tracker yes" in text
    assert f"Full result (paste it back to the agent): {out.resolve()}" in text


def test_an_administrator_window_is_refused_before_anything_is_read(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    monkeypatch.setattr(elevation, "relaunch_elevated", no_prompt)
    monkeypatch.setattr(etw_access, "read_all", lambda: pytest.fail("read in an administrator window"))
    out = tmp_path / "r.json"
    assert etw_access.main(["--out", str(out)]) == 2
    assert "Run this from a normal, not administrator, PowerShell window" in capsys.readouterr().out
    assert not out.exists()


def test_an_elevated_copy_that_is_not_elevated_reads_nothing_and_never_relaunches(unelevated, monkeypatch, tmp_path,
                                                                                    capsys):
    monkeypatch.setattr(elevation, "relaunch_elevated", no_prompt)
    monkeypatch.setattr(etw_access, "read_all", lambda: pytest.fail("read without admin rights"))
    out = tmp_path / "r.json"
    assert etw_access.main(["--out", str(out), "--child"]) == 3
    assert "not elevated" in capsys.readouterr().out and not out.exists()


def test_a_relative_out_reaches_the_elevated_copy_as_a_full_path(unelevated, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    launched = []
    monkeypatch.setattr(elevation, "relaunch_elevated", fake_child(saved(), launched=launched))
    assert etw_access.main(["--out", "x"]) == 0
    argv = launched[0][0]
    sent = Path(argv[argv.index("--out") + 1])
    assert sent.is_absolute() and sent == (tmp_path / "x").resolve() and sent.parent.samefile(tmp_path)


def test_a_folder_that_does_not_exist_is_refused_before_the_prompt(unelevated, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(elevation, "relaunch_elevated", no_prompt)
    assert etw_access.main(["--out", str(tmp_path / "missing" / "r.json")]) == 2
    assert "is not an existing folder" in capsys.readouterr().out


def test_a_folder_this_account_cannot_create_files_in_is_refused_before_the_prompt(unelevated, monkeypatch, tmp_path,
                                                                                     capsys):
    def denied(folder):
        raise PermissionError(13, "Access is denied", str(folder / "lag-doctor-write-test.tmp"))
    monkeypatch.setattr(etw_access, "_prove_creatable", denied)
    monkeypatch.setattr(elevation, "relaunch_elevated", no_prompt)
    assert etw_access.main(["--out", str(tmp_path / "r.json")]) == 2
    text = capsys.readouterr().out
    assert "could not create and delete a test file in" in text and "Access is denied" in text


def test_the_create_test_leaves_nothing_behind_and_fails_where_it_cannot_create(tmp_path):
    etw_access._prove_creatable(tmp_path)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(OSError):
        etw_access._prove_creatable(tmp_path / "missing")


def test_a_declined_prompt_reads_nothing(unelevated, monkeypatch, tmp_path, capsys):
    def decline(argv, cwd):
        raise PermissionError(13, "the administrator prompt was declined", None, 1223)
    monkeypatch.setattr(elevation, "relaunch_elevated", decline)
    assert etw_access.main(["--out", str(tmp_path / "r.json")]) == 3
    assert "declined" in capsys.readouterr().out


def test_a_failure_in_the_elevated_copy_comes_back_with_its_reason(unelevated, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(elevation, "relaunch_elevated", fake_child({"failed": "Traceback: boom"}, code=1))
    assert etw_access.main(["--out", str(tmp_path / "r.json")]) == 3
    out = capsys.readouterr().out
    assert "ended with code 1" in out and "Traceback: boom" in out


def test_a_saved_file_without_readings_is_no_result(unelevated, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(elevation, "relaunch_elevated", fake_child({"build": 26200}))
    assert etw_access.main(["--out", str(tmp_path / "r.json")]) == 3
    assert "ended with code 0 and saved no result" in capsys.readouterr().out


def test_the_first_window_says_when_the_elevated_copy_could_not_save(unelevated, monkeypatch, tmp_path, capsys):
    out = tmp_path / "r.json"
    monkeypatch.setattr(elevation, "relaunch_elevated", fake_child(None, code=etw_access.CHILD_COULD_NOT_SAVE))
    assert etw_access.main(["--out", str(out)]) == 3
    assert f"The elevated copy could not save its result to {out.resolve()}." in capsys.readouterr().out


def test_when_the_access_check_cannot_run_here_it_says_where_the_readings_are(unelevated, monkeypatch, tmp_path,
                                                                                capsys):
    def no_authz(*_args, **_kwargs):
        raise OSError(1, "no access check here")
    monkeypatch.setattr(etw_access, "Authz", no_authz)
    monkeypatch.setattr(elevation, "relaunch_elevated", fake_child(saved()))
    out = tmp_path / "r.json"
    assert etw_access.main(["--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "access check could not run here" in text and str(out.resolve()) in text


def test_the_elevated_copy_saves_and_prints_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    canned = saved()
    monkeypatch.setattr(etw_access, "read_all", lambda: canned)
    out = tmp_path / "r.json"
    assert etw_access.main(["--out", str(out), "--child"]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == canned and capsys.readouterr().out == ""


def test_an_elevated_copy_that_cannot_save_says_so_with_its_own_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    monkeypatch.setattr(etw_access, "read_all", lambda: 1 / 0)          # and its failure cannot be saved either

    def disk_full(out, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(etw_access, "_save", disk_full)
    assert etw_access.main(["--out", str(tmp_path / "r.json"), "--child"]) == etw_access.CHILD_COULD_NOT_SAVE == 4
    assert "Could not save the result" in capsys.readouterr().out


def test_an_existing_file_is_never_overwritten(monkeypatch, tmp_path, capsys):
    taken = tmp_path / "taken.json"
    taken.write_text("keep me", encoding="utf-8")
    # Asked for directly: refused before any prompt.
    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    monkeypatch.setattr(elevation, "relaunch_elevated", lambda argv, cwd: pytest.fail("prompted for a taken file"))
    assert etw_access.main(["--out", str(taken)]) == 2
    assert "already exists" in capsys.readouterr().out
    # In the elevated copy (the file appeared meanwhile): the write fails, with its own exit code, and replaces nothing.
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    monkeypatch.setattr(etw_access, "read_all", saved)
    assert etw_access.main(["--out", str(taken), "--child"]) == etw_access.CHILD_COULD_NOT_SAVE
    assert "Could not save the result" in capsys.readouterr().out
    assert taken.read_text(encoding="utf-8") == "keep me"
