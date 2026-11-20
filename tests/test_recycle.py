"""Moving a recording's folder to the Recycle Bin (lagdoctor/winapi/recycle.py), with SHFileOperationW replaced: the
real one would put the test's folder in this PC's own Recycle Bin (tests/conftest.py's _no_real_recycle_bin keeps
every other test off it; tests/test_recycle_live.py runs the real one on GitHub's runner)."""
from __future__ import annotations

import ctypes

import pytest

from lagdoctor.winapi import recycle

FOLDER = "D:\\lag-doctor\\sessions\\20261120-100000-moss_garden"


def fake(monkeypatch, *, code: int = 0, aborted: bool = False):
    seen = []

    def file_operation(ref):
        op = ref._obj
        seen.append({"hwnd": op.hwnd, "func": op.wFunc, "flags": op.fFlags, "to": op.pTo,
                     "from": ctypes.wstring_at(op.pFrom, len(FOLDER) + 2), "title": op.lpszProgressTitle})
        op.fAnyOperationsAborted = aborted
        return code
    monkeypatch.setattr(recycle, "_file_operation", file_operation)
    return seen


def test_a_folder_goes_to_the_recycle_bin_by_its_full_path_with_no_dialog_but_windows_warning(monkeypatch):
    seen = fake(monkeypatch)
    assert recycle.to_recycle_bin(FOLDER, 0x1234) is None
    [op] = seen
    assert op["hwnd"] == 0x1234 and op["func"] == recycle.FO_DELETE and op["to"] is None and op["title"] is None
    assert op["from"] == FOLDER + "\0\0"                         # one name, then the NUL that ends the list
    # read past the name's own NUL only as far as the buffer goes: names() gives the name and its NUL, and the buffer
    # adds the one that ends the list
    assert recycle.names(FOLDER) == FOLDER + "\0"
    assert ctypes.sizeof(ctypes.create_unicode_buffer(recycle.names(FOLDER))) == 2 * (len(FOLDER) + 2)
    for flag in ("FOF_ALLOWUNDO", "FOF_WANTNUKEWARNING", "FOF_SILENT", "FOF_NOCONFIRMATION", "FOF_NOERRORUI"):
        assert op["flags"] & getattr(recycle, flag), flag
    assert op["flags"] == recycle.FLAGS


def test_the_flags_and_the_structure_are_the_sdks():
    # shellapi.h (Windows SDK 10.0.26100.0)
    assert (recycle.FO_DELETE, recycle.FOF_SILENT, recycle.FOF_NOCONFIRMATION, recycle.FOF_ALLOWUNDO,
            recycle.FOF_NOERRORUI, recycle.FOF_WANTNUKEWARNING) == (3, 0x4, 0x10, 0x40, 0x400, 0x4000)
    # SHFILEOPSTRUCTW: natural alignment on 64-bit Windows (56 bytes), packed to bytes on 32-bit (30)
    assert ctypes.sizeof(recycle.SHFILEOPSTRUCTW) == (56 if ctypes.sizeof(ctypes.c_void_p) == 8 else 30)


@pytest.mark.parametrize("path", [
    "sessions\\20261120-100000-moss_garden",           # relative: deleted for good even with FOF_ALLOWUNDO
    "\\lag-doctor\\sessions\\x", "D:lag-doctor\\x", "D:\\", "D:", "\\\\server\\share\\sessions\\x",
    "\\\\?\\D:\\lag-doctor\\sessions\\x", "D:/lag-doctor/sessions/x", "D:\\lag-doctor\\..\\x", "D:\\lag-doctor\\.\\x",
    "D:\\lag-doctor\\sessions\\*", "D:\\lag-doctor\\sessions\\2026?", "D:\\lag-doctor\\sessions\\x\\",
    "D:\\lag-doctor\\\\sessions", "D:\\lag-doctor\\x\0D:\\other", "D:\\lag-doctor\\x\nD:\\y", "D:\\lag-doctor\\x.",
    "D:\\lag-doctor\\x ", "D:\\a:b", "D:\\" + "a" * 257, "", None, 5,
])
def test_a_path_that_is_not_a_full_one_is_refused_before_windows_is_called(monkeypatch, path):
    seen = fake(monkeypatch)
    with pytest.raises(ValueError):
        recycle.to_recycle_bin(path, None)
    assert seen == []


def test_the_longest_path_windows_takes_is_handed_over():
    longest = "D:\\" + "a" * (recycle.MAX_PATH_CHARS - 3)
    assert recycle.refused(longest) is None and recycle.refused(longest + "a") is not None


@pytest.mark.parametrize("code, aborted, words", [
    (0, True, "moving it to the Recycle Bin was cancelled"),         # 0 with the flag: the page says this happens
    (recycle.DE_OPCANCELLED, False, "moving it to the Recycle Bin was cancelled"),
    (0x78, False, "Windows could not move it to the Recycle Bin (code 0x78)"),
])
def test_a_move_windows_did_not_make_is_said(monkeypatch, code, aborted, words):
    fake(monkeypatch, code=code, aborted=aborted)
    with pytest.raises(OSError) as raised:
        recycle.to_recycle_bin(FOLDER, None)
    assert raised.value.strerror == words


def test_conftest_keeps_every_test_off_the_real_recycle_bin():
    # its guard is in place here too: a call reads as a move Windows refused, and fails the test at its end
    assert recycle._file_operation.__name__ == "refuse"


def test_the_recycle_bin_guard_fails_a_test_that_reached_the_real_call():
    from tests.conftest import recycle_guard
    mp = pytest.MonkeyPatch()
    windows = []
    # Below the guard's seam, Windows' own function is a stand-in too: a guard that patched nothing (the autouse one
    # shares its code) then fails this test, and still moves nothing into this PC's Recycle Bin.
    mp.setattr(recycle, "_SHFileOperationW", lambda op: windows.append("SHFileOperationW") or 0x78)
    try:
        guard = recycle_guard(mp)
        caught = next(guard)
        with pytest.raises(OSError, match="could not move it"):
            recycle.to_recycle_bin(FOLDER, None)
        assert caught == ["a test reached this PC's real Recycle Bin"] and windows == []
        with pytest.raises(AssertionError, match="a test reached this PC's real Recycle Bin"):
            next(guard)
    finally:
        mp.undo()
