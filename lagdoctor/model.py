"""The shared data model: what a capture holds and what a diagnosis says.

Every module codes against the names defined here. Collectors write columns
named by the F, S, G and N classes; the analysis reads them by the same names.
A SeriesBuilder refuses unknown column names, so a typo fails loudly at the
writer instead of silently producing an empty column at the reader.

Clock: every timestamp in a capture is seconds on the capture clock, which is
time.perf_counter() (QueryPerformanceCounter on Windows) minus
Capture.clock_origin. PresentMon's --qpc_time values, LagLab's logs and our own
samplers all share that clock, so no alignment guesswork is needed.
"""
from __future__ import annotations

import gzip
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

CAPTURE_SCHEMA = "lagdoctor.capture/1"


# ---------------------------------------------------------------- columns ---

class F:
    """Frame columns: one row per frame the game presented (from PresentMon 2.6, --qpc_time).

    PresentMon writes some missing values as 0 instead of NA (MsCPUBusy, MsCPUWait,
    MsBetweenAppStart, and MsGPUBusy on frames that are not the app's own); the
    parser stores those as None. See docs/research/presentmon.md section 2.3.
    """

    T = "t"                                # s, capture clock: when the app called Present (TimeInQPC)
    FRAME_MS = "frame_ms"                  # MsBetweenPresents: time since the previous present
    CPU_BUSY_MS = "cpu_busy_ms"            # MsCPUBusy: wall time from the last Present's return to this call
                                           # (an in-game frame limiter's sleep counts here: not CPU load)
    CPU_WAIT_MS = "cpu_wait_ms"            # MsCPUWait: time blocked (e.g. inside Present)
    GPU_LATENCY_MS = "gpu_latency_ms"      # CPU start -> GPU start
    GPU_BUSY_MS = "gpu_busy_ms"            # time the GPU was actually working on the frame
    GPU_WAIT_MS = "gpu_wait_ms"            # GPU idle time inside the frame
    DISPLAYED = "displayed"                # 1 shown, 0 dropped/never shown, None unknown
    DISPLAY_MS = "display_ms"              # MsBetweenDisplayChange: how long the previous frame stayed on screen
    DISPLAY_LATENCY_MS = "display_latency_ms"  # MsCPUBusy + MsUntilDisplayed: CPU start -> on screen
    INPUT_LATENCY_MS = "input_latency_ms"  # input -> photon, when PresentMon could measure it
    ANIM_ERROR_MS = "anim_error_ms"        # MsAnimationError: animation time vs display time (can be negative)
    PRESENT_MODE = "present_mode"          # e.g. "Hardware: Independent Flip", "Composed: Flip"
    RUNTIME = "runtime"                    # "DXGI", "D3D9", "Other" (Vulkan/OpenGL)
    SYNC_INTERVAL = "sync_interval"        # 0 = no VSync, -1 = unknown
    ALLOWS_TEARING = "allows_tearing"      # 1/0
    SWAP_CHAIN = "swap_chain"              # swap chain address (a process may have several)
    FRAME_TYPE = "frame_type"              # "Application", or a frame-generation type
    HYBRID = "hybrid"                      # 1 cross-adapter copy; 0 means only "not seen" (weak evidence)

    ALL = (T, FRAME_MS, CPU_BUSY_MS, CPU_WAIT_MS, GPU_LATENCY_MS, GPU_BUSY_MS, GPU_WAIT_MS,
           DISPLAYED, DISPLAY_MS, DISPLAY_LATENCY_MS, INPUT_LATENCY_MS, ANIM_ERROR_MS, PRESENT_MODE,
           RUNTIME, SYNC_INTERVAL, ALLOWS_TEARING, SWAP_CHAIN, FRAME_TYPE, HYBRID)


