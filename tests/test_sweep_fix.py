"""game_setting, the journaled edit a settings sweep makes (lagdoctor/fixes/game_setting.py)."""
from __future__ import annotations

import ast
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from lagdoctor import cli
from lagdoctor.capture import gamefiles
from lagdoctor.fixes import catalog, game_setting
from lagdoctor.fixes.game_setting import GameSettingFix
from lagdoctor.fixes.journal import Journal

ROOT = Path(__file__).resolve().parents[1]
RAW = (b"[ScalabilityGroups]\r\nsg.ShadowQuality=2\r\nsg.StaticMeshQuality=3\r\n\r\n"
       b"[/Script/Engine.GameUserSettings]\r\nbUseVSync=False\r\n")


@pytest.fixture
def ini(tmp_path) -> Path:
    path = tmp_path / "My Games" / "Saved" / "Config" / "WindowsNoEditor" / "GameUserSettings.ini"
    path.parent.mkdir(parents=True)
    path.write_bytes(RAW)
    return path


@pytest.fixture
def journal(tmp_path) -> Journal:
    return Journal(tmp_path / "journal.json", lock_timeout=0.3)


def _params(ini: Path, value: str = "1", key: str = "sg.StaticMeshQuality", section: str = "ScalabilityGroups"):
    return {"file": str(ini), "section": section, "key": key, "value": value, "game": "Game.exe"}


class Running:
    """The game's process, as the fix's check sees it."""

    def __init__(self, on: bool = False) -> None:
        self.on = on
        self.asked: list[str] = []

    def __call__(self, game: str) -> bool:
        self.asked.append(game)
        return self.on


def test_apply_journals_then_changes_only_the_value_and_undo_restores_the_bytes(ini, journal):
    fix = GameSettingFix(running=Running())
    plan = fix.plan(_params(ini))
    assert (plan.before, plan.after, plan.needs_restart) == ("3", "1", "game")
    res = fix.apply(plan, journal)
    assert res.ok and res.state == "applied"
    assert ini.read_bytes() == RAW.replace(b"sg.StaticMeshQuality=3", b"sg.StaticMeshQuality=1")
    (entry,) = journal.entries()
    assert (entry.fix_id, entry.before, entry.after, entry.state) == ("game_setting", "3", "1", "applied")
    assert fix.undo(entry, journal).ok
    assert ini.read_bytes() == RAW and journal.get(entry.id).state == "undone"
    assert not list(ini.parent.glob("*" + game_setting.TEMP_SUFFIX))


def test_nothing_is_written_while_the_game_runs(ini, journal):
    running = Running(on=True)
    fix = GameSettingFix(running=running)
    res = fix.apply(fix.plan(_params(ini)), journal)
    assert not res.ok and "close it first" in res.message
    assert ini.read_bytes() == RAW and running.asked == ["Game.exe"]
    assert journal.entries()[0].state == "failed"


def test_an_undo_waits_until_the_game_has_closed(ini, journal):
    running = Running()
    fix = GameSettingFix(running=running)
    entry_id = fix.apply(fix.plan(_params(ini)), journal).entry_id
    running.on = True
    res = fix.undo(journal.get(entry_id), journal)
    assert not res.ok and "close it first" in res.message and journal.get(entry_id).state == "applied"
    assert b"sg.StaticMeshQuality=1" in ini.read_bytes()
    running.on = False
    assert fix.undo(journal.get(entry_id), journal).ok and ini.read_bytes() == RAW


def test_a_value_the_game_or_player_changed_since_is_left_alone_unless_forced(ini, journal):
    fix = GameSettingFix(running=Running())
    entry_id = fix.apply(fix.plan(_params(ini)), journal).entry_id
    ini.write_bytes(ini.read_bytes().replace(b"sg.StaticMeshQuality=1", b"sg.StaticMeshQuality=2"))
    res = fix.undo(journal.get(entry_id), journal)
    assert not res.ok and res.state == "conflict" and "--force" in res.message
    assert b"sg.StaticMeshQuality=2" in ini.read_bytes()
    assert fix.undo(journal.get(entry_id), journal, force=True).ok and ini.read_bytes() == RAW


def test_a_read_only_file_is_left_alone(ini, journal):
    os.chmod(ini, stat.S_IREAD)
    try:
        fix = GameSettingFix(running=Running())
        res = fix.apply(fix.plan(_params(ini)), journal)
        assert not res.ok and "read-only" in res.message
        assert ini.read_bytes() == RAW
        assert not list(ini.parent.glob("*" + game_setting.TEMP_SUFFIX))
    finally:
        os.chmod(ini, stat.S_IREAD | stat.S_IWRITE)


