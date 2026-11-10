"""analysis/attribute.py: the active test, the lift test, explaining away, sides and residuals."""
from __future__ import annotations

import pytest

from lagdoctor.analysis import attribute as at
from lagdoctor.analysis import events as ev
from lagdoctor.analysis import frames as fa
from lagdoctor.model import F, G, S, Capture, SeriesBuilder, SourceStatus

TIMES = (4.0, 8.0, 12.0, 16.0, 20.0, 24.0)
BASE = {S.GAME_TOP_THREAD_PCT: 25.0, S.GAME_CPU_PCT: 30.0, S.CPU_TOTAL_PCT: 10.0, S.GAME_IO_READ_BPS: 0.0,
        S.DISK_BUSY_PCT: 2.0, S.DISK_READ_MS: 0.2, S.HARD_FAULTS_PS: 5.0, S.RAM_AVAIL_MB: 20000.0,
        S.GPU_OTHER_PCT: 2.0, S.DPC_PCT: 0.5, S.INTERRUPT_PCT: 0.4, S.CPU_PERF_PCT: 105.0,
        S.CPU_PERF_LIMIT_PCT: 100.0, S.GAME_FOCUSED: 1, S.VRAM_USED_MB: 3000.0, S.GAME_SHARED_MB: 100.0,
        S.DISK_READ_BPS: 0.07e6}
READS = {S.GAME_IO_READ_BPS: 30e6, S.DISK_READ_BPS: 30e6}      # the game read from disk, and the disks read it
OTHERS = [[7777, "python.exe", 100.0, 0.0, 100.0], [2656, "dwm.exe", 3.0, 0.0, 3.0], [1844, "MsMpEng.exe", 1.0, 1e4, 0.0]]


def lag_cap(times=TIMES, *, stutter_ms=160.0, gpu_ms=0.5, stutter_gpu_ms=None, base=None, during=None,
            others_during=None, duration=30.0, with_frames=True, with_system=True, follow_gpu_ms=None,
            ram_gb=32.0, vram_mb=8192, gpu_rows=None, hot=(0.0, 0.5), stall=False):
    """100 FPS frames with a stutter_ms frame at each time; 4 Hz system rows whose `during` values replace
    the base ones on the ticks from hot[0] to hot[1] seconds after each time. With follow_gpu_ms, the
    stutter frame is followed by a 0.7 ms frame and only that one reaches the screen (what composition
    does); with stall, the rows of those stutter_ms are simply missing."""
    b = SeriesBuilder(F.ALL)
    t, todo = 0.5, sorted(times)
    while t < duration - 0.2:
        if todo and t >= todo[0]:
            todo.pop(0)
            t += stutter_ms / 1000.0
            if stall:
                continue
            if follow_gpu_ms is None:
                b.add(t=t, frame_ms=stutter_ms, cpu_busy_ms=stutter_ms, gpu_busy_ms=stutter_gpu_ms or gpu_ms,
                      displayed=1, display_ms=stutter_ms, frame_type="Application")
            else:
                b.add(t=t, frame_ms=stutter_ms, cpu_busy_ms=stutter_ms, gpu_busy_ms=gpu_ms, displayed=0,
                      frame_type="Application")
                t += 0.0007
                b.add(t=t, frame_ms=0.7, cpu_busy_ms=0.7, gpu_busy_ms=follow_gpu_ms, displayed=1,
                      display_ms=stutter_ms + 6.9, frame_type="Application")
            continue
        t += 0.010
        b.add(t=t, frame_ms=10.0, cpu_busy_ms=10.0, gpu_busy_ms=gpu_ms, displayed=1, display_ms=10.0,
              frame_type="Application")
    sysb = SeriesBuilder(S.ALL)
    others = []
    k = 1
    while k * 0.25 <= duration:
        s = k * 0.25
        is_hot = any(h + hot[0] <= s <= h + hot[1] for h in times)
        row = dict(base or BASE)
        if is_hot and during:
            row.update(during)
        sysb.add(t=s, **row)
        others.append([s, (others_during if is_hot and others_during is not None else OTHERS)])
        k += 1
    inv = {"cpu": {"threads": 16}, "ram_gb": ram_gb, "settings": {"hags": False},
           "adapters": [{"luid": "L1", "vram_mb": vram_mb}]}
    return Capture(started_at="x", clock_origin=0.0, duration_s=duration, target={"pid": 1, "adapter_luid": "L1"},
                   sources={}, inventory=inv, frames=b.build() if with_frames else {},
                   system=sysb.build() if with_system else {}, others=others if with_system else [],
                   gpu=gpu_rows or {})


def analyse(cap):
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    b = fa.bound(cap, fr, evs)
    summary = at.attribute(cap, fr, evs, b)
    return [e for e in evs if e.kind in ("stutter", "fps_drop")], summary


def firsts(events):
    return [e.causes[0] if e.causes else None for e in events]


# ------------------------------------------------------------------------------ the active test ---

def parts_of(cap=None):
    cap = cap or lag_cap()
    return {c.name: c for c in at._causes(cap, at._others(cap))}


@pytest.mark.parametrize("reasons,held", [("sw_thermal_slowdown", "heat"),
                                          ("sw_thermal_slowdown,hw_power_brake_slowdown", "heat or its power brake")])
def test_the_gpu_heat_cause_names_only_what_the_readings_named(reasons, held):
    from tests import synth
    cap = lag_cap()
    cap.gpu = synth.gpu_table(30, nv_throttle_pct=80.0, nv_reasons=reasons)
    label = parts_of(cap)["gpu_throttle"].parts[0].label
    assert label.startswith(f"the NVIDIA GPU was held down by {held} {{v}} of the time"), label


def test_a_signal_must_clear_a_robust_z_of_35():
    part = at._Part(at._Series([0.0], [0.0]), +1, lambda m: 0.0, "x", str)
    base = at._Baseline(25.0, 2.0)                    # scale = 1.4826 * 2 = 2.9652
    assert at._judge(part, base, 35.38)[0]            # z = 3.501
    assert not at._judge(part, base, 35.37)[0]        # z = 3.497
    down = at._Part(at._Series([0.0], [0.0]), -1, lambda m: 0.0, "x", str)
    assert at._judge(down, base, 14.62)[0] and not at._judge(down, base, 14.63)[0]


