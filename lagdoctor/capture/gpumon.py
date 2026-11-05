"""The gpu collector: NVIDIA telemetry at 1 Hz (docs/DESIGN.md, Collector specifics -> gpu).

The source is NVML (winapi/nvml.py). When NVML cannot load, a streaming nvidia-smi
child takes over (-lms 1000; never one-shot calls, which cost about 100 ms each,
WT 4.3). It runs only when the game renders on the NVIDIA GPU or its adapter is
not known yet: polling NVML may wake a sleeping Optimus dGPU (WT 11.5 item 7), so
a game on another GPU leaves this source "off".

Throttling is a share, not a bit (WT 11.5 item 16): nv_throttle_pct is how much
the cumulative SW thermal, HW thermal and HW power-brake counters grew over the
row's interval, summed and clamped to 0..100 (LC 4.2). The SW power cap is the
design limit of a GPU-bound laptop, not throttling (LC 4.1, 4.2), so its share
goes to nv_power_cap_pct instead.

The nvidia-smi child runs in a job object that kills it when lag-doctor's last
handle to the job closes, which Windows does when lag-doctor dies however it
dies, so a hard kill never leaves it polling the GPU into a dead pipe.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import math
import os
import re
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes
from typing import IO, Any, Iterator, Sequence

from lagdoctor import clock
from lagdoctor.capture.base import Collector
from lagdoctor.model import G, SeriesBuilder
from lagdoctor.winapi import nvml
from lagdoctor.winapi.nvml import Nvml

PERIOD_S = 1.0
# NVML refreshes its values about every 500 ms (WT 4.4 caveat 1). Polled every 4 ms here (2026-11-04),
# utilisation, power, temperature, clock and the throttle counters all changed together, 496-506 ms apart,
# and each throttle step added the ~500 ms just elapsed. A row read at an arbitrary moment therefore holds
# one, two or three refreshes per second, and continuous throttling would read as 50 % or 150 %. So each row
# is read just after a refresh, found by polling a cheap fingerprint until it changes.
REFRESH_POLL_S = 0.01
# Waits out refresh periods up to ~0.85 s (nvidia-smi's help puts the utilisation sample period at 1/6 to 1 s
# "depending on the product") and still ends before the next 1 s tick.
REFRESH_WAIT_S = 0.9
# A row counts as aligned only if it was read at most this long after the fingerprint was last seen unchanged,
# so at most this long after the refresh (a normal poll gap is ~10 ms). A thread held up longer (load) reads
# late, and the share over its interval would be off [inferred].
ALIGN_SLACK_S = 0.025
NVIDIA_VENDOR_ID = 0x10DE     # DXGI VendorId of the NVIDIA adapter (WT 5.1)
# LC 1.4: treat a power reading at or below the 1.00 W minimum power limit as invalid [inferred].
MIN_VALID_POWER_W = 1.0
COUNTERS = tuple(nvml.THROTTLE_FIELDS)          # every cumulative counter read, from NVML or nvidia-smi
# LC 4.2: heat and the HW power brake hold the clocks down. (hw_slowdown has no counter; nv_reasons shows it.)
THROTTLE_REASONS = ("sw_thermal_slowdown", "hw_thermal_slowdown", "hw_power_brake_slowdown")
POWER_CAP_REASONS = ("sw_power_cap",)           # LC 4.1: the normal limit of a GPU-bound laptop
# power_default_w: the board's default power limit. LC 4.2's power-limit variant is an enforced limit below it
# (battery, a quiet mode); this laptop's enforced limit read both 55 W and ~88 W on one day, the default 80 W.
DEVICE_KEYS = ("name", "clock_max_mhz", "mem_total_mb", "temp_target_c", "power_default_w")
# Values the driver samples; nv_clock_max_mhz is left out because it comes from the device listing.
_SAMPLED = tuple(c for c in G.ALL if c not in (G.T, G.CLOCK_MAX_MHZ))

# nvidia-smi --query-gpu fields this driver accepts (WT 4.1). `name` goes last, so a comma inside a GPU
# name stays in the name instead of shifting the other columns.
SMI_FIELDS = (
    "timestamp", "index", "pci.bus_id", "utilization.gpu", "memory.used", "memory.reserved", "memory.total",
    "temperature.gpu", "power.draw", "enforced.power.limit", "power.default_limit", "clocks.current.graphics",
    "clocks.max.graphics", "pstate", "clocks_event_reasons.active",
    "clocks_event_reasons_counters.sw_thermal_slowdown", "clocks_event_reasons_counters.hw_thermal_slowdown",
    "clocks_event_reasons_counters.hw_power_brake_slowdown", "clocks_event_reasons_counters.sw_power_cap",
    "name",
)
SMI_STOP_TIMEOUT_S = 3.0
SMI_MAX_LINE = 4096
SMI_PICK_AFTER_S = 2.5        # list the GPUs from the stream's first loop, at most this long


# ------------------------------------------------------------ pure parts ---

def adapter_vendor(adapter: Any) -> str | None:
    """'nvidia', another vendor's name, or None when the adapter or its vendor is unknown."""
    if not isinstance(adapter, dict):
        return None
    vendor = adapter.get("vendor")
    vendor = vendor.strip().lower() if isinstance(vendor, str) else None
    vid = adapter.get("vendor_id")
    if isinstance(vid, str):
        try:
            vid = int(vid, 0)
        except ValueError:
            vid = None
    if vendor == "nvidia" or vid == NVIDIA_VENDOR_ID:
        return "nvidia"
    if vendor:
        return vendor
    return "other" if isinstance(vid, int) and not isinstance(vid, bool) else None