def test_a_file_onedrive_keeps_online_only_is_never_opened(ini, monkeypatch):
    real = os.stat(ini)
    placeholder = SimpleNamespace(st_mode=real.st_mode, st_size=real.st_size, st_nlink=1,
                                  st_file_attributes=gamefiles.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    monkeypatch.setattr(game_setting, "_file_stat", lambda path: placeholder)     # never os.stat: every module's

    def never(*a, **kw):
        raise AssertionError("an online-only file was opened")
    monkeypatch.setattr(game_setting, "open", never, raising=False)
    with pytest.raises(ValueError, match="online only"):
        GameSettingFix(running=Running()).read(_params(ini))


def test_a_key_the_file_does_not_have_is_refused_at_the_plan(ini):
    with pytest.raises(LookupError, match="sg.Missing"):
        GameSettingFix(running=Running()).plan(_params(ini, key="sg.Missing"))


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"file": r"\\server\share\GameUserSettings.ini"}, {"file": r"\\?\C:\x\GameUserSettings.ini"},
    {"file": "GameUserSettings.ini"}, {"file": r"C:\x\settings.txt"}, {"value": "1;2"}, {"value": "1\r\n[X]"},
    {"key": "sg X"}, {"section": "A]B"}, {"game": r"C:\Games\Game.exe"}, {"game": "Game"}, {"game": "-x.exe"},
    {"file": r"C:\x\a:b.ini"}, {"key": ".Paths"},
])
def test_params_the_fix_did_not_make_are_refused(ini, change):
    params = {**_params(ini), **change}
    with pytest.raises(ValueError):
        GameSettingFix(running=Running()).target(params)


def test_an_entry_a_crash_left_pending_is_settled_from_the_file(ini, journal):
    fix = GameSettingFix(running=Running())
    params = _params(ini)
    left = journal.begin("game_setting", fix.target(params), params, "3", "1")
    assert fix.heal(left, journal).state == "failed"                      # the change never happened
    done = journal.begin("game_setting", fix.target(params), params, "3", "1")
    ini.write_bytes(RAW.replace(b"sg.StaticMeshQuality=3", b"sg.StaticMeshQuality=1"))
    assert fix.heal(done, journal).state == "applied"                     # it did: undo can act on it


def test_a_move_refused_for_a_moment_is_tried_again(ini, monkeypatch):
    calls = []
    real = os.replace

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(13, "in use")
        real(src, dst)
    monkeypatch.setattr(game_setting, "_move", flaky)
    game_setting.replace_bytes(str(ini), b"[S]\r\n", sleep=lambda s: None)
    assert ini.read_bytes() == b"[S]\r\n" and len(calls) == 3
    assert not list(ini.parent.glob("*" + game_setting.TEMP_SUFFIX))


def test_a_move_that_keeps_failing_leaves_the_file_and_no_temp_file(ini, monkeypatch):
    def refused(src, dst):
        raise PermissionError(13, "in use")
    monkeypatch.setattr(game_setting, "_move", refused)
    with pytest.raises(PermissionError):
        game_setting.replace_bytes(str(ini), b"[S]\r\n", sleep=lambda s: None)
    assert ini.read_bytes() == RAW and not list(ini.parent.glob("*" + game_setting.TEMP_SUFFIX))


def test_the_process_check_matches_the_image_name_without_a_handle():
    procs = {1: SimpleNamespace(pid=1, name="FF7Rebirth_.exe"), 2: SimpleNamespace(pid=2, name="ff7rebirth.exe"),
             3: SimpleNamespace(pid=3, name="")}
    assert game_setting.processes_named("ff7rebirth_.exe", procs) == [1]
    assert game_setting.processes_named("python.exe") != []             # this very test runs under one


def test_lagdoctor_undo_puts_a_sweeps_edit_back(ini, capsys):
    # The journal under the test's own home (conftest), so `lagdoctor undo` finds it as it would after a crash.
    fix = GameSettingFix(running=Running())
    assert fix.apply(fix.plan(_params(ini)), Journal()).ok
    assert cli.main(["undo", "--all"]) == 0
    assert ini.read_bytes() == RAW
    assert "Restored" in capsys.readouterr().out


