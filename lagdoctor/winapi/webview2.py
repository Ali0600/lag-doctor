"""WebView2 from ctypes, through the installed runtime's own export: no loader DLL, no server, no compiled code.

Ported from M0's window spike (scripts/webview_spike.py), which passed the owner's run on 2026-11-08
(docs/research/desktop-app.md, section 11). This module is the thin layer: the COM plumbing, the vtable slots and IIDs,
the runtime's export and the Win32 calls a host window needs. What the window does with them (its settings, which
navigation it allows, which messages it trusts) is policy, in lagdoctor/app/window.py and lagdoctor/app/bridge.py.

Rules every user of this module keeps (the spike measured each):
- Every Handler and EnvOptions stays referenced for as long as WebView2 may call it: an unreferenced ctypes thunk is
  freed and WebView2 then calls freed memory (the spike's drop-refs check ended its process with an access violation).
- Every Handler is built with a guard that can never raise into C: under pythonw an exception there is silent and
  leaves the HRESULT unset.
- WebView2's own environment variables are read by the runtime even without Microsoft's loader: set them with
  set_env (SetEnvironmentVariableW), never os.environ, which removes a variable it is given empty.

Where the numbers come from: the vtable slots, IIDs and enum values are counted from Microsoft.Web.WebView2
1.0.4314.35 (nuget.org, nupkg SHA-256 90778deb1b77e36292828b4b5833c9255c27533f40e0ddd55891ae718944edc4), whose
WebView2.h C vtables agree with its WebView2.idl; tests/test_webview2_idl.py checks every entry below against that IDL.
The export, its arguments and the runtime's registry value: research note section 3 (webview/webview's loader.hh and
Wails' go-webview2, read 2026-11-08).
"""
from __future__ import annotations

import ctypes
import os
import re
import uuid
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Callable

STABLE_CHANNEL = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
CLIENT_STATE = "SOFTWARE\\Microsoft\\EdgeUpdate\\ClientState\\" + STABLE_CHANNEL
EXPORT = "CreateWebViewEnvironmentWithOptionsInternal"
RUNTIME_INSTALLED = 0              # webview/webview: webview2_runtime_type::installed (the Evergreen runtime)

