"""NVIDIA telemetry: the NVML wrapper over a fake library, the gpu collector's pure parts, its NVML loop over a
fake NVML with a simulated refresh grid, and its nvidia-smi fallback over a stand-in child process.

Live checks against this machine's GPU are in test_nvidia_live.py.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import io
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import gpumon
from lagdoctor.capture.gpumon import (NvidiaSampler, adapter_vendor, parse_smi_header, parse_smi_row, pci_location,
                                      pick_device, plausible_power, reasons_text, throttle_pct)
from lagdoctor.model import G
from lagdoctor.winapi import nvml
from lagdoctor.winapi.nvml import Nvml, reason_names

FIXTURES = Path(__file__).parent / "fixtures" / "nvidia"
SMI_CAPTURE = FIXTURES / "smi_stream_591.86.csv"       # real nvidia-smi 591.86 output from this laptop
FAKE_SMI = FIXTURES / "fake_smi.py"
GIB = 1 << 30


# ============================================================ NVML wrapper ===

class FakeLib:
    """The bound NVML functions, as plain Python: records each call and fills the out-parameters."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rc: dict[str, int] = {}
        self.count = 1
        self.handle_rc: dict[int, int] = {}
        self.util, self.mem_used, self.mem_total = 50, 3 * GIB, 8 * GIB
        self.temp, self.power_mw, self.limit_mw, self.clock, self.max_clock = 70, 45_500, 88_000, 1500, 2100
        self.pstate, self.reasons = 3, 0x24
        self.fields = {269: 7_000, 270: 0, 271: 11, 74: 9_000}
        self.field_rc: dict[int, int] = {}
        self.value_type = 3
        self.fill_fields = True          # False: report success but leave the entries as they were
        self.thresholds = {7: 75}

    def fns(self) -> dict:
        return {name: getattr(self, name) for name in dir(self) if name.startswith("nvml")}

    def _ret(self, name: str) -> int:
        self.calls.append(name)
        return self.rc.get(name, 0)

    def nvmlInit_v2(self): return self._ret("nvmlInit_v2")
    def nvmlShutdown(self): return self._ret("nvmlShutdown")
    def nvmlErrorString(self, rc): return b"Fake Error"

    def nvmlSystemGetDriverVersion(self, buf, n):
        rc = self._ret("nvmlSystemGetDriverVersion")
        if rc == 0:
            buf.value = b"591.86"
        return rc

    def nvmlDeviceGetCount_v2(self, ref):
        rc = self._ret("nvmlDeviceGetCount_v2")
        if rc == 0:
            ref._obj.value = self.count
        return rc

    def nvmlDeviceGetHandleByIndex_v2(self, index, ref):
        self.calls.append("nvmlDeviceGetHandleByIndex_v2")
        rc = self.handle_rc.get(index, 0 if index < self.count else 2)
        if rc == 0:
            ref._obj.value = 0x1000 + index
        return rc

    def nvmlDeviceGetName(self, h, buf, n):
        rc = self._ret("nvmlDeviceGetName")
        if rc == 0:
            buf.value = b"Fake GPU %d" % (h.value - 0x1000)
        return rc

    def nvmlDeviceGetPciInfo_v3(self, h, ref):
        rc = self._ret("nvmlDeviceGetPciInfo_v3")
        if rc == 0:
            ref._obj.busId = b"00000000:%02X:00.0" % (h.value - 0x1000 + 1)
        return rc

    def _out(self, name, ref, value):
        rc = self._ret(name)
        if rc == 0:
            ref._obj.value = value
        return rc

    def nvmlDeviceGetMemoryInfo(self, h, ref):
        rc = self._ret("nvmlDeviceGetMemoryInfo")
        if rc == 0:
            ref._obj.total, ref._obj.used, ref._obj.free = self.mem_total, self.mem_used, self.mem_total - self.mem_used
        return rc

    def nvmlDeviceGetUtilizationRates(self, h, ref):
        rc = self._ret("nvmlDeviceGetUtilizationRates")
        if rc == 0:
            ref._obj.gpu, ref._obj.memory = self.util, 9
        return rc

    def nvmlDeviceGetMaxClockInfo(self, h, kind, ref): return self._out("nvmlDeviceGetMaxClockInfo", ref, self.max_clock)
    def nvmlDeviceGetTemperature(self, h, sensor, ref): return self._out("nvmlDeviceGetTemperature", ref, self.temp)
    def nvmlDeviceGetPowerUsage(self, h, ref): return self._out("nvmlDeviceGetPowerUsage", ref, self.power_mw)
    def nvmlDeviceGetEnforcedPowerLimit(self, h, ref): return self._out("nvmlDeviceGetEnforcedPowerLimit", ref, self.limit_mw)
    def nvmlDeviceGetClockInfo(self, h, kind, ref): return self._out("nvmlDeviceGetClockInfo", ref, self.clock)
    def nvmlDeviceGetPerformanceState(self, h, ref): return self._out("nvmlDeviceGetPerformanceState", ref, self.pstate)
    def nvmlDeviceGetCurrentClocksEventReasons(self, h, ref):
        return self._out("nvmlDeviceGetCurrentClocksEventReasons", ref, self.reasons)
    def nvmlDeviceGetDisplayActive(self, h, ref): return self._out("nvmlDeviceGetDisplayActive", ref, 0)

    def nvmlDeviceGetTemperatureThreshold(self, h, kind, ref):
        self.calls.append("nvmlDeviceGetTemperatureThreshold")
        if kind not in self.thresholds:
            return 3
        ref._obj.value = self.thresholds[kind]
        return 0

    def nvmlDeviceGetFieldValues(self, h, count, entries):
        rc = self._ret("nvmlDeviceGetFieldValues")
        if self.fill_fields:
            for i in range(count):
                e = entries[i]
                e.nvmlReturn = self.field_rc.get(e.fieldId, 0)
                e.valueType = self.value_type
                e.value.ullVal = self.fields.get(e.fieldId, 0)
        return rc