@pytest.mark.parametrize("cause, k, normal, ok, short", [
    ("game_cpu_spike", 0, 25.0, 28.125, 28.12),        # 31.25 ms more thread time in 1 s = 3.125 points
    ("asset_streaming", 0, 0.0, 1048576.0, 1048575.0),  # 1 MiB/s more game reads
    ("asset_streaming", 1, 2.0, 90.0, 89.9),            # disk busy >= 90 % (idle < 10 %)
    ("asset_streaming", 2, 0.2, 15.0, 14.9),            # disk reads >= 15 ms
    ("memory_paging", 0, 5.0, 1005.0, 1004.9),          # +1000 hard faults/s
    ("background_cpu", 0, 130.0, 330.0, 329.9),         # other programs +2 logical CPUs
    ("background_cpu", 1, 104.0, 304.0, 303.9),         # the listed top processes +2 logical CPUs
    ("background_cpu", 2, 100.0, 200.0, 199.9),         # one process +1 logical CPU
    ("background_disk", 0, 1e4, 1e4 + 20e6, 1e4 + 19.9e6),   # one process +20 MB/s
    ("background_gpu", 0, 2.0, 12.0, 11.9),             # +10 % of the game's GPU engine
    ("driver_dpc", 0, 0.5, 10.5, 10.4),                 # DPC +10 % of a CPU
    ("driver_dpc", 1, 0.4, 5.4, 5.3),                   # interrupts +5 %
    ("cpu_throttle", 0, 105.0, 83.99, 84.1),            # speed 20 % (21 points) below normal
    ("cpu_throttle", 1, 0.0, 0.25, 0.24),               # limited in a quarter of the samples
    ("gpu_throttle", 0, 0.0, 30.0, 29.9),               # throttled 30 % of the time
    ("vram_overflow", 0, 0.0, 5 * 256 / 60 + 0.001, 5 * 256 / 60 - 0.01),   # a rise at 256 MiB/min within 5 s
    ("not_focused", 0, 0.0, 0.5, 0.49),                 # out of focus half of that second
])
def test_each_signal_floor(cause, k, normal, ok, short):
    part = parts_of()[cause].parts[k]
    base = at._Baseline(normal, 0.0)                  # MAD 0: z is infinite, so only the floor decides
    assert at._judge(part, base, ok)[0], (cause, k)
    assert not at._judge(part, base, short)[0], (cause, k)


def test_a_baseline_mostly_at_one_value_takes_its_spread_from_the_mean_deviation():
    # Item 4: the game's reads were 0 in 54 % of the first real capture's quiet seconds, so the MAD was 0, z infinite,
    # and every read of 1 MiB/s or more "a signal moved". 60 quiet seconds at 0 and 40 at 30 MB/s: median 0, MAD 0,
    # mean deviation 12 MB/s, so a spread of 1.2533 x 12 = 15.04 MB/s
    xs = [0.0] * 60 + [30e6] * 40
    series = at._Series([k + 0.5 for k in range(100)], xs)
    base = at._baseline(series, [(k, k + 1.0) for k in range(100)])
    assert base.med == 0.0 and base.scale == pytest.approx(1.2533141 * 12e6)
    part = parts_of()["asset_streaming"].parts[0]
    assert not at._judge(part, base, 23.5e6)[0]                         # z = 1.6: an ordinary read here
    assert at._judge(part, base, 3.5 * base.scale + 1.0)[0] and not at._judge(part, base, 3.5 * base.scale - 1.0)[0]
    still = at._baseline(at._Series([k + 0.5 for k in range(100)], [0.0] * 100), [(k, k + 1.0) for k in range(100)])
    assert still.scale == 0.0 and at._judge(part, still, 1048576.0)[0]   # a baseline that never moves: the floor


def reads_cap(io_at, marks, duration=120.0):
    """A capture without frame timing whose game reads io_at(t) bytes/s, and lag-key presses at `marks`."""
    cap = lag_cap(times=(), with_frames=False, duration=duration)
    cap.system[S.GAME_IO_READ_BPS] = [io_at(t) for t in cap.system[S.T]]
    cap.system[S.DISK_READ_BPS] = [io_at(t) for t in cap.system[S.T]]       # the disks read it too
    cap.marks = list(marks)
    return cap


def mark_summary(cap):
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    return [e for e in evs if e.kind == "mark"], at.attribute(cap, fr, evs, fa.bound(cap, fr, evs))


def test_a_mark_window_is_judged_against_quiet_stretches_as_long_as_it():
    # Item 5: an event is judged on the busiest second of its window (4.25 s for a mark), but its chance by
    # coincidence was the share of plain 1 s quiet windows: 0.36 for the first real capture's disk reads, where
    # stretches of a mark's length gave 0.64. Here a 0.5 s read burst every 7 s: plain 1 s quiet windows held one
    # 17 % of the time (lift 6.0, "supports"), quiet stretches as long as a mark's window 70 % (lift 1.4: measured)
    burst = lambda t: 200e6 if int(round(t * 4)) % 28 in (4, 5) else 0.0          # at 7k + 1.0 and 7k + 1.25 s
    marks, summary = mark_summary(reads_cap(burst, [7 * k + 3.0 for k in (1, 4, 7, 10, 13, 16)]))
    s = summary["asset_streaming"]
    assert s["a"] == s["n"] == 6 and firsts(marks) == ["asset_streaming"] * 6     # every mark held a burst
    assert 0.5 < s["b"] < 0.9 and s["lift"] < at.LIFT_MIN and s["label"] == "neutral"
    assert at._chance([True, False, False, False] * 25, list(range(100)), 1) == 0.25
    assert at._chance([True, False, False, False] * 25, list(range(100)), 2) == pytest.approx(0.5, abs=0.01)
    assert at._chance([True, False, False, False] * 25, list(range(100)), 4) == 1.0
    gapped = list(range(50)) + list(range(60, 110))                                 # a gap breaks the stretches
    assert at._chance([False] * 100, gapped, 48) is None                             # 3 + 3 stretches of 48: < 8 ...
    assert at._expected([False] * 100, gapped, [48]) == 0.0                          # ... so the longest with 8 (47)