class S:
    """System columns: one row per sampler tick (about 4 per second). None = not measured."""

    T = "t"
    # CPU, whole machine
    CPU_TOTAL_PCT = "cpu_total_pct"            # 0-100 over all logical CPUs
    CPU_MAX_CORE_PCT = "cpu_max_core_pct"      # busiest logical CPU, 0-100
    CPU_PERF_PCT = "cpu_perf_pct"              # effective speed vs base clock (>100 = turbo); drops = throttling
    CPU_PERF_LIMIT_PCT = "cpu_perf_limit_pct"  # Processor Information % Performance Limit; 100 = no power/thermal cap
    THERMAL_LIMIT_PCT = "thermal_limit_pct"    # lowest ACPI passive limit; 100 = not thermally limited
    DPC_PCT = "dpc_pct"                        # time in deferred procedure calls (driver work), busiest CPU
    INTERRUPT_PCT = "interrupt_pct"            # busiest CPU
    # The game process (read without opening a handle to it)
    GAME_CPU_PCT = "game_cpu_pct"              # 100 = one full logical CPU
    GAME_TOP_THREAD_PCT = "game_top_thread_pct"  # busiest game thread, 100 = one full logical CPU
    GAME_TOP_THREAD_ID = "game_top_thread_id"
    GAME_THREADS = "game_threads"
    GAME_WS_MB = "game_ws_mb"                  # working set
    GAME_PRIVATE_MB = "game_private_mb"
    GAME_PAGE_FAULTS_PS = "game_page_faults_ps"  # soft + hard
    GAME_IO_READ_BPS = "game_io_read_bps"
    GAME_IO_WRITE_BPS = "game_io_write_bps"
    GAME_IO_OPS_PS = "game_io_ops_ps"
    GAME_FOCUSED = "game_focused"              # 1 if the game owns the foreground window (Windows deprioritises it if not)
    # GPU, from Windows counters (all vendors)
    GAME_GPU_PCT = "game_gpu_pct"              # the game's busiest engine type (normally 3D) on its adapter
    GPU_PCT = "gpu_pct"                        # that engine type on the game's adapter, all processes
    GPU_OTHER_PCT = "gpu_other_pct"            # same, excluding the game
    VRAM_USED_MB = "vram_used_mb"              # dedicated memory in use on the game's adapter
    GAME_VRAM_MB = "game_vram_mb"              # the game's own dedicated usage
    GAME_SHARED_MB = "game_shared_mb"          # the game's shared (system RAM) GPU usage: spill when VRAM is full
    # Memory and disk
    RAM_AVAIL_MB = "ram_avail_mb"
    COMMIT_PCT = "commit_pct"                  # committed bytes / commit limit
    HARD_FAULTS_PS = "hard_faults_ps"          # Memory\\Pages Input/sec: reads from disk to satisfy page faults
    DISK_BUSY_PCT = "disk_busy_pct"            # 100 - % idle, busiest physical disk
    DISK_READ_BPS = "disk_read_bps"
    DISK_READ_MS = "disk_read_ms"              # Avg. Disk sec/Read x 1000, busiest physical disk
    DISK_QUEUE = "disk_queue"

    ALL = (T, CPU_TOTAL_PCT, CPU_MAX_CORE_PCT, CPU_PERF_PCT, CPU_PERF_LIMIT_PCT, THERMAL_LIMIT_PCT, DPC_PCT,
           INTERRUPT_PCT, GAME_CPU_PCT, GAME_TOP_THREAD_PCT, GAME_TOP_THREAD_ID, GAME_THREADS, GAME_WS_MB,
           GAME_PRIVATE_MB, GAME_PAGE_FAULTS_PS, GAME_IO_READ_BPS, GAME_IO_WRITE_BPS, GAME_IO_OPS_PS,
           GAME_FOCUSED, GAME_GPU_PCT, GPU_PCT, GPU_OTHER_PCT, VRAM_USED_MB, GAME_VRAM_MB, GAME_SHARED_MB,
           RAM_AVAIL_MB, COMMIT_PCT, HARD_FAULTS_PS, DISK_BUSY_PCT, DISK_READ_BPS, DISK_READ_MS, DISK_QUEUE)


