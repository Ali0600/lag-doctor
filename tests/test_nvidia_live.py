"""Live, read-only checks of NVIDIA telemetry against this machine, compared with nvidia-smi.

Values that depend on whatever keeps the GPU busy (here, a long ML training job) are printed, not asserted.
Nothing here changes GPU state: NVML and nvidia-smi are only queried.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time

import psutil
import pytest

from lagdoctor import clock
from lagdoctor.capture import gpumon
from lagdoctor.capture.gpumon import NvidiaSampler, throttle_pct
from lagdoctor.model import G
from lagdoctor.winapi.nvml import Nvml


@pytest.fixture(scope="module")
def nv():
    session = Nvml.open()
    if session is None:
        pytest.skip(f"NVML is not available on this machine: {Nvml.last_open_error}")
    yield session
    session.close()


@pytest.fixture(scope="module")
def smi() -> str:
    exe = gpumon.smi_path()
    if not os.path.isfile(exe):
        pytest.skip(f"{exe} not found")
    return exe


def smi_query(exe: str, fields: list[str]) -> list[dict[str, str]]:
    """One-shot `nvidia-smi --query-gpu`, one dict per GPU (only ever used outside the sampling loop)."""
    out = subprocess.run([exe, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return [dict(zip(fields, (p.strip() for p in line.split(",", len(fields) - 1))))
            for line in out.splitlines() if line.strip()]


def wmi_processes(where: str) -> list[tuple[int, str, int]]:
    """(pid, name, parent pid) of the processes matching a WMI filter (no handle from us)."""
    cmd = (f"Get-CimInstance Win32_Process -Filter \"{where}\" | "
           "ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.Name, $_.ParentProcessId }")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                         text=True, timeout=60, check=True).stdout
    rows = [line.strip().split("|") for line in out.splitlines() if line.count("|") == 2]
    return [(int(p), n, int(pp)) for p, n, pp in rows]


def test_nvml_lists_this_gpu_as_nvidia_smi_does(nv, smi):
    devices = nv.devices()
    rows = smi_query(smi, ["index", "name", "pci.bus_id", "memory.total", "clocks.max.graphics"])
    assert len(devices) == len(rows) >= 1
    for d, r in zip(devices, rows):
        assert d["index"] == int(r["index"])
        assert d["name"] == r["name"]
        assert d["pci_bus_id"] == r["pci.bus_id"]
        assert d["mem_total_mb"] == float(r["memory.total"])
        assert d["clock_max_mhz"] == int(r["clocks.max.graphics"])
    q = subprocess.run([smi, "-q", "-d", "TEMPERATURE", "-i", "0"], capture_output=True, text=True, timeout=30,
                       check=True).stdout
    m = re.search(r"GPU Target Temperature\s*:\s*(\d+) C", q)
    assert devices[0]["temp_target_c"] == (int(m[1]) if m else None), q


def test_nvml_sample_matches_a_one_shot_nvidia_smi_read(nv, smi):
    fields = ["temperature.gpu", "clocks.current.graphics", "memory.used", "memory.reserved", "power.draw",
              "enforced.power.limit", "pstate", "clocks_event_reasons.active",
              "clocks_event_reasons_counters.sw_thermal_slowdown", "clocks_event_reasons_counters.hw_thermal_slowdown",
              "clocks_event_reasons_counters.hw_power_brake_slowdown",
              "clocks_event_reasons_counters.sw_power_cap"]
    before = nv.sample(0)
    r = smi_query(smi, fields)[0]
    after = nv.sample(0)

    def within(key: str, smi_value: float, tol: float) -> None:
        lo, hi = sorted((before[key], after[key]))
        assert lo - tol <= smi_value <= hi + tol, (key, before[key], smi_value, after[key])

    # The bracket is ~0.1 s, so the driver refreshed at most once inside it; tolerances cover a slow call.
    within("temp_c", int(r["temperature.gpu"]), 2)
    within("clock_mhz", int(r["clocks.current.graphics"]), 0.1 * nv.devices()[0]["clock_max_mhz"])
    within("mem_used_mb", float(r["memory.used"]) + float(r["memory.reserved"]), 64)  # NVML used = used + reserved
    power = float(r["power.draw"])
    within("power_w", power, max(5.0, 0.15 * power))
    within("power_limit_w", float(r["enforced.power.limit"]), 1.0)
    assert int(r["pstate"].lstrip("P")) in (before["pstate"], after["pstate"])
    assert int(r["clocks_event_reasons.active"], 16) in (before["reasons"], after["reasons"])
    for reason in gpumon.THROTTLE_REASONS:
        # the same cumulative counter: nvidia-smi prints whole microseconds, NVML gives nanoseconds
        smi_ns = int(r[f"clocks_event_reasons_counters.{reason}"]) * 1000
        assert before["throttle_ns"][reason] - 999 <= smi_ns <= after["throttle_ns"][reason], reason


def _wait_refresh(nv: Nvml) -> None:
    mark = nv.refresh_mark(0)
    deadline = time.monotonic() + 1.0
    while nv.refresh_mark(0) == mark and time.monotonic() < deadline:
        time.sleep(0.005)


def test_print_the_throttle_share_per_reason(nv):
    _wait_refresh(nv)
    a, ta = nv.sample(0), clock.now()
    time.sleep(1.5)
    _wait_refresh(nv)
    b, tb = nv.sample(0), clock.now()
    per = {r: throttle_pct({r: a["throttle_ns"][r]}, {r: b["throttle_ns"][r]}, tb - ta) for r in a["throttle_ns"]}
    total = throttle_pct(a["throttle_ns"], b["throttle_ns"], tb - ta)
    print(f"\nthrottle share over {tb - ta:.3f} s: {per}; nv_throttle_pct = {total} (reasons now {b['reasons']:#x})")
    assert total is None or 0.0 <= total <= 100.0


def test_sampler_reads_each_row_just_after_a_driver_refresh(nv):
    # An independent recorder polls the driver's refresh fingerprint every 5 ms on its own NVML session.
    refreshes: list[float] = []
    done = threading.Event()

    def record() -> None:
        last = nv.refresh_mark(0)
        while not done.is_set():
            mark = nv.refresh_mark(0)
            if mark != last:
                refreshes.append(clock.now())
                last = mark
            time.sleep(0.005)

    recorder = threading.Thread(target=record, daemon=True)
    recorder.start()
    origin = clock.now()
    s = NvidiaSampler(None)
    s.start(origin)
    time.sleep(4.6)
    s.stop()
    done.set()
    recorder.join(5)
    st = s.status()
    assert st.state == "ok" and st.tool == "nvml" and "NVML" in st.detail, st
    cols = s.result()["columns"]
    t = cols[G.T]
    assert len(t) >= 4 and len(refreshes) >= 6, (t, refreshes)
    for i, row_t in enumerate(t):
        print(f"row {i}: t={row_t:.3f} util={cols[G.UTIL_PCT][i]} temp={cols[G.TEMP_C][i]} "
              f"clock={cols[G.CLOCK_MHZ][i]}/{cols[G.CLOCK_MAX_MHZ][i]} power={cols[G.POWER_W][i]} "
              f"reasons={cols[G.REASONS][i]!r} throttle={cols[G.THROTTLE_PCT][i]}")
        # The recorder sees a refresh up to a poll later than the sampler may, so the window is two-sided;
        # refreshes are ~500 ms apart, so a row read at a random moment lands this close only ~12 % of the time.
        nearest = min(refreshes, key=lambda r: abs(r - (origin + row_t)))
        assert abs(nearest - (origin + row_t)) <= 0.03, ("row not read just after a refresh", i, row_t)
    assert all(v is not None and 0.0 <= v <= 100.0 for v in cols[G.THROTTLE_PCT][1:]), cols[G.THROTTLE_PCT]
    dev = s.result()["device"]
    want = {k: nv.devices()[0][k] for k in gpumon.DEVICE_KEYS}
    assert dev == want


def test_fallback_streams_real_nvidia_smi_and_leaves_no_child(nv, smi, monkeypatch):
    monkeypatch.setattr(gpumon.Nvml, "open", classmethod(lambda cls: None))
    monkeypatch.setattr(gpumon.Nvml, "last_open_error", "test: NVML switched off")
    s = NvidiaSampler(None)
    s.start(clock.now())
    try:
        deadline = time.monotonic() + 15
        while len(s.series) < 3 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert len(s.series) >= 3, s.status()
        child = s._child
    finally:
        s.stop()
        if s._child is not None and s._child.poll() is None:
            s._child.kill()
            s._child.wait(5)
    assert child.returncode == 1, child.returncode            # TerminateProcess, as WT 4.2 saw
    if child.pid in psutil.pids():
        # Windows hands a freed PID out again within milliseconds (seen once here, with other test runs busy
        # starting processes), so a listed PID must be checked for being some other process.
        print(f"\npid {child.pid} is listed again: {wmi_processes(f'ProcessId={child.pid}')}")
    assert wmi_processes(f"ParentProcessId={os.getpid()} and Name='nvidia-smi.exe'") == []
    st = s.status()
    assert st.state == "ok" and st.tool == "nvidia-smi" and "test: NVML switched off" in st.detail, st
    cols = s.result()["columns"]
    for i in range(len(cols[G.T])):
        print(f"smi row {i}: " + ", ".join(f"{c}={cols[c][i]}" for c in G.ALL))
    assert all(0 <= v <= 100 for v in cols[G.UTIL_PCT]) and all(10 <= v <= 110 for v in cols[G.TEMP_C])
    assert all(v is None or 0.0 <= v <= 100.0 for v in cols[G.THROTTLE_PCT])
    d = nv.devices()[0]
    assert s.result()["device"] == {"name": d["name"], "clock_max_mhz": d["clock_max_mhz"],
                                    "mem_total_mb": d["mem_total_mb"], "temp_target_c": None}
