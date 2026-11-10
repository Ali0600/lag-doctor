"""Synthetic captures shaped like what lag-doctor's collectors record while LagLab runs.

`laglab(mode, ...)` runs a small model of LagLab's game loop (laglab/app.py: the same per-frame order of
work, hitch, draw, flip and frame-cap wait) and of what each collector would have seen:

- frames: PresentMon 2.6.0 rows with the OpenGL quirks LagLab shows (PM 8.2): runtime "Other", swap
  chain "0x0", SyncInterval -1, MsCPUWait 0 (so MsCPUBusy is the whole frame). LagLab is a window, so DWM
  composes it on the fixed-refresh panel: a frame reaches the screen on the first refresh after its GPU
  work is done, displayed intervals land on whole refresh periods, and a frame replaced before its refresh
  is dropped. PresentMon's first row per chain and its last displayed frame are not written (PM 2.4).
- system: one S row per ~250 ms tick. The game's thread CPU is accounted the way Windows does it, one
  15.625 ms clock tick at a time to whatever runs when the tick fires (WT 3.5), so a 250 ms reading is
  only good to about 6 %; GPU % is the trailing 1 s average (DESIGN, system).
- others: the top 8 non-game processes per tick, including this laptop's always-on ML training job
  (one saturated thread, 100 % of the NVIDIA GPU) as a steady confounder.
- network: one probe round every 0.5 s to the gateway and the internet anchor (DESIGN, network), with
  optional ping spikes, lost probes and server gaps (net_flows).

The ground truth is returned as a LagLab-shaped log (perf_counter seconds, clock_origin + capture t), so a
test scores events with scripts/e2e_truth.truth_moments() and match_events() exactly as the end-to-end
judge does. Every knob has a LagLab default; nothing here is tuned to make the analysis pass.

For unit tests that need exact values instead, `frames_table()`, `system_table()` and `capture()` build
hand-written tables through SeriesBuilder.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, field, fields
from typing import Any, Sequence

from lagdoctor.model import F, G, N, S, Capture, SeriesBuilder, SourceStatus

ORIGIN = 5000.0                   # perf_counter() at capture t = 0
REFRESH_HZ = 144.003              # this laptop's internal panel, QueryDisplayConfig rational (LC 3.3, L4)
ACCOUNT_TICK_S = 0.015625         # Windows charges thread CPU per 15.625 ms clock tick (WT 3.5)
SAMPLE_S = 0.25                   # system sampler (DESIGN: 4 Hz)
NET_ROUND_S = 0.5                 # network probe rounds (DESIGN: 2 Hz)
LOGICAL_CPUS = 16                 # i7-12650H (LC header)
INTEL_LUID = "0x00000000_0x0000a2c1"
NVIDIA_LUID = "0x00000000_0x0000b3d2"
GAME_PID, MAIN_TID, AUX_TID = 13576, 4242, 4250
TRAINING_JOB = (7777, "python.exe")   # the long-running ML training job (CLAUDE.md)
HITCH_MODES = ("hitch", "hitch-io", "hitch-wait")
MODES = ("smooth", "gpu", "cpu", "hitch", "hitch-io", "hitch-wait", "hog", "pace")
# Measured with laglab.app._wait_until at 120 FPS on this laptop, headless, ML job running: 3 of 1,197
# intervals woke more than 1.4 ms late (worst 19.2 ms). Off by default; see `hiccups`.
MEASURED_HICCUP_RATE = 3 / 1197


@dataclass
class Knobs:
    """LagLab's command-line defaults (laglab/app.py) plus what the model itself needs."""

    duration_s: float = 30.0      # capture length; LagLab runs from start_s to duration_s - tail_s
    start_s: float = 0.4
    tail_s: float = 0.2
    warmup_s: float = 3.0         # --warmup
    fps_cap: float = 120.0        # --fps-cap
    target_ms: float = 25.0       # --target-ms (gpu, cpu)
    hitch_ms: float = 150.0       # --hitch-ms
    every_s: float = 3.0          # --every
    io_reads: int = 1500          # --io-reads (4 KiB each)
    io_read_ms: tuple[float, float] = (0.08, 0.16)   # one uncached 4 KiB read on NVMe (model)
    hog_on_s: float = 6.0         # --hog-on
    hog_off_s: float = 6.0        # --hog-off
    hog_game_ms: float = 4.0      # --hog-game-ms
    hogs: int = 2 * LOGICAL_CPUS  # --hogs default: 2 per CPU
    pace_short_ms: float = 4.0    # --pace-short-ms
    pace_long_ms: float = 20.0    # --pace-long-ms
    overhead_ms: float = 0.6      # Python + pyglet + GL submission per frame (model)
    light_gpu_ms: float = 0.45    # the 8-iteration shader at 1280x720 on the Intel GPU (model)
    refresh_hz: float = REFRESH_HZ
    display: str = "composed"     # "composed" (a window, DWM) or "flip" (independent flip / VRR: no quantising)
    latch_ms: float = 1.0         # DWM needs the frame this long before the refresh it is shown on (model)
    hiccups: float = 0.0          # chance per frame that the cap's sleep wakes 1-11 ms late (MEASURED_HICCUP_RATE)
    seed: int = 1
    # what else happened during the capture
    with_frames: bool = True
    with_system: bool = True
    net_spikes: Sequence[tuple[float, float, float]] = ()   # (from, to, extra ms) on the internet anchor
    net_losses: Sequence[float] = ()                        # times of lost internet probes
    server_gaps: Sequence[tuple[float, float, str]] = ()    # net_flows gaps (t, ms, kind)
    marks: Sequence[float] = ()                             # hotkey presses
    unfocused: Sequence[tuple[float, float]] = ()           # spans when another window was in front
    decoy_chain: bool = False     # add a few rows of a second, mostly-dropped swap chain (PM 8.3)
    hags: bool | None = False     # HAGS on the game's (Intel) adapter: off on this laptop (PM V10)
    gpu_other_pct: float | None = None   # other programs on the game's adapter; None: the 1-3 % seen on the Intel GPU
                                         # (the training job's GPU is the NVIDIA one). 80: that job on the game's GPU


