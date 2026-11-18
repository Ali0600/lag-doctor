"""The laptop's fans (ASUS): the read-only reader (winapi/atkacpi.py), its collector (capture/fanmon.py), and how the
readings travel through the capture, the measured part, the analysis, the report and the app.

No test here opens the real device: tests/conftest.py fails any that reaches atkacpi._open, and the reader's calls go
to stand-ins. tests/test_fans_live.py reads the real fans, on an ASUS laptop only.
"""
from __future__ import annotations

import ast
import ctypes
import dataclasses
import re
import struct
import threading
import time
from pathlib import Path

import pytest

from lagdoctor.analysis import attribute, crop, window
from lagdoctor.analysis.diagnose import diagnose
from lagdoctor.app import views
from lagdoctor.capture import fanmon
from lagdoctor.capture.session import SessionOptions
from lagdoctor.model import C, COLUMN_CLASSES, Capture, SeriesBuilder, SourceStatus
from lagdoctor.report import html, labels, summary
from lagdoctor.winapi import atkacpi
from tests import synth
from tests.test_analysis_rules import GPU_BOUND, heat, is_finding, nv_cap, run
from tests.test_app_views import demo, entries_of  # noqa: F401 - demo is a fixture

ROOT = Path(__file__).resolve().parents[1]
DEVS = 0x53564544                    # the firmware's set method, which lag-doctor never sends


def fan_table(seconds: float, start: float = 1.0, **cols) -> dict[str, list]:
    """C rows once a second from `start` to `seconds`; each column a constant or f(t)."""
    b = SeriesBuilder(C.ALL)
    k = start
    while k <= seconds:
        b.add(t=float(k), **{c: (v(float(k)) if callable(v) else v) for c, v in cols.items()})
        k += 1.0
    return b.build()


# ------------------------------------------------------------------------------------ the read-only reader ---

def test_a_fan_read_is_dsts_with_the_fans_id_in_sixteen_bytes():
    assert atkacpi.dsts_buffer("cpu") == bytes.fromhex("44535453" "08000000" "13001100" "00000000")
    assert atkacpi.dsts_buffer("gpu") == bytes.fromhex("44535453" "08000000" "14001100" "00000000")
    assert atkacpi.dsts_buffer("mid") == bytes.fromhex("44535453" "08000000" "31001100" "00000000")
    for fan in atkacpi.FAN_IDS:
        assert atkacpi.dsts_buffer(fan)[:4] == b"DSTS" and atkacpi.is_fan_read(atkacpi.dsts_buffer(fan))
    with pytest.raises(KeyError):
        atkacpi.dsts_buffer("keyboard")


@pytest.mark.parametrize("buf", [
    struct.pack("<IIII", DEVS, 8, 0x00110013, 0),            # a set on the CPU fan (it could pin it at full speed)
    struct.pack("<IIII", DEVS, 8, 0x00110013, 1),
    struct.pack("<IIII", atkacpi.DSTS, 8, 0x00120075, 0),    # a read, but of the performance mode, not a fan
    struct.pack("<IIII", atkacpi.DSTS, 8, 0x00110024, 0),    # a fan curve's id
    struct.pack("<IIII", atkacpi.DSTS, 8, 0x00110013, 1),    # a second argument
    struct.pack("<IIII", atkacpi.DSTS, 12, 0x00110013, 0),
    struct.pack("<III", atkacpi.DSTS, 8, 0x00110013),        # too short
    struct.pack("<IIIII", atkacpi.DSTS, 8, 0x00110013, 0, 0),
    "DSTS", None,
])
def test_anything_but_a_fan_read_is_refused_before_it_reaches_the_device(monkeypatch, buf):
    sent = []
    monkeypatch.setattr(atkacpi, "_ioctl", lambda *a: sent.append(a) or 1)
    assert not atkacpi.is_fan_read(buf)
    with pytest.raises(ValueError, match="only a DSTS read"):
        atkacpi._call(7, buf)
    assert sent == []


@pytest.mark.parametrize("raw, rpm", [(0x00010023, 3500), (0x00010025, 3700), (0x00010000, 0), (0x00010045, 6900),
                                      (0x00010078, 12_000)])
def test_a_fans_status_is_hundreds_of_rpm_and_zero_is_a_fan_standing_still(raw, rpm):
    assert atkacpi.decode(raw) == rpm


@pytest.mark.parametrize("raw", [0x00010079, 0x0001028F, 0x0001FFFF])
def test_a_speed_past_the_bound_is_a_corrupt_reading_never_a_speed_or_a_missing_fan(raw):
    with pytest.raises(atkacpi.BadReading) as err:
        atkacpi.decode(raw)
    assert isinstance(err.value, OSError) and not isinstance(err.value, atkacpi.NoSuchFan)
    assert atkacpi.MAX_RPM == attribute.FAN_MAX_RPM          # the reader and the analysis hold one bound


