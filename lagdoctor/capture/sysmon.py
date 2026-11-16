"""The system sampler: CPU, GPU, memory and disk 4 times a second, for the machine and for the game.

Design: docs/research/windows-telemetry.md (WT) section 11, as corrected by its verification log. Each tick
runs one PDH collect (the WT 11.1 counter list) and one NtQuerySystemInformation snapshot with the game's
threads, and writes one row of S columns plus an `others` row (the top 8 other processes by CPU, then the top 4 of
the rest by reads and writes, each with its Windows session and whether its program sits in System32, or for
explorer.exe in the Windows folder itself).

- Process and thread CPU are the delta since the previous tick (about 250 ms), kept sharp so a short hitch
  shows in the tick that holds it. Thread times move in 15.625 ms steps (WT 3.5), so one tick is only good
  to about 6 % of a core: the analysis judges saturation over 1 s or more. Each thread is clamped to 100.
  game_top_thread_pct is each tick's busiest thread (for lag events); game_lead_thread_pct is this tick's share of
  the thread that had the most CPU over the previous second (for the bound), since the busiest of several threads
  about as busy reads high in any one tick.
- Identity is (pid, create_time) and (tid, create_time): PIDs and TIDs are reused.
- The game_* columns are the target process only; its child processes count as others.
- GPU Engine values stay unclamped per sample, are averaged over the trailing 1 s, and only the average is
  clamped (single samples reached 3,652 %, WT verify log 13). An others row gives each process's busiest
  engine on any adapter (field 4) and on the game's adapter (field 6, None if it has no engine there).
- A failed read is None in the row, never 0. Nothing here opens a handle to any process.
"""
from __future__ import annotations

import heapq
import ntpath
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from lagdoctor.capture import target as tgt
from lagdoctor.capture.base import Collector
from lagdoctor.model import S, SeriesBuilder
from lagdoctor.winapi import folders as winfolders
from lagdoctor.winapi import ntquery, windows
from lagdoctor.winapi.ntquery import ProcInfo
from lagdoctor.winapi.pdh import PdhQuery, status_name

PERIOD_S = 0.25
WINDOW_SAMPLES = 4            # the trailing 1 s at 4 Hz
TOP_OTHERS = 8
TOP_OTHERS_IO = 4            # besides the 8 by CPU, the 4 busiest by reads and writes: a disk user with little CPU
TOP_OTHERS_MEM = 4           # then the 4 of the rest holding the most RAM, the 4 with the most memory committed and the
                             # 4 with the most video memory on the game's GPU: a program can hold memory while idle
MEMORY_EVERY_S = 1.0         # ... read once a second: memory moves slowly, and 12 more entries a tick cost RAM
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


def pid_vram(proc_ded: dict, adapter: str | None,
             parse: Callable[[str], tuple[int, str, int] | None] = tgt.parse_process_memory
             ) -> dict[int, float | None] | None:
    """Each process's dedicated video memory on `adapter` (bytes; GPU Process Memory), for the others row: None when
    it cannot say for any process (the adapter is unknown, or the counter gave nothing), a process's None when one of
    its readings there failed. A process the read did not list holds none there: 0, as GAME_VRAM_MB counts it; a read
    that lists nobody on the adapter cannot say (a stale adapter after a driver reset, or names it cannot parse)."""
    if adapter is None:
        return None
    out: dict[int, float | None] = {}
    for inst, v in proc_ded.items():
        p = parse(inst)
        if p is None or p[1] != adapter:
            continue
        old = out.get(p[0], 0.0)
        out[p[0]] = None if v is None or old is None else old + float(v)
    return out or None


# ---------------------------------------------------------------- processes ---

GAME_COLUMNS = (S.GAME_CPU_PCT, S.GAME_TOP_THREAD_PCT, S.GAME_TOP_THREAD_ID, S.GAME_LEAD_THREAD_PCT, S.GAME_THREADS,
                S.GAME_WS_MB,
                S.GAME_PRIVATE_MB, S.GAME_PAGE_FAULTS_PS, S.GAME_IO_READ_BPS, S.GAME_IO_WRITE_BPS, S.GAME_IO_OPS_PS)


def _cpu_total(p: ProcInfo) -> int:
    return p.user_100ns + p.kernel_100ns