class G:
    """NVIDIA columns from NVML (nvidia-smi as fallback), about 1 per second. Absent on other GPUs."""

    T = "t"
    UTIL_PCT = "nv_util_pct"
    MEM_USED_MB = "nv_mem_used_mb"
    TEMP_C = "nv_temp_c"
    POWER_W = "nv_power_w"
    POWER_LIMIT_W = "nv_power_limit_w"
    CLOCK_MHZ = "nv_clock_mhz"
    CLOCK_MAX_MHZ = "nv_clock_max_mhz"
    PSTATE = "nv_pstate"
    REASONS = "nv_reasons"     # comma-separated active clock-event (throttle) reasons, "" = none
    THROTTLE_PCT = "nv_throttle_pct"  # share of the interval the clock was held down by heat or power (0-100)

    ALL = (T, UTIL_PCT, MEM_USED_MB, TEMP_C, POWER_W, POWER_LIMIT_W, CLOCK_MHZ, CLOCK_MAX_MHZ, PSTATE, REASONS,
           THROTTLE_PCT)


class N:
    """Network columns (about 4 per second). RTT None in a row = that probe was lost or not sent this tick.

    The game server's own packet stream (needs ETW) lives in Capture.net_flows, written by the
    net_flows collector, so each series keeps exactly one writer.
    """

    T = "t"
    GW_RTT_MS = "gw_rtt_ms"            # to the default gateway (your router)
    INET_RTT_MS = "inet_rtt_ms"        # to a stable internet anchor
    SERVER_RTT_MS = "server_rtt_ms"    # to the game server, when it answers pings
    WIFI_SIGNAL_PCT = "wifi_signal_pct"
    WIFI_RSSI_DBM = "wifi_rssi_dbm"
    WIFI_BSSID = "wifi_bssid"          # access point; a change = roaming (needs location permission)
    WIFI_CHANNEL = "wifi_channel"      # a change without a BSSID also hints at roaming
    WIFI_RX_MBPS = "wifi_rx_mbps"
    WIFI_TX_MBPS = "wifi_tx_mbps"
    WIFI_RETRIES_PS = "wifi_retries_ps"  # driver-reported retransmissions per second
    NIC_RX_BPS = "nic_rx_bps"          # whole adapter, all processes
    NIC_TX_BPS = "nic_tx_bps"

    ALL = (T, GW_RTT_MS, INET_RTT_MS, SERVER_RTT_MS, WIFI_SIGNAL_PCT, WIFI_RSSI_DBM, WIFI_BSSID,
           WIFI_CHANNEL, WIFI_RX_MBPS, WIFI_TX_MBPS, WIFI_RETRIES_PS, NIC_RX_BPS, NIC_TX_BPS)


class SeriesBuilder:
    """Collects rows into aligned columns. Missing values become None; unknown names raise."""

    def __init__(self, columns: Iterable[str]) -> None:
        self.columns: dict[str, list] = {c: [] for c in columns}
        if "t" not in self.columns:
            raise ValueError("a series needs a 't' column")

    def add(self, **row: Any) -> None:
        unknown = set(row) - set(self.columns)
        if unknown:
            raise KeyError(f"unknown column(s) {sorted(unknown)}; known: {sorted(self.columns)}")
        if row.get("t") is None:
            raise ValueError("every row needs a time 't'")
        for name, col in self.columns.items():
            col.append(row.get(name))

    def __len__(self) -> int:
        return len(self.columns["t"])

    def build(self) -> dict[str, list]:
        return {k: list(v) for k, v in self.columns.items()}


# ---------------------------------------------------------------- capture ---

SourceState = Literal["ok", "partial", "unavailable", "failed", "off"]


@dataclass
class SourceStatus:
    """Whether a data source worked, so the report never mistakes 'not measured' for 'fine'."""

    state: SourceState
    detail: str = ""
    tool: str = ""