@pytest.mark.parametrize("raw", [0x00000000, 0xFFFFFFFE, 0xFFFFFFFF, 0x00020023, 0x00030000, 0x00000023, -2])
def test_a_status_without_the_presence_word_is_no_such_fan(raw):
    with pytest.raises(atkacpi.NoSuchFan):
        atkacpi.decode(raw)


class FakeDevice:
    """Stands in for DeviceIoControl on the ASUS device: answers each fan id from `answers` (a status, or an
    error code to fail with), and keeps every buffer sent."""

    def __init__(self, answers, returned=16):
        self.answers = answers
        self.returned = returned
        self.sent: list[bytes] = []

    def __call__(self, handle, code, inp, n_in, out, n_out, got, overlapped):
        assert code == atkacpi.IOCTL_ATK and n_in == n_out == atkacpi.BUFFER_BYTES and overlapped is None
        buf = bytes(inp.raw[:n_in])
        self.sent.append(buf)
        answer = self.answers[struct.unpack_from("<I", buf, 8)[0]]
        if isinstance(answer, OSError):
            ctypes.set_last_error(answer.winerror)
            return 0
        ctypes.memmove(out, struct.pack("<I", answer & 0xFFFFFFFF), 4)
        got[0] = self.returned
        return 1


def reader(monkeypatch, answers, returned=16, opened=None):
    dev = FakeDevice(answers, returned)
    monkeypatch.setattr(atkacpi, "_ioctl", dev)
    calls = opened if opened is not None else []
    monkeypatch.setattr(atkacpi, "_open", lambda *a: calls.append(a) or 77)
    monkeypatch.setattr(atkacpi, "_close", lambda h: calls.append(("close", h)) or 1)
    return atkacpi.FanReader.open(), dev


CPU, GPU, MID = (atkacpi.FAN_IDS[f] for f in ("cpu", "gpu", "mid"))


def test_the_reader_opens_the_device_with_no_access_rights_and_reads_only(monkeypatch):
    opened = []
    r, dev = reader(monkeypatch, {CPU: 0x00010023, GPU: 0x00010025, MID: OSError(None, "", None, 1)},
                    opened=opened)
    assert opened[0] == (atkacpi.PATH, 0, 0x3, None, 3, 0, None)       # access 0, share read and write
    assert atkacpi.PATH == "\\\\.\\ATKACPI"
    assert (r.read("cpu"), r.read("gpu")) == (3500, 3700)
    with pytest.raises(atkacpi.NoSuchFan):                              # ERROR_INVALID_FUNCTION: no such fan
        r.read("mid")
    with pytest.raises(ValueError):
        r.read("pump")
    assert dev.sent and all(atkacpi.is_fan_read(b) and b[:4] == b"DSTS" for b in dev.sent)
    r.close()
    r.close()
    assert opened[1:] == [("close", 77)]
    with pytest.raises(ValueError, match="closed"):
        r.read("cpu")


def test_a_failed_or_short_call_is_an_error_never_a_reading(monkeypatch):
    r, _ = reader(monkeypatch, {CPU: OSError(None, "", None, 31), GPU: 0x00010025})
    with pytest.raises(OSError) as err:
        r.read("cpu")
    assert err.value.winerror == 31 and not isinstance(err.value, atkacpi.NoSuchFan)
    r, _ = reader(monkeypatch, {CPU: 0x00010023}, returned=2)
    with pytest.raises(OSError, match="too few"):
        r.read("cpu")


def test_no_test_reaches_the_real_device(_no_real_asus_device):
    # tests/conftest.py answers as a PC without the driver, and would fail this test at its end for the reach
    with pytest.raises(OSError) as err:
        atkacpi.FanReader.open()
    assert err.value.winerror == 2 and _no_real_asus_device == ["a test tried to open the real ASUS device"]
    _no_real_asus_device.clear()


def test_the_device_guard_fails_a_test_that_reached_the_device():
    from tests.conftest import asus_device_guard
    mp = pytest.MonkeyPatch()
    try:
        guard = asus_device_guard(mp)
        caught = next(guard)
        with pytest.raises(OSError):
            atkacpi.FanReader.open()
        assert caught == ["a test tried to open the real ASUS device"]
        with pytest.raises(AssertionError, match="a test tried to open the real ASUS device"):
            next(guard)
    finally:
        mp.undo()


def test_a_pc_without_the_asus_driver_cannot_open_it(monkeypatch):
    def refuse(*a):
        ctypes.set_last_error(2)
        return atkacpi.INVALID_HANDLE_VALUE
    monkeypatch.setattr(atkacpi, "_open", refuse)
    with pytest.raises(OSError) as err:
        atkacpi.FanReader.open()
    assert err.value.winerror == 2


def _strings(tree: ast.AST) -> list[str]:
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


# The reader's own ways into the device: another module that names one reaches past _call.
READER_INSIDES = ("_ioctl", "_open", "_close", "_kernel32", "_call", "IOCTL_ATK", "PATH", "dsts_buffer")