_PCI_HEX = re.compile(r"(?:[0-9a-f]{1,8}:)?(?P<bus>[0-9a-f]{1,2}):(?P<dev>[0-9a-f]{1,2})\.(?P<fn>[0-7])", re.I)
_PCI_WORDS = re.compile(r"PCI bus (?P<bus>\d{1,3}), device (?P<dev>\d{1,2}), function (?P<fn>\d)", re.I)
_PCI_TRIPLE = re.compile(r"\[?\s*(?P<bus>\d{1,3})\s*[,/: ]\s*(?P<dev>\d{1,2})\s*[,/: ]\s*(?P<fn>\d)\s*\]?")


def pci_location(value: Any) -> tuple[int, int, int] | None:
    """(bus, device, function) from NVML's "00000000:01:00.0", "01:00.0", Windows' "PCI bus 1, device 0,
    function 0", "1/0/0" or a (bus, device, function) sequence; None if it is none of those.

    The PCI domain is ignored: D3DKMT's adapter address has none (WT 5.3)."""
    if isinstance(value, (list, tuple)):
        if len(value) != 3 or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            return None
        bus, dev, fn = value
    elif isinstance(value, str):
        text = value.strip()
        for pattern, base in ((_PCI_HEX, 16), (_PCI_WORDS, 10), (_PCI_TRIPLE, 10)):
            m = pattern.fullmatch(text)
            if m:
                bus, dev, fn = (int(m[g], base) for g in ("bus", "dev", "fn"))
                break
        else:
            return None
    else:
        return None
    if 0 <= bus <= 255 and 0 <= dev <= 31 and 0 <= fn <= 7:
        return bus, dev, fn
    return None


