"""The app's data side in plain pytest: the session summary, the library's listing, the view-models, the samples
around a moment (analysis.window) and the chart labels."""
from __future__ import annotations

import array
import dataclasses
import json
import math
from datetime import datetime
from pathlib import Path

import pytest

from lagdoctor import paths
from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.analysis.frames import main_chain
from lagdoctor.analysis.window import window
from lagdoctor.app import library, views
from lagdoctor.model import F, G, N, S, Diagnosis, Evidence, Finding, FixSuggestion, LagEvent
from lagdoctor.report import labels, summary
from tests import app_demo, synth


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> tuple[Path, dict]:
    home = tmp_path_factory.mktemp("app-home")
    return home, app_demo.build(home)


def entries_of(home: Path) -> list[library.Entry]:
    return library.scan(home / "sessions")


# ------------------------------------------------------------------------------------------------ summary --

def test_a_summary_holds_who_when_and_the_numbers_the_library_shows():
    s = synth.laglab("hitch", duration_s=30.0)
    d = diagnose(s.cap)
    out = summary.build(d, s.cap, "watch")
    assert out["schema"] == summary.SCHEMA and out["kind"] == "watch"
    assert out["game"] == "LagLab - hitch" and out["exe_name"] == "python.exe"
    assert out["exe"] == r"C:\Python313\python.exe" and out["started_at"] == s.cap.started_at
    assert out["tool_version"] and out["lag_type"] == d.lag_type and out["headline"] == d.headline
    assert out["avg_fps"] == pytest.approx(d.stats["avg_fps"]) and out["frames_measured"] is True
    assert out["top_cause"] == d.findings[0].cause and out["refresh_hz"] == pytest.approx(synth.REFRESH_HZ)
    assert out["note"] is None                                   # watch without --note
    s.cap.note = "installed FrameFix"
    assert summary.build(d, s.cap, "watch")["note"] == "installed FrameFix"
    with pytest.raises(ValueError):
        summary.build(d, s.cap, "check-up")


def test_a_summary_reads_back_with_each_field_checked(tmp_path):
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps({
        "schema": summary.SCHEMA, "kind": "watch", "game": "G\x07ame", "avg_fps": "fast", "duration_s": 1e300,
        "frames_measured": "yes", "exe_name": 5, "headline": "x" * 1000}), encoding="utf-8")
    s = summary.read(tmp_path)
    assert s["game"] == "G ame"                       # a control character is no part of a name
    assert s["avg_fps"] is None and s["duration_s"] is None and s["frames_measured"] is None and s["exe_name"] is None
    assert len(s["headline"]) == summary.MAX_TEXT
    assert s["note"] is None                                     # a summary from before notes: none


@pytest.mark.parametrize("note, read", [
    ("shadows to low", "shadows to low"), ("sha" + chr(7) + "dows  " + chr(27) + "to low", "sha dows to low"),
    ("x" * 250, "x" * 197 + "..."), ("   ", None), (5, None), (["x"], None), ({"text": "x"}, None)])
def test_a_summarys_note_reads_back_as_one_clean_line_and_one_of_another_type_as_none(tmp_path, note, read):
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps({"schema": summary.SCHEMA, "kind": "watch", "note": note}),
                                                 encoding="utf-8")
    assert summary.read(tmp_path)["note"] == read


HEADROOM = ("At these settings the CPU could feed about 66-78 FPS, so a GPU about 1.1-1.3x faster would make the CPU "
            "the limit.")
ROOM_STATS = {"bound": "gpu", "steady_fps": 58.5, "bound_gpu_ms": 16.1, "bound_frame_ms": 16.9,
              "busiest_thread_pct": 75.0, "gpu_ceiling_fps": 62.1, "cpu_ceiling_low_fps": 66.3,
              "cpu_ceiling_high_fps": 78.0, "next_limit": "cpu", "speedup_low": 1.07, "speedup_high": 1.33,
              "gpu_clock_mhz": 780.0, "gpu_clock_max_mhz": 2100.0, "gpu_power_w": 61.0, "gpu_power_limit_w": 102.8,
              "headroom": HEADROOM}


def test_a_summary_keeps_what_limited_the_frame_rate_and_how_close_the_next_limit_was(tmp_path):
    s = synth.laglab("smooth", duration_s=30.0)
    d = diagnose(s.cap)
    d.stats.update(ROOM_STATS)
    out = summary.build(d, s.cap, "watch")
    assert {k: out[k] for k in ROOM_STATS} == ROOM_STATS
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps(out), encoding="utf-8")
    assert {k: summary.read(tmp_path)[k] for k in ROOM_STATS} == ROOM_STATS


