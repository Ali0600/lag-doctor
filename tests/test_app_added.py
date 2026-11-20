"""Add a game (screen 6): the games added by hand (lagdoctor/app/added.py), how the Library lists them and the
recorded games no store lists (library.listed_added, clash, wait_folders; the views), and the app's requests: the
Open dialog's answer, the program read (made-up programs from tests/pe_build.py), Add, Remove and Record when it
starts. No real dialog opens (tests/conftest.py) and no game's file is read."""
from __future__ import annotations

import json
import struct
import types
from datetime import datetime
from pathlib import Path

import pytest

from lagdoctor.app import added, library, recording, views
from lagdoctor.app import main as app_main
from lagdoctor.capture import target
from tests import app_demo, fake_recorder, pe_build

READY = {"ok": True, "text": "Frame timing ready", "detail": ""}
MOSS = {"ProductName": "Moss Garden"}


@pytest.fixture(autouse=True)
def _no_real_uac(monkeypatch):
    from lagdoctor.winapi import elevation
    monkeypatch.setattr(elevation, "launch_elevated", lambda *a, **k: pytest.fail("a test reached the UAC prompt"))


def _rec(home: Path, sid: str, exe: str | None, when: datetime) -> library.Entry:
    folder = home / "sessions" / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / library.CAPTURE_NAME).write_bytes(b"")
    stem = sid.split("-", 2)[2]
    return library.Entry(id=sid, folder=folder, kind="watch", game=stem, game_key=stem, exe_name=f"{stem}.exe",
                         started=when, summary=None if exe is None else {"exe": exe, "kind": "watch"})


def _added(*pairs: tuple[str, str]) -> list[library.AddedGame]:
    return library.added_games([added.Kept(exe=e, name=n) for e, n in pairs], lambda f: (f,))


def _store(gid: str, title: str, folder: str):
    import dataclasses
    return library.installed([dataclasses.replace(app_demo.INSTALLED[0], id=gid, title=title, folder=folder)],
                             lambda f: (f,))[0]


# ---------------------------------------------------------------- added-games.json ---

@pytest.mark.parametrize("path, kept", [
    ("D:\\Games\\Moss Garden\\Moss Garden.exe", ("D:\\Games\\Moss Garden\\Moss Garden.exe", "D:\\Games\\Moss Garden")),
    ("d:/games//moss/run.EXE", ("D:\\games\\moss\\run.EXE", "D:\\games\\moss")),
])
def test_a_program_is_kept_by_its_path_in_windows_own_form(path, kept):
    assert added.check_program(path) == kept


@pytest.mark.parametrize("path, why", [
    ("D:\\moss.exe", "is a whole drive"),
    ("C:\\Windows\\System32\\notepad.exe", "in the Windows folder"),
    ("D:\\Games\\Moss\\readme.txt", "is not a program"),
    ("D:\\Games\\Moss\\.exe", "is not a program"),
    ("D:\\Games\\Moss\\run.exe.", "is not a program"),
    ("D:\\Games\\..\\Moss\\run.exe", "may name another folder"),
    ("\\\\server\\share\\Moss\\run.exe", "not a folder on a local drive"),
    ("run.exe", "is not a program"),
    ("D:\\Games\\Mo\nss\\run.exe", "not the path of a program"),
    ("D:\\Games\\Moss\\r<n.exe", "is not a program"),
    ("D:\\Games\\" + "x" * 600 + ".exe", "not the path of a program"),
    ("D:\\Games\\Mo" + chr(0xD800) + "ss\\run.exe", "not the path of a program"),       # a lone surrogate
    ("D:\\Games\\Mo" + chr(0x2028) + "ss\\run.exe", "not the path of a program"),       # a line separator
    ("D:\\Games\\Mo" + chr(0x202E) + "ss\\run.exe", "not the path of a program"),       # a mark that reorders
    (None, "not the path of a program"),
])
def test_a_path_that_is_no_game_program_in_a_folder_of_its_own_is_refused(path, why, monkeypatch):
    monkeypatch.setattr(target, "_windows_folder", lambda: "C:\\Windows")
    with pytest.raises(ValueError, match=why):
        added.check_program(path)


def test_the_list_reads_back_as_written_in_the_order_added(tmp_path):
    games = [added.Kept("D:\\Games\\Moss\\moss.exe", "Moss Garden"), added.Kept("E:\\Kiln\\kiln.exe", "Kiln Runner")]
    added.save(tmp_path, games, unread=False)
    assert added.load(tmp_path) == (games, [])
    doc = json.loads((tmp_path / added.NAME).read_bytes())
    assert doc == {"version": 1, "games": [{"exe": "D:\\Games\\Moss\\moss.exe", "name": "Moss Garden"},
                                           {"exe": "E:\\Kiln\\kiln.exe", "name": "Kiln Runner"}]}
    assert not (tmp_path / (added.NAME + ".new")).exists()


def test_no_list_yet_is_none_and_no_problem(tmp_path):
    assert added.load(tmp_path) == ([], [])


