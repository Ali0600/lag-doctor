"""winapi/pdh.py: the PDH query wrapper, against this machine's real counters and a synthetic buffer."""
from __future__ import annotations

import ctypes
import struct
import time
from ctypes import wintypes

import psutil
import pytest

from lagdoctor.winapi import pdh
from lagdoctor.winapi.pdh import PdhError, PdhQuery

USER = r"\Processor Information(*)\% User Time"
PERF_TOTAL = r"\Processor Information(_Total)\% Processor Performance"   # explicit instance, rate counter
AVAIL = r"\Memory\Available MBytes"
COMMIT_LIMIT = r"\Memory\Commit Limit"
COMMITTED = r"\Memory\Committed Bytes"
MIB = 1 << 20


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _global_memory() -> MEMORYSTATUSEX:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    assert k32.GlobalMemoryStatusEx(ctypes.byref(ms)), ctypes.get_last_error()
    return ms


def test_rate_counters_read_empty_until_the_second_collect():
    with PdhQuery([USER, PERF_TOTAL, AVAIL]) as q:
        q.collect()
        # WT 1.3: a "(*)" rate path fails with PDH_CSTATUS_INVALID_DATA, an explicit instance with PDH_INVALID_DATA
        assert q.read(USER) == {}
        assert q.last_status[USER] == pdh.PDH_CSTATUS_INVALID_DATA
        assert q.read(PERF_TOTAL) == {}
        assert q.last_status[PERF_TOTAL] == pdh.PDH_INVALID_DATA
        raw = q.read(AVAIL)                      # a raw counter is valid after one collect
        assert set(raw) == {""} and raw[""] > 0
        time.sleep(0.25)
        q.collect()
        per_cpu = q.read(USER)
        assert "_Total" in per_cpu and "0,0" in per_cpu
        assert all(v is None or isinstance(v, float) for v in per_cpu.values())
        assert set(q.read(PERF_TOTAL)) == {"_Total"}


def test_a_missing_set_or_counter_is_recorded_and_reads_empty():
    with PdhQuery([AVAIL, r"\No Such Set Here\X", r"\Memory\No Such Counter Here"]) as q:
        assert q.add_errors == {r"\No Such Set Here\X": pdh.PDH_CSTATUS_NO_OBJECT,
                                r"\Memory\No Such Counter Here": pdh.PDH_CSTATUS_NO_COUNTER}
        q.collect()
        assert q.read(r"\No Such Set Here\X") == {}
        assert q.read(AVAIL)[""] > 0
        with pytest.raises(KeyError):
            q.read(r"\Memory\Pages Input/sec")           # never added


def test_a_query_where_nothing_could_be_added_raises_with_the_status():
    with pytest.raises(PdhError) as info:
        PdhQuery([r"\No Such Set Here\X"])
    assert isinstance(info.value, OSError)
    assert info.value.winerror == pdh.PDH_CSTATUS_NO_OBJECT
    assert "PDH_CSTATUS_NO_OBJECT" in str(info.value)


@pytest.mark.parametrize("bad", [[], "\\Memory\\Available MBytes", ["Memory\\Available MBytes"], [None]])
def test_bad_paths_are_refused(bad):
    with pytest.raises(ValueError):
        PdhQuery(bad)


def test_a_failed_collect_blocks_stale_reads(monkeypatch):
    with PdhQuery([AVAIL]) as q:
        q.collect()
        assert q.read(AVAIL)
        real = pdh._pdh.PdhCollectQueryData
        monkeypatch.setattr(pdh._pdh, "PdhCollectQueryData", lambda h: pdh.PDH_NO_DATA)
        with pytest.raises(PdhError) as info:
            q.collect()
        assert info.value.winerror == pdh.PDH_NO_DATA
        assert q.read(AVAIL) == {}, "values from the previous collect were handed out as fresh"
        monkeypatch.setattr(pdh._pdh, "PdhCollectQueryData", real)
        q.collect()
        assert q.read(AVAIL)


def test_reads_format_as_double_without_the_100_cap(monkeypatch):
    seen = []
    real = pdh._pdh.PdhGetFormattedCounterArrayW

    def spy(hc, fmt, size, count, buf):
        seen.append(fmt)
        return real(hc, fmt, size, count, buf)

    monkeypatch.setattr(pdh._pdh, "PdhGetFormattedCounterArrayW", spy)
    with PdhQuery([AVAIL]) as q:
        q.collect()
        q.read(AVAIL)
    assert seen and all(f == pdh.PDH_FMT_DOUBLE | pdh.PDH_FMT_NOCAP100 for f in seen), [hex(f) for f in seen]


