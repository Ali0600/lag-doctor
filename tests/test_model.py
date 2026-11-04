"""The shared data model: column names, series alignment, capture files."""
from __future__ import annotations

import gzip
import json
import math

import pytest

from lagdoctor.model import (CAPTURE_SCHEMA, F, G, N, S, Capture, Diagnosis, Evidence, Finding,
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


def test_diagnosis_json_has_no_nan():
    d = Diagnosis(lag_type="stutter", headline="x", stats={"p99": math.inf},
                  findings=[Finding(cause="c", title="t", category="stutter", confidence="low", score=0.1,
                                    summary="s", evidence=[Evidence("e", value=math.nan)])])
    text = json.dumps(d.to_json(), allow_nan=False)            # raises if any NaN/inf survived
    assert '"p99": null' in text
