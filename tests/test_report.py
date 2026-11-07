"""The console and HTML reports: escaping, self-containment, spikes survive, honesty about gaps."""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from lagdoctor.model import (F, N, S, Capture, Diagnosis, Evidence, Finding, FixSuggestion, LagEvent,
                             SeriesBuilder, SourceStatus)
from lagdoctor.report import charts, console, html as html_report, labels
from lagdoctor.winapi import power as winapi_power

EVIL = '<script>alert(1)</script>'
EVIL_ATTR = '"><img src=x onerror=alert(1)>'


def make_capture(name="game.exe", n_frames=3000, spike_at=(1000,), with_frames=True, adapter_name="RTX"):
    t, ms, x = [], [], 0.0
    for i in range(n_frames):
        f = 400.0 if i in spike_at else 8.33
        t.append(x)
        ms.append(f)
        x += f / 1000
    sysb = SeriesBuilder(S.ALL)
    netb = SeriesBuilder(N.ALL)
    k = 0
    while k * 0.25 < x:
        sysb.add(t=k * 0.25, game_gpu_pct=97.0, game_top_thread_pct=40.0, cpu_total_pct=20.0,
                 vram_used_mb=6000.0, game_shared_mb=300.0)
        netb.add(t=k * 0.25, gw_rtt_ms=3.0, inet_rtt_ms=None if k % 40 == 7 else 24.0)
        k += 1
    sources = {"frames": SourceStatus("ok", tool="PresentMon 2.6.0") if with_frames else
               SourceStatus("unavailable", "PresentMon needs admin rights (access denied)"),
               "system": SourceStatus("ok"), "network": SourceStatus("ok"),
               "net_flows": SourceStatus("unavailable", "needs ETW rights")}
    return Capture(
        started_at="2026-11-04T15:30:00+00:00", clock_origin=100.0, duration_s=x,
        target={"pid": 1, "name": name, "adapter_luid": "0x00000000_0x0000AAAA"}, sources=sources,
        inventory={"os": {"name": "Windows 11 Home", "build": 26200},
                   "cpu": {"name": "i7", "cores": 10, "threads": 16, "hybrid": True}, "ram_gb": 31.6,
                   "adapters": [{"luid": "0x00000000_0x0000AAAA", "name": adapter_name, "vendor": "NVIDIA",
                                 "vram_mb": 8192, "integrated": False}],
                   "displays": [{"device": r"\\.\DISPLAY1", "name": "Panel", "internal": True, "primary": True,
                                 "width": 2560, "height": 1440, "hz": 144.0, "max_hz": 165.0}]},
        frames={F.T: t, F.FRAME_MS: ms} if with_frames else {},
        system=sysb.build(), network=netb.build(), marks=[5.0])


def make_diagnosis(headline="Your game stutters while loading from disk.", evidence_text="19 of 23 stutters"):
    return Diagnosis(
        lag_type="stutter", headline=headline,
        stats={"frames": 3000, "avg_fps": 87.0, "low1_fps": 31.0, "stutters_per_min": 4.4,
               "worst_frame_ms": 400.0, "refresh_hz": 144.0, "ping_inet_ms": 24.0, "loss_pct": 2.5},
        events=[LagEvent(id=1, kind="stutter", t_start=8.3, t_end=8.7, severity=400.0,
                         detail={"worst_t": 8.33}, causes=["asset_streaming"]),
                LagEvent(id=2, kind="fps_drop", t_start=12.0, t_end=15.0, severity=35.0, causes=["background_cpu"])],
        findings=[Finding(cause="asset_streaming", title="The game stalls while loading from disk",
                          category="stutter", confidence="high", score=0.9, summary="It reads mid-frame.",
                          evidence=[Evidence(evidence_text)],
                          fixes=[FixSuggestion("Move the game to an SSD", "Steam: Properties > Installed Files"),
                                 FixSuggestion("Use the High performance power plan", fix_id="power_plan",
                                               needs_restart="game")])],
        measured={"frames": "ok", "net_flows": "needs ETW rights"})


