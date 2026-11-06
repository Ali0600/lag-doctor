"""The system sampler: CPU, GPU, memory and disk 4 times a second, for the machine and for the game.

Design: docs/research/windows-telemetry.md (WT) section 11, as corrected by its verification log. Each tick
runs one PDH collect (the WT 11.1 counter list) and one NtQuerySystemInformation snapshot with the game's
threads, and writes one row of S columns plus an `others` row (the top 8 other processes by CPU).

- Process and thread CPU are the delta since the previous tick (about 250 ms), kept sharp so a short hitch
  shows in the tick that holds it. Thread times move in 15.625 ms steps (WT 3.5), so one tick is only good
  to about 6 % of a core: the analysis judges saturation over 1 s or more. Each thread is clamped to 100.
- Identity is (pid, create_time) and (tid, create_time): PIDs and TIDs are reused.
- The game_* columns are the target process only; its child processes count as others.
- GPU Engine values stay unclamped per sample, are averaged over the trailing 1 s, and only the average is
  clamped (single samples reached 3,652 %, WT verify log 13). An others row gives each process's busiest
  engine on any adapter (field 4) and on the game's adapter (field 6, None if it has no engine there).
- A failed read is None in the row, never 0. Nothing here opens a handle to any process.
"""
from __future__ import annotations

import heapq
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from lagdoctor.capture import target as tgt
from lagdoctor.capture.base import Collector
from lagdoctor.model import S, SeriesBuilder
from lagdoctor.winapi import ntquery, windows
from lagdoctor.winapi.ntquery import ProcInfo
from lagdoctor.winapi.pdh import PdhQuery, status_name

PERIOD_S = 0.25
WINDOW_SAMPLES = 4            # the trailing 1 s at 4 Hz
TOP_OTHERS = 8
MAX_GAME_PIDS = 4096
MIB = float(1 << 20)
TOOL = "PDH + NtQuerySystemInformation"

# The WT 11.1 counter list (with its correction: % User Time + % Privileged Time instead of % Processor Time).
P_UTILITY = r"\Processor Information(*)\% Processor Utility"
P_PERF = r"\Processor Information(*)\% Processor Performance"
P_LIMIT = r"\Processor Information(_Total)\% Performance Limit"
P_LIMIT_FLAGS = r"\Processor Information(_Total)\Performance Limit Flags"
P_DPC = r"\Processor Information(*)\% DPC Time"
P_ISR = r"\Processor Information(*)\% Interrupt Time"
P_USER = r"\Processor Information(*)\% User Time"
P_PRIV = r"\Processor Information(*)\% Privileged Time"
M_AVAIL = r"\Memory\Available MBytes"
M_COMMITTED = r"\Memory\Committed Bytes"
M_LIMIT = r"\Memory\Commit Limit"
M_PAGES_IN = r"\Memory\Pages Input/sec"
D_IDLE = r"\PhysicalDisk(*)\% Idle Time"
D_READ_S = r"\PhysicalDisk(*)\Avg. Disk sec/Read"
D_WRITE_S = r"\PhysicalDisk(*)\Avg. Disk sec/Write"
D_READ_BPS = r"\PhysicalDisk(*)\Disk Read Bytes/sec"
D_WRITE_BPS = r"\PhysicalDisk(*)\Disk Write Bytes/sec"
G_ENGINE = tgt.GPU_ENGINE_UTIL
G_ADAPTER_DED = r"\GPU Adapter Memory(*)\Dedicated Usage"
G_ADAPTER_SHARED = r"\GPU Adapter Memory(*)\Shared Usage"
G_PROC_DED = r"\GPU Process Memory(*)\Dedicated Usage"
T_TEMP = r"\Thermal Zone Information(*)\Temperature"
T_PASSIVE = r"\Thermal Zone Information(*)\% Passive Limit"
# Two counters WT 11.1 leaves out but S needs (game_shared_mb, disk_queue). Both are in sets the query
# already collects, and both are listed in WT 2.1.
G_PROC_SHARED = r"\GPU Process Memory(*)\Shared Usage"
D_QUEUE = r"\PhysicalDisk(*)\Current Disk Queue Length"