def spill_cap(shared_at):
    """Three marks at 40, 44 and 48 s in 60 s, video memory 92 % full, the game's shared memory shared_at(t) MB."""
    cap = lag_cap(times=(), with_frames=False, duration=60.0, vram_mb=8192,
                  base=dict(BASE, **{S.VRAM_USED_MB: 7550.0}))
    cap.system[S.GAME_SHARED_MB] = [shared_at(t) for t in cap.system[S.T]]
    cap.marks = [40.0, 44.0, 48.0]
    return cap


def test_shared_memory_standing_at_a_level_is_no_spill_rising():
    # Item 4: the first real capture's game held 245 MB of shared memory flat through all three marks against a
    # 210 MB median, and each mark read "the spill rose to 245 MB (normally 210 MB)". A rise is measured within 5 s
    marks, summary = mark_summary(spill_cap(lambda t: 245.0 if 30.0 <= t < 52.0 else 210.0))
    assert not [m for m in marks if "vram_overflow" in m.causes] and summary["vram_overflow"]["a"] == 0
    marks, summary = mark_summary(spill_cap(lambda t: 800.0 if t >= 39.5 else 245.0 if t >= 30.0 else 210.0))
    assert "vram_overflow" in marks[0].causes                           # control: a real spill at the first mark
    assert any(w.startswith("the game's spill into shared memory rose 555 MB within seconds") for w in marks[0].detail["why"])
    assert at._rise(at._Series([0.0, 1.0, 2.0, 7.0, 8.0], [5.0, 9.0, 7.0, 7.0, None]), 5.0).v == [0.0, 4.0, 2.0, 0.0, None]


def test_memory_paging_also_needs_ram_nearly_full():
    cap = lag_cap(ram_gb=32.0, base=dict(BASE, **{S.RAM_AVAIL_MB: 3276.0}))      # 10 % of 32 GiB = 3276.8 MB
    assert parts_of(cap)["memory_paging"].condition(1.0, 2.0) is True
    cap = lag_cap(ram_gb=32.0, base=dict(BASE, **{S.RAM_AVAIL_MB: 3277.0}))
    assert parts_of(cap)["memory_paging"].condition(1.0, 2.0) is False
    cap = lag_cap(ram_gb=None, base=dict(BASE, **{S.RAM_AVAIL_MB: 63.0}))         # RAM size unknown: < 64 MB
    assert parts_of(cap)["memory_paging"].condition(1.0, 2.0) is True


def test_vram_overflow_also_needs_vram_90_percent_full():
    cap = lag_cap(vram_mb=4000, base=dict(BASE, **{S.VRAM_USED_MB: 3600.0}))
    assert parts_of(cap)["vram_overflow"].condition(1.0, 2.0) is True
    cap = lag_cap(vram_mb=4000, base=dict(BASE, **{S.VRAM_USED_MB: 3599.0}))
    assert parts_of(cap)["vram_overflow"].condition(1.0, 2.0) is False
    cap = lag_cap(vram_mb=None)
    assert parts_of(cap)["vram_overflow"].condition(1.0, 2.0) is None              # cannot tell


# -------------------------------------------------------------------------------- the lift test ---

@pytest.mark.parametrize("a, n, lift, label", [
    (3, 10, 2.0, "supports"), (2, 6, 5.0, "neutral"),          # a >= 3
    (3, 10, 1.99, "neutral"),                                   # lift >= 2
    (3, 11, 9.0, "neutral"),                                    # a / n = 0.27 < 0.30
    (4, 4, 9.0, "too_few_events"), (3, 5, 9.0, "supports"),     # n >= 5
    (1, 10, 1.0, "contradicts"), (2, 10, 1.0, "neutral"),       # a / n <= 0.10 with n >= 10
    (0, 9, 0.0, "neutral"),
])
def test_lift_labels(a, n, lift, label):
    assert at._label(a, n, lift, measured=True) == label


def test_unmeasured_signals_are_labelled_so():
    assert at._label(0, 20, None, measured=False) == "not_measured"


def test_lift_uses_a_floor_of_002_on_the_baseline_share():
    events, summary = analyse(lag_cap(during={S.GAME_TOP_THREAD_PCT: 95.0}))
    s = summary["game_cpu_spike"]
    assert s["a"] == s["n"] == 6 and s["b"] == 0.0
    assert s["lift"] == pytest.approx(1.0 / 0.02) and s["label"] == "supports" and s["weight"] == 3.0
    assert s["share"] == 1.0 and summary["asset_streaming"]["a"] == 0


def test_few_events_never_support_a_cause():
    events, summary = analyse(lag_cap(TIMES[:4], during={S.GAME_TOP_THREAD_PCT: 95.0}))
    assert firsts(events) == ["game_cpu_spike"] * 4                  # still each event's best cause
    assert summary["game_cpu_spike"]["label"] == "too_few_events"


def test_a_signal_quiet_in_most_of_ten_events_contradicts():
    times = tuple(3.0 + 2.6 * i for i in range(10))
    events, summary = analyse(lag_cap(times, during={S.GAME_TOP_THREAD_PCT: 95.0}))
    assert len(events) == 10
    assert summary["asset_streaming"]["label"] == "contradicts"
    assert summary["game_cpu_spike"]["label"] == "supports"


# --------------------------------------------------------------------------- explaining away ---

def test_reading_from_disk_explains_the_extra_cpu_work():
    events, summary = analyse(lag_cap(during=dict(READS, **{S.GAME_TOP_THREAD_PCT: 60.0})))
    assert firsts(events) == ["asset_streaming"] * 6
    assert all(e.causes[1:] == ["game_cpu_spike"] for e in events)    # still listed, second
    assert summary["asset_streaming"]["share"] == 1.0 and summary["game_cpu_spike"]["share"] == 0.0


