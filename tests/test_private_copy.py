"""Windows may keep what lag-doctor saves in another app's private copy of AppData (paths.private_copy).

The probe itself (live, in a temporary folder, which Windows does not move), its pure reading, and what each
command does about it: cli.main refuses what saves files other windows must find, and only notes it for
what reads.
"""
from __future__ import annotations

import argparse
import copy
import os
import re

import pytest

from lagdoctor import cli, paths
from lagdoctor.capture import session as sess
from lagdoctor.winapi import storage

# Measured on 2026-11-05 from a shell the Claude desktop app started (paths.private_copy's docstring).
LANDED = (r"\\?\C:\Users\A\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\lag-doctor"
          r"\.lagdoctor-probe-1-ab")
COPY = paths.PrivateCopy(app="Claude_pzs8sxrjxfjjc",
                         folder=r"C:\Users\A\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\lag-doctor")


# ------------------------------------------------------------------- reading a probe's path ---

def test_a_probe_that_landed_in_an_apps_local_cache_names_that_app_and_folder():
    assert paths.redirected(r"C:\Users\A\AppData\Local\lag-doctor", LANDED) == COPY


@pytest.mark.parametrize("asked,landed", [
    # landed where it was asked to: every window sees it
    (r"C:\Users\A\AppData\Local\lag-doctor",
     r"\\?\C:\Users\A\AppData\Local\lag-doctor\.lagdoctor-probe-1-ab"),
    # asked for a folder that is itself inside a private copy (LAGDOCTOR_HOME set there on purpose)
    (r"C:\Users\A\AppData\Local\Packages\X_1\LocalCache\Local\ld",
     r"\\?\C:\Users\A\AppData\Local\Packages\X_1\LocalCache\Local\ld\.lagdoctor-probe-1-ab"),
    ("C:/Users/A/AppData/Local/Packages/X_1/LocalCache/Local/ld",                 # the same, with slashes
     r"\\?\C:\Users\A\AppData\Local\Packages\X_1\LocalCache\Local\ld\.lagdoctor-probe-1-ab"),
    # an app's own folder, but not its LocalCache: that is no redirect
    (r"C:\ld", r"\\?\C:\Users\A\AppData\Local\Packages\X_1\Settings\.lagdoctor-probe-1-ab"),
])
def test_a_probe_that_landed_where_it_was_asked_is_no_private_copy(asked, landed):
    assert paths.redirected(asked, landed) is None


def test_the_reading_ignores_case_and_shows_plain_paths():
    got = paths.redirected(r"C:\ld", r"\\?\C:\USERS\A\APPDATA\LOCAL\PACKAGES\App_9\LOCALCACHE\Roaming\ld\.p")
    assert got == paths.PrivateCopy("App_9", r"C:\USERS\A\APPDATA\LOCAL\PACKAGES\App_9\LOCALCACHE\Roaming\ld")
    unc = paths.redirected(r"C:\ld", r"\\?\UNC\srv\share\Packages\App_9\LocalCache\Local\ld\.p")
    assert unc == paths.PrivateCopy("App_9", r"\\srv\share\Packages\App_9\LocalCache\Local\ld")


# ---------------------------------------------------------------------------- the live probe ---

def test_a_new_file_in_a_temporary_folder_lands_there_and_is_gone_afterwards(tmp_path):
    landed = storage.new_file_path(str(tmp_path))
    folder, name = landed.rsplit("\\", 1)
    assert name.startswith(f"{storage.PROBE_PREFIX}{os.getpid()}-")
    # %TEMP% is not moved, not even from a shell the Claude desktop app started (measured 2026-11-05).
    assert os.path.normcase(folder.removeprefix("\\\\?\\")) == os.path.normcase(os.path.realpath(tmp_path))
    assert list(tmp_path.iterdir()) == []                        # Windows deleted it when its handle closed
    assert paths.private_copy(tmp_path) is None


