"""capture/sysmon.py: the 4 Hz system sampler.

Pure per-tick math on literal sample points; the real collector loop on fake sources (failures, focus,
children); and one live run against a spinning child process with a spinning grandchild.
"""
from __future__ import annotations

import statistics
import subprocess
import sys
import time

import psutil
import pytest

from lagdoctor import clock
from lagdoctor.capture import sysmon
from lagdoctor.capture import target as tgt
from lagdoctor.capture.sysmon import GpuSample, ProcessRates, SystemSampler
from lagdoctor.model import S
from lagdoctor.winapi.ntquery import ProcInfo, ThreadInfo
from lagdoctor.winapi.pdh import PdhError

NV, INTEL = "0x00000000_0x00010f83", "0x00000000_0x00010c49"
MIB = 1 << 20
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)


def eng(pid, luid_low, eng_no, typ):
    return f"pid_{pid}_luid_0x00000000_0x{luid_low:08X}_phys_0_eng_{eng_no}_engtype_{typ}"


# ---------------------------------------------------------------- CPU ---

def test_cpu_busy_is_user_plus_privileged_per_logical_cpu_only():
    user = {"0,0": 60.0, "0,1": 5.0, "0,2": None, "_Total": 0.0, "0,_Total": 0.0}
    priv = {"0,0": 30.0, "0,1": 5.0, "0,2": 3.0, "_Total": 0.0, "0,_Total": 0.0}
    busy = sysmon.cpu_busy(user, priv)
    assert busy == {"0,0": 90.0, "0,1": 10.0}
    assert sysmon.cpu_load_columns([busy]) == {S.CPU_TOTAL_PCT: 50.0, S.CPU_MAX_CORE_PCT: 90.0}
    assert sysmon.cpu_busy({}, {}) is None


def test_cpu_load_is_the_1s_mean_per_cpu_then_across_cpus():
    window = [{"0,0": 100.0, "0,1": 0.0}, {"0,0": 50.0, "0,1": 50.0}, None,
              {"0,0": 100.0, "0,1": 10.0}, {"0,0": 90.0, "0,1": 20.0}]
    got = sysmon.cpu_load_columns(window)
    assert got[S.CPU_MAX_CORE_PCT] == pytest.approx(85.0)
    assert got[S.CPU_TOTAL_PCT] == pytest.approx((85.0 + 20.0) / 2)
    assert sysmon.cpu_load_columns([{"0,0": 106.25}])[S.CPU_MAX_CORE_PCT] == 100.0     # one tick of overshoot
    assert sysmon.cpu_load_columns([None, None]) == {S.CPU_TOTAL_PCT: None, S.CPU_MAX_CORE_PCT: None}


def test_cpu_state_columns_take_the_total_the_lowest_zone_and_the_busiest_cpu():
    got = sysmon.cpu_state_columns(
        perf={"0,0": 40.0, "_Total": 64.9}, limit={"_Total": 100.0},
        passive={"\\_TZ.THRM": 100.0, "\\_TZ.CPUZ": 85.0, "\\_TZ.X": None},
        dpc={"0,0": 24.5, "0,1": 1.0, "_Total": 30.0}, isr={"0,0": 6.1, "0,3": 7.0, "_Total": 0.8})
    assert got == {S.CPU_PERF_PCT: 64.9, S.CPU_PERF_LIMIT_PCT: 100.0, S.THERMAL_LIMIT_PCT: 85.0,
                   S.DPC_PCT: 24.5, S.INTERRUPT_PCT: 7.0}
    empty = sysmon.cpu_state_columns({}, {}, {}, {}, {})
    assert all(v is None for v in empty.values())


# ---------------------------------------------------------------- memory and disk ---

def test_memory_columns():
    got = sysmon.memory_columns({"": 14000.0}, {"": 34.5e9}, {"": 40.0e9}, {"": 20.8})
    assert got == {S.RAM_AVAIL_MB: 14000.0, S.COMMIT_PCT: pytest.approx(86.25), S.HARD_FAULTS_PS: 20.8}
    assert sysmon.memory_columns({}, {"": 1.0}, {"": 0.0}, {})[S.COMMIT_PCT] is None
    assert all(v is None for v in sysmon.memory_columns({}, {}, {}, {}).values())