def test_reads_the_file_cache_served_are_never_reading_from_disk():
    # The game's own read counter (ReadTransferCount) also counts reads the file cache served from memory: a buffered
    # 64 MiB read of a cached file grew it 64 MiB while the physical disks read 0.00 MiB (measured 2026-11-09). A real
    # recording read "471.7 MB/s from disk" beside 0.07 MB/s of disk reads. Only what the disks read can be from disk.
    game = {S.GAME_IO_READ_BPS: 471.7e6, S.GAME_TOP_THREAD_PCT: 60.0}
    events, summary = analyse(lag_cap(during=game))                  # BASE: the disks read 0.07 MB/s throughout
    assert firsts(events) == ["game_cpu_spike"] * 6 and summary["asset_streaming"]["a"] == 0
    assert not [w for e in events for w in e.detail["why"] if "from disk" in w]
    events, _ = analyse(lag_cap(during=dict(game, **{S.DISK_READ_BPS: 471.7e6})))    # control: the disks read it
    assert firsts(events) == ["asset_streaming"] * 6
    assert events[0].detail["why"][1] == "up to 314.5 MB/s of the game's reads came from disk (normally 0.0 MB/s)"
    blind = lag_cap(during=game)
    blind.system[S.DISK_READ_BPS] = [None] * len(blind.system[S.T])   # the disks' read counter was not read
    events, summary = analyse(blind)
    assert firsts(events) == ["game_cpu_spike"] * 6 and summary["asset_streaming"]["a"] == 0


def test_the_engines_gpu_verdict_never_stands_in_for_an_events_side():
    # With too few steady frames the 3D engine names the GPU, never frame timing; a stutter whose frames had no GPU timing
    # took that "gpu" for its own side ("the game is gpu-bound overall") and every CPU-side cause was left out
    cap = lag_cap(times=(1.0,), duration=2.5, gpu_ms=0.0, base=dict(BASE, **{S.GAME_GPU_PCT: 99.0}),
                  during={S.GAME_TOP_THREAD_PCT: 95.0})
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    b = fa.bound(cap, fr, evs)
    assert b["verdict"] == "gpu" and b["r"] is None                  # the engine's verdict
    at.attribute(cap, fr, evs, b)
    stutters = [e for e in evs if e.kind == "stutter"]
    assert stutters and all(e.detail["side"] is None and e.detail["side_source"] is None for e in stutters)
    assert all("so which side was slow is unknown" in e.detail["why"][0] for e in stutters)


def test_a_driver_is_never_the_first_cause_when_something_else_moved():
    events, summary = analyse(lag_cap(during={S.DPC_PCT: 40.0, S.GAME_TOP_THREAD_PCT: 60.0}))
    assert firsts(events) == ["game_cpu_spike"] * 6 and all("driver_dpc" in e.causes for e in events)
    assert summary["driver_dpc"]["cap"] == "possible" and summary["driver_dpc"]["weight"] == 1.0
    events, _ = analyse(lag_cap(during={S.DPC_PCT: 40.0}))
    assert firsts(events) == ["driver_dpc"] * 6                       # alone, it is still named


def test_not_in_focus():
    events, _ = analyse(lag_cap(during={S.GAME_FOCUSED: 0}))
    assert firsts(events) == ["not_focused"] * 6


def test_background_cpu_names_the_program():
    hog = OTHERS + [[9000 + i, "hog.exe", 50.0, 0.0, 0.0] for i in range(6)]
    events, summary = analyse(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=hog))
    assert firsts(events) == ["background_cpu"] * 6
    e = events[0]
    assert e.detail["process"] == "hog.exe" and sorted(e.detail["process_pids"]) == list(range(9000, 9006))
    assert any("hog.exe (6 processes)" in w and "%" in w for w in e.detail["why"])


def with_create_times(rows):
    return [list(p) + [134000000000000000 + p[0]] for p in rows]


def busy_python(cap):
    """lag_cap's others with create times; at each stutter python.exe 20000 rises from nothing to 60 % of a CPU
    while python.exe 7777 (a steady training job, lower PID) wobbles from 100 % to 101 %."""
    quiet = with_create_times(OTHERS)
    hot = with_create_times([[7777, "python.exe", 101.0, 0.0, 100.0], [20000, "python.exe", 60.0, 0.0, 0.0]]
                            + OTHERS[1:])
    cap.others = [[t, hot if rows is not OTHERS else quiet] for t, rows in cap.others]
    return cap


def test_the_process_named_is_the_one_that_rose_never_a_steady_one_of_the_same_name():
    # A5: a +1 % wobble of the training job used to count as a rise, and the fix then went to the lowest PID
    cap = busy_python(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=OTHERS + []))
    events, _ = analyse(cap)
    assert firsts(events) == ["background_cpu"] * 6
    assert all(e.detail["process"] == "python.exe" and e.detail["process_pids"] == [20000] for e in events)
    assert all(e.detail["process_rises"] == [[20000, 60.0]] and e.detail["process_role"] == "background"
               for e in events)
    d = diagnosed(busy_python(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=OTHERS + [])))
    hog = next(f for f in d.findings if f.cause == "background_hogs")
    fix = next(fx for fx in hog.fixes if fx.fix_id)
    assert fix.params["pid"] == 20000 and fix.params["name"] == "python.exe"
    assert "up 60% of a CPU" in fix.title
    assert not any("7777" in fx.title for fx in hog.fixes)
    assert any("against 0% normally" in e.text for e in hog.evidence)        # the riser's own normal, not the job's


@pytest.mark.parametrize("cpu, pids", [(25.0, [9001, 9000]), (24.9, [9001])])
def test_a_process_is_named_only_past_a_quarter_of_a_cpu(cpu, pids):
    # A5: the floor each process must rise by to be listed (attribute.BLAME_CPU_RISE_PCT), biggest rise first
    hog = OTHERS + [[9000, "hog.exe", cpu, 0.0, 0.0], [9001, "hog.exe", 100.0, 0.0, 0.0]]
    events, _ = analyse(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=hog))
    assert firsts(events) == ["background_cpu"] * 6
    assert all(e.detail["process"] == "hog.exe" and e.detail["process_pids"] == pids for e in events)