def _lead_thread(hist: Iterable[dict[tuple[int, int], int]],
                 deltas: dict[tuple[int, int], int]) -> tuple[int, int] | None:
    """The thread with the most CPU over the previous intervals (`hist`, up to 1 s) that is still running, chosen
    before this interval is read, so its share here is a fair reading of it. Each interval's busiest thread is not:
    of several threads about as busy it is whichever the 15.625 ms clock ticks favoured (WT 3.5), so four threads at
    75 % read 86 %. Ties go to the lowest (tid, create time)."""
    total: dict[tuple[int, int], int] = {}
    for d in hist:
        for k, v in d.items():
            total[k] = total.get(k, 0) + v
    alive = [k for k in total if k in deltas]
    return min(alive, key=lambda k: (-total[k], k)) if alive else None


class ProcessRates:
    """Rates between consecutive snapshots. A process or thread missing from the previous snapshot started
    since then, so all of its time falls inside the interval; one whose create time changed is a new one
    behind a reused id."""

    def __init__(self, pid: int, create_time: int) -> None:
        self.pid, self.create_time = pid, create_time
        self._prev: dict[int, ProcInfo] | None = None
        self._prev_ns = 0
        self._prev_threads: dict[tuple[int, int], int] | None = None
        self._thread_hist: deque[dict[tuple[int, int], int]] = deque(maxlen=WINDOW_SAMPLES)   # recent deltas

    def update(self, procs: dict[int, ProcInfo] | None, t_ns: int) -> tuple[Values, list[tuple] | None]:
        """(game_* columns, [(pid, name, cpu_pct, io_bps, create_time, session_id, private working set bytes or None,
        committed bytes), ...] for every other process, or None)."""
        cols: Values = dict.fromkeys(GAME_COLUMNS)
        if procs is None:
            self._prev, self._prev_threads = None, None
            self._thread_hist.clear()
            return cols, None
        prev, prev_ns, prev_threads = self._prev, self._prev_ns, self._prev_threads
        game = procs.get(self.pid)
        if game is not None and game.create_time != self.create_time:
            game = None
        threads_now = None
        if game is not None and game.threads is not None:
            threads_now = {(t.tid, t.create_time): t.user_100ns + t.kernel_100ns for t in game.threads}
        if threads_now is None:
            self._thread_hist.clear()
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
                deltas: dict[tuple[int, int], int] = {}
                for key, total in threads_now.items():
                    delta = total - base.get(key, 0)
                    if delta < 0:
                        continue
                    deltas[key] = delta
                    pct = min(100.0, 100.0 * delta / dt_100ns)     # one thread cannot exceed one CPU
                    if top is None or pct > top[1]:
                        top = (key[0], pct)
                if top is not None:
                    cols[S.GAME_TOP_THREAD_ID], cols[S.GAME_TOP_THREAD_PCT] = top
                lead = _lead_thread(self._thread_hist, deltas)
                if lead is not None:
                    cols[S.GAME_LEAD_THREAD_PCT] = min(100.0, 100.0 * deltas[lead] / dt_100ns)
                self._thread_hist.append(deltas)

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
            others.append((p.pid, p.name, 100.0 * d_cpu / dt_100ns, d_io / dt_s, p.create_time, p.session_id,
                           p.ws_private_bytes, p.private_bytes))
        return cols, others


def _mb(b: float | None) -> int | None:
    return None if b is None else round(b / MIB)