def test_disk_columns_follow_the_busiest_physical_disk():
    got = sysmon.disk_columns(
        idle={"0 C:": 92.4, "1 E:": 99.0, "_Total": 10.0},
        sec_read={"0 C:": 0.002, "1 E:": 0.050, "_Total": 0.026},
        read_bps={"0 C:": 1e6, "1 E:": 0.0, "_Total": 1e6},
        queue={"0 C:": 2.0, "1 E:": 0.0, "_Total": 2.0})
    assert got[S.DISK_BUSY_PCT] == pytest.approx(7.6)
    assert got[S.DISK_READ_MS] == pytest.approx(2.0)          # the busiest disk's latency, not the slowest's
    assert got[S.DISK_QUEUE] == 2.0 and got[S.DISK_READ_BPS] == 1e6


def test_an_idle_disk_reading_above_100_is_0_busy():
    assert sysmon.disk_columns({"1 E:": 100.3}, {}, {}, {})[S.DISK_BUSY_PCT] == 0.0
    assert sysmon.disk_columns({}, {}, {"_Total": 5.0}, {}) == {
        S.DISK_BUSY_PCT: None, S.DISK_READ_BPS: 5.0, S.DISK_READ_MS: None, S.DISK_QUEUE: None}


# ---------------------------------------------------------------- GPU ---

def test_gpu_sample_sums_an_engine_over_processes_and_takes_the_busiest_engine():
    s = sysmon.gpu_sample({
        eng(10, 0x10F83, 0, "3D"): 60.0, eng(20, 0x10F83, 0, "3D"): 50.0,     # one engine, two processes
        eng(10, 0x10F83, 3, "Copy"): 5.0, eng(10, 0x10F83, 4, "Copy"): 7.0,  # the game's two Copy engines
        eng(20, 0x10F83, 3, "Copy"): 1.0,
        eng(30, 0x10C49, 0, "3D"): 11.0, eng(40, 0x10C49, 0, "3D"): None, "not an engine": 99.0,
    }, game_pid=10)
    assert s.game == {(NV, "3D"): 60.0, (NV, "Copy"): 7.0}
    assert s.total == {(NV, "3D"): 110.0, (NV, "Copy"): 7.0, (INTEL, "3D"): 11.0}
    assert s.other == {(NV, "3D"): 50.0, (NV, "Copy"): 1.0, (INTEL, "3D"): 11.0}
    assert s.per_pid == {10: 60.0, 20: 50.0, 30: 11.0}
    assert s.per_pid_adapter == {(10, NV): 60.0, (20, NV): 50.0, (30, INTEL): 11.0}
    assert sysmon.gpu_sample({}, 10) is None


def _on(pid_values: dict[tuple[int, str], float]) -> GpuSample:
    return GpuSample({}, {}, {}, {}, pid_values)


@pytest.mark.parametrize("samples, expected", [
    ([400.0, 0.0, 0.0, 0.0], 100.0),         # unclamped samples, then the mean (one clamped sample would give 25)
    ([3652.0, 0.0, 0.0, 0.0], 100.0),        # only the mean is clamped (WT verify log 13)
    ([40.0, 20.0, None], 30.0),              # a failed read is left out, not counted as 0
    ([99.6, 100.4], 100.0),
    ([0.0, 0.0], 0.0),                       # an engine there that idled: 0, which is measured
])
def test_gpu_on_the_games_adapter_is_averaged_like_game_gpu_pct(samples, expected):
    window = [None if v is None else _on({(7, NV): v}) for v in samples]
    assert sysmon.pid_gpu_pct_on(window, 7, NV) == pytest.approx(expected)


def test_gpu_on_the_games_adapter_is_none_when_nothing_there_was_measured():
    window = [_on({(7, INTEL): 80.0}), _on({(7, INTEL): 60.0})]
    assert sysmon.pid_gpu_pct_on(window, 7, NV) is None           # it only uses the other GPU
    assert sysmon.pid_gpu_pct_on(window, 7, INTEL) == pytest.approx(70.0)
    assert sysmon.pid_gpu_pct_on(window, 7, None) is None         # the game's adapter is not known
    assert sysmon.pid_gpu_pct_on([None, None], 7, NV) is None
    # an engine there in part of the window: the absent samples count as idle, as game_gpu_pct counts them
    assert sysmon.pid_gpu_pct_on([_on({(7, NV): 40.0}), _on({})], 7, NV) == pytest.approx(20.0)


def _gs(game=0.0, total=None, other=0.0, typ="3D", luid=NV):
    return GpuSample({(luid, typ): game}, {(luid, typ): game if total is None else total},
                     {(luid, typ): other}, {})