def test_a_small_wobble_names_no_program():
    # A5: the machine was busy (another cause's part saw it), but no listed program rose a quarter of a CPU
    wobble = [[p[0], p[1], p[2] + 5.0, p[3], p[4]] for p in OTHERS]
    events, _ = analyse(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=wobble))
    assert firsts(events) == ["background_cpu"] * 6
    assert all("process" not in e.detail for e in events)


def test_a_program_whose_processes_each_rose_a_little_is_named_without_a_process_to_lower():
    # eight svc.exe processes rose 10 % each: the program rose, no one process of it by a quarter of a CPU
    small = OTHERS + [[8000 + i, "svc.exe", 10.0, 0.0, 0.0] for i in range(8)]
    events, _ = analyse(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=small))
    assert all(e.detail["process"] == "svc.exe" and e.detail["process_pids"] == [] for e in events)


def test_the_games_own_child_process_is_named_as_the_games_helper():
    # A10: the game's child (target "pids") still competes with it (DESIGN), but it is no background program
    helper = OTHERS + [[8888, "ShaderCompileWorker.exe", 300.0, 0.0, 0.0]]
    cap = lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=helper)
    cap.target["pids"] = [1, 8888]
    events, _ = analyse(cap)
    assert firsts(events) == ["background_cpu"] * 6
    assert all(e.detail["process"] == "ShaderCompileWorker.exe" and e.detail["process_role"] == "game_helper"
               for e in events)
    assert any(w.startswith("a helper process of the game rose the most") for w in events[0].detail["why"])
    cap = lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=helper)
    cap.target["pids"] = [1, 8888]
    hog = next(f for f in diagnosed(cap).findings if f.cause == "background_hogs")
    assert not any(fx.title.startswith("Pause or close") for fx in hog.fixes)
    assert not any(fx.fix_id for fx in hog.fixes)


PRESENTMON = r"C:\Users\x\AppData\Local\lag-doctor\presentmon\2.6.0\PresentMon-2.6.0-x64.exe"


@pytest.mark.parametrize("entry, mark", [
    ([5555, "python.exe", 300.0, 0.0, 0.0], {"tool_pids": [5555]}),           # lag-doctor's own capture process
    ([6000, "PresentMon-2.6.0-x64.exe", 300.0, 0.0, 0.0], {"presentmon": {"exe": PRESENTMON}})])   # its child
def test_lag_doctors_own_processes_are_never_named(entry, mark):
    # A10: what the session records as lag-doctor's own (target "tool_pids") and the PresentMon exe it ran. Changed on
    # purpose (item 10): its own 300 % once still made "another program using the CPU" fire ("the machine still was
    # busy") while no program could be named; its load is the measurement's cost, so it counts nowhere now
    cap = lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=OTHERS + [entry])
    cap.target.update(mark)
    events, _ = analyse(cap)
    assert len(events) == 6 and not [e for e in events if "background_cpu" in e.causes]
    assert all("process" not in e.detail for e in events)
    control = analyse(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=OTHERS + [entry]))[0]
    assert firsts(control) == ["background_cpu"] * 6                     # unmarked, the same load is another program's
    assert {e.detail.get("process") for e in control} == {entry[1]}     # and it is named
    # a real program makes the cause fire while lag-doctor's own process rises more: the program is the one named
    hog = [9000, "hog.exe", 250.0, 0.0, 0.0]
    both = lag_cap(during={S.CPU_TOTAL_PCT: 60.0}, others_during=OTHERS + [entry, hog])
    both.target.update(mark)
    events, _ = analyse(both)
    assert firsts(events) == ["background_cpu"] * 6 and {e.detail.get("process") for e in events} == {"hog.exe"}


def test_absurd_values_from_a_corrupt_capture_are_missing_never_evidence():
    # A15: 1e308 in byte, count and percent columns once overflowed math.fsum and took diagnose() down
    huge = 1e308
    bad = dict(BASE, **{S.GAME_IO_READ_BPS: huge, S.HARD_FAULTS_PS: huge, S.GPU_OTHER_PCT: huge, S.RAM_AVAIL_MB: huge,
                        S.GAME_SHARED_MB: huge, S.VRAM_USED_MB: huge, S.CPU_TOTAL_PCT: huge})
    rows = SeriesBuilder(G.ALL)
    for s in range(1, 30):
        rows.add(t=float(s), nv_throttle_pct=huge, nv_temp_c=huge)
    cap = lag_cap(during=bad, others_during=OTHERS + [[9000, "hog.exe", huge, huge, huge]], gpu_rows=rows.build())
    events, summary = analyse(cap)
    assert len(events) == 6
    for e in events:
        for sig in e.detail["signals"].values():
            assert sig["value"] is None or abs(sig["value"]) <= at.MAX_COUNT, (e.detail["why"], sig)
    d = diagnosed(lag_cap(during=bad, others_during=OTHERS + [[9000, "hog.exe", huge, huge, huge]],
                          gpu_rows=rows.build()))
    assert not [w for w in d.unchecked.values() if w.startswith("the check failed")]


@pytest.mark.parametrize("column, ok, short", [(S.GAME_IO_READ_BPS, 1e15, 1.0001e15), (S.GPU_OTHER_PCT, 1e7, 1.0001e7)])
def test_the_bounds_on_sampled_values(column, ok, short):
    # A15: sizes, rates and counts up to MAX_COUNT (1e15) are values; times, ms and % stop at frames' 1e7
    assert at.read(column, ok) == ok and at.read(column, short) is None
    assert at.read(column, -ok) == -ok and at.read(column, float("nan")) is None