def test_a_summarys_headroom_keys_read_back_checked_and_an_older_one_without_them_as_none(tmp_path):
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps({
        "schema": summary.SCHEMA, "kind": "watch", "bound": "the CPU, honest", "next_limit": "display",
        "steady_fps": "fast", "gpu_clock_mhz": 1e300, "busiest_thread_pct": True, "headroom": "a\x07b" + "x" * 400}),
        encoding="utf-8")
    s = summary.read(tmp_path)
    assert s["bound"] is None and s["next_limit"] == "display"          # only a verdict's own word is a verdict
    assert s["steady_fps"] is None and s["gpu_clock_mhz"] is None and s["busiest_thread_pct"] is None
    assert s["headroom"].startswith("a b") and len(s["headroom"]) == summary.MAX_TEXT
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps({"schema": summary.SCHEMA, "kind": "watch"}),
                                                 encoding="utf-8")
    old = summary.read(tmp_path)
    assert all(old[k] is None for k in ROOM_STATS)


@pytest.mark.parametrize("content", [b"", b"not json", b"[]", json.dumps({"schema": "other/1", "kind": "watch"}).encode(),
                                     json.dumps({"schema": summary.SCHEMA, "kind": "check-up"}).encode(),
                                     # Past the cap, even when what fits is a whole summary: the rest is never read.
                                     b'{"schema": "lagdoctor.summary/1", "kind": "watch"}' +
                                     b" " * summary.MAX_BYTES + b"tail", b"\xff\xfe"])
def test_anything_but_a_summary_reads_as_none(tmp_path, content):
    (tmp_path / summary.SUMMARY_NAME).write_bytes(content)
    assert summary.read(tmp_path) is None
    assert summary.read(tmp_path / "missing") is None


def test_a_watch_writes_its_summary_and_an_analyze_names_its_kind(tmp_path, monkeypatch):
    from lagdoctor import cli
    s = synth.laglab("smooth", duration_s=20.0)
    monkeypatch.setattr(cli, "say", lambda text: None)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    for kind, remember in (("watch", True), ("analyze", False)):
        folder = tmp_path / kind
        folder.mkdir()
        assert cli.analyze_and_report(s.cap, folder / cli.CAPTURE_NAME, folder, remember=remember) == 0
        assert summary.read(folder)["kind"] == kind


def test_a_summary_that_cannot_be_written_is_said_and_the_analysis_still_ends_well(tmp_path, monkeypatch):
    from lagdoctor import cli
    said = []
    monkeypatch.setattr(cli, "say", said.append)
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    real = cli._write_json

    def write(path, data):
        if path.name == summary.SUMMARY_NAME:
            raise PermissionError("denied")
        real(path, data)
    monkeypatch.setattr(cli, "_write_json", write)
    s = synth.laglab("smooth", duration_s=20.0)
    assert cli.analyze_and_report(s.cap, tmp_path / cli.CAPTURE_NAME, tmp_path) == 0
    assert (tmp_path / cli.REPORT_NAME).is_file()
    assert any("summary of this session could not be saved" in line for line in said)


# ------------------------------------------------------------------------------------------------ library --

def test_the_library_lists_recordings_newest_first_and_leaves_check_ups_and_analyses_out(demo):
    home, built = demo
    entries = entries_of(home)
    recordings = [e for e in entries if e.is_recording]
    assert [e.id for e in recordings] == sorted(built, key=lambda sid: built[sid]["demo"].when, reverse=True)
    assert [e.kind for e in entries if not e.is_recording] == ["check-up"]


def test_an_older_session_without_a_summary_is_named_by_its_folder(demo):
    home, built = demo
    old = next(e for e in entries_of(home) if e.id.endswith("-starplanner"))
    assert old.summary is None and old.game == "starplanner" and old.game_key == "starplanner"
    assert old.started == datetime(2026, 11, 2, 18, 30, 0)
    assert views.numbers(old)["avg_fps"] is not None          # its diagnosis.json still gives the numbers


def test_a_games_sessions_share_one_key_and_the_page_gets_an_opaque_id(demo):
    home, _ = demo
    racer = [e for e in entries_of(home) if e.game == "Demo Racer"]
    assert len(racer) == 2 and {e.game_key for e in racer} == {"demoracer"}
    assert racer[0].game_id == library.game_id("demoracer") and racer[0].game_id.startswith("g-")
    assert "demoracer" not in racer[0].game_id


@pytest.mark.parametrize("name, parsed", [
    ("20261104-194500-ff7rebirth_", (datetime(2026, 11, 4, 19, 45), "ff7rebirth_")),
    ("20261104-194500-game-2", (datetime(2026, 11, 4, 19, 45), "game")),          # session_dir's second folder
    ("20261104-194500-check-up", (datetime(2026, 11, 4, 19, 45), "check-up")),
    ("20261399-194500-x", (None, "x")),                                            # a date that is no date
    ("notes", None), ("2026-11-04-x", None), ("20261104-194500-", None)])
