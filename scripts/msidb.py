"""Read an MSI's tables and ask Windows Installer about a product, through msi.dll (ctypes, standard library only).

scripts/msi_build.py reads the MSI it built back (its properties and where each file goes); scripts/msi_check.py
asks Windows Installer what it registered for the product before, during and after an install. Nothing here changes
anything: the database is opened read-only, and the product queries only read.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import PurePosixPath

_msi = ctypes.WinDLL("msi")

MSIHANDLE = wintypes.DWORD
ERROR_SUCCESS, ERROR_MORE_DATA, ERROR_NO_MORE_ITEMS = 0, 234, 259
ERROR_UNKNOWN_PRODUCT, ERROR_UNKNOWN_PROPERTY = 1605, 1608
# INSTALLSTATE (msi.h)
INSTALLSTATE_MOREDATA, INSTALLSTATE_UNKNOWN, INSTALLSTATE_ABSENT, INSTALLSTATE_LOCAL = -3, -1, 2, 3
INSTALLSTATE_DEFAULT = 5
GUID_CHARS = 39                                   # "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" and its null

_msi.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(MSIHANDLE)]
_msi.MsiOpenDatabaseW.restype = wintypes.UINT
_msi.MsiDatabaseOpenViewW.argtypes = [MSIHANDLE, wintypes.LPCWSTR, ctypes.POINTER(MSIHANDLE)]
_msi.MsiDatabaseOpenViewW.restype = wintypes.UINT
_msi.MsiViewExecute.argtypes = [MSIHANDLE, MSIHANDLE]
_msi.MsiViewExecute.restype = wintypes.UINT
_msi.MsiViewFetch.argtypes = [MSIHANDLE, ctypes.POINTER(MSIHANDLE)]
_msi.MsiViewFetch.restype = wintypes.UINT
_msi.MsiRecordGetFieldCount.argtypes = [MSIHANDLE]
_msi.MsiRecordGetFieldCount.restype = wintypes.UINT
_msi.MsiRecordIsNull.argtypes = [MSIHANDLE, wintypes.UINT]
_msi.MsiRecordIsNull.restype = wintypes.BOOL
_msi.MsiRecordGetStringW.argtypes = [MSIHANDLE, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_msi.MsiRecordGetStringW.restype = wintypes.UINT
_msi.MsiCloseHandle.argtypes = [MSIHANDLE]
_msi.MsiCloseHandle.restype = wintypes.UINT
_msi.MsiQueryProductStateW.argtypes = [wintypes.LPCWSTR]
_msi.MsiQueryProductStateW.restype = ctypes.c_int
_msi.MsiGetComponentPathW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR,
                                      ctypes.POINTER(wintypes.DWORD)]
_msi.MsiGetComponentPathW.restype = ctypes.c_int
_msi.MsiEnumRelatedProductsW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR]
_msi.MsiEnumRelatedProductsW.restype = wintypes.UINT
_msi.MsiGetProductInfoW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR,
                                    ctypes.POINTER(wintypes.DWORD)]
_msi.MsiGetProductInfoW.restype = wintypes.UINT
_msi.MsiGetSummaryInformationW.argtypes = [MSIHANDLE, wintypes.LPCWSTR, wintypes.UINT, ctypes.POINTER(MSIHANDLE)]
_msi.MsiGetSummaryInformationW.restype = wintypes.UINT
_msi.MsiSummaryInfoGetPropertyW.argtypes = [MSIHANDLE, wintypes.UINT, ctypes.POINTER(wintypes.UINT),
                                            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(wintypes.FILETIME),
                                            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_msi.MsiSummaryInfoGetPropertyW.restype = wintypes.UINT
# Summary information property ids (msidefs.h) and the one data type read here
SUMMARY_IDS = {"title": 2, "subject": 3, "author": 4, "keywords": 5, "comments": 6, "template": 7}
VT_LPSTR = 30


class MsiError(OSError):
    pass


def _check(rc: int, what: str) -> None:
    if rc != ERROR_SUCCESS:
        raise MsiError(f"{what} failed with Windows Installer error {rc}")


def _field(record: int, index: int) -> str | None:
    if _msi.MsiRecordIsNull(record, index):
        return None
    size = wintypes.DWORD(256)
    while True:
        buf = ctypes.create_unicode_buffer(size.value + 1)
        size.value = len(buf)
        rc = _msi.MsiRecordGetStringW(record, index, buf, ctypes.byref(size))
        if rc == ERROR_MORE_DATA:              # size now holds the length needed, without the null
            size.value += 1
            continue
        _check(rc, "MsiRecordGetStringW")
        return buf.value


def query(msi_path: str, sql: str) -> list[tuple[str | None, ...]]:
    """Every row a SELECT returns from the MSI at msi_path, each field as text (None for a null)."""
    db, view = MSIHANDLE(), MSIHANDLE()
    _check(_msi.MsiOpenDatabaseW(str(msi_path), None, ctypes.byref(db)), f"opening {msi_path}")   # None: read-only
    try:
        _check(_msi.MsiDatabaseOpenViewW(db, sql, ctypes.byref(view)), f"the query {sql!r}")
        try:
            _check(_msi.MsiViewExecute(view, 0), f"running {sql!r}")
            rows = []
            while True:
                record = MSIHANDLE()
                rc = _msi.MsiViewFetch(view, ctypes.byref(record))
                if rc == ERROR_NO_MORE_ITEMS:
                    return rows
                _check(rc, "MsiViewFetch")
                try:
                    n = _msi.MsiRecordGetFieldCount(record)
                    rows.append(tuple(_field(record.value, i) for i in range(1, n + 1)))
                finally:
                    _msi.MsiCloseHandle(record)
        finally:
            _msi.MsiCloseHandle(view)
    finally:
        _msi.MsiCloseHandle(db)


def summary(msi_path: str) -> dict[str, str | None]:
    """The MSI's summary information strings (title, subject, author, keywords, comments, template)."""
    info = MSIHANDLE()
    _check(_msi.MsiGetSummaryInformationW(0, str(msi_path), 0, ctypes.byref(info)), f"reading {msi_path}'s summary")
    try:
        out = {}
        for name, pid in SUMMARY_IDS.items():
            kind, number, when = wintypes.UINT(), ctypes.c_int(), wintypes.FILETIME()
            size = wintypes.DWORD(0)
            buf = ctypes.create_unicode_buffer(1)
            rc = _msi.MsiSummaryInfoGetPropertyW(info, pid, ctypes.byref(kind), ctypes.byref(number),
                                                 ctypes.byref(when), buf, ctypes.byref(size))
            if rc == ERROR_MORE_DATA:
                buf = ctypes.create_unicode_buffer(size.value + 1)
                size.value = len(buf)
                rc = _msi.MsiSummaryInfoGetPropertyW(info, pid, ctypes.byref(kind), ctypes.byref(number),
                                                     ctypes.byref(when), buf, ctypes.byref(size))
            _check(rc, f"MsiSummaryInfoGetPropertyW({name})")
            out[name] = buf.value if kind.value == VT_LPSTR else None
        return out
    finally:
        _msi.MsiCloseHandle(info)


