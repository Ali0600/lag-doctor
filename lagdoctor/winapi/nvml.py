"""NVML, the NVIDIA driver's management library, through ctypes: list the GPUs, read one sample.

A thin wrapper with no policy. Values come out as plain Python numbers; any value
the driver will not give reads as None, never 0. Layouts, enum values and units are
from nvml.h at go-nvml v0.13.4-1, as checked in docs/research/windows-telemetry.md
section 4.4 ("WT 4.4") and its verification log; each constant below cites it.

nvml.dll is loaded from the Windows system directory by absolute path, never
through the DLL search path, so a stray nvml.dll elsewhere can never load.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Any, Callable

NVML_SUCCESS = 0
NVML_TEMPERATURE_GPU = 0      # nvmlTemperatureSensors_t (WT 4.4)
NVML_CLOCK_GRAPHICS = 0       # nvmlClockType_t (WT 4.4)
PSTATE_MAX = 15               # P0..P15; NVML_PSTATE_UNKNOWN is 32 (nvml.h, WT 4.4)
# nvmlTemperatureThresholds_t. GPS_CURR (7) is what `nvidia-smi -q -d TEMPERATURE` shows as "GPU Target
# Temperature" on this laptop (both 75 C, 2026-11-04); ACOUSTIC_CURR (5), "current temperature that is set as
# acoustic threshold", is Not Supported here and is only a fallback for boards that have it [inferred].
TEMP_THRESHOLD_GPS_CURR = 7
TEMP_THRESHOLD_ACOUSTIC_CURR = 5

# Cumulative time each clocks-event reason held the clocks down, in nanoseconds
# (nvmlDeviceGetFieldValues, WT 4.4; verification log #53). nvidia-smi's
# clocks_event_reasons_counters.* are the same counters in microseconds.
# Measured here (2026-11-04): fields 269 and 74 cost about 1.6 us per call, 270 and 271 about 250 us.
THROTTLE_FIELDS = {
    "sw_thermal_slowdown": 269,       # NVML_FI_DEV_CLOCKS_EVENT_REASON_SW_THERM_SLOWDOWN
    "hw_thermal_slowdown": 270,       # NVML_FI_DEV_CLOCKS_EVENT_REASON_HW_THERM_SLOWDOWN
    "hw_power_brake_slowdown": 271,   # NVML_FI_DEV_CLOCKS_EVENT_REASON_HW_POWER_BRAKE_SLOWDOWN
    "sw_power_cap": 74,               # NVML_FI_DEV_CLOCKS_EVENT_REASON_SW_POWER_CAP = NVML_FI_DEV_PERF_POLICY_POWER
}
_CHEAP_FIELDS = ("sw_thermal_slowdown", "sw_power_cap")

# Clocks-event-reason bits (WT 4.1 table; 0x200 and 0x400 added by verification log #45). Names follow
# nvidia-smi's clocks_event_reasons.* fields, or nvml.h's names where nvidia-smi has no field.
REASON_BITS = (
    (0x1, "gpu_idle"),
    (0x2, "applications_clocks_setting"),
    (0x4, "sw_power_cap"),
    (0x8, "hw_slowdown"),
    (0x10, "sync_boost"),
    (0x20, "sw_thermal_slowdown"),
    (0x40, "hw_thermal_slowdown"),
    (0x80, "hw_power_brake_slowdown"),
    (0x100, "display_clock_setting"),
    (0x200, "board_limit"),
    (0x400, "reliability"),
)

MAX_DEVICES = 64
_MIB = float(1 << 20)
_NAME_LEN = 256               # more than NVML_DEVICE_NAME_V2_BUFFER_SIZE (96)
_SENTINEL_RETURN = -1         # put in each field entry before a call: an entry the driver skipped reads as failed


_KNOWN_BITS = sum(bit for bit, _ in REASON_BITS)


def reason_names(mask: int) -> list[str]:
    """Names of the set bits, lowest first; a bit nvml.h does not name comes out as hex, never dropped."""
    if mask < 0:
        raise ValueError(f"not a reason bitmask: {mask}")
    names = [name for bit, name in REASON_BITS if mask & bit]
    rest = mask & ~_KNOWN_BITS
    names += [hex(1 << i) for i in range(rest.bit_length()) if rest >> i & 1]
    return names


# ------------------------------------------------------------------ structs ---

class PciInfo(ctypes.Structure):          # nvmlPciInfo_t, 68 bytes (WT 4.5)
    _fields_ = [("busIdLegacy", ctypes.c_char * 16), ("domain", ctypes.c_uint), ("bus", ctypes.c_uint),
                ("device", ctypes.c_uint), ("pciDeviceId", ctypes.c_uint), ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


class Memory(ctypes.Structure):           # nvmlMemory_t (v1), 24 bytes; used = reserved + allocated (WT 4.4)
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class Utilization(ctypes.Structure):      # nvmlUtilization_t, 8 bytes
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class Value(ctypes.Union):                # nvmlValue_t, 8 bytes; unsigned long is 32-bit on Windows
    _fields_ = [("dVal", ctypes.c_double), ("siVal", ctypes.c_int), ("uiVal", ctypes.c_uint),
                ("ulVal", ctypes.c_ulong), ("ullVal", ctypes.c_ulonglong), ("sllVal", ctypes.c_longlong),
                ("usVal", ctypes.c_ushort)]


class FieldValue(ctypes.Structure):       # nvmlFieldValue_t, 40 bytes (WT 4.4)
    _fields_ = [("fieldId", ctypes.c_uint), ("scopeId", ctypes.c_uint), ("timestamp", ctypes.c_longlong),
                ("latencyUsec", ctypes.c_longlong), ("valueType", ctypes.c_int), ("nvmlReturn", ctypes.c_int),
                ("value", Value)]


def _check_sizes(expected: list[tuple[type, int]]) -> None:
    """Refuse to load with a layout that drifted. A raise, not an assert, so `python -O` keeps the check."""
    for struct, size in expected:
        if ctypes.sizeof(struct) != size:
            raise ImportError(f"{struct.__name__} is {ctypes.sizeof(struct)} bytes; NVML's is {size}")


_check_sizes([(PciInfo, 68), (Memory, 24), (Utilization, 8), (Value, 8), (FieldValue, 40)])

# nvmlValueType_t (nvml.h): only the integer kinds; the counters come back as 3 (unsigned long long) here.
_VALUE_READERS: dict[int, Callable[[Value], int]] = {
    1: lambda v: v.uiVal,
    2: lambda v: v.ulVal,
    3: lambda v: v.ullVal,
    4: lambda v: v.sllVal,
    5: lambda v: v.siVal,
    6: lambda v: v.usVal,
}

# ----------------------------------------------------------- the library ---

_U = ctypes.POINTER(ctypes.c_uint)
_DEV = ctypes.c_void_p                    # nvmlDevice_t is an opaque pointer
_RC = ctypes.c_int                        # nvmlReturn_t
_PROTOTYPES: dict[str, tuple[list, Any]] = {
    "nvmlInit_v2": ([], _RC),
    "nvmlShutdown": ([], _RC),
    "nvmlErrorString": ([_RC], ctypes.c_char_p),
    "nvmlSystemGetDriverVersion": ([ctypes.c_char_p, ctypes.c_uint], _RC),
    "nvmlDeviceGetCount_v2": ([_U], _RC),
    "nvmlDeviceGetHandleByIndex_v2": ([ctypes.c_uint, ctypes.POINTER(_DEV)], _RC),
    "nvmlDeviceGetName": ([_DEV, ctypes.c_char_p, ctypes.c_uint], _RC),
    "nvmlDeviceGetPciInfo_v3": ([_DEV, ctypes.POINTER(PciInfo)], _RC),
    "nvmlDeviceGetMemoryInfo": ([_DEV, ctypes.POINTER(Memory)], _RC),
    "nvmlDeviceGetMaxClockInfo": ([_DEV, ctypes.c_int, _U], _RC),
    "nvmlDeviceGetTemperatureThreshold": ([_DEV, ctypes.c_int, _U], _RC),
    "nvmlDeviceGetUtilizationRates": ([_DEV, ctypes.POINTER(Utilization)], _RC),
    "nvmlDeviceGetTemperature": ([_DEV, ctypes.c_int, _U], _RC),
    "nvmlDeviceGetPowerUsage": ([_DEV, _U], _RC),                  # milliwatts
    "nvmlDeviceGetEnforcedPowerLimit": ([_DEV, _U], _RC),          # milliwatts
    "nvmlDeviceGetClockInfo": ([_DEV, ctypes.c_int, _U], _RC),     # MHz
    "nvmlDeviceGetPerformanceState": ([_DEV, ctypes.POINTER(ctypes.c_int)], _RC),
    "nvmlDeviceGetCurrentClocksEventReasons": ([_DEV, ctypes.POINTER(ctypes.c_ulonglong)], _RC),
    # the older name of the same call (deprecated in 13.0, WT 4.4 caveat 3), for drivers without the new one
    "nvmlDeviceGetCurrentClocksThrottleReasons": ([_DEV, ctypes.POINTER(ctypes.c_ulonglong)], _RC),
    "nvmlDeviceGetFieldValues": ([_DEV, ctypes.c_int, ctypes.POINTER(FieldValue)], _RC),
}
_REQUIRED = ("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetCount_v2", "nvmlDeviceGetHandleByIndex_v2")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
_kernel32.GetSystemDirectoryW.restype = wintypes.UINT


def system_dir() -> str:
    """The Windows system directory (C:\\Windows\\System32), from GetSystemDirectoryW."""
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH + 1)
    n = _kernel32.GetSystemDirectoryW(buf, len(buf))
    if n == 0 or n >= len(buf):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.value


def dll_path() -> str:
    return os.path.join(system_dir(), "nvml.dll")


def _load_library(path: str) -> Any:
    # Given a path with a backslash, ctypes loads exactly that file (LoadLibraryExW with
    # LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR; CPython 3.12 ctypes/__init__.py).
    return ctypes.CDLL(path)


class NvmlError(OSError):
    """An NVML call failed. `.code` is its nvmlReturn_t; NVML has no Win32 error, so `.winerror` is None."""

    def __init__(self, code: int, function: str, text: str = "") -> None:
        super().__init__(f"{function} failed: {text or 'NVML error'} (NVML error {code})")
        self.code = code
        self.function = function


def _bind(lib: Any) -> dict[str, Any]:
    """Set argtypes/restype on every export we use; optional ones that are missing are left out."""
    fns: dict[str, Any] = {}
    for name, (argtypes, restype) in _PROTOTYPES.items():
        try:
            fn = getattr(lib, name)
        except AttributeError:
            continue
        fn.argtypes = argtypes
        fn.restype = restype
        fns[name] = fn
    if "nvmlDeviceGetCurrentClocksEventReasons" not in fns and "nvmlDeviceGetCurrentClocksThrottleReasons" in fns:
        fns["nvmlDeviceGetCurrentClocksEventReasons"] = fns["nvmlDeviceGetCurrentClocksThrottleReasons"]
    missing = [n for n in _REQUIRED if n not in fns]
    if missing:
        raise OSError(f"nvml.dll does not export {', '.join(missing)} (driver too old?)")
    return fns


class Nvml:
    """One initialised NVML session. Use Nvml.open(); call close() when done (nvmlShutdown)."""

    last_open_error: str = ""             # why the most recent open() returned None ("" after a success)

    def __init__(self, fns: dict[str, Any]) -> None:
        """Private: takes the bound functions of an initialised library (tests pass fakes)."""
        self._fns = fns
        self._lock = threading.Lock()
        self._closed = False
        self._handles: dict[int, ctypes.c_void_p | None] = {}
        # Reused buffers: a sample allocates no ctypes objects.
        self._uint = ctypes.c_uint()
        self._int = ctypes.c_int()
        self._ull = ctypes.c_ulonglong()
        self._util = Utilization()
        self._mem = Memory()
        self._pci = PciInfo()
        self._name = ctypes.create_string_buffer(_NAME_LEN)
        self._field_names = tuple(THROTTLE_FIELDS)
        self._fields = (FieldValue * len(self._field_names))()
        self._mark_fields = (FieldValue * len(_CHEAP_FIELDS))()

    @classmethod
    def open(cls) -> "Nvml | None":
        """Load nvml.dll by absolute path and initialise it. None (reason in last_open_error) if that fails."""
        try:
            path = dll_path()
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{path} not found (no NVIDIA driver installed?)")
            fns = _bind(_load_library(path))
            rc = fns["nvmlInit_v2"]()
            if rc != NVML_SUCCESS:
                raise NvmlError(rc, "nvmlInit_v2", _error_text(fns, rc))
        except OSError as exc:
            cls.last_open_error = str(exc) or type(exc).__name__
            return None
        cls.last_open_error = ""
        return cls(fns)

    # ---- public ---------------------------------------------------------
    def devices(self) -> list[dict]:
        """{index, name, pci_bus_id, mem_total_mb, clock_max_mhz, temp_target_c} per GPU, in NVML index order."""
        with self._lock:
            self._check_open()
            count = ctypes.c_uint()
            rc = self._fns["nvmlDeviceGetCount_v2"](ctypes.byref(count))
            if rc != NVML_SUCCESS:
                raise NvmlError(rc, "nvmlDeviceGetCount_v2", _error_text(self._fns, rc))
            return [self._describe(i, self._handle(i)) for i in range(min(count.value, MAX_DEVICES))]

    def sample(self, index: int) -> dict:
        """{util_pct, mem_used_mb, temp_c, power_w, power_limit_w, clock_mhz, pstate, reasons, throttle_ns}.

        Each value is None when its call fails; throttle_ns maps each reason in THROTTLE_FIELDS to its
        cumulative nanoseconds (or None). Never calls nvmlDeviceGetDisplayActive (about 170 us, WT 4.4).
        """
        with self._lock:
            self._check_open()
            h = self._handle(index)
            out: dict[str, Any] = {"util_pct": None, "mem_used_mb": None, "temp_c": None, "power_w": None,
                                   "power_limit_w": None, "clock_mhz": None, "pstate": None, "reasons": None,
                                   "throttle_ns": dict.fromkeys(self._field_names)}
            if h is None:
                return out
            if self._call("nvmlDeviceGetUtilizationRates", h, ctypes.byref(self._util)):
                out["util_pct"] = self._util.gpu
            if self._call("nvmlDeviceGetMemoryInfo", h, ctypes.byref(self._mem)):
                out["mem_used_mb"] = self._mem.used / _MIB
            if self._call("nvmlDeviceGetTemperature", h, NVML_TEMPERATURE_GPU, ctypes.byref(self._uint)):
                out["temp_c"] = self._uint.value
            if self._call("nvmlDeviceGetPowerUsage", h, ctypes.byref(self._uint)):
                out["power_w"] = self._uint.value / 1000.0
            if self._call("nvmlDeviceGetEnforcedPowerLimit", h, ctypes.byref(self._uint)):
                out["power_limit_w"] = self._uint.value / 1000.0
            if self._call("nvmlDeviceGetClockInfo", h, NVML_CLOCK_GRAPHICS, ctypes.byref(self._uint)):
                out["clock_mhz"] = self._uint.value
            if self._call("nvmlDeviceGetPerformanceState", h, ctypes.byref(self._int)) \
                    and 0 <= self._int.value <= PSTATE_MAX:
                out["pstate"] = self._int.value
            if self._call("nvmlDeviceGetCurrentClocksEventReasons", h, ctypes.byref(self._ull)):
                out["reasons"] = self._ull.value
            out["throttle_ns"] = self._read_fields(h, self._fields, self._field_names)
            return out

    def refresh_mark(self, index: int) -> tuple:
        """A cheap fingerprint of values the driver refreshes together every ~500 ms (WT 4.4 caveat 1):
        (util %, power mW, temperature C, graphics MHz, SW thermal ns, SW power-cap ns), None where a call
        fails. It changes when the driver refreshes. About 10 us, against about 250 us for sample()."""
        with self._lock:
            self._check_open()
            h = self._handle(index)
            if h is None:
                return (None,) * 6
            util = self._util.gpu if self._call("nvmlDeviceGetUtilizationRates", h, ctypes.byref(self._util)) \
                else None
            power = self._uint.value if self._call("nvmlDeviceGetPowerUsage", h, ctypes.byref(self._uint)) \
                else None
            temp = self._uint.value if self._call("nvmlDeviceGetTemperature", h, NVML_TEMPERATURE_GPU,
                                                  ctypes.byref(self._uint)) else None
            mhz = self._uint.value if self._call("nvmlDeviceGetClockInfo", h, NVML_CLOCK_GRAPHICS,
                                                 ctypes.byref(self._uint)) else None
            counters = self._read_fields(h, self._mark_fields, _CHEAP_FIELDS)
            return (util, power, temp, mhz, counters["sw_thermal_slowdown"], counters["sw_power_cap"])

    def driver_version(self) -> str | None:
        with self._lock:
            self._check_open()
            if self._call("nvmlSystemGetDriverVersion", self._name, _NAME_LEN):
                return self._name.value.decode("ascii", "replace") or None
            return None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handles.clear()
            self._fns["nvmlShutdown"]()

    # ---- internals ------------------------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("NVML session is closed")

    def _call(self, name: str, *args: Any) -> bool:
        fn = self._fns.get(name)
        return fn is not None and fn(*args) == NVML_SUCCESS

    def _handle(self, index: int) -> ctypes.c_void_p | None:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < MAX_DEVICES:
            raise ValueError(f"not a GPU index: {index!r}")
        if index not in self._handles:
            h = ctypes.c_void_p()
            ok = self._call("nvmlDeviceGetHandleByIndex_v2", index, ctypes.byref(h)) and bool(h.value)
            self._handles[index] = h if ok else None
        return self._handles[index]

    def _describe(self, index: int, h: ctypes.c_void_p | None) -> dict:
        d: dict[str, Any] = {"index": index, "name": None, "pci_bus_id": None, "mem_total_mb": None,
                             "clock_max_mhz": None, "temp_target_c": None}
        if h is None:
            return d
        if self._call("nvmlDeviceGetName", h, self._name, _NAME_LEN):
            d["name"] = self._name.value.decode("utf-8", "replace") or None
        if self._call("nvmlDeviceGetPciInfo_v3", h, ctypes.byref(self._pci)):
            d["pci_bus_id"] = self._pci.busId.decode("ascii", "replace") or None
        if self._call("nvmlDeviceGetMemoryInfo", h, ctypes.byref(self._mem)):
            d["mem_total_mb"] = self._mem.total / _MIB
        if self._call("nvmlDeviceGetMaxClockInfo", h, NVML_CLOCK_GRAPHICS, ctypes.byref(self._uint)):
            d["clock_max_mhz"] = self._uint.value
        for threshold in (TEMP_THRESHOLD_GPS_CURR, TEMP_THRESHOLD_ACOUSTIC_CURR):
            if self._call("nvmlDeviceGetTemperatureThreshold", h, threshold, ctypes.byref(self._uint)):
                d["temp_target_c"] = self._uint.value
                break
        return d

    def _read_fields(self, h: ctypes.c_void_p, fields: Any, names: tuple[str, ...]) -> dict[str, int | None]:
        out: dict[str, int | None] = dict.fromkeys(names)
        for entry, name in zip(fields, names):
            entry.fieldId = THROTTLE_FIELDS[name]
            entry.scopeId = 0
            entry.nvmlReturn = _SENTINEL_RETURN
        if not self._call("nvmlDeviceGetFieldValues", h, len(names), fields):
            return out
        for entry, name in zip(fields, names):
            if entry.nvmlReturn != NVML_SUCCESS or entry.fieldId != THROTTLE_FIELDS[name]:
                continue
            reader = _VALUE_READERS.get(entry.valueType)
            if reader is not None:
                out[name] = int(reader(entry.value))
        return out


def _error_text(fns: dict[str, Any], rc: int) -> str:
    fn = fns.get("nvmlErrorString")
    if fn is None:
        return ""
    try:
        text = fn(rc)
    except (OSError, ctypes.ArgumentError):
        return ""
    return text.decode("ascii", "replace") if text else ""