def test_a_session_folders_name_reads_back_as_session_dir_wrote_it(name, parsed):
    assert library.parse_folder_name(name) == parsed


def test_session_dir_and_the_library_agree_on_a_games_stem(tmp_path):
    from lagdoctor.capture.session import session_dir
    for name in ("FF7Rebirth_.exe", "<b>quest</b>.exe", "Space Game!.exe", 42, None, "....exe"):
        folder = session_dir(tmp_path, {"name": name}, when=datetime(2026, 1, 2, 3, 4, 5))
        assert library.parse_folder_name(folder.name)[1] == paths.folder_stem(name)


def test_the_last_session_counts_only_when_it_is_a_folder_of_this_library(tmp_path):
    base = tmp_path / "sessions"
    (base / "20261104-194500-x").mkdir(parents=True)
    (tmp_path / library.LAST_SESSION).write_text(str(base / "20261104-194500-x"), encoding="utf-8")
    assert library.last_session(tmp_path, base) == "20261104-194500-x"
    (tmp_path / library.LAST_SESSION).write_text(str(tmp_path / "elsewhere" / "20261104-194500-x"), encoding="utf-8")
    assert library.last_session(tmp_path, base) is None
    (tmp_path / library.LAST_SESSION).write_text("", encoding="utf-8")
    assert library.last_session(tmp_path, base) is None
    (tmp_path / library.LAST_SESSION).unlink()
    assert library.last_session(tmp_path, base) is None


# ------------------------------------------------------------------------------------------------ views --

READY = {"ok": True, "text": "Frame timing ready", "detail": ""}


def test_the_library_view_has_one_tile_per_game_with_its_newest_recording(demo):
    home, _ = demo
    v = views.library_view(entries_of(home), READY)
    assert [g["name"] for g in v["games"]] == [app_demo.MARKUP_TITLE, "Demo Racer", "starplanner"]
    racer = v["games"][1]
    assert racer["recordings"] == 2 and racer["last"] == "3 Nov 2026, 21:00" and racer["lag_type"] == "No lag found"
    assert v["recordings"] == 4 and v["recent"] == [g["id"] for g in v["games"]] and v["readiness"] is READY


def test_the_game_view_puts_the_last_recording_on_top_and_marks_the_one_fix_acts_on(demo):
    home, built = demo
    entries = entries_of(home)
    gid = library.game_id("demoracer")
    v = views.game_view(entries, gid, last_id="20261103-210000-demoracer")
    assert v["name"] == "Demo Racer" and v["last"]["session"] == "20261103-210000-demoracer"
    assert [s["id"] for s in v["sessions"]] == ["20261103-210000-demoracer", "20261101-201500-demoracer"]
    assert [s["is_last"] for s in v["sessions"]] == [True, False]
    assert [s["note"] for s in v["sessions"]] == [app_demo.MARKUP_NOTE, ""]
    assert views.game_view(entries, "g-0000000000000000", None) is None


def _load(entry):
    from lagdoctor.model import Capture
    cap = Capture.load(entry.capture)
    return cap, Diagnosis.from_json(library.read_diagnosis(entry)), main_chain(cap)


def test_results_show_fix_commands_only_on_the_last_recording(demo):
    home, _ = demo
    entry = next(e for e in entries_of(home) if e.game == app_demo.MARKUP_TITLE)
    cap, d, main = _load(entry)
    last = views.results_view(entry, d, cap, main, is_last=True, has_report=True)
    other = views.results_view(entry, d, cap, main, is_last=False, has_report=True)
    commands = [fx["command"] for f in last["findings"] for fx in f["fixes"] if fx["command"]]
    assert commands and all(c.startswith("lagdoctor fix ") for c in commands) and last["fix_note"] == ""
    assert not [fx["command"] for f in other["findings"] for fx in f["fixes"] if fx["command"]]
    assert other["fix_note"] == views.NO_FIX_HERE
    assert last["game"] == app_demo.MARKUP_TITLE                # text, as it came: the page shows it as text
    assert last["note"] == ""                                     # recorded without a note


def test_results_show_the_recordings_note_as_its_capture_keeps_it(demo):
    home, _ = demo
    entry = next(e for e in entries_of(home) if e.game == "Demo Racer")    # the newest: the one with a note
    cap, d, main = _load(entry)
    assert views.results_view(entry, d, cap, main, is_last=False, has_report=True)["note"] == app_demo.MARKUP_NOTE
    cap.note = "a" + chr(7) + "b"                               # a Capture made in code: cleaned all the same
    assert views.results_view(entry, d, cap, main, is_last=False, has_report=True)["note"] == "a b"