@dataclass
class Synth:
    cap: Capture
    log: dict[str, Any]           # LagLab-shaped ground truth (perf_counter seconds)
    truth: list[tuple[float, float]] = field(default_factory=list)   # lag moments on the capture clock
    knobs: Knobs = field(default_factory=Knobs)


def laglab(mode: str = "smooth", **kw: Any) -> Synth:
    """A capture of LagLab `mode` (laglab.app.MODES). Unknown knob names raise."""
    if mode not in MODES:
        raise ValueError(f"unknown LagLab mode {mode!r}; modes: {MODES}")
    names = {f.name for f in fields(Knobs)}
    bad = set(kw) - names
    if bad:
        raise TypeError(f"unknown knob(s) {sorted(bad)}")
    k = Knobs(**kw)
    rng = random.Random(f"{mode}/{k.seed}")
    game = _run_game(mode, k, rng)
    frames = _presentmon(game, k, rng) if k.with_frames else {}
    system, others = _system(game, k, rng) if k.with_system else ({}, [])
    network, net_flows = _network(k, rng)
    log = {"schema": "laglab.log/1", "mode": mode, "pid": GAME_PID, "gl_renderer": "Intel(R) UHD Graphics",
           "params": {"mode": mode, "fps_cap": k.fps_cap, "every": k.every_s, "hitch_ms": k.hitch_ms},
           "t_start": ORIGIN + k.start_s, "t_stop": ORIGIN + game.stop,
           "frames": [ORIGIN + f.start for f in game.frames],
           "hitches": game.hitches, "hog_bursts": [[ORIGIN + a, ORIGIN + b] for a, b in game.bursts]}
    sources = {"frames": SourceStatus("ok", tool="PresentMon 2.6.0") if k.with_frames else
               SourceStatus("unavailable", "PresentMon needs admin rights or Performance Log Users: access denied"),
               "system": SourceStatus("ok") if k.with_system else SourceStatus("failed", "sampler did not run"),
               "gpu": SourceStatus("off", "the game renders on the Intel GPU; NVML not started"),
               "network": SourceStatus("ok"),
               "net_flows": SourceStatus("ok") if k.server_gaps else
               SourceStatus("unavailable", "needs ETW rights")}
    cap = Capture(
        started_at="2026-11-04T18:00:00-08:00", clock_origin=ORIGIN, duration_s=k.duration_s,
        target={"pid": GAME_PID, "create_time": 134000000000000000, "name": "python.exe",
                "exe": r"C:\Python313\python.exe", "window_title": f"LagLab - {mode}",
                "monitor_device": r"\\.\DISPLAY1", "adapter_luid": INTEL_LUID, "how": "pid", "pids": [GAME_PID]},
        sources=sources, inventory=_inventory(k), frames=frames, system=system, network=network,
        others=others, net_flows=net_flows, marks=[float(m) for m in k.marks])
    truth = ([(h["start"] - ORIGIN, h["end"] - ORIGIN) for h in game.hitches] if game.hitches
             else list(game.bursts))
    return Synth(cap=cap, log=log, truth=truth, knobs=k)


