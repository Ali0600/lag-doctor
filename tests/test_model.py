"""The shared data model: column names, series alignment, capture files."""
from __future__ import annotations

import gzip
import json
import math
import random
import threading
import time
from dataclasses import asdict

import pytest

from lagdoctor.model import (CAPTURE_SCHEMA, NOTE_CUT, NOTE_MAX, F, G, N, S, Capture, CaptureError, Diagnosis,
                             Evidence, Finding, SeriesBuilder, SourceStatus, clean_note)


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


@pytest.mark.parametrize("raw, kept", [
    ("installed FFVIIHook", "installed FFVIIHook"),
    ("  shadows\tto\r\nlow \x1b[2J ", "shadows to low [2J"),       # control characters are spaces, runs of space one
    ("DLSS" + chr(0x202E) + "on" + chr(0xD800), "DLSS on"),           # a mark that reorders text, a lone surrogate
    ("caf" + chr(0xE9) + " " + chr(0x5149), "caf" + chr(0xE9) + " " + chr(0x5149)),   # letters of any script stay
    ("", None), (" \x07\x1b\t\n ", None), (None, None), (5, None), (True, None), (["x"], None),
])
def test_a_note_is_kept_as_one_line_of_printable_text_and_an_empty_one_is_none(raw, kept):
    assert clean_note(raw) == kept and clean_note(raw, limit=None) == kept


def test_a_note_past_its_limit_is_cut_where_one_is_given_and_kept_whole_where_none_is():
    assert NOTE_MAX == 200
    assert clean_note("x" * NOTE_MAX) == "x" * NOTE_MAX                 # exactly the limit: whole
    cut = clean_note("word " * 60)                                      # 299 characters as kept
    assert len(cut) <= NOTE_MAX and cut.endswith(NOTE_CUT) and cut.startswith("word word")
    assert clean_note("y" * 201) == "y" * 197 + NOTE_CUT
    # A cut right after a space: never a space before the cut mark.
    assert clean_note("x" * 196 + " " + "y" * 50) == "x" * 196 + NOTE_CUT
    assert clean_note("z" * 500, limit=None) == "z" * 500


def test_a_capture_keeps_its_note_and_one_from_before_notes_has_none(tmp_path):
    cap = _capture()
    cap.note = "installed FFVIIHook"
    back = Capture.load(cap.save(tmp_path / "c.ldcap.json.gz"))
    assert back.note == "installed FFVIIHook" and not back.notes
    raw = _capture().to_json()
    del raw["note"]                                                     # a capture saved before notes existed
    _gz(tmp_path / "old.gz", raw)
    old = Capture.load(tmp_path / "old.gz")
    assert old.note is None and not old.notes


def test_a_note_read_from_a_file_is_cleaned_and_a_longer_one_cut_never_refused(tmp_path):
    # A newer build may keep a longer note; text a file holds is shown only as safe characters.
    _gz(tmp_path / "c.gz", dict(_capture().to_json(), note="a\x1b]0;x\x07b " + "c" * 300))
    note = Capture.load(tmp_path / "c.gz").note
    assert note.startswith("a ]0;x b ccc") and len(note) == NOTE_MAX and note.endswith(NOTE_CUT)
    _gz(tmp_path / "c.gz", dict(_capture().to_json(), note="   "))
    assert Capture.load(tmp_path / "c.gz").note is None


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
    (lambda p: _gz(p, dict(_capture().to_json(), note=["shadows to low"])), "note is a list"),
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


class _FullDisk:
    """A compressor whose output can never be written: the disk is full."""

    def compress(self, data):
        raise OSError(28, "There is not enough space on the disk")

    def flush(self):
        return b""


@pytest.mark.parametrize("how", ["unencodable", "write", "replace"])
def test_a_save_that_fails_leaves_no_half_written_file(tmp_path, monkeypatch, how):
    # Changed on purpose: it made json.dump fail, which the faster save no longer calls. Now the save fails while
    # encoding, while the worker thread writes (disk full), and at the final rename.
    import lagdoctor.model as model
    cap = _capture()
    if how == "unencodable":
        cap.notes = ["fine", {"a set"}]
    elif how == "write":
        monkeypatch.setattr(model.zlib, "compressobj", lambda *a, **k: _FullDisk())
    else:
        def broken(*a, **k):
            raise OSError(28, "There is not enough space on the disk")
        monkeypatch.setattr(model.os, "replace", broken)
    with pytest.raises(TypeError if how == "unencodable" else OSError):
        cap.save(tmp_path / "c.ldcap.json.gz")
    assert list(tmp_path.iterdir()) == []
    assert not [t for t in threading.enumerate() if t.name.startswith("lagdoctor-save")]     # its worker is gone