def test_gpu_columns_average_unclamped_samples_then_clamp():
    burst = [_gs(300.0), _gs(0.0), _gs(0.0), _gs(0.0)]
    assert sysmon.gpu_engine_columns(burst, NV) == {S.GAME_GPU_PCT: 75.0, S.GPU_PCT: 75.0, S.GPU_OTHER_PCT: 0.0}
    huge = [_gs(3652.0, other=500.0), _gs(0.0), _gs(0.0), _gs(0.0)]
    assert sysmon.gpu_engine_columns(huge, NV) == {S.GAME_GPU_PCT: 100.0, S.GPU_PCT: 100.0, S.GPU_OTHER_PCT: 100.0}


def test_gpu_columns_skip_failed_samples_and_need_an_adapter():
    got = sysmon.gpu_engine_columns([None, _gs(40.0), _gs(20.0), None], NV)
    assert got[S.GAME_GPU_PCT] == pytest.approx(30.0)
    assert all(v is None for v in sysmon.gpu_engine_columns([_gs(40.0)], None).values())
    assert all(v is None for v in sysmon.gpu_engine_columns([None, None], NV).values())
    assert all(v is None for v in sysmon.gpu_engine_columns([_gs(40.0, luid=INTEL)], NV).values())


def test_gpu_columns_use_the_games_busiest_engine_type():
    s = GpuSample({(NV, "3D"): 20.0, (NV, "VideoDecode"): 60.0},
                  {(NV, "3D"): 95.0, (NV, "VideoDecode"): 70.0},
                  {(NV, "3D"): 75.0, (NV, "VideoDecode"): 10.0}, {})
    assert sysmon.gpu_engine_columns([s], NV) == {S.GAME_GPU_PCT: 60.0, S.GPU_PCT: 70.0, S.GPU_OTHER_PCT: 10.0}


def test_choose_adapter():
    assert sysmon.choose_adapter({NV: 10.0, INTEL: 3.0}, INTEL, []) == NV
    assert sysmon.choose_adapter({NV: 0.0}, INTEL, []) == INTEL
    assert sysmon.choose_adapter({}, None, [_gs(0.0, luid=INTEL), None]) == INTEL
    two = GpuSample({(NV, "3D"): 0.0, (INTEL, "3D"): 0.0}, {}, {}, {})
    assert sysmon.choose_adapter({}, None, [two]) is None


def test_gpu_memory_columns():
    ded = {"luid_0x00000000_0x00010F83_phys_0": 4096.0 * MIB, "luid_0x00000000_0x00010C49_phys_0": 1.0 * MIB}
    pded = {"pid_10_luid_0x00000000_0x00010F83_phys_0": 3072.0 * MIB,
            "pid_20_luid_0x00000000_0x00010F83_phys_0": 100.0 * MIB,
            "pid_10_luid_0x00000000_0x00010C49_phys_0": 9.0 * MIB}
    pshared = {"pid_10_luid_0x00000000_0x00010F83_phys_0": 200.0 * MIB}
    assert sysmon.gpu_memory_columns(ded, pded, pshared, 10, NV) == {
        S.VRAM_USED_MB: 4096.0, S.GAME_VRAM_MB: 3072.0, S.GAME_SHARED_MB: 200.0}
    absent = sysmon.gpu_memory_columns(ded, {"pid_20_luid_0x00000000_0x00010F83_phys_0": 1.0}, {}, 10, NV)
    assert absent == {S.VRAM_USED_MB: 4096.0, S.GAME_VRAM_MB: 0.0, S.GAME_SHARED_MB: None}
    failed_item = sysmon.gpu_memory_columns(ded, {"pid_10_luid_0x00000000_0x00010F83_phys_0": None}, {}, 10, NV)
    assert failed_item[S.GAME_VRAM_MB] is None
    assert all(v is None for v in sysmon.gpu_memory_columns(ded, pded, pshared, 10, None).values())


# ---------------------------------------------------------------- processes ---

DT_NS = 250_000_000            # one 250 ms tick
TICK_100NS = 2_500_000


def p(pid, total=0, ppid=4, create=None, threads=None, faults=0, rb=0, wb=0, rops=0, wops=0, ws=0, priv=0,
      name="x.exe"):
    return ProcInfo(pid, ppid, 1000 + pid if create is None else create, name, 1, total, 0, ws, priv, faults,
                    rb, wb, 0, rops, wops, 0, len(threads or []), threads)