# Only what lag-doctor calls or implements. {interface: {method: slot}}; slots 0-2 are IUnknown's.
SLOTS = {
    "ICoreWebView2Environment": {"CreateCoreWebView2Controller": 3, "get_BrowserVersionString": 5},
    "ICoreWebView2Environment5": {"add_BrowserProcessExited": 12},
    "ICoreWebView2Environment12": {"CreateSharedBuffer": 24},
    "ICoreWebView2Controller": {"put_IsVisible": 4, "get_Bounds": 5, "put_Bounds": 6, "get_ZoomFactor": 7,
                                "MoveFocus": 12, "NotifyParentWindowPositionChanged": 23, "Close": 24,
                                "get_CoreWebView2": 25},
    "ICoreWebView2Controller3": {"put_DefaultBackgroundColor": 27, "get_RasterizationScale": 28},
    "ICoreWebView2": {"get_Settings": 3, "Navigate": 5, "add_NavigationStarting": 7, "add_NavigationCompleted": 15,
                      "add_FrameNavigationStarting": 17, "add_ScriptDialogOpening": 21,
                      "add_PermissionRequested": 23, "add_ProcessFailed": 25, "ExecuteScript": 29,
                      "PostWebMessageAsJson": 32, "add_WebMessageReceived": 34, "get_BrowserProcessId": 37,
                      "add_NewWindowRequested": 44},
    "ICoreWebView2_3": {"TrySuspend": 68, "Resume": 69, "SetVirtualHostNameToFolderMapping": 71},
    "ICoreWebView2_4": {"add_DownloadStarting": 75},
    "ICoreWebView2_17": {"PostSharedBufferToScript": 116},
    "ICoreWebView2_18": {"add_LaunchingExternalUriScheme": 117},
    "ICoreWebView2SharedBuffer": {"get_Size": 3, "get_Buffer": 4, "Close": 7},
    "ICoreWebView2Settings": {"get_IsScriptEnabled": 3, "put_IsScriptEnabled": 4, "get_IsWebMessageEnabled": 5,
                              "put_IsWebMessageEnabled": 6, "get_AreDefaultScriptDialogsEnabled": 7,
                              "put_AreDefaultScriptDialogsEnabled": 8, "get_IsStatusBarEnabled": 9,
                              "put_IsStatusBarEnabled": 10, "get_AreDevToolsEnabled": 11,
                              "put_AreDevToolsEnabled": 12, "get_AreDefaultContextMenusEnabled": 13,
                              "put_AreDefaultContextMenusEnabled": 14, "get_AreHostObjectsAllowed": 15,
                              "put_AreHostObjectsAllowed": 16, "get_IsZoomControlEnabled": 17,
                              "put_IsZoomControlEnabled": 18, "get_IsBuiltInErrorPageEnabled": 19,
                              "put_IsBuiltInErrorPageEnabled": 20},
    "ICoreWebView2Settings3": {"get_AreBrowserAcceleratorKeysEnabled": 23, "put_AreBrowserAcceleratorKeysEnabled": 24},
    "ICoreWebView2Settings4": {"get_IsPasswordAutosaveEnabled": 25, "put_IsPasswordAutosaveEnabled": 26,
                               "get_IsGeneralAutofillEnabled": 27, "put_IsGeneralAutofillEnabled": 28},
    "ICoreWebView2Settings5": {"get_IsPinchZoomEnabled": 29, "put_IsPinchZoomEnabled": 30},
    "ICoreWebView2Settings6": {"get_IsSwipeNavigationEnabled": 31, "put_IsSwipeNavigationEnabled": 32},
    "ICoreWebView2Settings8": {"get_IsReputationCheckingRequired": 35, "put_IsReputationCheckingRequired": 36},
    "ICoreWebView2WebMessageReceivedEventArgs": {"get_Source": 3, "get_WebMessageAsJson": 4},
    "ICoreWebView2NavigationStartingEventArgs": {"get_Uri": 3, "put_Cancel": 8},
    "ICoreWebView2NavigationCompletedEventArgs": {"get_IsSuccess": 3, "get_WebErrorStatus": 4},
    "ICoreWebView2NewWindowRequestedEventArgs": {"get_Uri": 3, "put_Handled": 6},
    "ICoreWebView2PermissionRequestedEventArgs": {"get_Uri": 3, "get_PermissionKind": 4, "put_State": 7},
    "ICoreWebView2ProcessFailedEventArgs": {"get_ProcessFailedKind": 3},
    "ICoreWebView2DownloadStartingEventArgs": {"put_Cancel": 5, "put_ResultFilePath": 7, "put_Handled": 9},
    "ICoreWebView2LaunchingExternalUriSchemeEventArgs": {"get_Uri": 3, "put_Cancel": 7},
    "ICoreWebView2ScriptDialogOpeningEventArgs": {"get_Kind": 4},
    "ICoreWebView2BrowserProcessExitedEventArgs": {"get_BrowserProcessExitKind": 3, "get_BrowserProcessId": 4},
    "ICoreWebView2EnvironmentOptions": {"get_AdditionalBrowserArguments": 3, "put_AdditionalBrowserArguments": 4,
                                        "get_Language": 5, "put_Language": 6, "get_TargetCompatibleBrowserVersion": 7,
                                        "put_TargetCompatibleBrowserVersion": 8,
                                        "get_AllowSingleSignOnUsingOSPrimaryAccount": 9,
                                        "put_AllowSingleSignOnUsingOSPrimaryAccount": 10},
    "ICoreWebView2EnvironmentOptions2": {"get_ExclusiveUserDataFolderAccess": 3, "put_ExclusiveUserDataFolderAccess": 4},
    "ICoreWebView2EnvironmentOptions3": {"get_IsCustomCrashReportingEnabled": 3, "put_IsCustomCrashReportingEnabled": 4},
}
IIDS = {
    "IUnknown": "00000000-0000-0000-C000-000000000046",
    "ICoreWebView2Environment": "B96D755E-0319-4E92-A296-23436F46A1FC",
    "ICoreWebView2Environment5": "319E423D-E0D7-4B8D-9254-AE9475DE9B17",
    "ICoreWebView2Environment12": "F503DB9B-739F-48DD-B151-FDFCF253F54E",
    "ICoreWebView2Controller": "4D00C0D1-9434-4EB6-8078-8697A560334F",
    "ICoreWebView2Controller3": "F9614724-5D2B-41DC-AEF7-73D62B51543B",
    "ICoreWebView2": "76ECEACB-0462-4D94-AC83-423A6793775E",
    "ICoreWebView2_3": "A0D6DF20-3B92-416D-AA0C-437A9C727857",
    "ICoreWebView2_4": "20D02D59-6DF2-42DC-BD06-F98A694B1302",
    "ICoreWebView2_17": "702E75D4-FD44-434D-9D70-1A68A6B1192A",
    "ICoreWebView2_18": "7A626017-28BE-49B2-B865-3BA2B3522D90",
    "ICoreWebView2SharedBuffer": "B747A495-0C6F-449E-97B8-2F81E9D6AB43",
    "ICoreWebView2Settings": "E562E4F0-D7FA-43AC-8D71-C05150499F00",
    "ICoreWebView2Settings3": "FDB5AB74-AF33-4854-84F0-0A631DEB5EBA",
    "ICoreWebView2Settings4": "CB56846C-4168-4D53-B04F-03B6D6796FF2",
    "ICoreWebView2Settings5": "183E7052-1D03-43A0-AB99-98E043B66B39",
    "ICoreWebView2Settings6": "11CB3ACD-9BC8-43B8-83BF-F40753714F87",
    "ICoreWebView2Settings8": "9E6B0E8F-86AD-4E81-8147-A9B5EDB68650",
    "ICoreWebView2EnvironmentOptions": "2FDE08A8-1E9A-4766-8C05-95A9CEB9D1C5",
    "ICoreWebView2EnvironmentOptions2": "FF85C98A-1BA7-4A6B-90C8-2B752C89E9E2",
    "ICoreWebView2EnvironmentOptions3": "4A5C436E-A9E3-4A2E-89C3-910D3513F5CC",
    "ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler": "4E8A3389-C9D8-4BD2-B6B5-124FEE6CC14D",
    "ICoreWebView2CreateCoreWebView2ControllerCompletedHandler": "6C4819F3-C9B7-4260-8127-C9F5BDE7F68C",
    "ICoreWebView2WebMessageReceivedEventHandler": "57213F19-00E6-49FA-8E07-898EA01ECBD2",
    "ICoreWebView2NavigationStartingEventHandler": "9ADBE429-F36D-432B-9DDC-F8881FBD76E3",
    "ICoreWebView2NavigationCompletedEventHandler": "D33A35BF-1C49-4F98-93AB-006E0533FE1C",
    "ICoreWebView2NewWindowRequestedEventHandler": "D4C185FE-C81C-4989-97AF-2D3FA7AB5651",
    "ICoreWebView2PermissionRequestedEventHandler": "15E1C6A3-C72A-4DF3-91D7-D097FBEC6BFD",
    "ICoreWebView2ProcessFailedEventHandler": "79E0AEA4-990B-42D9-AA1D-0FCC2E5BC7F1",
    "ICoreWebView2BrowserProcessExitedEventHandler": "FA504257-A216-4911-A860-FE8825712861",
    "ICoreWebView2DownloadStartingEventHandler": "EFEDC989-C396-41CA-83F7-07F845A55724",
    "ICoreWebView2LaunchingExternalUriSchemeEventHandler": "74F712E0-8165-43A9-A13F-0CCE597E75DF",
    "ICoreWebView2ScriptDialogOpeningEventHandler": "EF381BF9-AFA8-4E37-91C4-8AC48524BDFB",
    "ICoreWebView2ExecuteScriptCompletedHandler": "49511172-CC67-4BCA-9923-137112F4C4CC",
}