def test_entries_that_are_not_a_program_and_a_name_are_left_out_and_named(tmp_path):
    doc = {"version": 1, "games": [
        {"exe": "D:\\Games\\Moss\\moss.exe", "name": "Moss Garden"},
        {"exe": "D:\\moss.exe", "name": "On a drive"},
        {"exe": "D:\\Games\\Kiln\\kiln.exe", "name": " \u200f "},
        {"exe": "D:\\Games\\Kiln\\kiln.exe", "name": "Kiln", "extra": 1},
        "D:\\Games\\Ember\\ember.exe",
        {"exe": "d:/games/moss/MOSS.EXE", "name": "Moss again"},
        {"exe": "D:\\Games\\Ember\\ember.exe", "name": "Ember\nQuest"},
    ]}
    (tmp_path / added.NAME).write_bytes(json.dumps(doc).encode("utf-8"))
    games, problems = added.load(tmp_path)
    assert games == [added.Kept("D:\\Games\\Moss\\moss.exe", "Moss Garden"),
                     added.Kept("D:\\Games\\Ember\\ember.exe", "Ember Quest")]
    assert len(problems) == 5 and "is listed twice" in problems[-1]


def test_problems_are_plain_lines_named_up_to_twenty_and_counted_past(tmp_path):
    games = [{"exe": "D:\\Games\\Mo" + chr(0xD800) + "ss\\run.exe", "name": "x"}] * 2
    games += [{"exe": f"D:\\G{i}.exe", "name": "on a drive"} for i in range(25)]
    (tmp_path / added.NAME).write_bytes(json.dumps({"version": 1, "games": games}).encode("utf-8"))
    kept, problems = added.load(tmp_path)
    assert kept == [] and len(problems) == 21 and problems[-1].startswith("7 more problems")
    assert all(c.isprintable() for p in problems for c in p)                 # each one line, writable to the log
    (tmp_path / "log.txt").write_text("\n".join(problems), encoding="utf-8")


@pytest.mark.parametrize("data", [b"{not json", b'{"version": 2, "games": []}', b"[]", b'{"version": 1}',
                                  "\u00e9".encode("latin-1")])
def test_a_list_lag_doctor_did_not_write_is_named_and_none_of_it_taken(tmp_path, data):
    (tmp_path / added.NAME).write_bytes(data)
    games, problems = added.load(tmp_path)
    assert games == [] and problems == [f"{tmp_path / added.NAME} is not a list of games lag-doctor wrote"]


def test_a_list_past_its_size_is_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(added, "MAX_BYTES", 100)
    (tmp_path / added.NAME).write_bytes(b" " * 101)
    games, problems = added.load(tmp_path)
    assert games == [] and "larger than 100 bytes" in problems[0]


def test_a_list_off_the_local_disks_is_named_never_followed_and_never_written(tmp_path, monkeypatch):
    path = tmp_path / added.NAME
    path.write_bytes(b'{"version": 1, "games": []}')
    monkeypatch.setattr(target, "local_problem", lambda p: f"{p} leads onto a network share")
    assert added.load(tmp_path) == ([], [f"{path} leads onto a network share"])
    with pytest.raises(OSError, match="leads onto a network share"):
        added.save(tmp_path, [added.Kept("D:\\Games\\Moss\\moss.exe", "Moss Garden")], unread=True)
    assert path.read_bytes() == b'{"version": 1, "games": []}' and list(tmp_path.glob("*unread*")) == []


def test_a_write_that_fails_leaves_the_old_list_where_it_was(tmp_path):
    games = [added.Kept("D:\\Games\\Moss\\moss.exe", "Moss Garden")]
    added.save(tmp_path, games, unread=False)
    (tmp_path / (added.NAME + ".new")).mkdir()               # the new list cannot be written
    with pytest.raises(OSError):
        added.save(tmp_path, [], unread=True)
    assert added.load(tmp_path) == (games, []) and list(tmp_path.glob("*unread*")) == []


def test_at_most_max_games_are_taken(tmp_path, monkeypatch):
    monkeypatch.setattr(added, "MAX_GAMES", 3)
    doc = {"version": 1, "games": [{"exe": f"D:\\G\\g{i}\\g.exe", "name": f"G{i}"} for i in range(5)]}
    (tmp_path / added.NAME).write_bytes(json.dumps(doc).encode("utf-8"))
    games, problems = added.load(tmp_path)
    assert [g.name for g in games] == ["G0", "G1", "G2"] and problems == [
        f"{tmp_path / added.NAME} lists more than 3 games; the rest were left out"]


