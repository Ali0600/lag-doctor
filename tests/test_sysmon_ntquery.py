"""winapi/ntquery.py: snapshots of every process without handles, checked against our own process.

psutil is used only on our own process (psutil.Process() with no argument), as the independent readout.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from lagdoctor.winapi import ntquery
from lagdoctor.winapi.ntquery import (SPI_SIZE, STI_SIZE, SYSTEM_PROCESS_INFORMATION, SYSTEM_THREAD_INFORMATION,
                                      ProcInfo, Snapshotter)

EPOCH_AS_FILETIME = 116444736000000000      # 1970-01-01 in 100 ns units since 1601
TICK = 156250                                # thread times move in 15.625 ms steps (WT 3.5)


def _psutil_times(p: psutil.Process) -> tuple[int, int]:
    t = p.cpu_times()
    return round(t.user * 1e7), round(t.system * 1e7)


def test_our_own_process_matches_psutil():
    me = psutil.Process()                     # our own process: no other process is opened
    helper_stop = threading.Event()
    helper = threading.Thread(target=helper_stop.wait, daemon=True)
    helper.start()
    try:
        u0, k0 = _psutil_times(me)
        io0 = me.io_counters()
        procs = Snapshotter().snapshot(threads_for=[os.getpid()])
        u1, k1 = _psutil_times(me)
        io1 = me.io_counters()
        mem = me.memory_info()
        tids = {t.id for t in me.threads()}
    finally:
        helper_stop.set()
        helper.join()
    p = procs[os.getpid()]
    assert p.pid == os.getpid()
    assert p.ppid == me.ppid()
    assert p.name.lower() == os.path.basename(me.exe()).lower()
    assert abs((p.create_time - EPOCH_AS_FILETIME) / 1e7 - me.create_time()) < 0.01
    assert u0 - 1 <= p.user_100ns <= u1 + 1 and k0 - 1 <= p.kernel_100ns <= k1 + 1
    assert io0.read_count <= p.read_ops <= io1.read_count
    assert io0.read_bytes <= p.read_bytes <= io1.read_bytes
    assert io0.write_bytes <= p.write_bytes <= io1.write_bytes
    assert io0.other_count <= p.other_ops <= io1.other_count
    assert abs(p.private_bytes - mem.private) <= 8 << 20, (p.private_bytes, mem.private)
    assert abs(p.ws_bytes - mem.rss) <= 16 << 20, (p.ws_bytes, mem.rss)
    assert p.page_faults <= mem.num_page_faults
    assert p.thread_count == len(p.threads) and {t.tid for t in p.threads} == tids


def test_self_check_passes_on_this_machine():
    ntquery.self_check()
    ntquery.self_check(Snapshotter())


def _readout(**over) -> dict:
    base = {"create": 1000, "user": 50, "kernel": 20, "io": (1, 2, 3, 400, 500, 600), "faults": 70,
            "ws": 30 << 20, "private": 20 << 20, "image": "python.exe", "session": 1, "tid": 77,
            "thread": (1100, 10, 5)}
    base.update(over)
    return base


def _me(**over) -> ProcInfo:
    from lagdoctor.winapi.ntquery import ThreadInfo
    fields = dict(pid=10, ppid=4, create_time=1000, name="python.exe", session_id=1, user_100ns=55,
                  kernel_100ns=21, ws_bytes=31 << 20, private_bytes=20 << 20, page_faults=71, read_bytes=400,
                  write_bytes=500, other_bytes=600, read_ops=1, write_ops=2, other_ops=3, thread_count=1,
                  threads=[ThreadInfo(77, 1100, 11, 5, 8, 2)])
    fields.update(over)
    return ProcInfo(**fields)


def test_compare_own_accepts_values_inside_the_brackets():
    before, after = _readout(), _readout(user=60, kernel=25, faults=75, thread=(1100, 12, 6))
    assert ntquery.compare_own(before, _me(), after) == []


@pytest.mark.parametrize("over, word", [
    ({"create_time": 1001}, "CreateTime"),
    ({"user_100ns": 61}, "UserTime"),
    ({"kernel_100ns": 19}, "KernelTime"),
    ({"read_bytes": 399}, "ReadTransferCount"),
    ({"other_ops": 4}, "OtherOperationCount"),
    ({"page_faults": 69}, "PageFaultCount"),
    ({"private_bytes": 900 << 20}, "PrivatePageCount"),
    ({"name": "pythonw.exe"}, "ImageName"),
    ({"session_id": 2}, "SessionId"),
    ({"threads": []}, "missing from the thread list"),
])
def test_compare_own_names_each_drifted_field(over, word):
    before, after = _readout(), _readout(user=60, kernel=25, faults=75, thread=(1100, 12, 6))
    bad = ntquery.compare_own(before, _me(**over), after)
    assert len(bad) == 1 and word in bad[0], bad


def test_compare_own_lists_every_drifted_field_not_just_the_first():
    bad = ntquery.compare_own(_readout(), _me(create_time=1, session_id=9, name="x.exe"), _readout(user=60))
    assert len(bad) >= 3


def test_self_check_raises_when_the_layout_drifts(monkeypatch):
    real = Snapshotter.snapshot

    def drifted(self, threads_for=()):
        procs = real(self, threads_for)
        procs[os.getpid()].create_time += 1                 # what a shifted offset would look like
        return procs

    monkeypatch.setattr(Snapshotter, "snapshot", drifted)
    with pytest.raises(RuntimeError, match="CreateTime"):
        ntquery.self_check()


def test_the_buffer_grows_on_length_mismatch_and_is_reused():
    s = Snapshotter(initial_size=4096)
    procs = s.snapshot()
    assert s.grow_count >= 1
    assert s.buffer_size >= s.last_bytes > 4096
    assert os.getpid() in procs and len(procs) > 20
    grown = s.grow_count
    s.snapshot()
    assert s.grow_count == grown                              # the grown buffer is kept


def test_threads_are_parsed_only_for_the_pids_asked_for():
    procs = Snapshotter().snapshot(threads_for=[os.getpid()])
    assert procs[os.getpid()].threads
    assert all(p.threads is None for pid, p in procs.items() if pid != os.getpid())
    assert 0 in procs and procs[0].name == ""                 # the idle process has no image name
    with pytest.raises(ValueError):
        Snapshotter().snapshot(threads_for=[-4])


def test_a_spinning_thread_shows_its_cpu_and_matches_psutil():
    tid = []
    started = threading.Event()

    def spin(seconds: float) -> None:
        tid.append(threading.get_native_id())
        started.set()
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            pass

    t = threading.Thread(target=spin, args=(0.6,))
    t.start()
    started.wait(5)
    s = Snapshotter()
    a = s.snapshot(threads_for=[os.getpid()])
    a_ns = time.perf_counter_ns()
    time.sleep(0.5)
    b = s.snapshot(threads_for=[os.getpid()])
    b_ns = time.perf_counter_ns()
    ref = {th.id: th for th in psutil.Process().threads()}
    t.join()

    def busy(procs):
        th = next(x for x in procs[os.getpid()].threads if x.tid == tid[0])
        return th.user_100ns + th.kernel_100ns

    pct = 100.0 * (busy(b) - busy(a)) / ((b_ns - a_ns) / 100)
    assert 80.0 <= pct <= 110.0, pct
    assert busy(b) % TICK == 0                                # tick-quantised (WT 3.5)
    r = ref[tid[0]]
    assert abs(round((r.user_time + r.system_time) * 1e7) - busy(b)) <= 2 * TICK


def test_file_io_shows_in_the_io_counters(tmp_path):
    path = tmp_path / "io.bin"
    payload = os.urandom(2 << 20)
    s = Snapshotter()
    before = s.snapshot()[os.getpid()]
    with open(path, "wb", buffering=0) as f:
        f.write(payload)
    with open(path, "rb", buffering=0) as f:
        assert len(f.read()) == len(payload)
    after = s.snapshot()[os.getpid()]
    io = psutil.Process().io_counters()
    assert after.write_bytes - before.write_bytes >= len(payload)
    assert after.read_bytes - before.read_bytes >= len(payload)
    assert after.read_bytes <= io.read_bytes and after.write_bytes <= io.write_bytes


def test_image_path_of_our_own_process():
    got = ntquery.image_path(os.getpid())
    # The venv's python.exe is only a launcher (LC 1.3): this process is the base interpreter, so the
    # independent readouts are QueryFullProcessImageNameW (psutil's own-process exe()) and _base_executable.
    assert os.path.normcase(got) == os.path.normcase(psutil.Process().exe())
    base = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    assert os.path.normcase(got) == os.path.normcase(base)
    assert Path(got).is_file()


def test_image_path_retries_with_the_size_windows_asks_for(monkeypatch):
    monkeypatch.setattr(ntquery, "_PATH_START_BYTES", 8)       # 4 characters: too small for any path
    assert os.path.normcase(ntquery.image_path(os.getpid())) == os.path.normcase(psutil.Process().exe())


def test_image_path_is_none_for_no_process_and_for_the_kernel():
    running = set(psutil.pids())
    missing = next(p for p in range(400_000, 600_000, 4) if p not in running)
    assert ntquery.image_path(missing) is None
    assert ntquery.image_path(4) is None                       # System has no image path
    assert ntquery.image_path(0) is None


@pytest.mark.parametrize("bad", [-4, 2 ** 32, True, 3.0, "4"])
def test_image_path_refuses_junk_pids(bad):
    with pytest.raises(ValueError):
        ntquery.image_path(bad)


DEVICES = {"\\Device\\HarddiskVolume1": "D:", "\\Device\\HarddiskVolume10": "C:"}


@pytest.mark.parametrize("nt, dos", [
    ("\\Device\\HarddiskVolume10\\Games\\x.exe", "C:\\Games\\x.exe"),   # Volume1 is a prefix of Volume10
    ("\\Device\\HarddiskVolume1\\y.exe", "D:\\y.exe"),
    ("\\DEVICE\\harddiskvolume1\\z.exe", "D:\\z.exe"),
    ("\\??\\C:\\Windows\\a.exe", "C:\\Windows\\a.exe"),
    ("\\Device\\Mup\\server\\share\\g.exe", "\\\\server\\share\\g.exe"),
    ("\\Device\\VhdHardDisk{1}\\q.exe", "\\Device\\VhdHardDisk{1}\\q.exe"),
])
def test_nt_paths_turn_into_drive_paths(nt, dos):
    assert ntquery.nt_to_dos(nt, DEVICES) == dos


# ---------------------------------------------------------------- the parser ---

def test_the_fast_parser_agrees_with_the_ctypes_structures():
    s = Snapshotter()
    used = s._query()
    me = os.getpid()
    procs = s._parse(used, {me})
    off, seen = 0, 0
    while True:
        e = SYSTEM_PROCESS_INFORMATION.from_buffer(s._buf, off)
        p = procs[e.UniqueProcessId or 0]
        assert (p.ppid, p.create_time, p.session_id) == (e.InheritedFromUniqueProcessId or 0, e.CreateTime, e.SessionId)
        assert (p.user_100ns, p.kernel_100ns, p.ws_bytes, p.private_bytes, p.page_faults) == (
            e.UserTime, e.KernelTime, e.WorkingSetSize, e.PrivatePageCount, e.PageFaultCount)
        assert (p.read_ops, p.write_ops, p.other_ops, p.read_bytes, p.write_bytes, p.other_bytes) == (
            e.ReadOperationCount, e.WriteOperationCount, e.OtherOperationCount, e.ReadTransferCount,
            e.WriteTransferCount, e.OtherTransferCount)
        assert p.thread_count == e.NumberOfThreads
        if e.ImageName.Buffer:
            assert p.name == ctypes.wstring_at(e.ImageName.Buffer, e.ImageName.Length // 2)
        if p.pid == me:
            for i, th in enumerate(p.threads):
                t = SYSTEM_THREAD_INFORMATION.from_buffer(s._buf, off + SPI_SIZE + i * STI_SIZE)
                assert (th.tid, th.create_time, th.user_100ns, th.kernel_100ns, th.priority, th.state) == (
                    t.ClientId.UniqueThread, t.CreateTime, t.UserTime, t.KernelTime, t.Priority, t.ThreadState)
        seen += 1
        if not e.NextEntryOffset:
            break
        off += e.NextEntryOffset
    assert seen == len(procs)


def _write_entry(s: Snapshotter, off: int, *, nxt: int, pid: int, nthreads: int = 0, name: str = "",
                 name_at: int | None = None, name_ptr: int | None = None) -> int:
    e = SYSTEM_PROCESS_INFORMATION.from_buffer(s._buf, off)
    ctypes.memset(ctypes.addressof(e), 0, SPI_SIZE + nthreads * STI_SIZE)
    e.NextEntryOffset, e.NumberOfThreads, e.UniqueProcessId = nxt, nthreads, pid
    e.InheritedFromUniqueProcessId, e.CreateTime, e.UserTime, e.KernelTime = 4, 1000 + pid, 7 * pid, 3 * pid
    e.ReadTransferCount, e.WriteTransferCount, e.SessionId = 11 * pid, 13 * pid, 1
    end = off + SPI_SIZE + nthreads * STI_SIZE
    for i in range(nthreads):
        t = SYSTEM_THREAD_INFORMATION.from_buffer(s._buf, off + SPI_SIZE + i * STI_SIZE)
        t.ClientId.UniqueThread, t.CreateTime, t.UserTime, t.KernelTime = 100 + i, 2000 + i, 5, 6
    if name:
        at = end if name_at is None else name_at
        enc = name.encode("utf-16-le")
        ctypes.memmove(ctypes.addressof(s._buf) + at, enc, len(enc))
        e.ImageName.Length, e.ImageName.MaximumLength = len(enc), len(enc)
        e.ImageName.Buffer = ctypes.addressof(s._buf) + at if name_ptr is None else name_ptr
        end = max(end, at + len(enc))
    return end


def test_a_synthetic_chain_parses_field_by_field():
    s = Snapshotter(initial_size=4096)
    first_end = _write_entry(s, 0, nxt=512, pid=8, nthreads=2, name="game.exe")
    assert first_end <= 512
    used = _write_entry(s, 512, nxt=0, pid=12, name="other.exe")
    procs = s._parse(used, {8})
    assert set(procs) == {8, 12}
    g = procs[8]
    assert (g.name, g.ppid, g.create_time, g.user_100ns, g.kernel_100ns, g.read_bytes, g.write_bytes) == (
        "game.exe", 4, 1008, 56, 24, 88, 104)
    assert [(t.tid, t.create_time, t.user_100ns, t.kernel_100ns) for t in g.threads] == [
        (100, 2000, 5, 6), (101, 2001, 5, 6)]
    assert procs[12].name == "other.exe" and procs[12].threads is None


def test_threads_that_run_past_the_data_are_refused():
    s = Snapshotter(initial_size=4096)
    used = _write_entry(s, 0, nxt=0, pid=8, nthreads=1)
    SYSTEM_PROCESS_INFORMATION.from_buffer(s._buf, 0).NumberOfThreads = 1000
    with pytest.raises(OSError) as info:
        s._parse(used, {8})
    assert info.value.winerror == 13


def test_a_next_offset_inside_the_threads_is_refused():
    s = Snapshotter(initial_size=4096)
    _write_entry(s, 0, nxt=SPI_SIZE, pid=8, nthreads=1)           # "next" points at its own thread entry
    tail = SYSTEM_PROCESS_INFORMATION.from_buffer(s._buf, SPI_SIZE)
    tail.NextEntryOffset = 0                                       # ... which would parse as a last entry
    with pytest.raises(OSError) as info:
        s._parse(2 * SPI_SIZE, {8})
    assert info.value.winerror == 13


def test_an_image_name_outside_the_data_is_refused():
    s = Snapshotter(initial_size=4096)
    used = _write_entry(s, 0, nxt=0, pid=8, name="game.exe", name_ptr=ctypes.addressof(s._buf) - 64)
    with pytest.raises(OSError) as info:
        s._parse(used, set())
    assert info.value.winerror == 13