def th(tid, create, total):
    return ThreadInfo(tid, create, total, 0, 8, 2)


def test_game_rates_threads_clamped_new_threads_counted_whole():
    r = ProcessRates(100, 1100)
    before = {0: p(0, 0, ppid=0), 100: p(100, 0, threads=[th(1, 5, 0), th(2, 6, 1_000_000)]),
              104: p(104, 0, ppid=100), 200: p(200, 10_000_000, create=1)}
    cols, others = r.update(before, 10 * DT_NS)
    assert others is None and cols[S.GAME_CPU_PCT] is None          # first snapshot: only a baseline
    assert cols[S.GAME_THREADS] == 2
    after = {0: p(0, 40_000_000, ppid=0),
             100: p(100, TICK_100NS, threads=[th(1, 5, 3_125_000), th(3, 8, 1_000_000)],
                    faults=500, rb=1_000_000, wb=250_000, rops=10, wops=15, ws=512 * MIB, priv=256 * MIB),
             104: p(104, TICK_100NS, ppid=100),
             200: p(200, 1_000_000, create=2)}                          # PID 200 reused by a new process
    cols, others = r.update(after, 11 * DT_NS)
    assert cols[S.GAME_CPU_PCT] == pytest.approx(100.0)                # the target only: not +100 for its child
    assert cols[S.GAME_TOP_THREAD_ID] == 1 and cols[S.GAME_TOP_THREAD_PCT] == 100.0   # 125 % clamped
    assert cols[S.GAME_PAGE_FAULTS_PS] == pytest.approx(2000.0)
    assert cols[S.GAME_IO_READ_BPS] == pytest.approx(4e6) and cols[S.GAME_IO_WRITE_BPS] == pytest.approx(1e6)
    assert cols[S.GAME_IO_OPS_PS] == pytest.approx(100.0)
    assert (cols[S.GAME_WS_MB], cols[S.GAME_PRIVATE_MB], cols[S.GAME_THREADS]) == (512.0, 256.0, 2)
    by_pid = {o[0]: o for o in others}
    assert 100 not in by_pid and 0 not in by_pid                       # the game itself, and idle time
    assert by_pid[104][2] == pytest.approx(100.0)                      # the child counts as another process
    assert by_pid[200][2] == pytest.approx(40.0)                       # the new PID-200 process, from zero


def test_a_reused_thread_id_is_a_new_thread():
    r = ProcessRates(100, 1100)
    r.update({100: p(100, 0, threads=[th(1, 5, 0), th(2, 6, 4_000_000)])}, 0)
    cols, _ = r.update({100: p(100, 0, threads=[th(1, 5, 250_000), th(2, 7, 1_500_000)])}, DT_NS)
    assert cols[S.GAME_TOP_THREAD_ID] == 2 and cols[S.GAME_TOP_THREAD_PCT] == pytest.approx(60.0)


def test_a_gone_or_replaced_game_has_no_game_columns():
    r = ProcessRates(100, 1100)
    r.update({100: p(100, 0, threads=[th(1, 5, 0)])}, 0)
    cols, others = r.update({100: p(100, 9, create=5000, threads=[])}, DT_NS)   # same PID, other process
    assert all(cols[c] is None for c in sysmon.GAME_COLUMNS)
    assert [o[0] for o in others] == [100]                              # ... which is just another process
    cols, _ = r.update(None, 2 * DT_NS)
    assert all(cols[c] is None for c in sysmon.GAME_COLUMNS)
    cols, others = r.update({100: p(100, 0, threads=[th(1, 5, 0)])}, 3 * DT_NS)
    assert others is None and cols[S.GAME_CPU_PCT] is None              # no baseline after a failed snapshot


def test_top_others_keeps_the_8_busiest_that_did_anything():
    rates = [(pid, f"p{pid}.exe", float(pid), 0.0, 1000 + pid) for pid in range(1, 13)]
    rows = sysmon.top_others(rates, [], NV)
    assert [r[0] for r in rows] == [12, 11, 10, 9, 8, 7, 6, 5]
    assert rows[0] == [12, "p12.exe", 12.0, 0.0, None, 1012, None]    # the create time rides along
    gpu = [GpuSample({}, {}, {}, {31: 5.0, 34: 9.0}, {(31, INTEL): 5.0, (34, NV): 9.0})]
    rows = sysmon.top_others([(30, "idle.exe", 0.0, 0.0, 7), (31, "enc.exe", 0.0, 0.0, 8),
                              (32, "io.exe", 0.0, 9e5, 9), (33, "busy.exe", 50.0, 0.0, 10),
                              (34, "gpu.exe", 0.0, 0.0, 11)], gpu, NV)
    assert [r[0] for r in rows] == [33, 32, 34, 31]                   # fields 0-5 and the ranking as before
    assert rows[2] == [34, "gpu.exe", 0.0, 0.0, 9.0, 11, 9.0]         # busy on the game's GPU
    assert rows[3] == [31, "enc.exe", 0.0, 0.0, 5.0, 8, None]         # busy, but only on the other GPU