class TagBalance(HTMLParser):
    VOID = {"meta", "br", "circle", "line", "rect", "path", "i"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.tags = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.tags.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}> with open {self.stack[-3:]}")
        else:
            self.stack.pop()


def test_html_is_self_contained_with_no_script_or_url():
    page = html_report.render(make_diagnosis(), make_capture())
    low = page.lower()
    for bad in ("<script", "http:", "https:", " src=", "href=", "url(", "<link", "@import"):
        assert bad not in low, bad
    assert "@media (prefers-color-scheme: dark)" in page


def test_untrusted_strings_are_escaped_everywhere():
    cap = make_capture(name=EVIL, adapter_name=EVIL_ATTR)
    page = html_report.render(make_diagnosis(headline=EVIL, evidence_text=EVIL_ATTR), cap)
    assert "<script" not in page.lower()
    assert "<img" not in page.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    p = TagBalance()
    p.feed(page)
    assert "img" not in p.tags and "script" not in p.tags


def test_html_tags_are_balanced():
    p = TagBalance()
    p.feed(html_report.render(make_diagnosis(), make_capture()))
    assert p.errors == [] and p.stack == []


def test_downsampling_keeps_a_single_spike_among_many_smooth_frames():
    xs = [i * 0.008 for i in range(100_000)]
    ys = [8.0] * 100_000
    ys[61_234] = 400.0
    pts = [p for p in charts.downsample(xs, ys, 0.0, xs[-1], 900, agg="max") if p]
    assert max(y for _, y in pts) == 400.0
    mean_pts = [p for p in charts.downsample(xs, ys, 0.0, xs[-1], 900, agg="mean") if p]
    # Averaging would have hidden it: that bucket's mean is (110 * 8 + 400) / 111 = 11.5 ms.
    assert max(y for _, y in mean_pts) == pytest.approx(11.53, abs=0.01)


def test_the_frame_chart_draws_the_spike_at_the_top():
    cap = make_capture(n_frames=20_000, spike_at=(15_000,))
    page = html_report.render(make_diagnosis(), cap)
    svg = page[page.index("<h2>Frame times</h2>"):page.index("<h2>What is causing it</h2>")]
    d = re.search(r'<path class="ln s1" d="([^"]+)"', svg).group(1)
    ys = [float(m) for m in re.findall(r"[ML][\d.]+,([\d.]+)", d)]
    assert min(ys) == pytest.approx(charts.TOP, abs=0.2)     # the highest point sits at the plot's top edge


def test_missing_sources_are_reported_not_hidden():
    cap = make_capture(with_frames=False)
    page = html_report.render(make_diagnosis(), cap)
    assert "Not measured" in page
    assert "PresentMon needs admin rights" in page
    assert "needs ETW rights" in page
    text = console.render(make_diagnosis(), cap)
    assert "Not measured, so not ruled out" in text and "PresentMon needs admin rights" in text


def _visible(page: str) -> str:
    """The HTML report's text as a reader sees it: no style block, no tags, entities decoded."""
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<style>.*?</style>", "", page, flags=re.S)))


def test_missing_sources_are_named_in_words_in_both_reports():
    # Ids like net_flows are jargon: each report names a source as a player would (literals here, not
    # labels.SOURCES). A source the labels do not know is still listed, and so is a standard source the capture
    # never recorded, which only the HTML report listed before.
    cap = make_capture(with_frames=False)               # frames and net_flows unavailable, no "gpu" source at all
    cap.sources["future_probe"] = SourceStatus("failed", "it crashed")
    cap.sources["odd"] = "damaged"                       # not a SourceStatus: a damaged capture file
    d = make_diagnosis()
    d.measured = {"gpu": "not recorded in this capture", "system": "ok", "net_flows": "needs ETW rights",
                  "latency_probe": "ok"}                 # measured, though the capture's own list lacks it
    text = " ".join(console.render(d, cap, width=300).split())
    page = " ".join(_visible(html_report.render(d, cap)).split())
    for out in (text, page):
        assert "frame timing (PresentMon): PresentMon needs admin rights (access denied)" in out
        assert out.count("the game's own network traffic (ETW): needs ETW rights") == 1
        assert "NVIDIA telemetry (NVML): not recorded in this capture" in out
        assert "future probe: it crashed" in out
        assert "odd: not measured" in out
        assert "the system sampler:" not in out and "latency probe:" not in out
        assert not re.search(r"\b(net_flows|future_probe)\b", out)


