"""Performance counters (PDH) through ctypes: one query, English counter paths, formatted arrays.

The recipe, constants and struct layouts are from docs/research/windows-telemetry.md (WT) section 1.1, with
the rules of 1.3 and verification log rows 4-14:

- counters are added with PdhAddEnglishCounterW, so paths work on any display language;
- values are formatted with PDH_FMT_DOUBLE | PDH_FMT_NOCAP100: without NOCAP100 PDH cuts values to 100,
  and GPU Engine samples reach 3,652 % (verify log 13), which callers must average before clamping;
- every array read probes the size with a NULL buffer first (Microsoft: do not trust the size returned
  for a non-zero buffer that was too small), then formats into a buffer this query keeps and reuses;
- a rate counter fails the whole array call until the query has been collected twice
  (PDH_CSTATUS_INVALID_DATA for "(*)" paths, PDH_INVALID_DATA for explicit instances), and later calls
  can still fail whole (PDH_CALC_NEGATIVE_DENOMINATOR) or per item. A failed item reads as None and a
  failed whole call as {}; nothing is ever turned into 0.
"""
from __future__ import annotations

import ctypes
import struct
import threading
import weakref
from ctypes import wintypes

# Constants: WT 1.1 / verify log 4-5 (SDK um/pdh.h, um/pdhmsg.h).
PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x00000000
PDH_CSTATUS_NEW_DATA = 0x00000001        # pdhmsg.h line 23: also a valid value (data changed since last read)
PDH_CSTATUS_NO_INSTANCE = 0x800007D1
PDH_NO_DATA = 0x800007D5
PDH_CALC_NEGATIVE_DENOMINATOR = 0x800007D6
PDH_CSTATUS_NO_OBJECT = 0xC0000BB8       # the counter set does not exist (fails at add time)
PDH_CSTATUS_NO_COUNTER = 0xC0000BB9      # the set exists, the counter does not
PDH_CSTATUS_INVALID_DATA = 0xC0000BBA    # e.g. a "(*)" rate counter read after one collect
PDH_INVALID_HANDLE = 0xC0000BBC
PDH_INVALID_DATA = 0xC0000BC6            # e.g. an explicit-instance rate counter read after one collect

_FMT = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
_VALID_ITEM = (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA)
_MAX_BUFFER = 16 << 20                   # GPU Engine needs about 73 KB here (WT 1.3); refuse anything absurd
_MAX_ATTEMPTS = 4

_STATUS_NAMES = {
    PDH_MORE_DATA: "PDH_MORE_DATA", PDH_CSTATUS_NO_INSTANCE: "PDH_CSTATUS_NO_INSTANCE",
    PDH_NO_DATA: "PDH_NO_DATA", PDH_CALC_NEGATIVE_DENOMINATOR: "PDH_CALC_NEGATIVE_DENOMINATOR",
    PDH_CSTATUS_NO_OBJECT: "PDH_CSTATUS_NO_OBJECT", PDH_CSTATUS_NO_COUNTER: "PDH_CSTATUS_NO_COUNTER",
    PDH_CSTATUS_INVALID_DATA: "PDH_CSTATUS_INVALID_DATA", PDH_INVALID_HANDLE: "PDH_INVALID_HANDLE",
    PDH_INVALID_DATA: "PDH_INVALID_DATA",
}


class _Value(ctypes.Union):
    _fields_ = [("longValue", ctypes.c_long), ("doubleValue", ctypes.c_double), ("largeValue", ctypes.c_longlong),
                ("AnsiStringValue", ctypes.c_char_p), ("WideStringValue", ctypes.c_wchar_p)]


class PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("CStatus", wintypes.DWORD), ("u", _Value)]


class PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", PDH_FMT_COUNTERVALUE)]


