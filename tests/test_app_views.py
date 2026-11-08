"""The app's data side in plain pytest: the session summary, the library's listing, the view-models, the samples
around a moment (analysis.window) and the chart labels."""
from __future__ import annotations

import array
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
    assert names == ["Power plan and mode", "Refresh rate", "Frames timed", "Measured length"]
    assert v["note"] == ""


def test_compare_says_when_an_older_recording_has_no_conditions(demo):
    home, _ = demo
    old = next(e for e in entries_of(home) if e.summary is None)
    v = views.compare_view(old, old)
    assert v["note"] and v["conditions"][0]["a"] == "-"


def test_readiness_says_whether_frame_timing_works_and_whether_windows_will_ask():
    assert views.readiness_view(True, "", "now")["text"].endswith("no permission prompt")
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