def test_a_list_that_was_not_read_whole_is_moved_aside_before_a_new_one_replaces_it(tmp_path, monkeypatch):
    monkeypatch.setattr(added, "_stamp", lambda: "20261119-120000")      # both in the same second
    (tmp_path / added.NAME).write_bytes(b"{not json")
    _, problems = added.load(tmp_path)
    added.save(tmp_path, [added.Kept("D:\\Games\\Moss\\moss.exe", "Moss Garden")], unread=bool(problems))
    aside = sorted(tmp_path.glob("added-games.unread-*.json"))
    assert len(aside) == 1 and aside[0].read_bytes() == b"{not json"
    assert added.load(tmp_path)[0] == [added.Kept("D:\\Games\\Moss\\moss.exe", "Moss Garden")]
    (tmp_path / added.NAME).write_bytes(b"{again not json")
    added.save(tmp_path, [], unread=True)                    # a second one in the same second keeps the first
    assert sorted(p.read_bytes() for p in tmp_path.glob("added-games.unread-*.json")) == [b"{again not json",
                                                                                         b"{not json"]


# ---------------------------------------------------------------- the Library ---

def test_an_added_game_shows_by_its_name_with_its_recordings_and_waits_on_its_folder(tmp_path):
    rec = _rec(tmp_path, "20261110-100000-moss", "D:\\Games\\Moss\\bin\\moss.exe", datetime(2026, 11, 10, 10))
    other = _rec(tmp_path, "20261109-100000-kiln", "E:\\Kiln\\kiln.exe", datetime(2026, 11, 9, 10))
    listed = _added(("D:\\Games\\Moss\\moss.exe", "Moss Garden"), ("F:\\Ember\\ember.exe", "Ember Quest"))
    v = views.library_view([rec, other], READY, (), added=listed)
    assert [(g["name"], g["recordings"], g["store"], g["installed"], g["added"]) for g in v["games"]] == [
        ("Moss Garden", 1, library.ADDED_BY_YOU, True, True), ("kiln", 1, "", False, False),
        ("Ember Quest", 0, library.ADDED_BY_YOU, True, True)]
    assert v["games"][2]["exe_name"] == "ember.exe" and v["installed"] == 2
    moss = listed[0].game_id
    assert moss == library.added_id("d:/games/moss/MOSS.exe")             # one program, one game, however written
    g = views.game_view([rec, other], moss, None, (), play="PLAY WORDS", added=listed)
    assert g["name"] == "Moss Garden" and g["store"] == library.ADDED_BY_YOU and g["added"] is True
    assert g["play"] is None and g["play_note"] == "" and [s["id"] for s in g["sessions"]] == [rec.id]
    assert g["wait"] == {"folder": "D:\\Games\\Moss", "text": views.WAIT_TEXT.format(wait="5 minutes") + " PLAY WORDS"}
    assert views.wait_of([rec, other], moss, (), listed) == ("Moss Garden", "D:\\Games\\Moss")
    assert views.installed_of([rec, other], moss, (), listed) is None              # no Play for it


def test_an_added_game_a_store_lists_shows_as_the_stores_with_play(tmp_path):
    racer = _store("3999901", "Demo Racer", "C:\\Games\\Demo Racer")
    inside = _added(("C:\\Games\\Demo Racer\\Bin\\racer.exe", "My Racer"), ("C:\\Games\\Demo Racer\\racer.exe", "R"))
    assert library.listed_added(inside, [racer]) == []
    apart = _added(("C:\\Games\\Demo Racer 2\\racer.exe", "Racer Two"))
    assert library.listed_added(apart, [racer]) == apart                     # a name's prefix is no folder of it


def test_an_added_folder_holding_another_games_takes_no_recording(tmp_path):
    rec = _rec(tmp_path, "20261110-100000-racer", "C:\\Games\\Racer\\racer.exe", datetime(2026, 11, 10, 10))
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    broad = _added(("C:\\Games\\launcher.exe", "All my games"))
    joins = library.joined([rec], [racer], roots=[], added=broad)
    assert joins[rec.id].game.title == "Racer"
    alone = _rec(tmp_path, "20261109-100000-tool", "C:\\Games\\Tool\\tool.exe", datetime(2026, 11, 9, 10))
    assert alone.id not in library.joined([alone], [racer], roots=[], added=broad)
    assert library.joined([alone], [], roots=[], added=broad)[alone.id].name == "All my games"   # holds none: joins


def test_a_recorded_game_no_store_lists_waits_on_its_own_folder_when_it_has_one(tmp_path):
    moss = _rec(tmp_path, "20261110-100000-moss", "D:\\Games\\Moss\\moss.exe", datetime(2026, 11, 10, 10))
    old = _rec(tmp_path, "20261101-100000-kiln", None, datetime(2026, 11, 1, 10))           # no summary: no folder
    v = views.library_view([moss, old], READY)
    gids = {g["name"]: g["id"] for g in v["games"]}
    g = views.game_view([moss, old], gids["moss"], None, play="P")
    assert g["wait"]["folder"] == "D:\\Games\\Moss" and g["added"] is False and g["play_note"] == ""
    k = views.game_view([moss, old], gids["kiln"], None, play="P")
    assert k["wait"] is None and k["play_note"] == views.PLAY_NOTE
    assert views.wait_of([moss, old], gids["kiln"]) is None