def test_a_file_with_other_names_is_refused(ini, tmp_path):
    os.link(ini, tmp_path / "another-name.ini")
    with pytest.raises(ValueError, match="hard links"):
        GameSettingFix(running=Running()).read(_params(ini))


def test_a_link_to_another_file_is_refused(ini, tmp_path):
    link = tmp_path / "link.ini"
    try:
        os.symlink(ini, link)
    except OSError as exc:                                 # needs Developer Mode or admin rights on Windows
        pytest.skip(f"this account cannot make a symbolic link ({exc})")
    with pytest.raises(ValueError, match="link to another file"):
        GameSettingFix(running=Running()).read(_params(link))


def test_a_file_on_a_network_drive_is_refused(ini, monkeypatch):
    monkeypatch.setattr(game_setting, "_drive_type", lambda file: 4)       # DRIVE_REMOTE
    with pytest.raises(ValueError, match="not on one of this PC's own disks"):
        GameSettingFix(running=Running()).read(_params(ini))


def test_the_move_is_tried_again_until_its_deadline_not_a_count(ini, monkeypatch):
    now = {"t": 0.0}
    tries = []

    def refused(src, dst):
        tries.append(now["t"])
        raise PermissionError(13, "in use")

    def sleep(seconds):
        now["t"] += seconds
    monkeypatch.setattr(game_setting, "_move", refused)
    with pytest.raises(PermissionError):
        game_setting.replace_bytes(str(ini), b"[S]\r\n", sleep=sleep, clock=lambda: now["t"])
    assert tries[-1] >= game_setting.REPLACE_DEADLINE_S and len(tries) > 5
    assert ini.read_bytes() == RAW


def test_the_games_own_spelling_of_lag_doctors_value_is_still_lag_doctors(ini, journal):
    fix = GameSettingFix(running=Running())
    entry_id = fix.apply(fix.plan(_params(ini, value="1")), journal).entry_id
    ini.write_bytes(ini.read_bytes().replace(b"sg.StaticMeshQuality=1", b"sg.StaticMeshQuality=1.000000"))
    assert fix.undo(journal.get(entry_id), journal).ok and ini.read_bytes() == RAW


def test_the_original_in_the_games_own_spelling_counts_as_put_back(ini, journal):
    fix = GameSettingFix(running=Running())
    entry_id = fix.apply(fix.plan(_params(ini, value="1")), journal).entry_id
    respelt = RAW.replace(b"sg.StaticMeshQuality=3", b"sg.StaticMeshQuality=3.000000")
    ini.write_bytes(respelt)
    res = fix.undo(journal.get(entry_id), journal)
    assert res.ok and journal.get(entry_id).state == "undone" and ini.read_bytes() == respelt   # nothing written


def test_a_line_the_game_dropped_comes_back_only_by_a_forced_undo(ini, journal):
    fix = GameSettingFix(running=Running())
    entry_id = fix.apply(fix.plan(_params(ini, value="1")), journal).entry_id
    ini.write_bytes(ini.read_bytes().replace(b"sg.StaticMeshQuality=1\r\n", b""))
    res = fix.undo(journal.get(entry_id), journal)
    assert not res.ok and "could not be read" in res.message and b"StaticMeshQuality" not in ini.read_bytes()
    assert fix.undo(journal.get(entry_id), journal, force=True).ok and ini.read_bytes() == RAW
    with pytest.raises(LookupError):                       # the plan of a missing key still never adds one
        fix.plan(_params(ini, key="sg.Missing"))


def test_lagdoctor_undo_by_the_fix_id_touches_only_the_sweeps_edits(ini, capsys):
    fix = GameSettingFix(running=Running())
    journal = Journal()
    assert fix.apply(fix.plan(_params(ini)), journal).ok
    other = journal.begin("power_plan", "the power plan", {"scheme": "x"}, "a", "b")
    journal.set_state(other.id, "applied")
    assert cli.main(["undo", "game_setting"]) == 0
    assert ini.read_bytes() == RAW and journal.get(other.id).state == "applied"


def test_no_report_ever_offers_it():
    # In-game settings stay advice (DESIGN, Fixes): only the sweep writes them. A rule or report that named the fix
    # would put it among `lagdoctor fix`'s offers.
    assert "game_setting" in catalog.ids()
    for folder in ("analysis", "report"):
        for path in sorted((ROOT / "lagdoctor" / folder).rglob("*.py")):
            names = {n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            assert "game_setting" not in names, path