def pick_device(devices: Sequence[dict], adapter: dict | None) -> tuple[dict | None, str]:
    """The NVIDIA device the game renders on, and how it was chosen (or why none was).

    By PCI location when the adapter gives one, else the first NVIDIA device. When the location matches
    nothing and there is only one NVIDIA device, that device is the game's (the adapter is NVIDIA), and the
    reason says the locations disagreed.
    """
    if not devices:
        return None, "the driver lists no NVIDIA GPU"
    raw = adapter.get("pci") if isinstance(adapter, dict) else None
    want = pci_location(raw)
    if want is not None:
        for d in devices:
            if pci_location(d.get("pci_bus_id")) == want:
                return d, "matched to the game's GPU by PCI location"
        if len(devices) == 1:
            d = devices[0]
            return d, (f"the game's GPU is at PCI {raw!r} but the driver puts its only NVIDIA GPU at "
                       f"{d.get('pci_bus_id')!r}; using that one")
        found = ", ".join(repr(d.get("pci_bus_id")) for d in devices)
        return None, f"none of the {len(devices)} NVIDIA GPUs ({found}) is at the game's PCI location {raw!r}"
    # "first" skips a GPU the driver could not open (no name, no location) when another one answers
    first = next((d for d in devices if d.get("name") or d.get("pci_bus_id")), devices[0])
    if adapter is None:
        return first, "the game's GPU is not known yet; watching the first NVIDIA GPU"
    if raw:
        return first, f"the game's PCI location {raw!r} was not understood; watching the first NVIDIA GPU"
    return first, "the game's GPU has no PCI location; watching the first NVIDIA GPU"


def share_pct(prev: dict | None, cur: dict | None, dt_s: Any, reasons: Sequence[str]) -> float | None:
    """Share (0..100) of dt_s that `reasons` held the clocks down, from two readings of the cumulative
    per-reason nanosecond counters. None when it cannot be known: no earlier reading, no counter in both,
    a counter that went backwards (driver reload), or a bad interval."""
    if not prev or not cur or not isinstance(dt_s, (int, float)) or not 0 < dt_s < math.inf:
        return None
    total = 0
    seen = 0
    for reason in reasons:
        a, b = prev.get(reason), cur.get(reason)
        if a is None or b is None:
            continue
        if b < a:
            return None
        total += b - a
        seen += 1
    if not seen:
        return None
    # Reasons can overlap, so the sum can pass 100 and is clamped: an upper bound of the time held down.
    return min(100.0, 100.0 * total / (dt_s * 1e9))


def throttle_pct(prev: dict | None, cur: dict | None, dt_s: Any) -> float | None:
    """nv_throttle_pct: the share held down by heat or the HW power brake (LC 4.2), never the power cap."""
    return share_pct(prev, cur, dt_s, THROTTLE_REASONS)


def power_cap_pct(prev: dict | None, cur: dict | None, dt_s: Any) -> float | None:
    """nv_power_cap_pct: the share spent at the SW power cap, the design limit (LC 4.1)."""
    return share_pct(prev, cur, dt_s, POWER_CAP_REASONS)


def reasons_text(mask: int | None) -> str | None:
    """The G.REASONS cell: comma-separated active reasons, "" for none, None when not read."""
    return None if mask is None else ",".join(nvml.reason_names(mask))


def plausible_power(watts: float | None) -> float | None:
    return watts if watts is not None and watts > MIN_VALID_POWER_W else None


def make_row(t: float, s: dict, clock_max_mhz: Any, pct: float | None, cap_pct: float | None) -> dict:
    """One G row from a sample dict shaped like Nvml.sample(); pct and cap_pct are the two shares."""
    return {G.T: t, G.UTIL_PCT: s.get("util_pct"), G.MEM_USED_MB: s.get("mem_used_mb"), G.TEMP_C: s.get("temp_c"),
            G.POWER_W: plausible_power(s.get("power_w")), G.POWER_LIMIT_W: s.get("power_limit_w"),
            G.CLOCK_MHZ: s.get("clock_mhz"), G.CLOCK_MAX_MHZ: clock_max_mhz, G.PSTATE: s.get("pstate"),
            G.REASONS: reasons_text(s.get("reasons")), G.THROTTLE_PCT: pct, G.POWER_CAP_PCT: cap_pct}


# --------------------------------------------------------- nvidia-smi text ---