def top_others(rates: list[tuple], gpu_window: list[GpuSample | None], adapter: str | None,
               n: int = TOP_OTHERS, n_io: int = TOP_OTHERS_IO,
               in_system: Callable[[int, int, str], bool | None] = lambda pid, created, name: None,
               vram: dict[int, float | None] | None = None, n_mem: int = TOP_OTHERS_MEM,
               memory: bool = True) -> list[list]:
    """The others row: the top `n` by CPU among processes that did anything, then the top `n_io` of the rest by
    reads and writes (a program that loads the disk with little CPU), then of the rest the top `n_mem` by RAM, by
    memory committed and by video memory on the game's GPU (a program can hold memory while idle), as [pid, name,
    cpu, io, gpu, create_time, gpu_game, session, system_folder, ram_mb, commit_mb, vram_mb]. gpu is the busiest
    engine on any adapter; gpu_game the busiest on `adapter`, the game's (pid_gpu_pct_on: None when that adapter is
    unknown or the process has no engine there). The create time (raw FILETIME ticks) lets a fix on that process prove
    later that the PID was not reused. session is its Windows session (0: services and Windows' own); system_folder
    is `in_system(pid, create_time, name)` for the listed processes only: True when its program sits in Windows' own
    System32 or SysWOW64 folder, or is explorer.exe in the Windows folder itself (SystemFolders), None unread. ram_mb is its private working set (the RAM it alone holds, Task
    Manager's "Memory"), commit_mb the memory committed to it (its private bytes, which count toward Windows' commit
    limit), vram_mb its dedicated video memory on `adapter` (`vram`, from pid_vram: None when not read), each in whole
    MiB. Without `memory` (the sampler reads memory once a second) the row has no memory picks and fields 9-11 are
    None: not read at that tick."""
    rows = []
    for pid, name, cpu, io, created, session, ram, commit in rates:
        gpu = pid_gpu_pct(gpu_window, pid)
        held = None if vram is None else vram.get(pid, 0.0)
        rows.append((cpu, io, gpu or 0.0, pid, name, gpu, created, session, ram, commit, held,
                     cpu > 0 or io > 0 or (gpu or 0) > 0))
    active = [r for r in rows if r[11]]
    best = heapq.nlargest(n, active, key=lambda r: (r[0], r[1], r[2], -r[3]))
    listed = {r[3] for r in best}

    def add(k: int, pool: Iterable[tuple], key: Callable[[tuple], tuple]) -> None:
        got = heapq.nlargest(k, (r for r in pool if r[3] not in listed), key=key)
        best.extend(got)
        listed.update(r[3] for r in got)

    add(n_io, (r for r in active if r[1] > 0), lambda r: (r[1], r[0], -r[3]))
    if memory:
        for i in (8, 9, 10):            # RAM, committed memory, video memory: whoever holds some, busy or not
            add(n_mem, (r for r in rows if (r[i] or 0) > 0), lambda r, i=i: (r[i], -r[3]))
    out = []
    for cpu, io, _g, pid, name, gpu, created, session, ram, commit, held, _a in best:
        game_gpu = pid_gpu_pct_on(gpu_window, pid, adapter)
        held_mb = [_mb(ram), _mb(commit), _mb(held)] if memory else [None, None, None]
        out.append([pid, name, round(cpu, 2), round(io, 1), None if gpu is None else round(gpu, 2), created,
                    None if game_gpu is None else round(game_gpu, 2), session, in_system(pid, created, name),
                    *held_mb])
    return out


# The programs Windows runs from the Windows folder itself rather than System32, which SystemFolders counts there:
# its shell. rules.WINDOWS_FOLDER_PARTS is the same set (a test holds them equal); any other program directly in the
# Windows folder (a svchost.exe dropped there, say) reads False, as it did before 2026-11-16
WINDOWS_FOLDER_PROGRAMS = frozenset({"explorer.exe"})