def test_background_cpu_from_many_small_processes():
    # 40 % of 16 CPUs is busy, but the top-8 table only shows eight services at 10 % each: the machine-wide
    # reading (total CPU minus the game) is what sees it
    small = OTHERS[:1] + [[8000 + i, "svc.exe", 10.0, 0.0, 0.0] for i in range(7)]
    events, _ = analyse(lag_cap(during={S.CPU_TOTAL_PCT: 40.0}, others_during=small))
    assert firsts(events) == ["background_cpu"] * 6
    assert any("other programs used" in w for w in events[0].detail["why"])


def test_why_lines_carry_numbers():
    events, _ = analyse(lag_cap(during={S.GAME_TOP_THREAD_PCT: 95.0}))
    why = events[0].detail["why"]
    assert any("slowest frame (160 ms)" in w for w in why)
    assert any("busiest thread ran" in w and "normally 25%" in w for w in why)
    assert events[0].detail["side"] == "cpu" and "game_cpu_spike" in events[0].detail["signals"]


# ------------------------------------------------------------------------ sides and residuals ---

def test_gpu_side_events_never_blame_cpu_causes():
    hog = OTHERS + [[9000 + i, "hog.exe", 50.0, 0.0, 0.0] for i in range(6)]
    events, _ = analyse(lag_cap(stutter_gpu_ms=158.0, during={S.CPU_TOTAL_PCT: 40.0}, others_during=hog))
    assert all(e.detail["side"] == "gpu" for e in events)
    assert firsts(events) == ["gpu_spike"] * 6 and all("background_cpu" not in e.causes for e in events)


def test_gpu_side_threshold_is_r_090():
    events, _ = analyse(lag_cap(stutter_gpu_ms=144.0))                # r = 0.90
    assert {e.detail["side"] for e in events} == {"gpu"}
    events, _ = analyse(lag_cap(stutter_gpu_ms=143.0))                # r = 0.894
    assert {e.detail["side"] for e in events} == {"mixed"}
    events, _ = analyse(lag_cap(stutter_gpu_ms=120.0))                # r = 0.75
    assert {e.detail["side"] for e in events} == {"cpu"}
    events, _ = analyse(lag_cap(stutter_gpu_ms=121.0))                # r = 0.756
    assert {e.detail["side"] for e in events} == {"mixed"}


def test_nothing_moved_and_the_thread_idle_is_the_game_waiting_on_itself():
    events, summary = analyse(lag_cap())
    assert firsts(events) == ["game_stall"] * 6
    assert summary["game_stall"]["label"] == "supports" and summary["game_stall"]["lift"] is None
    assert any("waited on itself" in w for w in events[0].detail["why"])


def test_nothing_moved_but_the_thread_saturated_is_the_games_own_work():
    events, summary = analyse(lag_cap(base=dict(BASE, **{S.GAME_TOP_THREAD_PCT: 100.0})))
    assert firsts(events) == ["game_cpu_spike"] * 6
    assert summary["game_cpu_spike"]["residual"] == 6 and summary["game_cpu_spike"]["a"] == 0


def test_without_system_samples_the_cause_is_unknown_not_a_stall():
    events, summary = analyse(lag_cap(with_system=False))
    assert len(events) == 6 and all(e.causes == [] for e in events)
    assert any("not measured" in w for w in events[0].detail["why"])
    assert summary["game_cpu_spike"]["label"] == "not_measured" and summary["game_stall"]["a"] == 0


def test_the_side_comes_from_the_slowest_presented_frame_not_the_displayed_one():
    # composition dropped the 160 ms frame; the 0.7 ms frame after it (GPU busy 0.6 ms, r = 0.86) is the
    # one on screen. The slow side is still the CPU's.
    events, _ = analyse(lag_cap(follow_gpu_ms=0.6))
    assert len(events) == 6 and {e.detail["side"] for e in events} == {"cpu"}
    assert firsts(events) == ["game_stall"] * 6


def diagnosed(cap):
    """The whole diagnosis of a lag_cap capture, with its frame and system sources marked as measured."""
    from lagdoctor.analysis.diagnose import diagnose
    cap.sources = {"frames": SourceStatus("ok"), "system": SourceStatus("ok")}
    return diagnose(cap)


def idle_gpu_lines(d):
    return [e.text for f in d.findings for e in f.evidence if "GPU sat idle" in e.text]


@pytest.mark.parametrize("label, kw, verdict", [
    ("capped", {}, None),
    ("GPU-bound", {"gpu_ms": 9.6}, "gpu"),                                   # r = 0.96 on the steady frames
    ("CPU-bound", {"base": dict(BASE, **{S.GAME_TOP_THREAD_PCT: 95.0})}, "cpu_main")])
def test_a_stall_has_no_side(label, kw, verdict):
    # A3: rows are missing for 160 ms, so no presented frame holds that time and neither side can be named; the
    # overall verdict says nothing about this moment. It once gave GPU-bound games gpu_spike ("the GPU's own work
    # took long") and CPU-bound ones "the GPU sat idle through most of the slow frame".
    cap = lag_cap(stall=True, **kw)
    events, _ = analyse(cap)
    assert len(events) == 6 and all(e.detail["stall"] for e in events)
    assert {e.detail["side"] for e in events} == {None} and {e.detail["side_source"] for e in events} == {None}
    assert "gpu_spike" not in firsts(events)
    if verdict:
        assert fa.bound(cap, fa.main_chain(cap), events)["verdict"] == verdict, label    # the fixture's own premise
    assert not idle_gpu_lines(diagnosed(lag_cap(stall=True, **kw)))


def test_a_stall_with_the_thread_idle_says_the_gpus_share_is_unknown():
    events, _ = analyse(lag_cap(stall=True, gpu_ms=9.6))
    assert firsts(events) == ["game_stall"] * 6
    line = events[0].detail["why"][-1]
    assert "the GPU's share of it is unknown" in line and "the GPU was idle" not in line


def without_gpu_timing_on_the_stutters(cap):
    """The stutter frames (160 ms) carry no GPU busy; the steady frames keep theirs."""
    fr = cap.frames
    fr[F.GPU_BUSY_MS] = [None if ms == 160.0 else g for ms, g in zip(fr[F.FRAME_MS], fr[F.GPU_BUSY_MS])]
    return cap