def test_lost_pings_are_drawn_as_marks():
    page = html_report.render(make_diagnosis(), make_capture())
    net = page[page.index("<h2>Network</h2>"):page.index("<h2>Worst moments</h2>")]
    assert "lost ping (internet)" in net
    assert net.count('<circle class="dot bad"') >= 2


def test_fix_commands_and_restart_notes_are_shown():
    page = html_report.render(make_diagnosis(), make_capture())
    assert "lagdoctor fix power_plan" in page
    assert "Restart the game afterwards." in page
    assert "lagdoctor undo" in page


def test_console_is_ascii_and_wrapped_even_with_unicode_input():
    d = make_diagnosis(headline="Your game \u201cstutters\u201d \u2013 often\u2026 " * 8)
    text = console.render(d, make_capture(), width=80)
    assert text.isascii()
    assert all(len(line) <= 80 for line in text.splitlines()), max(text.splitlines(), key=len)
    # The command sits alone on its own line, so a copy never picks up prose or a line break.
    assert "       run: lagdoctor fix power_plan" in text.splitlines()


def test_write_is_atomic(tmp_path):
    out = html_report.write(make_diagnosis(), make_capture(), tmp_path / "report.html")
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert not list(tmp_path.glob("*.tmp"))


def _segments(path_d: str) -> int:
    return path_d.count("M")


def test_a_line_sampled_more_sparsely_than_pixels_stays_continuous():
    # 4 samples a second over 168 s on a ~900-pixel chart: many pixel columns have no sample.
    xs = [i * 0.25 for i in range(672)]
    ch = charts.Chart("t", 0.0, 168.0, 0.0, 100.0).line(xs, [50.0] * len(xs), "s1", "load", agg="mean")
    d = re.search(r'd="([^"]+)"', ch.svg()).group(1)
    assert _segments(d) == 1, f"line broke into {_segments(d)} pieces"


def test_a_real_missing_value_still_breaks_the_line():
    xs = [i * 0.25 for i in range(672)]
    ys = [50.0] * len(xs)
    for i in range(300, 304):          # a whole second of lost samples
        ys[i] = None
    ch = charts.Chart("t", 0.0, 168.0, 0.0, 100.0).line(xs, ys, "s1", "load", agg="mean")
    d = re.search(r'd="([^"]+)"', ch.svg()).group(1)
    assert _segments(d) == 2


def test_spikes_cut_off_at_the_top_are_explained():
    page = html_report.render(make_diagnosis(), make_capture())
    assert "cut off at the top; the worst took 400 ms" in page


def test_charts_leave_out_points_that_are_not_sane_numbers():
    # A15 at the charts' own seam: a damaged capture can put anything in a column
    assert [charts.number(v) for v in (3, 2.5, -1e7, 1e7 + 1, float("nan"), float("inf"), "4", True, None)] == \
        [3.0, 2.5, -1e7, None, None, None, None, None, None]
    assert charts.fmt_clock(75.4) == "1:15" and charts.fmt_clock(-3) == "0:00"
    assert [charts.fmt_clock(v) for v in (None, float("nan"), 1e308, "soon")] == ["-"] * 4
    xs = [0.0, "x", float("nan"), 1e308, 2.0, 3.0, None]
    ys = [5.0, 5.0, 5.0, 5.0, "busy", float("inf"), 5.0]
    pts = charts.downsample(xs, ys, 0.0, 4.0, 4)
    assert pts == [(0.5, 5.0), None, None]          # x junk dropped; y junk at 2 and 3 s is a gap
    ch = charts.Chart("t", float("nan"), "end", None, float("inf"))
    assert (ch.x0, ch.x1, ch.y0, ch.y1) == (0.0, 1.0, 0.0, 1.0)
    ch = charts.Chart("t", 0.0, 10.0, 0.0, 100.0)
    ch.dots([(1.0, 50.0), ("x", 50.0), (2.0, float("nan")), (1e308, 1.0)], "bad", "stutter")
    ch.vline("later", "mark", "m").vline(float("nan"), "mark", "m").vline(1e308, "mark", "m").vline(5.0, "mark", "m")
    ch.band(None, 5.0, "drop", "d").band(2.0, "soon", "drop", "d").band(2.0, 4.0, "drop", "d")
    ch.hline(float("nan"), "ref", "r").hline("x", "ref", "r").hline(50.0, "ref", "r")
    svg = ch.svg()
    assert svg.count("<circle") == 1 and svg.count('class="vl mark"') == 1
    assert svg.count('class="band drop"') == 1 and svg.count('class="hl ref"') == 1
    assert "nan" not in svg.lower() and "inf" not in svg.lower()


