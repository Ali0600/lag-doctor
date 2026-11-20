"""The Library's changes after screen 6: each game's program icon (an Epic game's, a recorded game's: views.icon_programs
and App.read_icons), renaming a game added by hand, and deleting recordings to the Recycle Bin (App.do_delete_recordings
and delete). Every program is made up (tests/pe_build.py), every recording a folder under the test's own home, and the
Recycle Bin a stand-in: no test reaches the real one (tests/conftest.py)."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from lagdoctor.app import added, library, recording, views
from lagdoctor.capture import target
from lagdoctor.stores import Game
from lagdoctor.stores import exe as exe_mod
from lagdoctor.stores import launch as store_launch
from lagdoctor.report import summary as summary_mod
from tests.test_app_added import Window, ask, make_app, program


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "launch_elevated", lambda *a, **k: pytest.fail("a test reached the UAC prompt"))


def epic_game(folder: str, exe: str | None) -> Game:
    return Game("epic", "demo:0a1b2c3d4e:LanternDrift", "Lantern Drift", folder,
                store_launch.epic_link("demo", "0a1b2c3d4e", "LanternDrift"), exe)


def recording_of(home: Path, sid: str, exe: str | None = None, game: str = "Moss Garden") -> Path:
    """A recording's folder as `watch` leaves one, under the home's sessions: its capture, and with `exe` its summary,
    which names its program and its game (a game's recordings share their program's file name)."""
    folder = home / "sessions" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / library.CAPTURE_NAME).write_bytes(b"")
    if exe is not None:
        (folder / summary_mod.SUMMARY_NAME).write_text(json.dumps({
            "schema": summary_mod.SCHEMA, "kind": "watch", "exe": exe, "exe_name": exe.replace("/", "\\").rsplit(
                "\\", 1)[-1], "game": game}), encoding="utf-8")
    return folder


def reads(monkeypatch) -> list[str]:
    """The programs exe.read is asked for, each still read."""
    seen, real = [], exe_mod.read
    monkeypatch.setattr(exe_mod, "read", lambda p: seen.append(p) or real(p))
    return seen


def tile_of(app, name: str) -> dict:
    return next(g for g in ask(app, "library")["games"] if g["name"] == name)


# ---------------------------------------------------------------- icons ---

def test_an_epic_games_tile_shows_the_icon_of_the_program_its_launcher_names(tmp_path, monkeypatch):
    exe = program(tmp_path, folder="Lantern Drift", name="Lantern.exe")
    game = epic_game(str(Path(exe).parent), exe)
    app = make_app(tmp_path, games=[game])
    gid = library.installed([game], lambda f: (f,))[0].game_id
    seen = reads(monkeypatch)
    assert ask(app, "game", game=gid)["tile"]["icon"] is True and seen == [exe]     # its page, before any listing
    kind, data = app.art_for(gid)
    assert kind == exe_mod.ICO and data.startswith(b"\0\0\1\0")
    t = tile_of(app, "Lantern Drift")
    assert t["tile"]["art"].endswith(gid) and t["tile"]["icon"] is True and seen == [exe]     # once a run


def test_the_program_chosen_in_add_a_game_is_not_read_again_at_the_listing(tmp_path, monkeypatch):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    seen = reads(monkeypatch)
    ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")
    assert tile_of(app, "Moss Garden")["tile"]["icon"] is True and seen == [exe]


def test_a_store_cover_is_kept_and_no_program_is_read_for_it(tmp_path, monkeypatch):
    exe = program(tmp_path, folder="Lantern Drift", name="Lantern.exe")
    app = make_app(tmp_path, games=[epic_game(str(Path(exe).parent), exe)])
    cover = ("image/png", b"\x89PNG made up")
    app.covers = lambda games: {(g.store, g.id): cover for g in games}
    seen = reads(monkeypatch)
    t = tile_of(app, "Lantern Drift")
    assert t["tile"]["art"] and t["tile"]["icon"] is False and app.art_for(t["id"]) == cover and seen == []


def test_a_recorded_game_no_store_lists_shows_its_newest_recordings_program_icon(tmp_path, monkeypatch):
    old = program(tmp_path, folder="Moss Garden 1.0", name="moss_garden.exe", icons=[])    # no icon: never reached
    new = program(tmp_path, folder="Moss Garden", name="moss_garden.exe")
    app = make_app(tmp_path)
    recording_of(app.home, "20261110-100000-moss_garden", old)
    recording_of(app.home, "20261111-100000-moss_garden", new)
    seen = reads(monkeypatch)
    t = tile_of(app, "Moss Garden")
    assert t["recordings"] == 2 and t["tile"]["icon"] is True and seen == [new]


def test_the_newest_recordings_program_stands_in_when_the_launchers_has_no_icon(tmp_path, monkeypatch):
    stub = program(tmp_path, folder="Lantern Drift", name="Lantern.exe", icons=[])
    real = program(tmp_path, folder="Lantern Drift", name="Lantern-Win64-Shipping.exe")
    app = make_app(tmp_path, games=[epic_game(str(Path(stub).parent), stub)])
    recording_of(app.home, "20261110-100000-lantern-win64-shipping", real, game="Lantern Drift")
    seen = reads(monkeypatch)
    t = tile_of(app, "Lantern Drift")
    assert t["recordings"] == 1 and t["tile"]["icon"] is True and seen == [stub, real]
    ask(app, "library")
    assert seen == [stub, real]                                         # each program looked at once a run


def test_a_program_found_without_an_icon_is_not_read_again_but_a_new_one_is(tmp_path, monkeypatch):
    plain = program(tmp_path, folder="Moss Garden 1.0", name="moss.exe", icons=[])
    app = make_app(tmp_path)
    recording_of(app.home, "20261110-100000-moss", plain)
    seen = reads(monkeypatch)
    assert tile_of(app, "Moss Garden")["tile"]["icon"] is False
    assert tile_of(app, "Moss Garden")["tile"]["icon"] is False and seen == [plain]
    shiny = program(tmp_path, folder="Moss Garden", name="moss.exe")
    recording_of(app.home, "20261111-100000-moss", shiny)             # a newer recording, of another program
    assert tile_of(app, "Moss Garden")["tile"]["icon"] is True and seen == [plain, shiny]


@pytest.mark.parametrize("exe", ["D:\\Games\\Moss Garden\\moss.txt", "moss.exe", "C:\\Windows\\moss.exe",
                                 "D:\\Games\\Moss Garden\\" + chr(0x202E) + "exe.moss.exe"])
def test_a_recordings_program_that_is_no_game_program_is_never_read(tmp_path, monkeypatch, exe):
    app = make_app(tmp_path)
    recording_of(app.home, "20261110-100000-moss", exe)
    seen = reads(monkeypatch)
    ask(app, "library")
    assert seen == []


def test_the_programs_a_tile_may_take_its_icon_from_are_its_own_then_its_newest_recordings(tmp_path):
    def entry(sid: str, exe: str) -> library.Entry:
        folder = tmp_path / sid
        folder.mkdir()
        (folder / library.CAPTURE_NAME).write_bytes(b"")
        return library.Entry(id=sid, folder=folder, kind="watch", game="a", game_key="a", exe_name="a.exe",
                             started=None, summary={"exe": exe})
    store = library.installed([epic_game("D:\\G\\L", "D:\\G\\L\\l.exe")], lambda f: (f,))
    mine = library.added_games([added.Kept(exe="D:\\G\\M\\m.exe", name="M")], lambda f: (f,))
    entries = [entry("20261112-100000-l", "D:\\G\\L\\Bin\\l.exe"),     # Lantern's newest: its own program first
               entry("20261111-100000-m", "d:\\g\\m\\M.EXE"),             # the added game's own, named again: once
               entry("20261110-100000-a", "D:\\G\\A\\a.exe"), entry("20261109-100000-a", "D:\\G\\A\\old.exe")]
    assert views.icon_programs(entries, store, mine) == {
        store[0].game_id: ["D:\\G\\L\\l.exe", "D:\\G\\L\\Bin\\l.exe"], mine[0].game_id: ["D:\\G\\M\\m.exe"],
        entries[2].game_id: ["D:\\G\\A\\a.exe"]}
    steam = library.installed([Game("steam", "3999901", "Demo Racer", "D:\\G\\R", store_launch.steam_link("3999901"))],
                              lambda f: (f,))
    assert views.icon_programs([], steam) == {}                         # no program of its own, none recorded


# ---------------------------------------------------------------- rename ---

def test_renaming_an_added_game_keeps_its_program_its_id_and_its_recordings(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]
    recording_of(app.home, "20261110-100000-moss_garden", exe)
    assert ask(app, "rename_game", game=gid, name="  Moss Garden: Deluxe  ") == {"game": gid,
                                                                              "name": "Moss Garden: Deluxe"}
    assert added.load(app.home) == ([added.Kept(exe=exe, name="Moss Garden: Deluxe")], [])
    page = ask(app, "game", game=gid)
    assert page["name"] == "Moss Garden: Deluxe" and page["added"] and len(page["sessions"]) == 1
    assert [g["name"] for g in ask(app, "library")["games"]] == ["Moss Garden: Deluxe"]


def test_a_rename_with_nothing_to_show_or_of_a_game_not_added_is_refused_and_changes_nothing(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]
    before = (app.home / added.NAME).read_bytes()
    assert ask(app, "rename_game", game=gid, name=" \u200b ") == "Give the game a name to show by."
    assert ask(app, "rename_game", game="g-0000000000000000", name="x") == (
        "That game was not added by hand, so it keeps the name its store or its recordings give it.")
    assert (app.home / added.NAME).read_bytes() == before


def test_renaming_over_a_list_that_held_an_unread_entry_keeps_its_bytes_aside(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path)
    original = json.dumps({"version": 1, "games": [{"exe": exe, "name": "Moss Garden"},
                                                  {"exe": "D:\\moss.exe", "name": "On a drive"}]}).encode("utf-8")
    (app.home / added.NAME).write_bytes(original)
    gid = library.added_id(exe)
    assert ask(app, "rename_game", game=gid, name="Moss")["name"] == "Moss"
    [aside] = app.home.glob("added-games.unread-*.json")
    assert aside.read_bytes() == original and added.load(app.home) == ([added.Kept(exe=exe, name="Moss")], [])


def test_a_rename_that_cannot_be_saved_is_said_and_keeps_the_old_name(tmp_path, monkeypatch):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]

    def fail(*a, **k):
        raise OSError(13, "Access is denied")
    monkeypatch.setattr(added, "save", fail)
    assert ask(app, "rename_game", game=gid, name="Moss") == ("lag-doctor could not save its list of games (Access "
                                                              "is denied).")
    assert added.load(app.home) == ([added.Kept(exe=exe, name="Moss Garden")], [])


# ---------------------------------------------------------------- delete ---

class Bin:
    """The Recycle Bin's stand-in (App's `trash`): each folder handed over is moved into a folder of the test's own."""

    def __init__(self, root: Path, fail: dict | None = None) -> None:
        self.root, self.fail, self.moved = root / "bin", fail or {}, []
        self.root.mkdir(parents=True, exist_ok=True)

    def __call__(self, path: str, hwnd):
        self.moved.append((path, hwnd))
        why = self.fail.get(Path(path).name)
        if why == "stays":
            return
        if why is not None:
            raise why
        shutil.move(path, self.root / Path(path).name)