_UNIT_SUFFIX = re.compile(r"\s*\[[^\]]*\]\s*$")
_INT = re.compile(r"\d{1,19}")
_PSTATE = re.compile(r"P(\d{1,2})")
_MASK = re.compile(r"0x[0-9a-fA-F]{1,16}")


def parse_smi_header(line: str) -> list[str] | None:
    """Column names of a CSV header line ("utilization.gpu [%]" -> "utilization.gpu"), or None if the line
    is not a header. nounits still prints the units in the header (WT 4.2)."""
    names = [_UNIT_SUFFIX.sub("", part).strip() for part in line.split(",")]
    return names if names[0] == "timestamp" else None


def _na(v: str) -> bool:
    return not v or v.startswith("[") or v.upper() == "N/A"      # "[N/A]", "[Not Supported]", ...


# The number parsers accept only well-formed numbers, so "[N/A]" and friends already read as None there.
def _int(v: str) -> int | None:
    return int(v) if _INT.fullmatch(v) else None


def _float(v: str) -> float | None:
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _time(v: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(v, "%Y/%m/%d %H:%M:%S.%f")
    except ValueError:
        return None


def parse_smi_row(line: str, columns: Sequence[str]) -> dict | None:
    """One data row (asked for with --format=csv,nounits) as a sample dict like Nvml.sample(), plus
    index, pci_bus_id, name, mem_total_mb, clock_max_mhz, power_default_w and when (nvidia-smi's own
    timestamp). Columns are read by name, so one the header lacks reads None. None if the line is not a
    data row."""
    parts = [p.strip() for p in line.split(",", len(columns) - 1)]
    if len(parts) != len(columns):
        return None
    raw = dict(zip(columns, parts))
    when = _time(raw.get("timestamp", ""))
    index = _int(raw.get("index", ""))
    if when is None or index is None:
        return None
    used, reserved = _float(raw.get("memory.used", "")), _float(raw.get("memory.reserved", ""))
    m = _PSTATE.fullmatch(raw.get("pstate", ""))
    mask = raw.get("clocks_event_reasons.active", "")
    throttle: dict[str, int | None] = {}
    for reason in COUNTERS:
        us = _int(raw.get(f"clocks_event_reasons_counters.{reason}", ""))
        throttle[reason] = None if us is None else us * 1000            # microseconds -> ns, as NVML gives
    bus_id = raw.get("pci.bus_id", "")
    name = raw.get("name", "")
    return {
        "when": when, "index": index,
        "pci_bus_id": bus_id if pci_location(bus_id) else None,
        "name": None if _na(name) else name[:200],
        "util_pct": _int(raw.get("utilization.gpu", "")),
        # NVML's "used" is nvidia-smi's used plus reserved (WT 4.4), so both sources mean the same thing
        "mem_used_mb": None if used is None else used + (reserved or 0.0),
        "mem_total_mb": _float(raw.get("memory.total", "")),
        "temp_c": _int(raw.get("temperature.gpu", "")),
        "power_w": _float(raw.get("power.draw", "")),
        "power_limit_w": _float(raw.get("enforced.power.limit", "")),
        "power_default_w": _float(raw.get("power.default_limit", "")),
        "clock_mhz": _int(raw.get("clocks.current.graphics", "")),
        "clock_max_mhz": _int(raw.get("clocks.max.graphics", "")),
        "pstate": int(m[1]) if m and int(m[1]) <= nvml.PSTATE_MAX else None,
        "reasons": int(mask, 16) if _MASK.fullmatch(mask) else None,
        "throttle_ns": throttle,
    }


def smi_path() -> str:
    return os.path.join(nvml.system_dir(), "nvidia-smi.exe")


def smi_argv() -> list[str]:
    """The streaming nvidia-smi command (an argv list, absolute path). Raises OSError when it is missing."""
    exe = smi_path()
    if not os.path.isfile(exe):
        raise FileNotFoundError(f"{exe} not found")
    return [exe, f"--query-gpu={','.join(SMI_FIELDS)}", "--format=csv,nounits", "-lms", str(round(PERIOD_S * 1000))]


def _lines(stream: IO[bytes], limit: int) -> Iterator[str | None]:
    """Decoded lines from a byte stream. A line longer than `limit` bytes is skipped, never buffered whole,
    and yields None once so the caller can count it."""
    skipping = False
    while True:
        raw = stream.readline(limit)
        if not raw:
            return
        complete = raw.endswith(b"\n")
        if skipping:
            skipping = not complete
            continue
        if not complete and len(raw) >= limit:
            skipping = True
            yield None
            continue
        yield raw.decode("utf-8", "replace").rstrip("\r\n")


class _Tail:
    """Drains a pipe to EOF (an unread pipe blocks the child once it fills) and keeps its last bytes."""

    def __init__(self, keep: int = 2048) -> None:
        self._keep = keep
        self._buf = b""
        self._lock = threading.Lock()

    def drain(self, stream: IO[bytes]) -> None:
        try:
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    return
                with self._lock:
                    self._buf = (self._buf + chunk)[-self._keep:]
        except (OSError, ValueError):
            return

    def text(self) -> str:
        with self._lock:
            buf = self._buf
        if buf.startswith(b"\xff\xfe") or buf.count(b"\x00") > len(buf) // 4:
            s = buf.decode("utf-16-le", "replace").lstrip("﻿")
        else:
            s = buf.decode("utf-8", "replace")
        return " ".join(s.split())[-300:]


class _Stop(Exception):
    """Ends the nvidia-smi read loop with a status."""

    def __init__(self, state: str, why: str) -> None:
        super().__init__(why)
        self.state, self.why = state, why


# ------------------------------------------------- the nvidia-smi child's job ---

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000       # winnt.h
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9         # JOBOBJECTINFOCLASS


class _BasicLimits(ctypes.Structure):             # JOBOBJECT_BASIC_LIMIT_INFORMATION, 64 bytes on x64
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):          # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, 144 bytes on x64
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


