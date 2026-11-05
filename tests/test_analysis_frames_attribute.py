"""analysis/attribute.py: the active test, the lift test, explaining away, sides and residuals."""
from __future__ import annotations

import pytest

from lagdoctor.analysis import attribute as at
from lagdoctor.analysis import events as ev
from lagdoctor.analysis import frames as fa
from lagdoctor.model import F, G, S, Capture, SeriesBuilder

TIMES = (4.0, 8.0, 12.0, 16.0, 20.0, 24.0)
BASE = {S.GAME_TOP_THREAD_PCT: 25.0, S.GAME_CPU_PCT: 30.0, S.CPU_TOTAL_PCT: 10.0, S.GAME_IO_READ_BPS: 0.0,
        S.DISK_BUSY_PCT: 2.0, S.DISK_READ_MS: 0.2, S.HARD_FAULTS_PS: 5.0, S.RAM_AVAIL_MB: 20000.0,
        S.GPU_OTHER_PCT: 2.0, S.DPC_PCT: 0.5, S.INTERRUPT_PCT: 0.4, S.CPU_PERF_PCT: 105.0,
        S.CPU_PERF_LIMIT_PCT: 100.0, S.GAME_FOCUSED: 1, S.VRAM_USED_MB: 3000.0, S.GAME_SHARED_MB: 100.0}
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
    ("vram_overflow", 0, 100.0, 100.0 + 256 / 60 + 0.001, 100.0 + 256 / 60 - 0.01),   # +256 MiB/min
    ("not_focused", 0, 0.0, 0.5, 0.49),                 # out of focus half of that second
])
def test_each_signal_floor(cause, k, normal, ok, short):
    part = parts_of()[cause].parts[k]
    base = at._Baseline(normal, 0.0)                  # MAD 0: z is infinite, so only the floor decides
    assert at._judge(part, base, ok)[0], (cause, k)
    assert not at._judge(part, base, short)[0], (cause, k)


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
    events, summary = analyse(lag_cap(during={S.GAME_IO_READ_BPS: 30e6, S.GAME_TOP_THREAD_PCT: 60.0}))
    assert firsts(events) == ["asset_streaming"] * 6
    assert all(e.causes[1:] == ["game_cpu_spike"] for e in events)    # still listed, second
    assert summary["asset_streaming"]["share"] == 1.0 and summary["game_cpu_spike"]["share"] == 0.0


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


def test_a_stall_has_no_side():
    # rows are missing for 160 ms: no presented frame holds that time, so neither side can be named
    events, _ = analyse(lag_cap(stall=True))
    assert len(events) == 6 and all(e.detail["stall"] for e in events)
    assert {e.detail["side"] for e in events} == {None}


def test_page_faults_without_low_ram_are_not_paging():
    events, _ = analyse(lag_cap(during={S.HARD_FAULTS_PS: 5000.0}))
    assert "memory_paging" not in {c for e in events for c in e.causes}
    events, _ = analyse(lag_cap(during={S.HARD_FAULTS_PS: 5000.0}, base=dict(BASE, **{S.RAM_AVAIL_MB: 1000.0})))
    assert firsts(events) == ["memory_paging"] * 6


def test_the_window_starts_half_a_second_before_the_event():
    # the only moving sample is the tick 0.25 s before the stutter began
    events, _ = analyse(lag_cap(during={S.GAME_IO_READ_BPS: 30e6}, hot=(-0.3, -0.2)))
    assert firsts(events) == ["asset_streaming"] * 6


def test_the_window_ends_a_quarter_second_after_the_event():
    # the only moving sample is the tick about 0.08 s after the stutter frame was presented
    events, _ = analyse(lag_cap(during={S.GAME_IO_READ_BPS: 30e6}, hot=(0.2, 0.3)))
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
    cap = lag_cap(during={S.GAME_IO_READ_BPS: 30e6}, with_frames=False)
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