# ------------------------------------------------------------------------ the game ---

@dataclass
class _Frame:
    start: float
    present: float
    gpu_start: float
    gpu_ready: float
    gpu_ms: float


@dataclass
class _Game:
    frames: list[_Frame]
    busy: list[tuple[float, float, float]]      # main-thread (from, to, share of that time it ran)
    reads: list[tuple[float, float, int]]       # uncached read bursts (from, to, bytes)
    hitches: list[dict[str, Any]]
    bursts: list[tuple[float, float]]
    stop: float


def _run_game(mode: str, k: Knobs, rng: random.Random) -> _Game:
    """LagLab's main loop, one frame at a time (laglab/app.py main())."""
    t, stop = k.start_s, k.duration_s - k.tail_s
    bursts: list[tuple[float, float]] = []
    if mode == "hog":
        a = k.start_s + k.warmup_s
        while a < stop:
            bursts.append((a, min(a + k.hog_on_s, stop)))
            a += k.hog_on_s + k.hog_off_s
    budget = 1.0 / k.fps_cap if k.fps_cap > 0 and mode not in ("gpu", "cpu") else 0.0
    next_hitch = t + k.warmup_s
    gpu_free = prev_ready = t
    pace_short = True
    frames: list[_Frame] = []
    busy: list[tuple[float, float, float]] = []
    reads: list[tuple[float, float, int]] = []
    hitches: list[dict[str, Any]] = []

    def run(seconds: float, share: float = 1.0) -> None:
        nonlocal t
        if seconds > 0:
            busy.append((t, t + seconds, share))
            t += seconds

    while t < stop:
        fs = t
        work = k.overhead_ms * rng.uniform(0.85, 1.15)
        if mode == "cpu":
            work += k.target_ms
        elif mode == "hog":
            work += k.hog_game_ms
        if any(a <= fs < b for a, b in bursts):
            # 2 hogs per logical CPU: the game's thread gets a third to a half of a core, and is now and
            # then left waiting through a whole hog quantum or two
            stretch = rng.uniform(2.0, 3.0)
            run(work * stretch / 1000.0, 1.0 / stretch)
            if rng.random() < 0.10:
                t += rng.uniform(15.0, 50.0) / 1000.0
        else:
            run(work / 1000.0)
        if mode in HITCH_MODES and fs >= next_hitch:
            h0 = t
            detail: dict[str, Any] = {}
            if mode == "hitch":
                run(k.hitch_ms / 1000.0)
            elif mode == "hitch-wait":
                t += k.hitch_ms / 1000.0
            else:
                dur = sum(rng.uniform(*k.io_read_ms) for _ in range(k.io_reads)) / 1000.0
                run(dur, 0.1)          # mostly blocked in ReadFile; the syscalls themselves cost ~10 %
                reads.append((h0, t, k.io_reads * 4096))
                detail["bytes"] = k.io_reads * 4096
            hitches.append({"start": ORIGIN + h0, "end": ORIGIN + t, "kind": mode, **detail})
            next_hitch += k.every_s
        if mode == "gpu":
            g_ms = k.target_ms * (1.0 + 0.07 * math.sin(2 * math.pi * fs / 2.3) + rng.gauss(0.0, 0.02))
        else:
            g_ms = k.light_gpu_ms * rng.uniform(0.9, 1.1)
        g_start = max(t, gpu_free)
        g_ready = g_start + max(0.05, g_ms) / 1000.0
        gpu_free = g_ready
        present = max(t, prev_ready)          # SwapBuffers waits until the GPU finished the previous frame
        t = present
        frames.append(_Frame(fs, present, g_start, g_ready, max(0.05, g_ms)))
        prev_ready = g_ready
        run(0.00015)                          # the flip itself
        if mode == "pace":
            deadline = fs + (k.pace_short_ms if pace_short else k.pace_long_ms) / 1000.0
            pace_short = not pace_short
        else:
            deadline = fs + budget if budget else None
        if deadline is not None and deadline > t:
            # laglab.app._wait_until: sleep until 1.5 ms before the deadline, then spin
            if deadline - t > 0.002:
                wake = deadline - 0.0015 + rng.uniform(0.00002, 0.0003)
                if rng.random() < k.hiccups:
                    wake += rng.uniform(0.001, 0.011)
                t = wake
                run(max(0.0, deadline - t))
            else:
                run(deadline - t)
    return _Game(frames, busy, reads, hitches, bursts, stop)