PDH_PATHS = (P_UTILITY, P_PERF, P_LIMIT, P_LIMIT_FLAGS, P_DPC, P_ISR, P_USER, P_PRIV,
             M_AVAIL, M_COMMITTED, M_LIMIT, M_PAGES_IN,
             D_IDLE, D_READ_S, D_WRITE_S, D_READ_BPS, D_WRITE_BPS, D_QUEUE,
             G_ENGINE, G_ADAPTER_DED, G_ADAPTER_SHARED, G_PROC_DED, G_PROC_SHARED,
             T_TEMP, T_PASSIVE)

CPU_INSTANCE = re.compile(r"^\d+,\d+$")      # Processor Information instances are "group,index" (WT 2.1)

Values = dict[str, Any]


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return lo if v < lo else hi if v > hi else v


def single(values: dict[str, float | None]) -> float | None:
    """The value of a counter with one instance ("" for no instances, "_Total" for an explicit one)."""
    if len(values) == 1:
        return next(iter(values.values()))
    return values.get("", values.get("_Total"))


# ---------------------------------------------------------------- CPU ---

def cpu_busy(user: dict[str, float | None], priv: dict[str, float | None]) -> dict[str, float] | None:
    """Per logical CPU: % User Time + % Privileged Time (WT 11.1: valid in every poll, unlike % Processor Time)."""
    busy = {}
    for inst, u in user.items():
        p = priv.get(inst)
        if u is not None and p is not None and CPU_INSTANCE.match(inst):
            busy[inst] = u + p
    return busy or None


