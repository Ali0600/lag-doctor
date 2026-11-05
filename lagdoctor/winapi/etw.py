"""ETW sessions through ctypes: one controller plus one consumer thread per named session.

Layouts, constants and the call sequence are the tested recipe in
docs/research/network-and-etw.md (NE) section 5. Every struct size and key offset
is checked at import against the NE 5.1 table.

Thin wrapper, no policy: events come out as plain `EtwEvent` tuples, failures as
OSError with `.winerror`.

- Timestamps: the session runs on the QPC clock (Wnode.ClientContext = 1) and the
  consumer asks for raw timestamps, so `EtwEvent.timestamp` is in QueryPerformanceCounter
  ticks, the same clock as `lagdoctor.clock` (convert with `clock.qpc_to_seconds`).
- A real-time session needs an elevated token or Performance Log Users: unelevated,
  StartTraceW returns 5 (NE 5.6).
- `private=True` starts a private in-process session instead, which standard users may
  run (NE 5.6). Private sessions cannot be real time (StartTraceW returns 87, NE 5.6), so
  it logs to a temporary .etl file and the consumer delivers its events when `stop()` is
  called. It only sees events written by this process: a test and self-test harness.
"""
from __future__ import annotations

import atexit
import ctypes
import os
import struct
import tempfile
import threading
import uuid
from ctypes import wintypes
from typing import Callable, NamedTuple

_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG, USHORT, UCHAR = ctypes.c_ulong, ctypes.c_ushort, ctypes.c_ubyte
TRACEHANDLE = ctypes.c_uint64
REGHANDLE = ctypes.c_uint64

# Constants: NE 5.1 (SDK evntrace.h, evntcons.h, wmistr.h).
WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_FILE_MODE_SEQUENTIAL = 0x00000001
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_TRACE_PRIVATE_LOGGER_MODE = 0x00000800
EVENT_TRACE_PRIVATE_IN_PROC = 0x00020000
EVENT_TRACE_CONTROL_QUERY = 0
EVENT_TRACE_CONTROL_STOP = 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_RAW_TIMESTAMP = 0x00001000
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF
CLOCK_QPC = 1                       # Wnode.ClientContext: query performance counter (NE 5.3)

# Error codes: NE 5.1 (SDK winerror.h) and NE 5.4.
ERROR_ACCESS_DENIED = 5
ERROR_BAD_LENGTH = 24
ERROR_INVALID_PARAMETER = 87
ERROR_ALREADY_EXISTS = 183
ERROR_NOACCESS = 998
ERROR_CANCELLED = 1223
ERROR_NO_SYSTEM_RESOURCES = 1450
ERROR_WMI_INSTANCE_NOT_FOUND = 4201
ERROR_CTX_CLOSE_PENDING = 7007

NAME_CHARS = 1024                   # session-name and log-file-name limit, in characters (NE 5.2)
BUFFER_KB = 64                      # NE 5.3: 64 KB buffers, ETW picks the buffer counts
FLUSH_TIMER_S = 1                   # NE 5.3: deliver within about a second
PRIVATE_FILE_MB = 64                # cap on a private session's temporary .etl file
STOP_JOIN_S = 1.5                   # per wait while stopping a real-time consumer (two waits at most)
PRIVATE_DRAIN_S = 30.0              # a private session's whole file is delivered inside stop()


# ---------------------------------------------------------------- structs (NE 5.2) ---

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ULONG), ("Data2", USHORT), ("Data3", USHORT), ("Data4", UCHAR * 8)]

    @classmethod
    def of(cls, text: str) -> "GUID":
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g

    def text(self) -> str:
        return str(uuid.UUID(bytes_le=bytes(self)))


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", ULONG), ("ProviderId", ULONG), ("HistoricalContext", ctypes.c_uint64),
                ("TimeStamp", ctypes.c_int64), ("Guid", GUID), ("ClientContext", ULONG), ("Flags", ULONG)]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", ULONG), ("MinimumBuffers", ULONG),
                ("MaximumBuffers", ULONG), ("MaximumFileSize", ULONG), ("LogFileMode", ULONG),
                ("FlushTimer", ULONG), ("EnableFlags", ULONG), ("FlushThreshold", ctypes.c_long),
                ("NumberOfBuffers", ULONG), ("FreeBuffers", ULONG), ("EventsLost", ULONG),
                ("BuffersWritten", ULONG), ("LogBuffersLost", ULONG), ("RealTimeBuffersLost", ULONG),
                ("LoggerThreadId", wintypes.HANDLE), ("LogFileNameOffset", ULONG), ("LoggerNameOffset", ULONG)]


class EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [("Size", USHORT), ("FieldTypeFlags", USHORT), ("Version", ULONG), ("ThreadId", ULONG),
                ("ProcessId", ULONG), ("TimeStamp", ctypes.c_int64), ("Guid", GUID),
                ("ProcessorTime", ctypes.c_uint64)]


class EVENT_TRACE(ctypes.Structure):
    _fields_ = [("Header", EVENT_TRACE_HEADER), ("InstanceId", ULONG), ("ParentInstanceId", ULONG),
                ("ParentGuid", GUID), ("MofData", ctypes.c_void_p), ("MofLength", ULONG),
                ("ClientContext", ULONG)]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wintypes.WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute",
                                              "wSecond", "wMilliseconds")]


class TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [("Bias", ctypes.c_long), ("StandardName", ctypes.c_wchar * 32), ("StandardDate", SYSTEMTIME),
                ("StandardBias", ctypes.c_long), ("DaylightName", ctypes.c_wchar * 32),
                ("DaylightDate", SYSTEMTIME), ("DaylightBias", ctypes.c_long)]


class TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", ULONG), ("Version", ULONG), ("ProviderVersion", ULONG),
                ("NumberOfProcessors", ULONG), ("EndTime", ctypes.c_int64), ("TimerResolution", ULONG),
                ("MaximumFileSize", ULONG), ("LogFileMode", ULONG), ("BuffersWritten", ULONG),
                ("StartBuffers", ULONG), ("PointerSize", ULONG), ("EventsLost", ULONG), ("CpuSpeedInMHz", ULONG),
                ("LoggerName", ctypes.c_void_p), ("LogFileName", ctypes.c_void_p),
                ("TimeZone", TIME_ZONE_INFORMATION), ("BootTime", ctypes.c_int64), ("PerfFreq", ctypes.c_int64),
                ("StartTime", ctypes.c_int64), ("ReservedFlags", ULONG), ("BuffersLost", ULONG)]


class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Id", USHORT), ("Version", UCHAR), ("Channel", UCHAR), ("Level", UCHAR), ("Opcode", UCHAR),
                ("Task", USHORT), ("Keyword", ctypes.c_uint64)]


class EVENT_HEADER(ctypes.Structure):
    _fields_ = [("Size", USHORT), ("HeaderType", USHORT), ("Flags", USHORT), ("EventProperty", USHORT),
                ("ThreadId", ULONG), ("ProcessId", ULONG), ("TimeStamp", ctypes.c_int64), ("ProviderId", GUID),
                ("EventDescriptor", EVENT_DESCRIPTOR), ("ProcessorTime", ctypes.c_uint64), ("ActivityId", GUID)]


class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [("ProcessorNumber", UCHAR), ("Alignment", UCHAR), ("LoggerId", USHORT)]


class EVENT_HEADER_EXTENDED_DATA_ITEM(ctypes.Structure):
    _fields_ = [("Reserved1", USHORT), ("ExtType", USHORT), ("Linkage", USHORT), ("DataSize", USHORT),
                ("DataPtr", ctypes.c_uint64)]


class EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", EVENT_HEADER), ("BufferContext", ETW_BUFFER_CONTEXT),
                ("ExtendedDataCount", USHORT), ("UserDataLength", USHORT),
                ("ExtendedData", ctypes.POINTER(EVENT_HEADER_EXTENDED_DATA_ITEM)),
                ("UserData", ctypes.c_void_p), ("UserContext", ctypes.c_void_p)]