def fake_nvml(lib: FakeLib | None = None) -> tuple[Nvml, FakeLib]:
    lib = lib or FakeLib()
    return Nvml(lib.fns()), lib


SAMPLE_CALLS = [  # (function, key in the sample) for every value sample() reads
    ("nvmlDeviceGetUtilizationRates", "util_pct"), ("nvmlDeviceGetMemoryInfo", "mem_used_mb"),
    ("nvmlDeviceGetTemperature", "temp_c"), ("nvmlDeviceGetPowerUsage", "power_w"),
    ("nvmlDeviceGetEnforcedPowerLimit", "power_limit_w"), ("nvmlDeviceGetClockInfo", "clock_mhz"),
    ("nvmlDeviceGetPerformanceState", "pstate"), ("nvmlDeviceGetCurrentClocksEventReasons", "reasons"),
]


def test_sample_converts_units():
    nv, _ = fake_nvml()
    s = nv.sample(0)
    assert s == {"util_pct": 50, "mem_used_mb": 3072.0, "temp_c": 70, "power_w": 45.5, "power_limit_w": 88.0,
                 "clock_mhz": 1500, "pstate": 3, "reasons": 0x24,
                 "throttle_ns": {"sw_thermal_slowdown": 7_000, "hw_thermal_slowdown": 0,
                                 "hw_power_brake_slowdown": 11, "sw_power_cap": 9_000}}


@pytest.mark.parametrize("function, key", SAMPLE_CALLS)
def test_a_failed_call_reads_as_none_even_after_a_good_one(function, key):
    nv, lib = fake_nvml()
    assert nv.sample(0)[key] is not None          # fills the reused buffers first
    lib.rc[function] = 3                          # NVML_ERROR_NOT_SUPPORTED
    s = nv.sample(0)
    assert s[key] is None
    assert all(v is not None for k, v in s.items() if k not in (key, "throttle_ns"))


@pytest.mark.parametrize("pstate, expected", [(0, 0), (15, 15), (16, None), (32, None), (-1, None)])
def test_unknown_pstate_reads_none(pstate, expected):
    nv, lib = fake_nvml()
    lib.pstate = pstate
    assert nv.sample(0)["pstate"] == expected


def test_a_failed_field_entry_reads_none_and_the_others_stay():
    nv, lib = fake_nvml()
    lib.field_rc[270] = 3
    assert nv.sample(0)["throttle_ns"] == {"sw_thermal_slowdown": 7_000, "hw_thermal_slowdown": None,
                                           "hw_power_brake_slowdown": 11, "sw_power_cap": 9_000}


def test_a_failed_field_call_is_not_trusted_even_if_its_entries_look_filled():
    nv, lib = fake_nvml()
    lib.rc["nvmlDeviceGetFieldValues"] = 3
    assert set(nv.sample(0)["throttle_ns"].values()) == {None}


def test_entries_the_driver_did_not_fill_are_never_reused_from_the_last_call():
    nv, lib = fake_nvml()
    assert nv.sample(0)["throttle_ns"]["sw_thermal_slowdown"] == 7_000
    lib.fill_fields = False                       # success, but nothing written this time
    assert set(nv.sample(0)["throttle_ns"].values()) == {None}


@pytest.mark.parametrize("value_type, expected", [(3, 7_000), (1, 7_000), (0, None), (99, None)])
def test_field_value_types(value_type, expected):
    nv, lib = fake_nvml()
    lib.value_type = value_type
    assert nv.sample(0)["throttle_ns"]["sw_thermal_slowdown"] == expected


def test_display_active_stays_out_of_the_sampling_calls():
    nv, lib = fake_nvml()
    nv.devices()
    for _ in range(3):
        nv.sample(0)
        nv.refresh_mark(0)
    assert "nvmlDeviceGetPowerUsage" in lib.calls              # calls are being recorded by name
    assert "nvmlDeviceGetDisplayActive" not in lib.calls


def test_refresh_mark_reads_the_cheap_values_only():
    nv, lib = fake_nvml()
    lib.calls.clear()
    assert nv.refresh_mark(0) == (50, 45_500, 70, 1500, 7_000, 9_000)
    asked = [c for c in lib.calls if c != "nvmlDeviceGetHandleByIndex_v2"]
    assert "nvmlDeviceGetMemoryInfo" not in asked
    lib.calls.clear()
    nv.refresh_mark(0)
    assert lib.calls.count("nvmlDeviceGetFieldValues") == 1


def test_devices_describe_each_gpu_and_a_gpu_without_a_handle_is_listed_empty():
    lib = FakeLib()
    lib.count = 2
    lib.handle_rc[1] = 15                         # NVML_ERROR_GPU_IS_LOST
    nv, _ = fake_nvml(lib)
    assert nv.devices() == [
        {"index": 0, "name": "Fake GPU 0", "pci_bus_id": "00000000:01:00.0", "mem_total_mb": 8192.0,
         "clock_max_mhz": 2100, "temp_target_c": 75},
        {"index": 1, "name": None, "pci_bus_id": None, "mem_total_mb": None, "clock_max_mhz": None,
         "temp_target_c": None},
    ]
    assert all(v is None for k, v in nv.sample(1).items() if k != "throttle_ns")


@pytest.mark.parametrize("thresholds, expected", [({7: 75}, 75), ({5: 83}, 83), ({7: 75, 5: 83}, 75), ({}, None)])
def test_target_temperature_prefers_gps_then_acoustic(thresholds, expected):
    lib = FakeLib()
    lib.thresholds = thresholds
    nv, _ = fake_nvml(lib)
    assert nv.devices()[0]["temp_target_c"] == expected


def test_device_count_failure_raises_an_oserror_with_the_nvml_code():
    nv, lib = fake_nvml()
    lib.rc["nvmlDeviceGetCount_v2"] = 9
    with pytest.raises(OSError) as info:
        nv.devices()
    assert info.value.code == 9 and "Fake Error" in str(info.value) and info.value.winerror is None