def test_an_added_game_whose_folder_is_broad_offers_no_wait_and_says_why(tmp_path):
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    listed = _added(("C:\\Games\\launcher.exe", "All my games"), ("C:\\Program Files\\tool.exe", "Tool"))
    for a in listed:
        g = views.game_view([], a.game_id, None, [racer], added=listed)
        assert g["wait"] is None and g["play"] is None and g["added"] is True
        assert g["play_note"] == views.ADDED_BROAD_NOTE.format(folder=a.folder)
        assert views.wait_of([], a.game_id, [racer], listed) is None


def test_an_added_game_under_a_broad_store_folder_stays_listed_and_is_not_the_stores(tmp_path):
    everything = _store("3999913", "Everything", "C:\\Games")           # it holds Racer's folder: broad
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    moss = _added(("C:\\Games\\Moss\\moss.exe", "Moss Garden"))
    assert library.listed_added(moss, [everything, racer], roots=[]) == moss
    assert library.clash("C:\\Games\\Kiln\\kiln.exe", ("C:\\Games\\Kiln",), [everything, racer], [],
                         roots=[]) is None


def test_a_folder_holding_two_recorded_games_is_refused_and_one_is_its_own(tmp_path):
    moss = library.RecordedGame("g-0000000000000001", "moss", "D:\\Games\\Moss")
    kiln = library.RecordedGame("g-0000000000000002", "kiln", "D:\\Games\\Kiln")
    how, game = library.clash("D:\\Games\\launcher.exe", ("D:\\Games",), [], [], [moss, kiln])
    assert how == "holds" and game is moss
    assert views.clash_view(how, game) == {"existing": None, "why": views.CLASH_WORDS["holds"].format(name="moss",
                                                                                                    store="")}
    stub = library.clash("D:\\Games\\Moss\\moss.exe", ("D:\\Games\\Moss",), [], [],
                         [library.RecordedGame("g-0000000000000003", "moss_", "D:\\Games\\Moss\\Bin\\Win64"),
                          kiln])
    assert stub is None                                     # its own game's recordings, from under its folder


def test_the_recorded_games_add_a_game_weighs_are_those_no_store_or_added_game_holds(tmp_path):
    a = _rec(tmp_path, "20261110-100000-alpha", "D:\\Games\\Alpha\\alpha.exe", datetime(2026, 11, 10, 10))
    b = _rec(tmp_path, "20261109-100000-racer", "C:\\Games\\Racer\\racer.exe", datetime(2026, 11, 9, 10))
    c = _rec(tmp_path, "20261108-100000-old", None, datetime(2026, 11, 8, 10))
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    assert views.recorded_games([a, b, c], [racer]) == [
        library.RecordedGame(library.game_id("alpha"), "alpha", "D:\\Games\\Alpha")]


def test_a_recorded_games_folder_of_many_games_is_not_waited_on(tmp_path):
    a = _rec(tmp_path, "20261110-100000-alpha", "D:\\Games\\alpha.exe", datetime(2026, 11, 10, 10))
    b = _rec(tmp_path, "20261109-100000-beta", "D:\\Games\\beta.exe", datetime(2026, 11, 9, 10))    # same folder
    c = _rec(tmp_path, "20261108-100000-gamma", "D:\\Games\\Gamma\\gamma.exe", datetime(2026, 11, 8, 10))
    d = _rec(tmp_path, "20261107-100000-tool", "C:\\Program Files\\tool.exe", datetime(2026, 11, 7, 10))
    e = _rec(tmp_path, "20261106-100000-racer", "C:\\Games\\racer.exe", datetime(2026, 11, 6, 10))
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    folders = library.wait_folders({"a": a.summary["exe"], "b": b.summary["exe"], "c": c.summary["exe"],
                                    "d": d.summary["exe"], "e": e.summary["exe"]}, [racer],
                                   roots=["C:\\Program Files"])
    # a and b share D:\Games, which holds c's folder too; d sits in a shared root; e's folder holds Racer's
    assert folders == {"c": "D:\\Games\\Gamma"}


@pytest.mark.parametrize("exe, how, name", [
    ("D:\\Games\\Moss\\moss.exe", "same", "Moss Garden"),
    ("D:\\Games\\Moss\\launcher.exe", "added", "Moss Garden"),
    ("D:\\Games\\Moss\\Bin\\moss.exe", "added", "Moss Garden"),
    ("C:\\Games\\Racer\\Bin\\racer.exe", "store", "Racer"),
    ("C:\\Games\\launcher.exe", "holds", "Racer"),
    ("D:\\Games\\launcher.exe", "holds", "Moss Garden"),
])
def test_a_program_the_library_has_already_is_met_as_its_game(exe, how, name):
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    listed = _added(("D:\\Games\\Moss\\moss.exe", "Moss Garden"))
    folder = added.check_program(exe)[1]
    found = library.clash(exe, (folder,), [racer], listed)
    assert found is not None and found[0] == how
    view = views.clash_view(*found)
    assert name in view["why"]
    assert (view["existing"] is None) == (how == "holds")
    assert library.clash("E:\\Kiln\\kiln.exe", ("E:\\Kiln",), [racer], listed) is None