def deleting(tmp_path, *, games=(), fail=None):
    app = make_app(tmp_path, games=games)
    app.trash = Bin(tmp_path, fail)
    folders = [recording_of(app.home, sid) for sid in ("20261110-100000-moss", "20261111-100000-moss",
                                                       "20261112-100000-star")]
    moss = tile_of(app, "moss")["id"]
    return app, folders, moss


def test_deleting_picked_recordings_moves_each_folder_by_its_full_path_and_the_game_stays(tmp_path):
    app, (first, second, star), moss = deleting(tmp_path)
    got = ask(app, "delete_recordings", game=moss, sessions=[first.name])
    assert got == {"text": views.DELETED_ONE, "deleted": 1, "listed": True}
    assert app.trash.moved == [(str(first), Window.hwnd)] and not first.exists() and second.exists()
    assert (app.trash.root / first.name / library.CAPTURE_NAME).is_file()
    assert [s["id"] for s in ask(app, "game", game=moss)["sessions"]] == [second.name]


def test_deleting_a_recorded_games_last_recordings_takes_it_out_of_the_library(tmp_path):
    app, (first, second, star), moss = deleting(tmp_path)
    got = ask(app, "delete_recordings", game=moss, sessions=[second.name, first.name])
    assert got == {"text": views.DELETED.format(n=2), "deleted": 2, "listed": False}
    assert [p for p, _ in app.trash.moved] == [str(second), str(first)]
    assert [g["name"] for g in ask(app, "library")["games"]] == ["star"]