# The record arrives as a bare address: one copy of its 112 bytes and one unpack is cheaper per
# event than walking nested ctypes views (NE 5.4 budgets 14-22 us of Python per event).
EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)
BUFFER_CALLBACK = ctypes.WINFUNCTYPE(ULONG, ctypes.c_void_p)


class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [("LogFileName", ctypes.c_wchar_p), ("LoggerName", ctypes.c_wchar_p),
                ("CurrentTime", ctypes.c_int64), ("BuffersRead", ULONG), ("ProcessTraceMode", ULONG),
                ("CurrentEvent", EVENT_TRACE), ("LogfileHeader", TRACE_LOGFILE_HEADER),
                ("BufferCallback", BUFFER_CALLBACK), ("BufferSize", ULONG), ("Filled", ULONG),
                ("EventsLost", ULONG), ("EventRecordCallback", EVENT_RECORD_CALLBACK),
                ("IsKernelTrace", ULONG), ("Context", ctypes.c_void_p)]


class EVENT_FILTER_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Ptr", ctypes.c_uint64), ("Size", ULONG), ("Type", ULONG)]


class ENABLE_TRACE_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", ULONG), ("EnableProperty", ULONG), ("ControlFlags", ULONG), ("SourceId", GUID),
                ("EnableFilterDesc", ctypes.POINTER(EVENT_FILTER_DESCRIPTOR)), ("FilterDescCount", ULONG)]


class EVENT_DATA_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Ptr", ctypes.c_uint64), ("Size", ULONG), ("Reserved", ULONG)]


# x64 sizes and offsets from the NE 5.1 table (each proven there by an API accepting or filling the struct).
LAYOUTS: list[tuple[type, int, dict[str, int]]] = [
    (GUID, 16, {}),
    (WNODE_HEADER, 48, {"Guid": 24, "ClientContext": 40, "Flags": 44}),
    (EVENT_TRACE_PROPERTIES, 120, {"BufferSize": 48, "LogFileMode": 64, "FlushTimer": 68, "EventsLost": 88,
                                   "RealTimeBuffersLost": 100, "LoggerThreadId": 104, "LogFileNameOffset": 112,
                                   "LoggerNameOffset": 116}),
    (EVENT_TRACE_HEADER, 48, {}),
    (EVENT_TRACE, 88, {"MofData": 72}),
    (TIME_ZONE_INFORMATION, 172, {}),
    (TRACE_LOGFILE_HEADER, 280, {"PointerSize": 44, "LoggerName": 56, "TimeZone": 72, "BootTime": 248,
                                 "PerfFreq": 256, "StartTime": 264, "ReservedFlags": 272}),
    (EVENT_TRACE_LOGFILEW, 448, {"LoggerName": 8, "ProcessTraceMode": 28, "CurrentEvent": 32,
                                 "LogfileHeader": 120, "BufferCallback": 400, "EventRecordCallback": 424,
                                 "IsKernelTrace": 432, "Context": 440}),
    (EVENT_DESCRIPTOR, 16, {}),
    (EVENT_HEADER, 80, {"ProcessId": 12, "TimeStamp": 16, "ProviderId": 24, "EventDescriptor": 40,
                        "ActivityId": 64}),
    (ETW_BUFFER_CONTEXT, 4, {}),
    (EVENT_RECORD, 112, {"BufferContext": 80, "UserDataLength": 86, "ExtendedData": 88, "UserData": 96,
                         "UserContext": 104}),
    (EVENT_HEADER_EXTENDED_DATA_ITEM, 16, {}),
    (ENABLE_TRACE_PARAMETERS, 48, {"EnableFilterDesc": 32, "FilterDescCount": 40}),
    (EVENT_FILTER_DESCRIPTOR, 16, {}),
    (EVENT_DATA_DESCRIPTOR, 16, {}),
]


