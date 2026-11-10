"""Have Windows Explorer open a link or start a program, so whatever starts is Explorer's child, as from a click.

Play (docs/LAUNCHER-PLAN.md, M6) starts a game only through its store's link, and lag-doctor must not become the
parent of a store client that the link starts: a child inherits its parent's environment and job, and End process
tree on lag-doctor would close it (research note desktop-app.md, section 14). Explorer's own ShellExecute, reached
through the desktop's folder view, runs the link's handler as Explorer's child and hands lag-doctor no handle
(Raymond Chen, The Old New Thing, 2013-11-18; research note section 5). This module only makes that call; which
links may be opened is lagdoctor/stores/launch.py's to decide.

The chain, with the vtable slots and IIDs of the Windows SDK's C headers (ExDisp.h, servprov.h, ShObjIdl_core.h,
ShlDisp.h), which tests/test_launch.py reads back from those headers:
CoCreateInstance(ShellWindows) -> IShellWindows::FindWindowSW(CSIDL_DESKTOP, SWC_DESKTOP, SWFO_NEEDDISPATCH) ->
IServiceProvider::QueryService(SID_STopLevelBrowser, IShellBrowser) -> IShellBrowser::QueryActiveShellView ->
IShellView::GetItemObject(SVGIO_BACKGROUND, IDispatch) -> IShellFolderViewDual::get_Application ->
IShellDispatch2::ShellExecute. ShellWindows is registered only as a local server, so creating it reaches the
running Explorer's desktop; with no Explorer the desktop is not found, and the call is refused.

The call is a cross-process one into Explorer: it blocks while Explorer does not answer, so a window's caller makes
it off its UI thread.
"""
from __future__ import annotations

import ctypes
import uuid
from ctypes import wintypes