def test_results_name_every_chart_series_in_words_and_say_why_a_source_is_missing(demo):
    home, _ = demo
    entry = next(e for e in entries_of(home) if e.game == app_demo.MARKUP_TITLE)
    cap, d, main = _load(entry)
    v = views.results_view(entry, d, cap, main, is_last=False, has_report=False)
    by_id = {c["id"]: c for c in v["charts"]}
    assert [s["label"] for s in by_id["cpu_gpu"]["series"]][:1] == ["the game's GPU use (%)"]
    assert by_id["nv_clock"]["missing"].startswith("Not measured: the game renders on the Intel GPU")
    assert all(len(s["values"]) == len(by_id["cpu_gpu"]["t"]) for s in by_id["cpu_gpu"]["series"])
    assert v["events"] and all(e["t"] is not None for e in v["events"])
    assert [e["t"] for e in v["events"]] == sorted(e["t"] for e in v["events"])


def test_the_frame_times_go_over_as_doubles_times_first_with_gaps_as_nan():
    main = {"t": [0.0, 0.01, "junk", 0.03, 0.04], "frame_ms": [10.0, None, 7.0, -1.0, 12.5]}
    data, n = views.frame_bytes(main)
    assert n == 4
    values = array.array("d")
    values.frombytes(data)
    assert list(values[:4]) == [0.0, 0.01, 0.03, 0.04]
    assert values[4] == 10.0 and math.isnan(values[5]) and math.isnan(values[6]) and values[7] == 12.5


def test_a_moment_shows_the_samples_around_it_and_why_its_cause_was_named(demo):
    home, _ = demo
    entry = next(e for e in entries_of(home) if e.game == "Demo Racer" and e.summary["lag_type"] == "stutter")
    cap, d, main = _load(entry)
    ev = max((e for e in d.events if e.kind == "stutter"), key=lambda e: e.severity)
    m = views.moment_view(cap, d, main, ev.id)
    assert m["span"] == [pytest.approx(max(0.0, ev.t_start - views.MOMENT_PAD_S)),
                         pytest.approx(ev.t_end + views.MOMENT_PAD_S)]
    assert m["why"] == ev.detail["why"] and m["frames"] is not None
    assert all(m["span"][0] <= t <= m["span"][1] for t in m["frames"]["t"])
    assert views.moment_view(cap, d, main, 10 ** 6) is None


def test_compare_puts_the_conditions_beside_the_numbers_and_says_which_is_better(demo):
    home, _ = demo
    a, b = (e for e in entries_of(home) if e.game == "Demo Racer")
    v = views.compare_view(a, b)
    rows = {r["name"]: r for r in v["numbers"]}
    assert rows["Average FPS"]["better"] == ("a" if views.numbers(a)["avg_fps"] > views.numbers(b)["avg_fps"] else "b")
    assert rows["Stutters / min"]["better"] == "a"              # the smooth recording, newer, stuttered less
    names = [r["name"] for r in v["conditions"]]
    assert names == ["Your note", "Power plan and mode", "Refresh rate", "Frames timed", "Measured length",
                     "Game's exe file", "Drop-in files", "Unreal settings files"]
    assert v["note"] == ""


def test_compare_shows_what_limited_each_recording_and_how_close_the_next_limit_was(demo):
    home, _ = demo
    a, b = (e for e in entries_of(home) if e.game == "Demo Racer")
    cpu = dict(ROOM_STATS, bound="cpu_main", steady_fps=40.0, bound_gpu_ms=10.5, bound_frame_ms=25.0,
               busiest_thread_pct=95.0, gpu_clock_mhz=None, headroom=None)
    a = dataclasses.replace(a, summary=dict(a.summary, **ROOM_STATS))
    b = dataclasses.replace(b, summary=dict(b.summary, **cpu))
    v = views.compare_view(a, b)
    rows = {r["name"]: r for r in v["numbers"]}
    assert rows["FPS outside lag moments"] == {"name": "FPS outside lag moments", "a": "58", "b": "40", "better": "a"}
    assert rows["Limited by"] == {"name": "Limited by", "a": "GPU", "b": "CPU (1 thread)", "better": None}
    assert (rows["GPU busy per frame"]["a"], rows["GPU busy per frame"]["b"]) == ("16.1 of 16.9 ms", "10.5 of 25.0 ms")
    assert (rows["Busiest game thread"]["a"], rows["Busiest game thread"]["b"]) == ("75% of a CPU", "95% of a CPU")
    assert rows["Busiest game thread"]["better"] is None
    assert (rows["NVIDIA GPU clock"]["a"], rows["NVIDIA GPU clock"]["b"]) == ("780 of 2,100 MHz", "-")
    assert (rows["Headroom"]["a"], rows["Headroom"]["b"]) == (HEADROOM, "-")
    assert v["note"] == labels.HEADROOM_NOTE                               # what the figures rest on, once
    # the demo's own recordings: LagLab capped at 120 FPS on the Intel GPU, so no limiting part and no NVIDIA clock
    plain = views.compare_view(*(e for e in entries_of(home) if e.game == "Demo Racer"))
    rows = {r["name"]: r for r in plain["numbers"]}
    assert rows["Limited by"]["a"] == rows["Limited by"]["b"] == "FPS cap"
    assert rows["Headroom"]["a"] == rows["NVIDIA GPU clock"]["a"] == "-" and plain["note"] == ""
    # a summary from before the headroom figures: dashes
    old = dataclasses.replace(a, summary={k: v for k, v in a.summary.items() if k not in ROOM_STATS})
    rows = {r["name"]: r for r in views.compare_view(old, b)["numbers"]}
    assert all(rows[name]["a"] == "-" for name in ("FPS outside lag moments", "Limited by", "GPU busy per frame",
                                                    "Busiest game thread", "NVIDIA GPU clock", "Headroom"))