def test_an_installed_game_stays_in_the_library_without_its_recordings(tmp_path):
    folder = tmp_path / "games" / "Lantern Drift"
    folder.mkdir(parents=True)
    app = make_app(tmp_path, games=[epic_game(str(folder), None)])
    app.trash = Bin(tmp_path)
    rec = recording_of(app.home, "20261110-100000-lantern", str(folder / "lantern.exe"))
    gid = tile_of(app, "Lantern Drift")["id"]
    assert ask(app, "delete_recordings", game=gid, sessions=[rec.name])["listed"] is True
    assert tile_of(app, "Lantern Drift")["recordings"] == 0


@pytest.mark.parametrize("which", ["another game's", "not there"])
def test_a_recording_not_listed_under_the_game_named_is_never_deleted(tmp_path, which):
    app, (first, second, star), moss = deleting(tmp_path)
    other = star.name if which == "another game's" else "20261113-100000-moss"
    got = ask(app, "delete_recordings", game=moss, sessions=[first.name, other])
    assert got == "Some of those recordings are no longer listed under this game: open its page again."
    assert app.trash.moved == [] and first.exists() and star.exists()


def test_nothing_is_deleted_while_another_process_records(tmp_path):
    import uuid
    from lagdoctor.winapi import elevation
    app, (first, second, star), moss = deleting(tmp_path)
    app.capture_lock = f"Local\\lag-doctor-test-capture-{uuid.uuid4().hex}"     # this test's own, never the owner's
    held = elevation.hold_named_mutex(app.capture_lock)       # a terminal's watch, analysing
    try:
        assert ask(app, "delete_recordings", game=moss, sessions=[first.name]) == (
            "Another lag-doctor recording is running, in a terminal or from an earlier window: delete recordings once "
            "it has ended.")
        assert app.trash.moved == [] and first.exists()
    finally:
        elevation.release_named_mutex(held)
    # once it has ended: each delete looks again and lets the lock go, so the next one is free as well
    assert ask(app, "delete_recordings", game=moss, sessions=[first.name])["deleted"] == 1
    assert ask(app, "delete_recordings", game=moss, sessions=[second.name])["deleted"] == 1