def cpu_load_columns(window: list[dict[str, float] | None]) -> Values:
    """cpu_total_pct / cpu_max_core_pct over the trailing window (per CPU, then across CPUs)."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for busy in window:
        for cpu, v in (busy or {}).items():
            sums[cpu] = sums.get(cpu, 0.0) + v
            counts[cpu] = counts.get(cpu, 0) + 1
    if not sums:
        return {S.CPU_TOTAL_PCT: None, S.CPU_MAX_CORE_PCT: None}
    avg = [_clamp(sums[c] / counts[c]) for c in sums]
    return {S.CPU_TOTAL_PCT: sum(avg) / len(avg), S.CPU_MAX_CORE_PCT: max(avg)}


def busiest_cpu(values: dict[str, float | None]) -> float | None:
    found = [_clamp(v) for inst, v in values.items() if v is not None and CPU_INSTANCE.match(inst)]
    return max(found) if found else None


def cpu_state_columns(perf: dict, limit: dict, passive: dict, dpc: dict, isr: dict) -> Values:
    passive_ok = [v for v in passive.values() if v is not None]
    return {
        S.CPU_PERF_PCT: perf.get("_Total"),
        S.CPU_PERF_LIMIT_PCT: single(limit),
        S.THERMAL_LIMIT_PCT: min(passive_ok) if passive_ok else None,     # the most limited zone
        S.DPC_PCT: busiest_cpu(dpc),
        S.INTERRUPT_PCT: busiest_cpu(isr),
    }


# ---------------------------------------------------------------- memory and disk ---

def memory_columns(avail: dict, committed: dict, limit: dict, pages_in: dict) -> Values:
    used, cap = single(committed), single(limit)
    return {
        S.RAM_AVAIL_MB: single(avail),
        S.COMMIT_PCT: 100.0 * used / cap if used is not None and cap else None,
        S.HARD_FAULTS_PS: single(pages_in),
    }


def disk_columns(idle: dict, sec_read: dict, read_bps: dict, queue: dict) -> Values:
    """Busy %, read latency and queue of the busiest physical disk; read bytes/s of all disks (_Total)."""
    disks = {n: v for n, v in idle.items() if n != "_Total" and v is not None}
    out: Values = {S.DISK_BUSY_PCT: None, S.DISK_READ_BPS: read_bps.get("_Total"), S.DISK_READ_MS: None,
                   S.DISK_QUEUE: None}
    if disks:
        busiest = min(disks, key=lambda n: (disks[n], n))          # least idle
        out[S.DISK_BUSY_PCT] = _clamp(100.0 - disks[busiest])      # % Idle Time can read 100.3 (WT 11.5.2)
        sec = sec_read.get(busiest)
        out[S.DISK_READ_MS] = sec * 1000.0 if sec is not None else None
        out[S.DISK_QUEUE] = queue.get(busiest)
    return out


# ---------------------------------------------------------------- GPU ---

@dataclass(slots=True)
class GpuSample:
    """One GPU Engine read, reduced. Keys are (luid, engine type); values are % of that engine, unclamped."""
    game: dict[tuple[str, str], float]      # the game's busiest engine of each type
    total: dict[tuple[str, str], float]     # the busiest engine of each type, summed over all processes
    other: dict[tuple[str, str], float]     # the same without the game
    per_pid: dict[int, float]               # each process's busiest engine on any adapter
    # each process's busiest engine on each adapter it has an engine on, keyed (pid, luid)
    per_pid_adapter: dict[tuple[int, str], float] = field(default_factory=dict)


def gpu_sample(engine: dict[str, float | None], game_pid: int,
               parsed: dict[str, tuple[int, str, int, int, str] | None] | None = None) -> GpuSample | None:
    """Reduce one \\GPU Engine(*) read as Task Manager does (WT 2.3): an engine is time-shared, so sum it
    over processes; across engines take the busiest, never a sum or an average. None for a failed read."""
    if not engine:
        return None
    eng_all: dict[tuple[str, int, int, str], float] = {}
    eng_other: dict[tuple[str, int, int, str], float] = {}
    game: dict[tuple[str, str], float] = {}
    per_pid: dict[int, float] = {}
    per_pid_adapter: dict[tuple[int, str], float] = {}
    for inst, v in engine.items():
        if v is None:
            continue
        p = parsed.get(inst) if parsed is not None else tgt.parse_engine(inst)
        if p is None:
            continue
        pid, luid, phys, eng, typ = p
        key = (luid, phys, eng, typ)
        eng_all[key] = eng_all.get(key, 0.0) + v
        if pid == game_pid:
            game[(luid, typ)] = max(game.get((luid, typ), 0.0), v)
        else:
            eng_other[key] = eng_other.get(key, 0.0) + v
        per_pid[pid] = max(per_pid.get(pid, 0.0), v)
        per_pid_adapter[(pid, luid)] = max(per_pid_adapter.get((pid, luid), 0.0), v)
    total: dict[tuple[str, str], float] = {}
    for (luid, _phys, _eng, typ), v in eng_all.items():
        total[(luid, typ)] = max(total.get((luid, typ), 0.0), v)
    other: dict[tuple[str, str], float] = {}
    for (luid, _phys, _eng, typ), v in eng_other.items():
        other[(luid, typ)] = max(other.get((luid, typ), 0.0), v)
    return GpuSample(game, total, other, per_pid, per_pid_adapter)


def choose_adapter(luid_3d_use: dict[str, float], fallback: str | None, window: list[GpuSample | None]) -> str | None:
    """The adapter whose 3D engine the game used most so far; else the one it was picked on; else its only
    3D engine's adapter; else unknown."""
    used = {k: v for k, v in luid_3d_use.items() if v > 0}
    if used:
        return max(used, key=lambda k: (used[k], k))
    if fallback:
        return fallback
    seen = {luid for s in window if s is not None for (luid, typ) in s.game if tgt.is_3d(typ)}
    return next(iter(seen)) if len(seen) == 1 else None