@pytest.mark.parametrize("bad", [-1, 64, True, "0", 1.0])
def test_a_bad_gpu_index_is_refused(bad):
    nv, _ = fake_nvml()
    with pytest.raises(ValueError):
        nv.sample(bad)


def test_close_is_idempotent_and_a_closed_session_refuses_calls():
    nv, lib = fake_nvml()
    nv.close()
    nv.close()
    assert lib.calls.count("nvmlShutdown") == 1
    with pytest.raises(ValueError):
        nv.sample(0)


def test_dll_path_is_absolute_in_the_system_directory():
    path = nvml.dll_path()
    assert os.path.isabs(path)
    expected = os.path.join(os.environ["SystemRoot"], "System32", "nvml.dll")    # independent readout
    assert os.path.normcase(path) == os.path.normcase(expected)


def test_open_returns_none_with_the_reason_when_the_dll_is_missing(monkeypatch, tmp_path):
    missing = str(tmp_path / "nvml.dll")
    monkeypatch.setattr(nvml, "dll_path", lambda: missing)
    assert Nvml.open() is None
    assert missing in Nvml.last_open_error and "no NVIDIA driver" in Nvml.last_open_error


def test_open_returns_none_when_init_fails(monkeypatch):
    lib = FakeLib()
    lib.rc["nvmlInit_v2"] = 9                     # NVML_ERROR_DRIVER_NOT_LOADED
    monkeypatch.setattr(nvml, "dll_path", lambda: __file__)     # exists; the load itself is faked below
    monkeypatch.setattr(nvml, "_load_library", lambda path: lib)
    monkeypatch.setattr(nvml, "_bind", lambda loaded: loaded.fns())
    assert Nvml.open() is None
    assert "nvmlInit_v2" in Nvml.last_open_error and "Fake Error" in Nvml.last_open_error


def test_bind_refuses_a_library_without_the_core_exports():
    with pytest.raises(OSError, match="nvmlInit_v2"):
        nvml._bind(object())


@pytest.mark.parametrize("mask, names", [
    (0, []),
    (0x20, ["sw_thermal_slowdown"]),
    (0x24, ["sw_power_cap", "sw_thermal_slowdown"]),
    (0x600, ["board_limit", "reliability"]),
    (0x824, ["sw_power_cap", "sw_thermal_slowdown", "0x800"]),
    (1 << 40, ["0x10000000000"]),
])
def test_reason_names(mask, names):
    assert reason_names(mask) == names


def test_struct_sizes_match_the_research():
    # WT 4.4 / 4.5 and its verification log
    assert (ctypes.sizeof(nvml.PciInfo), ctypes.sizeof(nvml.Memory), ctypes.sizeof(nvml.Utilization),
            ctypes.sizeof(nvml.FieldValue)) == (68, 24, 8, 40)


def test_a_drifted_struct_size_stops_the_import():
    nvml._check_sizes([(nvml.PciInfo, 68)])
    with pytest.raises(ImportError, match="PciInfo is 68 bytes"):
        nvml._check_sizes([(nvml.FieldValue, 40), (nvml.PciInfo, 67)])


# ====================================================== collector, pure ===

@pytest.mark.parametrize("adapter, vendor", [
    (None, None), ({}, None), ("0x10de", None),
    ({"vendor": "nvidia"}, "nvidia"), ({"vendor": "NVIDIA "}, "nvidia"), ({"vendor_id": 0x10DE}, "nvidia"),
    ({"vendor_id": "0x10de"}, "nvidia"), ({"vendor": "other", "vendor_id": 0x10DE}, "nvidia"),
    ({"vendor": "intel", "vendor_id": 0x8086}, "intel"), ({"vendor_id": 0x8086}, "other"),
    ({"vendor": "microsoft"}, "microsoft"),
])
def test_adapter_vendor(adapter, vendor):
    assert adapter_vendor(adapter) == vendor


@pytest.mark.parametrize("value, expected", [
    ("00000000:01:00.0", (1, 0, 0)), ("0000:01:00.0", (1, 0, 0)), ("01:00.0", (1, 0, 0)),
    ("0A:1F.7", (10, 31, 7)), ("PCI bus 1, device 0, function 0", (1, 0, 0)), ("1/0/0", (1, 0, 0)),
    ("[1, 0, 0]", (1, 0, 0)), ("1:0:0", (1, 0, 0)), ((1, 0, 0), (1, 0, 0)), ([2, 0, 0], (2, 0, 0)),
    ("", None), (None, None), ("01:00", None), ("01:20.0", None), ("01:00.8", None), ("garbage", None),
    ((1, 0), None), ((True, 0, 0), None), ("256/0/0", None), ("PCI bus 1, device 32, function 0", None),
])
def test_pci_location(value, expected):
    assert pci_location(value) == expected


DEVS = [{"index": 0, "name": "A", "pci_bus_id": "00000000:02:00.0"},
        {"index": 1, "name": "B", "pci_bus_id": "00000000:01:00.0"}]


def test_pick_device_by_pci_when_the_wanted_gpu_is_not_first():
    dev, how = pick_device(DEVS, {"vendor": "nvidia", "pci": "PCI bus 1, device 0, function 0"})
    assert dev["index"] == 1 and "PCI location" in how


def test_pick_device_refuses_to_guess_between_several_gpus():
    dev, how = pick_device(DEVS, {"vendor": "nvidia", "pci": "03:00.0"})
    assert dev is None and "03:00.0" in how


def test_pick_device_uses_the_only_gpu_and_says_the_locations_disagreed():
    dev, how = pick_device(DEVS[:1], {"vendor": "nvidia", "pci": "03:00.0"})
    assert dev["index"] == 0 and "03:00.0" in how and "00000000:02:00.0" in how


@pytest.mark.parametrize("adapter, words", [(None, "not known"), ({"vendor": "nvidia"}, "no PCI location"),
                                            ({"vendor": "nvidia", "pci": "gpu#1"}, "not understood")])
