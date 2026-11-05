"""The shared data model: column names, series alignment, capture files."""
from __future__ import annotations

import gzip
import json
import math

import pytest

from lagdoctor.model import (CAPTURE_SCHEMA, F, G, N, S, Capture, CaptureError, Diagnosis, Evidence, Finding,
                             SeriesBuilder, SourceStatus)


def test_column_sets_have_unique_names_and_a_time_column():
    for cls in (F, S, G, N):
        assert len(set(cls.ALL)) == len(cls.ALL), cls.__name__
        assert cls.ALL[0] == "t"
        declared = {v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str)}
        assert declared == set(cls.ALL), f"{cls.__name__}.ALL is missing {declared - set(cls.ALL)}"


def test_series_builder_keeps_columns_aligned_with_none_for_missing():
    sb = SeriesBuilder(S.ALL)
    sb.add(t=0.0, cpu_total_pct=10.0)
    sb.add(t=0.25, game_cpu_pct=99.0)
    cols = sb.build()
    assert cols["t"] == [0.0, 0.25]
    assert cols["cpu_total_pct"] == [10.0, None]
    assert cols["game_cpu_pct"] == [None, 99.0]
    assert all(len(v) == 2 for v in cols.values())


def test_series_builder_rejects_unknown_columns_and_missing_time():
    sb = SeriesBuilder(S.ALL)
    with pytest.raises(KeyError, match="game_cpu_pc"):
        sb.add(t=0.0, game_cpu_pc=5.0)        # typo of game_cpu_pct
    with pytest.raises(ValueError):
        sb.add(cpu_total_pct=1.0)
    assert len(sb) == 0                       # a rejected row adds nothing


def _capture() -> Capture:
    return Capture(
        started_at="2026-11-04T13:00:00+00:00", clock_origin=1234.5, duration_s=2.0,
        target={"pid": 42, "name": "game.exe"},
        sources={"frames": SourceStatus("ok", tool="PresentMon 2.6.0"), "network": SourceStatus("off")},
        inventory={"os": "Windows 11"},
        frames={"t": [0.0, 0.016], "frame_ms": [16.6, float("nan")]},
        marks=[1.5], tool_version="0.1.0")


def test_capture_round_trips_through_a_gzip_file(tmp_path):
    path = _capture().save(tmp_path / "c.ldcap.json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        raw = json.load(f)
    assert raw["schema"] == CAPTURE_SCHEMA
    assert raw["frames"]["frame_ms"] == [16.6, None]          # NaN stored as null, not invalid JSON
    back = Capture.load(path)
    assert back.target == {"pid": 42, "name": "game.exe"}
    assert back.sources["frames"] == SourceStatus("ok", tool="PresentMon 2.6.0")
    assert back.marks == [1.5]
    assert not list(tmp_path.glob("*.tmp"))                    # the atomic-write temp file is gone


def test_loading_a_file_that_is_not_a_capture_fails_loudly(tmp_path):
    p = tmp_path / "x.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        json.dump({"schema": "something/else"}, f)
    with pytest.raises(ValueError, match="not a lag-doctor capture"):
        Capture.load(p)


def _gz(path, payload) -> None:
    with gzip.open(path, "wb") as f:
        f.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8"))


def test_a_capture_from_a_newer_build_with_extra_fields_still_loads(tmp_path):
    raw = _capture().to_json()
    raw["future_field"] = {"x": 1}
    raw["sources"]["frames"]["confidence"] = 0.9
    raw["tool_version"] = "0.2.0\x1b[2J"                      # text from a file: shown only as safe characters
    _gz(tmp_path / "c.gz", raw)
    back = Capture.load(tmp_path / "c.gz")
    assert back.target == {"pid": 42, "name": "game.exe"} and back.sources["frames"] == SourceStatus(
        "ok", tool="PresentMon 2.6.0")
    (note,) = [n for n in back.notes if "does not know" in n]
    assert "future_field" in note and "sources.confidence" in note and "(0.2.0??2J)" in note and "\x1b" not in note


def test_a_newer_capture_format_is_refused_as_newer(tmp_path):
    _gz(tmp_path / "c.gz", dict(_capture().to_json(), schema="lagdoctor.capture/2"))
    with pytest.raises(CaptureError, match="newer version") as info:
        Capture.load(tmp_path / "c.gz")
    assert info.value.kind == "newer"


@pytest.mark.parametrize("make", [
    lambda p: p.write_text('{"findings": []}', encoding="utf-8"),             # a diagnosis.json: not gzip at all
    lambda p: _gz(p, [1, 2, 3]),
    lambda p: _gz(p, {"schema": "other.tool/1"}),
    lambda p: _gz(p, {"schema": "lagdoctor.capture/x"}),
    lambda p: _gz(p, {"no": "schema"}),
])
def test_files_that_are_not_captures_say_so(tmp_path, make):
    make(tmp_path / "c.gz")
    with pytest.raises(CaptureError, match="not a lag-doctor capture") as info:
        Capture.load(tmp_path / "c.gz")
    assert info.value.kind == "foreign"


@pytest.mark.parametrize("make,why", [
    (lambda p: p.write_bytes(gzip.compress(json.dumps(_capture().to_json()).encode())[:30]), "cut short"),
    (lambda p: _gz(p, b"{not json"), "not valid JSON"),
    (lambda p: _gz(p, {k: v for k, v in _capture().to_json().items() if k != "started_at"}), "no started_at"),
    (lambda p: _gz(p, dict(_capture().to_json(), target=["a"])), "target is a list"),
    (lambda p: _gz(p, dict(_capture().to_json(), duration_s=True)), "duration_s is a bool"),
    (lambda p: _gz(p, dict(_capture().to_json(), sources={"frames": {"detail": "x"}})), "has no state"),
])
def test_a_damaged_capture_says_damaged(tmp_path, make, why):
    make(tmp_path / "c.gz")
    with pytest.raises(CaptureError, match=why) as info:
        Capture.load(tmp_path / "c.gz")
    assert info.value.kind == "damaged" and "damaged" in str(info.value)


def test_a_capture_that_expands_past_the_limit_is_not_loaded(tmp_path, monkeypatch):
    import lagdoctor.model as model
    cap = _capture()
    cap.notes = ["x" * 5000]
    cap.save(tmp_path / "c.gz")
    monkeypatch.setattr(model, "MAX_CAPTURE_JSON_BYTES", 4096)
    with pytest.raises(CaptureError, match="more than") as info:
        Capture.load(tmp_path / "c.gz")
    assert info.value.kind == "too_large"


def test_a_save_that_fails_leaves_no_half_written_file(tmp_path, monkeypatch):
    import lagdoctor.model as model

    def broken(*a, **k):
        raise OSError(28, "There is not enough space on the disk")
    monkeypatch.setattr(model.json, "dump", broken)
    with pytest.raises(OSError):
        _capture().save(tmp_path / "c.ldcap.json.gz")
    assert list(tmp_path.iterdir()) == []


def test_diagnosis_json_has_no_nan():
    d = Diagnosis(lag_type="stutter", headline="x", stats={"p99": math.inf},
                  findings=[Finding(cause="c", title="t", category="stutter", confidence="low", score=0.1,
                                    summary="s", evidence=[Evidence("e", value=math.nan)])])
    text = json.dumps(d.to_json(), allow_nan=False)            # raises if any NaN/inf survived
    assert '"p99": null' in text