def test_cost_stats_median_from_the_histogram():
    c = sysmon.CostStats()
    assert c.median_ms() is None
    for ms in (10.0, 12.0, 14.0):
        c.add(ms)
    assert c.median_ms() == pytest.approx(12.125)
    assert c.summary()["ticks"] == 3 and c.summary()["max_ms"] == 14.0


WT_11_1 = {r"\Processor Information(*)\% Processor Utility", r"\Processor Information(*)\% Processor Performance",
           r"\Processor Information(_Total)\% Performance Limit",
           r"\Processor Information(_Total)\Performance Limit Flags", r"\Processor Information(*)\% DPC Time",
           r"\Processor Information(*)\% Interrupt Time", r"\Processor Information(*)\% User Time",
           r"\Processor Information(*)\% Privileged Time", r"\Memory\Available MBytes", r"\Memory\Committed Bytes",
           r"\Memory\Commit Limit", r"\Memory\Pages Input/sec", r"\PhysicalDisk(*)\% Idle Time",
           r"\PhysicalDisk(*)\Avg. Disk sec/Read", r"\PhysicalDisk(*)\Avg. Disk sec/Write",
           r"\PhysicalDisk(*)\Disk Read Bytes/sec", r"\PhysicalDisk(*)\Disk Write Bytes/sec",
           r"\GPU Engine(*)\Utilization Percentage", r"\GPU Adapter Memory(*)\Dedicated Usage",
           r"\GPU Adapter Memory(*)\Shared Usage", r"\GPU Process Memory(*)\Dedicated Usage",
           r"\Thermal Zone Information(*)\Temperature", r"\Thermal Zone Information(*)\% Passive Limit"}


def test_the_counter_list_is_wt_11_1_and_never_a_forbidden_counter():
    paths = set(sysmon.PDH_PATHS)
    assert WT_11_1 <= paths
    assert paths - WT_11_1 == {r"\GPU Process Memory(*)\Shared Usage", r"\PhysicalDisk(*)\Current Disk Queue Length"}
    for path in paths:                                                   # WT 11.3
        assert "% Processor Time" not in path
        assert not (path.startswith("\\Processor") and "% Idle Time" in path)
        for banned in ("\\Process(", "\\Process V2(", "\\Thread(", "\\System\\", "\\Network Interface("):
            assert not path.startswith(banned), path


# ---------------------------------------------------------------- the loop, on fake sources ---

GAME, CHILD = 100, 104


class FakePdh:
    """Counter values per tick; collect() on the ticks in `fail` raises."""

    def __init__(self, paths, fail=(), never=()):
        self.ticks, self.fail, self.never = 0, set(fail), set(never)
        self.add_errors = {}
        self.closed = False

    def collect(self):
        self.ticks += 1
        if self.ticks in self.fail:
            err = PdhError(0, "PdhCollectQueryData failed")
            err.winerror = 0x800007D5
            raise err

    def read(self, path):
        k = self.ticks
        if path in self.never:
            return {}
        return {
            sysmon.P_USER: {"0,0": 40.0, "0,1": 10.0, "_Total": 25.0},
            sysmon.P_PRIV: {"0,0": 10.0, "0,1": 0.0, "_Total": 5.0},
            sysmon.P_PERF: {"_Total": 50.0 + k},
            sysmon.M_AVAIL: {"": 1000.0 + k},
            sysmon.G_ENGINE: {eng(GAME, 0x10C49, 0, "3D"): 30.0, eng(CHILD, 0x10C49, 0, "3D"): 10.0},
        }.get(path, {"": 1.0})   # also answers after a failed collect: the sampler must not ask then

    def close(self):
        self.closed = True