def test_nothing_is_deleted_when_windows_cannot_say_whether_one_records(tmp_path, monkeypatch):
    from lagdoctor.winapi import elevation
    app, (first, second, star), moss = deleting(tmp_path)
    app.capture_lock = "Local\\lag-doctor-test-capture-unread"

    def fail(name):
        raise OSError(5, "Access is denied")
    monkeypatch.setattr(elevation, "hold_named_mutex", fail)
    assert ask(app, "delete_recordings", game=moss, sessions=[first.name]) == (
        "lag-doctor could not tell whether a recording is running, so it deleted nothing.")
    assert app.trash.moved == [] and first.exists()


def test_nothing_is_deleted_while_a_recording_runs(tmp_path):
    app, (first, second, star), moss = deleting(tmp_path)
    app.rec_state = recording.RecordingState()                      # starting: active
    assert ask(app, "delete_recordings", game=moss, sessions=[first.name]) == (
        "A recording is running: delete recordings once it has ended.")
    assert app.trash.moved == [] and first.exists()


def test_a_move_windows_refused_or_that_left_the_folder_is_said_and_the_others_still_go(tmp_path):
    fail = {"20261110-100000-moss": OSError(0, "Windows could not move it to the Recycle Bin (code 0x78)"),
            "20261111-100000-moss": "stays"}
    app, (first, second, star), moss = deleting(tmp_path, fail=fail)
    got = ask(app, "delete_recordings", game=moss, sessions=[first.name, second.name])
    assert got == {"text": " ".join([
        views.NOT_DELETED.format(when="10 Nov 2026, 10:00", why="Windows could not move it to the Recycle Bin (code "
                                                                "0x78)"),
        views.NOT_DELETED.format(when="11 Nov 2026, 10:00", why="its folder is still there")]),
        "deleted": 0, "listed": True}
    assert first.exists() and second.exists()
    app.trash.fail = {"20261110-100000-moss": ValueError("it is not a folder's path")}
    got = ask(app, "delete_recordings", game=moss, sessions=[first.name, second.name])
    assert got["text"] == " ".join([views.DELETED_ONE, views.NOT_DELETED.format(when="10 Nov 2026, 10:00",
                                                                               why="it is not a folder's path")])
    assert first.exists() and not second.exists()