def device_mentions(root: Path) -> list[str]:
    """Each module under `root` but winapi/atkacpi.py that names the ASUS device (in any string, any case, any
    separators), its control code, the firmware's set method or one of the reader's insides, and the reader itself
    if it holds the set method's id: the reader is the one way in, and it only reads."""
    found = []
    code = re.compile(r"0x0*22240c|\b2237452\b", re.IGNORECASE)
    set_id = re.compile(r"0x0*53564544|\b1398162756\b", re.IGNORECASE)
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        if rel == "winapi/atkacpi.py":
            if set_id.search(text) or any(b"DEVS" in n.value for n in ast.walk(ast.parse(text))
                                          if isinstance(n, ast.Constant) and isinstance(n.value, bytes)):
                found.append(f"{rel} holds the set method's id")
            continue
        tree = ast.parse(text)
        # a string naming the device: every spelling but the module's own path (winapi/atkacpi.py, atkacpi.FanReader)
        named = [v for v in _strings(tree) if re.search(r"atkacpi(?![._a-z])", v, re.IGNORECASE)
                 or re.search(r"[\\/]atkacpi(?![._a-z])", v, re.IGNORECASE)]
        if named or code.search(text) or "ATKACPI" in text:
            found.append(f"{rel} names the ASUS device")
        if set_id.search(text) or re.search(r"\bDEVS\b", text):
            found.append(f"{rel} names the set method")
        insides = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in READER_INSIDES
                   and isinstance(n.value, ast.Name) and n.value.id == "atkacpi"}
        insides |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.module or "").endswith(
            "atkacpi") for a in n.names if a.name in READER_INSIDES}
        if insides:
            found.append(f"{rel} reaches into the reader ({', '.join(sorted(insides))})")
    return found


def test_only_the_reader_names_the_asus_device_and_nothing_names_the_set_method():
    assert device_mentions(ROOT / "lagdoctor") == []


def test_the_device_scan_finds_a_planted_mention(tmp_path):
    (tmp_path / "winapi").mkdir()
    (tmp_path / "winapi" / "atkacpi.py").write_text('PATH = "ATKACPI"\nIOCTL = 0x0022240C\n', encoding="utf-8")
    plants = {
        "raw.py": 'DEVICE = r"\\\\.\\atkacpi"\n',              # the raw string, lower case
        "escaped.py": 'DEVICE = "\\\\\\\\.\\\\AtkAcpi"\n',      # an escaped string, mixed case
        "slashes.py": 'DEVICE = "//./atkacpi"\n',
        "upper.py": "NAME = 'ATKACPI'\n",
        "code.py": "CODE = 0x0022240c\n",
        "setter.py": "METHOD = 0x53564544\n",
        "words.py": 'SAID = "a DEVS call sets"\n',
        "inside.py": "from lagdoctor.winapi import atkacpi\natkacpi._ioctl(1, 2, 3)\n",
        "fromimport.py": "from lagdoctor.winapi.atkacpi import _call\n",
        "winapi/more.py": "SET = 1398162756\n",
        "fine.py": "from lagdoctor.winapi import atkacpi\nr = atkacpi.FanReader.open()  # winapi/atkacpi.py\n",
    }
    for name, text in plants.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    assert device_mentions(tmp_path) == [
        "code.py names the ASUS device", "escaped.py names the ASUS device",
        "fromimport.py reaches into the reader (_call)", "inside.py reaches into the reader (_ioctl)",
        "raw.py names the ASUS device", "setter.py names the set method", "slashes.py names the ASUS device",
        "upper.py names the ASUS device", "winapi/more.py names the set method", "words.py names the set method"]
    (tmp_path / "winapi" / "atkacpi.py").write_text("DEVS = 0x53564544\n", encoding="utf-8")
    assert "winapi/atkacpi.py holds the set method's id" in device_mentions(tmp_path)
    (tmp_path / "winapi" / "atkacpi.py").write_text("METHOD = b'DEVS'\n", encoding="utf-8")
    assert "winapi/atkacpi.py holds the set method's id" in device_mentions(tmp_path)


def device_reaches(tree: ast.AST) -> list[str]:
    """Where a module reaches the device but through _call: each load of _ioctl outside _call, and each
    DeviceIoControl or _kernel32 inside a function or lambda (at module level they only bind and describe it)."""
    out = []

    def walk(node: ast.AST, func: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else (
                "<lambda>" if isinstance(child, ast.Lambda) else func)
            if isinstance(child, ast.Name) and child.id == "_ioctl" and isinstance(child.ctx, ast.Load) \
                    and func != "_call":
                out.append(f"_ioctl in {func or 'the module'}")
            if func is not None and (isinstance(child, ast.Attribute) and child.attr == "DeviceIoControl"
                                     or isinstance(child, ast.Name) and child.id == "_kernel32"):
                out.append(f"{getattr(child, 'attr', None) or child.id} in {func}")
            walk(child, inner)
    walk(tree, None)
    return out


def test_the_reader_reaches_the_device_only_through_its_guarded_call():
    tree = ast.parse((ROOT / "lagdoctor" / "winapi" / "atkacpi.py").read_text(encoding="utf-8"))
    assert device_reaches(tree) == []
    call = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "_call")
    first = next(s for s in call.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)))
    assert isinstance(first, ast.If) and "is_fan_read" in ast.unparse(first.test)       # the check comes first
    assert sum(1 for n in ast.walk(call) if isinstance(n, ast.Name) and n.id == "_ioctl") == 1