def test_time_ticks_land_on_clock_friendly_steps():
    assert charts.time_ticks(0, 168, 8) == [0, 30, 60, 90, 120, 150]
    assert charts.time_ticks(0, 3600, 8) == [0, 600, 1200, 1800, 2400, 3000, 3600]
    assert charts.time_ticks(0, 20, 8) == [0, 5, 10, 15, 20]


def test_nice_ticks_and_clock_format_literal_points():
    assert charts.nice_ticks(0, 100, 4) == [0, 25, 50, 75, 100]
    assert charts.nice_ticks(0, 40, 4) == [0, 10, 20, 30, 40]
    assert charts.nice_ticks(0, 300, 8) == [0, 50, 100, 150, 200, 250, 300]
    assert charts.fmt_clock(0) == "0:00" and charts.fmt_clock(75.4) == "1:15" and charts.fmt_clock(3600) == "60:00"


def test_checked_and_fine_and_could_not_check_are_both_shown():
    d = make_diagnosis()
    d.cleared = {"gpu_throttle": "the GPU held 2100 MHz with no throttle reason for the whole capture"}
    d.unchecked = {"driver_dpc": "interrupt counters were not readable"}
    page = html_report.render(d, make_capture())
    assert "<h2>Checked and fine</h2>" in page
    assert "<strong>GPU heat and power limits</strong>: the GPU held 2100 MHz" in page
    assert "Causes that could not be checked" in page
    assert "<strong>driver delays (DPC and interrupts)</strong>: interrupt counters were not readable" in page
    text = console.render(d, make_capture())
    # Topics, not problem labels: "checked and fine: the GPU slowing down" would say the opposite.
    assert "Checked and fine: GPU heat and power limits" in text
    assert "- driver delays (DPC and interrupts): interrupt counters were not readable" in text


def test_nothing_cleared_means_no_checked_and_fine_section():
    page = html_report.render(make_diagnosis(), make_capture())
    assert "Checked and fine" not in page
    assert "Checked and fine" not in console.render(make_diagnosis(), make_capture())


def test_the_limited_by_tile_shows_the_bound_verdict():
    d = make_diagnosis()
    d.stats["bound"] = "cpu_main"
    page = html_report.render(d, make_capture())
    assert '<div class="v">CPU (1 thread)</div><div class="k">Limited by</div>' in page
    assert "limited by CPU (1 thread)" in console.render(d, make_capture())
    d.stats.pop("bound")
    assert '<div class="v">-</div><div class="k">Limited by</div>' in html_report.render(d, make_capture())


def test_the_frame_chart_uses_only_the_main_swap_chain():
    cap = make_capture(n_frames=3000, spike_at=())
    n = len(cap.frames[F.T])
    cap.frames[F.SWAP_CHAIN] = ["0x1A2B"] * n
    cap.frames[F.SWAP_CHAIN][500] = "0x0"            # one frame of a side chain ...
    cap.frames[F.FRAME_MS][500] = 900.0              # ... that is very slow
    d = make_diagnosis()
    d.stats["main_chain"] = "0x1A2B"
    assert "the worst took 900 ms" not in html_report.render(d, cap)
    d.stats.pop("main_chain")                        # without a main chain every frame is charted
    assert "the worst took 900 ms" in html_report.render(d, cap)