def test_compare_shows_what_changed_in_the_game_between_the_two_recordings(demo):
    home, _ = demo
    a, b = (e for e in entries_of(home) if e.game == "Demo Racer")          # newest first: after the fix, before it
    rows = {r["name"]: r for r in views.compare_view(a, b)["conditions"]}
    assert rows["Drop-in files"] == {"name": "Drop-in files", "a": "dxgi.dll, scripts\\FrameFix.asi",
                                     "b": "none found", "better": None, "differs": True}
    # Settings changed in the game's own menu: the same file in another copy, marked on both sides.
    assert rows["Unreal settings files"]["a"] == rows["Unreal settings files"]["b"] == \
        "Engine.ini, GameUserSettings.ini (different copy)"
    assert rows["Unreal settings files"]["differs"] is True
    assert rows["Game's exe file"] == {"name": "Game's exe file", "a": "77.5 MB, dated 30 Oct 2026, 09:12",
                                       "b": "77.5 MB, dated 30 Oct 2026, 09:12", "better": None, "differs": False}
    same = {r["name"]: r["differs"] for r in views.compare_view(b, b)["conditions"]}
    assert not any(same.values())


def test_compare_shows_each_recordings_note_and_marks_two_notes_that_differ(demo):
    home, _ = demo
    a, b = (e for e in entries_of(home) if e.game == "Demo Racer")          # newest first: the one with a note
    row = views.compare_view(a, b)["conditions"][0]
    assert row == {"name": "Your note", "a": app_demo.MARKUP_NOTE, "b": "-", "better": None, "differs": False}
    assert views.compare_view(b, a)["conditions"][0]["a"] == "-"
    assert views.compare_view(a, a)["conditions"][0]["differs"] is False   # the same note on both sides
    other = dataclasses.replace(b, summary=dict(b.summary, note="shadows to low"))
    assert views.compare_view(a, other)["conditions"][0] == {
        "name": "Your note", "a": app_demo.MARKUP_NOTE, "b": "shadows to low", "better": None, "differs": True}
    old = next(e for e in entries_of(home) if e.summary is None)          # no summary at all: no note either
    assert views.compare_view(a, old)["conditions"][0]["b"] == "-"


def test_compare_shows_a_dash_for_a_recording_from_before_lag_doctor_looked_at_the_game(demo):
    home, _ = demo
    racer = next(e for e in entries_of(home) if e.game == "Demo Racer")
    older = next(e for e in entries_of(home) if e.summary is not None and e.game != "Demo Racer")
    assert older.summary["drop_ins"] is None                       # a capture with no folder check
    v = views.compare_view(racer, older)
    rows = {r["name"]: r for r in v["conditions"]}
    for name in ("Game's exe file", "Drop-in files", "Unreal settings files"):
        assert rows[name]["b"] == "-" and rows[name]["a"] != "-" and rows[name]["differs"] is False
    assert v["note"] == views.OLDER_FILES_NOTE


def _files(*rows, state=summary.CHECKED, more=0):
    return {"state": state, "why": None, "files": [list(r) for r in rows], "more": more}


DLL = ("dxgi.dll", "a" * 16, 2048, "2026-11-01T10:00:00+00:00")