def layout_mismatches(layouts: list[tuple[type, int, dict[str, int]]]) -> list[str]:
    """Every size or offset in `layouts` that the ctypes definitions do not match (all of them, not the first)."""
    bad = []
    for cls, size, offsets in layouts:
        if ctypes.sizeof(cls) != size:
            bad.append(f"sizeof({cls.__name__}) = {ctypes.sizeof(cls)}, expected {size}")
        for field, want in offsets.items():
            got = getattr(cls, field).offset
            if got != want:
                bad.append(f"{cls.__name__}.{field} at {got}, expected {want}")
    return bad


# The per-event decoder reads EVENT_RECORD with struct instead of ctypes: these are the fields it
# reads, in order, as (path into EVENT_RECORD, struct code). Its offsets are checked against the
# ctypes definition below, so the two descriptions of the record cannot drift apart.
_RECORD_READS = [
    ("EventHeader.ThreadId", "I"), ("EventHeader.ProcessId", "I"), ("EventHeader.TimeStamp", "q"),
    ("EventHeader.EventDescriptor.Id", "H"), ("EventHeader.EventDescriptor.Version", "B"),
    ("EventHeader.EventDescriptor.Level", "B"), ("EventHeader.EventDescriptor.Opcode", "B"),
    ("EventHeader.EventDescriptor.Task", "H"), ("EventHeader.EventDescriptor.Keyword", "Q"),
    ("UserDataLength", "H"), ("UserData", "Q"),
]
_PROVIDER_SLICE = slice(EVENT_HEADER.ProviderId.offset, EVENT_HEADER.ProviderId.offset + 16)


def _field(path: str) -> tuple[int, int]:
    """(offset, size) of a field inside EVENT_RECORD, from the ctypes definition."""
    cls, off = EVENT_RECORD, 0
    for name in path.split("."):
        off += getattr(cls, name).offset
        cls = dict(cls._fields_)[name]
    return off, ctypes.sizeof(cls)


def record_reads_mismatches(reads: list[tuple[str, str]]) -> list[str]:
    """Reads that are out of order or whose struct code is not the ctypes field's size."""
    bad, pos = [], 0
    for path, code in reads:
        off, size = _field(path)
        if off < pos:
            bad.append(f"EVENT_RECORD read {path} at {off} overlaps the read before it")
        if struct.calcsize("<" + code) != size:
            bad.append(f"EVENT_RECORD read {path} uses {code!r} for a {size}-byte field")
        pos = off + size
    return bad


def _record_format(reads: list[tuple[str, str]]) -> str:
    fmt, pos = "<", 0
    for path, code in reads:
        off, size = _field(path)
        fmt += (f"{off - pos}x" if off > pos else "") + code
        pos = off + size
    tail = ctypes.sizeof(EVENT_RECORD) - pos
    return fmt + (f"{tail}x" if tail > 0 else "")


_LAYOUT_ERRORS = layout_mismatches(LAYOUTS) + record_reads_mismatches(_RECORD_READS)
if _LAYOUT_ERRORS:
    raise ImportError("ETW struct layouts do not match NE 5.1: " + "; ".join(_LAYOUT_ERRORS))
_RECORD = struct.Struct(_record_format(_RECORD_READS))


# ---------------------------------------------------------------- prototypes ---

_advapi32.StartTraceW.argtypes = [ctypes.POINTER(TRACEHANDLE), wintypes.LPCWSTR, ctypes.c_void_p]
_advapi32.StartTraceW.restype = ULONG
_advapi32.ControlTraceW.argtypes = [TRACEHANDLE, wintypes.LPCWSTR, ctypes.c_void_p, ULONG]
_advapi32.ControlTraceW.restype = ULONG
_advapi32.EnableTraceEx2.argtypes = [TRACEHANDLE, ctypes.POINTER(GUID), ULONG, UCHAR, ctypes.c_uint64,
                                     ctypes.c_uint64, ULONG, ctypes.POINTER(ENABLE_TRACE_PARAMETERS)]