def test_causes_blocked_by_the_same_missing_source_are_listed_together():
    from lagdoctor.report import labels
    groups = labels.group_unchecked({
        "game_stall": "needs frame timing (PresentMon), which was not measured: no admin rights; press the lag key",
        "gpu_limit": "needs frame timing (PresentMon), which was not measured: no admin rights",
        "wifi_weak": "needs the ping and Wi-Fi monitor, which was not measured: off"})
    assert groups == [("needs frame timing (PresentMon), which was not measured: no admin rights",
                       ["GPU load", "the game waiting on itself"]),
                      ("needs the ping and Wi-Fi monitor, which was not measured: off", ["Wi-Fi signal"])]


def test_every_cause_has_a_problem_label_and_a_neutral_topic():
    import re
    from pathlib import Path
    from lagdoctor.analysis import rules
    from lagdoctor.report import labels
    src = (Path(__file__).resolve().parents[1] / "lagdoctor" / "analysis" / "attribute.py").read_text(encoding="utf-8")
    produced = {r.cause for r in rules.RULES} | set(re.findall(r'_Cause\("([a-z_]+)"', src)) | {"game_stall", "gpu_spike"}
    assert len(produced) > 30                       # the scan found the real lists, not nothing
    assert produced <= set(labels.CAUSES), sorted(produced - set(labels.CAUSES))
    assert set(labels.TOPICS) == set(labels.CAUSES), sorted(set(labels.TOPICS) ^ set(labels.CAUSES))


def test_the_settings_check_up_reads_as_a_check_up():
    cap = make_capture(with_frames=False)
    d = make_diagnosis(headline="Frame timing was not measured (not recorded in this capture), so ...")
    d.unchecked = {f"c{i}": "needs frame timing (PresentMon), which was not measured: not recorded" for i in range(30)}
    text = console.render(d, cap, checkup=True)
    assert text.startswith("lag-doctor settings check-up (no game recorded")
    assert "VERDICT" not in text and "Frame timing was not measured" not in text and "fps |" not in text
    assert "Settings that can cost performance" in text
    assert "Not checked without a recording (30 more causes" in text and "lagdoctor watch" in text
    assert "Could not check:" not in text and "Not measured, so not ruled out" not in text


def test_lost_pings_come_from_the_collectors_record_not_from_every_gap():
    cap = make_capture()
    n_none = sum(1 for v in cap.network[N.INET_RTT_MS] if v is None)
    assert n_none >= 2
    cap.net_flows = {"lost": {"internet": [1.75]}}          # only one of those gaps was a probe that got no reply
    net = html_report.render(make_diagnosis(), cap)
    net = net[net.index("<h2>Network</h2>"):net.index("<h2>Worst moments</h2>")]
    assert net.count('<circle class="dot bad"') == 1


def section(page: str, heading: str) -> str:
    """The HTML between <h2>heading</h2> and the next <h2>."""
    start = page.index(f"<h2>{heading}</h2>")
    nxt = page.find("<h2>", start + 4)
    return page[start:nxt if nxt >= 0 else len(page)]


def test_the_console_never_prints_a_control_character_from_a_capture():
    # SEC-8: ESC and BEL in a capture's strings drove the terminal (OSC 52 clipboard writes, cursor moves)
    cap = make_capture(name="Game\x1b]52;c;ZXZpbA==\x07.exe")
    cap.started_at = "2026-11-04T15:30\x1b[2J\x1b[H"
    cap.sources["net_flows"] = SourceStatus("unavailable", "\x1b]8;;x\x07needs ETW rights\x1b]8;;\x07\r")
    d = make_diagnosis(headline="Stutter\x1b[1A\x1b[2K", evidence_text="19 of 23\rstutters\x7f\x08\t")
    d.findings[0].title = "Loading\x07 from disk"
    d.unchecked = {"driver_dpc": "counters \x1b[31mred\x1b[0m"}
    d.cleared = {"gpu_\x1b[5m": "fine"}
    text = console.render(d, cap)
    bad = sorted({hex(ord(c)) for c in text if (ord(c) < 32 and c != "\n") or ord(c) == 127})
    assert bad == [], bad
    assert "Game?]52;c;ZXZpbA==?.exe" in text and "needs ETW rights" in text and "19 of 23?stutters" in text