# Enum values, from the same IDL (an enum without explicit values counts from 0).
ACCESS_DENY = 0                      # COREWEBVIEW2_HOST_RESOURCE_ACCESS_KIND_DENY
PERMISSION_DENY = 2                  # COREWEBVIEW2_PERMISSION_STATE_DENY
FAILED_BROWSER_EXITED = 0            # COREWEBVIEW2_PROCESS_FAILED_KIND_BROWSER_PROCESS_EXITED
MOVE_FOCUS_PROGRAMMATIC = 0          # COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC
SHARED_BUFFER_READ_ONLY = 0          # COREWEBVIEW2_SHARED_BUFFER_ACCESS_READ_ONLY

S_OK = 0


def _signed(hr: int) -> int:
    """An HRESULT as the signed 32-bit value a c_long return carries."""
    return hr - (1 << 32) if hr & 0x80000000 else hr


E_FAIL = _signed(0x80004005)
E_NOINTERFACE = _signed(0x80004002)
E_POINTER = _signed(0x80004003)
E_OUTOFMEMORY = _signed(0x8007000E)

# ------------------------------------------------------------------------------------------------- structs --

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("ptMinTrackSize", wintypes.POINT), ("ptMaxTrackSize", wintypes.POINT)]


class COREWEBVIEW2_COLOR(ctypes.Structure):
    _fields_ = [("A", ctypes.c_ubyte), ("R", ctypes.c_ubyte), ("G", ctypes.c_ubyte), ("B", ctypes.c_ubyte)]