def test_the_clash_words_name_the_store_of_a_store_game():
    racer = _store("3999914", "Racer", "C:\\Games\\Racer")
    assert views.clash_view("store", racer) == {
        "existing": racer.game_id,
        "why": "This program is Racer's, which Steam lists: it is in the Library already, with Play."}


def test_a_tile_says_when_its_art_is_a_programs_icon():
    assert views.tile("Moss Garden", "g-0123456789abcdef", {"g-0123456789abcdef"}, {"g-0123456789abcdef"})["icon"]
    assert not views.tile("Moss Garden", "g-0123456789abcdef", {"g-0123456789abcdef"})["icon"]
    assert views.tile("Moss Garden", "g-0123456789abcdef", set(), {"g-0123456789abcdef"}) == {
        "initials": "MG", "hue": views.tile("Moss Garden")["hue"], "art": None, "icon": False}


# ---------------------------------------------------------------- the app ---

class Window:
    """What App's Add a game and Record when it starts use of the window: replies posted, work run at once."""
    hwnd = 0x4321

    def __init__(self) -> None:
        self.posted, self.view_hidden, self.queued = [], False, []

    def post(self, obj):
        self.posted.append(obj)
        return True

    def submit(self, work, done):
        try:
            result, error = work(), None
        except BaseException as exc:  # noqa: BLE001
            result, error = None, exc
        done(result, error)
        return True

    def call_soon(self, fn):                    # the recorder's messages wait: each test feeds its own
        self.queued.append(fn)

    def set_recording(self, on):
        pass

    def in_front(self):
        return True

    def want_attention(self):
        pass

    def drop_attention(self):
        pass


def make_app(tmp_path: Path, *, picked=None, games=(), log=None, asked=None):
    home = tmp_path / "home"
    asked = [] if asked is None else asked
    app = app_main.App(home=home, runtime={"version": "1.0.0.0", "build": 4000}, create_fn=None,
                       log=(log.append if log is not None else lambda s: None), play=lambda n: None,
                       flash=lambda on: None, launcher=lambda *a, **k: None, installed=lambda: list(games),
                       forms_of=lambda folder: (folder,), picker=picked,
                       play_recorder=lambda folder: asked.append(folder) or [str(fake_recorder.BASE_PY), "-I", "-c",
                                                                              "pass"])
    app.window = Window()
    app.get_readiness = lambda: READY
    return app


def program(tmp_path: Path, folder: str = "Moss Garden", name: str = "Moss Garden.exe", strings=MOSS,
            icons=None) -> str:
    where = tmp_path / "games" / folder
    where.mkdir(parents=True, exist_ok=True)
    path = where / name
    path.write_bytes(pe_build.program(strings, [pe_build.icon_bitmap(32, 32)] if icons is None else icons))
    return str(path)


def ask(app, kind: str, **args):
    """A request as the page sends it, answered as the window answers it; the reply's data, or its error."""
    from lagdoctor.app import bridge
    req = bridge.Request(kind=kind, id=7, args=args)
    app.window.posted.clear()
    app.on_message(req)
    (reply,) = app.window.posted
    return reply["data"] if reply["ok"] else reply["error"]