def test_an_animation_error_pacing_event_reads_as_animation_error_in_the_worst_moments():
    # A11: every shown frame held one refresh, so the event's severity is its animation error, not a swing
    d = make_diagnosis()
    d.events.append(LagEvent(id=3, kind="pacing", t_start=1.0, t_end=19.0, severity=6.4, detail={"criterion": "anim"}))
    d.events.append(LagEvent(id=4, kind="pacing", t_start=20.0, t_end=22.0, severity=7.0, detail={"criterion": "swing"}))
    worst = section(html_report.render(d, make_capture()), "Worst moments")
    assert "6.4 ms animation error" in worst and "7.0 ms swings" in worst and "6.4 ms swings" not in worst


ODD = [None, float("nan"), float("inf"), -float("inf"), 1e308, "fast", [1], True, {"x": 1}]


def odd_diagnosis():
    d = make_diagnosis()
    for i, key in enumerate(("avg_fps", "low1_fps", "stutters_per_min", "worst_frame_ms", "ping_inet_ms", "jitter_ms",
                             "loss_pct", "frames", "refresh_hz")):
        d.stats[key] = ODD[(i + 1) % len(ODD)]
    d.stats["bound"] = ["cpu_main"]
    d.stats["marks"] = [{"t": float("nan"), "events": [1]}, "a mark", {"t": 5.0, "events": "all", "causes": 7},
                        {"t": 6.0, "events": [1, "x", None], "causes": ["background_cpu", 3]},
                        {"t": 7.0, "events": [True]}]                  # True is no event id (it equals 1)
    d.events += [LagEvent(id=7, kind="stutter", t_start=1.0, t_end=1.2, severity=float("nan"), detail=None),
                 LagEvent(id=8, kind="net_spike", t_start=2.0, t_end=1e308, severity=1e308, detail={"worst_t": "x"}),
                 LagEvent(id=9, kind="fps_drop", t_start="soon", t_end=None, severity="bad"),
                 LagEvent(id=10, kind="pacing", t_start=float("nan"), t_end=3.0, severity=4.0, detail=[1])]
    return d


def damaged_capture():
    cap = make_capture()
    n = len(cap.frames[F.T])
    cap.frames[F.T][3], cap.frames[F.T][4], cap.frames[F.T][5] = "x", float("nan"), 1e308
    cap.frames[F.FRAME_MS][6], cap.frames[F.FRAME_MS][7], cap.frames[F.FRAME_MS][8] = "lots", -5.0, None
    cap.frames[F.SWAP_CHAIN] = ["0x1"] * (n - 1)                       # one value short
    cap.system[S.GAME_GPU_PCT][2], cap.system[S.CPU_TOTAL_PCT][3] = "busy", float("inf")
    cap.system[S.VRAM_USED_MB][1], cap.system[S.T][9] = 1e308, "later"
    cap.network[N.INET_RTT_MS][1], cap.network[N.T][2] = "fast", None
    cap.inventory.update(os="Windows", cpu=["i7"], ram_gb="lots", power={"slider": 5, "on_battery": [1]},
                         settings="on", game=None, network=7)
    cap.inventory["adapters"] += ["not an adapter", {"luid": "0x00000000_0x0000AAAA", "vram_mb": "eight"}]
    cap.inventory["displays"] += [{"hz": "fast"}, None]
    cap.net_flows = {"lost": [1.0, 2.0]}
    cap.marks = ["later", float("nan"), 1e308, 5.0]
    cap.duration_s = 1e308
    return cap