class SystemFolders:
    """in_system for top_others: whether a process's program sits directly in one of Windows' own program folders
    (System32, SysWOW64), where Windows and its drivers keep theirs, or is one of WINDOWS_FOLDER_PROGRAMS (explorer.exe,
    Windows' shell) directly in the Windows folder itself (`windows_folder` says whether that was checked: since
    2026-11-16). Only an administrator writes to any of them; a folder below one (C:\\Windows\\Temp) is not it. Each
    process's path is read handle-free (ntquery.image_path); an answer is kept only while the process stays listed
    (`keep`), so the cache stays the size of an others row, and a failed read is never kept. None when it cannot say:
    the path could not be read (the process ended), is no drive path (System, Registry and Memory Compression have no
    program file; a volume Windows could not name), names another program than the snapshot did (its PID was reused
    since), or the folders are unknown (`problem` says why). `folders` and `windows` are for tests: by default both are
    Windows' own answers."""

    def __init__(self, folders: Iterable[str] | None = None,
                 path_of: Callable[[int], str | None] = ntquery.image_path, windows: str | None = None) -> None:
        self.problem = ""
        if folders is None:
            try:
                folders = winfolders.system_folders()
            except OSError as exc:
                folders = ()
                self.problem = (f"Windows' System32 folder could not be read ({exc}), so no process was checked "
                                "against it")
            if folders:
                try:
                    windows = winfolders.windows_folder()
                except OSError as exc:
                    self.problem = (f"the Windows folder could not be read ({exc}), so explorer.exe was judged by its "
                                    "name alone")
        folders = [f for f in folders if f]
        self.windows_folder = bool(folders and windows)     # the analysis judges explorer.exe by where it runs
        self._folders = frozenset(ntpath.normcase(ntpath.normpath(f)) for f in folders)
        self._windows = ntpath.normcase(ntpath.normpath(windows)) if self.windows_folder else None
        self._path_of = path_of
        self._seen: dict[tuple[int, int], bool] = {}

    def __call__(self, pid: int, created: int, name: str | None = None) -> bool | None:
        key = (pid, created)
        if key in self._seen:
            return self._seen[key]
        got = self._read(pid, name)
        if got is not None:
            self._seen[key] = got
        return got

    def _read(self, pid: int, name: str | None) -> bool | None:
        if not self._folders:
            return None
        try:
            path = self._path_of(pid)
        except (OSError, ValueError):
            return None
        if not isinstance(path, str) or not ntpath.splitdrive(path)[0] or not ntpath.isabs(path):
            return None
        path = ntpath.normpath(path)
        if name is not None and ntpath.basename(path).casefold() != name.casefold():
            return None
        folder = ntpath.normcase(ntpath.dirname(path))
        return folder in self._folders or (folder == self._windows
                                           and ntpath.basename(path).casefold() in WINDOWS_FOLDER_PROGRAMS)

    def keep(self, row: list[list]) -> None:
        """Forget every process this others row does not list."""
        listed = {(p[0], p[5]) for p in row}
        self._seen = {k: v for k, v in self._seen.items() if k in listed}


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
                 layout_check: Callable[[Any], None] = ntquery.self_check,
                 system_folders: SystemFolders | None = None, memory_every_s: float = MEMORY_EVERY_S) -> None:
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
        self._mem_names: dict[str, tuple | None] = {}    # GPU Process Memory instance -> parse_process_memory
        self._mem_every = max(1, round(memory_every_s / period_s))      # ticks between memory readings
        self._mem_tick = 0
        self._mem_listed: list[list] = []        # the last memory tick's row: its folder answers stay cached
        self._cpu_window: deque = deque(maxlen=WINDOW_SAMPLES)
        self._gpu_window: deque = deque(maxlen=WINDOW_SAMPLES)
        self._rates = ProcessRates(pid, create_time)
        self._system = system_folders if system_folders is not None else SystemFolders()
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
                "cost": self.cost.summary(), "windows_folder": self._system.windows_folder}

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
            memory = self._mem_tick % self._mem_every == 0
            self._mem_tick += 1
            vram = None
            if memory:
                proc_ded = get(G_PROC_DED)
                if proc_ded:        # parsed names of the instances read now: the cache never grows without bound
                    self._mem_names = {n: self._mem_names[n] if n in self._mem_names else tgt.parse_process_memory(n)
                                       for n in proc_ded}
                vram = pid_vram(proc_ded, adapter, self._mem_names.__getitem__)
            listed = top_others(rates, window, adapter, in_system=self._system,
                                vram=vram, memory=memory)
            if memory:
                self._mem_listed = listed
            self._system.keep(listed + self._mem_listed)
            self.others.append([t, listed])
        temps = [k for k in get(T_TEMP).values() if k is not None]
        self.publish(t=t, cpu_total_pct=row[S.CPU_TOTAL_PCT], cpu_max_core_pct=row[S.CPU_MAX_CORE_PCT],
                     game_cpu_pct=row[S.GAME_CPU_PCT], game_top_thread_pct=row[S.GAME_TOP_THREAD_PCT],
                     game_gpu_pct=row[S.GAME_GPU_PCT], gpu_pct=row[S.GPU_PCT], ram_avail_mb=row[S.RAM_AVAIL_MB],
                     disk_busy_pct=row[S.DISK_BUSY_PCT], game_alive=alive, game_focused=row.get(S.GAME_FOCUSED),
                     thermal_zone_c=(max(temps) - 273.15) if temps else None,
                     perf_limit_flags=single(get(P_LIMIT_FLAGS)), adapter_luid=adapter)

    def _final_status(self) -> None:
        notes = list(self.problems) + ([self._system.problem] if self._system.problem else [])
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