def test_the_program_chosen_is_shown_with_its_name_and_icon_before_it_is_added(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    d = ask(app, "pick_game")
    folder = str(Path(exe).parent)
    gid = library.added_id(exe)
    assert d["name"] == "Moss Garden" and d["exe"] == exe and d["folder"] == folder and d["note"] == ""
    assert d["exe_name"] == "Moss Garden.exe" and d["draft"].startswith("d-") and d["text"] == views.ADD_TEXT
    assert d["tile"]["art"].endswith(gid) and d["tile"]["icon"] is True
    kind, data = app.art_for(gid)
    assert kind == "image/x-icon" and data[22:] == pe_build.icon_bitmap(32, 32)
    assert not (app.home / added.NAME).exists()                    # nothing is added until the player says so


def test_the_dialog_is_owned_by_the_window_and_closing_it_changes_nothing(tmp_path):
    owners = []
    app = make_app(tmp_path, picked=lambda hwnd: owners.append(hwnd) or None)
    assert ask(app, "pick_game") == {"cancelled": True}
    assert owners == [Window.hwnd] and app.draft is None


def test_a_program_that_names_no_game_shows_by_its_file_and_says_so(tmp_path):
    exe = program(tmp_path, name="kiln_runner.exe", strings=None, icons=[])
    d = ask(make_app(tmp_path, picked=lambda hwnd: exe), "pick_game")
    assert d["name"] == "kiln_runner" and d["note"] == views.NAMED_BY_FILE and d["tile"]["art"] is None


def test_adding_keeps_the_program_by_the_name_given_and_opens_its_page(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    d = ask(app, "pick_game")
    r = ask(app, "add_game", draft=d["draft"], name="  Moss\nGarden: Deluxe ")
    assert r == {"game": library.added_id(exe)}
    assert added.load(app.home) == ([added.Kept(exe=exe, name="Moss Garden: Deluxe")], [])
    lib = ask(app, "library")
    assert [(g["name"], g["added"], g["tile"]["icon"]) for g in lib["games"]] == [("Moss Garden: Deluxe", True, True)]
    page = ask(app, "game", game=r["game"])
    assert page["wait"]["folder"] == str(Path(exe).parent) and page["added"] is True
    # the same draft adds once: it is gone once used
    assert ask(app, "add_game", draft=d["draft"], name="Again") == ("That program is no longer being added: choose it "
                                                                   "again with Add a game.")


def test_a_name_with_nothing_to_show_or_a_draft_never_given_is_refused(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    d = ask(app, "pick_game")
    assert ask(app, "add_game", draft=d["draft"], name=" \t ") == "Give the game a name to show by."
    assert ask(app, "add_game", draft=d["draft"], name="") == "Give the game a name to show by."    # said, not lost
    assert ask(app, "add_game", draft="d-0123456789abcdef", name="Moss").startswith("That program is no longer")
    assert not (app.home / added.NAME).exists()


def test_a_program_the_library_has_is_met_as_its_game(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    d = ask(app, "pick_game")
    ask(app, "add_game", draft=d["draft"], name="Moss Garden")
    again = ask(app, "pick_game")
    assert again == {"existing": library.added_id(exe), "why": "Moss Garden is in the Library already."}
    launcher = program(tmp_path, name="launcher.exe")
    app.picker = lambda hwnd: launcher
    assert ask(app, "pick_game")["existing"] == library.added_id(exe)       # its folder's game


def test_a_program_in_a_store_games_folder_is_met_as_the_store_game(tmp_path):
    exe = program(tmp_path, folder="Demo Racer\\Bin")
    import dataclasses
    racer = dataclasses.replace(app_demo.INSTALLED[0], folder=str(tmp_path / "games" / "Demo Racer"))
    app = make_app(tmp_path, picked=lambda hwnd: exe, games=[racer])
    got = ask(app, "pick_game")
    assert got["existing"] == library.game_id(f"store:steam:{racer.id}") and "which Steam lists" in got["why"]


def test_a_program_whose_folder_holds_another_games_is_refused(tmp_path):
    exe = program(tmp_path, folder="", name="all.exe")
    import dataclasses
    racer = dataclasses.replace(app_demo.INSTALLED[0], folder=str(tmp_path / "games" / "Demo Racer"))
    app = make_app(tmp_path, picked=lambda hwnd: exe, games=[racer])
    assert "holds Demo Racer's folder too" in ask(app, "pick_game")


def test_a_program_in_a_program_files_folder_itself_is_refused(tmp_path, monkeypatch):
    exe = program(tmp_path, folder="PF", name="tool.exe")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "games" / "PF"))
    assert "not in a folder of its own" in ask(make_app(tmp_path, picked=lambda hwnd: exe), "pick_game")
    # and one whose folder leads there through a junction: its real path is a Program Files folder
    other = program(tmp_path, folder="Apps", name="tool.exe")
    app = make_app(tmp_path, picked=lambda hwnd: other)
    app.forms_of = lambda folder: (folder, str(tmp_path / "games" / "PF"))
    assert "not in a folder of its own" in ask(app, "pick_game")


def test_a_program_whose_folder_holds_two_recorded_games_is_refused(tmp_path):
    exe = program(tmp_path, folder="", name="launcher.exe")
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    games = tmp_path / "games"
    recs = [_rec(app.home, "20261110-100000-moss", str(games / "Moss" / "moss.exe"), datetime(2026, 11, 10, 10)),
            _rec(app.home, "20261109-100000-kiln", str(games / "Kiln" / "kiln.exe"), datetime(2026, 11, 9, 10))]
    app.scan = lambda: recs
    assert "holds moss's folder too" in ask(app, "pick_game")


def test_a_program_off_the_local_disks_is_refused_unread(tmp_path, monkeypatch):
    exe = program(tmp_path)
    monkeypatch.setattr(target, "local_problem", lambda p: f"{p} is kept online only" if p == exe else None)
    got = ask(make_app(tmp_path, picked=lambda hwnd: exe), "pick_game")
    assert got == f"lag-doctor adds only programs on this PC's own disks: {exe} is kept online only."


def test_a_path_the_dialog_gave_that_is_no_program_is_refused(tmp_path):
    got = ask(make_app(tmp_path, picked=lambda hwnd: str(tmp_path / "notes.txt")), "pick_game")
    assert got.startswith("lag-doctor cannot add that program:") and "is not a program" in got


def test_a_dialog_that_failed_is_said_and_a_second_while_one_is_up_is_refused(tmp_path):
    def broken(hwnd):
        raise OSError(0, "Windows could not show it")
    app = make_app(tmp_path, picked=broken)
    assert ask(app, "pick_game") == "Windows' Open dialog could not open (Windows could not show it)."
    app.picking = True
    assert ask(app, "pick_game") == "Windows' Open dialog is open already."
    app.picker = None
    app.picking = False
    assert ask(app, "pick_game") == "Adding a game is not available in this window."


def test_an_app_given_no_picker_never_opens_the_real_dialog(tmp_path):
    from lagdoctor.winapi import dialogs
    app = make_app(tmp_path, picked=None)
    assert ask(app, "pick_game") == "Adding a game is not available in this window."
    assert dialogs._get_open_file_name.__name__ == "refuse"        # conftest's guard, which no call reached


def test_a_game_added_meanwhile_is_not_added_twice(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    d = ask(app, "pick_game")
    added.save(app.home, [added.Kept(exe=exe, name="Moss Garden")], unread=False)     # another window added it
    assert ask(app, "add_game", draft=d["draft"], name="Moss") == "Moss Garden is in the Library already."
    assert added.load(app.home)[0] == [added.Kept(exe=exe, name="Moss Garden")]


def test_adding_over_a_list_that_could_not_be_read_keeps_its_bytes_aside(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    (app.home / added.NAME).write_bytes(b"{written by something else")
    ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")
    [aside] = app.home.glob("added-games.unread-*.json")
    assert aside.read_bytes() == b"{written by something else"
    assert added.load(app.home)[0] == [added.Kept(exe=exe, name="Moss Garden")]


def test_icons_past_the_arts_total_are_left_out(tmp_path, monkeypatch):
    from lagdoctor.stores import art
    exe = program(tmp_path)
    monkeypatch.setattr(art, "MAX_TOTAL_BYTES", 100)
    d = ask(make_app(tmp_path, picked=lambda hwnd: exe), "pick_game")
    assert d["name"] == "Moss Garden" and d["tile"]["art"] is None and d["tile"]["icon"] is False


def test_the_library_holds_at_most_max_games_added_by_hand(tmp_path, monkeypatch):
    monkeypatch.setattr(added, "MAX_GAMES", 1)
    first, second = program(tmp_path, folder="A", name="a.exe"), program(tmp_path, folder="B", name="b.exe")
    app = make_app(tmp_path, picked=lambda hwnd: first)
    ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="A")
    app.picker = lambda hwnd: second
    got = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="B")
    assert got == "The Library holds at most 1 games added by hand: remove one first."


def test_removing_from_a_list_that_held_an_unread_entry_keeps_its_bytes_aside(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    original = json.dumps({"version": 1, "games": [{"exe": exe, "name": "Moss Garden"},
                                                  {"exe": "D:\\moss.exe", "name": "On a drive"}]}).encode("utf-8")
    (app.home / added.NAME).write_bytes(original)
    assert ask(app, "remove_game", game=library.added_id(exe)) == {}
    [aside] = app.home.glob("added-games.unread-*.json")
    assert aside.read_bytes() == original and added.load(app.home) == ([], [])


def test_removing_an_added_game_keeps_its_recordings_under_their_recorded_game(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]
    rec = _rec(app.home, "20261110-100000-moss_garden", exe, datetime(2026, 11, 10, 10))
    app.scan = lambda: [rec]
    assert [g["name"] for g in ask(app, "library")["games"]] == ["Moss Garden"]
    assert ask(app, "remove_game", game=gid) == {}
    assert added.load(app.home) == ([], [])
    assert [g["name"] for g in ask(app, "library")["games"]] == ["moss_garden"]
    assert ask(app, "remove_game", game=gid) == "That game was not added by hand, so there is nothing to remove."


def test_record_when_it_starts_waits_on_the_added_games_folder_and_opens_no_link(tmp_path):
    asked = []
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe, asked=asked)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]
    opened = []
    app.open_link = lambda link, should_go: opened.append(link)
    view = ask(app, "wait", game=gid)
    folder = str(Path(exe).parent)
    assert asked == [folder] and app.rec.folder == folder and app.rec_link is None
    assert app.rec_state.play == {"game": gid, "title": "Moss Garden", "store": "", "self": True}
    assert view["play"] == {"game": gid, "wait": True} and view["game"] == "Moss Garden"
    app.on_recorder(app.rec, {"t": "launch"})
    assert opened == [] and app.rec_state.phase == "launching"
    assert app.recording_view()["title"] == "Start the game now"
    app.rec.proc.wait(30)


def test_record_when_it_starts_refuses_a_folder_that_is_gone_or_a_game_with_none(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]
    import shutil
    shutil.rmtree(Path(exe).parent)
    assert ask(app, "wait", game=gid) == (f"That game's folder, {Path(exe).parent}, is not there any more, so "
                                         "lag-doctor cannot wait for it.")
    assert ask(app, "wait", game="g-0000000000000000").startswith("lag-doctor knows no folder of that game")
    assert app.rec is None


def test_play_never_starts_an_added_game(tmp_path):
    exe = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: exe)
    gid = ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")["game"]
    assert ask(app, "play", game=gid) == "That game is not installed any more, so lag-doctor cannot start it."
    assert app.rec is None