def test_odd_values_in_the_stats_events_and_capture_read_as_not_measured_never_crash():
    # A15: a damaged capture once took html.write down (a string in a column, a non-dict inventory section), and
    # with it the report the user was waiting for. Every part must still be drawn: no part may need the fallback
    d, cap = odd_diagnosis(), damaged_capture()
    text = console.render(d, cap)
    assert "VERDICT [STUTTER]" in text and "could not be shown" not in text
    assert ("avg - fps | 1% low - fps | - stutters/min | worst frame - | limited by - | ping - | loss -"
            in " ".join(text.split()))
    assert ("You marked 3 moments: 1 matched a stutter, FPS drop or network event; 2 showed nothing in what was "
            "measured.") in " ".join(text.split())
    page = html_report.render(d, cap)
    assert "could not be drawn" not in page
    for heading in ("Frame times", "What is causing it", "CPU and GPU", "Video memory", "Network", "Worst moments",
                    "Moments you marked", "This PC"):
        assert f"<h2>{heading}</h2>" in page, heading
    for tile in ("Average FPS", "1% low FPS", "Worst frame", "Limited by", "Jitter", "Packet loss"):
        assert f'<div class="v">-</div><div class="k">{tile}</div>' in page, tile
    worst = section(page, "Worst moments")
    assert "<td>0:01</td><td>Stutter</td><td>-</td>" in worst and "<td>0:02</td><td>Ping spike</td><td>-</td>" in worst
    assert "FPS drop" not in worst.replace("<td>0:12</td><td>FPS drop</td>", "")   # the one without a time is left out
    for junk in (text, page):
        assert not re.search(r"\b(nan|inf|1e\+308)\b", junk, flags=re.I)
    p = TagBalance()
    p.feed(page)
    assert p.errors == [] and p.stack == []


@pytest.mark.parametrize("power, text", [
    ({"scheme_name": "Silent", "personality": "balanced", "slider": "best_performance", "on_battery": False},
     "plan Silent; power mode best performance; plugged in"),
    ({"scheme_name": "High performance", "personality": "high_performance", "slider": None, "on_battery": False},
     "plan High performance; no power mode on this plan; plugged in"),
    # The plan's type wins over a position the capture still holds: that plan has no slider to set
    ({"scheme_name": "Power saver", "personality": "power_saver", "slider": "balanced", "on_battery": True},
     "plan Power saver; no power mode on this plan; on battery"),
    # Saved before the plan's type was read
    ({"scheme_name": "Silent", "slider": "balanced", "on_battery": False},
     "plan Silent; power mode balanced; plugged in"),
    ({"scheme_name": "Silent", "personality": "balanced", "slider": None, "on_battery": None},
     "plan Silent; power mode ?; power source unknown"),
])
def test_the_pc_details_name_a_power_mode_only_where_the_plan_has_one(power, text):
    cap = make_capture()
    cap.inventory["power"] = power
    assert f"<tr><th>Power</th><td>{text}</td></tr>" in html_report.render(make_diagnosis(), cap)


@pytest.mark.parametrize("power, has, mode", [
    ({"personality": "balanced", "slider": "best_efficiency"}, True, "best_efficiency"),
    ({"personality": "high_performance", "slider": "best_efficiency"}, False, None),
    ({"personality": "power_saver", "slider": "balanced"}, False, None),
    ({"slider": "best_efficiency"}, None, "best_efficiency"),            # saved before the type was read
    ({"personality": "turbo", "slider": "balanced"}, None, "balanced"),   # an unknown type is not "no slider"
    ({"personality": ["balanced"], "slider": 5}, None, None),
    ({"slider": "fast"}, None, None),
    ("power", None, None),
])
def test_power_mode_and_whether_the_plan_has_one_from_a_capture(power, has, mode):
    assert labels.plan_has_power_mode(power) is has
    assert labels.power_mode(power) == mode


def test_the_reports_plan_types_and_slider_positions_are_the_ones_lag_doctor_reads():
    # The report side and winapi/power.py each list the plan types and slider positions: pinned together, so a type
    # added to one is not silently "unknown" to the other.
    p = winapi_power
    assert set(labels.PLAN_TYPES_WITHOUT_POWER_MODE) | {p.SLIDER_PERSONALITY} == set(p.PERSONALITIES.values())
    for kind in p.PERSONALITIES.values():
        assert labels.plan_has_power_mode({"personality": kind}) is (kind == p.SLIDER_PERSONALITY), kind
    assert set(labels.POWER_MODES) == set(p.SLIDER_MODES.values())