@pytest.mark.parametrize("planted, said", [
    ("def poke(h, b):\n    return _kernel32.DeviceIoControl(h, 1, b, 16, None, 0, None, None)\n",
     ["DeviceIoControl in poke", "_kernel32 in poke"]),
    ("poke = lambda h, b: _ioctl(h, IOCTL_ATK, b, 16, None, 0, None, None)\n", ["_ioctl in <lambda>"]),
    ("send = _ioctl\n", ["_ioctl in the module"]),
    ("def again(h, b):\n    return _ioctl(h, 1, b, 16, None, 0, None, None)\n", ["_ioctl in again"]),
])
def test_the_guarded_call_check_finds_each_way_around_it(planted, said):
    text = (ROOT / "lagdoctor" / "winapi" / "atkacpi.py").read_text(encoding="utf-8") + "\n" + planted
    assert device_reaches(ast.parse(text)) == said


# ------------------------------------------------------------------------------------------- the collector ---

class FakeReader:
    """A FanReader stand-in: `script[fan]` gives each read's answer in turn (an int, or an exception to raise),
    repeating the last."""

    def __init__(self, script):
        self.script = {f: list(v) for f, v in script.items()}
        self.reads = {f: 0 for f in script}
        self.closed = 0

    def read(self, fan):
        k = self.reads[fan]
        self.reads[fan] += 1
        answers = self.script[fan]
        answer = answers[min(k, len(answers) - 1)]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        self.closed += 1


def sample(script, ticks=4, period=0.02):
    r = FakeReader(script)
    c = fanmon.FanSampler(reader_factory=lambda: r, period_s=period)
    done = threading.Event()
    real_add = c.series.add

    def add(**row):
        real_add(**row)
        if len(c.series) >= ticks:
            c._stop_event.set()
            done.set()
    c.series.add = add
    c.start(0.0)
    deadline = time.monotonic() + 5.0
    while not done.wait(0.01) and not c.ended:
        assert time.monotonic() < deadline, "the fan sampler wrote too few rows"
    c.stop()
    return c, r


NONE = atkacpi.NoSuchFan("no such fan")


def test_two_fans_are_read_each_second_and_a_fan_the_laptop_lacks_is_asked_once():
    c, r = sample({"cpu": [3500], "gpu": [3700, 3800], "mid": [NONE]})
    res = c.result()
    cols = res["columns"]
    assert cols[C.CPU_FAN_RPM][:4] == [3500] * 4 and cols[C.GPU_FAN_RPM][:4] == [3700, 3800, 3800, 3800]
    assert all(v is None for v in cols[C.MID_FAN_RPM])
    assert r.reads["mid"] == 1 and r.reads["cpu"] >= 4 and r.closed == 1
    assert res["device"] == {"fans": ["cpu", "gpu"]}
    assert res["cost"]["median_ms"] >= 0 and res["cost"]["max_ms"] >= res["cost"]["median_ms"]
    st = c.status()
    assert st.state == "ok" and st.tool == fanmon.TOOL
    assert st.detail.startswith("CPU fan and GPU fan, ") and "ms a reading" in st.detail


def test_a_failed_reading_is_none_for_that_second_and_a_still_fan_reads_zero():
    c, r = sample({"cpu": [0], "gpu": [3700, OSError(31, "busy"), 3600], "mid": [NONE]})
    cols = c.result()["columns"]
    assert cols[C.CPU_FAN_RPM][:4] == [0, 0, 0, 0]                    # standing still is a reading, not a gap
    assert cols[C.GPU_FAN_RPM][:3] == [3700, None, 3600]
    st = c.status()
    assert st.state == "partial" and st.detail.endswith("; 1 fan reading failed")


def test_a_fan_that_vanishes_after_answering_is_a_failed_reading_not_a_missing_fan():
    c, r = sample({"cpu": [3500], "gpu": [3700, NONE], "mid": [NONE]}, ticks=3)
    assert c.result()["columns"][C.GPU_FAN_RPM][:3] == [3700, None, None]
    assert c.result()["device"] == {"fans": ["cpu", "gpu"]} and r.reads["gpu"] >= 3
    assert c.status().state == "partial"