class FakeSnap:
    """The game, its child and another process; CPU grows each call; calls in `fail` raise."""

    def __init__(self, fail=()):
        self.calls, self.fail = 0, set(fail)

    def snapshot(self, threads_for=()):
        self.calls += 1
        if self.calls in self.fail:
            err = OSError(0, "NtQuerySystemInformation failed")
            err.winerror = 87
            raise err
        k = self.calls
        return {GAME: p(GAME, k * 1_000_000, create=1100, threads=[th(1, 5, k * 1_000_000)], ws=100 * MIB),
                CHILD: p(CHILD, k * 500_000, ppid=GAME, create=1200),
                300: p(300, k * 100_000, create=1300, name="other.exe")}


def _run(sampler, ticks, period):
    sampler.start(clock.now())
    deadline = time.perf_counter() + 10
    while len(sampler.series) < ticks - 1 and time.perf_counter() < deadline:
        time.sleep(period / 4)
    sampler.stop()
    return sampler.result()


def test_failed_reads_become_none_and_the_first_tick_is_only_a_baseline():
    pdh = {}
    snap = FakeSnap(fail={5})
    s = SystemSampler({"pid": GAME, "create_time": 1100}, period_s=0.05,
                      pdh_factory=lambda paths: pdh.setdefault("q", FakePdh(paths, fail={3})),
                      snapshotter=snap, foreground=lambda: CHILD, layout_check=lambda snapshotter: None)
    r = _run(s, 9, 0.05)
    cols = r["columns"]
    n = len(cols[S.T])
    assert n >= 7 and set(cols) == set(S.ALL)
    assert pdh["q"].closed
    # Tick 1 is the baseline, so row j holds tick j + 2 (1-based): tick 3 lost PDH, tick 5 the snapshot.
    assert snap.calls == n + 1
    pdh_lost, snap_lost = 3 - 2, 5 - 2
    assert cols[S.CPU_PERF_PCT][pdh_lost] is None and cols[S.RAM_AVAIL_MB][pdh_lost] is None
    assert cols[S.GAME_THREADS][pdh_lost] == 1
    assert cols[S.RAM_AVAIL_MB][snap_lost] is not None
    assert cols[S.GAME_THREADS][snap_lost] is None and cols[S.GAME_CPU_PCT][snap_lost] is None
    assert cols[S.GAME_THREADS][snap_lost + 1] == 1 and cols[S.GAME_CPU_PCT][snap_lost + 1] is None
    good = [i for i in range(n) if i not in (pdh_lost, snap_lost, snap_lost + 1)]
    assert all(cols[S.GAME_CPU_PCT][i] is not None and cols[S.RAM_AVAIL_MB][i] is not None for i in good)
    assert all(cols[S.CPU_TOTAL_PCT][i] == pytest.approx(30.0) for i in good)
    assert cols[S.GAME_FOCUSED] == [0] * n             # the foreground is the game's child, not the game
    assert r["game_pids"] == [GAME, CHILD]
    assert r["game_adapter_luid"] == INTEL
    assert all(row[1] and {o[0] for o in row[1]} == {CHILD, 300} for row in r["others"])
    st = s.status()
    assert st.state == "ok" and "PDH collect failed in 1" in st.detail and "snapshot failed in 1" in st.detail


class TwoGpuPdh(FakePdh):
    """The game draws on the Intel GPU; other processes load either GPU (WT 2.2 instance names)."""

    def read(self, path):
        if path == sysmon.G_ENGINE:
            return {eng(GAME, 0x10C49, 0, "3D"): 30.0, eng(CHILD, 0x10C49, 0, "3D"): 10.0,
                    eng(300, 0x10F83, 0, "3D"): 80.0, eng(300, 0x10C49, 5, "VideoDecode"): 5.0,
                    eng(301, 0x10F83, 0, "Compute_0"): 40.0}
        return super().read(path)


class ThreeOthersSnap(FakeSnap):
    def snapshot(self, threads_for=()):
        procs = super().snapshot(threads_for)
        procs[301] = p(301, self.calls * 50_000, create=1301, name="trainer.exe")
        return procs