@pytest.mark.parametrize("a, b, differs", [
    (_files(DLL), _files(DLL), False),
    (_files(DLL), _files(("DXGI.DLL",) + DLL[1:]), False),                        # Windows ignores the case
    (_files(DLL), _files(DLL[:1] + ("b" * 16,) + DLL[2:]), True),                 # another copy, same size and date
    (_files(DLL), _files(DLL[:1] + (None,) + DLL[2:]), False),                    # one not hashed: size and date
    (_files(DLL), _files(DLL[:1] + (None, 2049) + DLL[3:]), True),
    (_files(DLL[:1] + (None,) + DLL[2:]), _files(DLL[:3] + ("2026-11-02T10:00:00+00:00",)), True),
    (_files(DLL, DLL), _files(DLL), True),                                        # one copy more
    (_files(DLL, more=1), _files(DLL), True),
    (_files(), _files(DLL), True),
    (_files(state=summary.NOT_UNREAL), _files(state=summary.NOT_UNREAL), False),
    (_files(state=summary.NOT_CHECKED), _files(DLL), False),                      # unread: no difference claimed
    (None, _files(DLL), False),
])
def test_two_lists_of_files_differ_unless_each_file_has_its_match(a, b, differs):
    assert views.files_row("Drop-in files", a, b, "none found")["differs"] is differs
    assert views.files_row("Drop-in files", b, a, "none found")["differs"] is differs


def test_a_files_row_says_what_it_could_not_read_and_what_did_not_fit():
    unread = {"state": summary.NOT_CHECKED, "why": "C:\\Games\\scripts could not be read: Access is denied",
              "files": [], "more": 0}
    row = views.files_row("Drop-in files", unread, _files(DLL, more=2), "none found")
    assert row["a"] == "not read: C:\\Games\\scripts could not be read: Access is denied"
    assert row["b"] == "dxgi.dll, and 2 more"
    assert views.files_row("Unreal settings files", _files(state=summary.NOT_UNREAL), None, "none found")["a"] == \
        views.NOT_UNREAL_TEXT
    assert views.exe_row({"state": summary.NOT_CHECKED, "why": None, "bytes": None, "modified": None},
                         None)["a"] == "not read"
    exe = {"state": summary.CHECKED, "why": None, "bytes": 100, "modified": "2026-11-01T10:00:00+00:00"}
    assert views.exe_row(exe, dict(exe))["differs"] is False
    assert views.exe_row(exe, dict(exe, modified="2026-11-02T10:00:00+00:00"))["differs"] is True   # an update
    assert views.exe_row(exe, dict(exe, bytes=101))["differs"] is True


# A capture's inventory as the folder check leaves it (capture/gamefiles.py, capture/inventory.py).
SHA = "0123456789abcdef" * 4


def _inv(**game) -> dict:
    base = {"exe": "D:\\Games\\Racer\\Racer\\Binaries\\Win64\\Racer.exe", "exe_bytes": 81_234_567,
            "exe_modified": "2026-10-30T09:12:44-07:00",
            "drop_ins": {"folder": "D:\\Games\\Racer\\Racer\\Binaries\\Win64", "more": 0, "files": [
                {"path": "D:\\Games\\Racer\\Racer\\Binaries\\Win64\\dxgi.dll", "name": "dxgi.dll", "bytes": 1_843_200,
                 "modified": "2026-11-02T18:00:00-07:00", "sha256": SHA, "why": None}]},
            "unreal": {"found": True, "project": "Racer", "looked_in": [], "config_more": 0, "mods_more": 1,
                       "config": [{"path": "C:\\Users\\Jo Tester\\AppData\\Local\\Racer\\Saved\\Config\\Windows\\"
                                           "Engine.ini", "name": "Engine.ini", "bytes": 412,
                                   "modified": "2026-10-30T09:20:01-07:00", "sha256": None, "why": "out of time"}],
                       "mods": [{"path": "x", "name": "~mods\\Sky_P.pak", "bytes": 40 << 20, "modified": None,
                                 "sha256": None, "why": "larger than 32 MB"}]}}
    base.update(game)
    return {"game": base, "errors": {}}


def test_the_summary_keeps_each_game_file_by_name_short_hash_size_and_date():
    out = summary.game_files(_inv())
    assert out["game_exe"] == {"state": "checked", "why": None, "bytes": 81_234_567,
                               "modified": "2026-10-30T09:12:44-07:00"}       # a size charts.number would refuse
    assert out["drop_ins"] == {"state": "checked", "why": None, "more": 1, "files": [
        ["dxgi.dll", SHA[:16], 1_843_200, "2026-11-02T18:00:00-07:00"], ["~mods\\Sky_P.pak", None, 40 << 20, None]]}
    assert out["unreal_config"] == {"state": "checked", "why": None, "more": 0, "files": [
        ["Engine.ini", None, 412, "2026-10-30T09:20:01-07:00"]]}