# WT 1.1 / verify log 6: 16 and 24 bytes on x64. The item reader below unpacks the same layout with struct.
assert ctypes.sizeof(PDH_FMT_COUNTERVALUE) == 16
assert ctypes.sizeof(PDH_FMT_COUNTERVALUE_ITEM_W) == 24
_ITEM = struct.Struct("<QI4xd")          # szName pointer, CStatus, padding, doubleValue
assert _ITEM.size == ctypes.sizeof(PDH_FMT_COUNTERVALUE_ITEM_W)
assert PDH_FMT_COUNTERVALUE_ITEM_W.FmtValue.offset == 8 and PDH_FMT_COUNTERVALUE.u.offset == 8

_pdh = ctypes.WinDLL("pdh.dll")
_PDH_HANDLE = wintypes.HANDLE
_pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(_PDH_HANDLE)]
_pdh.PdhAddEnglishCounterW.argtypes = [_PDH_HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(_PDH_HANDLE)]
_pdh.PdhCollectQueryData.argtypes = [_PDH_HANDLE]
_pdh.PdhGetFormattedCounterArrayW.argtypes = [_PDH_HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                              ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_pdh.PdhCloseQuery.argtypes = [_PDH_HANDLE]
for _name in ("PdhOpenQueryW", "PdhAddEnglishCounterW", "PdhCollectQueryData", "PdhGetFormattedCounterArrayW",
              "PdhCloseQuery"):
    getattr(_pdh, _name).restype = ctypes.c_long        # PDH_STATUS is a LONG (WT verify log 4)


def status_name(status: int) -> str:
    return _STATUS_NAMES.get(status, f"0x{status:08X}")


class PdhError(OSError):
    """A failed PDH call. `.winerror` holds the PDH status (e.g. 0xC0000BB8)."""


def _error(call: str, status: int) -> PdhError:
    err = PdhError(0, f"{call} failed: {status_name(status)} (0x{status:08X})")
    err.winerror = status     # set after construction: the constructor squeezes it into a 32-bit C long
    return err


def _close_query(handle: int) -> None:
    _pdh.PdhCloseQuery(handle)


class PdhQuery:
    """One PDH query holding several counters, collected together.

    `paths` are English counter paths; "(*)" wildcards are fine (prefer them: an explicit instance that does
    not exist yet is accepted silently, WT 1.3). A path that cannot be added is recorded in `add_errors`
    (path -> PDH status) and reads as {}; if no path at all can be added, the constructor raises PdhError.
    """

    def __init__(self, paths: list[str]) -> None:
        if isinstance(paths, str) or not paths:
            raise ValueError("PdhQuery needs a non-empty list of counter paths")
        self._lock = threading.Lock()
        self._counters: dict[str, int | None] = {}
        self.add_errors: dict[str, int] = {}
        self.last_status: dict[str, int] = {}
        hq = _PDH_HANDLE()
        status = _pdh.PdhOpenQueryW(None, 0, ctypes.byref(hq)) & 0xFFFFFFFF
        if status != 0:
            raise _error("PdhOpenQueryW", status)
        self._hq: int | None = hq.value
        self._finalizer = weakref.finalize(self, _close_query, hq.value)
        for path in paths:
            if not isinstance(path, str) or not path.startswith("\\"):
                self.close()
                raise ValueError(f"not a counter path: {path!r}")
            if path in self._counters:
                continue
            hc = _PDH_HANDLE()
            status = _pdh.PdhAddEnglishCounterW(hq, path, 0, ctypes.byref(hc)) & 0xFFFFFFFF
            if status != 0:
                self.add_errors[path] = status
                self._counters[path] = None
            else:
                self._counters[path] = hc.value
        if all(h is None for h in self._counters.values()):
            first = next(iter(self.add_errors.items()))
            self.close()
            raise _error(f"PdhAddEnglishCounterW({first[0]!r}) (no counter could be added)", first[1])
        self._collected_ok = False
        self._buf = (ctypes.c_ulonglong * 1024)()        # 8 KiB to start; grown on demand, then reused

    @property
    def paths(self) -> list[str]:
        return list(self._counters)

    def collect(self) -> None:
        """PdhCollectQueryData. Rate counters need two collects; until then their reads are {}.

        A failed collect raises PdhError, and every read returns {} until the next successful collect,
        so values formatted from an older collect are never handed out as fresh.
        """
        with self._lock:
            if self._hq is None:
                raise ValueError("PdhQuery is closed")
            status = _pdh.PdhCollectQueryData(self._hq) & 0xFFFFFFFF
            self._collected_ok = status == 0
            if status != 0:
                raise _error("PdhCollectQueryData", status)

    def read(self, path: str) -> dict[str, float | None]:
        """instance name -> value from the last collect. A failed item is None; a failed whole call is {}.

        Counters without instances (e.g. \\Memory\\Available MBytes) come back under the name "".
        A repeated instance name gets "#1", "#2", ... appended so no value is dropped.
        """
        if path not in self._counters:
            raise KeyError(f"{path!r} was not added to this query")
        with self._lock:
            if self._hq is None:
                raise ValueError("PdhQuery is closed")
            hc = self._counters[path]
            if hc is None:
                return {}
            if not self._collected_ok:
                self.last_status[path] = PDH_NO_DATA
                return {}
            status, items = self._read_array(hc)
            self.last_status[path] = status
            return items if status == 0 else {}

    def _read_array(self, hc: int) -> tuple[int, dict[str, float | None]]:
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        for _ in range(_MAX_ATTEMPTS):
            # Always probe from size 0 (WT 1.3: a too-small non-zero size returns an unreliable size).
            size.value, count.value = 0, 0
            status = _pdh.PdhGetFormattedCounterArrayW(hc, _FMT, ctypes.byref(size), ctypes.byref(count),
                                                       None) & 0xFFFFFFFF
            if status != PDH_MORE_DATA:
                # Status 0 here would mean "no items and no bytes needed": an empty array is not a reading.
                return (status if status != 0 else PDH_NO_DATA), {}
            need = size.value
            if need <= 0 or need > _MAX_BUFFER:
                return PDH_NO_DATA, {}
            if need > ctypes.sizeof(self._buf):
                self._buf = (ctypes.c_ulonglong * ((need + 7) // 8))()
            size.value = ctypes.sizeof(self._buf)
            status = _pdh.PdhGetFormattedCounterArrayW(hc, _FMT, ctypes.byref(size), ctypes.byref(count),
                                                       self._buf) & 0xFFFFFFFF
            if status == PDH_MORE_DATA:
                continue          # another collect landed in between: probe again
            if status != 0:
                return status, {}
            return 0, self._parse(count.value, min(size.value, ctypes.sizeof(self._buf)))
        return PDH_MORE_DATA, {}

    def _parse(self, count: int, used: int) -> dict[str, float | None]:
        buf = self._buf
        base = ctypes.addressof(buf)
        names_at = count * _ITEM.size
        if count <= 0 or names_at > used:
            return {}
        # The item names live in our own buffer, after the items (WT 1.3). Decode that region once, and
        # take each name from it by offset, so no pointer is ever followed outside the buffer.
        raw = memoryview(buf).cast("B")
        text = raw[names_at:used].tobytes().decode("utf-16-le", errors="replace")
        out: dict[str, float | None] = {}
        for i in range(count):
            name_ptr, cstatus, value = _ITEM.unpack_from(raw, i * _ITEM.size)
            rel = name_ptr - base - names_at
            if name_ptr == 0 or rel < 0 or rel % 2 or rel >= used - names_at:
                continue                      # a name outside our buffer: drop the item, never dereference
            start = rel // 2
            end = text.find("\0", start)
            name = text[start:end if end >= 0 else len(text)]
            if name in out:
                n = 1
                while f"{name}#{n}" in out:
                    n += 1
                name = f"{name}#{n}"
            out[name] = float(value) if cstatus in _VALID_ITEM else None
        return out

    def close(self) -> None:
        """PdhCloseQuery. Idempotent."""
        with self._lock:
            if self._hq is None:
                return
            self._hq = None
            self._finalizer()

    def __enter__(self) -> "PdhQuery":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
