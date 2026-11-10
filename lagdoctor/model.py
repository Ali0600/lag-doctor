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
import re
import zlib
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from functools import partial
from operator import is_not
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

CAPTURE_SCHEMA = "lagdoctor.capture/1"
CAPTURE_FAMILY, CAPTURE_VERSION = "lagdoctor.capture", 1
MAX_CAPTURE_JSON_BYTES = 2 << 30          # uncompressed; a file that expands past this is refused, not loaded
# gzip level of Capture.save: the fastest. Closing the console window leaves the save 4.5 s (cli). Measured
# here on a synthetic 30-minute 144 FPS capture (259,200 frames), the whole save: level 1 2.6 s and 12.0 MB,
# 4 2.9 s and 10.4 MB, 6 5.0 s and 9.6 MB, 9 28.3 s and 9.5 MB (the old save: 36 s, 9.1 MB).
SAVE_GZIP_LEVEL = 1
SAVE_PIECES_AHEAD = 4                     # encoded columns waiting for the compressor (memory stays bounded)
NOTE_MAX = 200                            # characters in a recording's note (watch --note), counted as it is kept
NOTE_CUT = "..."                          # ends a note read from a file that held more than NOTE_MAX


def clean_note(value: Any, limit: int | None = NOTE_MAX) -> str | None:
    """A recording's note as it is kept and shown: the player's own words, one line. Each character that is not
    printable (a control character, a lone surrogate, a mark that reorders text) becomes a space, each run of spaces
    one space, and an empty note is none; anything but a string is none. Past `limit` characters it is cut and ends
    in NOTE_CUT (a note from a file: `watch` refuses a longer one instead); limit None keeps it whole."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    if limit is not None and len(text) > limit:
        text = text[:limit - len(NOTE_CUT)].rstrip() + NOTE_CUT
    return text or None


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
    GAME_TOP_THREAD_PCT = "game_top_thread_pct"  # busiest game thread this tick, 100 = one full logical CPU
    GAME_TOP_THREAD_ID = "game_top_thread_id"
    # this tick's share of the thread that was busiest over the 1 s before it: what the bound judges, since the
    # busiest of several equally busy threads in one tick reads high (15.625 ms accounting, WT 3.5)
    GAME_LEAD_THREAD_PCT = "game_lead_thread_pct"
    GAME_THREADS = "game_threads"
    GAME_WS_MB = "game_ws_mb"                  # working set
    GAME_PRIVATE_MB = "game_private_mb"
    GAME_PAGE_FAULTS_PS = "game_page_faults_ps"  # soft + hard
    GAME_IO_READ_BPS = "game_io_read_bps"      # every read the game made, the file cache's included (not disk reads)
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
    DISK_READ_BPS = "disk_read_bps"            # Disk Read Bytes/sec of every physical disk together (_Total)
    DISK_READ_MS = "disk_read_ms"              # Avg. Disk sec/Read x 1000, busiest physical disk
    DISK_QUEUE = "disk_queue"

    ALL = (T, CPU_TOTAL_PCT, CPU_MAX_CORE_PCT, CPU_PERF_PCT, CPU_PERF_LIMIT_PCT, THERMAL_LIMIT_PCT, DPC_PCT,
           INTERRUPT_PCT, GAME_CPU_PCT, GAME_TOP_THREAD_PCT, GAME_TOP_THREAD_ID, GAME_LEAD_THREAD_PCT, GAME_THREADS,
           GAME_WS_MB, GAME_PRIVATE_MB, GAME_PAGE_FAULTS_PS, GAME_IO_READ_BPS, GAME_IO_WRITE_BPS, GAME_IO_OPS_PS,
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
    THROTTLE_PCT = "nv_throttle_pct"  # share of the interval held down by heat or the HW power brake (0-100)
    POWER_CAP_PCT = "nv_power_cap_pct"  # share at the SW power cap (0-100): the design limit, not throttling

    ALL = (T, UTIL_PCT, MEM_USED_MB, TEMP_C, POWER_W, POWER_LIMIT_W, CLOCK_MHZ, CLOCK_MAX_MHZ, PSTATE, REASONS,
           THROTTLE_PCT, POWER_CAP_PCT)


class N:
    """Network columns (2 per second: one probe round every 0.5 s). RTT None in a row = that probe was lost or not
    sent this tick.

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


