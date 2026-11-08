"""Every process's PID, CPU, threads, memory, I/O, parent and image path, without opening any process.

NtQuerySystemInformation(SystemProcessInformation = 5) fills one buffer with an entry per process, each
followed by its threads; SystemProcessIdInformation (88) returns one process's image path. Neither needs a
handle to the process (anti-cheat cannot object), and both work on protected processes.

Layouts are the x64 ones from docs/research/windows-telemetry.md (WT) section 3.1 (phnt ntexapi.h @53fbbdc),
checked field by field in its verification log row 29; the per-process I/O counters are the IO_COUNTERS
block the SDK calls Reserved7, at offset 208 (docs/research/lag-causes.md (LC) section 1.3 and L24). Microsoft
calls these structures "subject to change", so `self_check()` compares our own process against
GetProcessTimes / GetProcessIoCounters before anything trusts the layout.
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable

SystemProcessInformation = 5            # WT 3.1, verify log 28 (SDK winternl.h line 376)
SystemProcessIdInformation = 88         # WT 9.4, verify log 93 (phnt SYSTEM_INFORMATION_CLASS)
STATUS_SUCCESS = 0x00000000
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004  # WT 3.1, verify log 28 (SDK ntstatus.h line 2021)
STATUS_INVALID_CID = 0xC000000B         # class 88 for a PID that is not running (seen here; SDK ntstatus.h)

_MIB = 1 << 20
_INITIAL_BUFFER = 1 * _MIB              # about 580 KB needed here for ~300 processes (WT 3.3, 3.6)
_MAX_BUFFER = 64 * _MIB
_HEADROOM = 64 * 1024                   # the process list can grow between the failed call and the retry
_PATH_START_BYTES = 1024                # class 88 asks for more when a path is longer (WT 9.4)


# ---------------------------------------------------------------- layouts ---

class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class SYSTEM_PROCESS_INFORMATION(ctypes.Structure):
    """WT 3.1 table (phnt names; the SDK hides most of them as Reserved*)."""
    _fields_ = [
        ("NextEntryOffset", wintypes.ULONG), ("NumberOfThreads", wintypes.ULONG),
        ("WorkingSetPrivateSize", ctypes.c_ulonglong), ("HardFaultCount", wintypes.ULONG),
        ("NumberOfThreadsHighWatermark", wintypes.ULONG), ("CycleTime", ctypes.c_ulonglong),
        ("CreateTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong), ("KernelTime", ctypes.c_longlong),
        ("ImageName", UNICODE_STRING), ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p), ("InheritedFromUniqueProcessId", ctypes.c_void_p),
        ("HandleCount", wintypes.ULONG), ("SessionId", wintypes.ULONG), ("UniqueProcessKey", ctypes.c_size_t),
        ("PeakVirtualSize", ctypes.c_size_t), ("VirtualSize", ctypes.c_size_t), ("PageFaultCount", wintypes.ULONG),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivatePageCount", ctypes.c_size_t),
        # IO_COUNTERS order (LC 1.3: Reserved7[6] at offset 208)
        ("ReadOperationCount", ctypes.c_longlong), ("WriteOperationCount", ctypes.c_longlong),
        ("OtherOperationCount", ctypes.c_longlong), ("ReadTransferCount", ctypes.c_longlong),
        ("WriteTransferCount", ctypes.c_longlong), ("OtherTransferCount", ctypes.c_longlong),
    ]


class CLIENT_ID(ctypes.Structure):
    _fields_ = [("UniqueProcess", ctypes.c_void_p), ("UniqueThread", ctypes.c_void_p)]


class SYSTEM_THREAD_INFORMATION(ctypes.Structure):
    """WT 3.1: KernelTime@0, UserTime@8, CreateTime@0x10, ..., ClientId@0x28, Priority@0x38, ThreadState@0x44."""
    _fields_ = [
        ("KernelTime", ctypes.c_longlong), ("UserTime", ctypes.c_longlong), ("CreateTime", ctypes.c_longlong),
        ("WaitTime", wintypes.ULONG), ("StartAddress", ctypes.c_void_p), ("ClientId", CLIENT_ID),
        ("Priority", ctypes.c_long), ("BasePriority", ctypes.c_long), ("ContextSwitches", wintypes.ULONG),
        ("ThreadState", wintypes.ULONG), ("WaitReason", wintypes.ULONG),
    ]


class SYSTEM_PROCESS_ID_INFORMATION(ctypes.Structure):
    _fields_ = [("ProcessId", ctypes.c_void_p), ("ImageName", UNICODE_STRING)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                  "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


# Sizes and offsets from WT 3.1 / verify log 29, LC 1.3 / L24, WT 9.4 (24-byte class 88 struct).
assert ctypes.sizeof(UNICODE_STRING) == 16
assert ctypes.sizeof(SYSTEM_PROCESS_INFORMATION) == 0x100
assert SYSTEM_PROCESS_INFORMATION.CreateTime.offset == 0x20
assert SYSTEM_PROCESS_INFORMATION.ImageName.offset == 0x38
assert SYSTEM_PROCESS_INFORMATION.UniqueProcessId.offset == 80
assert SYSTEM_PROCESS_INFORMATION.InheritedFromUniqueProcessId.offset == 0x58
assert SYSTEM_PROCESS_INFORMATION.SessionId.offset == 0x64
assert SYSTEM_PROCESS_INFORMATION.PageFaultCount.offset == 0x80
assert SYSTEM_PROCESS_INFORMATION.WorkingSetSize.offset == 0x90
assert SYSTEM_PROCESS_INFORMATION.PrivatePageCount.offset == 0xC8
assert SYSTEM_PROCESS_INFORMATION.ReadOperationCount.offset == 208
assert SYSTEM_PROCESS_INFORMATION.OtherTransferCount.offset == 0xF8
assert ctypes.sizeof(SYSTEM_THREAD_INFORMATION) == 0x50
assert SYSTEM_THREAD_INFORMATION.CreateTime.offset == 0x10
assert SYSTEM_THREAD_INFORMATION.ClientId.offset + CLIENT_ID.UniqueThread.offset == 48
assert SYSTEM_THREAD_INFORMATION.Priority.offset == 0x38
assert SYSTEM_THREAD_INFORMATION.ThreadState.offset == 0x44
assert ctypes.sizeof(SYSTEM_PROCESS_ID_INFORMATION) == 24
assert ctypes.sizeof(IO_COUNTERS) == 48
assert ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX) == 80


_CODES = {ctypes.c_ulong: "I", ctypes.c_long: "i", ctypes.c_ushort: "H", ctypes.c_ulonglong: "Q",
          ctypes.c_longlong: "q", ctypes.c_void_p: "Q", ctypes.c_size_t: "Q"}


def _flat(structure: type[ctypes.Structure], prefix: str = "", base: int = 0) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for name, ctype in structure._fields_:
        off = base + getattr(structure, name).offset
        if isinstance(ctype, type) and issubclass(ctype, ctypes.Structure):
            out += _flat(ctype, f"{prefix}{name}.", off)
        else:
            out.append((off, f"{prefix}{name}", _CODES[ctype]))
    return out


def _struct_for(structure: type[ctypes.Structure], keep: set[str] | None = None) -> tuple[struct.Struct, dict[str, int]]:
    """A little-endian struct.Struct that unpacks the fields of `structure` named in `keep` (all by default),
    with every other byte as padding, and the index of each kept field in the unpacked tuple.

    Built from the ctypes declaration above, so the fast parser can never disagree with the asserted layout.
    """
    fmt, pos, index = "<", 0, {}
    for off, name, code in _flat(structure):
        if keep is not None and name not in keep:
            continue
        if off > pos:
            fmt += f"{off - pos}x"
        fmt += code
        pos = off + struct.calcsize("<" + code)
        index[name] = len(index)
    size = ctypes.sizeof(structure)
    if size > pos:
        fmt += f"{size - pos}x"
    st = struct.Struct(fmt)
    assert st.size == size, (structure.__name__, st.size, size)
    if keep is not None:
        assert set(index) == keep, keep - set(index)
    return st, index


# The parser unpacks only what ProcInfo / ThreadInfo carry (unpacking all 40 fields cost about 0.7 ms a tick).
_SPI, _P = _struct_for(SYSTEM_PROCESS_INFORMATION, {
    "NextEntryOffset", "NumberOfThreads", "CreateTime", "UserTime", "KernelTime", "ImageName.Length",
    "ImageName.Buffer", "UniqueProcessId", "InheritedFromUniqueProcessId", "SessionId", "PageFaultCount",
    "WorkingSetSize", "PrivatePageCount", "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"})
_STI, _T = _struct_for(SYSTEM_THREAD_INFORMATION, {
    "KernelTime", "UserTime", "CreateTime", "ClientId.UniqueThread", "Priority", "ThreadState"})
SPI_SIZE, STI_SIZE = _SPI.size, _STI.size


# ---------------------------------------------------------------- API ---

_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                            ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long          # NTSTATUS
_ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
_ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE                 # pseudo-handle: nothing is opened
_k32.GetCurrentThread.argtypes = []
_k32.GetCurrentThread.restype = wintypes.HANDLE
_k32.GetCurrentThreadId.argtypes = []
_k32.GetCurrentThreadId.restype = wintypes.DWORD
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
_k32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetThreadTimes.restype = wintypes.BOOL
_k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_COUNTERS)]
_k32.GetProcessIoCounters.restype = wintypes.BOOL
_k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                                         wintypes.DWORD]
_k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
_k32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.ProcessIdToSessionId.restype = wintypes.BOOL
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
_k32.GetLogicalDriveStringsW.restype = wintypes.DWORD
_k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
_k32.QueryDosDeviceW.restype = wintypes.DWORD


def _nt_error(call: str, status: int) -> OSError:
    dos = _ntdll.RtlNtStatusToDosError(ctypes.c_long(status - (1 << 32) if status & 0x80000000 else status).value)
    err = OSError(0, f"{call} failed: NTSTATUS 0x{status:08X}")
    err.winerror = int(dos)
    err.ntstatus = status
    return err


def _layout_error(msg: str) -> OSError:
    err = OSError(0, f"NtQuerySystemInformation returned data that does not fit the expected layout: {msg}")
    err.winerror = 13                     # ERROR_INVALID_DATA
    return err


def _check_pid(pid: int) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 <= pid <= 0xFFFFFFFF:
        raise ValueError(f"not a process id: {pid!r}")
    return pid


@dataclass(slots=True)
class ThreadInfo:
    tid: int
    create_time: int        # FILETIME, 100 ns since 1601
    user_100ns: int
    kernel_100ns: int
    priority: int
    state: int              # KTHREAD_STATE: 2 running, 5 waiting, ... (WT 3.1)


@dataclass(slots=True)
class ProcInfo:
    pid: int
    ppid: int               # the PID that created it; that PID may since have been reused (check create_time)
    create_time: int        # FILETIME, 100 ns since 1601: with pid, the process's identity
    name: str               # image name, e.g. "python.exe"; "" for pid 0
    session_id: int
    user_100ns: int
    kernel_100ns: int
    ws_bytes: int
    private_bytes: int
    page_faults: int        # soft + hard, cumulative
    read_bytes: int         # cumulative I/O (files, network, devices), like GetProcessIoCounters
    write_bytes: int
    other_bytes: int
    read_ops: int
    write_ops: int
    other_ops: int
    thread_count: int
    threads: list[ThreadInfo] | None   # only for the pids asked for


class Snapshotter:
    """One reusable buffer for NtQuerySystemInformation(5), grown on STATUS_INFO_LENGTH_MISMATCH (WT 3.3)."""

    def __init__(self, initial_size: int = _INITIAL_BUFFER, max_size: int = _MAX_BUFFER) -> None:
        if not 4096 <= initial_size <= max_size:
            raise ValueError("initial_size must be between 4 KiB and max_size")
        self._lock = threading.Lock()
        self._max = max_size
        self._buf = (ctypes.c_ulonglong * ((initial_size + 7) // 8))()   # 8-byte aligned
        self._names: dict[tuple[int, int], str] = {}
        self.grow_count = 0
        self.last_bytes = 0

    @property
    def buffer_size(self) -> int:
        return ctypes.sizeof(self._buf)

    def snapshot(self, threads_for: Iterable[int] = ()) -> dict[int, ProcInfo]:
        """pid -> ProcInfo for every process; `threads` is filled only for the pids in `threads_for`."""
        want = {_check_pid(p) for p in threads_for}
        with self._lock:
            used = self._query()
            return self._parse(used, want)

    def _query(self) -> int:
        ret = wintypes.ULONG(0)
        for _ in range(8):
            size = ctypes.sizeof(self._buf)
            status = _ntdll.NtQuerySystemInformation(SystemProcessInformation, self._buf, size,
                                                     ctypes.byref(ret)) & 0xFFFFFFFF
            if status == STATUS_SUCCESS:
                if not SPI_SIZE <= ret.value <= size:
                    raise _layout_error(f"it reported {ret.value} bytes for a {size}-byte buffer")
                self.last_bytes = ret.value
                return ret.value
            if status != STATUS_INFO_LENGTH_MISMATCH:
                raise _nt_error("NtQuerySystemInformation(SystemProcessInformation)", status)
            need = ret.value if ret.value > size else size * 2
            new_size = need + max(_HEADROOM, need // 8)
            if new_size > self._max:
                raise _nt_error(f"NtQuerySystemInformation(SystemProcessInformation) needs {need} bytes, "
                                f"over the {self._max}-byte cap;", status)
            self._buf = (ctypes.c_ulonglong * ((new_size + 7) // 8))()
            self.grow_count += 1
        raise _nt_error("NtQuerySystemInformation(SystemProcessInformation) kept growing;",
                        STATUS_INFO_LENGTH_MISMATCH)

    def _parse(self, used: int, want: set[int]) -> dict[int, ProcInfo]:
        raw = memoryview(self._buf).cast("B")[:used]
        base = ctypes.addressof(self._buf)
        names = self._names
        seen: dict[tuple[int, int], str] = {}
        procs: dict[int, ProcInfo] = {}
        unpack, unpack_thread = _SPI.unpack_from, _STI.unpack_from
        (i_next, i_nthreads, i_create, i_user, i_kernel, i_name_len, i_name_ptr, i_pid, i_ppid, i_session,
         i_faults, i_ws, i_private, i_rops, i_wops, i_oops, i_rbytes, i_wbytes, i_obytes) = (_P[k] for k in (
            "NextEntryOffset", "NumberOfThreads", "CreateTime", "UserTime", "KernelTime", "ImageName.Length",
            "ImageName.Buffer", "UniqueProcessId", "InheritedFromUniqueProcessId", "SessionId", "PageFaultCount",
            "WorkingSetSize", "PrivatePageCount", "ReadOperationCount", "WriteOperationCount",
            "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"))
        t_tid, t_create, t_user, t_kernel, t_prio, t_state = (_T[k] for k in (
            "ClientId.UniqueThread", "CreateTime", "UserTime", "KernelTime", "Priority", "ThreadState"))
        off = 0
        for _ in range(used // SPI_SIZE + 1):           # a hard bound on the walk, whatever the offsets say
            if off + SPI_SIZE > used:
                raise _layout_error(f"an entry at {off} runs past the {used} bytes returned")
            f = unpack(raw, off)
            nthreads = f[i_nthreads]
            threads_at = off + SPI_SIZE
            if threads_at + nthreads * STI_SIZE > used:
                raise _layout_error(f"{nthreads} threads at {off} run past the {used} bytes returned")
            pid, create = f[i_pid], f[i_create]
            key = (pid, create)
            name = names.get(key)
            if name is None:
                name = _image_name(raw, base, used, f[i_name_len], f[i_name_ptr])
            seen[key] = name
            threads = None
            if pid in want:
                threads = []
                for i in range(nthreads):
                    t = unpack_thread(raw, threads_at + i * STI_SIZE)
                    threads.append(ThreadInfo(t[t_tid], t[t_create], t[t_user], t[t_kernel], t[t_prio], t[t_state]))
            procs[pid] = ProcInfo(pid, f[i_ppid], create, name, f[i_session], f[i_user], f[i_kernel], f[i_ws],
                                  f[i_private], f[i_faults], f[i_rbytes], f[i_wbytes], f[i_obytes], f[i_rops],
                                  f[i_wops], f[i_oops], nthreads, threads)
            nxt = f[i_next]
            if nxt == 0:
                break
            if nxt < SPI_SIZE + nthreads * STI_SIZE:
                raise _layout_error(f"entry at {off} says the next one starts {nxt} bytes on, inside its threads")
            off += nxt
        else:
            raise _layout_error("the entry chain never ended")
        self._names = seen              # only the processes alive now: the cache cannot grow without bound
        return procs


def _image_name(raw: memoryview, base: int, used: int, length: int, ptr: int) -> str:
    if ptr == 0 or length == 0:
        return ""                       # pid 0, the idle process (WT 3.1)
    rel = ptr - base
    if rel < 0 or length % 2 or rel + length > used:
        raise _layout_error(f"an image name points outside the returned data ({rel}, {length})")
    return raw[rel:rel + length].tobytes().decode("utf-16-le", errors="replace")


def pids(snapshotter: Snapshotter | None = None) -> frozenset[int]:
    """The PID of every running process, 0 (the idle process) included, from one snapshot: the list EnumProcesses
    gives (psutil.pids() calls it), without opening any process. Pass a Snapshotter to reuse its buffer."""
    return frozenset((snapshotter or Snapshotter()).snapshot())


# ---------------------------------------------------------------- image path (class 88) ---

_dos_lock = threading.Lock()
_dos_map: dict[str, str] | None = None


def _drive_devices() -> dict[str, str]:
    """NT device name -> drive letter, e.g. {'\\Device\\HarddiskVolume3': 'C:'}, via QueryDosDeviceW."""
    buf = ctypes.create_unicode_buffer(512)
    n = _k32.GetLogicalDriveStringsW(len(buf), buf)
    if n == 0 or n > len(buf):
        raise ctypes.WinError(ctypes.get_last_error())
    out: dict[str, str] = {}
    target = ctypes.create_unicode_buffer(1024)
    for root in buf[:n].split("\0"):
        if len(root) < 2 or root[1] != ":":
            continue
        drive = root[:2].upper()
        if _k32.QueryDosDeviceW(drive, target, len(target)):
            out[target.value] = drive         # the first string of the multi-string is the current target
    return out


def nt_to_dos(nt_path: str, devices: dict[str, str]) -> str:
    """Turn '\\Device\\HarddiskVolume3\\x.exe' into 'C:\\x.exe'. Unknown forms come back unchanged."""
    if nt_path.startswith("\\??\\"):
        return nt_path[4:]
    low = nt_path.lower()
    if low.startswith("\\systemroot\\"):
        return os.environ.get("SystemRoot", "C:\\Windows") + nt_path[len("\\SystemRoot"):]
    if low.startswith("\\device\\mup\\"):
        return "\\\\" + nt_path[len("\\Device\\Mup\\"):]
    for device, drive in devices.items():
        # The device name must end at a path separator: HarddiskVolume1 is a prefix of HarddiskVolume10.
        if low.startswith(device.lower() + "\\"):
            return drive + nt_path[len(device):]
    return nt_path


def _to_dos(nt_path: str) -> str:
    global _dos_map
    with _dos_lock:
        if _dos_map is None:
            _dos_map = _drive_devices()
        dos = nt_to_dos(nt_path, _dos_map)
        if dos == nt_path and nt_path.lower().startswith("\\device\\"):
            _dos_map = _drive_devices()         # a drive may have appeared since: look once more
            dos = nt_to_dos(nt_path, _dos_map)
        return dos


def image_path(pid: int) -> str | None:
    """The full exe path of any process (protected ones too), or None if it is not running or has none."""
    _check_pid(pid)
    if pid == 0:
        return None
    nbytes = _PATH_START_BYTES
    for _ in range(3):
        buf = ctypes.create_unicode_buffer(nbytes // 2)
        info = SYSTEM_PROCESS_ID_INFORMATION(pid, UNICODE_STRING(0, nbytes, ctypes.cast(buf, ctypes.c_void_p)))
        status = _ntdll.NtQuerySystemInformation(SystemProcessIdInformation, ctypes.byref(info),
                                                 ctypes.sizeof(info), None) & 0xFFFFFFFF
        if status == STATUS_SUCCESS:
            length = info.ImageName.Length
            if length % 2 or length > nbytes:
                raise _layout_error(f"class 88 returned a {length}-byte name for a {nbytes}-byte buffer")
            path = ctypes.wstring_at(buf, length // 2)
            return _to_dos(path) if path else None      # pid 4 (System) has no image path
        if status == STATUS_INVALID_CID:
            return None
        if status != STATUS_INFO_LENGTH_MISMATCH:
            raise _nt_error(f"NtQuerySystemInformation(SystemProcessIdInformation, pid {pid})", status)
        need = info.ImageName.MaximumLength           # bytes needed, terminator included (WT 9.4)
        if need <= nbytes:
            need = nbytes * 2
        nbytes = min(need, 0xFFFE)
    raise _nt_error(f"NtQuerySystemInformation(SystemProcessIdInformation, pid {pid}) kept asking for more;",
                    STATUS_INFO_LENGTH_MISMATCH)


# ---------------------------------------------------------------- self check ---

def _filetime(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _own_times() -> tuple[int, int, int]:
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    if not _k32.GetProcessTimes(_k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k),
                                ctypes.byref(u)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _filetime(c), _filetime(u), _filetime(k)


def _own_thread_times() -> tuple[int, int, int]:
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    if not _k32.GetThreadTimes(_k32.GetCurrentThread(), ctypes.byref(c), ctypes.byref(e), ctypes.byref(k),
                               ctypes.byref(u)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _filetime(c), _filetime(u), _filetime(k)


def _own_io() -> tuple[int, ...]:
    io = IO_COUNTERS()
    if not _k32.GetProcessIoCounters(_k32.GetCurrentProcess(), ctypes.byref(io)):
        raise ctypes.WinError(ctypes.get_last_error())
    return tuple(getattr(io, n) for n, _ in IO_COUNTERS._fields_)


def _own_memory() -> PROCESS_MEMORY_COUNTERS_EX:
    pmc = PROCESS_MEMORY_COUNTERS_EX()
    pmc.cb = ctypes.sizeof(pmc)
    if not _k32.K32GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return pmc


def _own_image_name() -> str:
    buf = ctypes.create_unicode_buffer(32768)
    n = wintypes.DWORD(len(buf))
    if not _k32.QueryFullProcessImageNameW(_k32.GetCurrentProcess(), 0, buf, ctypes.byref(n)):
        raise ctypes.WinError(ctypes.get_last_error())
    return os.path.basename(buf.value)


def _own_session() -> int:
    sid = wintypes.DWORD()
    if not _k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sid.value


def compare_own(before: dict, me: ProcInfo | None, after: dict) -> list[str]:
    """Every field of our own snapshot entry that disagrees with the documented APIs read around it."""
    if me is None:
        return ["our own PID is missing from the snapshot"]
    bad = []

    def bracket(label: str, lo: int, got: int, hi: int) -> None:
        if not lo <= got <= hi:
            bad.append(f"{label} {got} not within [{lo}, {hi}]")

    if me.create_time != before["create"]:
        bad.append(f"CreateTime {me.create_time} != GetProcessTimes {before['create']}")
    bracket("UserTime", before["user"], me.user_100ns, after["user"])
    bracket("KernelTime", before["kernel"], me.kernel_100ns, after["kernel"])
    got_io = (me.read_ops, me.write_ops, me.other_ops, me.read_bytes, me.write_bytes, me.other_bytes)
    for label, lo, got, hi in zip(("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                   "ReadTransferCount", "WriteTransferCount", "OtherTransferCount"),
                                  before["io"], got_io, after["io"]):
        bracket(label, lo, got, hi)
    bracket("PageFaultCount", before["faults"], me.page_faults, after["faults"])
    for label, got, ref in (("WorkingSetSize", me.ws_bytes, after["ws"]),
                            ("PrivatePageCount", me.private_bytes, after["private"])):
        if abs(got - ref) > max(64 * _MIB, ref // 2):       # these move both ways: a loose but telling check
            bad.append(f"{label} {got} far from GetProcessMemoryInfo {ref}")
    if me.name.lower() != before["image"].lower():
        bad.append(f"ImageName {me.name!r} != {before['image']!r}")
    if me.session_id != before["session"]:
        bad.append(f"SessionId {me.session_id} != ProcessIdToSessionId {before['session']}")
    mine = [t for t in (me.threads or []) if t.tid == before["tid"]]
    if not mine:
        bad.append(f"our thread {before['tid']} is missing from the thread list")
    else:
        t = mine[0]
        if t.create_time != before["thread"][0]:
            bad.append(f"thread CreateTime {t.create_time} != GetThreadTimes {before['thread'][0]}")
        bracket("thread UserTime", before["thread"][1], t.user_100ns, after["thread"][1])
        bracket("thread KernelTime", before["thread"][2], t.kernel_100ns, after["thread"][2])
    return bad


def _own_readout() -> dict:
    create, user, kernel = _own_times()
    pmc = _own_memory()
    return {"create": create, "user": user, "kernel": kernel, "io": _own_io(), "faults": pmc.PageFaultCount,
            "ws": pmc.WorkingSetSize, "private": pmc.PrivateUsage, "image": _own_image_name(),
            "session": _own_session(), "tid": _k32.GetCurrentThreadId(), "thread": _own_thread_times()}


def self_check(snapshotter: Snapshotter | None = None) -> None:
    """Compare our own process in a snapshot with GetProcessTimes / GetProcessIoCounters (and friends).

    Raises RuntimeError listing every field that does not match: the undocumented layout drifted.
    """
    me = os.getpid()
    snap = snapshotter or Snapshotter()
    before = _own_readout()
    procs = snap.snapshot(threads_for=(me,))
    after = _own_readout()
    bad = compare_own(before, procs.get(me), after)
    if bad:
        raise RuntimeError("the NtQuerySystemInformation layout does not match this Windows build: "
                           + "; ".join(bad))