def test_a_move_the_player_cancelled_ends_the_delete(tmp_path):
    from lagdoctor.winapi import recycle
    app, (first, second, star), moss = deleting(
        tmp_path, fail={"20261111-100000-moss": recycle.Cancelled(0, "moving it to the Recycle Bin was cancelled")})
    got = ask(app, "delete_recordings", game=moss, sessions=[second.name, first.name])
    assert got["text"] == " ".join([
        views.NOT_DELETED.format(when="11 Nov 2026, 10:00", why="it was cancelled"),
        views.NOT_DELETED.format(when="10 Nov 2026, 10:00", why="it was not tried, since the one before it was "
                                                                "cancelled")])
    assert [p for p, _ in app.trash.moved] == [str(second)] and first.exists() and second.exists()


def test_a_listing_that_fails_after_the_delete_still_says_what_was_deleted(tmp_path, monkeypatch):
    app, (first, second, star), moss = deleting(tmp_path)

    def broken(*a, **k):
        raise RuntimeError("made up")
    monkeypatch.setattr(views, "game_view", broken)
    assert ask(app, "delete_recordings", game=moss, sessions=[first.name]) == {
        "text": views.DELETED_ONE, "deleted": 1, "listed": True}
    assert not first.exists()


def test_an_error_moving_one_recording_is_said_and_the_page_still_answered(tmp_path):
    app, (first, second, star), moss = deleting(tmp_path, fail={"20261110-100000-moss": RuntimeError("made up")})
    got = ask(app, "delete_recordings", game=moss, sessions=[first.name, second.name])
    assert got["text"] == " ".join([views.DELETED_ONE, views.NOT_DELETED.format(
        when="10 Nov 2026, 10:00", why="lag-doctor hit an error moving it; the details are in its log")])
    assert first.exists() and not second.exists()


def test_a_folder_off_the_local_disks_is_never_handed_over(tmp_path, monkeypatch):
    app, (first, second, star), moss = deleting(tmp_path)
    monkeypatch.setattr(target, "local_problem", lambda p: f"{p} is not on a local fixed or removable disk")
    got = ask(app, "delete_recordings", game=moss, sessions=[first.name])
    assert got["deleted"] == 0 and "lag-doctor deletes only recordings on this PC's own disks" in got["text"]
    assert app.trash.moved == [] and first.exists()