@dataclass
class Capture:
    """Everything recorded during one session, saved as gzip-compressed JSON."""

    started_at: str                          # ISO-8601 wall-clock time with offset
    clock_origin: float                      # time.perf_counter() value at t = 0
    duration_s: float
    target: dict[str, Any]                   # pid, name, exe, how it was chosen, adapter it renders on ...
    sources: dict[str, SourceStatus]         # "frames", "system", "gpu", "network", "net_flows", ...
    inventory: dict[str, Any]                # static facts: hardware, settings, displays (see capture.inventory)
    frames: dict[str, list] = field(default_factory=dict)    # F columns
    system: dict[str, list] = field(default_factory=dict)    # S columns
    gpu: dict[str, list] = field(default_factory=dict)       # G columns
    network: dict[str, list] = field(default_factory=dict)   # N columns
    others: list = field(default_factory=list)  # [[t, [[pid, name, cpu_pct, io_bps, gpu_pct], ...]], ...]
    net_flows: dict[str, Any] = field(default_factory=dict)  # game server endpoint, top talkers by process
    marks: list[float] = field(default_factory=list)         # user's "it lagged now" hotkey presses
    notes: list[str] = field(default_factory=list)           # anything a collector wants the report to say
    schema: str = CAPTURE_SCHEMA
    tool_version: str = ""

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        return _clean_floats(data)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Capture":
        if data.get("schema") != CAPTURE_SCHEMA:
            raise ValueError(f"not a lag-doctor capture (schema {data.get('schema')!r})")
        sources = {k: SourceStatus(**v) for k, v in data.get("sources", {}).items()}
        fields = {k: v for k, v in data.items() if k != "sources"}
        return cls(sources=sources, **fields)

    def save(self, path: Path) -> Path:
        """Write atomically: a crash mid-save never leaves a half-written capture."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(self.to_json(), f, separators=(",", ":"))
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: Path) -> "Capture":
        with gzip.open(Path(path), "rt", encoding="utf-8") as f:
            return cls.from_json(json.load(f))


def _clean_floats(obj: Any) -> Any:
    """JSON has no NaN/inf; store them as None. Round floats to keep files small."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _clean_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_floats(v) for v in obj]
    return obj


# -------------------------------------------------------------- diagnosis ---

Confidence = Literal["high", "medium", "low"]
Category = Literal["fps", "stutter", "pacing", "latency", "network", "setup"]
EventKind = Literal["stutter", "fps_drop", "pacing", "net_spike", "net_loss", "net_gap", "mark"]
LagType = Literal["none", "low_fps", "stutter", "pacing", "network", "mixed", "unknown"]


@dataclass
class Evidence:
    """One measured fact behind a finding, in plain English, with its number."""

    text: str
    value: float | None = None
    unit: str = ""
    threshold: float | None = None


@dataclass
class FixSuggestion:
    """A fix the tool can apply (fix_id set) or advice the user follows by hand (fix_id None)."""

    title: str
    detail: str = ""
    fix_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    needs_admin: bool = False
    needs_restart: str | None = None      # "game", "reboot" or None


@dataclass
class Finding:
    """One cause of lag, with its evidence and what to do about it."""

    cause: str                 # stable id, e.g. "gpu_limit", "wrong_gpu", "background_cpu"
    title: str                 # one plain-English line
    category: Category
    confidence: Confidence
    score: float               # 0..1, for ranking only
    summary: str               # one or two sentences
    evidence: list[Evidence] = field(default_factory=list)
    fixes: list[FixSuggestion] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)


@dataclass
class LagEvent:
    """A stretch of time where the game lagged: a stutter, an FPS drop, a ping spike ..."""

    id: int
    kind: EventKind
    t_start: float
    t_end: float
    severity: float            # kind-specific: worst frame ms, FPS drop %, RTT ms ...
    detail: dict[str, Any] = field(default_factory=dict)
    causes: list[str] = field(default_factory=list)   # cause ids, most likely first


@dataclass
class Diagnosis:
    """The verdict on one capture."""

    lag_type: LagType
    headline: str
    stats: dict[str, Any] = field(default_factory=dict)
    events: list[LagEvent] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    measured: dict[str, str] = field(default_factory=dict)   # source -> "ok" / why it was not measured
    cleared: dict[str, str] = field(default_factory=dict)    # cause -> what was measured that rules it out
    unchecked: dict[str, str] = field(default_factory=dict)  # cause -> why it could not be checked

    def to_json(self) -> dict[str, Any]:
        return _clean_floats(asdict(self))