# x64 sizes these layouts must produce (WinUser.h, WebView2.h).
assert ctypes.sizeof(ctypes.c_void_p) == 8, "lag-doctor's window targets 64-bit Python only"
assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(WNDCLASSEXW) == 80
assert ctypes.sizeof(MINMAXINFO) == 40
assert ctypes.sizeof(COREWEBVIEW2_COLOR) == 4

# ----------------------------------------------------------------------------------------- Win32 bindings --

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


def _bind(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


H, U, I, P, B = wintypes.HWND, wintypes.UINT, ctypes.c_int, ctypes.c_void_p, wintypes.BOOL
RegisterClassExW = _bind(_user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
CreateWindowExW = _bind(_user32, "CreateWindowExW", H, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, I, I, I, I, H, wintypes.HMENU, wintypes.HINSTANCE, P)
DefWindowProcW = _bind(_user32, "DefWindowProcW", LRESULT, H, U, wintypes.WPARAM, wintypes.LPARAM)
ShowWindow = _bind(_user32, "ShowWindow", B, H, I)
SetForegroundWindow = _bind(_user32, "SetForegroundWindow", B, H)
DestroyWindow = _bind(_user32, "DestroyWindow", B, H)
GetMessageW = _bind(_user32, "GetMessageW", B, ctypes.POINTER(wintypes.MSG), H, U, U)
TranslateMessage = _bind(_user32, "TranslateMessage", B, ctypes.POINTER(wintypes.MSG))
DispatchMessageW = _bind(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
PostMessageW = _bind(_user32, "PostMessageW", B, H, U, wintypes.WPARAM, wintypes.LPARAM)
PostQuitMessage = _bind(_user32, "PostQuitMessage", None, I)
SendMessageW = _bind(_user32, "SendMessageW", LRESULT, H, U, wintypes.WPARAM, wintypes.LPARAM)
GetClientRect = _bind(_user32, "GetClientRect", B, H, ctypes.POINTER(wintypes.RECT))
SetWindowPos = _bind(_user32, "SetWindowPos", B, H, H, I, I, I, I, U)
SetTimer = _bind(_user32, "SetTimer", ctypes.c_size_t, H, ctypes.c_size_t, U, P)
KillTimer = _bind(_user32, "KillTimer", B, H, ctypes.c_size_t)
LoadCursorW = _bind(_user32, "LoadCursorW", wintypes.HANDLE, wintypes.HINSTANCE, P)
LoadImageW = _bind(_user32, "LoadImageW", wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, U, I, I, U)
DestroyIcon = _bind(_user32, "DestroyIcon", B, wintypes.HICON)
GetDpiForWindow = _bind(_user32, "GetDpiForWindow", U, H)
GetDpiForSystem = _bind(_user32, "GetDpiForSystem", U)
GetSystemMetricsForDpi = _bind(_user32, "GetSystemMetricsForDpi", I, I, U)
SetProcessDpiAwarenessContext = _bind(_user32, "SetProcessDpiAwarenessContext", B, ctypes.c_ssize_t)
FindWindowW = _bind(_user32, "FindWindowW", H, wintypes.LPCWSTR, wintypes.LPCWSTR)
IsIconic = _bind(_user32, "IsIconic", B, H)
MessageBoxW = _bind(_user32, "MessageBoxW", I, H, wintypes.LPCWSTR, wintypes.LPCWSTR, U)
CreateSolidBrush = _bind(_gdi32, "CreateSolidBrush", wintypes.HBRUSH, wintypes.COLORREF)
GetModuleHandleW = _bind(_kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
GetCurrentThreadId = _bind(_kernel32, "GetCurrentThreadId", wintypes.DWORD)
SetEnvironmentVariableW = _bind(_kernel32, "SetEnvironmentVariableW", B, wintypes.LPCWSTR, wintypes.LPCWSTR)
GetEnvironmentVariableW = _bind(_kernel32, "GetEnvironmentVariableW", wintypes.DWORD, wintypes.LPCWSTR,
                                wintypes.LPWSTR, wintypes.DWORD)
CoInitializeEx = _bind(_ole32, "CoInitializeEx", ctypes.c_long, P, wintypes.DWORD)
CoUninitialize = _bind(_ole32, "CoUninitialize", None)
CoTaskMemAlloc = _bind(_ole32, "CoTaskMemAlloc", P, ctypes.c_size_t)
CoTaskMemFree = _bind(_ole32, "CoTaskMemFree", None, P)
SetCurrentProcessExplicitAppUserModelID = _bind(_shell32, "SetCurrentProcessExplicitAppUserModelID", ctypes.c_long,
                                                wintypes.LPCWSTR)
GetCurrentProcessExplicitAppUserModelID = _bind(_shell32, "GetCurrentProcessExplicitAppUserModelID", ctypes.c_long,
                                                ctypes.POINTER(P))

# Window messages and flags (WinUser.h) the host uses.
WM_DESTROY, WM_MOVE, WM_SIZE, WM_SETFOCUS, WM_CLOSE = 0x0002, 0x0003, 0x0005, 0x0007, 0x0010
WM_GETMINMAXINFO, WM_SETICON, WM_TIMER, WM_MOVING, WM_DPICHANGED = 0x0024, 0x0080, 0x0113, 0x0216, 0x02E0
WM_APP = 0x8000
WS_OVERLAPPEDWINDOW = 0x00CF0000
CW_USEDEFAULT = -0x80000000
SW_SHOWNORMAL, SW_SHOWNOACTIVATE, SW_RESTORE = 1, 4, 9
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
ICON_SMALL, ICON_BIG = 0, 1
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXICON, SM_CXSMICON = 11, 49
COINIT_APARTMENTTHREADED = 0x2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_ENVVAR_NOT_FOUND = 203
MB_OK, MB_ICONWARNING, MB_ICONINFORMATION, MB_SETFOREGROUND = 0x0, 0x30, 0x40, 0x10000
IDC_ARROW = 32512

# -------------------------------------------------------------------------------------------------- COM core --


def guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID = {name: guid(text) for name, text in IIDS.items()}


def guid_bytes(g) -> bytes:
    return ctypes.string_at(ctypes.addressof(g), 16)


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


def check(hr: int, what: str) -> None:
    if hr < 0:
        raise ComError(what, hr)


def take_wstr(p: ctypes.c_void_p) -> str | None:
    """Read and free a CoTaskMemAlloc'd string a getter returned."""
    if not p.value:
        return None
    try:
        return ctypes.wstring_at(p.value)
    finally:
        CoTaskMemFree(p.value)


def co_wstr(text: str) -> int:
    """A CoTaskMemAlloc'd copy of `text` for a getter we implement; the caller frees it. 0 when out of memory."""
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    p = CoTaskMemAlloc(size)
    if p:
        ctypes.memmove(p, buf, size)
    return p or 0


class Com:
    """A COM interface pointer, owned (released by release()) or borrowed (an event's sender or arguments)."""

    __slots__ = ("ptr", "iface", "owned")

    def __init__(self, ptr: int | None, iface: str, owned: bool = True) -> None:
        self.ptr = ptr or 0
        self.iface = iface
        self.owned = owned

    def __bool__(self) -> bool:
        return bool(self.ptr)

    def call(self, method: str, argtypes: tuple = (), *args) -> int:
        return vcall(self.ptr, SLOTS[self.iface][method], argtypes, *args)

    def do(self, method: str, argtypes: tuple = (), *args) -> None:
        check(self.call(method, argtypes, *args), f"{self.iface}::{method}")

    def get_bool(self, method: str) -> bool:
        v = wintypes.BOOL()
        self.do(method, (ctypes.POINTER(wintypes.BOOL),), ctypes.byref(v))
        return bool(v.value)

    def put_bool(self, method: str, value: bool) -> None:
        self.do(method, (wintypes.BOOL,), 1 if value else 0)

    def get_str(self, method: str) -> str | None:
        p = ctypes.c_void_p()
        self.do(method, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(p))
        return take_wstr(p)

    def get_u32(self, method: str) -> int:
        v = ctypes.c_uint32()
        self.do(method, (ctypes.POINTER(ctypes.c_uint32),), ctypes.byref(v))
        return v.value

    def get_u64(self, method: str) -> int:
        v = ctypes.c_uint64()
        self.do(method, (ctypes.POINTER(ctypes.c_uint64),), ctypes.byref(v))
        return v.value

    def get_int(self, method: str) -> int:
        v = ctypes.c_int()
        self.do(method, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(v))
        return v.value

    def get_double(self, method: str) -> float:
        v = ctypes.c_double()
        self.do(method, (ctypes.POINTER(ctypes.c_double),), ctypes.byref(v))
        return v.value

    def get_ptr(self, method: str, iface: str) -> Com:
        """An owned pointer an [out, retval] getter returns, as `iface`."""
        p = ctypes.c_void_p()
        self.do(method, (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(p))
        return Com(p.value, iface)

    def qi(self, iface: str) -> Com:
        """An owned pointer to `iface`, or a null Com when the object does not have it."""
        out = ctypes.c_void_p()
        hr = vcall(self.ptr, 0, (ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)),
                   ctypes.byref(IID[iface]), ctypes.byref(out))
        return Com(out.value if hr == S_OK else 0, iface)

    def release(self) -> None:
        if self.ptr and self.owned:
            vcall(self.ptr, 2, (), restype=ctypes.c_ulong)
        self.ptr = 0


QI_PROTO = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
REF_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
EVENT = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)        # sender, args
DONE_PTR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p)    # errorCode, result
DONE_STR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, ctypes.c_wchar_p)   # errorCode, json
GET_STR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
PUT_STR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)
GET_BOOL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL))
PUT_BOOL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.BOOL)