# ------------------------------------------------------------------ the fast save (LC-2) ---

def _old_clean(obj):
    """model._clean_floats as it was when every capture was saved through it: the frozen reference."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _old_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_old_clean(v) for v in obj]
    return obj


def _old_save(cap: Capture, path) -> None:
    """Capture.save as it was before (asdict, json.dump, gzip level 9)."""
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(_old_clean(asdict(cap)), f, separators=(",", ":"))


def _canonical(path) -> str:
    """What a file loads as, written back out: floats from their exact doubles, ints as ints, bools as bools."""
    with gzip.open(path, "rb") as f:
        return json.dumps(json.loads(f.read()), separators=(",", ":"))


class _Sub(float):
    pass


def _awkward_capture(seed: int = 3) -> Capture:
    rnd = random.Random(seed)
    awkward = [0.0, -0.0, 5e-324, 1e-7, 4.9999995e-7, 5.0000005e-7, 0.0000005, 1.0000005, 2.675, 0.1 + 0.2,
               123456789.1234565, 1e15 + 0.1, 1e16, 1e22, -1e300, 7.0, -3.5, 16.6]
    many = awkward + [rnd.choice((-1, 1)) * 10 ** rnd.uniform(-12, 15) for _ in range(5000)]
    t, acc = [], 0.0
    for _ in range(3000):
        acc += rnd.gauss(6.944, 0.3) / 1000 + rnd.random() * 1e-7
        t.append(acc)
    return Capture(
        started_at="2026-11-05T12:00:00+01:00", clock_origin=12345.678901234, duration_s=20.123456789,
        target={"pid": 4242, "create_time": 133000000000000001, "name": "Gäme.exe", "tool_pids": [4, 8],
                "presentmon": {"exe": "C:\\pm.exe", "rows": 3}, "scale": 1.23456789},
        sources={"frames": SourceStatus("ok", "fine", "PresentMon"), "gpu": SourceStatus("partial", "nan\x1b")},
        inventory={"nested": SourceStatus("ok"), "displays": [{"hz": 144.003001, "modes_hz": [60, 144]}],
                   "tuple": (1.5, 2.25), "none": None},
        frames={"t": t, "frame_ms": [round(rnd.gauss(6.944, 0.3), 4) for _ in range(2999)] + [None],
                "cpu_busy_ms": [None if i % 3 else rnd.random() * 10 for i in range(3000)],      # gaps everywhere
                "gpu_busy_ms": [math.nan, math.inf, -math.inf] + [rnd.random() for _ in range(100)],
                "display_ms": many, "displayed": [1, 0, None] * 1000, "sync_interval": [0, -1] * 1500,
                "allows_tearing": [True, False, None] * 1000, "present_mode": ["Hardware: Indépendant Flip ✓\x1b",
                                                                              None] * 1500,
                "anim_error_ms": [None, None] + [rnd.gauss(0, 0.5) for _ in range(1100)] + [None] +
                                 [rnd.gauss(0, 0.5) for _ in range(511)] + [None, None],      # runs at the edges
                "hybrid": [1, 2.5, None, 3], "input_latency_ms": [None] * 3000, "frame_type": [],
                "swap_chain": [_Sub(1.23456789), _Sub(2.0)]},
        system={"t": [k * 0.25 for k in range(400)], "cpu_total_pct": [rnd.uniform(0, 100) for _ in range(400)]},
        others=[[k * 0.25 + 1e-9, [[1000 + j, f"p{j}.exe", rnd.uniform(0, 50), rnd.randint(0, 10 ** 6),
                                    (rnd.uniform(0, 30), None), 133000000000000000 + j] for j in range(8)]]
                for k in range(50)],
        net_flows={"server_rx": [[float(k), 64] for k in range(20)], "gaps": [[3.0000004, 300.0, "delay"]],
                   "server": None},
        marks=[1.5, 2.0000006], notes=["a note", "ünïcode"], tool_version="0.1.0")


def test_a_saved_capture_loads_exactly_what_the_old_save_wrote(tmp_path):
    """The fast save writes floats as "%.6f": the file must load as exactly the values (and types) the old save's
    file did, for every column shape, NaN and inf, the edges of the 512-value batches and round()'s halfway cases."""
    cap = _awkward_capture()
    new, old = tmp_path / "new.ldcap.json.gz", tmp_path / "old.ldcap.json.gz"
    cap.save(new)
    _old_save(cap, old)
    assert _canonical(new) == _canonical(old)
    back = Capture.load(new)
    assert json.dumps(back.to_json()) == json.dumps(cap.to_json())          # and to_json() agrees with the file