def test_a_part_that_still_breaks_is_announced_and_the_rest_of_the_report_is_written(monkeypatch):
    # A15's last line of defence: one broken part becomes a note, never a lost report
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(html_report, "_network_chart", broken)
    page = html_report.render(make_diagnosis(), make_capture())
    net = section(page, "Network")
    assert "This part of the report could not be drawn from this capture (RuntimeError)." in net
    assert "<h2>Worst moments</h2>" in page and "lagdoctor fix power_plan" in page and page.endswith("</html>")
    monkeypatch.setattr(console, "_cleared", broken)
    d = make_diagnosis()
    d.cleared = {"gpu_throttle": "fine"}
    d.unchecked = {"driver_dpc": "interrupt counters were not readable"}
    text = console.render(d, make_capture())
    assert "(Part of this summary could not be shown from this capture: RuntimeError.)" in text
    assert "VERDICT" in text and "Could not check:" in text


def marked_diagnosis():
    d = make_diagnosis()
    d.stats["marks"] = [{"t": 5.0, "events": [1], "causes": []}, {"t": 9.0, "events": [], "causes": ["background_cpu"]},
                        {"t": 14.0, "events": [], "causes": []}]
    return d


def test_the_marked_moments_are_listed_with_what_was_found_at_each():
    # CC-5: the user's lag-key presses reached no part of the report but a line on the frame chart
    d = marked_diagnosis()
    page = html_report.render(d, make_capture())
    marks = section(page, "Moments you marked")
    assert ("You marked 3 moments: 1 matched a stutter, FPS drop or network event; 1 had no lag event but a system "
            "signal moved; 1 showed nothing in what was measured. No stutter or FPS drop was measured at those "
            "moments.") in marks
    assert "<td>0:05</td><td>Stutter (400 ms frame)</td>" in marks
    assert "<td>0:09</td><td>No lag event; meanwhile another program using the CPU</td>" in marks
    assert "<td>0:14</td><td>Nothing in what was measured</td>" in marks
    text = " ".join(console.render(d, make_capture()).split())
    assert ("You marked 3 moments: 1 matched a stutter, FPS drop or network event; 1 had no lag event but a system "
            "signal moved; 1 showed nothing") in text
    assert "- 0:05: Stutter (400 ms frame)" in text and "- 0:09: no lag event; meanwhile another program using the CPU" \
        in text and "- nothing at 0:14" in text
    d.measured["frames"] = "PresentMon needs admin rights (access denied)"
    unseen = "Frame timing was not measured, so a stutter at those moments could not be seen."
    assert unseen in " ".join(console.render(d, make_capture()).split())
    assert unseen in section(html_report.render(d, make_capture()), "Moments you marked")


def test_marks_are_drawn_on_the_frame_cpu_gpu_and_network_charts():
    page = html_report.render(marked_diagnosis(), make_capture())
    for heading in ("Frame times", "CPU and GPU", "Network"):
        assert section(page, heading).count('class="vl mark"') == 3, heading
    assert 'class="vl mark"' not in section(page, "Video memory")


def test_no_marks_no_marked_moments_section():
    d = make_diagnosis()
    d.stats["marks"] = []
    page = html_report.render(d, make_capture())                  # the capture lists a press at 5 s ...
    assert "Moments you marked" not in page and 'class="vl mark"' not in page     # ... the analysis found none
    assert "You marked" not in console.render(d, make_capture())
    older = html_report.render(make_diagnosis(), make_capture())  # a diagnosis without the key: the capture's own
    assert "Moments you marked" not in older and section(older, "Frame times").count('class="vl mark"') == 1


def test_console_shows_at_most_three_findings():
    d = make_diagnosis()
    base = d.findings[0]
    d.findings = [Finding(cause=f"c{i}", title=f"Finding number {i}", category="stutter", confidence="medium",
                          score=1 - i / 10, summary="") for i in range(5)]
    text = console.render(d, make_capture())
    assert "3. Finding number 2" in text and "Finding number 3" not in text
    assert "... and 2 more in the full report." in text
    assert base.title not in text