Guard = Callable[[str, Callable], Callable]      # (interface, fn(a, b)) -> the C-callable fn(this, a, b) -> HRESULT


class Handler:
    """A COM object of our own: IUnknown plus one Invoke, for one WebView2 handler interface.

    The object is one pointer-sized cell holding the address of a four-entry vtable of ctypes thunks. Whoever made it
    keeps this Python object referenced for as long as WebView2 may call it. `guard(iface, fn)` returns what Invoke
    runs: it calls fn(a, b) and must never raise into C."""

    def __init__(self, iface: str, proto, fn, guard: Guard) -> None:
        self.iface = iface
        self.refs = 1
        self._iid = guid_bytes(IID[iface])
        self._unknown = guid_bytes(IID["IUnknown"])
        self._fns = [QI_PROTO(self._query), REF_PROTO(self._addref), REF_PROTO(self._release),
                     proto(guard(iface, fn))]
        self._vtbl = (ctypes.c_void_p * 4)(*[ctypes.cast(f, ctypes.c_void_p) for f in self._fns])
        self._cell = ctypes.c_void_p(ctypes.addressof(self._vtbl))
        self.ptr = ctypes.addressof(self._cell)

    def _query(self, this, riid, ppv):
        try:
            asked = ctypes.string_at(riid, 16)
            if asked in (self._iid, self._unknown):
                ppv[0] = this
                self.refs += 1
                return S_OK
            ppv[0] = None
            return E_NOINTERFACE
        except BaseException:  # noqa: BLE001 - nothing may raise into C
            return E_FAIL

    def _addref(self, this):
        self.refs += 1
        return self.refs

    def _release(self, this):
        self.refs = max(0, self.refs - 1)
        return self.refs        # the Python object lives on in its owner's keep list; nothing is freed here