def test_pick_device_falls_back_to_the_first_gpu(adapter, words):
    dev, how = pick_device(DEVS, adapter)
    assert dev["index"] == 0 and words in how


def test_pick_device_with_no_gpus():
    assert pick_device([], None)[0] is None


def test_pick_device_skips_a_gpu_the_driver_could_not_open():
    lost = {"index": 0, "name": None, "pci_bus_id": None}
    assert pick_device([lost, DEVS[1]], None)[0]["index"] == 1
    assert pick_device([lost], None)[0] is lost          # nothing better: the only GPU is still the one


def _ns(**kw):
    return {"sw_thermal_slowdown": 0, "hw_thermal_slowdown": 0, "hw_power_brake_slowdown": 0, "sw_power_cap": 0,
            **kw}


# LC 4.1/4.2: the SW power cap is the design limit of a GPU-bound laptop, not throttling, so it never adds to
# nv_throttle_pct (it used to: 0.3 s thermal + 0.2 s power cap read as 50 %); it has its own share.
@pytest.mark.parametrize("cur, dt_s, expected", [
    (_ns(sw_thermal_slowdown=500_000_000), 1.0, 50.0),
    (_ns(sw_thermal_slowdown=300_000_000, sw_power_cap=200_000_000), 1.0, 30.0),
    (_ns(hw_thermal_slowdown=250_000_000, hw_power_brake_slowdown=250_000_000), 2.0, 25.0),
    (_ns(sw_thermal_slowdown=995_000_000), 1.0, 99.5),               # just under the clamp
    (_ns(sw_thermal_slowdown=1_005_000_000), 1.0, 100.0),            # just over it
    (_ns(sw_thermal_slowdown=600_000_000, hw_thermal_slowdown=600_000_000), 1.0, 100.0),   # overlapping reasons
    (_ns(sw_power_cap=1_000_000_000), 1.0, 0.0),                     # at the power cap all along: no throttling
    (_ns(), 1.0, 0.0),
])
def test_throttle_share_literal_points(cur, dt_s, expected):
    assert throttle_pct(_ns(), cur, dt_s) == pytest.approx(expected)


@pytest.mark.parametrize("cur, dt_s, expected", [
    (_ns(sw_power_cap=1_000_000_000), 1.0, 100.0),
    (_ns(sw_power_cap=300_000_000, sw_thermal_slowdown=700_000_000), 1.0, 30.0),   # heat is not the power cap
    (_ns(sw_power_cap=500_000_000), 2.0, 25.0),
    (_ns(sw_power_cap=1_005_000_000), 1.0, 100.0),
    (_ns(sw_thermal_slowdown=10**9), 1.0, 0.0),
])
def test_power_cap_share_literal_points(cur, dt_s, expected):
    assert gpumon.power_cap_pct(_ns(), cur, dt_s) == pytest.approx(expected)


def test_power_cap_share_is_none_when_it_cannot_be_known():
    assert gpumon.power_cap_pct(_ns(sw_power_cap=10), _ns(sw_power_cap=5), 1.0) is None           # went backwards
    assert gpumon.power_cap_pct({"sw_power_cap": None}, {"sw_power_cap": 5}, 1.0) is None
    assert gpumon.power_cap_pct(_ns(), _ns(sw_power_cap=5), 0.0) is None


def test_throttle_share_rejects_a_counter_that_went_backwards():
    assert throttle_pct(_ns(sw_thermal_slowdown=10), _ns(sw_thermal_slowdown=5, hw_thermal_slowdown=10**9), 1.0) is None


@pytest.mark.parametrize("dt_s", [0, 0.0, -1.0, math.nan, math.inf, None, "1.0"])
def test_throttle_share_rejects_bad_intervals(dt_s):
    assert throttle_pct(_ns(), _ns(sw_thermal_slowdown=10**8), dt_s) is None


def test_throttle_share_uses_only_counters_present_in_both_readings():
    prev = {"sw_thermal_slowdown": 0, "hw_thermal_slowdown": None}
    cur = {"sw_thermal_slowdown": 400_000_000, "hw_thermal_slowdown": 900_000_000}
    assert throttle_pct(prev, cur, 1.0) == pytest.approx(40.0)
    assert throttle_pct(None, cur, 1.0) is None
    assert throttle_pct({"sw_thermal_slowdown": None}, {"sw_thermal_slowdown": 5}, 1.0) is None


@pytest.mark.parametrize("watts, expected", [(1.0, None), (1.01, 1.01), (0.0, None), (None, None), (math.nan, None),
                                             (47.5, 47.5)])
def test_power_at_or_below_one_watt_is_invalid(watts, expected):
    assert plausible_power(watts) == expected


def test_reasons_text():
    assert (reasons_text(None), reasons_text(0), reasons_text(0x24)) == (None, "", "sw_power_cap,sw_thermal_slowdown")


# ------------------------------------------------------- nvidia-smi text ---

def _capture_lines() -> list[str]:
    return SMI_CAPTURE.read_text(encoding="utf-8").splitlines()


def test_parse_smi_header_strips_units_and_matches_what_we_ask_for():
    assert parse_smi_header(_capture_lines()[0]) == list(gpumon.SMI_FIELDS)
    assert parse_smi_header(_capture_lines()[1]) is None


def test_parse_smi_row_from_a_real_capture():
    row = parse_smi_row(_capture_lines()[1], gpumon.SMI_FIELDS)
    assert row == {
        "when": dt.datetime(2026, 11, 4, 18, 8, 2, 93000), "index": 0, "pci_bus_id": "00000000:01:00.0",
        "name": "NVIDIA GeForce RTX 3070 Laptop GPU", "util_pct": 63,
        "mem_used_mb": 4282.0,                      # used 4108 + reserved 174, NVML's definition
        "mem_total_mb": 8192.0, "temp_c": 82, "power_w": 47.51, "power_limit_w": 55.0, "clock_mhz": 720,
        "clock_max_mhz": 2100, "pstate": 3, "reasons": 0x24,
        "throttle_ns": {"sw_thermal_slowdown": 27_495_830_139_000, "hw_thermal_slowdown": 0,
                        "hw_power_brake_slowdown": 244_000, "sw_power_cap": 662_562_344_000},
    }