def test_the_summary_tells_an_unread_folder_from_an_older_capture_and_from_another_engine():
    assert summary.game_files({"game": {"exe": "D:\\x.exe", "drive": "D:"}}) == dict.fromkeys(summary.GAME_FILE_KEYS)
    inv = _inv(drop_ins=None, unreal=None, exe_bytes=None)
    inv["errors"] = {"game.drop_ins": "OSError: D:\\Games could not be read: Access is denied",
                     "game.unreal": "OSError: Windows did not say where this account's AppData and Documents "
                                    "folders are (failed)"}
    out = summary.game_files(inv)
    assert out["drop_ins"] == {"state": "not_checked", "why": "D:\\Games could not be read: Access is denied",
                               "files": [], "more": 0}
    assert out["unreal_config"]["state"] == "not_checked" and out["unreal_config"]["why"].startswith("Windows did")
    assert out["game_exe"] == {"state": "not_checked", "why": None, "bytes": None, "modified": None}
    no_exe = summary.game_files(_inv(exe=None, drop_ins=None, unreal=None))
    assert no_exe["drop_ins"]["why"] == no_exe["unreal_config"]["why"] == labels.NO_EXE
    other = summary.game_files(_inv(unreal={"found": False, "project": None, "looked_in": [], "config": [],
                                            "config_more": 0, "mods": [], "mods_more": 0}))
    assert other["unreal_config"]["state"] == "not_unreal" and [f[0] for f in other["drop_ins"]["files"]] == \
        ["dxgi.dll"]


def test_the_summary_keeps_at_most_its_listed_files_and_counts_the_rest():
    many = [{"name": f"f{i}.asi", "bytes": 1, "modified": None, "sha256": None} for i in range(summary.MAX_LISTED + 3)]
    out = summary.game_files(_inv(drop_ins={"files": many, "more": 2}, unreal=None))
    assert len(out["drop_ins"]["files"]) == summary.MAX_LISTED and out["drop_ins"]["more"] == 5


def test_the_game_files_read_back_and_a_damaged_field_reads_as_missing(tmp_path):
    good = summary.game_files(_inv())
    base = {"schema": summary.SCHEMA, "kind": "watch"}
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps({**base, **good}), encoding="utf-8")
    back = summary.read(tmp_path)
    assert {k: back[k] for k in summary.GAME_FILE_KEYS} == good
    for bad in ({"state": "checked", "why": None, "files": "dxgi.dll", "more": 0},
                {"state": "fine", "why": None, "files": [], "more": 0},
                {"state": "checked", "why": None, "files": [["dxgi.dll", None, 1]], "more": 0},
                {"state": "checked", "why": None, "files": [[5, None, 1, None]], "more": 0},
                {"state": "checked", "why": None, "files": [["a", None, 1, None]] * (summary.MAX_LISTED + 1),
                 "more": 0}):
        (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps({**base, "drop_ins": bad}), encoding="utf-8")
        assert summary.read(tmp_path)["drop_ins"] is None, bad
    (tmp_path / summary.SUMMARY_NAME).write_text(json.dumps(base), encoding="utf-8")       # from before the check
    assert all(summary.read(tmp_path)[k] is None for k in summary.GAME_FILE_KEYS)


def test_compare_says_when_an_older_recording_has_no_conditions(demo):
    home, _ = demo
    old = next(e for e in entries_of(home) if e.summary is None)
    v = views.compare_view(old, old)
    assert v["note"] and v["conditions"][0]["a"] == "-"


def test_readiness_says_whether_frame_timing_works_and_whether_windows_will_ask():
    # The app's recordings always ask (they run with --elevate): one in Performance Log Users, for the tracker alone.
    assert views.readiness_view(True, "", "now")["text"].endswith("asks Windows once, for the game-server check")
    assert views.readiness_view(True, "", "no")["text"].endswith("each recording asks Windows once")
    assert views.readiness_view(True, "", "unknown")["text"].endswith("each recording asks Windows once")
    off = views.readiness_view(False, "PresentMon 2.6.0 is not installed", "now")
    assert not off["ok"] and "lagdoctor setup" in off["text"] and off["detail"].startswith("PresentMon")


# ------------------------------------------------------------------------------------- diagnosis.json --

def test_a_saved_diagnosis_reads_back_whole():
    s = synth.laglab("hitch", duration_s=30.0)
    d = diagnose(s.cap)
    back = Diagnosis.from_json(json.loads(json.dumps(d.to_json())))
    assert back.to_json() == d.to_json()
    assert isinstance(back.findings[0], Finding) and isinstance(back.events[0], LagEvent)
    assert all(isinstance(x, Evidence) for x in back.findings[0].evidence)
    assert all(isinstance(x, FixSuggestion) for f in back.findings for x in f.fixes)