def tables(msi_path: str) -> set[str]:
    return {r[0] for r in query(msi_path, "SELECT `Name` FROM `_Tables`") if r[0]}


def properties(msi_path: str) -> dict[str, str | None]:
    return {r[0]: r[1] for r in query(msi_path, "SELECT `Property`, `Value` FROM `Property`") if r[0]}


def rows(msi_path: str, table: str, columns: tuple[str, ...]) -> list[dict[str, str | None]]:
    """The table's rows as dicts; [] when the MSI has no such table (WiX leaves out empty ones)."""
    if table not in tables(msi_path):
        return []
    cols = ", ".join(f"`{c}`" for c in columns)
    return [dict(zip(columns, r)) for r in query(msi_path, f"SELECT {cols} FROM `{table}`")]


def long_name(value: str | None) -> str:
    """The long name in a Directory or File name column: "SHORT~1|long name" -> "long name"."""
    text = value or ""
    return text.split("|", 1)[1] if "|" in text else text


def target_dir_name(default_dir: str | None) -> str:
    """A Directory row's DefaultDir as the installed folder name: "target:source" names, each maybe short|long."""
    return long_name((default_dir or "").split(":", 1)[0])


def relative_paths(directories: list[dict], components: list[dict], files: list[dict],
                   root: str) -> dict[str, str]:
    """Pure: each File row's path under the Directory `root` (e.g. INSTALLFOLDER), "a/b/c.txt", keyed by its File
    key. A file outside `root` raises ValueError: nothing this MSI installs may land anywhere else."""
    parent = {d["Directory"]: (d["Directory_Parent"], target_dir_name(d["DefaultDir"])) for d in directories}
    comp_dir = {c["Component"]: c["Directory_"] for c in components}
    out = {}
    for f in files:
        parts, d, seen = [long_name(f["FileName"])], comp_dir.get(f["Component_"]), set()
        while d != root:
            if d is None or d in seen or d not in parent:
                raise ValueError(f"the file {f['File']} ({parts[-1]}) is not installed under {root}")
            seen.add(d)
            up, name = parent[d]
            if name not in (".", ""):
                parts.append(name)
            d = up
        out[f["File"]] = str(PurePosixPath(*reversed(parts)))
    return out