def test_the_real_capture_gives_a_per_reason_share_that_matches_by_hand():
    a, b = (parse_smi_row(line, gpumon.SMI_FIELDS) for line in _capture_lines()[1:3])
    dt_s = (b["when"] - a["when"]).total_seconds()                       # 1.007 s
    only = {"sw_thermal_slowdown": a["throttle_ns"]["sw_thermal_slowdown"]}
    later = {"sw_thermal_slowdown": b["throttle_ns"]["sw_thermal_slowdown"]}
    assert throttle_pct(only, later, dt_s) == pytest.approx(100 * 999_720 / 1_007_000, abs=1e-6)
    # Both counters ran the whole second (mask 0x24): heat alone is the throttling, the power cap its own share.
    # (Summed, the two used to read as a clamped 100 %.)
    assert throttle_pct(a["throttle_ns"], b["throttle_ns"], dt_s) == pytest.approx(100 * 999_720 / 1_007_000)
    assert gpumon.power_cap_pct(a["throttle_ns"], b["throttle_ns"], dt_s) == pytest.approx(100 * 999_720 / 1_007_000)


def _with(line: str, **values: str) -> str:
    parts = line.split(", ")
    for name, v in values.items():
        parts[gpumon.SMI_FIELDS.index(name.replace("__", "."))] = v
    return ", ".join(parts)


@pytest.mark.parametrize("field, text", [("power.draw", "[N/A]"), ("temperature.gpu", "[Not Supported]"),
                                         ("utilization.gpu", "N/A"), ("pstate", "[Unknown Error]"),
                                         ("clocks_event_reasons.active", "[N/A]"), ("memory.used", "nan"),
                                         ("name", "[N/A]"), ("name", "N/A")])
def test_parse_smi_row_reads_not_available_as_none(field, text):
    line = _with(_capture_lines()[1], **{field.replace(".", "__"): text})
    row = parse_smi_row(line, gpumon.SMI_FIELDS)
    key = {"power.draw": "power_w", "temperature.gpu": "temp_c", "utilization.gpu": "util_pct",
           "pstate": "pstate", "clocks_event_reasons.active": "reasons", "memory.used": "mem_used_mb",
           "name": "name"}[field]
    assert row is not None and row[key] is None


def test_parse_smi_row_counters_not_available_read_none():
    line = _with(_capture_lines()[1], clocks_event_reasons_counters__sw_power_cap="[N/A]")
    assert parse_smi_row(line, gpumon.SMI_FIELDS)["throttle_ns"]["sw_power_cap"] is None


@pytest.mark.parametrize("text, expected", [("P0", 0), ("P15", 15), ("P16", None), ("P", None), ("3", None)])
def test_parse_smi_pstate(text, expected):
    assert parse_smi_row(_with(_capture_lines()[1], pstate=text), gpumon.SMI_FIELDS)["pstate"] == expected


def test_parse_smi_row_keeps_a_comma_in_the_gpu_name():
    line = _capture_lines()[1].replace("NVIDIA GeForce RTX 3070 Laptop GPU", "Odd, Name GPU")
    assert parse_smi_row(line, gpumon.SMI_FIELDS)["name"] == "Odd, Name GPU"


@pytest.mark.parametrize("line", ["", "garbage", "2026/11/04 18:08:02.093, 0", "NVIDIA-SMI has failed because it "
                                  "couldn't communicate with the NVIDIA driver.",
                                  _with(_capture_lines()[1], timestamp="yesterday"),
                                  _with(_capture_lines()[1], index="[N/A]")])
def test_parse_smi_row_refuses_what_is_not_a_row(line):
    assert parse_smi_row(line, gpumon.SMI_FIELDS) is None


def test_lines_strip_crlf_and_skip_an_overlong_line_whole():
    data = b"a,b\r\n" + b"x" * 10_000 + b"\r\n" + b"c,d\r\n" + b"tail-without-newline"
    assert list(gpumon._lines(io.BufferedReader(io.BytesIO(data)), 4096)) == ["a,b", None, "c,d",
                                                                             "tail-without-newline"]


# ============================================= collector over a fake NVML ===