_advapi32.EnableTraceEx2.restype = ULONG
_advapi32.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
_advapi32.OpenTraceW.restype = TRACEHANDLE
_advapi32.ProcessTrace.argtypes = [ctypes.POINTER(TRACEHANDLE), ULONG, ctypes.c_void_p, ctypes.c_void_p]
_advapi32.ProcessTrace.restype = ULONG
_advapi32.CloseTrace.argtypes = [TRACEHANDLE]
_advapi32.CloseTrace.restype = ULONG
_advapi32.EventRegister.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(REGHANDLE)]
_advapi32.EventRegister.restype = ULONG
_advapi32.EventUnregister.argtypes = [REGHANDLE]
_advapi32.EventUnregister.restype = ULONG
_advapi32.EventWrite.argtypes = [REGHANDLE, ctypes.POINTER(EVENT_DESCRIPTOR), ULONG,
                                 ctypes.POINTER(EVENT_DATA_DESCRIPTOR)]
_advapi32.EventWrite.restype = ULONG


def _error(rc: int, what: str) -> OSError:
    # FormatError(0) would format GetLastError() instead (NE 5.4), so only real codes get a message.
    text = ctypes.FormatError(rc).strip() if rc else "failed"
    return OSError(None, f"{what}: {text}", None, rc)


def _check_name(name: object, what: str = "session name") -> str:
    if not isinstance(name, str) or not name or len(name) >= NAME_CHARS or "\x00" in name:
        raise ValueError(f"{what} must be 1-{NAME_CHARS - 1} characters with no NUL: {name!r}")
    return name


class _Properties:
    """EVENT_TRACE_PROPERTIES followed by room for the session name, then the log file name (NE 5.2)."""

    def __init__(self, mode: int = 0, guid: GUID | None = None, logfile: str | None = None,
                 max_file_mb: int = 0) -> None:
        base = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
        total = base + 2 * NAME_CHARS * ctypes.sizeof(ctypes.c_wchar)
        self.buf = ctypes.create_string_buffer(total)          # zero-filled, as the docs require
        p = self.p = EVENT_TRACE_PROPERTIES.from_buffer(self.buf)
        p.Wnode.BufferSize = total
        p.Wnode.Flags = WNODE_FLAG_TRACED_GUID
        p.Wnode.ClientContext = CLOCK_QPC
        if guid is not None:
            p.Wnode.Guid = guid
        p.BufferSize = BUFFER_KB
        p.FlushTimer = FLUSH_TIMER_S
        p.LogFileMode = mode
        p.MaximumFileSize = max_file_mb
        p.LoggerNameOffset = base                                # ETW copies the session name here
        p.LogFileNameOffset = base + NAME_CHARS * ctypes.sizeof(ctypes.c_wchar)
        if logfile:                                              # the log file name is ours to copy
            raw = _check_name(logfile, "log file path").encode("utf-16-le") + b"\0\0"
            ctypes.memmove(ctypes.addressof(self.buf) + p.LogFileNameOffset, raw, len(raw))

    def ptr(self) -> int:
        return ctypes.addressof(self.buf)

    def stats(self) -> dict:
        p = self.p
        return {"events_lost": int(p.EventsLost), "buffers_lost": int(p.RealTimeBuffersLost + p.LogBuffersLost),
                "buffers_written": int(p.BuffersWritten), "buffers": int(p.NumberOfBuffers),
                "free_buffers": int(p.FreeBuffers)}


# ---------------------------------------------------------------- public API ---

class EtwEvent(NamedTuple):
    timestamp: int      # EventHeader.TimeStamp in raw QPC ticks (QPC clock, raw timestamps)
    provider: str       # lower-case GUID of one of the session's providers
    id: int
    version: int
    opcode: int
    level: int
    task: int
    keyword: int
    process_id: int     # EventHeader.ProcessId: where the event was logged, not a payload field
    thread_id: int
    data: bytes         # the UserData payload, UserDataLength bytes


def stop_session(name: str) -> int:
    """Stop a session by name (orphan cleanup). 0 stopped one, 4201 none was running, 5 no rights."""
    props = _Properties()
    return int(_advapi32.ControlTraceW(0, _check_name(name), props.ptr(), EVENT_TRACE_CONTROL_STOP))