# --------------------------------------------------------------------- PresentMon ---

def _presentmon(game: _Game, k: Knobs, rng: random.Random) -> dict[str, list]:
    fs = game.frames
    period = 1.0 / k.refresh_hz
    phase = rng.uniform(0.0, period)
    screen: list[float | None] = []
    if k.display == "composed":
        slot = [math.ceil((f.gpu_ready + k.latch_ms / 1000.0 - phase) / period) for f in fs]
        for i, s in enumerate(slot):
            replaced = i + 1 < len(slot) and slot[i + 1] == s     # a newer frame made the same refresh
            screen.append(None if replaced else phase + s * period)
    else:
        screen = [f.gpu_ready + 0.0003 for f in fs]
    shown = [i for i, s in enumerate(screen) if s is not None]
    last = shown[-1] if shown else -1          # PresentMon holds the last displayed frame back (PM 2.4)

    b = SeriesBuilder(F.ALL)
    prev_disp = None
    for i in range(1, last):                   # the first present only seeds the chain (PM 2.4)
        f, p = fs[i], fs[i - 1]
        frame_ms = (f.present - p.present) * 1000.0
        row: dict[str, Any] = dict(
            t=f.present, frame_ms=frame_ms, cpu_busy_ms=frame_ms, cpu_wait_ms=0.0,
            gpu_latency_ms=(f.gpu_start - p.present) * 1000.0, gpu_busy_ms=f.gpu_ms, gpu_wait_ms=0.0,
            displayed=0 if screen[i] is None else 1, present_mode="Composed: Flip" if k.display == "composed"
            else "Hardware: Independent Flip", runtime="Other", sync_interval=-1, allows_tearing=0,
            swap_chain="0x0", frame_type="Application", hybrid=0)
        if screen[i] is not None:
            row["display_latency_ms"] = frame_ms + (screen[i] - f.present) * 1000.0
            if prev_disp is not None:
                row["display_ms"] = (screen[i] - screen[prev_disp]) * 1000.0
                # cpuStart = previous present (time in present is 0 for "Other")
                row["anim_error_ms"] = ((p.present - fs[prev_disp - 1].present)
                                        - (screen[i] - screen[prev_disp])) * 1000.0
            prev_disp = i
        b.add(**row)
    out = b.build()
    if k.decoy_chain and out[F.T]:
        # a launcher-style second chain: many rows, few of them displayed (PM 8.3)
        for j in range(0, len(out[F.T]), 7):
            for name in F.ALL:
                out[name].append(out[name][j] if name not in (F.SWAP_CHAIN, F.DISPLAYED, F.DISPLAY_MS) else None)
            out[F.SWAP_CHAIN][-1] = "0x1A2B3C4D5E"
            out[F.DISPLAYED][-1] = 1 if j % 70 == 0 else 0
    return out


# ------------------------------------------------------------------------- system ---

