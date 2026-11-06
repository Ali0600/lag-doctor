"""The Collector base class: one sampler thread with start / stop / status / result / live.

Subclasses implement `run()` (their loop) and `result()`. The base class gives
them the rules from docs/DESIGN.md#collector-contract for free:

- a crash inside `run()` becomes status "failed" with the reason, never an
  exception in the session;
- `stop()` is idempotent and bounded: it asks, waits up to a deadline, and
  reports a thread that would not stop instead of hanging;
- `ticks()` paces a loop on absolute deadlines and skips ahead when it falls
  behind, rather than firing a burst of catch-up samples;
- `publish()` / `live()` hand the latest values to the status line safely.
"""
from __future__ import annotations

import threading
import traceback
from typing import Any, Iterator

from lagdoctor import clock
from lagdoctor.model import SourceState, SourceStatus


class Collector:
    name: str = ""

    def __init__(self) -> None:
        self.origin = 0.0
        self._status = SourceStatus("off")
        self._status_lock = threading.Lock()
        self._live: dict[str, Any] = {}
        self._live_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.missed_ticks = 0
        self.crash_trace = ""

    # ---- lifecycle ------------------------------------------------------
    def start(self, origin: float) -> None:
        if self._thread is not None:
            raise RuntimeError(f"{self.name} collector already started")
        self.origin = origin
        self._thread = threading.Thread(target=self._run_safely, name=f"lagdoctor-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        try:
            self.on_stop()
        except Exception as exc:  # noqa: BLE001 - stop must never raise
            self.set_status("partial", f"error while stopping: {type(exc).__name__}: {exc}")
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.set_status("partial", f"{self.name} did not stop within {timeout:.0f} s; its data may be cut short")
        self._thread = None

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    @property
    def ended(self) -> bool:
        """Its run is over though nobody asked it to stop: started, its thread finished, no stop() yet."""
        t = self._thread
        return t is not None and not t.is_alive() and not self._stop_event.is_set()

    # ---- for subclasses -------------------------------------------------
    def run(self) -> None:
        raise NotImplementedError

    def on_stop(self) -> None:
        """Called from stop() before joining: unblock run() here (e.g. end a child process)."""

    def result(self) -> dict[str, Any]:
        raise NotImplementedError

    def now(self) -> float:
        """Seconds on the capture clock."""
        return clock.now() - self.origin

    def ticks(self, period: float) -> Iterator[float]:
        """Yield once per period (capture-clock time of the tick) until stop() is called."""
        start = clock.now()
        k = 0
        while not self._stop_event.is_set():
            yield clock.now() - self.origin
            k += 1
            deadline = start + k * period
            behind = clock.now() - deadline
            if behind > period:
                skipped = int(behind // period)
                self.missed_ticks += skipped
                k += skipped
                deadline = start + k * period
            wait = deadline - clock.now()
            if wait > 0 and self._stop_event.wait(wait):
                return

    def set_status(self, state: SourceState, detail: str = "", tool: str = "") -> None:
        with self._status_lock:
            self._status = SourceStatus(state, detail, tool or self._status.tool)

    def status(self) -> SourceStatus:
        with self._status_lock:
            return SourceStatus(self._status.state, self._status.detail, self._status.tool)

    def publish(self, **values: Any) -> None:
        with self._live_lock:
            self._live.update(values)

    def live(self) -> dict[str, Any]:
        with self._live_lock:
            return dict(self._live)

    # ---- internals ------------------------------------------------------
    def _run_safely(self) -> None:
        try:
            self.run()
        except Exception as exc:  # noqa: BLE001 - a collector crash must degrade, not kill the session
            self.crash_trace = traceback.format_exc()
            self.set_status("failed", f"{type(exc).__name__}: {exc}")