def query_session(name: str) -> int:
    """ControlTraceW QUERY by name: 0 a session of that name is running, 4201 none (NE 5.6)."""
    props = _Properties()
    return int(_advapi32.ControlTraceW(0, _check_name(name), props.ptr(), EVENT_TRACE_CONTROL_QUERY))


def _check_providers(providers: object) -> list[tuple[GUID, int, int, str]]:
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ValueError("a session needs at least one (guid, level, keywords) provider")
    out = []
    for item in providers:
        guid_text, level, keywords = item
        canonical = str(uuid.UUID(str(guid_text)))
        if not isinstance(level, int) or not 0 <= level <= 255:
            raise ValueError(f"provider level must be 0-255: {level!r}")
        if not isinstance(keywords, int) or not 0 <= keywords <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f"provider keywords must fit in 64 bits: {keywords!r}")
        out.append((GUID.of(canonical), level, keywords, canonical))
    return out


class RealtimeSession:
    """One named ETW session with its providers enabled, consumed on a worker thread.

    `on_event(EtwEvent)` runs on the consumer thread for every event of the enabled providers;
    it must be quick. An exception it raises is caught and counted (`health()["callback_errors"]`),
    never allowed to escape into ctypes (NE 5.4: that floods stderr and goes uncounted).

    For `private=True`, register the provider in this process before `start()` (EventRegister,
    see `Provider`): the session takes the first provider's GUID, as in NE 5.6.
    """

    def __init__(self, name: str, providers: list[tuple[str, int, int]], on_event: Callable[[EtwEvent], None],
                 private: bool = False) -> None:
        if not callable(on_event):
            raise TypeError("on_event must be callable")
        self.name = _check_name(name)
        self.private = bool(private)
        self._providers = _check_providers(providers)
        self._wanted = {bytes(g): text for g, _, _, text in self._providers}
        self._on_event = on_event
        self._cb = EVENT_RECORD_CALLBACK(self._on_record)    # kept for the object's lifetime (NE 5.4)
        self._lock = threading.Lock()                        # serialises start/stop
        self._consumer_lock = threading.Lock()
        self._session = TRACEHANDLE(0)                       # non-zero while the session runs
        self._consumer = INVALID_PROCESSTRACE_HANDLE
        self._logfile: EVENT_TRACE_LOGFILEW | None = None    # kept alive while ProcessTrace runs
        self._pump: threading.Thread | None = None
        self._file_closed = threading.Event()                # private: the .etl file is complete
        self._etl_path: str | None = None
        self._started = False
        self._stopped = False
        self._atexit_hook: Callable[[], None] | None = None
        self._final: dict | None = None                      # stats returned by the STOP call
        self._events = 0
        self._skipped = 0
        self._callback_errors = 0
        self._first_error: str | None = None
        self._process_trace_rc: int | None = None
        self._open_error: int | None = None
        self._orphan_stop_rc: int | None = None
        self._stop_rc: int | None = None
        self._close_rc: int | None = None
        self._clock: tuple[int, int] | None = None           # (ReservedFlags clock type, PerfFreq) from OpenTraceW
        self._file_note: str | None = None

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Start the session (stopping an orphan of the same name first), enable the providers and
        start consuming. Raises OSError (.winerror 5 = no rights) and leaves nothing running."""
        with self._lock:
            if self._started:
                raise RuntimeError(f"ETW session {self.name!r} was already started")
            self._started = True
            self._orphan_stop_rc = stop_session(self.name)
            if self.private:
                self._etl_path = os.path.join(tempfile.gettempdir(),
                                              f"lagdoctor-etw-{os.getpid()}-{uuid.uuid4().hex}.etl")
                mode = EVENT_TRACE_PRIVATE_LOGGER_MODE | EVENT_TRACE_PRIVATE_IN_PROC | EVENT_TRACE_FILE_MODE_SEQUENTIAL
                props = _Properties(mode, guid=self._providers[0][0], logfile=self._etl_path,
                                    max_file_mb=PRIVATE_FILE_MB)
            else:
                props = _Properties(EVENT_TRACE_REAL_TIME_MODE)
            rc = _advapi32.StartTraceW(ctypes.byref(self._session), self.name, props.ptr())
            if rc == ERROR_ALREADY_EXISTS:                   # lost a race with another starter (NE 5.5)
                stop_session(self.name)
                rc = _advapi32.StartTraceW(ctypes.byref(self._session), self.name, props.ptr())
            if rc != 0:
                self._session = TRACEHANDLE(0)
                self._stopped = True
                self._remove_file()
                raise _error(rc, "StartTraceW")
            self._atexit_hook = self.stop
            atexit.register(self._atexit_hook)
            try:
                for guid, level, keywords, _ in self._providers:
                    rc = _advapi32.EnableTraceEx2(self._session, ctypes.byref(guid), EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                                  level, keywords, 0, 0, None)
                    if rc != 0:
                        raise _error(rc, "EnableTraceEx2")
                if self.private:
                    target = self._pump_file
                else:
                    self._open(EVENT_TRACE_LOGFILEW(LoggerName=self.name),
                               PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
                               | PROCESS_TRACE_MODE_RAW_TIMESTAMP)
                    target = self._pump_realtime
                self._pump = threading.Thread(target=target, name=f"etw-{self.name}", daemon=True)
                self._pump.start()
            except BaseException:
                self._stopped = True
                self._stop_locked()
                raise

    def stop(self) -> None:
        """Stop the session and the consumer. Idempotent; safe before start and from atexit."""
        with self._lock:
            if not self._started or self._stopped:
                return
            self._stopped = True
            self._stop_locked()

    def health(self) -> dict:
        """{events, lost, callback_errors, pump_alive, process_trace_rc} plus details; lost is None
        when it could not be read (a session that never ran, or a failed QUERY)."""
        handle = self._session.value
        query_rc = None
        stats = self._final
        if handle:
            q = _Properties()
            query_rc = int(_advapi32.ControlTraceW(handle, None, q.ptr(), EVENT_TRACE_CONTROL_QUERY))
            stats = q.stats() if query_rc == 0 else None
        pump = self._pump
        return {
            "events": self._events,
            "lost": stats["events_lost"] if stats else None,
            "callback_errors": self._callback_errors,
            "pump_alive": pump is not None and pump.is_alive(),
            "process_trace_rc": self._process_trace_rc,
            "buffers_lost": stats["buffers_lost"] if stats else None,
            "skipped": self._skipped,
            "first_error": self._first_error,
            "running": bool(handle),
            "query_rc": query_rc,
            "orphan_stop_rc": self._orphan_stop_rc,
            "stop_rc": self._stop_rc,
            "open_error": self._open_error,
            "clock": self._clock,
            "file": self._file_note,
        }

    # ---- internals ------------------------------------------------------
    def _stop_locked(self) -> None:
        if self._session.value:
            q = _Properties()
            rc = int(_advapi32.ControlTraceW(self._session, None, q.ptr(), EVENT_TRACE_CONTROL_STOP))
            self._stop_rc = rc
            if rc == 0:
                self._final = q.stats()
            elif rc != ERROR_WMI_INSTANCE_NOT_FOUND:
                self._stop_rc = stop_session(self.name)      # the handle failed: fall back to the name
            self._session = TRACEHANDLE(0)
        pump = self._pump
        if self.private:
            self._file_closed.set()                          # the pump now delivers the whole file
            if pump is not None:
                pump.join(PRIVATE_DRAIN_S)
        elif pump is not None:
            pump.join(STOP_JOIN_S)                           # ProcessTrace returns once STOP lands (NE 5.5)
        if pump is not None and pump.is_alive():
            self._close_consumer()                           # 7007: ProcessTrace drains and returns (NE 5.5)
            pump.join(STOP_JOIN_S)
        if pump is None or not pump.is_alive():
            self._close_consumer()
            if self.private:
                self._remove_file()
        if self._atexit_hook is not None:
            atexit.unregister(self._atexit_hook)
            self._atexit_hook = None

    def _open(self, logfile: EVENT_TRACE_LOGFILEW, mode: int) -> None:
        logfile.ProcessTraceMode = mode
        logfile.EventRecordCallback = self._cb
        handle = _advapi32.OpenTraceW(ctypes.byref(logfile))
        if handle == INVALID_PROCESSTRACE_HANDLE:
            err = ctypes.get_last_error()
            self._open_error = err
            raise _error(err, "OpenTraceW")
        self._logfile = logfile
        with self._consumer_lock:
            self._consumer = handle
        header = logfile.LogfileHeader
        self._clock = (int(header.ReservedFlags), int(header.PerfFreq))

    def _close_consumer(self) -> None:
        with self._consumer_lock:
            if self._consumer != INVALID_PROCESSTRACE_HANDLE:
                self._close_rc = int(_advapi32.CloseTrace(self._consumer))
                self._consumer = INVALID_PROCESSTRACE_HANDLE

    def _pump_realtime(self) -> None:
        handles = (TRACEHANDLE * 1)(self._consumer)
        self._process_trace_rc = int(_advapi32.ProcessTrace(handles, 1, None, None))

    def _pump_file(self) -> None:
        self._file_closed.wait()
        try:
            self._open(EVENT_TRACE_LOGFILEW(LogFileName=self._etl_path),
                       PROCESS_TRACE_MODE_EVENT_RECORD | PROCESS_TRACE_MODE_RAW_TIMESTAMP)
        except OSError as exc:
            self._file_note = f"could not open the private session file: {exc}"
            return
        handles = (TRACEHANDLE * 1)(self._consumer)
        self._process_trace_rc = int(_advapi32.ProcessTrace(handles, 1, None, None))

    def _remove_file(self) -> None:
        path = self._etl_path
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as exc:
                self._file_note = f"could not delete {path}: {exc}"

    def _on_record(self, address: int | None) -> None:
        try:
            raw = ctypes.string_at(address, _RECORD.size)
            provider = self._wanted.get(raw[_PROVIDER_SLICE])
            if provider is None:                             # e.g. the trace header event of a file
                self._skipped += 1
                return
            tid, pid, ts, eid, ver, level, opcode, task, keyword, length, data_ptr = _RECORD.unpack(raw)
            data = ctypes.string_at(data_ptr, length) if length and data_ptr else b""
            self._events += 1
            self._on_event(EtwEvent(ts, provider, eid, ver, opcode, level, task, keyword, pid, tid, data))
        except Exception as exc:  # noqa: BLE001 - nothing may escape into ProcessTrace
            self._callback_errors += 1
            if self._first_error is None:
                self._first_error = f"{type(exc).__name__}: {exc}"


class Provider:
    """An ETW provider registered in this process (EventRegister), to write test events to a private session."""

    def __init__(self, guid: str) -> None:
        self.guid = str(uuid.UUID(str(guid)))
        self._guid = GUID.of(self.guid)
        self._handle = REGHANDLE(0)
        rc = _advapi32.EventRegister(ctypes.byref(self._guid), None, None, ctypes.byref(self._handle))
        if rc != 0:
            raise _error(rc, "EventRegister")

    def write(self, event_id: int, payload: bytes, *, version: int = 0, level: int = 4, opcode: int = 0,
              task: int = 0, keyword: int = 0) -> int:
        """EventWrite with one data descriptor; returns its error code (0 = written or nobody listening)."""
        if not self._handle.value:
            raise ValueError("provider is closed")
        if len(payload) > 0xFFFF:
            raise ValueError("an ETW event payload must be under 64 KB")
        desc = EVENT_DESCRIPTOR(Id=event_id, Version=version, Level=level, Opcode=opcode, Task=task, Keyword=keyword)
        buf = ctypes.create_string_buffer(bytes(payload), len(payload))
        data = EVENT_DATA_DESCRIPTOR(Ptr=ctypes.addressof(buf), Size=len(payload))
        return int(_advapi32.EventWrite(self._handle, ctypes.byref(desc), 1, ctypes.byref(data)))

    def close(self) -> None:
        if self._handle.value:
            _advapi32.EventUnregister(self._handle)
            self._handle = REGHANDLE(0)

    def __enter__(self) -> "Provider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