class CaptureError(ValueError):
    """A file that cannot be loaded as a capture. `kind`: "foreign" (not a lag-doctor capture), "newer" (a
    format a newer lag-doctor wrote) or "damaged". The message is one plain sentence."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


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
    note: str | None = None                  # the player's own words about this recording (watch --note): clean_note
    # Set only on a copy cut to part of the recording (analysis/crop.py): {from_s, to_s, recorded_s, how}. Never saved
    # or read from a file, which always holds the whole recording.
    span: dict[str, Any] | None = field(default=None, init=False)

    def to_json(self) -> dict[str, Any]:
        return _clean_floats(self._fields())

    def _fields(self) -> dict[str, Any]:
        """The saved fields as they are (no copy): _clean_floats and save() turn nested dataclasses into dicts."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.init}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Capture":
        """The Capture in `data`. Fields a newer build added are left out, with a note in `notes`; anything
        else that does not fit raises CaptureError saying what it is (another format, a newer one, damaged)."""
        if not isinstance(data, dict):
            raise CaptureError("foreign", "not a lag-doctor capture (it holds no capture record)")
        _check_schema(data.get("schema"))
        known = {f.name: f for f in fields(cls) if f.init}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = [str(k) for k in data if k not in known]
        missing = [n for n, f in known.items()
                   if n not in kwargs and f.default is MISSING and f.default_factory is MISSING]
        if missing:
            raise CaptureError("damaged", f"the capture is damaged: it has no {', '.join(missing)}")
        for name, want in _CAPTURE_TYPES.items():
            if name in kwargs and (not isinstance(kwargs[name], want) or isinstance(kwargs[name], bool)):
                raise CaptureError("damaged", f"the capture is damaged: its {name} is a "
                                              f"{type(kwargs[name]).__name__}")
        status_fields = {f.name for f in fields(SourceStatus)}
        sources: dict[str, SourceStatus] = {}
        for src, st in kwargs["sources"].items():
            if not isinstance(st, dict) or not isinstance(st.get("state"), str):
                raise CaptureError("damaged", f"the capture is damaged: its source {_shown(src)} has no state")
            extra += [f"sources.{k}" for k in st if k not in status_fields and f"sources.{k}" not in extra]
            sources[src] = SourceStatus(**{k: v for k, v in st.items() if k in status_fields})
        kwargs["sources"] = sources
        cap = cls(**kwargs)
        # A newer build may keep a longer note: shown cut, never refused (it is a line of text, not a measurement).
        cap.note = clean_note(cap.note)
        if extra:
            cap.notes = list(cap.notes) + [_extra_fields_note(sorted(extra), cap.tool_version)]
        return cap

    def save(self, path: Path) -> Path:
        """Write atomically: a crash mid-save never leaves a half-written capture.

        The same gzip-compressed JSON as ever, written one column at a time with the C encoder, so the save
        is quick (closing the console window leaves it 4.5 s) and never builds a copy of the whole capture.
        Floats are written as "%.6f" text, which loads as exactly round(x, 6), the value to_json() holds.
        One worker thread compresses and writes while the next column is encoded (zlib lets go of the GIL).
        A copy cut to part of the recording (`span` set) is refused: saved, it would read as the whole recording.
        """
        if self.span is not None:
            raise ValueError("a capture cut to part of the recording is never saved: save the whole recording")
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "wb") as raw, ThreadPoolExecutor(1, thread_name_prefix="lagdoctor-save") as pool:
                gz = zlib.compressobj(SAVE_GZIP_LEVEL, zlib.DEFLATED, 31)     # wbits 31: gzip header + CRC

                def put(data: bytes) -> None:                               # runs on the worker, in order
                    raw.write(gz.compress(data))
                pending: deque[Future] = deque()
                for piece in _json_pieces(self._fields(), 2):
                    pending.append(pool.submit(put, piece.encode("ascii")))  # ensure_ascii: ASCII only
                    while len(pending) > SAVE_PIECES_AHEAD:
                        pending.popleft().result()                          # bounded; raises the worker's error
                while pending:
                    pending.popleft().result()
                raw.write(gz.flush())
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)     # no half-written file left behind
            except OSError:
                pass
            raise
        return path

    @classmethod
    def load(cls, path: Path) -> "Capture":
        """Read a capture file. CaptureError (one plain sentence) when the file is not a capture this version
        can read; OSError when it cannot be opened at all."""
        with open(Path(path), "rb") as raw:
            if raw.read(2) != b"\x1f\x8b":
                raise CaptureError("foreign", "not a lag-doctor capture (it is not a gzip-compressed file)")
            raw.seek(0)
            try:
                with gzip.GzipFile(fileobj=raw, mode="rb") as f:
                    data = _read_bounded(f, MAX_CAPTURE_JSON_BYTES)
                obj = json.loads(data)
            except (OSError, EOFError, zlib.error) as exc:      # BadGzipFile (CRC, length) is an OSError
                raise CaptureError("damaged", f"the capture is damaged: its compressed data is cut short or "
                                              f"corrupt ({exc})") from exc
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise CaptureError("damaged", f"the capture is damaged: its contents are not valid JSON "
                                              f"({type(exc).__name__})") from exc
        return cls.from_json(obj)