class EnvOptions:
    """ICoreWebView2EnvironmentOptions, Options2 and Options3 as one COM object: three faces, one QueryInterface.

    `strings` holds AdditionalBrowserArguments, Language and TargetCompatibleBrowserVersion; `bools` holds
    AllowSingleSignOnUsingOSPrimaryAccount, ExclusiveUserDataFolderAccess and IsCustomCrashReportingEnabled. Setters
    change nothing. `asked` records every getter the runtime calls, so a caller can show the runtime read it."""

    STRINGS = ("AdditionalBrowserArguments", "Language", "TargetCompatibleBrowserVersion")
    BOOLS = ("AllowSingleSignOnUsingOSPrimaryAccount", "ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled")

    def __init__(self, strings: dict[str, str], bools: dict[str, bool]) -> None:
        if set(strings) != set(self.STRINGS) or set(bools) != set(self.BOOLS):
            raise ValueError("EnvOptions needs exactly its three strings and three flags")
        self.asked: list[str] = []
        self.refs = 1
        self._keep: list = []
        common = [QI_PROTO(self._query), REF_PROTO(self._addref), REF_PROTO(self._release)]

        def getter_str(name):
            def get(this, out):
                try:
                    self.asked.append(name)
                    if not out:
                        return E_POINTER
                    p = co_wstr(strings[name])
                    out[0] = p
                    return S_OK if p else E_OUTOFMEMORY
                except BaseException:  # noqa: BLE001
                    return E_FAIL
            return GET_STR(get)

        def getter_bool(name):
            def get(this, out):
                try:
                    self.asked.append(name)
                    if not out:
                        return E_POINTER
                    out[0] = 1 if bools[name] else 0
                    return S_OK
                except BaseException:  # noqa: BLE001
                    return E_FAIL
            return GET_BOOL(get)

        def setter(name, proto):
            def put(this, value):
                try:
                    self.asked.append("put_" + name)
                except BaseException:  # noqa: BLE001
                    pass
                return S_OK
            return proto(put)

        faces = {
            "ICoreWebView2EnvironmentOptions": [
                getter_str("AdditionalBrowserArguments"), setter("AdditionalBrowserArguments", PUT_STR),
                getter_str("Language"), setter("Language", PUT_STR),
                getter_str("TargetCompatibleBrowserVersion"), setter("TargetCompatibleBrowserVersion", PUT_STR),
                getter_bool("AllowSingleSignOnUsingOSPrimaryAccount"),
                setter("AllowSingleSignOnUsingOSPrimaryAccount", PUT_BOOL)],
            "ICoreWebView2EnvironmentOptions2": [
                getter_bool("ExclusiveUserDataFolderAccess"), setter("ExclusiveUserDataFolderAccess", PUT_BOOL)],
            "ICoreWebView2EnvironmentOptions3": [
                getter_bool("IsCustomCrashReportingEnabled"), setter("IsCustomCrashReportingEnabled", PUT_BOOL)],
        }
        self._face_ptrs: dict[bytes, int] = {}
        for iface, methods in faces.items():
            if len(methods) != len(SLOTS[iface]):          # every slot of the interface is filled, in order
                raise AssertionError(iface)
            fns = common + methods
            vtbl = (ctypes.c_void_p * len(fns))(*[ctypes.cast(f, ctypes.c_void_p) for f in fns])
            cell = ctypes.c_void_p(ctypes.addressof(vtbl))
            self._keep += fns + [vtbl, cell]
            self._face_ptrs[guid_bytes(IID[iface])] = ctypes.addressof(cell)
        self.ptr = self._face_ptrs[guid_bytes(IID["ICoreWebView2EnvironmentOptions"])]
        self._face_ptrs[guid_bytes(IID["IUnknown"])] = self.ptr

    def _query(self, this, riid, ppv):
        try:
            face = self._face_ptrs.get(ctypes.string_at(riid, 16))
            ppv[0] = face
            if face is None:
                return E_NOINTERFACE
            self.refs += 1
            return S_OK
        except BaseException:  # noqa: BLE001
            return E_FAIL

    def _addref(self, this):
        self.refs += 1
        return self.refs

    def _release(self, this):
        self.refs = max(0, self.refs - 1)
        return self.refs