_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_oleaut32 = ctypes.WinDLL("oleaut32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

CLSID_SHELL_WINDOWS = "9BA05972-F6A8-11CF-A442-00A0C90A8F39"
SID_TOP_LEVEL_BROWSER = "4C96BE40-915C-11CF-99D3-00AA004AE837"
IIDS = {
    "IDispatch": "00020400-0000-0000-C000-000000000046",
    "IShellWindows": "85CB6900-4D95-11CF-960C-0080C7F4EE85",
    "IServiceProvider": "6D5140C1-7436-11CE-8034-00AA006009FA",
    "IShellBrowser": "000214E2-0000-0000-C000-000000000046",
    "IShellView": "000214E3-0000-0000-C000-000000000046",
    "IShellFolderViewDual": "E7A1AF80-4D96-11CF-960C-0080C7F4EE85",
    "IShellDispatch2": "A4C6892C-3BA9-11D2-9DEA-00C04FB16162",
}
SLOTS = {
    "IShellWindows": {"FindWindowSW": 15},
    "IServiceProvider": {"QueryService": 3},
    "IShellBrowser": {"QueryActiveShellView": 15},
    "IShellView": {"GetItemObject": 15},
    "IShellFolderViewDual": {"get_Application": 7},
    "IShellDispatch2": {"ShellExecute": 31},
}

CSIDL_DESKTOP = 0x0000
SWC_DESKTOP = 0x8
SWFO_NEEDDISPATCH = 0x1
SVGIO_BACKGROUND = 0
CLSCTX_LOCAL_SERVER = 0x4
COINIT_APARTMENTTHREADED = 0x2
S_OK, S_FALSE = 0, 1
RPC_E_CHANGED_MODE = -0x7FFEFEFA            # 0x80010106: the thread is in the other apartment model already
E_POINTER = -0x7FFFBFFD                     # 0x80004003
E_NOINTERFACE = -0x7FFFBFFE                 # 0x80004002
VT_EMPTY, VT_I4, VT_BSTR = 0, 3, 8
SW_HIDE, SW_SHOWNORMAL = 0, 1
MAX_TARGET_CHARS = 2048                     # a store link or a program's path; ShellExecute takes far more


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _VariantValue(ctypes.Union):
    # The largest member is BRECORD's two pointers; DECIMAL overlays the whole VARIANT and adds no size.
    _fields_ = [("llVal", ctypes.c_longlong), ("lVal", ctypes.c_long), ("bstrVal", ctypes.c_void_p),
                ("record", ctypes.c_void_p * 2)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("wReserved1", wintypes.WORD), ("wReserved2", wintypes.WORD),
                ("wReserved3", wintypes.WORD), ("value", _VariantValue)]


assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(VARIANT) == (24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


def _bind(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


_PG = ctypes.POINTER(GUID)
_PP = ctypes.POINTER(ctypes.c_void_p)
_CoInitializeEx = _bind(_ole32, "CoInitializeEx", ctypes.c_long, ctypes.c_void_p, wintypes.DWORD)
_CoUninitialize = _bind(_ole32, "CoUninitialize", None)
_CoCreateInstance = _bind(_ole32, "CoCreateInstance", ctypes.c_long, _PG, ctypes.c_void_p, wintypes.DWORD, _PG, _PP)
_SysAllocString = _bind(_oleaut32, "SysAllocString", ctypes.c_void_p, wintypes.LPCWSTR)
_SysFreeString = _bind(_oleaut32, "SysFreeString", None, ctypes.c_void_p)
_GetWindowThreadProcessId = _bind(_user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND,
                                  ctypes.POINTER(wintypes.DWORD))
_AllowSetForegroundWindow = _bind(_user32, "AllowSetForegroundWindow", wintypes.BOOL, wintypes.DWORD)


def guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


class ComError(OSError):
    def __init__(self, what: str, hr: int) -> None:
        super().__init__(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        self.hr = hr & 0xFFFFFFFF


_PROTOS: dict = {}


def vcall(this: int, slot: int, argtypes: tuple, *args, restype=ctypes.c_long):
    """Call vtable slot `slot` of the COM object at `this`."""
    if not this:
        raise ComError(f"slot {slot} on a null pointer", E_POINTER)
    key = (restype, argtypes)
    proto = _PROTOS.get(key)
    if proto is None:
        proto = _PROTOS[key] = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    vtbl = ctypes.cast(ctypes.c_void_p(this), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return proto(vtbl[slot])(this, *args)


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise ComError(what, hr)


def _release(ptr: int | None) -> None:
    if ptr:
        vcall(ptr, 2, (), restype=ctypes.c_ulong)


def _qi(ptr: int, iface: str, held: list[int]) -> int:
    out = ctypes.c_void_p()
    _check(vcall(ptr, 0, (_PG, _PP), ctypes.byref(guid(IIDS[iface])), ctypes.byref(out)),
           f"QueryInterface({iface})")
    if not out.value:
        raise ComError(f"QueryInterface({iface}) returned nothing", E_NOINTERFACE)
    held.append(out.value)
    return out.value


def _variant_text(text: str, bstrs: list[int]) -> VARIANT:
    v = VARIANT()
    v.vt = VT_BSTR
    b = _SysAllocString(text)
    if not b:
        raise MemoryError("SysAllocString")
    bstrs.append(b)
    v.value.bstrVal = b
    return v


def _out(held: list[int], value: int | None) -> int | None:
    if value:
        held.append(value)
    return value


def _desktop(held: list[int]) -> tuple[int, int]:
    """(Explorer's IShellDispatch2, the desktop window's handle); every interface along the way is kept in `held`
    for the caller to release, as soon as it is got, so a failure later in the chain releases it too."""
    windows = ctypes.c_void_p()
    hr = _CoCreateInstance(ctypes.byref(guid(CLSID_SHELL_WINDOWS)), None, CLSCTX_LOCAL_SERVER,
                           ctypes.byref(guid(IIDS["IShellWindows"])), ctypes.byref(windows))
    _out(held, windows.value)
    _check(hr, "CoCreateInstance(ShellWindows)")
    loc, root = VARIANT(), VARIANT()
    loc.vt, loc.value.lVal = VT_I4, CSIDL_DESKTOP
    root.vt = VT_EMPTY
    hwnd = ctypes.c_long(0)
    disp = ctypes.c_void_p()
    hr = vcall(windows.value, SLOTS["IShellWindows"]["FindWindowSW"],
               (ctypes.POINTER(VARIANT), ctypes.POINTER(VARIANT), ctypes.c_int, ctypes.POINTER(ctypes.c_long),
                ctypes.c_int, _PP),
               ctypes.byref(loc), ctypes.byref(root), SWC_DESKTOP, ctypes.byref(hwnd), SWFO_NEEDDISPATCH,
               ctypes.byref(disp))
    _out(held, disp.value)
    _check(hr, "IShellWindows::FindWindowSW")
    if hr == S_FALSE or not disp.value:
        raise OSError("Explorer's desktop was not found: Explorer may be restarting, or another program is the shell")
    provider = _qi(disp.value, "IServiceProvider", held)
    browser = ctypes.c_void_p()
    hr = vcall(provider, SLOTS["IServiceProvider"]["QueryService"], (_PG, _PG, _PP),
               ctypes.byref(guid(SID_TOP_LEVEL_BROWSER)), ctypes.byref(guid(IIDS["IShellBrowser"])),
               ctypes.byref(browser))
    _out(held, browser.value)
    _check(hr, "IServiceProvider::QueryService(SID_STopLevelBrowser)")
    view = ctypes.c_void_p()
    hr = vcall(browser.value, SLOTS["IShellBrowser"]["QueryActiveShellView"], (_PP,), ctypes.byref(view))
    _out(held, view.value)
    _check(hr, "IShellBrowser::QueryActiveShellView")
    background = ctypes.c_void_p()
    hr = vcall(view.value, SLOTS["IShellView"]["GetItemObject"], (wintypes.UINT, _PG, _PP), SVGIO_BACKGROUND,
               ctypes.byref(guid(IIDS["IDispatch"])), ctypes.byref(background))
    _out(held, background.value)
    _check(hr, "IShellView::GetItemObject")
    folder_view = _qi(background.value, "IShellFolderViewDual", held)
    app = ctypes.c_void_p()
    hr = vcall(folder_view, SLOTS["IShellFolderViewDual"]["get_Application"], (_PP,), ctypes.byref(app))
    _out(held, app.value)
    _check(hr, "IShellFolderViewDual::get_Application")
    return _qi(app.value, "IShellDispatch2", held), hwnd.value & 0xFFFFFFFF


def window_pid(hwnd: int) -> int | None:
    """The pid of the process that owns a window, or None when the window is gone."""
    pid = wintypes.DWORD(0)
    if not _GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return None
    return pid.value or None


def open_through_explorer(target: str, args: str = "", show: int = SW_SHOWNORMAL) -> dict:
    """Have Explorer's desktop open `target` with `args`, as a double-click on it would: the shell runs it inside
    Explorer, so a program that starts is Explorer's child, and this process gets no handle to it.

    Returns {"explorer_pid": the process that owns the desktop window, "foreground_allowed": whether that process
    was lent the right to bring what it opens to the front}. Raises OSError when Explorer's desktop cannot be
    reached (Explorer restarting, another shell) or a call fails, and ValueError for a target or arguments it does
    not take. Which targets are opened is the caller's policy: this only makes the call.
    """
    for name, text in (("target", target), ("args", args)):
        if not isinstance(text, str) or "\0" in text or len(text) > MAX_TARGET_CHARS:
            raise ValueError(f"not a {name} for Explorer to open: {text!r}")
    if not target.strip():
        raise ValueError("no target for Explorer to open")
    if isinstance(show, bool) or not isinstance(show, int) or not 0 <= show <= 11:
        raise ValueError(f"not a show command: {show!r}")
    init = _CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if init not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
        raise ComError("CoInitializeEx", init)
    held: list[int] = []
    bstrs: list[int] = []
    try:
        shell, hwnd = _desktop(held)
        explorer = window_pid(hwnd)
        if explorer is None:
            raise OSError("Explorer's desktop window closed while it was being reached")
        # While the player clicks Play the front of the screen is this window's: lend it to Explorer, so the store's
        # window and the game's can come to the front. It fails when this window is not in front, and is reported.
        allowed = bool(_AllowSetForegroundWindow(explorer))
        file = _SysAllocString(target)
        if not file:
            raise MemoryError("SysAllocString")
        bstrs.append(file)
        shown = VARIANT()
        shown.vt, shown.value.lVal = VT_I4, show
        _check(vcall(shell, SLOTS["IShellDispatch2"]["ShellExecute"],
                     (ctypes.c_void_p, VARIANT, VARIANT, VARIANT, VARIANT),
                     file, _variant_text(args, bstrs), _variant_text("", bstrs), _variant_text("", bstrs), shown),
               "IShellDispatch2::ShellExecute")
        return {"explorer_pid": explorer, "foreground_allowed": allowed}
    finally:
        for b in bstrs:
            _SysFreeString(b)
        for ptr in reversed(held):
            _release(ptr)
        if init in (S_OK, S_FALSE):
            _CoUninitialize()