# Capture fields and the JSON types they must have (bool is refused where a number is wanted).
_CAPTURE_TYPES: dict[str, type | tuple[type, ...]] = {
    "started_at": str, "clock_origin": (int, float), "duration_s": (int, float), "target": dict, "sources": dict,
    "inventory": dict, "frames": dict, "system": dict, "gpu": dict, "network": dict, "others": list,
    "net_flows": dict, "marks": list, "notes": list, "schema": str, "tool_version": str, "note": (str, type(None))}


def _shown(value: object, limit: int = 60) -> str:
    """A value from a file, safe to print: repr() escapes control characters; long ones are cut."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _check_schema(schema: object) -> None:
    if schema == CAPTURE_SCHEMA:
        return
    family, _, version = schema.partition("/") if isinstance(schema, str) else ("", "", "")
    if family == CAPTURE_FAMILY and re.fullmatch(r"[0-9]{1,6}", version) and int(version) > CAPTURE_VERSION:
        raise CaptureError("newer", f"it was made by a newer version of lag-doctor (format {_shown(schema)}); "
                                    "update lag-doctor to read it")
    raise CaptureError("foreign", f"not a lag-doctor capture (schema {_shown(schema)})")


def _extra_fields_note(names: list[str], tool_version: str) -> str:
    shown = [re.sub(r"[^A-Za-z0-9_.-]", "?", n)[:40] for n in names[:5]]
    more = f" and {len(names) - 5} more" if len(names) > 5 else ""
    who = re.sub(r"[^A-Za-z0-9_.+-]", "?", tool_version)[:20] if isinstance(tool_version, str) else ""
    return (f"This capture has fields this version of lag-doctor does not know ({', '.join(shown)}{more}); they "
            f"were left out. It was probably made by a newer version" + (f" ({who})." if who else "."))


def _read_bounded(f, limit: int) -> bytearray:
    out = bytearray()
    while chunk := f.read(1 << 20):
        out += chunk
        if len(out) > limit:
            raise CaptureError("too_large", f"the capture expands to more than {limit >> 20} MiB, which is more "
                                            "than lag-doctor reads")
    return out


_PLAIN = frozenset({int, str, bool, type(None)})         # JSON values _clean_floats leaves alone
_FLOAT_OR_NONE = frozenset({float, type(None)})
_SEP = (",", ":")
_F6 = "%.6f".__mod__
_not_none = partial(is_not, None)


def _clean_floats(obj: Any) -> Any:
    """JSON has no NaN/inf; store them as None. Round floats to keep files small. Dataclasses become dicts."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 6)
    if type(obj) in _PLAIN:
        return obj
    if isinstance(obj, dict):
        return {k: _clean_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_floats(v) for v in obj]
    if is_dataclass(obj) and not isinstance(obj, type):
        return _clean_floats(asdict(obj))
    return obj


