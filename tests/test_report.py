"""The console and HTML reports: escaping, self-containment, spikes survive, honesty about gaps."""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from lagdoctor.model import (F, N, S, Capture, Diagnosis, Evidence, Finding, FixSuggestion, LagEvent,
                             SeriesBuilder, SourceStatus)
from lagdoctor.report import charts, console, html as html_report

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


def test_console_shows_at_most_three_findings():
    d = make_diagnosis()
    base = d.findings[0]
    d.findings = [Finding(cause=f"c{i}", title=f"Finding number {i}", category="stutter", confidence="medium",
                          score=1 - i / 10, summary="") for i in range(5)]
    text = console.render(d, make_capture())
    assert "3. Finding number 2" in text and "Finding number 3" not in text
    assert "... and 2 more in the full report." in text
    assert base.title not in text