def gpu_engine_columns(window: list[GpuSample | None], adapter: str | None) -> Values:
    """game_gpu_pct = the game's busiest engine type on its adapter; gpu_pct / gpu_other_pct = that engine
    type on that adapter for everyone / everyone but the game. Mean of the window's valid samples, unclamped,
    then clamped to 100."""
    none = {S.GAME_GPU_PCT: None, S.GPU_PCT: None, S.GPU_OTHER_PCT: None}
    valid = [s for s in window if s is not None]
    if not valid or adapter is None:
        return none
    types = {typ for s in valid for (luid, typ) in (*s.game, *s.total) if luid == adapter}
    if not types:
        return none
    n = len(valid)

    def mean(part: str, typ: str) -> float:
        return sum(getattr(s, part).get((adapter, typ), 0.0) for s in valid) / n

    best = max(types, key=lambda typ: (mean("game", typ), tgt.is_3d(typ), typ))
    return {S.GAME_GPU_PCT: min(100.0, mean("game", best)),
            S.GPU_PCT: min(100.0, mean("total", best)),
            S.GPU_OTHER_PCT: min(100.0, mean("other", best))}


def pid_gpu_pct(window: list[GpuSample | None], pid: int) -> float | None:
    valid = [s for s in window if s is not None]
    if not valid:
        return None
    return min(100.0, sum(s.per_pid.get(pid, 0.0) for s in valid) / len(valid))


def pid_gpu_pct_on(window: list[GpuSample | None], pid: int, adapter: str | None) -> float | None:
    """A process's busiest engine on `adapter` (the game's), averaged like game_gpu_pct: unclamped samples
    over the trailing window, then the mean clamped to 100. None when the adapter is unknown, or when the
    process has no engine on it in the window (it may use another GPU: that is not this GPU's load)."""
    valid = [s for s in window if s is not None]
    if adapter is None or not any((pid, adapter) in s.per_pid_adapter for s in valid):
        return None
    return min(100.0, sum(s.per_pid_adapter.get((pid, adapter), 0.0) for s in valid) / len(valid))


def _sum_matching(values: dict[str, float | None], match: Callable[[str], bool]) -> float | None:
    got = [v for inst, v in values.items() if match(inst)]
    if not got or any(v is None for v in got):
        return None
    return float(sum(got))


def gpu_memory_columns(adapter_ded: dict, proc_ded: dict, proc_shared: dict, game_pid: int,
                       adapter: str | None) -> Values:
    out: Values = {S.VRAM_USED_MB: None, S.GAME_VRAM_MB: None, S.GAME_SHARED_MB: None}
    if adapter is None:
        return out

    def on_adapter(inst: str) -> bool:
        p = tgt.parse_adapter_memory(inst)
        return p is not None and p[0] == adapter

    def game_on_adapter(inst: str) -> bool:
        p = tgt.parse_process_memory(inst)
        return p is not None and p[0] == game_pid and p[1] == adapter

    vram = _sum_matching(adapter_ded, on_adapter)
    out[S.VRAM_USED_MB] = vram / MIB if vram is not None else None
    for col, values in ((S.GAME_VRAM_MB, proc_ded), (S.GAME_SHARED_MB, proc_shared)):
        if values:          # a valid read in which the game holds nothing on its adapter means 0 MB
            got = _sum_matching(values, game_on_adapter)
            any_mine = any(game_on_adapter(i) for i in values)
            out[col] = (got / MIB) if got is not None else (None if any_mine else 0.0)
    return out


# ---------------------------------------------------------------- processes ---

GAME_COLUMNS = (S.GAME_CPU_PCT, S.GAME_TOP_THREAD_PCT, S.GAME_TOP_THREAD_ID, S.GAME_THREADS, S.GAME_WS_MB,
                S.GAME_PRIVATE_MB, S.GAME_PAGE_FAULTS_PS, S.GAME_IO_READ_BPS, S.GAME_IO_WRITE_BPS, S.GAME_IO_OPS_PS)


def _cpu_total(p: ProcInfo) -> int:
    return p.user_100ns + p.kernel_100ns