def test_each_added_programs_icon_is_read_once_a_run(tmp_path, monkeypatch):
    from lagdoctor.stores import exe as exe_mod
    path = program(tmp_path)
    app = make_app(tmp_path, picked=lambda hwnd: path)
    ask(app, "add_game", draft=ask(app, "pick_game")["draft"], name="Moss Garden")
    app2 = make_app(tmp_path)                                   # the next run of the window, on the same home
    reads, real = [], exe_mod.read
    monkeypatch.setattr(exe_mod, "read", lambda p: reads.append(p) or real(p))
    ask(app2, "library")
    ask(app2, "library")
    assert reads == [path] and app2.art_for(library.added_id(path))[0] == "image/x-icon"


# ---------------------------------------------------------------- the running screen ---

SELF = {"game": "g-0123456789abcdef", "title": "Moss Garden", "store": "", "self": True}
SELF_RUN = [{"t": "hello", "v": 2, "pid": 7}, {"t": "elevate", "argv": ["x"], "cwd": "c"},
            {"t": "prompt", "how": "approved"}, {"t": "launch"}, {"t": "waiting"},
            {"t": "target", "name": "moss.exe", "pid": 9, "how": "folder"},
            {"t": "recording", "duration_s": 600.0, "hotkey": True, "frames": True, "network": True}]