class GridNvml:
    """A fake NVML whose values refresh on a fixed grid in real time, as the driver's do every ~500 ms.

    The `reason` counter (SW thermal unless told) grows by `share` of each refresh period, so the true share
    of that reason is `share`; the other counters stay at 0."""

    def __init__(self, period: float, share: float, devices: list[dict] | None = None, mark_moves: bool = True,
                 values: dict | None = None, reason: str = "sw_thermal_slowdown", mark_delay: float = 0.0) -> None:
        self.t0 = clock.now()
        self.period, self.share, self.mark_moves, self.reason = period, share, mark_moves, reason
        self.mark_delay = mark_delay              # each fingerprint read takes this long (a thread held up)
        self._devices = devices or [{"index": 0, "name": "Fake GPU", "pci_bus_id": "00000000:01:00.0",
                                     "mem_total_mb": 8192.0, "clock_max_mhz": 2100, "temp_target_c": 75}]
        self.values = values
        self.sampled: list[int] = []
        self.closed = 0

    def _k(self) -> int:
        return int((clock.now() - self.t0) // self.period)

    def devices(self):
        return [dict(d) for d in self._devices]

    def driver_version(self):
        return "999.99"

    def refresh_mark(self, index):
        if self.mark_delay:
            time.sleep(self.mark_delay)
        return (self._k() if self.mark_moves else 0,)

    def sample(self, index):
        self.sampled.append(index)
        k = self._k()
        if self.mark_moves:
            ns = int(k * self.period * self.share * 1e9)
        else:                                     # counters grow smoothly, the fingerprint never moves
            ns = int((clock.now() - self.t0) * self.share * 1e9)
        s = {"util_pct": 90, "mem_used_mb": 4000.0, "temp_c": 80, "power_w": 50.0 + k % 2, "power_limit_w": 88.0,
             "clock_mhz": 780, "pstate": 3, "reasons": 0x20,
             "throttle_ns": {**dict.fromkeys(gpumon.COUNTERS, 0), self.reason: ns}}
        if self.values is not None:
            s.update(self.values)
        return s

    def close(self):
        self.closed += 1


def run_sampler(monkeypatch, fake, seconds: float, adapter=None) -> NvidiaSampler:
    monkeypatch.setattr(gpumon.Nvml, "open", classmethod(lambda cls: fake))
    s = NvidiaSampler(adapter)
    s.start(clock.now())
    time.sleep(seconds)
    s.stop()
    return s


def shares(s: NvidiaSampler, column: str = G.THROTTLE_PCT) -> list:
    return s.result()["columns"][column][1:]


@pytest.mark.parametrize("share", [0.4, 1.0])
def test_rows_are_read_just_after_a_refresh_so_the_share_is_exact(monkeypatch, share):
    # Production timing (1 s rows, 0.6 s wait) over a 0.45 s grid: a row read at a fixed moment would hold two
    # or three refreshes per second and read 36 %/54 % (or 90 %/100 %) instead of the true share.
    s = run_sampler(monkeypatch, GridNvml(period=0.45, share=share), 4.4)
    got = shares(s)
    assert len(got) >= 3, got
    assert all(v is not None and abs(v - 100 * share) <= 2.5 for v in got), got
    assert s.status().state == "ok", s.status()


def test_rows_stay_exact_at_a_faster_rate(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.4), 3.0)
    got = shares(s)
    assert len(got) >= 4 and all(v is not None and abs(v - 40) <= 3 for v in got), got


def test_a_gpu_at_its_power_cap_is_not_throttling_and_gets_its_own_share(monkeypatch):
    # LC 4.1/4.2: the power cap held the clocks the whole time; heat and the power brake never did.
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=1.0, reason="sw_power_cap"), 2.2)
    throttle, cap = shares(s), shares(s, G.POWER_CAP_PCT)
    assert len(throttle) >= 2 and all(v == 0.0 for v in throttle), throttle
    assert all(v is not None and abs(v - 100) <= 3 for v in cap), cap
    assert s.result()["columns"][G.POWER_CAP_PCT][0] is None          # no interval before the first row
    assert s.status().state == "ok", s.status()


def test_heat_throttling_reads_as_throttling_and_not_as_the_power_cap(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.6), 2.2)
    throttle, cap = shares(s), shares(s, G.POWER_CAP_PCT)
    assert len(cap) >= 2 and all(v == 0.0 for v in cap), cap
    assert all(v is not None and abs(v - 60) <= 3 for v in throttle), throttle


@pytest.mark.parametrize("reason", ["sw_thermal_slowdown", "sw_power_cap"])
def test_an_unaligned_row_reports_no_share_rather_than_a_wrong_one(monkeypatch, reason):
    column = {"sw_thermal_slowdown": G.THROTTLE_PCT, "sw_power_cap": G.POWER_CAP_PCT}[reason]
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.2)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.5, mark_moves=False, reason=reason), 2.0)
    got = shares(s, column)
    assert len(got) >= 2 and all(v is None for v in got), got


def test_an_unaligned_row_with_no_throttle_still_reads_zero(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.2)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.0, mark_moves=False), 2.0)
    got, cap = shares(s), shares(s, G.POWER_CAP_PCT)
    assert len(got) >= 2 and all(v == 0.0 for v in got), got
    assert all(v == 0.0 for v in cap), cap


@pytest.mark.parametrize("mark_moves", [True, False])
def test_each_row_records_whether_it_was_read_just_after_a_refresh(monkeypatch, mark_moves):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)               # longer than the 0.22 s grid: never misses one
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.5, mark_moves=mark_moves), 1.6)
    assert len(s.row_aligned) == len(s.series) >= 2
    assert set(s.row_aligned) == {mark_moves}


def test_a_refresh_seen_late_is_not_aligned_and_gives_no_share(monkeypatch):
    # Each look at the fingerprint takes 40 ms (the thread held up under load): the refresh it finds may be up to
    # ~50 ms old, past ALIGN_SLACK_S, so the row's time does not match its counters and no share is claimed.
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.35)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.5, mark_delay=0.04), 2.2)
    assert len(s.row_aligned) >= 3 and set(s.row_aligned) == {False}, s.row_aligned
    assert all(v is None for v in shares(s)), shares(s)


def _no_smi():
    raise OSError("nvidia-smi is not used in this test")


def test_a_game_on_another_gpu_leaves_the_source_off_without_opening_nvml(monkeypatch):
    opened = []
    monkeypatch.setattr(gpumon.Nvml, "open", classmethod(lambda cls: opened.append(1)))
    monkeypatch.setattr(gpumon, "smi_argv", _no_smi)
    s = NvidiaSampler({"vendor": "intel", "vendor_id": 0x8086, "name": "Intel(R) UHD Graphics", "pci": "00:02.0"})
    s.start(clock.now())
    time.sleep(0.3)
    s.stop()
    st = s.status()
    assert st.state == "off" and "Intel(R) UHD Graphics" in st.detail and "not an NVIDIA GPU" in st.detail
    assert opened == []
    assert s.result() == {"columns": {}, "device": None}