def test_a_fan_whose_first_reads_fail_joins_when_it_answers():
    c, _ = sample({"cpu": [OSError(31, "busy"), 3500], "gpu": [NONE], "mid": [NONE]}, ticks=3)
    assert c.result()["columns"][C.CPU_FAN_RPM][:3] == [None, 3500, 3500]
    assert c.result()["device"] == {"fans": ["cpu"]} and c.status().state == "partial"
    assert c.status().detail.startswith("CPU fan, ")


def test_a_corrupt_reading_is_a_failed_reading():
    c, _ = sample({"cpu": [3500, atkacpi.BadReading("6,553,500 RPM"), 3500], "gpu": [NONE], "mid": [NONE]}, ticks=3)
    assert c.result()["columns"][C.CPU_FAN_RPM][:3] == [3500, None, 3500]
    assert c.status().state == "partial" and c.status().detail.endswith("; 1 fan reading failed")


def test_a_fan_that_never_answers_is_given_up_without_spoiling_the_others():
    # an absent fan whose id errors with something else than "no such fan": tried UNKNOWN_TRIES times, never counted
    busy = OSError(None, "busy", None, 31)
    c, r = sample({"cpu": [3500], "gpu": [3700], "mid": [busy]}, ticks=fanmon.UNKNOWN_TRIES + 3)
    assert r.reads["mid"] == fanmon.UNKNOWN_TRIES and r.reads["cpu"] >= fanmon.UNKNOWN_TRIES + 3
    st = c.status()
    assert st.state == "ok" and st.detail.endswith("; the middle fan never answered"), st
    assert c.result()["device"] == {"fans": ["cpu", "gpu"]}


def test_a_device_whose_every_reading_fails_is_failed_with_the_last_error():
    busy = OSError(None, "busy", None, 31)
    c, r = sample({"cpu": [busy], "gpu": [busy], "mid": [NONE]}, ticks=fanmon.UNKNOWN_TRIES + 5)
    n = fanmon.UNKNOWN_TRIES
    assert len(c.result()["columns"][C.T]) == n and r.reads["cpu"] == n and c.result()["device"] is None
    assert c.status() == SourceStatus("failed", f"the ASUS System Control Interface answered no fan reading in {n} "
                                                f"tries (the last error: {busy})", fanmon.TOOL)


def test_a_recording_stopped_before_the_first_reading_is_partial():
    gate = threading.Event()

    def slow():
        gate.wait(5.0)
        return FakeReader({"cpu": [3500], "gpu": [3700], "mid": [NONE]})
    c = fanmon.FanSampler(reader_factory=slow, period_s=0.02)
    c.start(0.0)
    c._stop_event.set()
    gate.set()
    c.stop()
    assert c.status() == SourceStatus("partial", "stopped before the first fan reading", fanmon.TOOL)


@pytest.mark.parametrize("code, said", [(2, fanmon.NO_DEVICE), (3, fanmon.NO_DEVICE),
                                        (5, "Windows refused lag-doctor access to the ASUS System Control Interface")])
def test_a_device_that_does_not_open_leaves_the_source_unavailable_with_the_reason(code, said):
    def refuse():
        raise OSError(None, "nope", None, code)
    c = fanmon.FanSampler(reader_factory=refuse)
    c.start(0.0)
    c._thread.join(5.0)
    c.stop()
    assert c.status() == SourceStatus("unavailable", said, fanmon.TOOL)
    assert c.result() == {"columns": {}, "device": None, "cost": None}


def test_a_device_with_no_fans_keeps_no_rows():
    c, r = sample({"cpu": [NONE], "gpu": [NONE], "mid": [NONE]}, ticks=1)
    assert c.status().state == "unavailable" and "reported no fans" in c.status().detail
    assert c.result()["columns"] == {} and c.result()["device"] is None and r.closed == 1


def test_a_reader_that_crashes_is_closed_and_the_source_failed():
    class Boom(FakeReader):
        def read(self, fan):
            raise RuntimeError("boom")
    r = Boom({"cpu": [1], "gpu": [1], "mid": [1]})
    c = fanmon.FanSampler(reader_factory=lambda: r, period_s=0.02)
    c.start(0.0)
    c._thread.join(5.0)
    c.stop()
    assert r.closed == 1 and c.status().state == "failed" and "boom" in c.status().detail


def test_making_the_collector_touches_no_device():
    called = []
    fanmon.FanSampler(reader_factory=lambda: called.append(1))
    fanmon.FanSampler()
    assert called == []
    with pytest.raises(ValueError):
        fanmon.FanSampler(period_s=0)


# --------------------------------------------------------------------------------- the capture and the cut ---

def test_every_column_class_is_listed_once_for_the_tests_that_check_them_all():
    from lagdoctor import model
    classes = [v for v in vars(model).values()
               if isinstance(v, type) and isinstance(getattr(v, "ALL", None), tuple) and v.__module__ == model.__name__]
    assert set(classes) == set(COLUMN_CLASSES) and len(COLUMN_CLASSES) == len(set(COLUMN_CLASSES))