def _accounted(busy: list[tuple[float, float, float]], ticks: list[float], rng: random.Random,
               idle_share: float = 0.0) -> list[float]:
    """CPU seconds Windows charges to a thread between consecutive sample times (WT 3.5)."""
    if not ticks:
        return []
    phase = rng.uniform(0.0, ACCOUNT_TICK_S)
    charged = [0.0] * len(ticks)
    j, k = 0, 0
    tau = phase
    while tau <= ticks[-1]:
        while j < len(busy) and busy[j][1] <= tau:
            j += 1
        share = busy[j][2] if j < len(busy) and busy[j][0] <= tau < busy[j][1] else idle_share
        if share >= 1.0 or (share > 0 and rng.random() < share):
            while ticks[k] < tau:
                k += 1
            charged[k] += ACCOUNT_TICK_S
        tau += ACCOUNT_TICK_S
    return charged


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _system(game: _Game, k: Knobs, rng: random.Random) -> tuple[dict[str, list], list]:
    ticks = []
    x = SAMPLE_S
    while x <= k.duration_s:
        ticks.append(x + rng.uniform(0.0, 0.004))
        x += SAMPLE_S
    main = _accounted(game.busy, ticks, rng)
    aux = _accounted([], ticks, rng, idle_share=0.04)        # a driver / audio thread of the game
    ready = [f.gpu_ready for f in game.frames]
    b = SeriesBuilder(S.ALL)
    rows_others = []
    hist: list[tuple[float, float]] = []      # the alive intervals' (main, aux) %: the lead pick looks back 1 s
    for i, s in enumerate(ticks):
        prev = ticks[i - 1] if i else None
        row: dict[str, Any] = {"t": s}
        alive = k.start_s <= s <= game.stop + SAMPLE_S
        hog_on = any(a <= s - SAMPLE_S / 2 < bb for a, bb in game.bursts)
        others = _others_at(s, hog_on, k, rng)
        rows_others.append([s, others[:8]])
        others_cpu = sum(o[2] for o in others)
        if prev is not None and alive:
            dt = s - prev
            m_pct, a_pct = 100.0 * main[i] / dt, 100.0 * aux[i] / dt
            game_cpu = m_pct + a_pct
            row.update(game_top_thread_pct=min(100.0, max(m_pct, a_pct)),
                       game_top_thread_id=MAIN_TID if m_pct >= a_pct else AUX_TID, game_cpu_pct=game_cpu)
            if hist:              # sysmon._lead_thread: picked on the last 1 s, read this tick; ties to the lower tid
                back = hist[-4:]
                lead = m_pct if sum(h[0] for h in back) >= sum(h[1] for h in back) else a_pct
                row.update(game_lead_thread_pct=min(100.0, lead))
            hist.append((m_pct, a_pct))
            got = sum(_overlap(prev, s, r0, r1) / (r1 - r0) * n for r0, r1, n in game.reads if r1 > r0)
            in_io = sum(_overlap(prev, s, r0, r1) for r0, r1, _ in game.reads) / dt
            noise = 4096.0 * rng.choice([0, 0, 0, 1])
            row.update(game_io_read_bps=(got + noise) / dt, game_io_write_bps=rng.choice([0.0, 0.0, 8192.0]),
                       game_io_ops_ps=(got / 4096.0 + noise / 4096.0) / dt,
                       disk_busy_pct=min(100.0, rng.uniform(0.5, 3.0) + 85.0 * in_io),
                       disk_read_bps=(got + noise + rng.uniform(0, 60e3)) / dt,
                       disk_read_ms=rng.uniform(0.08, 0.2), disk_queue=1.0 if in_io > 0.5 else 0.0)
        else:
            game_cpu = 0.0
            hist.clear()
        busy_gpu = 0.0
        for f in game.frames[bisect.bisect_right(ready, s - 1.0):]:
            if f.gpu_start >= s:
                break
            busy_gpu += _overlap(s - 1.0, s, f.gpu_start, f.gpu_ready)
        game_gpu = min(100.0, 100.0 * busy_gpu / 1.0)      # trailing 1 s average (DESIGN, system)
        gpu_other = rng.uniform(1.0, 3.0)
        if k.gpu_other_pct is not None:                    # the default draws above are unchanged by the knob
            gpu_other = k.gpu_other_pct * rng.uniform(0.97, 1.03)
        focused = 0 if any(a <= s <= bb for a, bb in k.unfocused) else 1
        system_pct = rng.uniform(15.0, 35.0)
        row.update(cpu_total_pct=min(100.0, (game_cpu + others_cpu + system_pct) / LOGICAL_CPUS),
                   cpu_max_core_pct=min(100.0, max(row.get("game_top_thread_pct") or 0.0, 100.0 if hog_on else
                                                   rng.uniform(25.0, 40.0))),
                   cpu_perf_pct=rng.uniform(101.0, 107.0), cpu_perf_limit_pct=100.0, thermal_limit_pct=100.0,
                   dpc_pct=rng.uniform(0.3, 1.0), interrupt_pct=rng.uniform(0.2, 0.7),
                   game_threads=12 if alive else None, game_ws_mb=212.0 + rng.uniform(-1, 1) if alive else None,
                   game_private_mb=164.0 if alive else None,
                   game_page_faults_ps=rng.uniform(10.0, 60.0) if alive else None,
                   game_focused=focused if alive else None, game_gpu_pct=game_gpu if alive else None,
                   gpu_pct=game_gpu + gpu_other, gpu_other_pct=gpu_other, vram_used_mb=810.0 + rng.uniform(-5, 5),
                   game_vram_mb=64.0 if alive else None, game_shared_mb=140.0 if alive else None,
                   ram_avail_mb=14500.0 + rng.uniform(-40, 40), commit_pct=62.0 + rng.uniform(-0.3, 0.3),
                   hard_faults_ps=rng.uniform(0.0, 12.0))
        b.add(**row)
    return b.build(), rows_others