def test_others_rows_say_how_busy_each_process_kept_the_games_own_gpu():
    # A10: field 4 is a process's busiest engine on any adapter, so a job on the other GPU looked like it was
    # competing with the game. Field 6 is the same on the game's adapter (the one game_gpu_pct uses).
    s = SystemSampler({"pid": GAME, "create_time": 1100}, period_s=0.05,
                      pdh_factory=lambda paths: TwoGpuPdh(paths), snapshotter=ThreeOthersSnap(),
                      foreground=lambda: GAME, layout_check=lambda snapshotter: None)
    r = _run(s, 6, 0.05)
    assert r["game_adapter_luid"] == INTEL and r["columns"][S.GAME_GPU_PCT][-1] == pytest.approx(30.0)
    assert r["others"]
    for _t, row in r["others"]:
        by_pid = {o[0]: o for o in row}
        assert set(by_pid) == {CHILD, 300, 301} and all(len(o) == 7 for o in row)
        assert {pid: o[5] for pid, o in by_pid.items()} == {CHILD: 1200, 300: 1300, 301: 1301}   # create times
        assert [by_pid[pid][4] for pid in (CHILD, 300, 301)] == [10.0, 80.0, 40.0]     # any adapter, unchanged
        assert [by_pid[pid][6] for pid in (CHILD, 300, 301)] == [10.0, 5.0, None]      # the game's adapter only


def test_others_rows_have_no_game_gpu_figure_while_the_games_adapter_is_unknown():
    class NoGameGpu(TwoGpuPdh):            # the game has no GPU engine at all, and nothing named its adapter
        def read(self, path):
            got = super().read(path)
            if path == sysmon.G_ENGINE:
                got = {k: v for k, v in got.items() if not k.startswith(f"pid_{GAME}_")}
            return got

    s = SystemSampler({"pid": GAME, "create_time": 1100}, period_s=0.05,
                      pdh_factory=lambda paths: NoGameGpu(paths), snapshotter=ThreeOthersSnap(),
                      foreground=lambda: GAME, layout_check=lambda snapshotter: None)
    r = _run(s, 5, 0.05)
    assert r["game_adapter_luid"] is None and r["others"]
    assert all(o[6] is None and o[4] is not None for _t, row in r["others"] for o in row)


def test_a_counter_that_never_reads_makes_the_source_partial():
    s = SystemSampler({"pid": GAME, "create_time": 1100}, period_s=0.05,
                      pdh_factory=lambda paths: FakePdh(paths, never={sysmon.G_ENGINE}),
                      snapshotter=FakeSnap(), foreground=lambda: GAME, layout_check=lambda snapshotter: None)
    r = _run(s, 6, 0.05)
    cols = r["columns"]
    assert cols[S.GAME_FOCUSED] == [1] * len(cols[S.T])
    assert all(v is None for v in cols[S.GAME_GPU_PCT])
    st = s.status()
    assert st.state == "partial" and r"\GPU Engine(*)\Utilization Percentage gave no reading" in st.detail


def test_a_layout_drift_keeps_the_counters_but_drops_the_process_data():
    def drift(snapshotter):
        raise RuntimeError("the NtQuerySystemInformation layout does not match")

    s = SystemSampler({"pid": GAME, "create_time": 1100}, period_s=0.05, pdh_factory=lambda paths: FakePdh(paths),
                      snapshotter=FakeSnap(), foreground=lambda: GAME, layout_check=drift)
    r = _run(s, 5, 0.05)
    cols = r["columns"]
    assert all(v is not None for v in cols[S.RAM_AVAIL_MB])
    assert all(v is None for v in cols[S.GAME_CPU_PCT]) and r["others"] == []
    st = s.status()
    assert st.state == "partial" and "process data is unavailable" in st.detail


def test_nothing_working_is_a_loud_failure():
    def no_pdh(paths):
        raise PdhError(0, "PdhOpenQueryW failed")

    def drift(snapshotter):
        raise RuntimeError("layout drifted")

    s = SystemSampler({"pid": GAME, "create_time": 1100}, pdh_factory=no_pdh, snapshotter=FakeSnap(),
                      layout_check=drift)
    s.start(clock.now())
    time.sleep(0.3)
    s.stop()
    st = s.status()
    assert st.state == "failed" and "PdhOpenQueryW" in st.detail and "layout drifted" in st.detail
    assert s.result()["columns"][S.T] == [] and s.result()["others"] == []


@pytest.mark.parametrize("bad", [{}, {"pid": 0, "create_time": 1}, {"pid": 8, "create_time": None},
                                 {"pid": "8", "create_time": 1}])
def test_a_target_without_an_identity_is_refused(bad):
    with pytest.raises(ValueError):
        SystemSampler(bad)


# ---------------------------------------------------------------- live ---

