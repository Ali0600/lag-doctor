"""The command line: argument checks, the elevation choice, and fix / undo through the journal."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lagdoctor import cli, paths
from lagdoctor.fixes import catalog
from lagdoctor.fixes.base import GONE, Fix
from lagdoctor.fixes.journal import Journal

STORE: dict[str, object] = {}


class FakeFix(Fix):
    id = "test_cli_fake"
    title = "Turn the fake setting up"

    def target(self, params):
        return f"fake setting {params['name']}"

    def read(self, params):
        return STORE.get(params["name"], GONE if params.get("gone") else 1)

    def write(self, params, value):
        STORE[params["name"]] = value

    def wanted(self, params, current):
        return 5


@pytest.fixture
def fake_fix():
    STORE.clear()
    catalog.register(FakeFix)
    yield FakeFix
    catalog._REGISTRY.pop(FakeFix.id, None)
    STORE.clear()


def write_last_diagnosis(fixes):
    d = paths.sessions_dir() / "20261104-180000-Game"
    d.mkdir(parents=True, exist_ok=True)
    diag = {"findings": [{"cause": "x", "title": "x", "fixes": fixes}]}
    (d / cli.DIAGNOSIS_NAME).write_text(json.dumps(diag), encoding="utf-8")
    (paths.home() / cli.LAST_SESSION).write_text(str(d), encoding="utf-8")
    return d


def test_watch_arguments_are_checked():
    p = cli.build_parser()
    a = p.parse_args(["watch", "--pid", "5", "--duration", "30", "--elevate", "--server", " 203.0.113.7 "])
    assert (a.pid, a.duration, a.elevate, a.server) == (5, 30.0, True, "203.0.113.7")
    for bad in (["watch", "--elevate", "--no-elevate"], ["watch", "--duration", "0"], ["watch", "--duration", "nan"],
                ["watch", "--server", "evil.example; calc"], ["watch", "--pid", "5", "--game", "x.exe"]):
        with pytest.raises(SystemExit):
            p.parse_args(bad)


def test_elevation_choice_literal_points():
    never = lambda *a, **k: pytest.fail("must not ask")  # noqa: E731
    assert cli.elevation_choice(True, False, False, never) == "elevate"
    assert cli.elevation_choice(False, True, True, never) == "skip"
    assert cli.elevation_choice(False, False, False, never) == "skip"       # no console to ask on: never elevate
    assert cli.elevation_choice(False, False, True, lambda *a, **k: True) == "elevate"
    assert cli.elevation_choice(False, False, True, lambda *a, **k: False) == "skip"


def test_the_elevated_child_records_only_this_game_into_this_file(tmp_path):
    args = cli.build_parser().parse_args(["watch", "--game", "x.exe", "--duration", "20", "--no-network",
                                          "--hotkey", "none", "--elevate"])
    argv = cli.elevated_argv(args, {"pid": 4242, "name": "x.exe"}, tmp_path / "c.ldcap.json.gz")
    assert argv[1:4] == ["-m", "lagdoctor", "watch"]
    assert ["--pid", "4242"] == argv[4:6] and "--capture-only" in argv
    assert argv[argv.index("--out-file") + 1] == str(tmp_path / "c.ldcap.json.gz")
    assert "--elevate" not in argv and "--game" not in argv and "--no-network" in argv
    assert argv[argv.index("--duration") + 1] == "20.0" and argv[argv.index("--hotkey") + 1] == "none"
    assert all(isinstance(a, str) for a in argv)


def test_fix_then_undo_round_trip(fake_fix):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 0
    assert STORE["a"] == 5
    entries = Journal().undoable()
    assert [e.fix_id for e in entries] == ["test_cli_fake"] and entries[0].before == 1
    assert cli.main(["undo", "--all"]) == 0
    assert STORE["a"] == 1 and Journal().undoable() == []


def test_fix_asks_first_and_a_no_changes_nothing(fake_fix, monkeypatch):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["fix", "test_cli_fake"]) == 0
    assert "a" not in STORE and Journal().entries() == []


def test_fix_refuses_what_the_report_did_not_suggest(fake_fix):
    write_last_diagnosis([{"title": "Advice only", "fix_id": None, "params": {}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 2
    assert STORE == {}


def test_fix_refuses_to_run_elevated(fake_fix, monkeypatch):
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    monkeypatch.setattr(cli, "_elevated", lambda: True)
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 2
    assert STORE == {}


def test_start_up_settles_an_interrupted_change_and_ended_session_changes(fake_fix):
    j = Journal()
    pending = j.begin("test_cli_fake", "fake setting a", {"name": "a"}, 1, 5)
    STORE["a"] = 5                                            # the write happened, then the tool crashed
    ended = j.begin("test_cli_fake", "fake setting b", {"name": "b", "gone": True}, 1, 5, session_only=True)
    j.set_state(ended.id, "applied")                          # its process has since exited (reads GONE)
    cli._settle_journal()
    assert Journal().get(pending.id).state == "applied"
    assert Journal().get(ended.id).state == "undone"


def test_a_fix_that_refuses_at_plan_time_is_reported_and_changes_nothing(fake_fix, monkeypatch, capsys):
    from lagdoctor.fixes.priority import Refused

    def refuse(self, params):
        raise Refused("that process is a Windows service, so lag-doctor leaves it alone")
    monkeypatch.setattr(FakeFix, "plan", refuse)
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    assert cli.main(["fix", "test_cli_fake", "--yes"]) == 1
    assert "Not changed: that process is a Windows service" in capsys.readouterr().out
    assert STORE == {} and Journal().entries() == []


def test_a_plans_notes_are_shown_before_asking(fake_fix, monkeypatch, capsys):
    real_plan = FakeFix.plan

    def with_note(self, params):
        p = real_plan(self, params)
        p.notes.append("this value's meaning is community-sourced")
        return p
    monkeypatch.setattr(FakeFix, "plan", with_note)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    write_last_diagnosis([{"title": "Turn it up", "fix_id": "test_cli_fake", "params": {"name": "a"}}])
    cli.main(["fix", "test_cli_fake"])
    assert "note:   this value's meaning is community-sourced" in capsys.readouterr().out


def test_fix_offers_are_deduplicated():
    diag = {"findings": [{"fixes": [{"fix_id": "a", "title": "A", "params": {"x": 1}}, {"fix_id": None}]},
                         {"fixes": [{"fix_id": "a", "title": "A again", "params": {"x": 1}},
                                    {"fix_id": "a", "title": "A other", "params": {"x": 2}}]}]}
    assert cli.fix_offers(diag) == [("a", "A", {"x": 1}), ("a", "A other", {"x": 2})]


def test_the_last_diagnosis_falls_back_to_the_newest_session(tmp_path):
    d = paths.sessions_dir() / "20261104-170000-Old"
    d.mkdir(parents=True)
    (d / cli.DIAGNOSIS_NAME).write_text(json.dumps({"findings": [], "headline": "old"}), encoding="utf-8")
    (paths.home() / cli.LAST_SESSION).write_text(str(tmp_path / "gone"), encoding="utf-8")
    assert cli.load_last_diagnosis()["headline"] == "old"
