"""The Collector base class: pacing, crash containment, bounded stop."""
from __future__ import annotations

import threading
import time

from lagdoctor.capture.base import Collector
from lagdoctor.model import S, SeriesBuilder


class Ticker(Collector):
    name = "ticker"

    def __init__(self, period: float, work: float = 0.0) -> None:
        super().__init__()
        self.period, self.work = period, work
        self.series = SeriesBuilder(S.ALL)

    def run(self) -> None:
        self.set_status("ok")
        for t in self.ticks(self.period):
            self.series.add(t=t)
            self.publish(last=t)
            if self.work:
                time.sleep(self.work)

    def result(self):
        return {"columns": self.series.build()}


def test_ticks_run_at_the_requested_rate():
    c = Ticker(0.05)
    c.start(time.perf_counter())
    time.sleep(1.0)
    c.stop()
    n = len(c.series)
    assert 17 <= n <= 23, n                       # about 20 ticks in 1 s at 20 Hz
    t = c.series.build()["t"]
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert max(gaps) < 0.15
    assert c.status().state == "ok"


def test_a_slow_tick_skips_ahead_instead_of_bursting():
    c = Ticker(0.05, work=0.3)                    # each tick overruns by 5 periods
    c.start(time.perf_counter())
    time.sleep(1.0)
    c.stop()
    t = c.series.build()["t"]
    gaps = [b - a for a, b in zip(t, t[1:])]
    assert gaps and min(gaps) > 0.2, gaps          # no burst of back-to-back catch-up ticks
    assert c.missed_ticks > 0


def test_a_crash_in_run_becomes_a_failed_status():
    class Crasher(Collector):
        name = "crasher"

        def run(self):
            raise ValueError("counter vanished")

        def result(self):
            return {}

    c = Crasher()
    c.start(time.perf_counter())
    time.sleep(0.2)
    c.stop()
    st = c.status()
    assert st.state == "failed" and "counter vanished" in st.detail
    assert "ValueError" in c.crash_trace


def test_stop_is_bounded_and_reports_a_stuck_thread():
    release = threading.Event()

    class Stuck(Collector):
        name = "stuck"

        def run(self):
            self.set_status("ok")
            release.wait(10)                       # ignores the stop request

        def result(self):
            return {}

    c = Stuck()
    c.start(time.perf_counter())
    start = time.monotonic()
    c.stop(timeout=0.5)
    try:
        assert time.monotonic() - start < 2.0
        assert c.status().state == "partial" and "did not stop" in c.status().detail
    finally:
        release.set()


def test_stop_is_idempotent_and_safe_before_start():
    c = Ticker(0.05)
    c.stop()
    c.start(time.perf_counter())
    c.stop()
    c.stop()


def test_live_returns_a_copy():
    c = Ticker(0.05)
    c.publish(fps=60)
    snap = c.live()
    snap["fps"] = 1
    assert c.live() == {"fps": 60}