# Spins its main thread, then reports when, and how much CPU it really got (its own thread and process
# times): the independent readout the sampler is judged against, so a busy machine does not fail the test.
SPINNER = r"""
import os, subprocess, sys, time
delay, spin, depth = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
kid = None
if depth:
    kid = subprocess.Popen([sys.executable, "-I", "-c", sys.argv[4], sys.argv[1], sys.argv[2], "0", sys.argv[4]])
time.sleep(delay)
c0, p0, t0 = time.thread_time(), time.process_time(), time.perf_counter()
while time.perf_counter() < t0 + spin:
    pass
t1, c1, p1 = time.perf_counter(), time.thread_time(), time.process_time()
print("child" if depth else "grandchild", os.getpid(), repr(t0), repr(t1), repr(c1 - c0), repr(p1 - p0), flush=True)
if kid:
    kid.wait()
"""


def _windows_of_4(values):
    return [statistics.mean(values[i:i + 4]) for i in range(len(values) - 3)]


def test_live_sampler_on_a_spinning_child_and_its_spinning_grandchild():
    child = subprocess.Popen([BASE_PYTHON, "-I", "-c", SPINNER, "1.0", "3.0", "1", SPINNER],
                             stdout=subprocess.PIPE, text=True)
    sampler = None
    try:
        target = tgt.find_target(pid=child.pid, sample_s=0)
        origin = clock.now()
        sampler = SystemSampler(target)
        sampler.start(origin)
        spins = {}
        for _ in range(2):
            role, pid, t0, t1, thread_s, proc_s = child.stdout.readline().split()
            t0, t1 = float(t0) - origin, float(t1) - origin
            spins[role] = (int(pid), t0, t1, 100 * float(thread_s) / (t1 - t0), 100 * float(proc_s) / (t1 - t0))
        child.wait(timeout=10)
        time.sleep(0.6)
    finally:
        if sampler is not None:
            sampler.stop()
        if child.poll() is None:
            child.kill()
    r = sampler.result()
    cols = r["columns"]
    _pid, t0, t1, thread_share, proc_share = spins["child"]
    gpid, _g0, _g1, _gthread, g_share = spins["grandchild"]
    inside = [i for i, t in enumerate(cols[S.T]) if t0 + 0.3 <= t <= t1 - 0.05]
    assert len(inside) >= 8, (t0, t1, cols[S.T])
    top = [cols[S.GAME_TOP_THREAD_PCT][i] for i in inside]
    cpu = [cols[S.GAME_CPU_PCT][i] for i in inside]
    report = f"spinner's own share: thread {thread_share:.1f} %, process {proc_share:.1f} %; top {top}; cpu {cpu}"
    assert thread_share >= 60.0, f"the machine was too busy to judge: {report}"
    assert all(v <= 100.0 for v in top), report                          # each thread clamped
    assert abs(statistics.mean(top) - min(100.0, thread_share)) <= 8.0, report
    assert abs(statistics.mean(cpu) - proc_share) <= 10.0, report
    assert all(m >= 60.0 for m in _windows_of_4(top)), report             # near saturation in every 1 s window
    assert max(cpu) < 150.0, f"the grandchild's CPU was counted as the game's: {report}"
    rows = [row for row in r["others"] if t0 + 0.3 <= row[0] <= t1 - 0.05]
    gc = [next((o[2] for o in row[1] if o[0] == gpid), 0.0) for row in rows]
    assert abs(statistics.mean(gc) - g_share) <= 10.0, (gc, g_share)      # the grandchild is an "other"
    assert {child.pid, gpid} <= set(r["game_pids"])
    assert cols[S.GAME_CPU_PCT][-1] is None and cols[S.GAME_THREADS][-1] is None
    assert all(cols[c][i] is not None for i in inside
               for c in (S.CPU_TOTAL_PCT, S.CPU_MAX_CORE_PCT, S.RAM_AVAIL_MB, S.COMMIT_PCT, S.DISK_BUSY_PCT))
    assert all(b > a for a, b in zip(cols[S.T], cols[S.T][1:]))
    ram = psutil.virtual_memory().available / MIB
    assert abs(cols[S.RAM_AVAIL_MB][-1] - ram) <= max(512, 0.05 * ram)
    st = sampler.status()
    assert st.state == "ok" and "exited" in st.detail, st
    cost = r["cost"]
    print(f"\nper-tick cost: median {cost['median_ms']} ms, mean {cost['mean_ms']} ms, max {cost['max_ms']} ms "
          f"over {cost['ticks']} ticks; status: {st.detail}")
    assert cost["median_ms"] < 50.0