def test_the_verdict_stands_in_only_for_a_slow_frame_without_gpu_timing_and_says_so():
    # A3: the slow frame is there but has no GPU timing, so the CPU-bound verdict gives its side, marked as such;
    # the rules never count it as the frame-timing evidence "the GPU sat idle"
    base = dict(BASE, **{S.GAME_TOP_THREAD_PCT: 95.0})
    events, _ = analyse(without_gpu_timing_on_the_stutters(lag_cap(base=base)))
    assert {e.detail["side"] for e in events} == {"cpu"} and {e.detail["side_source"] for e in events} == {"verdict"}
    assert all("no GPU timing" in e.detail["why"][0] for e in events)
    assert not idle_gpu_lines(diagnosed(without_gpu_timing_on_the_stutters(lag_cap(base=base))))
    assert idle_gpu_lines(diagnosed(lag_cap(base=base)))           # control: with GPU timing the split counts
    capped, _ = analyse(without_gpu_timing_on_the_stutters(lag_cap()))
    assert {e.detail["side"] for e in capped} == {None}            # a cap's verdict names no side


def test_page_faults_without_low_ram_are_not_paging():
    events, _ = analyse(lag_cap(during={S.HARD_FAULTS_PS: 5000.0}))
    assert "memory_paging" not in {c for e in events for c in e.causes}
    events, _ = analyse(lag_cap(during={S.HARD_FAULTS_PS: 5000.0}, base=dict(BASE, **{S.RAM_AVAIL_MB: 1000.0})))
    assert firsts(events) == ["memory_paging"] * 6


def test_the_window_starts_half_a_second_before_the_event():
    # the only moving sample is the tick 0.25 s before the stutter began
    events, _ = analyse(lag_cap(during=READS, hot=(-0.3, -0.2)))
    assert firsts(events) == ["asset_streaming"] * 6


def test_the_window_ends_a_quarter_second_after_the_event():
    # the only moving sample is the tick about 0.08 s after the stutter frame was presented
    events, _ = analyse(lag_cap(during=READS, hot=(0.2, 0.3)))
    assert firsts(events) == ["asset_streaming"] * 6


def test_fps_drop_side_from_its_frames():
    b = SeriesBuilder(F.ALL)
    t = 0.5
    while t < 29.8:
        slow = 10.0 <= t < 15.0
        ms = 25.0 if slow else 10.0
        t += ms / 1000.0
        b.add(t=t, frame_ms=ms, cpu_busy_ms=ms, gpu_busy_ms=24.5 if slow else 2.0, displayed=1, display_ms=ms)
    cap = lag_cap(times=())
    cap.frames = b.build()
    events, _ = analyse(cap)
    drops = [e for e in events if e.kind == "fps_drop"]
    assert len(drops) == 1 and drops[0].detail["side"] == "gpu" and drops[0].causes == ["gpu_spike"]


def test_gpu_throttle_from_the_1_hz_nvidia_rows():
    rows = SeriesBuilder(G.ALL)
    for s in range(1, 30):
        rows.add(t=float(s), nv_throttle_pct=90.0 if any(h <= s <= h + 1.0 for h in TIMES) else 0.0)
    events, summary = analyse(lag_cap(stutter_gpu_ms=158.0, gpu_rows=rows.build()))
    assert firsts(events) == ["gpu_throttle"] * 6 and summary["gpu_throttle"]["label"] == "supports"


@pytest.mark.parametrize("hot_reasons, label", [("sw_power_cap", "not_measured"),
                                                ("sw_thermal_slowdown,sw_power_cap", "supports")])
def test_an_earlier_captures_throttle_share_links_to_stutters_only_where_heat_was_named(hot_reasons, label):
    # Item 11: before the power-cap split nv_throttle_pct held the SW power cap too, which engages whenever the GPU
    # works hardest, so a reading that names neither heat nor the brake says nothing about either
    rows = SeriesBuilder(G.ALL)
    for s in range(1, 30):
        hot = any(h <= s <= h + 1.0 for h in TIMES)
        rows.add(t=float(s), nv_throttle_pct=90.0 if hot else 0.0, nv_reasons=hot_reasons if hot else "")
    gpu = rows.build()
    events, today = analyse(lag_cap(stutter_gpu_ms=158.0, gpu_rows=dict(gpu)))
    assert today["gpu_throttle"]["label"] == "supports"          # control: today the column is heat and the brake
    del gpu[G.POWER_CAP_PCT]
    events, earlier = analyse(lag_cap(stutter_gpu_ms=158.0, gpu_rows=gpu))
    assert earlier["gpu_throttle"]["label"] == label
    assert ("gpu_throttle" in firsts(events)) is (label == "supports")


def with_game_adapter(rows, game_gpu):
    """others rows as the sampler writes them now (A10): a create time (field 6), then the busiest engine on the
    game's own adapter (gpu_game_pct, field 7), from `game_gpu` by name (absent: no engine there, None)."""
    return [list(p) + [134000000000000000 + p[0], game_gpu.get(p[1])] for p in rows]


def test_the_game_adapter_gpu_field_is_read_where_a_row_has_it_bounded():
    # Item 12 (A10): field 7 (the game's adapter) where the row has it, a missing value as 0; else field 5 (any
    # adapter), said so. Both bounded like every percentage (A15: past 1e7 a value is corrupt, so missing)
    row = [9100, "renderer.exe", 5.0, 0.0, 90.0, 134000000000009100]
    assert at.game_gpu_pct(row) == (90.0, True)
    assert at.game_gpu_pct(row + [40.0]) == (40.0, False)
    assert at.game_gpu_pct(row + [None]) == (0.0, False)                     # no engine on the game's adapter
    assert at.game_gpu_pct(row + [1e7]) == (1e7, False) and at.game_gpu_pct(row + [1.0001e7]) == (0.0, False)
    assert at.game_gpu_pct(row[:4] + [1e7]) == (1e7, True) and at.game_gpu_pct(row[:4] + [1.0001e7]) == (0.0, True)