class ProcessRates:
    """Rates between consecutive snapshots. A process or thread missing from the previous snapshot started
    since then, so all of its time falls inside the interval; one whose create time changed is a new one
    behind a reused id."""

    def __init__(self, pid: int, create_time: int) -> None:
        self.pid, self.create_time = pid, create_time
        self._prev: dict[int, ProcInfo] | None = None
        self._prev_ns = 0
        self._prev_threads: dict[tuple[int, int], int] | None = None

    def update(self, procs: dict[int, ProcInfo] | None, t_ns: int) -> tuple[Values, list[tuple] | None]:
        """(game_* columns, [(pid, name, cpu_pct, io_bps), ...] for every other process, or None)."""
        cols: Values = dict.fromkeys(GAME_COLUMNS)
        if procs is None:
            self._prev, self._prev_threads = None, None
            return cols, None
        prev, prev_ns, prev_threads = self._prev, self._prev_ns, self._prev_threads
        game = procs.get(self.pid)
        if game is not None and game.create_time != self.create_time:
            game = None
        threads_now = None
        if game is not None and game.threads is not None:
            threads_now = {(t.tid, t.create_time): t.user_100ns + t.kernel_100ns for t in game.threads}
        self._prev, self._prev_ns, self._prev_threads = procs, t_ns, threads_now

        if game is not None:
            cols[S.GAME_THREADS] = game.thread_count
            cols[S.GAME_WS_MB] = game.ws_bytes / MIB
            cols[S.GAME_PRIVATE_MB] = game.private_bytes / MIB
        if prev is None or t_ns <= prev_ns:
            return cols, None
        dt_100ns = (t_ns - prev_ns) / 100.0
        dt_s = dt_100ns / 1e7

        if game is not None:
            old = prev.get(self.pid)
            if old is not None and old.create_time != self.create_time:
                old = None
            d_cpu = _cpu_total(game) - (_cpu_total(old) if old else 0)
            if d_cpu >= 0:
                cols[S.GAME_CPU_PCT] = 100.0 * d_cpu / dt_100ns
            for col, now_v, old_v in (
                    (S.GAME_PAGE_FAULTS_PS, game.page_faults, old.page_faults if old else 0),
                    (S.GAME_IO_READ_BPS, game.read_bytes, old.read_bytes if old else 0),
                    (S.GAME_IO_WRITE_BPS, game.write_bytes, old.write_bytes if old else 0),
                    (S.GAME_IO_OPS_PS, game.read_ops + game.write_ops,
                     (old.read_ops + old.write_ops) if old else 0)):
                if now_v >= old_v:
                    cols[col] = (now_v - old_v) / dt_s
            if threads_now is not None and (old is None or prev_threads is not None):
                base = prev_threads or {}
                top = None
                for key, total in threads_now.items():
                    delta = total - base.get(key, 0)
                    if delta < 0:
                        continue
                    pct = min(100.0, 100.0 * delta / dt_100ns)     # one thread cannot exceed one CPU
                    if top is None or pct > top[1]:
                        top = (key[0], pct)
                if top is not None:
                    cols[S.GAME_TOP_THREAD_ID], cols[S.GAME_TOP_THREAD_PCT] = top

        others = []
        for p in procs.values():
            if p.pid == 0 or (p.pid == self.pid and p.create_time == self.create_time):
                continue                       # pid 0 is the idle process: its "CPU" is idle time
            old = prev.get(p.pid)
            if old is not None and old.create_time != p.create_time:
                old = None
            d_cpu = _cpu_total(p) - (_cpu_total(old) if old else 0)
            d_io = (p.read_bytes + p.write_bytes) - ((old.read_bytes + old.write_bytes) if old else 0)
            if d_cpu < 0 or d_io < 0:
                continue
            others.append((p.pid, p.name, 100.0 * d_cpu / dt_100ns, d_io / dt_s, p.create_time))
        return cols, others


