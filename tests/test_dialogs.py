"""Windows' Open dialog for Add a game (lagdoctor/winapi/dialogs.py), with GetOpenFileNameW replaced: the real one
waits for a person (tests/conftest.py's _no_real_dialog keeps every other test off it)."""
from __future__ import annotations

import ctypes

import pytest

from lagdoctor.winapi import dialogs

PICKED = "D:\\Games\\Moss Garden\\Moss Garden.exe"


def fake(monkeypatch, *, returns: int = 1, path: str = PICKED, code: int = 0):
    seen = {}

    def get_open_file_name(ref):
        ofn = ref._obj
        seen.update(size=ofn.lStructSize, owner=ofn.hwndOwner, flags=ofn.Flags, title=ofn.lpstrTitle,
                    max_file=ofn.nMaxFile, index=ofn.nFilterIndex,
                    filter=ctypes.wstring_at(ofn.lpstrFilter, len(dialogs.FILTER) + 1))
        if returns:
            ctypes.memmove(ofn.lpstrFile, ctypes.create_unicode_buffer(path), 2 * (len(path) + 1))
        return returns
    monkeypatch.setattr(dialogs, "_get_open_file_name", get_open_file_name)
    monkeypatch.setattr(dialogs, "_extended_error", lambda: code)
    return seen


def test_the_dialog_lists_programs_only_takes_an_existing_file_and_changes_nothing(monkeypatch):
    seen = fake(monkeypatch)
    assert dialogs.pick_program(0x1234) == PICKED
    assert seen["owner"] == 0x1234 and seen["title"] == dialogs.TITLE and seen["index"] == 1
    assert seen["max_file"] == dialogs.MAX_PATH_CHARS
    assert seen["filter"] == "Programs (*.exe)\0*.exe\0\0"          # pairs, then an empty name ends the list
    for flag in ("OFN_EXPLORER", "OFN_FILEMUSTEXIST", "OFN_PATHMUSTEXIST", "OFN_NOCHANGEDIR", "OFN_DONTADDTORECENT",
                 "OFN_HIDEREADONLY"):
        assert seen["flags"] & getattr(dialogs, flag), flag
    assert seen["flags"] == dialogs.FLAGS


def test_the_structure_is_the_size_windows_expects():
    # commdlg.h's OPENFILENAMEW on 64-bit Windows: 152 bytes (on 32-bit, 88)
    assert ctypes.sizeof(dialogs.OPENFILENAMEW) == (152 if ctypes.sizeof(ctypes.c_void_p) == 8 else 88)


def test_closing_the_dialog_without_a_choice_is_none(monkeypatch):
    fake(monkeypatch, returns=0, code=0)
    assert dialogs.pick_program(None) is None


@pytest.mark.parametrize("code, words", [(dialogs.FNERR_BUFFERTOOSMALL, "the program's path is too long"),
                                         (0xFFFF, "Windows could not show it")])
def test_a_dialog_that_failed_says_so_with_windows_code(monkeypatch, code, words):
    fake(monkeypatch, returns=0, code=code)
    with pytest.raises(OSError, match=words) as raised:
        dialogs.pick_program(None)
    assert f"0x{code:04X}" in str(raised.value)


def test_conftest_keeps_every_test_off_the_real_dialog():
    # its guard is in place here too: a call reads as closed without a choice, and fails the test at its end
    assert dialogs._get_open_file_name.__name__ == "refuse" and dialogs._extended_error() == 0


def test_the_dialog_guard_fails_a_test_that_reached_the_dialog():
    from tests.conftest import dialog_guard
    mp = pytest.MonkeyPatch()
    windows = []
    # Below the guard's seam, Windows' own function is a stand-in too: a guard that patched nothing (the autouse one
    # shares its code) then fails this test, and still shows no dialog.
    mp.setattr(dialogs, "_GetOpenFileNameW", lambda ofn: windows.append("GetOpenFileNameW") or 0)
    mp.setattr(dialogs, "_CommDlgExtendedError", lambda: 0)
    try:
        guard = dialog_guard(mp)
        caught = next(guard)
        assert dialogs.pick_program(None) is None              # read as closed, never shown
        assert caught == ["a test opened Windows' real Open dialog"] and windows == []
        with pytest.raises(AssertionError, match="a test opened Windows' real Open dialog"):
            next(guard)
    finally:
        mp.undo()