def test_the_session_puts_the_fans_in_the_capture_and_their_names_in_the_inventory():
    from tests.test_session import Fake, full_set, run_session
    fans = Fake("fans", result={"columns": {"t": [0.0], "cpu_fan_rpm": [3500], "gpu_fan_rpm": [3700],
                                            "mid_fan_rpm": [None]},
                                "device": {"fans": ["cpu", "gpu"]}, "cost": {"median_ms": 0.5, "max_ms": 0.9}})
    _, cap = run_session(full_set() + [fans])
    assert cap.fans == {"t": [0.0], "cpu_fan_rpm": [3500], "gpu_fan_rpm": [3700], "mid_fan_rpm": [None]}
    assert cap.inventory["fans"] == {"fans": ["cpu", "gpu"]} and cap.sources["fans"].state == "ok"
    _, cap = run_session(full_set() + [Fake("fans", result={"columns": {}, "device": None})])
    assert cap.fans == {} and "fans" not in cap.inventory


def test_a_real_recording_runs_the_fan_collector_on_any_pc():
    from lagdoctor.capture.session import default_collectors
    names = [c.name for c in default_collectors({"pid": 1, "create_time": 1, "name": "x.exe"}, {"adapters": []},
                                                SessionOptions(network=False, frames=False))]
    assert names == ["system", "fans"]


def test_fans_save_and_load_and_an_older_capture_has_none(tmp_path):
    cap = synth.capture(duration=5.0, fans=fan_table(5.0, cpu_fan_rpm=3500, gpu_fan_rpm=lambda t: 3600 + t))
    back = Capture.load(cap.save(tmp_path / "c.ldcap.json.gz"))
    assert back.fans == cap.fans and back.fans[C.GPU_FAN_RPM][0] == 3601
    data = cap.to_json()
    del data["fans"]
    assert Capture.from_json(data).fans == {}
    data["fans"] = [1, 2]
    with pytest.raises(Exception, match="its fans is a list"):
        Capture.from_json(data)


def test_a_measured_part_keeps_only_its_own_fan_readings():
    cap = synth.capture(synth.frames_table([1000.0 / 60] * 5700), synth.system_table(100.0, cpu_total_pct=10.0),
                        duration=100.0, fans=fan_table(100.0, cpu_fan_rpm=3500, gpu_fan_rpm=lambda t: 1000 + t))
    cap.sources = synth.sources(("frames", "system", "fans"))
    part = crop.crop(cap, 40.0, 80.0)
    assert part.fans[C.T] == [float(t) for t in range(40, 81)] and part.fans[C.GPU_FAN_RPM][0] == 1040
    assert set(part.fans) == set(cap.fans)
    assert window.window(cap, 10.0, 12.0)["fans"][C.T] == [10.0, 11.0, 12.0]
    late = dataclasses.replace(cap, fans=fan_table(30.0, cpu_fan_rpm=3500))
    late.sources = cap.sources
    assert crop.crop(late, 40.0, 80.0).sources["fans"] == SourceStatus("unavailable", crop.EMPTY_IN_SPAN)


# ----------------------------------------------------------------------------------------------- analysis ---

def heated(fans: dict | None, gpu=None) -> Capture:
    cap = nv_cap(gpu or heat())
    if fans is not None:
        cap.fans = fans
        cap.sources["fans"] = SourceStatus("ok")
    return cap


def throttle(cap: Capture):
    return run("gpu_throttle", cap, stats={"avg_fps": 58.0}, bound=GPU_BOUND)


def fan_line(res) -> list[str]:
    return [e.text for e in res.evidence if "fan" in e.text]


def test_a_gpu_held_down_by_heat_says_how_fast_the_fans_ran_meanwhile_and_their_fastest():
    fans = fan_table(30.0, start=0.6, cpu_fan_rpm=3500, gpu_fan_rpm=lambda t: 5800 if t < 3 else 1200)
    res = throttle(heated(fans))
    assert is_finding(res)
    assert res.evidence[2].text == ("While heat held it down, the CPU fan ran at a median 3,500 RPM and the GPU fan "
                                    "at 1,200 RPM; the fastest they ran in the capture was 3,500 and 5,800 RPM.")
    assert res.evidence[1].text.startswith("While heat held it down, it ran at a median 780 MHz")
    assert res.score == throttle(heated(None)).score


def test_without_fan_readings_the_gpu_finding_says_nothing_of_fans():
    res = throttle(heated(None))
    assert is_finding(res) and fan_line(res) == []
    res = throttle(heated(fan_table(30.0, cpu_fan_rpm=None, gpu_fan_rpm=None)))
    assert is_finding(res) and fan_line(res) == []


