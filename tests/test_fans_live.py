"""The real fans of an ASUS laptop, read through the real device: read only, a few readings, no setting touched.

On a PC without the ASUS System Control Interface these fail rather than skip, unless LAGDOCTOR_ABSENT names
asus_acpi (tests/capabilities.py): a device that stopped opening on the laptop must never pass as "not an ASUS".
"""
from __future__ import annotations

import time

import pytest

from lagdoctor.capture import fanmon
from lagdoctor.model import C
from lagdoctor.winapi import atkacpi

pytestmark = pytest.mark.needs("asus_acpi")


def test_the_real_fans_read_as_hundreds_of_rpm_through_a_handle_with_no_rights():
    with atkacpi.FanReader.open() as r:
        read = {}
        for fan in atkacpi.FAN_IDS:
            try:
                read[fan] = r.read(fan)
            except atkacpi.NoSuchFan:
                read[fan] = None
    assert read["cpu"] is not None or read["gpu"] is not None, read
    for fan, rpm in read.items():
        assert rpm is None or (0 <= rpm <= 12_000 and rpm % 100 == 0), (fan, rpm)


def test_the_fans_collector_records_the_real_fans_once_each_period():
    c = fanmon.FanSampler(period_s=0.2)
    c.start(0.0)
    deadline = time.monotonic() + 5.0
    while len(c.series) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    c.stop()
    res, st = c.result(), c.status()
    assert st.state == "ok", st
    assert len(res["columns"][C.T]) >= 3 and res["device"]["fans"]
    for fan in res["device"]["fans"]:
        assert all(v is not None for v in res["columns"][fanmon.COLUMN[fan]]), fan
    assert res["cost"]["max_ms"] < 100.0, res["cost"]      # measured about 0.5 ms a read (2026-11-17)