nvml._check_sizes([(_BasicLimits, 64), (_ExtendedLimits, 144)])   # a wrong size makes SetInformationJobObject fail

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
_k32.IsProcessInJob.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL


class KillOnCloseJob:
    """A job object whose processes die when its last handle closes: at close(), or when this process
    dies, however it dies, since Windows then closes the handle. Children are added by the handle
    subprocess.Popen already holds, so no process is ever opened."""

    def __init__(self) -> None:
        self._handle = _k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(self._handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(limits), ctypes.sizeof(limits)):
            err = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(err)

    @staticmethod
    def _process(proc: subprocess.Popen) -> int:
        handle = getattr(proc, "_handle", None)        # CreateProcess's handle, full access to our own child
        if not handle:
            raise OSError("the child's process handle is not available")
        return int(handle)

    def add(self, proc: subprocess.Popen) -> None:
        if not _k32.AssignProcessToJobObject(self._handle, self._process(proc)):
            raise ctypes.WinError(ctypes.get_last_error())

    def holds(self, proc: subprocess.Popen) -> bool:
        inside = wintypes.BOOL()
        if not _k32.IsProcessInJob(self._process(proc), self._handle, ctypes.byref(inside)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(inside.value)

    def close(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


# --------------------------------------------------------------- collector ---

class NvidiaSampler(Collector):
    """Collector "gpu": G columns at 1 Hz, plus the device description for inventory["nvidia"].

    `row_aligned[i]` says whether row i was read just after a driver refresh (NVML; False when none came
    within REFRESH_WAIT_S or the read came more than ALIGN_SLACK_S after it), or None for nvidia-smi rows,
    which cannot be aligned."""

    name = "gpu"

    def __init__(self, adapter: dict | None) -> None:
        super().__init__()
        self.adapter = adapter if isinstance(adapter, dict) else None
        self.series = SeriesBuilder(G.ALL)
        self.row_aligned: list[bool | None] = []
        self._lock = threading.Lock()                 # guards series, row_aligned and _device for result()
        self._device: dict | None = None
        self._child: subprocess.Popen | None = None
        self._child_lock = threading.Lock()
        self._job: KillOnCloseJob | None = None
        self.smi_malformed = 0
        self.smi_exit_code: int | None = None
        self.smi_stderr = ""
        self._smi_junk: deque[str] = deque(maxlen=3)

    # ---- Collector ------------------------------------------------------
    def run(self) -> None:
        vendor = adapter_vendor(self.adapter)
        if vendor not in (None, "nvidia"):
            gpu = self.adapter.get("name") or "another GPU"
            self.set_status("off", f"the game renders on {gpu} ({vendor}), not an NVIDIA GPU; NVML was not "
                                   "started, so a sleeping NVIDIA GPU stays asleep")
            return
        nv = Nvml.open()
        if nv is None:
            self._run_smi(Nvml.last_open_error or "no reason given")
            return
        try:
            self._run_nvml(nv)
        finally:
            nv.close()

    def on_stop(self) -> None:
        self._end_child(wait=False)

    def result(self) -> dict:
        with self._lock:
            columns = self.series.build() if len(self.series) else {}
            device = dict(self._device) if self._device else None
        return {"columns": columns, "device": device}

    # ---- NVML -----------------------------------------------------------
    def _run_nvml(self, nv: Nvml) -> None:
        try:
            devices = nv.devices()
        except OSError as exc:
            self.set_status("failed", f"NVML loaded but could not list the GPUs: {exc}", tool="nvml")
            return
        dev, how = pick_device(devices, self.adapter)
        if dev is None:
            self.set_status("unavailable", f"NVML: {how}", tool="nvml")
            return
        self._set_device(dev)
        driver = nv.driver_version()
        detail = (f"NVML{f' (driver {driver})' if driver else ''}: {dev.get('name') or 'NVIDIA GPU'} at PCI "
                  f"{dev.get('pci_bus_id') or '?'}; {how}")
        prev: tuple[float, dict, bool] | None = None
        for _ in self.ticks(PERIOD_S):
            got = self._read_after_refresh(nv, dev["index"])
            if got is None:
                break
            sample, t, aligned = got
            pct = cap = None
            if prev is not None:
                both = aligned and prev[2]
                pct = throttle_pct(prev[1], sample["throttle_ns"], t - prev[0])
                cap = power_cap_pct(prev[1], sample["throttle_ns"], t - prev[0])
                # An unaligned read cannot tell which refreshes its counter steps belong to; a zero is still zero.
                if pct and not both:
                    pct = None
                if cap and not both:
                    cap = None
            if self._add(make_row(t, sample, dev.get("clock_max_mhz"), pct, cap), "nvml", aligned) == 1:
                self.set_status("ok", detail, tool="nvml")
            prev = (t, sample["throttle_ns"], aligned)
        self._finish(detail, "nvml")

    def _read_after_refresh(self, nv: Nvml, index: int) -> tuple[dict, float, bool] | None:
        """(sample, t, aligned): a full sample read just after the driver's next refresh. aligned is False
        after REFRESH_WAIT_S without one, or when the refresh may lie more than ALIGN_SLACK_S before the
        read (the poll that saw it came late, or the sample did). None when stopping."""
        mark = nv.refresh_mark(index)
        unchanged_at = clock.now()               # the last moment the fingerprint was seen unchanged
        deadline = unchanged_at + REFRESH_WAIT_S
        changed_at = None
        while clock.now() < deadline:
            if self.stopping:
                return None
            time.sleep(REFRESH_POLL_S)
            if nv.refresh_mark(index) != mark:
                changed_at = clock.now()
                break
            unchanged_at = clock.now()
        if self.stopping:
            return None
        sample = nv.sample(index)
        read_at = clock.now()
        aligned = changed_at is not None and read_at - unchanged_at <= ALIGN_SLACK_S
        return sample, read_at - self.origin, aligned

    # ---- nvidia-smi -----------------------------------------------------
    def _run_smi(self, nvml_reason: str) -> None:
        why = f"NVML could not load ({nvml_reason})"
        try:
            argv = smi_argv()
        except OSError as exc:
            self.set_status("unavailable", f"{why}, and nvidia-smi cannot run either: {exc}", tool="nvidia-smi")
            return
        with self._child_lock:
            if self.stopping:
                self.set_status("partial", f"{why}; stopped before nvidia-smi started", tool="nvidia-smi")
                return
            try:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError as exc:
                self.set_status("unavailable", f"{why}, and nvidia-smi did not start: {exc}", tool="nvidia-smi")
                return
            self._child = child
            # Tied to our lifetime at once. (A hard kill in the instant before this line still leaves it running.)
            job_note = self._join_job(child)
        tail = _Tail()
        drain = threading.Thread(target=tail.drain, args=(child.stderr,), name="lagdoctor-gpu-smi-stderr",
                                 daemon=True)
        drain.start()
        detail = f"nvidia-smi streaming every {PERIOD_S:g} s ({why})" + (f"; {job_note}" if job_note else "")
        stop: _Stop | None = None
        try:
            detail = self._read_smi(child.stdout, detail)
        except _Stop as exc:
            stop = exc
        finally:
            self.smi_exit_code = self._end_child(wait=True)
            if self._job is not None:
                self._job.close()             # after the reap, so its exit code stays TerminateProcess's own
            drain.join(SMI_STOP_TIMEOUT_S)
            for stream in (child.stdout, child.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            self.smi_stderr = tail.text()
        if stop is not None:
            self.set_status(stop.state, f"{detail}; {stop.why}", tool="nvidia-smi")
        elif not self.stopping:
            told = self.smi_stderr or " | ".join(self._smi_junk) or "nothing on stderr"
            n = len(self.series)
            if n == 0:
                self.set_status("failed", f"{detail}; nvidia-smi exited (code {self.smi_exit_code}) before "
                                          f"giving a sample: {told}", tool="nvidia-smi")
            else:
                self.set_status("partial", f"{detail}; nvidia-smi stopped by itself after {n} samples "
                                           f"(exit code {self.smi_exit_code}): {told}", tool="nvidia-smi")
        else:
            self._finish(detail, "nvidia-smi")

    def _read_smi(self, stream: IO[bytes], detail: str) -> str:
        columns: list[str] | None = None
        pending: list[tuple[float, dict]] = []    # the first loop's rows, one per GPU, until we pick one
        chosen: dict | None = None
        prev: tuple[_dt.datetime, dict] | None = None
        for line in _lines(stream, SMI_MAX_LINE):
            t = self.now()
            if line is None:
                self.smi_malformed += 1
                continue
            if not line.strip():
                continue
            header = parse_smi_header(line)
            if header is not None:
                if header != list(SMI_FIELDS):
                    raise _Stop("failed", f"nvidia-smi printed other columns than asked for: {', '.join(header)}")
                columns = header
                continue
            row = parse_smi_row(line, columns) if columns else None
            if row is None:
                self.smi_malformed += 1
                self._smi_junk.append(line[:200])
                continue
            if chosen is None:
                first_loop = (not pending or t - pending[0][0] < SMI_PICK_AFTER_S)
                if first_loop and len(pending) < nvml.MAX_DEVICES \
                        and all(r["index"] != row["index"] for _, r in pending):
                    pending.append((t, row))
                    continue
                chosen, detail, prev = self._smi_pick(pending, detail)
                pending = []
            if row["index"] == chosen["index"]:
                prev = self._smi_add(t, row, prev, detail)
        if chosen is None and pending:            # stopped inside the first loop
            chosen, detail, prev = self._smi_pick(pending, detail)
        return detail

    def _smi_pick(self, pending: list[tuple[float, dict]], detail: str):
        chosen, how = pick_device([r for _, r in pending], self.adapter)
        if chosen is None:
            raise _Stop("unavailable", how)
        self._set_device(chosen)
        detail = f"{detail}: {chosen.get('name') or 'NVIDIA GPU'} at PCI {chosen.get('pci_bus_id') or '?'}; {how}"
        prev = None
        for t, row in pending:
            if row["index"] == chosen["index"]:
                prev = self._smi_add(t, row, prev, detail)
        return chosen, detail, prev

    def _smi_add(self, t: float, row: dict, prev: tuple | None, detail: str) -> tuple:
        # nvidia-smi's rows cannot be aligned to the driver's refreshes, so one row's share can read 50 %
        # or 150 % (clamped) when its second holds one or three of them; analysis averages over windows.
        pct = cap = None
        if prev is not None:
            dt_s = (row["when"] - prev[0]).total_seconds()
            pct = throttle_pct(prev[1], row["throttle_ns"], dt_s)
            cap = power_cap_pct(prev[1], row["throttle_ns"], dt_s)
        if self._add(make_row(t, row, row.get("clock_max_mhz"), pct, cap), "nvidia-smi", None) == 1:
            self.set_status("ok", detail, tool="nvidia-smi")
        return row["when"], row["throttle_ns"]

    def _join_job(self, child: subprocess.Popen) -> str | None:
        """Put the nvidia-smi child in a kill-on-close job; on failure, the note that says it is not."""
        try:
            self._job = KillOnCloseJob()
            self._job.add(child)
        except OSError as exc:
            if self._job is not None:
                self._job.close()
                self._job = None
            return f"nvidia-smi is not tied to lag-doctor's lifetime, so a hard kill could leave it running ({exc})"
        return None

    def _end_child(self, wait: bool) -> int | None:
        """Stop the nvidia-smi child (TerminateProcess: it holds no ETW session or anything else that outlives
        it, and leaves no survivor, WT 4.2). With wait, reap it and return its exit code."""
        with self._child_lock:
            child = self._child
        if child is None:
            return None
        if child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass
        if not wait:
            return None
        try:
            return child.wait(SMI_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            try:
                return child.wait(SMI_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return None

    # ---- shared ---------------------------------------------------------
    def _set_device(self, dev: dict) -> None:
        with self._lock:
            self._device = {k: dev.get(k) for k in DEVICE_KEYS}

    def _add(self, row: dict, source: str, aligned: bool | None) -> int:
        with self._lock:
            self.series.add(**row)
            self.row_aligned.append(aligned)
            n = len(self.series)
        self.publish(source=source, **row)
        return n

    def _finish(self, detail: str, tool: str) -> None:
        with self._lock:
            cols = self.series.build()
        n = len(cols[G.T])
        if n == 0:
            if self.stopping:
                self.set_status("partial", f"{detail}; stopped before the first sample", tool=tool)
            else:
                self.set_status("failed", f"{detail}; no sample was recorded", tool=tool)
            return
        empty = sum(1 for i in range(n) if all(cols[c][i] is None for c in _SAMPLED))
        never = [c for c in _SAMPLED if all(v is None for v in cols[c])]
        if empty == n:
            self.set_status("failed", f"{detail}; the driver answered but gave no values in {n} samples", tool=tool)
        elif empty or never:
            parts = ([f"{empty} of {n} samples had no values"] if empty else []) \
                + ([f"never measured: {', '.join(never)}"] if never else [])
            self.set_status("partial", f"{detail}; {'; '.join(parts)}", tool=tool)
        else:
            self.set_status("ok", detail, tool=tool)
