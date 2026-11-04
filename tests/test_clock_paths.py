"""The shared clock really is QPC, and LAGDOCTOR_HOME really redirects every file."""
from __future__ import annotations

import time

from lagdoctor import clock, paths


def test_perf_counter_is_qpc_on_this_machine():
    # Frame/sample alignment depends on this: PresentMon reports raw QPC ticks.
    before = time.perf_counter()
    qpc_s = clock.qpc_to_seconds(clock.qpc_now())
    after = time.perf_counter()
    assert before - 0.001 <= qpc_s <= after + 0.001, (before, qpc_s, after)


def test_qpc_frequency_is_plausible():
    assert 1_000_000 <= clock.qpc_frequency() <= 10_000_000_000


def test_home_override_redirects_all_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("LAGDOCTOR_HOME", str(tmp_path / "ld"))
    assert paths.home() == tmp_path / "ld"
    for p in (paths.sessions_dir(), paths.journal_path(), paths.presentmon_dir("2.6.0")):
        assert tmp_path in p.parents, p