# --------------------------------------------------------------------------------------------- the runtime --

def parse_runtime_version(text: str) -> tuple[int, int, int, int] | None:
    m = re.fullmatch(r"(\d{1,5})\.(\d{1,5})\.(\d{1,5})\.(\d{1,5})", text or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def find_runtime() -> dict | None:
    """The installed Evergreen runtime, as webview/webview finds it: machine-wide first, then per-user. None when
    none is registered. {"folder", "version", "build", "dll", "exe"}; the files are not checked here."""
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, CLIENT_STATE, 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
                value, kind = winreg.QueryValueEx(key, "EBWebView")
        except OSError:
            continue
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
            continue
        folder = Path(os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value)
        version = parse_runtime_version(folder.name)
        if version is None:
            continue
        return {"folder": str(folder), "version": folder.name, "build": version[2],
                "dll": str(folder / "EBWebView" / "x64" / "EmbeddedBrowserWebView.dll"),
                "exe": str(folder / "msedgewebview2.exe")}
    return None


CREATE_PROTO = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_bool, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_void_p,
                                  ctypes.c_void_p)


def environment_creator(dll: str):
    """The runtime's CreateWebViewEnvironmentWithOptionsInternal(checkRunningInstance, runtimeType, userDataFolder,
    environmentOptions, handler) from `dll`. OSError when the DLL does not load, AttributeError when it lacks the
    export."""
    return CREATE_PROTO((EXPORT, ctypes.WinDLL(dll)))


# -------------------------------------------------------------------------------------- the environment block --

def env_state(name: str) -> str | None:
    """The variable as this process's environment block holds it: None when absent (not just empty)."""
    size = 256
    while True:
        buf = ctypes.create_unicode_buffer(size)
        ctypes.set_last_error(0)
        n = GetEnvironmentVariableW(name, buf, len(buf))
        if n == 0:
            return None if ctypes.get_last_error() == ERROR_ENVVAR_NOT_FOUND else ""
        if n < size:
            return buf.value
        size = n + 1                                  # too small: n is the size it needs


def set_env(name: str, value: str | None) -> None:
    """Set (or, with None, remove) a variable in this process's environment block. An empty value stays present
    and empty, which os.environ cannot do (it removes the variable)."""
    if not SetEnvironmentVariableW(name, value):
        raise ctypes.WinError(ctypes.get_last_error())
