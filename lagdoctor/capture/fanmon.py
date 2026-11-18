"""The fans collector: an ASUS laptop's fan speeds once a second (docs/DESIGN.md, Collector specifics -> fans).

It reads through winapi/atkacpi.py, which only ever reads. On a PC without ASUS's System Control Interface the
device does not open and this source is "unavailable" with the reason: lag-doctor has no other way to read fans.
A fan the firmware says the laptop does not have is never read again and stays None; a reading that failed is None
for that second; 0 is a fan standing still.

Its own thread, apart from the system sampler: a slow or stuck firmware call delays only these rows, never the
4 Hz CPU, GPU and disk ones.
"""
from __future__ import annotations

import statistics as st
import threading
from typing import Any, Callable

from lagdoctor import clock
from lagdoctor.capture.base import Collector
from lagdoctor.model import C, SeriesBuilder
from lagdoctor.winapi import atkacpi

PERIOD_S = 1.0
UNKNOWN_TRIES = 10       # a fan that errors this many times before it ever answers is given up on
TOOL = "ASUS System Control Interface"
COLUMN = {"cpu": C.CPU_FAN_RPM, "gpu": C.GPU_FAN_RPM, "mid": C.MID_FAN_RPM}
FAN_WORDS = {"cpu": "CPU fan", "gpu": "GPU fan", "mid": "middle fan"}
ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, ERROR_ACCESS_DENIED = 2, 3, 5
NO_DEVICE = ("this PC has no ASUS System Control Interface, the only way lag-doctor reads fan speeds "
             "(ASUS laptops have it)")


def open_reason(exc: OSError) -> str:
    """Why the ASUS device did not open, in words."""
    code = getattr(exc, "winerror", None)
    if code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return NO_DEVICE
    if code == ERROR_ACCESS_DENIED:
        return "Windows refused lag-doctor access to the ASUS System Control Interface"
    return f"the ASUS System Control Interface did not open ({exc})"


class FanSampler(Collector):
    """result(): {"columns": C columns, "device": {"fans": [names the firmware has]} or None,
    "cost": {"median_ms", "max_ms"} or None}."""

    name = "fans"

    def __init__(self, reader_factory: Callable[[], Any] | None = None, period_s: float = PERIOD_S) -> None:
        super().__init__()
        if not period_s > 0:
            raise ValueError(f"a fan sampling period must be positive, not {period_s!r}")
        self._factory = reader_factory or atkacpi.FanReader.open     # called in run(): constructing touches nothing
        self.period_s = period_s
        self.series = SeriesBuilder(C.ALL)
        self._lock = threading.Lock()                               # guards series, _present and _cost_ms
        self._present: list[str] | None = None
        self._cost_ms: list[float] = []

    # ---- Collector ------------------------------------------------------
    def run(self) -> None:
        try:
            reader = self._factory()
        except OSError as exc:
            self.set_status("unavailable", open_reason(exc), tool=TOOL)
            return
        try:
            self._loop(reader)
        finally:
            reader.close()

    def result(self) -> dict:
        with self._lock:
            columns = self.series.build() if len(self.series) else {}
            device = {"fans": list(self._present)} if self._present else None
            cost = _cost(self._cost_ms)
        return {"columns": columns, "device": device, "cost": cost}

    # ---- the loop -------------------------------------------------------
    def _loop(self, reader: Any) -> None:
        unknown = {fan: 0 for fan in atkacpi.FAN_IDS}    # not yet answered: fan -> its failed tries so far
        present: list[str] = []
        silent: dict[str, str] = {}                      # no answer in UNKNOWN_TRIES tries: fan -> its last error
        failed = 0
        for t in self.ticks(self.period_s):
            row: dict[str, Any] = {C.T: round(t, 3)}
            start = clock.now()
            for fan in present + list(unknown):
                try:
                    row[COLUMN[fan]] = reader.read(fan)
                except atkacpi.NoSuchFan:
                    if fan in unknown:           # never had it: drop it for good
                        del unknown[fan]
                    else:                        # had it a moment ago: a failed reading this second
                        failed += 1
                except OSError as exc:           # a corrupt reading (atkacpi.BadReading) included
                    if fan not in unknown:
                        failed += 1
                    else:                        # not yet answered: a failure only once it answers
                        unknown[fan] += 1
                        if unknown[fan] >= UNKNOWN_TRIES:
                            del unknown[fan]
                            silent[fan] = str(exc) or type(exc).__name__
                else:
                    if fan in unknown:
                        failed += unknown.pop(fan)
                        present.append(fan)
            ms = (clock.now() - start) * 1000.0
            with self._lock:
                self.series.add(**row)
                self._present = [f for f in atkacpi.FAN_IDS if f in present]
                self._cost_ms.append(ms)
            if not present and not unknown:      # nothing left to read
                break
        self._finish(present, list(unknown), silent, failed)

    def _finish(self, present: list[str], unknown: list[str], silent: dict[str, str], failed: int) -> None:
        none = not present and not unknown and not silent       # the firmware has none of the fans
        with self._lock:
            if none:                             # rows of nothing but times say nothing: keep none
                self.series = SeriesBuilder(C.ALL)
            n = len(self.series)
            cost = _cost(self._cost_ms)
        if none:
            self.set_status("unavailable", "the ASUS System Control Interface opened but reported no fans", tool=TOOL)
            return
        if n == 0:
            if self.stopping:
                self.set_status("partial", "stopped before the first fan reading", tool=TOOL)
            else:
                self.set_status("failed", "no fan reading was recorded", tool=TOOL)
            return
        if not present:
            last = next(iter(silent.values()), None)
            self.set_status("failed", f"the ASUS System Control Interface answered no fan reading in {n} tries"
                                      + (f" (the last error: {last})" if last else ""), tool=TOOL)
            return
        fans = " and ".join(FAN_WORDS[f] for f in atkacpi.FAN_IDS if f in present)
        detail = f"{fans}, {n} readings once every {self.period_s:g} s"
        if cost:
            detail += f"; {cost['median_ms']:.1f} ms a reading (max {cost['max_ms']:.1f} ms)"
        quiet = [FAN_WORDS[f] for f in atkacpi.FAN_IDS if f in silent or f in unknown]
        if quiet:                                # neither a fan nor a failure: never known to exist
            detail += f"; the {' and the '.join(quiet)} never answered"
        if failed:
            self.set_status("partial", f"{detail}; {failed} fan reading{'' if failed == 1 else 's'} failed", tool=TOOL)
        else:
            self.set_status("ok", detail, tool=TOOL)


def _cost(ms: list[float]) -> dict[str, float] | None:
    return {"median_ms": round(st.median(ms), 3), "max_ms": round(max(ms), 3)} if ms else None