def _json_pieces(obj: Any, depth: int) -> Iterator[str]:
    """The JSON text of _clean_floats(obj), in pieces: dicts with text keys are opened `depth` levels down, so
    no piece is bigger than one column."""
    if depth > 0 and isinstance(obj, dict) and all(isinstance(k, str) for k in obj):
        sep = "{"
        for k, v in obj.items():
            yield sep + json.dumps(k) + ":"
            sep = ","
            yield from _json_pieces(v, depth - 1)
        yield "}" if sep == "," else "{}"
    elif type(obj) is list:
        yield _list_json(obj)
    else:
        yield json.dumps(_clean_floats(obj), separators=_SEP)


def _list_json(col: list) -> str:
    """json.dumps(_clean_floats(col)) for one list, two to three times as fast for the usual columns. Floats
    are written with "%.6f": the correctly rounded digits round(x, 6) is built from, so the text loads as
    exactly the same float (one conversion instead of round()'s two and repr()'s one). Any other list (ints
    mixed in, NaN, nested rows) goes the generic way."""
    kinds = set(map(type, col))
    if kinds <= _PLAIN:
        return json.dumps(col, separators=_SEP)
    if kinds <= _FLOAT_OR_NONE and all(map(math.isfinite, filter(_not_none, col))):
        if type(None) not in kinds:
            return "[" + _f6_text(col) + "]"
        nones = [i for i, v in enumerate(col) if v is None]
        if len(nones) * 8 > len(col):                   # gaps everywhere: one value at a time
            return "[" + ",".join(["null" if v is None else _F6(v) for v in col]) + "]"
        parts, start = [], 0
        for i in nones:
            if i > start:
                parts.append(_f6_text(col[start:i]))
            parts.append("null")
            start = i + 1
        if start < len(col):
            parts.append(_f6_text(col[start:]))
        return "[" + ",".join(parts) + "]"
    return json.dumps(_clean_floats(col), separators=_SEP)


_F6_BATCH = 512
_F6_FORMAT = ",".join(["%.6f"] * _F6_BATCH)


def _f6_text(values: list) -> str:
    """'%.6f' of every float, comma-separated: 512 to one formatting call, a third faster than one by one."""
    full = len(values) - len(values) % _F6_BATCH
    parts = [_F6_FORMAT % tuple(values[i:i + _F6_BATCH]) for i in range(0, full, _F6_BATCH)]
    if full < len(values):
        parts.append(",".join(map(_F6, values[full:])))
    return ",".join(parts)


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

    @classmethod
    def from_json(cls, data: Any) -> "Diagnosis":
        """The Diagnosis a diagnosis.json holds (to_json's output), for reading a saved session back. Keys a newer
        build added are dropped; ValueError when the record or one of its parts is not the shape to_json writes."""
        def part(kind, value, nested=None):
            if not isinstance(value, dict):
                raise ValueError(f"a {kind.__name__} in the diagnosis is not a record")
            names = {f.name for f in fields(kind)}
            kwargs = {k: v for k, v in value.items() if k in names}
            for name, sub in (nested or {}).items():
                items = kwargs.get(name, [])
                if not isinstance(items, list):
                    raise ValueError(f"the {name} of a {kind.__name__} is not a list")
                kwargs[name] = [part(sub, v) for v in items]
            try:
                return kind(**kwargs)
            except TypeError as exc:                   # a required field is missing
                raise ValueError(f"a {kind.__name__} in the diagnosis is incomplete: {exc}") from None

        if not isinstance(data, dict):
            raise ValueError("not a diagnosis record")
        lists = {"events": LagEvent, "findings": Finding}
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name in lists:
                if not isinstance(value, list):
                    raise ValueError(f"the diagnosis's {f.name} is not a list")
                nested = {"evidence": Evidence, "fixes": FixSuggestion} if f.name == "findings" else None
                value = [part(lists[f.name], v, nested) for v in value]
            elif f.name in ("stats", "measured", "cleared", "unchecked") and not isinstance(value, dict):
                raise ValueError(f"the diagnosis's {f.name} is not a record")
            kwargs[f.name] = value
        if not isinstance(kwargs.get("lag_type"), str) or not isinstance(kwargs.get("headline"), str):
            raise ValueError("the diagnosis has no lag type or headline")
        return cls(**kwargs)