def _others_at(s: float, hog_on: bool, k: Knobs, rng: random.Random) -> list[list]:
    """Non-game processes at one tick, busiest first: [pid, name, cpu_pct, io_bps, gpu_pct]."""
    procs = [[TRAINING_JOB[0], TRAINING_JOB[1], rng.uniform(96.0, 104.0), 0.0, 100.0],
             [1844, "MsMpEng.exe", rng.uniform(0.2, 1.8), rng.uniform(0, 2e4), 0.0],
             [2656, "dwm.exe", rng.uniform(2.0, 4.5), 0.0, rng.uniform(2.0, 4.0)],
             [5120, "explorer.exe", rng.uniform(0.0, 0.6), 0.0, 0.0],
             [9100, "Code.exe", rng.uniform(0.5, 2.5), rng.uniform(0, 5e3), 0.3],
             [612, "svchost.exe", rng.uniform(0.1, 0.9), rng.uniform(0, 1e4), 0.0],
             [14000, "msedgewebview2.exe", rng.uniform(0.3, 1.3), 0.0, 0.1],
             [3100, "OneDrive.exe", rng.uniform(0.0, 0.4), 0.0, 0.0],
             [7010, "RuntimeBroker.exe", rng.uniform(0.0, 0.3), 0.0, 0.0]]
    if hog_on:
        share = 100.0 * LOGICAL_CPUS / (k.hogs + 2.5)      # hogs + the training job + the game share the CPUs
        procs += [[20000 + 4 * j, "python.exe", share * rng.uniform(0.9, 1.1), 0.0, 0.0] for j in range(k.hogs)]
    procs.sort(key=lambda p: -p[2])
    return procs


# ------------------------------------------------------------------------ network ---

def _network(k: Knobs, rng: random.Random) -> tuple[dict[str, list], dict[str, Any]]:
    b = SeriesBuilder(N.ALL)
    lost_t: list[float] = []
    times = []
    x = NET_ROUND_S
    while x <= k.duration_s:
        times.append(x + rng.uniform(0.0, 0.01))
        x += NET_ROUND_S
    for s in times:
        inet = 6.4 + abs(rng.gauss(0.0, 0.5))
        for a, bb, extra in k.net_spikes:
            if a <= s <= bb:
                inet += extra
        if any(abs(s - m) < NET_ROUND_S / 2 for m in k.net_losses):
            inet = None
            lost_t.append(s)
        b.add(t=s, gw_rtt_ms=2.2 + abs(rng.gauss(0.0, 0.6)), inet_rtt_ms=inet,
              wifi_signal_pct=86.0, wifi_rssi_dbm=-53.0, wifi_channel=36, wifi_rx_mbps=433.3, wifi_tx_mbps=173.3,
              wifi_retries_ps=rng.uniform(0.0, 4.0), nic_rx_bps=rng.uniform(2e3, 3e4), nic_tx_bps=rng.uniform(1e3, 1e4))
    sent = len(times)
    nf = {"targets": {"gateway": "192.168.0.1", "internet": "1.1.1.1", "server": None},
          "probes": {"gateway": {"sent": sent, "lost": 0, "from_t": times[0] if times else None},
                     "internet": {"sent": sent, "lost": len(lost_t), "from_t": times[0] if times else None}},
          "lost": {"gateway": [], "internet": lost_t}, "wifi_scans": [],
          "route": {"if_index": 8, "description": "Intel(R) Wi-Fi 6 AX201 160MHz", "vpn": False}, "downloads": []}
    if k.server_gaps:
        # first_rx_t / last_rx_t: the flow's first and last packet on the capture clock (CC-1, DESIGN net_flows)
        nf.update(server={"ip": "203.0.113.7", "port": 27015, "proto": "udp", "rx_packets": 12000,
                          "tx_packets": 6000, "rx_bytes": 2_400_000, "tx_bytes": 600_000, "tick_ms": 15.6,
                          "first_rx_t": k.start_s, "last_rx_t": k.duration_s - k.tail_s},
                  gaps=[[t, ms, kind] for t, ms, kind in k.server_gaps], layout="unverified")
    return b.build(), nf