def test_one_fan_is_named_alone_and_three_in_a_list():
    one = throttle(heated(fan_table(30.0, gpu_fan_rpm=4000)))
    assert fan_line(one) == ["While heat held it down, the GPU fan ran at a median 4,000 RPM; the fastest it ran in "
                             "the capture was 4,000 RPM."]
    three = throttle(heated(fan_table(30.0, cpu_fan_rpm=3000, gpu_fan_rpm=4000, mid_fan_rpm=2000)))
    assert fan_line(three) == ["While heat held it down, the CPU fan ran at a median 3,000 RPM, the GPU fan at 4,000 "
                               "RPM and the middle fan at 2,000 RPM; the fastest they ran in the capture was 3,000, "
                               "4,000 and 2,000 RPM."]


def test_the_fan_median_counts_only_hot_readings_near_a_fan_reading():
    # idle GPU readings (t <= 20, most of them) leave the median; fan readings past FAN_NEAR_S from every GPU
    # reading match none
    fans = fan_table(30.0, gpu_fan_rpm=lambda t: 6000 if t <= 20 else 2000)
    res = throttle(heated(fans, heat(idle=20)))
    assert fan_line(res) == ["While heat held it down, the GPU fan ran at a median 2,000 RPM; the fastest it ran in "
                             "the capture was 6,000 RPM."]
    gpu = heat()                                                   # idle readings that also name heat: still out
    gpu["nv_reasons"] = ["gpu_idle,sw_thermal_slowdown" if t <= 20 else r for t, r in zip(gpu["t"], gpu["nv_reasons"])]
    assert fan_line(throttle(heated(fans, gpu))) == fan_line(res)
    far = fan_table(200.0, start=100.0, gpu_fan_rpm=3000)          # after every GPU reading (1 to 30 s)
    assert fan_line(throttle(heated(far))) == []
    hot_last = heat(idle=29)                                       # one hot reading, at 30 s
    near = fan_table(30.9, start=30.9, gpu_fan_rpm=3000)           # 0.9 s from it: within FAN_NEAR_S
    assert attribute.fans_while(heated(near, hot_last), attribute.heat_named) == [("GPU fan", 3000.0, 3000.0)]
    past = fan_table(31.1, start=31.1, gpu_fan_rpm=3000)           # 1.1 s from it: out of reach
    assert attribute.fans_while(heated(past, hot_last), attribute.heat_named) == []


def test_a_corrupt_fan_reading_is_left_out():
    fans = fan_table(30.0, gpu_fan_rpm=lambda t: 99_999 if t == 5 else (-100 if t == 6 else 3000))
    assert attribute.fan_readings(heated(fans))[C.GPU_FAN_RPM][4:6] == [(7.0, 3000.0), (8.0, 3000.0)]
    assert fan_line(throttle(heated(fans)))[0].endswith("the fastest it ran in the capture was 3,000 RPM.")


def test_a_gpu_at_its_lowered_power_cap_rather_than_held_by_heat_gets_no_fan_line():
    # the fan line speaks of heat held: a GPU at a lowered cap (a quiet mode's 55 W of an 80 W default) gets none,
    # though some readings name heat too (so fans_while finds readings; only "held" keeps the line out)
    gpu = synth.gpu_table(30, nv_util_pct=99.0, nv_throttle_pct=0.0, nv_power_cap_pct=98.0, nv_temp_c=70.0,
                          nv_clock_mhz=1200, nv_clock_max_mhz=2100, nv_power_w=54.8, nv_power_limit_w=55.0,
                          nv_reasons=lambda t: "sw_power_cap,sw_thermal_slowdown" if t <= 3 else "sw_power_cap")
    assert attribute.fans_while(heated(fan_table(30.0, gpu_fan_rpm=3000), gpu), attribute.heat_named)
    res = throttle(heated(fan_table(30.0, gpu_fan_rpm=3000), gpu))
    assert is_finding(res) and res.evidence[1].text.startswith("At its power cap")
    assert fan_line(res) == []


def test_the_diagnosis_keeps_each_fans_median_and_the_summary_carries_it():
    cap = synth.capture(synth.frames_table([1000.0 / 60] * 600), synth.system_table(10.0, cpu_total_pct=10.0),
                        duration=10.0, fans=fan_table(10.0, cpu_fan_rpm=lambda t: 3000 + 100 * t,
                                                      gpu_fan_rpm=lambda t: 0 if t <= 5 else 3000))
    cap.sources = synth.sources(("frames", "system", "fans"))
    d = diagnose(cap)
    assert (d.stats["fan_cpu_rpm"], d.stats["fan_gpu_rpm"]) == (3550.0, 1500.0)    # a still fan's 0 counts
    assert attribute.fan_readings(cap)[C.GPU_FAN_RPM][0] == (1.0, 0.0)
    assert attribute.fan_stats(synth.capture()) == {"fan_cpu_rpm": None, "fan_gpu_rpm": None}
    fields = summary.headroom_fields(d.stats)
    assert (fields["fan_cpu_rpm"], fields["fan_gpu_rpm"]) == (3550.0, 1500.0)
    assert summary.headroom_fields({"fan_gpu_rpm": None})["fan_gpu_rpm"] is None
    assert summary.headroom_fields({})["fan_cpu_rpm"] is None                  # a summary from before the fans