def top_others(rates: list[tuple], gpu_window: list[GpuSample | None], adapter: str | None,
               n: int = TOP_OTHERS) -> list[list]:
    """The others row: the top `n` by CPU among processes that did anything,
    as [pid, name, cpu, io, gpu, create_time, gpu_game]. gpu is the busiest engine on any adapter;
    gpu_game the busiest on `adapter`, the game's (pid_gpu_pct_on: None when that adapter is unknown or the
    process has no engine there). The create time (raw FILETIME ticks) lets a fix on that process prove
    later that the PID was not reused."""
    rows = []
    for pid, name, cpu, io, created in rates:
        gpu = pid_gpu_pct(gpu_window, pid)
        if cpu > 0 or io > 0 or (gpu or 0) > 0:
            rows.append((cpu, io, gpu or 0.0, pid, name, gpu, created, pid_gpu_pct_on(gpu_window, pid, adapter)))
    best = heapq.nlargest(n, rows, key=lambda r: (r[0], r[1], r[2], -r[3]))
    return [[pid, name, round(cpu, 2), round(io, 1), None if gpu is None else round(gpu, 2), created,
             None if game_gpu is None else round(game_gpu, 2)]
            for cpu, io, _g, pid, name, gpu, created, game_gpu in best]


# ---------------------------------------------------------------- the collector ---

class CostStats:
    """Per-tick cost in a fixed histogram (0.25 ms bins), so a long capture costs no extra memory."""

    BIN_MS = 0.25
    BINS = 2000

    def __init__(self) -> None:
        self.counts = [0] * (self.BINS + 1)
        self.n = 0
        self.max_ms = 0.0
        self.total_ms = 0.0

    def add(self, ms: float) -> None:
        self.counts[min(self.BINS, int(ms / self.BIN_MS))] += 1
        self.n += 1
        self.total_ms += ms
        self.max_ms = max(self.max_ms, ms)

    def median_ms(self) -> float | None:
        if not self.n:
            return None
        half, run = (self.n + 1) / 2, 0
        for i, c in enumerate(self.counts):
            run += c
            if run >= half:
                return (i + 0.5) * self.BIN_MS
        return self.max_ms

    def summary(self) -> dict:
        return {"ticks": self.n, "median_ms": self.median_ms(), "max_ms": round(self.max_ms, 2),
                "mean_ms": round(self.total_ms / self.n, 2) if self.n else None}