# ------------------------------------------------------------ small hand builders ---

def frames_table(frame_ms: Sequence[float], *, t0: float = 1.0, gpu_ms: Any = None, display_ms: Any = None,
                 displayed: Any = None, chain: str = "0x0", frame_type: Any = None, cpu_busy: Any = None,
                 runtime: str = "Other", mode: str = "Composed: Flip") -> dict[str, list]:
    """F columns for hand-written frame times. Each per-frame knob is a list (one value per frame), a single
    value for every frame, or None. Times start at t0 and advance by each frame's own time."""

    def pick(v: Any, i: int) -> Any:
        return v[i] if isinstance(v, (list, tuple)) else v

    b = SeriesBuilder(F.ALL)
    t = t0
    for i, f in enumerate(frame_ms):
        t += f / 1000.0
        b.add(t=t, frame_ms=f, cpu_busy_ms=pick(cpu_busy, i) if cpu_busy is not None else f, cpu_wait_ms=0.0,
              gpu_busy_ms=pick(gpu_ms, i), display_ms=pick(display_ms, i), displayed=pick(displayed, i),
              swap_chain=chain, frame_type=pick(frame_type, i), runtime=runtime, present_mode=mode)
    return b.build()


def system_table(seconds: float, **cols: Any) -> dict[str, list]:
    """S rows every 250 ms from 0.25 s to `seconds`; each column a constant or a function of t."""
    b = SeriesBuilder(S.ALL)
    k = 1
    while k * SAMPLE_S <= seconds:
        t = k * SAMPLE_S
        b.add(t=t, **{c: (v(t) if callable(v) else v) for c, v in cols.items()})
        k += 1
    return b.build()


def gpu_table(seconds: float, **cols: Any) -> dict[str, list]:
    """G rows once a second (NVML's rate, DESIGN gpu) from 1 s to `seconds`; each column a constant or f(t)."""
    b = SeriesBuilder(G.ALL)
    k = 1
    while k <= seconds:
        b.add(t=float(k), **{c: (v(float(k)) if callable(v) else v) for c, v in cols.items()})
        k += 1
    return b.build()


def network_table(seconds: float, **cols: Any) -> dict[str, list]:
    """N rows every 0.5 s (one probe round, DESIGN network) from 0.5 s to `seconds`; constants or f(t)."""
    b = SeriesBuilder(N.ALL)
    k = 1
    while k * NET_ROUND_S <= seconds:
        t = k * NET_ROUND_S
        b.add(t=t, **{c: (v(t) if callable(v) else v) for c, v in cols.items()})
        k += 1
    return b.build()