def test_values_above_100_come_through_uncapped():
    """Outcome check for NOCAP100: GPU Engine samples overshoot (WT verify log 13), so do idle disks."""
    paths = [r"\GPU Engine(*)\Utilization Percentage", r"\PhysicalDisk(*)\% Idle Time"]
    with PdhQuery(paths) as q:
        q.collect()
        deadline = time.perf_counter() + 4.0
        best = 0.0
        while time.perf_counter() < deadline:
            time.sleep(0.25)
            q.collect()
            for p in paths:
                best = max([best] + [v for v in q.read(p).values() if v is not None])
            if best > 100.0:
                return
    pytest.skip(f"no counter went above 100 in 4 s (max {best:.1f}); the format flag is still pinned by "
                "test_reads_format_as_double_without_the_100_cap")


def _synthetic(q: PdhQuery, items: list[tuple[int | None, int, float]], names: list[str]) -> int:
    """Lay out PDH_FMT_COUNTERVALUE_ITEM_W items followed by their names in q's buffer, as PDH does."""
    raw = memoryview(q._buf).cast("B")
    base = ctypes.addressof(q._buf)
    names_at = len(items) * 24
    offsets, pos = [], names_at
    for n in names:
        enc = (n + "\0").encode("utf-16-le")
        raw[pos:pos + len(enc)] = enc
        offsets.append(pos)
        pos += len(enc)
    for i, (name_index, status, value) in enumerate(items):
        if name_index is None:
            ptr = base + ctypes.sizeof(q._buf) + 64          # outside the buffer
        elif name_index < 0:
            ptr = base + offsets[-name_index - 1] + 1        # misaligned: points mid-character
        else:
            ptr = base + offsets[name_index]
        struct.pack_into("<QI4xd", raw, i * 24, ptr, status, value)
    return pos


def test_item_statuses_map_to_values_or_none():
    with PdhQuery([AVAIL]) as q:
        used = _synthetic(q, [(0, pdh.PDH_CSTATUS_VALID_DATA, 1.5),
                              (1, pdh.PDH_CSTATUS_INVALID_DATA, 7.0),
                              (2, pdh.PDH_CSTATUS_NEW_DATA, 2.5),
                              (3, pdh.PDH_CALC_NEGATIVE_DENOMINATOR, 9.0)],
                          ["0,0", "0,1", "_Total", "0,2"])
        assert q._parse(4, used) == {"0,0": 1.5, "0,1": None, "_Total": 2.5, "0,2": None}


def test_names_outside_the_buffer_are_dropped_and_repeats_kept():
    with PdhQuery([AVAIL]) as q:
        used = _synthetic(q, [(0, 0, 1.0), (None, 0, 2.0), (-2, 0, 3.0), (0, 0, 4.0), (1, 0, 5.0)],
                          ["svchost", "dwm"])
        assert q._parse(5, used) == {"svchost": 1.0, "svchost#1": 4.0, "dwm": 5.0}


def test_available_memory_and_commit_match_independent_readouts():
    with PdhQuery([AVAIL, COMMIT_LIMIT, COMMITTED]) as q:
        q.collect()
        avail = q.read(AVAIL)[""]
        limit = q.read(COMMIT_LIMIT)[""]
        committed = q.read(COMMITTED)[""]
    ref_avail = psutil.virtual_memory().available / MIB
    ms = _global_memory()
    assert abs(avail - ref_avail) <= max(256, 0.03 * ref_avail), (avail, ref_avail)
    assert abs(limit - ms.ullTotalPageFile) <= 0.01 * ms.ullTotalPageFile, (limit, ms.ullTotalPageFile)
    ref_committed = ms.ullTotalPageFile - ms.ullAvailPageFile
    assert abs(committed - ref_committed) <= 0.03 * ref_committed, (committed, ref_committed)


def test_close_is_idempotent_and_blocks_further_use():
    q = PdhQuery([AVAIL])
    q.close()
    q.close()
    with pytest.raises(ValueError):
        q.collect()
    with pytest.raises(ValueError):
        q.read(AVAIL)