# --------------------------------------------------------------------------------------- report and app ---

def test_the_report_draws_the_fans_and_says_why_when_they_were_not_measured():
    cap = heated(fan_table(30.0, cpu_fan_rpm=3500, gpu_fan_rpm=1200))
    cap.frames = synth.frames_table([1000.0 / 60] * 1700)
    d = diagnose(cap)
    page = html.render(d, cap)
    fans = page[page.index("<h2>Fans</h2>"):page.index("<h2>Video memory</h2>")]
    assert "<svg" in fans and "CPU fan" in fans and "GPU fan" in fans and "middle fan" not in fans
    assert "Fan speeds (RPM)" in fans and not re.search(r"\d RPM</text>", fans)    # no tick wider than its margin
    damaged = heated(fan_table(30.0, cpu_fan_rpm=lambda t: 6_553_500 if t == 9 else 3500))
    damaged.frames = cap.frames
    # the y ticks print as {:g}: 2000 or, at millions, 2e+06
    tops = [float(x) for x in re.findall(r">(\d[\d.]*(?:e\+\d+)?)</text>", html._fan_chart(diagnose(damaged), damaged))]
    assert tops and max(tops) <= attribute.FAN_MAX_RPM * 1.1        # one corrupt reading never sets the scale
    assert float("2e+06") in [float(x) for x in re.findall(r">(\d[\d.]*(?:e\+\d+)?)</text>", ">2e+06</text>")]
    cap = heated(None)
    cap.frames = synth.frames_table([1000.0 / 60] * 1700)
    cap.sources["fans"] = SourceStatus("unavailable", fanmon.NO_DEVICE, fanmon.TOOL)
    page = html.render(diagnose(cap), cap)
    fans = page[page.index("<h2>Fans</h2>"):page.index("<h2>Video memory</h2>")]
    assert "<svg" not in fans and f"Not measured: {fanmon.NO_DEVICE}" in fans
    assert ("fan speeds (ASUS)", fanmon.NO_DEVICE) in labels.not_measured(cap.sources, {})


def test_results_chart_the_fans_and_say_why_they_are_missing():
    spec = next(s for s in views.CHARTS if s[0] == "fans")
    assert spec == ("fans", "Fan speeds", "RPM", (C.CPU_FAN_RPM, C.GPU_FAN_RPM, C.MID_FAN_RPM), "fans")
    cap = heated(fan_table(5.0, cpu_fan_rpm=3500, gpu_fan_rpm=3700))
    chart = views._chart(cap, spec)
    assert [s["label"] for s in chart["series"]] == ["CPU fan (RPM)", "GPU fan (RPM)"]
    assert chart["t"] == [1.0, 2.0, 3.0, 4.0, 5.0] and chart["series"][0]["values"] == [3500.0] * 5
    cap = heated(None)
    cap.sources["fans"] = SourceStatus("unavailable", fanmon.NO_DEVICE, fanmon.TOOL)
    assert views._chart(cap, spec)["missing"] == f"Not measured: {fanmon.NO_DEVICE}"


def test_compare_puts_each_recordings_fans_side_by_side(demo):  # noqa: F811 - the fixture
    home, _ = demo
    a, b = (e for e in entries_of(home) if e.game == "Demo Racer")
    a = dataclasses.replace(a, summary=dict(a.summary, fan_cpu_rpm=3500.0, fan_gpu_rpm=5800.0))
    b = dataclasses.replace(b, summary=dict(b.summary, fan_cpu_rpm=None, fan_gpu_rpm=1200.0))
    rows = {r["name"]: r for r in views.compare_view(a, b)["numbers"]}
    assert rows["Fans (median)"] == {"name": "Fans (median)", "a": "CPU 3,500 RPM, GPU 5,800 RPM",
                                     "b": "GPU 1,200 RPM", "better": None}
    old = dataclasses.replace(a, summary={k: v for k, v in a.summary.items() if not k.startswith("fan_")})
    assert {r["name"]: r for r in views.compare_view(old, b)["numbers"]}["Fans (median)"]["a"] == "-"


def test_the_fans_text_a_player_reads_is_plain_and_their_columns_are_labelled():
    for col in C.ALL[1:]:
        assert col in labels.COLUMNS
    assert labels.source_text("fans") == "fan speeds (ASUS)"
    for text in (fanmon.NO_DEVICE, fanmon.open_reason(OSError(None, "", None, 5)), fanmon.TOOL):
        assert text.isascii() and "ATKACPI" not in text and "DSTS" not in text
    assert labels.COLUMNS["gpu_fan_rpm"] == ("GPU fan", "RPM")