def test_the_game_gpu_is_picked_by_pci_location_not_position(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)
    devices = [{"index": 0, "name": "First", "pci_bus_id": "00000000:02:00.0", "mem_total_mb": 4096.0,
                "clock_max_mhz": 1800, "temp_target_c": 80},
               {"index": 1, "name": "Second", "pci_bus_id": "00000000:01:00.0", "mem_total_mb": 8192.0,
                "clock_max_mhz": 2100, "temp_target_c": 75}]
    fake = GridNvml(period=0.22, share=0.0, devices=devices)
    s = run_sampler(monkeypatch, fake, 1.2, adapter={"vendor": "nvidia", "pci": "01:00.0"})
    assert fake.sampled and set(fake.sampled) == {1}
    assert s.result()["device"] == {"name": "Second", "clock_max_mhz": 2100, "mem_total_mb": 8192.0,
                                    "temp_target_c": 75}
    assert "Second" in s.status().detail and "PCI location" in s.status().detail


def test_no_gpu_at_the_game_location_is_unavailable(monkeypatch):
    devices = [dict(DEVS[0]), dict(DEVS[1])]
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.0, devices=devices), 0.3,
                    adapter={"vendor": "nvidia", "pci": "03:00.0"})
    assert s.status().state == "unavailable" and "03:00.0" in s.status().detail
    assert s.result()["columns"] == {}


def test_result_columns_device_live_and_close(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)
    fake = GridNvml(period=0.22, share=0.5)
    s = run_sampler(monkeypatch, fake, 1.3)
    r = s.result()
    cols = r["columns"]
    assert set(cols) == set(G.ALL)
    assert len(cols[G.T]) >= 2 and all(b > a for a, b in zip(cols[G.T], cols[G.T][1:]))
    assert cols[G.REASONS][0] == "sw_thermal_slowdown" and cols[G.CLOCK_MAX_MHZ][0] == 2100
    assert cols[G.THROTTLE_PCT][0] is None                 # no interval before the first row
    assert set(r["device"]) == set(gpumon.DEVICE_KEYS)
    live = s.live()
    assert live["source"] == "nvml" and live[G.T] == cols[G.T][-1]
    assert fake.closed == 1
    st = s.status()
    assert st.state == "ok" and st.tool == "nvml" and "999.99" in st.detail


def test_a_value_never_measured_makes_the_source_partial(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.3)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.5, values={"power_w": None}), 1.3)
    st = s.status()
    assert st.state == "partial" and "never measured: nv_power_w" in st.detail, st


def test_samples_with_no_values_make_the_source_failed(monkeypatch):
    monkeypatch.setattr(gpumon, "PERIOD_S", 0.5)
    monkeypatch.setattr(gpumon, "REFRESH_WAIT_S", 0.2)
    empty = {k: None for k in ("util_pct", "mem_used_mb", "temp_c", "power_w", "power_limit_w", "clock_mhz",
                               "pstate", "reasons")}
    empty["throttle_ns"] = dict.fromkeys(gpumon.COUNTERS)
    s = run_sampler(monkeypatch, GridNvml(period=0.22, share=0.0, values=empty), 1.3)
    st = s.status()
    assert st.state == "failed" and "gave no values" in st.detail, st


def test_stop_is_prompt_while_waiting_for_a_refresh(monkeypatch):
    fake = GridNvml(period=0.45, share=0.0, mark_moves=False)          # every tick waits the full 0.6 s
    monkeypatch.setattr(gpumon.Nvml, "open", classmethod(lambda cls: fake))
    s = NvidiaSampler(None)
    s.start(clock.now())
    time.sleep(0.3)
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 0.3
    assert fake.closed == 1


# ============================================== nvidia-smi fallback (fake) ===

def fake_smi_argv(*args: str) -> list[str]:
    # the base interpreter, not the venv launcher, so terminating the child ends the process that writes
    return [getattr(sys, "_base_executable", sys.executable), "-I", str(FAKE_SMI), "--fixture", str(SMI_CAPTURE),
            *args]