def test_a_relative_home_is_probed_where_it_really_is(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAGDOCTOR_HOME", "relative-home")         # the probe itself takes absolute paths only
    assert paths.private_copy() is None and (tmp_path / "relative-home").is_dir()


def test_a_probe_that_cannot_be_made_raises_rather_than_answers(tmp_path):
    with pytest.raises(OSError):
        storage.new_file_path(str(tmp_path / "not-there"))
    with pytest.raises(ValueError):
        storage.new_file_path("relative")
    with pytest.raises(OSError):
        paths.private_copy(tmp_path / "not-there")


# ---------------------------------------------------------------------- what the commands do ---

@pytest.fixture
def private(monkeypatch):
    """Windows keeps lag-doctor's files in the Claude app's private copy. Lists the folders asked about."""
    asked = []

    def fake(folder=None):
        asked.append(folder)
        return COPY
    monkeypatch.setattr(paths, "private_copy", fake)
    return asked


def _no_commands(monkeypatch) -> list[str]:
    started: list[str] = []
    for name in ("cmd_setup", "cmd_watch", "cmd_fix", "cmd_undo", "cmd_check", "cmd_analyze"):
        monkeypatch.setattr(cli, name, lambda args, n=name: started.append(n) or 0)
    monkeypatch.setattr(cli, "_settle_journal", lambda: started.append("_settle_journal"))
    return started


@pytest.mark.parametrize("argv", [["setup", "--download"], ["watch", "--pid", "4242", "--no-elevate"],
                                  ["fix", "--list"], ["fix", "power_plan", "--yes"], ["undo", "--all"]])
def test_commands_that_save_for_other_windows_refuse_and_say_where_and_why(private, monkeypatch, capsys, argv):
    started = _no_commands(monkeypatch)
    assert cli.main(argv) == cli.EXIT_PRIVATE_COPY
    out = capsys.readouterr().out
    assert out.startswith("Not started: ") and COPY.app in out and COPY.folder in out and "Start menu" in out
    # Refused before the command, and before the journal healing writes anything.
    assert started == [] and private == [paths.home()]


@pytest.mark.parametrize("argv,name", [(["check"], "cmd_check"), (["analyze", "x.ldcap.json.gz"], "cmd_analyze")])
def test_commands_that_only_read_say_where_their_files_go_and_carry_on(private, monkeypatch, capsys, argv, name):
    started = _no_commands(monkeypatch)
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith("Note: ") and COPY.app in out and COPY.folder in out
    # They run, but never heal the journal there: that would write a copy apart from the user's own journal.
    assert started == [name]


def test_a_packaged_python_is_named_as_the_cause_with_the_one_remedy_that_works(private, monkeypatch, capsys):
    # A venv on the Microsoft Store's Python keeps every file in that Python's own private folder, in every window.
    monkeypatch.setattr(storage, "own_package_family", lambda: COPY.app.upper())
    _no_commands(monkeypatch)
    assert cli.main(["setup"]) == cli.EXIT_PRIVATE_COPY
    out = capsys.readouterr().out
    assert "lag-doctor runs on a packaged Python (" in out and "python.org" in out and "Start menu" not in out
    monkeypatch.setattr(storage, "own_package_family", lambda: "Some.Other.App_0123456789abc")
    cli.main(["setup"])
    assert "Start menu" in capsys.readouterr().out


def test_this_process_has_no_package_identity_or_a_well_formed_one():
    # Live: measured "none" in the Claude desktop app's shell, though its AppData files are moved.
    got = storage.own_package_family()
    assert got is None or re.fullmatch(r"[\w.-]+_[a-z0-9]{13}", got), got


def test_a_volume_without_drive_letter_paths_is_probed_by_its_nt_path(monkeypatch, tmp_path):
    # ImDisk and OSFMount RAM disks fail GetFinalPathNameByHandleW's drive-letter form (ERROR_INVALID_FUNCTION).
    asked = []

    def final(handle, flags):
        asked.append(flags)
        if flags & 0x2:                                               # VOLUME_NAME_NT
            return r"\Device\HarddiskVolume9\Users\A\AppData\Local\Packages\App_1\LocalCache\Local\ld\.p"
        raise OSError(22, "Incorrect function", None, 1)
    monkeypatch.setattr(storage, "_final_path_of", final)
    landed = storage.new_file_path(str(tmp_path))
    assert asked == [0, 2] and landed.startswith("\\Device\\")
    assert paths.redirected(r"C:\ld", landed) == paths.PrivateCopy("App_1", r"\Device\HarddiskVolume9\Users\A\AppData"
                                                                            r"\Local\Packages\App_1\LocalCache\Local\ld")
    assert list(tmp_path.iterdir()) == []                             # the probe was made, and is gone

    def never(handle, flags):
        raise OSError(22, "Incorrect function", None, 1)
    monkeypatch.setattr(storage, "_final_path_of", never)
    with pytest.raises(OSError):
        storage.new_file_path(str(tmp_path))


def test_a_check_up_still_runs_in_full_after_the_note(private, monkeypatch, capsys):
    from tests import synth
    inv = copy.deepcopy(synth.laglab("smooth").cap.inventory)
    monkeypatch.setattr(sess, "_collect_inventory", lambda target: copy.deepcopy(inv))
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.index("Note: ") < out.index("settings check-up")


def test_a_probe_that_fails_refuses_what_saves_and_lets_reading_go_on(monkeypatch, capsys):
    def broken(folder=None):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(paths, "private_copy", broken)
    started = _no_commands(monkeypatch)
    assert cli.main(["undo", "--all"]) == 1 and started == []
    assert "cannot save files in" in capsys.readouterr().out
    assert cli.main(["analyze", "x.ldcap.json.gz"]) == 0 and started == ["cmd_analyze"]


def test_without_a_private_copy_every_command_runs_as_before(monkeypatch, capsys):
    monkeypatch.setattr(paths, "private_copy", lambda folder=None: None)
    started = _no_commands(monkeypatch)
    for argv in (["setup"], ["fix", "--list"], ["undo"], ["check"], ["analyze", "x"]):
        assert cli.main(argv) == 0
    assert capsys.readouterr().out == ""
    assert started == ["cmd_setup", "_settle_journal", "cmd_fix", "_settle_journal", "cmd_undo",
                       "_settle_journal", "cmd_check", "cmd_analyze"]


def test_every_command_has_a_policy_and_only_those_that_save_refuse():
    sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == set(cli.PRIVATE_COPY_POLICY)
    assert {c for c, p in cli.PRIVATE_COPY_POLICY.items() if p == "refuse"} == {"setup", "watch", "fix", "undo"}
    assert {c for c, p in cli.PRIVATE_COPY_POLICY.items() if p == "note"} == {"check", "analyze"}