def test_the_encoder_runs_at_most_a_few_pieces_ahead_of_the_compressor(tmp_path, monkeypatch):
    """Memory stays bounded: with a slow disk, the save waits instead of queueing every encoded column."""
    import lagdoctor.model as model
    submitted, done, leads = [0], [0], []
    real = model.zlib.compressobj

    class CountingPool(model.ThreadPoolExecutor):
        def submit(self, fn, *a, **k):
            submitted[0] += 1
            return super().submit(fn, *a, **k)

    class SlowDisk:
        def __init__(self, *a):
            self.co = real(*a)

        def compress(self, data):
            time.sleep(0.01)
            done[0] += 1
            leads.append(submitted[0] - done[0])
            return self.co.compress(data)

        def flush(self):
            return self.co.flush()
    monkeypatch.setattr(model, "ThreadPoolExecutor", CountingPool)
    monkeypatch.setattr(model.zlib, "compressobj", SlowDisk)
    cap = _awkward_capture()
    path = cap.save(tmp_path / "c.ldcap.json.gz")
    assert len(leads) > 3 * model.SAVE_PIECES_AHEAD and max(leads) <= model.SAVE_PIECES_AHEAD + 1, leads
    assert Capture.load(path).frames["displayed"] == cap.frames["displayed"]         # and the file is whole


def test_the_save_compresses_at_the_fastest_gzip_level(tmp_path):
    path = _capture().save(tmp_path / "c.ldcap.json.gz")
    head = path.read_bytes()[:10]
    assert head[:3] == b"\x1f\x8b\x08" and head[8] == 4                     # gzip XFL 4: fastest (level 1)


def _frames_capture(n: int) -> Capture:
    rnd = random.Random(1)
    t, acc = [], 0.0
    for _ in range(n):
        acc += rnd.gauss(6.944, 0.3) / 1000
        t.append(acc)
    floats = {c: [round(rnd.gauss(6.944, 0.3), 4) for _ in range(n)] for c in
              ("frame_ms", "cpu_busy_ms", "cpu_wait_ms", "gpu_busy_ms", "display_ms", "anim_error_ms")}
    return Capture(started_at="2026-11-05T12:00:00+01:00", clock_origin=1.0, duration_s=n / 144, target={},
                   sources={}, inventory={}, frames={"t": t, **floats, "displayed": [1] * n,
                                                     "present_mode": ["Hardware: Independent Flip"] * n})


def test_the_save_takes_a_fraction_of_the_old_saves_time(tmp_path):
    # Measured against the old save on the same data, the two in turns and each at its best of three, so a slow
    # stretch of a busy PC lands on both alike. With the old save timed once, before the new one's three, it failed
    # once on CI (Python 3.11: 0.35 s against 0.84 s). On a 30-minute 144 FPS capture here: 2.6 s against 36 s.
    cap = _frames_capture(15_000)
    old = new = math.inf
    for _ in range(3):
        t0 = time.perf_counter()
        _old_save(cap, tmp_path / "old.gz")
        old = min(old, time.perf_counter() - t0)
        t0 = time.perf_counter()
        cap.save(tmp_path / "new.gz")
        new = min(new, time.perf_counter() - t0)
    assert new < 0.3 * old, f"the save took {new:.2f} s, the old one {old:.2f} s"


def test_diagnosis_json_has_no_nan():
    d = Diagnosis(lag_type="stutter", headline="x", stats={"p99": math.inf},
                  findings=[Finding(cause="c", title="t", category="stutter", confidence="low", score=0.1,
                                    summary="s", evidence=[Evidence("e", value=math.nan)])])
    text = json.dumps(d.to_json(), allow_nan=False)            # raises if any NaN/inf survived
    assert '"p99": null' in text