def wait_rows(s: NvidiaSampler, n: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(s.series) >= n:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def no_nvml(monkeypatch):
    monkeypatch.setattr(gpumon.Nvml, "open", classmethod(lambda cls: None))
    monkeypatch.setattr(gpumon.Nvml, "last_open_error", "test: NVML switched off")


def start_fallback(monkeypatch, *args: str, adapter=None) -> NvidiaSampler:
    monkeypatch.setattr(gpumon, "smi_argv", lambda: fake_smi_argv(*args))
    s = NvidiaSampler(adapter)
    s.start(clock.now())
    return s


def _cleanup(s: NvidiaSampler) -> None:
    s.stop()
    child = s._child
    if child is not None and child.poll() is None:      # never leave a stand-in running
        child.kill()
        child.wait(5)


def test_fallback_streams_rows_and_says_which_source_ran(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--share", "0.5", "--cap-share", "0.3")
    try:
        assert wait_rows(s, 4, 10), s.status()
        assert s._job is not None and s._job.holds(s._child)          # dies with us, however we die (LC-2)
    finally:
        _cleanup(s)
    st = s.status()
    assert st.state == "ok" and st.tool == "nvidia-smi", st
    assert "nvidia-smi streaming" in st.detail and "test: NVML switched off" in st.detail
    assert "not tied to lag-doctor's lifetime" not in st.detail
    cols = s.result()["columns"]
    assert cols[G.UTIL_PCT][0] == 63 and cols[G.MEM_USED_MB][0] == 4282.0 and cols[G.PSTATE][0] == 3
    assert cols[G.REASONS][0] == "sw_power_cap,sw_thermal_slowdown"
    got = cols[G.THROTTLE_PCT][1:]
    assert got and all(v is not None and abs(v - 50) <= 3 for v in got), got      # heat only, not + 30 power cap
    cap = cols[G.POWER_CAP_PCT][1:]
    assert all(v is not None and abs(v - 30) <= 3 for v in cap), cap
    assert cols[G.POWER_CAP_PCT][0] is None and set(s.row_aligned) == {None}     # smi rows are never aligned
    assert s.result()["device"] == {"name": "NVIDIA GeForce RTX 3070 Laptop GPU", "clock_max_mhz": 2100,
                                    "mem_total_mb": 8192.0, "temp_target_c": None}


def test_a_child_that_cannot_join_the_job_still_streams_and_says_so(monkeypatch, no_nvml):
    class NoJob:
        def __init__(self):
            raise OSError(5, "Access is denied")

    monkeypatch.setattr(gpumon, "KillOnCloseJob", NoJob)
    s = start_fallback(monkeypatch, "--rows", "3")
    try:
        assert wait_rows(s, 2, 10), s.status()
    finally:
        _cleanup(s)
    st = s.status()
    assert st.state == "ok" and "not tied to lag-doctor's lifetime" in st.detail and "Access is denied" in st.detail


ROOT = Path(__file__).resolve().parents[1]
BASE_PY = getattr(sys, "_base_executable", sys.executable)


def test_a_hard_kill_of_lag_doctor_takes_its_nvidia_smi_child_with_it(tmp_path):
    # LC-2: closing the console window, Ctrl+Break or a crash ends lag-doctor with no finally and no atexit.
    # The real nvidia-smi then polls the GPU into a dead pipe for good; the stand-in beats a file instead.
    beat, out = tmp_path / "beat", tmp_path / "out.txt"
    try:
        with open(tmp_path / "helper.log", "wb") as log:
            helper = subprocess.run([BASE_PY, "-I", str(FIXTURES / "smi_orphan.py"), str(ROOT), str(beat), str(out)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, timeout=30)
        pid, rows = (int(x) for x in out.read_text().split())
        assert helper.returncode == 9 and pid > 0 and rows >= 2, (helper.returncode, out.read_text(),
                                                                   (tmp_path / "helper.log").read_text())
        time.sleep(0.5)                                            # let a beat in flight land
        size = beat.stat().st_size
        time.sleep(1.5)                                            # it beats every 0.25 s while it lives
        assert beat.stat().st_size == size, "the nvidia-smi stand-in outlived the process that started it"
    finally:
        (tmp_path / "beat.stop").write_text("x")                  # a survivor exits at its next beat


def test_fallback_stop_ends_the_child_and_drains_its_pipes(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--rows", "2")                  # then idles, like nvidia-smi
    try:
        assert wait_rows(s, 2, 10), s.status()
        child = s._child
        t0 = time.monotonic()
        s.stop()
        assert time.monotonic() - t0 < 4, "stop() waited on the child"
        assert child.returncode == 1, child.returncode               # TerminateProcess(handle, 1)
        assert child.stdout.closed and child.stderr.closed
        assert s.status().state == "ok", s.status()
    finally:
        _cleanup(s)


def test_fallback_drains_stderr_so_a_chatty_child_cannot_block(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--stderr-bytes", str(1 << 20))
    try:
        assert wait_rows(s, 2, 10), s.status()
    finally:
        _cleanup(s)
    assert s.smi_stderr.endswith("e" * 50)


def test_fallback_child_that_exits_at_once_fails_with_what_it_said(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--fail-text",
                       "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.")
    try:
        deadline = time.monotonic() + 10
        while s.status().state != "failed" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        _cleanup(s)
    st = s.status()
    assert st.state == "failed" and "code 9" in st.detail and "couldn't communicate" in st.detail, st


def test_fallback_child_that_stops_by_itself_is_partial(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--rows", "2", "--exit", "0")
    try:
        deadline = time.monotonic() + 10
        while s.status().state not in ("partial", "failed") and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        _cleanup(s)
    st = s.status()
    assert st.state == "partial" and "stopped by itself after 2 samples" in st.detail, st


def test_fallback_header_with_other_columns_fails_loudly(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--extra-column")
    try:
        deadline = time.monotonic() + 10
        while s.status().state != "failed" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        _cleanup(s)
    st = s.status()
    assert st.state == "failed" and "other columns" in st.detail and "extra.field" in st.detail, st
    assert len(s.series) == 0


def test_fallback_counts_junk_and_overlong_lines_and_keeps_going(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--junk")
    try:
        assert wait_rows(s, 3, 10), s.status()
    finally:
        _cleanup(s)
    assert s.smi_malformed == 2


def test_fallback_picks_the_game_gpu_by_pci_location(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--gpus", "2", adapter={"vendor": "nvidia", "pci": "PCI bus 2, device 0, "
                                                                                       "function 0"})
    try:
        assert wait_rows(s, 3, 10), s.status()
    finally:
        _cleanup(s)
    assert "00000000:02:00.0" in s.status().detail
    t = s.result()["columns"][G.T]
    assert all(b - a > 0.15 for a, b in zip(t, t[1:])), t        # one GPU's rows only, not both per loop


def test_fallback_with_no_gpu_at_the_game_location_is_unavailable(monkeypatch, no_nvml):
    s = start_fallback(monkeypatch, "--gpus", "2", adapter={"vendor": "nvidia", "pci": "03:00.0"})
    try:
        deadline = time.monotonic() + 10
        while s.status().state != "unavailable" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        _cleanup(s)
    st = s.status()
    assert st.state == "unavailable" and "03:00.0" in st.detail, st
    assert s._child.returncode is not None


def test_a_stop_before_nvidia_smi_starts_starts_no_child(monkeypatch, no_nvml):
    monkeypatch.setattr(gpumon, "smi_argv", lambda: fake_smi_argv("--rows", "1", "--exit", "0"))
    s = NvidiaSampler(None)
    s._stop_event.set()                  # stop() arrived between NVML failing and the child starting
    s._run_smi("test reason")
    st = s.status()
    assert s._child is None
    assert st.state == "partial" and "stopped before nvidia-smi started" in st.detail, st


def test_fallback_unavailable_when_nvidia_smi_is_missing(monkeypatch, no_nvml, tmp_path):
    monkeypatch.setattr(gpumon, "smi_path", lambda: str(tmp_path / "nvidia-smi.exe"))
    s = NvidiaSampler(None)
    s.start(clock.now())
    time.sleep(0.3)
    s.stop()
    st = s.status()
    assert st.state == "unavailable" and "test: NVML switched off" in st.detail and "not found" in st.detail, st