def test_a_home_named_by_a_relative_path_hands_over_the_full_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from lagdoctor.app import main as app_main
    app = app_main.App(home=Path("home"), runtime={"version": "1.0.0.0", "build": 4000}, create_fn=None,
                       log=lambda s: None, play=lambda n: None, flash=lambda on: None, installed=lambda: [],
                       forms_of=lambda folder: (folder,), trash=Bin(tmp_path))
    app.window = Window()
    app.get_readiness = lambda: {"ok": True, "text": "ready", "detail": ""}
    rec = recording_of(Path("home"), "20261110-100000-moss")
    ask(app, "delete_recordings", game=tile_of(app, "moss")["id"], sessions=[rec.name])
    assert app.trash.moved == [(str(tmp_path / "home" / "sessions" / rec.name), Window.hwnd)]


def test_an_app_given_no_recycle_bin_deletes_nothing(tmp_path):
    app, (first, second, star), moss = deleting(tmp_path)
    bin_, app.trash = app.trash, None
    assert ask(app, "delete_recordings", game=moss, sessions=[first.name]) == (
        "Deleting recordings is not available in this window.")
    assert bin_.moved == [] and first.exists()


def test_a_deleted_recording_is_never_served_from_the_one_read_last(tmp_path):
    app, (first, second, star), moss = deleting(tmp_path)
    app.loaded = (first.name, None, None, None)                       # as load() keeps the last recording read
    ask(app, "delete_recordings", game=moss, sessions=[first.name])
    assert app.loaded is None
    assert ask(app, "results", session=first.name) == "That recording is no longer there."


def test_each_deletion_is_logged_by_its_folders_name(tmp_path):
    log = []
    app = make_app(tmp_path, log=log)
    app.trash = Bin(tmp_path)
    rec = recording_of(app.home, "20261110-100000-moss")
    ask(app, "delete_recordings", game=tile_of(app, "moss")["id"], sessions=[rec.name])
    assert f"moved a recording to the Recycle Bin: {rec.name}" in log


def test_the_words_a_deletion_says():
    when = library.Entry(id="20261110-100000-moss", folder=Path("x"), kind="watch", game="moss", game_key="moss",
                         exe_name=None, started=datetime(2026, 11, 10, 10), summary=None)
    undated = library.Entry(id="20261111-100000-moss", folder=Path("y"), kind="watch", game="moss", game_key="moss",
                            exe_name=None, started=None, summary=None)
    # Windows may have asked to delete one for good, and the player said yes: the words promise no Recycle Bin then.
    assert views.deleted_view(1, [], True) == {"text": "The recording was deleted. Unless Windows asked to delete it "
                                                       "for good, it is in the Recycle Bin, where Windows can put it "
                                                       "back.", "deleted": 1, "listed": True}
    assert views.deleted_view(3, [(when, "it is still there"), (undated, "it was cancelled")], False)["text"] == (
        "3 recordings were deleted. Unless Windows asked to delete them for good, they are in the Recycle Bin, where "
        "Windows can put them back. The recording of 10 Nov 2026, 10:00 was not deleted: it is still there. The "
        "recording of 20261111-100000-moss was not deleted: it was cancelled.")


def test_the_pages_cap_on_a_delete_is_the_bridges():
    from lagdoctor.app import ASSETS, bridge
    import re
    [cap] = re.findall(r"^const MAX_DELETE = (\d+);$", (ASSETS / "app.js").read_text(encoding="utf-8"), re.M)
    assert int(cap) == bridge.MAX_DELETE          # a larger one would send what the bridge drops unanswered


def test_a_session_id_ending_in_a_dot_is_none_windows_keeps_apart():
    from lagdoctor.app import bridge
    # Windows drops a last dot: ...-moss. would name ...-moss's folder
    assert bridge.SESSION_ID.fullmatch("20261110-100000-moss") and bridge.SESSION_ID.fullmatch("20261110-100000-m.s")
    assert not bridge.SESSION_ID.fullmatch("20261110-100000-moss.")
    assert bridge.SESSION_ID.fullmatch("20261110-100000-" + "a" * 64)
    assert not bridge.SESSION_ID.fullmatch("20261110-100000-" + "a" * 65)