def install_layout(msi_path: str, root: str = "INSTALLFOLDER") -> dict[str, str]:
    """Where every file in the MSI goes, relative to the folder `root`, keyed by its File key."""
    return relative_paths(rows(msi_path, "Directory", ("Directory", "Directory_Parent", "DefaultDir")),
                          rows(msi_path, "Component", ("Component", "Directory_")),
                          rows(msi_path, "File", ("File", "Component_", "FileName")), root)


# ------------------------------------------------------------------ the installed product ---

def product_state(product_code: str) -> int:
    """INSTALLSTATE_DEFAULT (5) when installed for this machine, INSTALLSTATE_UNKNOWN (-1) when not known at all."""
    return _msi.MsiQueryProductStateW(product_code)


def component_path(product_code: str, component_id: str) -> tuple[int, str]:
    """(INSTALLSTATE, path) Windows Installer gives for one component of the product: INSTALLSTATE_LOCAL with the
    key file's path when it is installed."""
    size = wintypes.DWORD(1024)
    while True:
        buf = ctypes.create_unicode_buffer(size.value + 1)
        size.value = len(buf)
        state = _msi.MsiGetComponentPathW(product_code, component_id, buf, ctypes.byref(size))
        if state != INSTALLSTATE_MOREDATA:          # size now holds the length needed, without the null
            return state, buf.value


def related_products(upgrade_code: str) -> list[str]:
    """Product codes Windows Installer has registered under an upgrade code."""
    out, i = [], 0
    while True:
        buf = ctypes.create_unicode_buffer(GUID_CHARS)
        rc = _msi.MsiEnumRelatedProductsW(upgrade_code, 0, i, buf)
        if rc == ERROR_NO_MORE_ITEMS:
            return out
        _check(rc, "MsiEnumRelatedProductsW")
        out.append(buf.value)
        i += 1


def product_info(product_code: str, prop: str) -> str | None:
    """One registered property of an installed product (InstallLocation, VersionString...); None if unknown."""
    size = wintypes.DWORD(0)
    rc = _msi.MsiGetProductInfoW(product_code, prop, None, ctypes.byref(size))
    if rc in (ERROR_UNKNOWN_PRODUCT, ERROR_UNKNOWN_PROPERTY):
        return None
    if rc not in (ERROR_SUCCESS, ERROR_MORE_DATA):
        _check(rc, f"MsiGetProductInfoW({prop})")
    buf = ctypes.create_unicode_buffer(size.value + 1)
    size.value = len(buf)
    rc = _msi.MsiGetProductInfoW(product_code, prop, buf, ctypes.byref(size))
    if rc in (ERROR_UNKNOWN_PRODUCT, ERROR_UNKNOWN_PROPERTY):
        return None
    _check(rc, f"MsiGetProductInfoW({prop})")
    return buf.value