JOB_QUIET = [[7777, "python.exe", 100.0, 0.0, 5.0], [2656, "dwm.exe", 3.0, 0.0, 3.0]]
JOB_HOT = [[7777, "python.exe", 100.0, 0.0, 95.0], [2656, "dwm.exe", 3.0, 0.0, 3.0]]   # its own GPU got busier


def gpu_blame_cap(quiet, hot):
    cap = lag_cap(during={S.GPU_OTHER_PCT: 40.0}, others_during=OTHERS + [])
    cap.others = [[t, hot if rows is not OTHERS else quiet] for t, rows in cap.others]
    return cap


def test_a_gpu_user_on_another_adapter_is_never_blamed_for_the_games_gpu():
    # Item 12 (A10): at each stutter the game's GPU gets 38 points busier, and the training job's busiest engine
    # rises too, on the other GPU. Field 5 (any adapter) blamed the job; field 7 says it has no engine on the game's
    quiet = with_game_adapter(JOB_QUIET, {"dwm.exe": 3.0})
    events, _ = analyse(gpu_blame_cap(quiet, with_game_adapter(JOB_HOT, {"dwm.exe": 3.0})))
    assert firsts(events) == ["background_gpu"] * 6 and all("process" not in e.detail for e in events)
    renderer = [9100, "renderer.exe", 5.0, 0.0, 40.0]                       # this one rose on the game's GPU
    cap = gpu_blame_cap(quiet, with_game_adapter(JOB_HOT + [renderer], {"dwm.exe": 3.0, "renderer.exe": 40.0}))
    events, _ = analyse(cap)
    assert {e.detail.get("process") for e in events} == {"renderer.exe"}
    line = next(w for w in events[0].detail["why"] if w.startswith("the program that rose the most"))
    assert line.startswith("the program that rose the most: renderer.exe reached ")
    assert line.endswith("% of a GPU engine, against 0% normally")             # the game's adapter: no caveat
    hog = next(f for f in diagnosed(gpu_blame_cap(quiet, with_game_adapter(
        JOB_HOT + [renderer], {"dwm.exe": 3.0, "renderer.exe": 40.0}))).findings if f.cause == "background_hogs")
    assert "renderer.exe" in hog.title and "python.exe" not in hog.title


def test_an_older_table_blames_by_any_adapter_and_says_so():
    # the fallback for captures without field 7: the busiest engine on any adapter, with the caveat
    events, _ = analyse(gpu_blame_cap(JOB_QUIET, JOB_HOT))
    assert {e.detail.get("process") for e in events} == {"python.exe"}
    assert any("(its busiest engine on any adapter)" in w for w in events[0].detail["why"])


# ------------------------------------------------------------------------------- the baseline ---

def test_dense_events_relax_the_quiet_guard():
    # an event every 2.5 s to 27 s: no 1 s window is 2 s or 1 s clear of events (bar 4 at the end), but
    # two per gap are 0.5 s clear
    times = tuple(2.0 + 2.5 * i for i in range(11))
    events, summary = analyse(lag_cap(times, during={S.GAME_TOP_THREAD_PCT: 95.0}))
    assert summary["game_cpu_spike"]["baseline_guard_s"] == 0.5
    assert firsts(events) == ["game_cpu_spike"] * len(events)
    events, summary = analyse(lag_cap(during={S.GAME_TOP_THREAD_PCT: 95.0}))
    assert summary["game_cpu_spike"]["baseline_guard_s"] == 2.0         # sparse events keep LC 5.2's 2 s


def test_marks_are_attributed_at_half_weight_when_frames_were_not_measured():
    cap = lag_cap(during=READS, with_frames=False)
    cap.marks = [h + 0.4 for h in TIMES]
    fr = fa.main_chain(cap)
    evs = ev.find_events(cap, fr, fa.frame_stats(fr, cap.duration_s))
    summary = at.attribute(cap, fr, evs, fa.bound(cap, fr, evs))
    marks = [e for e in evs if e.kind == "mark"]
    assert firsts(marks) == ["asset_streaming"] * 6
    assert summary["asset_streaming"]["half_weight"] is True and summary["asset_streaming"]["weight"] == 1.5


def test_scans_stay_bounded_on_junk_time_spans():
    # a corrupt capture's times can span days: the scans coarsen instead of growing with the span. The
    # bounds are literals (a day of ticks, an hour of ticks), never read back from the module under test.
    assert len(at._quiet_windows(0.0, 2 * 86400.0, [], 0.5)) <= 345_600

    class Counting(at._Series):
        calls = 0

        def mean(self, a, b):
            Counting.calls += 1
            return super().mean(a, b)

    s = Counting([0.0, 2 * 86400.0], [1.0, 2.0])
    assert s.peak(0.0, 2 * 86400.0, +1) is not None
    assert Counting.calls <= 14_400


def test_others_are_stored_only_where_a_process_was_listed():
    ticks = [0.25 * (k + 1) for k in range(1000)]
    others = [[t, [[1, "a.exe", 10.0, 0.0, 0.0]] + ([[2, "b.exe", 80.0, 0.0, 0.0]] if k == 500 else [])]
              for k, t in enumerate(ticks)]
    cap = Capture(started_at="x", clock_origin=0.0, duration_s=250.0, target={}, sources={}, inventory={},
                  others=others)
    o = at._others(cap)
    b = o.per[(2, "b.exe")]["cpu"]
    assert b.t == [ticks[500]] and b.v == [80.0]
    # an unlisted tick counts as 0: over the 4 ticks of the second around it, the mean is 20 %
    assert b.mean(ticks[500] - 0.5, ticks[500] + 0.25) == pytest.approx(80.0 / 4)
    assert o.by_name["a.exe"]["cpu"].mean(10.0, 11.0) == pytest.approx(10.0)


def test_no_events_no_summary():
    cap = lag_cap(times=())
    fr = fa.main_chain(cap)
    assert at.attribute(cap, fr, [], None) == {}