def test_a_newer_diagnosis_keeps_what_this_build_knows():
    d = Diagnosis.from_json({"lag_type": "none", "headline": "ok", "new_field": 1,
                             "events": [{"id": 1, "kind": "stutter", "t_start": 1.0, "t_end": 1.1, "severity": 80,
                                         "later": True}]})
    assert d.events[0].severity == 80


@pytest.mark.parametrize("data", [None, [], {"headline": "x"}, {"lag_type": "none"}, {"lag_type": 1, "headline": "x"},
                                  {"lag_type": "none", "headline": "x", "events": {}},
                                  {"lag_type": "none", "headline": "x", "events": [1]},
                                  {"lag_type": "none", "headline": "x", "events": [{"id": 1}]},
                                  {"lag_type": "none", "headline": "x", "stats": []},
                                  {"lag_type": "none", "headline": "x", "findings": [{"cause": "a", "title": "t",
                                   "category": "fps", "confidence": "high", "score": 1, "summary": "s",
                                   "evidence": "e"}]}])
def test_anything_else_is_no_diagnosis(data):
    with pytest.raises(ValueError):
        Diagnosis.from_json(data)


# ------------------------------------------------------------------------------------ analysis.window --

def _cap():
    return synth.capture(frames={"t": [0.5, 1.0, 1.5, 2.0, 2.5], "frame_ms": [5, 6, 7, 8, 9]},
                         system={"t": [1.0, 2.0, float("nan"), 3.0], "cpu_total_pct": [10, 20, 30, 40]},
                         gpu={"t": [0.2, 2.0, "x"], "nv_temp_c": [70]}, network={"t": []},
                         others=[[1.0, [[1, "a.exe", 5.0, 0, 0]]], [2.6, []], "junk", [None, []]],
                         marks=[1.2, "x", 2.0, 9.0])


def test_window_keeps_every_tables_rows_inside_the_span_both_ends_included():
    w = window(_cap(), 1.0, 2.0)
    assert w["t0"] == 1.0 and w["t1"] == 2.0
    assert w["frames"] == {"t": [1.0, 1.5, 2.0], "frame_ms": [6, 7, 8]}
    assert w["system"] == {"t": [1.0, 2.0], "cpu_total_pct": [10, 20]}         # a NaN time is no time
    assert w["gpu"] == {"t": [2.0], "nv_temp_c": [None]}                        # a short column reads as None
    assert w["network"] == {"t": []}
    assert w["others"] == [[1.0, [[1, "a.exe", 5.0, 0, 0]]]] and w["marks"] == [1.2, 2.0]


def test_window_cuts_the_frame_table_it_is_given():
    w = window(_cap(), 0.0, 1.0, frames={"t": [0.9, 1.1], "frame_ms": [3, 4]})
    assert w["frames"] == {"t": [0.9], "frame_ms": [3]}


@pytest.mark.parametrize("t0, t1", [(2.0, 1.0), (float("nan"), 1.0), (0.0, float("inf")), ("0", 1.0), (True, 2.0)])
def test_window_refuses_what_is_not_a_span(t0, t1):
    with pytest.raises(ValueError):
        window(_cap(), t0, t1)


def test_window_changes_nothing_in_the_capture():
    cap = _cap()
    before = json.dumps(cap.to_json(), sort_keys=True)
    window(cap, 0.0, 10.0)["system"]["t"].append(99)
    assert json.dumps(cap.to_json(), sort_keys=True) == before


# ------------------------------------------------------------------------------------------- chart labels --

def test_every_model_column_but_t_is_charted_with_a_label_or_listed_as_not_charted():
    columns = {c for cls in (F, S, G, N) for c in cls.ALL} - {"t"}
    assert set(labels.COLUMNS) | labels.NOT_CHARTED == columns
    assert not set(labels.COLUMNS) & labels.NOT_CHARTED
    for col, (label, unit) in labels.COLUMNS.items():
        assert label and label == label.strip() and isinstance(unit, str), col


def test_a_series_is_named_with_its_unit():
    assert labels.column_text("frame_ms") == "time between frames (ms)"
    assert labels.column_text("game_focused") == "the game in front (1) or not (0)"
    assert labels.column_text("made_up") == "made up"


def test_compare_names_the_better_side_whichever_order_the_two_come_in(demo):
    home, _ = demo
    a, b = (e for e in entries_of(home) if e.game == "Demo Racer")
    forward = {r["name"]: r["better"] for r in views.compare_view(a, b)["numbers"]}
    backward = {r["name"]: r["better"] for r in views.compare_view(b, a)["numbers"]}
    assert forward["Stutters / min"] == "a" and backward["Stutters / min"] == "b"
    assert {k: {"a": "b", "b": "a", None: None}[v] for k, v in forward.items()} == backward