def startup_then_smooth(startup_s: float = 60.0, total_s: float = 180.0, every_s: float = 3.0,
                        hitch_ms: float = 150.0, fps: float = 60.0, first_hitch_s: float = 5.0) -> Capture:
    """A recording whose game starts up first (M5's case, docs/LAUNCHER-PLAN.md "Measuring the right part"): from
    first_hitch_s until startup_s a hitch_ms frame comes every every_s, each with the game's busiest thread pegged (its
    own work, as in shader preparation and loading), then smooth play at `fps` until total_s. Frames, the system
    table and both sources are measured; the display runs at 60 Hz."""
    ms, t, nxt = [], 1.0, first_hitch_s
    while t < total_s - 0.5:
        hitch = nxt <= t < startup_s
        if hitch:
            nxt += every_s
        f = hitch_ms if hitch else 1000.0 / fps
        ms.append(f)
        t += f / 1000.0
    fr = frames_table(ms, gpu_ms=0.5, display_ms=ms, displayed=1)
    spans = [(x - f / 1000.0, x) for x, f in zip(fr[F.T], ms) if f >= 50]

    def busy(x: float) -> bool:
        return any(a - 0.1 <= x <= b + 0.1 for a, b in spans)

    sys_ = system_table(total_s, game_top_thread_pct=lambda x: 100.0 if busy(x) else 30.0,
                        game_cpu_pct=lambda x: 120.0 if busy(x) else 40.0, cpu_total_pct=12.0, game_focused=1)
    cap = capture(fr, sys_, hz=60.0, duration=total_s)
    cap.sources = sources(("frames", "system"))
    return cap


def sources(ok: Sequence[str] = (), **other: SourceStatus) -> dict[str, SourceStatus]:
    """Capture.sources: "ok" for the names in `ok`, any SourceStatus given by keyword, nothing else."""
    out: dict[str, SourceStatus] = {name: SourceStatus("ok") for name in ok}
    out.update(other)
    return out


def capture(frames: dict | None = None, system: dict | None = None, *, hz: float | None = None,
            hags: bool | None = None, threads: int = LOGICAL_CPUS, duration: float = 30.0, **fields: Any) -> Capture:
    """A Capture with only what a test needs: the CPU's logical count, HAGS and, with hz, the game's display.
    Other Capture fields (network, net_flows, others, gpu, marks ...) pass through."""
    inv: dict[str, Any] = {"cpu": {"threads": threads}, "settings": {"hags": hags}}
    if hz:
        inv["displays"] = [{"device": r"\\.\DISPLAY1", "primary": True, "hz": hz}]
    return Capture(started_at="2026-11-04T00:00:00+00:00", clock_origin=0.0, duration_s=duration,
                   target={"pid": 1}, sources={}, inventory=inv, frames=frames or {}, system=system or {}, **fields)


# ---------------------------------------------------------------------- inventory ---

def _inventory(k: Knobs) -> dict[str, Any]:
    hz = k.refresh_hz
    return {
        "os": {"name": "Windows 11 Home", "build": 26200},
        "cpu": {"name": "12th Gen Intel(R) Core(TM) i7-12650H", "cores": 10, "threads": LOGICAL_CPUS,
                "hybrid": True, "p_threads": 12, "e_threads": 4},
        "ram_gb": 31.7, "laptop": True,
        "adapters": [{"luid": NVIDIA_LUID, "name": "NVIDIA GeForce RTX 3070 Laptop GPU", "vendor": "nvidia",
                      "vendor_id": 0x10DE, "vram_mb": 8192, "integrated": False, "software": False, "hags": True,
                      "hybrid": "discrete", "pci": "0000:01:00.0"},
                     {"luid": INTEL_LUID, "name": "Intel(R) UHD Graphics", "vendor": "intel", "vendor_id": 0x8086,
                      "vram_mb": 128, "integrated": True, "software": False, "hags": k.hags, "hybrid": "integrated",
                      "pci": "0000:00:02.0"}],
        "displays": [{"device": r"\\.\DISPLAY1", "name": "Internal panel", "adapter_luid": INTEL_LUID,
                      "internal": True, "primary": True, "width": 1920, "height": 1080, "hz": hz,
                      "max_hz": round(hz), "modes_hz": [60, round(hz)]}],
        "settings": {"game_mode": True, "game_dvr": False, "background_recording": False, "hags": k.hags,
                     "windowed_optimizations": True, "vrr": None, "memory_integrity": True},
        "game": {"exe": r"C:\Python313\python.exe", "drive": "C:", "media": "SSD", "free_gb": 210.0,
                 "gpu_preference": None, "monitor_device": r"\\.\DISPLAY1", "launcher": None},
    }