class SystemSampler(Collector):
    """Collector "system": S columns at 4 Hz, others, the game's adapter and its process tree."""

    name = "system"

    def __init__(self, target: dict, *, period_s: float = PERIOD_S,
                 pdh_factory: Callable[[list[str]], Any] = PdhQuery,
                 snapshotter: Any = None,
                 foreground: Callable[[], int | None] = windows.foreground_pid,
                 layout_check: Callable[[Any], None] = ntquery.self_check) -> None:
        super().__init__()
        pid, create_time = target.get("pid"), target.get("create_time")
        tgt._check_identity(pid, create_time)
        if not 0.05 <= period_s <= 5.0:
            raise ValueError("period_s must be between 0.05 and 5 seconds")
        self.target = dict(target)
        self.pid, self.create_time = pid, create_time
        self._period = period_s
        self._pdh_factory = pdh_factory
        self._snapshotter = snapshotter
        self._foreground = foreground
        self._layout_check = layout_check
        self.series = SeriesBuilder(S.ALL)
        self.others: list[list] = []
        self.cost = CostStats()
        self.problems: list[str] = []
        self._pids_seen: set[int] = {pid}
        self._luid_3d: dict[str, float] = {}
        self._engine_names: dict[str, tuple | None] = {}
        self._cpu_window: deque = deque(maxlen=WINDOW_SAMPLES)
        self._gpu_window: deque = deque(maxlen=WINDOW_SAMPLES)
        self._rates = ProcessRates(pid, create_time)
        self._read_failures: dict[str, int] = {}
        self._snapshot_failures = 0
        self._last_snapshot_error = ""
        self._last_collect_error = ""
        self._rows = 0
        self._warm = False
        self._game_gone_at: float | None = None
        self._thread_cpu_s = 0.0
        self._run_wall_s = 0.0
        self._setup_s = 0.0

    # ---- results ---------------------------------------------------------
    def game_adapter_luid(self) -> str | None:
        used = {k: v for k, v in self._luid_3d.items() if v > 0}
        if used:
            return max(used, key=lambda k: (used[k], k))
        return self.target.get("adapter_luid")

    def result(self) -> dict[str, Any]:
        return {"columns": self.series.build(), "others": [list(r) for r in self.others],
                "game_adapter_luid": self.game_adapter_luid(), "game_pids": sorted(self._pids_seen),
                "cost": self.cost.summary()}

    # ---- the loop --------------------------------------------------------
    def run(self) -> None:
        setup0 = time.perf_counter()
        query = None
        try:
            query = self._pdh_factory(list(PDH_PATHS))
            for path, st in query.add_errors.items():
                self.problems.append(f"counter {path} is unavailable ({status_name(st)})")
        except OSError as exc:
            self.problems.append(f"performance counters are unavailable: {exc}")
        snap = self._snapshotter or ntquery.Snapshotter()
        try:
            self._layout_check(snap)
        except (OSError, RuntimeError) as exc:
            snap = None
            self.problems.append(f"process data is unavailable: {exc}")
        if query is None and snap is None:
            self.set_status("failed", "; ".join(self.problems), tool=TOOL)
            return
        self.set_status("partial" if self.problems else "ok", "; ".join(self.problems), tool=TOOL)
        # The first PDH query in a process loads the counter providers (about 0.65 s here, once):
        # report it apart from the steady per-tick cost.
        self._setup_s = time.perf_counter() - setup0
        cpu0, wall0 = time.thread_time(), time.perf_counter()
        try:
            for _ in self.ticks(self._period):
                t0 = time.perf_counter()
                self._tick(query, snap)
                ms = (time.perf_counter() - t0) * 1000.0
                self.cost.add(ms)
                self.publish(tick_ms=round(ms, 2))
        finally:
            if query is not None:
                query.close()
            self._thread_cpu_s = time.thread_time() - cpu0
            self._run_wall_s = time.perf_counter() - wall0
            self._final_status()

    def _read_all(self, query) -> dict[str, dict] | None:
        try:
            query.collect()
        except OSError as exc:
            self._read_failures["(collect)"] = self._read_failures.get("(collect)", 0) + 1
            self._last_collect_error = str(exc)
            return None
        out = {}
        for path in PDH_PATHS:
            if path in query.add_errors:
                out[path] = {}
                continue
            out[path] = query.read(path)
            if self._warm and not out[path]:
                self._read_failures[path] = self._read_failures.get(path, 0) + 1
        return out

    def _tick(self, query, snap) -> None:
        pdh = self._read_all(query) if query is not None else None
        procs = None
        a = time.perf_counter_ns()
        if snap is not None:
            try:
                procs = snap.snapshot(threads_for=(self.pid,))
            except OSError as exc:
                self._snapshot_failures += 1
                self._last_snapshot_error = str(exc)
        t_ns = (a + time.perf_counter_ns()) // 2
        t = t_ns / 1e9 - self.origin
        v = pdh if pdh is not None else {}

        def get(path: str) -> dict:
            return v.get(path) or {}

        self._cpu_window.append(cpu_busy(get(P_USER), get(P_PRIV)))
        engine = get(G_ENGINE)
        if engine:       # parsed names of the processes alive now: the cache never grows without bound
            self._engine_names = {n: self._engine_names[n] if n in self._engine_names else tgt.parse_engine(n)
                                  for n in engine}
        sample = gpu_sample(engine, self.pid, self._engine_names)
        self._gpu_window.append(sample)
        if sample is not None:
            for (luid, typ), val in sample.game.items():
                if tgt.is_3d(typ):
                    self._luid_3d[luid] = self._luid_3d.get(luid, 0.0) + val
        game_cols, rates = self._rates.update(procs, t_ns)
        alive = None
        if procs is not None:
            game = procs.get(self.pid)
            alive = game is not None and game.create_time == self.create_time
            if alive:
                for p in tgt.descendants(procs, self.pid, self.create_time):
                    if len(self._pids_seen) >= MAX_GAME_PIDS:
                        break
                    self._pids_seen.add(p)
            if not alive and self._game_gone_at is None:
                self._game_gone_at = t
        if not self._warm:
            self._warm = True               # the first tick is only the baseline for every delta
            return

        focused = None
        if alive is not False:
            try:
                fg = self._foreground()
                focused = 1 if fg == self.pid else 0
            except OSError:
                focused = None
        window = list(self._gpu_window)
        adapter = choose_adapter(self._luid_3d, self.target.get("adapter_luid"), window)
        row: Values = {S.T: t, S.GAME_FOCUSED: focused}
        row.update(cpu_load_columns(list(self._cpu_window)))
        row.update(cpu_state_columns(get(P_PERF), get(P_LIMIT), get(T_PASSIVE), get(P_DPC), get(P_ISR)))
        row.update(game_cols)
        row.update(gpu_engine_columns(window, adapter))
        row.update(gpu_memory_columns(get(G_ADAPTER_DED), get(G_PROC_DED), get(G_PROC_SHARED), self.pid, adapter))
        row.update(memory_columns(get(M_AVAIL), get(M_COMMITTED), get(M_LIMIT), get(M_PAGES_IN)))
        row.update(disk_columns(get(D_IDLE), get(D_READ_S), get(D_READ_BPS), get(D_QUEUE)))
        self.series.add(**row)
        self._rows += 1
        if rates is not None:
            # the same adapter as game_gpu_pct, so gpu_game says what else loaded the game's own GPU
            self.others.append([t, top_others(rates, window, adapter)])
        temps = [k for k in get(T_TEMP).values() if k is not None]
        self.publish(t=t, cpu_total_pct=row[S.CPU_TOTAL_PCT], cpu_max_core_pct=row[S.CPU_MAX_CORE_PCT],
                     game_cpu_pct=row[S.GAME_CPU_PCT], game_top_thread_pct=row[S.GAME_TOP_THREAD_PCT],
                     game_gpu_pct=row[S.GAME_GPU_PCT], gpu_pct=row[S.GPU_PCT], ram_avail_mb=row[S.RAM_AVAIL_MB],
                     disk_busy_pct=row[S.DISK_BUSY_PCT], game_alive=alive, game_focused=row.get(S.GAME_FOCUSED),
                     thermal_zone_c=(max(temps) - 273.15) if temps else None,
                     perf_limit_flags=single(get(P_LIMIT_FLAGS)), adapter_luid=adapter)

    def _final_status(self) -> None:
        notes = list(self.problems)
        med = self.cost.median_ms()
        cost = (f"{self._rows} rows at {1 / self._period:g} Hz; median {med:.1f} ms per tick "
                f"(max {self.cost.max_ms:.1f} ms)" if med is not None else "no ticks ran")
        if self._run_wall_s > 0:
            cost += (f"; sampler thread CPU {100 * self._thread_cpu_s / self._run_wall_s:.1f} % of one CPU "
                     f"after a {self._setup_s:.2f} s setup")
        degraded = bool(self.problems)
        warm_ticks = max(0, self.cost.n - 1)
        for path, n in sorted(self._read_failures.items()):
            if path == "(collect)":
                notes.append(f"PDH collect failed in {n} of {self.cost.n} ticks ({self._last_collect_error})")
                degraded = degraded or n >= self.cost.n
                continue
            notes.append(f"{path} gave no reading in {n} of {warm_ticks} ticks")
            if warm_ticks and n >= warm_ticks:
                degraded = True
        if self._snapshot_failures:
            notes.append(f"the process snapshot failed in {self._snapshot_failures} of {self.cost.n} ticks "
                         f"({self._last_snapshot_error})")
            degraded = degraded or self._snapshot_failures >= self.cost.n
        if self._game_gone_at is not None:
            notes.append(f"the game exited at t={self._game_gone_at:.1f} s; game columns are empty after that")
        self.set_status("partial" if degraded else "ok", "; ".join([cost] + notes), tool=TOOL)