def test_record_when_it_starts_says_when_to_start_the_game():
    state = recording.RecordingState(play=dict(SELF))
    seen = [views.recording_view(state, 99.0, "ctrl+alt+l")]
    for i, m in enumerate(SELF_RUN):
        state.apply(m, 100.0 + i)
        seen.append(views.recording_view(state, 100.0 + i, "ctrl+alt+l"))
    starting, asking, preparing, launching, waiting = seen[0], seen[2], seen[3], seen[4], seen[5]
    assert "Do not start the game yet" in starting["detail"] and "Do not start the game yet" in preparing["detail"]
    assert asking["detail"].endswith("Start the game only once you have answered it.")
    for v in (launching, waiting):
        assert v["title"] == "Start the game now" and v["detail"].startswith("Start Moss Garden yourself")
        assert "for up to 5 minutes" in v["detail"] and "store" not in v["detail"]
    assert all(v["play"] == {"game": SELF["game"], "wait": True} for v in seen)
    assert seen[-1]["title"] == "Recording"


@pytest.mark.parametrize("reason, title, words", [
    ("no_game", "The game did not start", "Press Record when it starts again, answer Windows' prompt, then start"),
    ("game_running", "Not recorded", "start the game once Windows' prompt is answered"),
])
def test_how_record_when_it_starts_ended_is_said_in_its_own_words(reason, title, words):
    state = recording.RecordingState(play=dict(SELF))
    for m in SELF_RUN[:5] + [{"t": "done", "reason": reason, "code": 2, "session": None}]:
        state.apply(m, 100.0)
    v = views.recording_view(state, 200.0, "ctrl+alt+l")
    assert v["title"] == title and words in v["detail"] and "store" not in v["detail"]


def test_the_page_names_a_draft_only_by_its_token_and_a_name_only_as_text():
    from lagdoctor.app import bridge
    ok = bridge.parse(json.dumps({"type": "request", "id": 3, "kind": "add_game", "draft": "d-0123456789abcdef",
                                  "name": "Moss Garden"}))
    assert ok.kind == "add_game" and ok.args == {"draft": "d-0123456789abcdef", "name": "Moss Garden"}
    empty = bridge.parse(json.dumps({"type": "request", "id": 3, "kind": "add_game", "draft": "d-0123456789abcdef",
                                     "name": ""}))
    assert empty.args["name"] == ""                          # the app refuses it in words (do_add_game)
    for bad in [{"draft": "D:\\Games\\moss.exe", "name": "x"}, {"draft": "d-0123", "name": "x"},
                {"draft": "d-0123456789abcdef", "name": 5},
                {"draft": "d-0123456789abcdef", "name": "x" * (bridge.MAX_NAME_CHARS + 1)},
                {"draft": "d-0123456789abcdef"}, {"draft": "d-0123456789abcdef", "name": "x", "exe": "y"},
                {"draft": "d-0123456789abcdef0", "name": "x"}]:
        assert bridge.parse(json.dumps({"type": "request", "id": 3, "kind": "add_game", **bad})) is None, bad
    assert bridge.parse(json.dumps({"type": "request", "id": 3, "kind": "pick_game", "path": "C:\\x.exe"})) is None
    assert bridge.parse(json.dumps({"type": "request", "id": 3, "kind": "wait", "game": "D:\\Games"})) is None
