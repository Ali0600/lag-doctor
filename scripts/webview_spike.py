"""M0, the window spike (docs/LAUNCHER-PLAN.md): lag-doctor's own window, drawn by WebView2 from Python's ctypes
through the runtime's own export, with no loader DLL, no server and no third-party package.

The owner's run, in their own terminal (about three minutes; the window shows four steps that need a person):

    uv run python scripts/webview_spike.py

    --auto       the automated checks alone (agents and CI); the window does not take the focus
    --rehearse   the owner's four steps with automatic answers, so their code runs; it checks nothing
    --ci         like --auto (or with --rehearse, a rehearsal), and writes GitHub's job summary
    --work-dir   where the run folders go

While the automated checks run, the window opens DevTools on purpose, goes blank for a few seconds, reloads, and
moves itself for about 3 s (Windows' move mode puts the mouse pointer on its title bar, then the spike puts it back);
two short windows follow at the end. Every check prints PASS, FAIL or NOT RUN with what it measured, and a check that
needs a control runs it: a check whose control does not fail as predicted cannot pass. Results and a log go to a fresh
folder under %TEMP%\\lag-doctor-webview-spike, printed at the start and at the end. Nothing else is written, apart
from a download the window is refused (redirected into that folder in case the refusal fails); WebView2's data
folder is in it too.

Exit codes: 0 every check passed (or, rehearsing, none failed), 1 a check failed, 2 bad arguments, 3 no WebView2
Runtime (an answer, on CI), 4 the run was cut short (the window closed or Ctrl+C before the end).

Where the numbers come from:
- Vtable slots and IIDs: counted from the C vtables in WebView2.h of Microsoft.Web.WebView2 1.0.4314.35
  (nuget.org, nupkg SHA-256 90778deb1b77e36292828b4b5833c9255c27533f40e0ddd55891ae718944edc4). All 297 interfaces
  in that header agree with the package's WebView2.idl (order, IIDs and parents), checked by script on 2026-11-08.
  M1 pins them in a test against that IDL (docs/LAUNCHER-PLAN.md, M1).
- The export, its arguments and the runtime's registry value: docs/research/desktop-app.md section 3, re-read in
  webview/webview's loader.hh and Wails' go-webview2 env_create.go (2026-11-08).
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import faulthandler
import gc
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lagdoctor import setup as _setup  # noqa: E402  WinVerifyTrust and the signer's name, as for PresentMon
from lagdoctor.winapi import ntquery  # noqa: E402  handle-free process snapshots

# ------------------------------------------------------------------------------------------------- constants --

APP_HOST = "app.lagdoctor.example"          # RFC 6761 name, one host for the bundled files (research note, section 3)
APP_ORIGIN = "https://" + APP_HOST
ENTRY = APP_ORIGIN + "/index.html"
AUMID = "LagDoctor.WebViewSpike"             # the app will use its own ID; the spike's must not collide with it
WINDOW_TITLE = "lag-doctor window spike"
STABLE_CHANNEL = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
CLIENT_STATE = "SOFTWARE\\Microsoft\\EdgeUpdate\\ClientState\\" + STABLE_CHANNEL
EXPORT = "CreateWebViewEnvironmentWithOptionsInternal"
RUNTIME_INSTALLED = 0                        # webview/webview: webview2_runtime_type::installed
MIN_BUILD = 1150                             # webview/webview's api_version: the third part of the runtime version
MICROSOFT = "Microsoft Corporation"
WORK = Path(tempfile.gettempdir()) / "lag-doctor-webview-spike"
RUN_MARKER = ".spike-run"                    # only folders holding this file are ever pruned, and it is never deleted
KEEP_RUNS = 5                                # newest runs kept whole; older ones lose only their WebView2 data
KEEP_EVIDENCE = 50                           # runs whose results and log are kept at all
LIVE_GRACE_S = 2 * 3600                      # an unfinished run younger than this may still be running: never touched
STARTUP_LIMIT_S = 10.0
RECOVERY_LIMIT_S = 10.0
CLOSE_LIMIT_S = 5.0                          # the plan's number; one run of eight here took 5.16 s (2026-11-08)
CHILD_TIMEOUT_S = 120.0
REHEARSAL_ANSWER_S = 45                      # a rehearsal's prompt that gets no automatic answer ends NOT RUN
OWNER_IDS = frozenset({"drag", "keys", "dpi", "taskbar"})     # the checks only a person can answer
FILETIME_EPOCH = 116444736000000000                          # 1970-01-01 in 100 ns units since 1601
# Every check a full run reports, in order: one missing from the results means the run stopped before it.
AUTO_IDS = ("env", "runtime", "startup", "options", "settings", "view", "footprint-1", "assets", "sockets-1", "csp",
            "messages", "watch", "nav-site", "nav-own", "nav-app", "popup", "permissions", "download", "dialogs",
            "windows", "footprint-2", "modal", "suspend", "recovery", "options-again", "settings-again", "sockets-2",
            "env-back", "close", "pythonw", "keepalive", "fatal")
OWNER_ORDER = ("drag", "keys", "dpi", "taskbar")
# The keys the owner is asked to press. No F-keys: on many laptops they are hardware keys (fan mode, airplane mode).
OWNER_KEYS = {"ctrl+r": "Ctrl+R", "ctrl+p": "Ctrl+P", "ctrl+f": "Ctrl+F", "ctrl+s": "Ctrl+S",
              "ctrl+shift+i": "Ctrl+Shift+I", "ctrl+plus": "Ctrl+Plus", "ctrl+minus": "Ctrl+Minus",
              "alt+arrowleft": "Alt+Left"}

# Environment variables WebView2 reads itself, even without Microsoft's loader. Set, not removed: Wails' loader notes
# that WebView2 checks they exist, so an empty one also stops the matching registry override (env_create.go). They
# are set only around the environment's creation and restored after, so processes started later inherit the user's.
HARDENED_ENV = {
    "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER": "",
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "",
    "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE": "0",
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": "",
    "WEBVIEW2_USER_DATA_FOLDER": "",
}

# Generated from WebView2.h 1.0.4314.35 (see the module docstring): only what the spike calls or implements.
SLOTS = {
    "ICoreWebView2Environment": {"CreateCoreWebView2Controller": 3, "get_BrowserVersionString": 5},
    "ICoreWebView2Environment5": {"add_BrowserProcessExited": 12},
    "ICoreWebView2Controller": {"put_IsVisible": 4, "get_Bounds": 5, "put_Bounds": 6, "get_ZoomFactor": 7, "add_ZoomFactorChanged": 9, "MoveFocus": 12, "add_AcceleratorKeyPressed": 19, "NotifyParentWindowPositionChanged": 23, "Close": 24, "get_CoreWebView2": 25},
    "ICoreWebView2Controller3": {"put_DefaultBackgroundColor": 27, "get_RasterizationScale": 28, "get_ShouldDetectMonitorScaleChanges": 30, "add_RasterizationScaleChanged": 32, "get_BoundsMode": 34},
    "ICoreWebView2": {"get_Settings": 3, "Navigate": 5, "add_NavigationStarting": 7, "add_NavigationCompleted": 15, "add_FrameNavigationStarting": 17, "add_ScriptDialogOpening": 21, "add_PermissionRequested": 23, "add_ProcessFailed": 25, "PostWebMessageAsJson": 32, "add_WebMessageReceived": 34, "remove_WebMessageReceived": 35, "get_BrowserProcessId": 37, "add_NewWindowRequested": 44, "OpenDevToolsWindow": 51},
    "ICoreWebView2_3": {"TrySuspend": 68, "Resume": 69, "get_IsSuspended": 70, "SetVirtualHostNameToFolderMapping": 71},
    "ICoreWebView2_4": {"add_DownloadStarting": 75},
    "ICoreWebView2_18": {"add_LaunchingExternalUriScheme": 117},
    "ICoreWebView2Settings": {"get_IsScriptEnabled": 3, "put_IsScriptEnabled": 4, "get_IsWebMessageEnabled": 5, "put_IsWebMessageEnabled": 6, "get_AreDefaultScriptDialogsEnabled": 7, "put_AreDefaultScriptDialogsEnabled": 8, "get_IsStatusBarEnabled": 9, "put_IsStatusBarEnabled": 10, "get_AreDevToolsEnabled": 11, "put_AreDevToolsEnabled": 12, "get_AreDefaultContextMenusEnabled": 13, "put_AreDefaultContextMenusEnabled": 14, "get_AreHostObjectsAllowed": 15, "put_AreHostObjectsAllowed": 16, "get_IsZoomControlEnabled": 17, "put_IsZoomControlEnabled": 18, "get_IsBuiltInErrorPageEnabled": 19, "put_IsBuiltInErrorPageEnabled": 20},
    "ICoreWebView2Settings3": {"get_AreBrowserAcceleratorKeysEnabled": 23, "put_AreBrowserAcceleratorKeysEnabled": 24},
    "ICoreWebView2Settings4": {"get_IsPasswordAutosaveEnabled": 25, "put_IsPasswordAutosaveEnabled": 26, "get_IsGeneralAutofillEnabled": 27, "put_IsGeneralAutofillEnabled": 28},
    "ICoreWebView2Settings5": {"get_IsPinchZoomEnabled": 29, "put_IsPinchZoomEnabled": 30},
    "ICoreWebView2Settings6": {"get_IsSwipeNavigationEnabled": 31, "put_IsSwipeNavigationEnabled": 32},
    "ICoreWebView2Settings8": {"get_IsReputationCheckingRequired": 35, "put_IsReputationCheckingRequired": 36},
    "ICoreWebView2WebMessageReceivedEventArgs": {"get_Source": 3, "get_WebMessageAsJson": 4},
    "ICoreWebView2NavigationStartingEventArgs": {"get_Uri": 3, "get_IsUserInitiated": 4, "put_Cancel": 8},
    "ICoreWebView2NavigationCompletedEventArgs": {"get_IsSuccess": 3, "get_WebErrorStatus": 4},
    "ICoreWebView2NewWindowRequestedEventArgs": {"get_Uri": 3, "put_Handled": 6},
    "ICoreWebView2PermissionRequestedEventArgs": {"get_Uri": 3, "get_PermissionKind": 4, "put_State": 7},
    "ICoreWebView2ProcessFailedEventArgs": {"get_ProcessFailedKind": 3},
    "ICoreWebView2AcceleratorKeyPressedEventArgs": {"get_KeyEventKind": 3, "get_VirtualKey": 4},
    "ICoreWebView2DownloadStartingEventArgs": {"put_Cancel": 5, "put_ResultFilePath": 7, "put_Handled": 9},
    "ICoreWebView2LaunchingExternalUriSchemeEventArgs": {"get_Uri": 3, "put_Cancel": 7},
    "ICoreWebView2ScriptDialogOpeningEventArgs": {"get_Kind": 4},
    "ICoreWebView2BrowserProcessExitedEventArgs": {"get_BrowserProcessExitKind": 3, "get_BrowserProcessId": 4},
    "ICoreWebView2EnvironmentOptions": {"get_AdditionalBrowserArguments": 3, "put_AdditionalBrowserArguments": 4, "get_Language": 5, "put_Language": 6, "get_TargetCompatibleBrowserVersion": 7, "put_TargetCompatibleBrowserVersion": 8, "get_AllowSingleSignOnUsingOSPrimaryAccount": 9, "put_AllowSingleSignOnUsingOSPrimaryAccount": 10},
    "ICoreWebView2EnvironmentOptions2": {"get_ExclusiveUserDataFolderAccess": 3, "put_ExclusiveUserDataFolderAccess": 4},
    "ICoreWebView2EnvironmentOptions3": {"get_IsCustomCrashReportingEnabled": 3, "put_IsCustomCrashReportingEnabled": 4},
}
IIDS = {
    "IUnknown": "00000000-0000-0000-C000-000000000046",
    "ICoreWebView2Environment": "B96D755E-0319-4E92-A296-23436F46A1FC",
    "ICoreWebView2Environment5": "319E423D-E0D7-4B8D-9254-AE9475DE9B17",
    "ICoreWebView2Controller": "4D00C0D1-9434-4EB6-8078-8697A560334F",
    "ICoreWebView2Controller3": "F9614724-5D2B-41DC-AEF7-73D62B51543B",
    "ICoreWebView2": "76ECEACB-0462-4D94-AC83-423A6793775E",
    "ICoreWebView2_3": "A0D6DF20-3B92-416D-AA0C-437A9C727857",
    "ICoreWebView2_4": "20D02D59-6DF2-42DC-BD06-F98A694B1302",
    "ICoreWebView2_18": "7A626017-28BE-49B2-B865-3BA2B3522D90",
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
    "ICoreWebView2AcceleratorKeyPressedEventHandler": "B29C7E28-FA79-41A8-8E44-65811C76DCB2",
    "ICoreWebView2ZoomFactorChangedEventHandler": "B52D71D6-C4DF-4543-A90C-64A3E60F38CB",
    "ICoreWebView2BrowserProcessExitedEventHandler": "FA504257-A216-4911-A860-FE8825712861",
    "ICoreWebView2RasterizationScaleChangedEventHandler": "9C98C8B1-AC53-427E-A345-3049B5524BBE",
    "ICoreWebView2DownloadStartingEventHandler": "EFEDC989-C396-41CA-83F7-07F845A55724",
    "ICoreWebView2LaunchingExternalUriSchemeEventHandler": "74F712E0-8165-43A9-A13F-0CCE597E75DF",
    "ICoreWebView2ScriptDialogOpeningEventHandler": "EF381BF9-AFA8-4E37-91C4-8AC48524BDFB",
    "ICoreWebView2TrySuspendCompletedHandler": "00F206A7-9D17-4605-91F6-4E8E4DE192E3",
}

# Enum values, from the same header.
ACCESS_DENY = 0                      # COREWEBVIEW2_HOST_RESOURCE_ACCESS_KIND_DENY
PERMISSION_DENY = 2                  # COREWEBVIEW2_PERMISSION_STATE_DENY
FAILED_BROWSER_EXITED = 0            # COREWEBVIEW2_PROCESS_FAILED_KIND_BROWSER_PROCESS_EXITED
MOVE_FOCUS_PROGRAMMATIC = 0
PERMISSION_KINDS = {0: "unknown", 1: "microphone", 2: "camera", 3: "geolocation", 4: "notifications",
                    5: "other sensors", 6: "clipboard read"}

S_OK = 0


def _signed(hr: int) -> int:
    """An HRESULT as the signed 32-bit value a c_long return carries."""
    return hr - (1 << 32) if hr & 0x80000000 else hr


E_FAIL = _signed(0x80004005)
E_NOINTERFACE = _signed(0x80004002)
E_POINTER = _signed(0x80004003)
E_OUTOFMEMORY = _signed(0x8007000E)

# Window messages and flags (WinUser.h).
WM_DESTROY, WM_MOVE, WM_SIZE, WM_SETFOCUS, WM_CLOSE = 0x0002, 0x0003, 0x0005, 0x0007, 0x0010
WM_GETMINMAXINFO, WM_SETICON, WM_TIMER, WM_MOVING = 0x0024, 0x0080, 0x0113, 0x0216
WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, WM_DPICHANGED = 0x0231, 0x0232, 0x02E0
WM_KEYDOWN, WM_SYSCOMMAND, WM_CANCELMODE = 0x0100, 0x0112, 0x001F
SC_MOVE, VK_ESCAPE = 0xF010, 0x1B
WM_APP = 0x8000
WM_APP_CALL = WM_APP + 1            # run queued calls on the UI thread, outside any COM callback
WM_APP_WORKER = WM_APP + 2          # a worker's result, posted to the window
WM_APP_THREAD = WM_APP + 3          # a worker's result, posted to the thread (the control: lost in modal loops)
WS_OVERLAPPEDWINDOW = 0x00CF0000
CW_USEDEFAULT = -0x80000000
SW_SHOWNORMAL, SW_SHOWNOACTIVATE = 1, 4
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
ICON_SMALL, ICON_BIG = 0, 1
SM_CXICON, SM_CXSMICON = 11, 49
MONITOR_DEFAULTTONEAREST = 2
COINIT_APARTMENTTHREADED = 0x2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
ERROR_ENVVAR_NOT_FOUND = 203
ERROR_INSUFFICIENT_BUFFER = 122
SEM_FAILCRITICALERRORS, SEM_NOGPFAULTERRORBOX = 0x0001, 0x0002
CREATE_NO_WINDOW = 0x08000000
HEARTBEAT_TIMER = 1

# ------------------------------------------------------------------------------------------ Win32 bindings --

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


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


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class COREWEBVIEW2_COLOR(ctypes.Structure):
    _fields_ = [("A", ctypes.c_ubyte), ("R", ctypes.c_ubyte), ("G", ctypes.c_ubyte), ("B", ctypes.c_ubyte)]


# x64 sizes these layouts must produce (WinUser.h, WinGDI.h, WebView2.h).
assert ctypes.sizeof(ctypes.c_void_p) == 8, "the spike targets 64-bit Python only"
assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(WNDCLASSEXW) == 80
assert ctypes.sizeof(MINMAXINFO) == 40
assert ctypes.sizeof(ICONINFO) == 32
assert ctypes.sizeof(BITMAPINFOHEADER) == 40
assert ctypes.sizeof(COREWEBVIEW2_COLOR) == 4

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)


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
PostThreadMessageW = _bind(_user32, "PostThreadMessageW", B, wintypes.DWORD, U, wintypes.WPARAM, wintypes.LPARAM)
PostQuitMessage = _bind(_user32, "PostQuitMessage", None, I)
SendMessageW = _bind(_user32, "SendMessageW", LRESULT, H, U, wintypes.WPARAM, wintypes.LPARAM)
GetClientRect = _bind(_user32, "GetClientRect", B, H, ctypes.POINTER(wintypes.RECT))
SetWindowPos = _bind(_user32, "SetWindowPos", B, H, H, I, I, I, I, U)
SetTimer = _bind(_user32, "SetTimer", ctypes.c_size_t, H, ctypes.c_size_t, U, P)
KillTimer = _bind(_user32, "KillTimer", B, H, ctypes.c_size_t)
LoadCursorW = _bind(_user32, "LoadCursorW", wintypes.HANDLE, wintypes.HINSTANCE, P)
GetDpiForWindow = _bind(_user32, "GetDpiForWindow", U, H)
GetDpiForSystem = _bind(_user32, "GetDpiForSystem", U)
GetSystemMetricsForDpi = _bind(_user32, "GetSystemMetricsForDpi", I, I, U)
SetProcessDpiAwarenessContext = _bind(_user32, "SetProcessDpiAwarenessContext", B, ctypes.c_ssize_t)
MonitorFromWindow = _bind(_user32, "MonitorFromWindow", P, H, wintypes.DWORD)
EnumWindows = _bind(_user32, "EnumWindows", B, WNDENUMPROC, wintypes.LPARAM)
GetWindowThreadProcessId = _bind(_user32, "GetWindowThreadProcessId", wintypes.DWORD, H,
                                 ctypes.POINTER(wintypes.DWORD))
IsWindowVisible = _bind(_user32, "IsWindowVisible", B, H)
ReleaseCapture = _bind(_user32, "ReleaseCapture", B)
GetCursorPos = _bind(_user32, "GetCursorPos", B, ctypes.POINTER(wintypes.POINT))
SetCursorPos = _bind(_user32, "SetCursorPos", B, I, I)
GetWindowTextW = _bind(_user32, "GetWindowTextW", I, H, wintypes.LPWSTR, I)
GetClassNameW = _bind(_user32, "GetClassNameW", I, H, wintypes.LPWSTR, I)
CreateIconIndirect = _bind(_user32, "CreateIconIndirect", wintypes.HICON, ctypes.POINTER(ICONINFO))
DestroyIcon = _bind(_user32, "DestroyIcon", B, wintypes.HICON)
CreateDIBSection = _bind(_gdi32, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC,
                         ctypes.POINTER(BITMAPINFOHEADER), U, ctypes.POINTER(P), wintypes.HANDLE, wintypes.DWORD)
CreateBitmap = _bind(_gdi32, "CreateBitmap", wintypes.HBITMAP, I, I, U, U, P)
DeleteObject = _bind(_gdi32, "DeleteObject", B, wintypes.HGDIOBJ)
CreateSolidBrush = _bind(_gdi32, "CreateSolidBrush", wintypes.HBRUSH, wintypes.COLORREF)
GetModuleHandleW = _bind(_kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
GetCurrentThreadId = _bind(_kernel32, "GetCurrentThreadId", wintypes.DWORD)
SetEnvironmentVariableW = _bind(_kernel32, "SetEnvironmentVariableW", B, wintypes.LPCWSTR, wintypes.LPCWSTR)
GetEnvironmentVariableW = _bind(_kernel32, "GetEnvironmentVariableW", wintypes.DWORD, wintypes.LPCWSTR,
                                wintypes.LPWSTR, wintypes.DWORD)
SetErrorMode = _bind(_kernel32, "SetErrorMode", U, U)
CoInitializeEx = _bind(_ole32, "CoInitializeEx", ctypes.c_long, P, wintypes.DWORD)
CoUninitialize = _bind(_ole32, "CoUninitialize", None)
CoTaskMemAlloc = _bind(_ole32, "CoTaskMemAlloc", P, ctypes.c_size_t)
CoTaskMemFree = _bind(_ole32, "CoTaskMemFree", None, P)
SetCurrentProcessExplicitAppUserModelID = _bind(_shell32, "SetCurrentProcessExplicitAppUserModelID", ctypes.c_long,
                                                wintypes.LPCWSTR)
GetCurrentProcessExplicitAppUserModelID = _bind(_shell32, "GetCurrentProcessExplicitAppUserModelID", ctypes.c_long,
                                                ctypes.POINTER(P))
GetExtendedTcpTable = _bind(_iphlpapi, "GetExtendedTcpTable", wintypes.DWORD, P, ctypes.POINTER(wintypes.DWORD),
                            B, wintypes.ULONG, I, wintypes.ULONG)
GetExtendedUdpTable = _bind(_iphlpapi, "GetExtendedUdpTable", wintypes.DWORD, P, ctypes.POINTER(wintypes.DWORD),
                            B, wintypes.ULONG, I, wintypes.ULONG)

# --------------------------------------------------------------------------------------------- pure policy --
# The window's whole security rests on these two answers; tests/test_webview_spike.py holds them, red-first.


def origin_ok(source: str | None) -> bool:
    """True only for a document of lag-doctor's own host: https, that exact host, no port, no user info."""
    if not isinstance(source, str) or not source:
        return False
    try:
        parts = urllib.parse.urlsplit(source)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == APP_HOST and port is None
            and parts.username is None and parts.password is None)


def navigation_allowed(uri: str | None, first: bool) -> bool:
    """Only the first navigation of a view, and only to the entry page; everything later is refused."""
    if not first or not origin_ok(uri):
        return False
    parts = urllib.parse.urlsplit(uri)
    return parts.path == "/index.html" and not parts.query and not parts.fragment


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))          # ceil(n * pct / 100), at least 1
    return ordered[int(min(rank, len(ordered))) - 1]


def parse_runtime_version(text: str) -> tuple[int, int, int, int] | None:
    m = re.fullmatch(r"(\d{1,5})\.(\d{1,5})\.(\d{1,5})\.(\d{1,5})", text or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


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

    def get_int(self, method: str) -> int:
        v = ctypes.c_int()
        self.do(method, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(v))
        return v.value

    def get_double(self, method: str) -> float:
        v = ctypes.c_double()
        self.do(method, (ctypes.POINTER(ctypes.c_double),), ctypes.byref(v))
        return v.value

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
DONE_BOOL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_long, wintypes.BOOL)     # errorCode, ok
GET_STR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
PUT_STR = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)
GET_BOOL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL))
PUT_BOOL = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.BOOL)


class Handler:
    """A COM object of our own: IUnknown plus one Invoke, for one WebView2 handler interface.

    The object is one pointer-sized cell holding the address of a four-entry vtable of ctypes thunks. Whoever made
    it must keep this Python object referenced for as long as WebView2 may call it: an unreferenced thunk is freed,
    and WebView2 then calls freed memory (the drop-refs check proves it). `guard` wraps every call so nothing can
    raise into C: under pythonw an exception there is silent and leaves the return value unset."""

    def __init__(self, iface: str, proto, fn, guard=None) -> None:
        self.iface = iface
        self.refs = 1
        self._iid = guid_bytes(IID[iface])
        self._unknown = guid_bytes(IID["IUnknown"])
        self._fns = [QI_PROTO(self._query), REF_PROTO(self._addref), REF_PROTO(self._release),
                     proto(guard(iface, fn) if guard else self._raw(fn))]
        self._vtbl = (ctypes.c_void_p * 4)(*[ctypes.cast(f, ctypes.c_void_p) for f in self._fns])
        self._cell = ctypes.c_void_p(ctypes.addressof(self._vtbl))
        self.ptr = ctypes.addressof(self._cell)

    @staticmethod
    def _raw(fn):
        def call(this, a, b):        # deliberately unguarded: the pythonw control
            fn(a, b)
            return S_OK
        return call

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
        return self.refs        # the Python object lives on in the keep list; nothing is freed here


class EnvOptions:
    """ICoreWebView2EnvironmentOptions, Options2 and Options3 as one COM object: three faces, one QueryInterface.

    It asks for exclusive use of the data folder and for crash dumps to stay on the PC (custom crash reporting), and
    it records every getter the runtime calls, so the run shows whether the runtime read the object at all."""

    def __init__(self, target_version: str) -> None:
        self.asked: list[str] = []
        self.refs = 1
        strings = {"AdditionalBrowserArguments": "", "Language": "", "TargetCompatibleBrowserVersion": target_version}
        bools = {"AllowSingleSignOnUsingOSPrimaryAccount": False, "ExclusiveUserDataFolderAccess": True,
                 "IsCustomCrashReportingEnabled": True}
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
                self.asked.append("put_" + name)
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
            assert len(methods) == len(SLOTS[iface]), iface       # every slot of the interface is filled
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


# ----------------------------------------------------------------------------------------- runtime + env --

def find_runtime() -> dict | None:
    """The installed Evergreen runtime, as webview/webview finds it: machine-wide first, then per-user."""
    for root, root_name in ((winreg.HKEY_LOCAL_MACHINE, "HKLM"), (winreg.HKEY_CURRENT_USER, "HKCU")):
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
        return {"registry": f"{root_name}\\{CLIENT_STATE}", "folder": str(folder), "version": folder.name,
                "build": version[2], "dll": str(folder / "EBWebView" / "x64" / "EmbeddedBrowserWebView.dll"),
                "exe": str(folder / "msedgewebview2.exe")}
    return None


def signature(path: str) -> dict:
    rc, signer = _setup.authenticode(Path(path))
    note = ""
    if rc in _setup.REVOCATION_UNKNOWN:
        rc, signer = _setup.authenticode(Path(path), revocation=False)
        note = "revocation not checkable offline"
    return {"path": path, "trusted": rc == 0, "result": f"0x{rc:08X}", "signer": signer, "note": note}


def env_state(name: str) -> str | None:
    """The variable as this process's environment block holds it: None when absent (not just empty)."""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.set_last_error(0)
    n = GetEnvironmentVariableW(name, buf, len(buf))
    if n == 0:
        return None if ctypes.get_last_error() == ERROR_ENVVAR_NOT_FOUND else ""
    return buf.value


def harden_env() -> dict:
    """Set WebView2's own variables in-process, through SetEnvironmentVariableW, and keep what they were.

    CPython's os.environ cannot do it: os.putenv on Windows calls _wputenv("NAME="), which removes a variable
    instead of leaving it empty. Two controls, on a scratch name: the reader must tell a deleted variable from an
    empty one (or "set empty" proves nothing), and an empty os.environ value must read as removed (why the API)."""
    before = {name: env_state(name) for name in HARDENED_ENV}
    for name, value in HARDENED_ENV.items():
        SetEnvironmentVariableW(name, value)
    values = {name: env_state(name) for name in HARDENED_ENV}
    probe = "LAGDOCTOR_SPIKE_PROBE"
    SetEnvironmentVariableW(probe, None)
    absent = env_state(probe)
    SetEnvironmentVariableW(probe, "")
    empty = env_state(probe)
    SetEnvironmentVariableW(probe, None)
    os.environ[probe] = ""
    via_os = env_state(probe)
    os.environ.pop(probe, None)
    SetEnvironmentVariableW(probe, None)
    return {"ok": all(values[n] == v for n, v in HARDENED_ENV.items()), "values": values, "before": before,
            "reader_ok": absent is None and empty == "",
            "os_environ_empty_is": "removed" if via_os is None else repr(via_os)}


def restore_env(before: dict) -> bool:
    """Put each variable back as it was (an absent one is deleted again); True when every one reads back so."""
    for name, value in before.items():
        SetEnvironmentVariableW(name, value)
    return all(env_state(name) == value for name, value in before.items())


# --------------------------------------------------------------------------------------- sockets + processes --

class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwState", wintypes.DWORD), ("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD),
                ("dwRemoteAddr", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("ucRemoteAddr", ctypes.c_ubyte * 16),
                ("dwRemoteScopeId", wintypes.DWORD), ("dwRemotePort", wintypes.DWORD), ("dwState", wintypes.DWORD),
                ("dwOwningPid", wintypes.DWORD)]


class MIB_UDPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwLocalAddr", wintypes.DWORD), ("dwLocalPort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


class MIB_UDP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("ucLocalAddr", ctypes.c_ubyte * 16), ("dwLocalScopeId", wintypes.DWORD),
                ("dwLocalPort", wintypes.DWORD), ("dwOwningPid", wintypes.DWORD)]


assert ctypes.sizeof(MIB_TCPROW_OWNER_PID) == 24
assert ctypes.sizeof(MIB_TCP6ROW_OWNER_PID) == 56
assert ctypes.sizeof(MIB_UDPROW_OWNER_PID) == 12
assert ctypes.sizeof(MIB_UDP6ROW_OWNER_PID) == 28

TCP_TABLE_OWNER_PID_ALL = 5                  # every state, listeners and connections both
UDP_TABLE_OWNER_PID = 1
MIB_TCP_STATE_LISTEN, MIB_TCP_STATE_ESTAB = 2, 5
AF_INET, AF_INET6 = 2, 23


def _table(fn, family: int, table_class: int, row_type) -> list:
    size = wintypes.DWORD(0)
    for _ in range(8):
        buf = ctypes.create_string_buffer(max(size.value, 4))
        rc = fn(buf, ctypes.byref(size), False, family, table_class, 0)
        if rc == 0:
            count = ctypes.c_uint32.from_buffer(buf).value
            offset = 4 if ctypes.alignment(row_type) == 4 else 8      # rows follow dwNumEntries, aligned
            if offset + count * ctypes.sizeof(row_type) > len(buf):
                raise OSError("the socket table is shorter than its row count")
            return [row_type.from_buffer_copy(buf, offset + i * ctypes.sizeof(row_type)) for i in range(count)]
        if rc != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(f"the socket table call failed with error {rc}")
        size = wintypes.DWORD(size.value + 4096)
    raise OSError("the socket table kept growing")


def open_sockets(pids: set[int]) -> list[dict]:
    """Every TCP socket in any state, and every bound UDP endpoint, IPv4 and IPv6, owned by any of `pids`.

    state: "listen" and "bound" are a server's; "established" and the rest are connections."""
    out = []
    for proto, fn, fam, cls, row in (("tcp4", GetExtendedTcpTable, AF_INET, TCP_TABLE_OWNER_PID_ALL,
                                      MIB_TCPROW_OWNER_PID),
                                     ("tcp6", GetExtendedTcpTable, AF_INET6, TCP_TABLE_OWNER_PID_ALL,
                                      MIB_TCP6ROW_OWNER_PID),
                                     ("udp4", GetExtendedUdpTable, AF_INET, UDP_TABLE_OWNER_PID, MIB_UDPROW_OWNER_PID),
                                     ("udp6", GetExtendedUdpTable, AF_INET6, UDP_TABLE_OWNER_PID,
                                      MIB_UDP6ROW_OWNER_PID)):
        for r in _table(fn, fam, cls, row):
            if r.dwOwningPid not in pids:
                continue
            item = {"proto": proto, "pid": r.dwOwningPid, "port": socket.ntohs(r.dwLocalPort & 0xFFFF)}
            if proto.startswith("tcp"):
                item["state"] = ("listen" if r.dwState == MIB_TCP_STATE_LISTEN else
                                 "established" if r.dwState == MIB_TCP_STATE_ESTAB else f"state {r.dwState}")
                item["remote_port"] = socket.ntohs(r.dwRemotePort & 0xFFFF)
            else:
                item["state"] = "bound"
            out.append(item)
    return out


def text_scale() -> float:
    """Windows' Text size setting as a factor (1.0 when never set): WebView2's rasterization scale includes it."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Microsoft\\Accessibility") as key:
            value, kind = winreg.QueryValueEx(key, "TextScaleFactor")
        return value / 100 if kind == winreg.REG_DWORD and 100 <= value <= 225 else 1.0
    except OSError:
        return 1.0


def process_tree(snap: dict, root_pid: int) -> dict[int, ntquery.ProcInfo]:
    """root_pid and every descendant (a child must be younger than its parent: PIDs are reused)."""
    if root_pid not in snap:
        return {}
    tree = {root_pid: snap[root_pid]}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for p in snap.values():
            if p.ppid == parent and p.pid not in tree and p.create_time >= snap[parent].create_time:
                tree[p.pid] = p
                frontier.append(p.pid)
    return tree


def visible_windows(pids: set[int], skip: int = 0) -> list[dict]:
    """Visible top-level windows owned by `pids` (DevTools, menus, dialogs and prompts are all top-level)."""
    found: list[dict] = []

    def each(hwnd, _):
        if hwnd == skip or not IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            title = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            GetClassNameW(hwnd, cls, 256)
            found.append({"hwnd": hwnd, "pid": pid.value, "class": cls.value, "title": title.value})
        return len(found) < 64
    cb = WNDENUMPROC(each)
    EnumWindows(cb, 0)
    return found


# -------------------------------------------------------------------------------------------- the waits --

@dataclass
class Sleep:
    ms: int


@dataclass
class Page:
    test: str
    args: dict = field(default_factory=dict)
    timeout_ms: int = 15_000


@dataclass
class Until:
    pred: object
    timeout_ms: int
    poll_ms: int = 50


@dataclass
class Owner:
    title: str
    text: str
    buttons: list


@dataclass
class Work:
    fn: object        # runs in a worker thread; its return value (or exception) resumes the script


@dataclass
class Callback:
    start: object     # start(resume) begins something whose completion calls resume(value)
    timeout_ms: int


@dataclass
class Result:
    id: str
    name: str
    status: str               # PASS, FAIL, NOT RUN, INFO
    measured: str
    detail: dict = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------- the page --

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self'; style-src 'self';
 img-src 'self'; connect-src 'self'; worker-src 'none'; base-uri 'none'; form-action 'none'">
<title>lag-doctor window spike</title>
<link rel="stylesheet" href="style.css">
<script src="app.js" defer></script>
</head>
<body>
<header><h1>lag-doctor window spike</h1><p id="status">Starting</p></header>
<section id="prompt" hidden>
  <h2 id="prompt-title"></h2>
  <p id="prompt-text"></p>
  <div id="prompt-buttons"></div>
</section>
<section><h2>Checks</h2><ol id="results"></ol></section>
</body>
</html>
"""

OTHER_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>other</title></head>
<body><p>If you can read this, a navigation was not blocked.</p></body></html>
"""

STYLE_CSS = """:root { --spike-css: loaded; color-scheme: light; }
[hidden] { display: none !important; }
body { margin: 0; font: 15px/1.45 "Segoe UI", system-ui, sans-serif; background: #f6f4f1; color: #1d1b19; }
header { background: #e8590c; color: #fff; padding: 18px 24px; }
h1 { margin: 0; font-size: 22px; }
#status { margin: 4px 0 0; opacity: .92; }
section { padding: 12px 24px; }
#prompt { background: #fff; border: 2px solid #e8590c; border-radius: 8px; margin: 16px 24px; padding: 16px 20px; }
#prompt h2 { margin: 0 0 6px; font-size: 18px; }
#prompt-buttons button { font: inherit; margin: 8px 8px 0 0; padding: 8px 16px; border-radius: 6px;
  border: 1px solid #b44708; background: #fff3eb; cursor: pointer; }
#prompt-buttons button:first-child { background: #e8590c; color: #fff; }
ol { padding-left: 22px; }
li { margin: 3px 0; }
.PASS { color: #1b7a33; } .FAIL { color: #b3261e; } .NOTRUN, .INFO { color: #6b6560; }
.tag { display: inline-block; min-width: 64px; font-weight: 600; }
"""

APP_JS = r"""'use strict';
const wv = window.chrome && window.chrome.webview;
const instance = crypto.randomUUID();
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const send = (obj) => wv.postMessage(obj);
const pct = (sorted, p) => sorted.length ? sorted[Math.min(sorted.length, Math.max(1, Math.ceil(sorted.length * p / 100))) - 1] : null;

let echoSink = null, pongSink = null, pong2Sink = null;
let keysWatch = false, keysSeen = [], contextMenus = 0, ctrlWheels = 0;
let ticks = 0, ticker = null;

const handlers = {
  echo: (m) => { if (echoSink) echoSink(m); },
  pong: (m) => { if (pongSink) pongSink(m); },
  pong2: (m) => { if (pong2Sink) pong2Sink(m); },
  status: (m) => { $('status').textContent = m.text; },
  result: (m) => {
    const li = document.createElement('li');
    const tag = document.createElement('span');
    tag.className = 'tag ' + String(m.status).replace(' ', '');
    tag.textContent = m.status;
    li.append(tag, document.createTextNode(' ' + m.name + (m.measured ? ': ' + m.measured : '')));
    $('results').append(li);
  },
  prompt: (m) => {
    $('prompt-title').textContent = m.title;
    $('prompt-text').textContent = m.text;
    const box = $('prompt-buttons');
    box.replaceChildren();
    for (const label of m.buttons) {
      const b = document.createElement('button');
      b.textContent = label;
      b.addEventListener('click', () => { $('prompt').hidden = true; send({type: 'answer', id: m.id, button: label}); });
      box.append(b);
    }
    $('prompt').hidden = false;
    if (m.auto) setTimeout(() => { for (const b of box.children) if (b.textContent === m.auto) { b.click(); break; } }, 1000);
  },
  run: async (m) => {
    let report;
    try { report = await (tests[m.test] || (async () => ({error: 'no such test'})))(m.args || {}); }
    catch (err) { report = {error: String((err && err.message) || err)}; }
    send(Object.assign({type: 'report', token: m.token, test: m.test, instance}, report));
  },
};
wv.addEventListener('message', (e) => {
  const m = e.data;
  if (m && typeof m === 'object' && Object.hasOwn(handlers, m.type)) handlers[m.type](m);
});

function combo(e) {  // "ctrl+shift+i", "ctrl+plus", "alt+arrowleft"; a modifier pressed alone is no combination
  let k = String(e.key).toLowerCase();
  if (['control', 'shift', 'alt', 'meta', 'os'].includes(k)) return null;
  if (k === '=' || k === '+') k = 'plus';
  if (k === '-' || k === '_') k = 'minus';
  const mods = [];
  if (e.ctrlKey) mods.push('ctrl');
  if (e.shiftKey && k !== 'plus' && k !== 'minus') mods.push('shift');
  if (e.altKey) mods.push('alt');
  return mods.concat(k).join('+');
}
document.addEventListener('keydown', (e) => {
  const c = combo(e);
  if (keysWatch && c) keysSeen.push(c);
});
document.addEventListener('contextmenu', () => { if (keysWatch) contextMenus++; });
document.addEventListener('wheel', (e) => { if (keysWatch && e.ctrlKey) ctrlWheels++; }, {passive: true});

function watchDpr() {
  matchMedia(`(resolution: ${devicePixelRatio}dppx)`).addEventListener('change', () => {
    send({type: 'dpr', dpr: devicePixelRatio, w: innerWidth, h: innerHeight});
    watchDpr();
  }, {once: true});
}

const tests = {
  async assets({secret}) {
    const r = await fetch('data.json');
    const j = await r.json();
    const traversal = [];
    const abs = String(secret || '');  // the file beside the mapped folder, by its absolute path too
    for (const p of ['/../secret.txt', '/%2e%2e/secret.txt', '/..%2fsecret.txt', '/%2e%2e%2fsecret.txt',
                     '/..%5csecret.txt', '/%2e%2e%5csecret.txt', '/%5c..%5csecret.txt',
                     '/' + encodeURIComponent(abs), '/' + abs.replace(/\\/g, '/')]) {
      try { const x = await fetch(p); traversal.push({path: p, status: x.status, body: x.ok ? (await x.text()).slice(0, 60) : ''}); }
      catch (e) { traversal.push({path: p, status: 'error', body: String(e.name)}); }
    }
    return {origin: location.origin, script: new URL(document.scripts[0].src).origin,
            css: getComputedStyle(document.documentElement).getPropertyValue('--spike-css').trim(),
            data_ok: r.ok && j.marker === 'spike-data', traversal};
  },
  async csp() {
    const violations = [];
    const onV = (e) => { violations.push({directive: e.effectiveDirective, blocked: String(e.blockedURI)}); };
    document.addEventListener('securitypolicyviolation', onV);
    window.__spikeInline = false;
    const div = document.createElement('div');
    div.innerHTML = '<img src="/no-such-image.png" onerror="window.__spikeInline = true">';  // the attack, on purpose
    document.body.append(div);
    await sleep(800);
    let outside;  // no-cors: without the CSP this would load (opaque), so only the CSP can block it
    try { await fetch('https://example.com/', {mode: 'no-cors'}); outside = 'loaded'; } catch (e) { outside = 'blocked'; }
    await sleep(300);
    div.remove();
    document.removeEventListener('securitypolicyviolation', onV);
    return {inline_ran: window.__spikeInline === true, violations, outside_fetch: outside};
  },
  async burst({count}) {
    const sent = new Array(count);
    const got = [];
    const done = new Promise((resolve) => {
      echoSink = (m) => { got.push([m.seq, performance.now()]); if (got.length === count) resolve(); };
      setTimeout(resolve, 20000);
    });
    for (let i = 0; i < count; i++) { sent[i] = performance.now(); send({type: 'echo', seq: i}); }
    await done;
    echoSink = null;
    let inOrder = got.length === count;
    for (let i = 0; i < got.length && inOrder; i++) if (got[i][0] !== i) inOrder = false;
    const lat = got.map(([s, t]) => t - sent[s]).sort((a, b) => a - b);
    return {count, received: got.length, in_order: inOrder, p50: pct(lat, 50), p95: pct(lat, 95), max: lat.at(-1) ?? null};
  },
  async pingpong({count}) {
    const lat = [];
    for (let i = 0; i < count; i++) {
      const t = performance.now();
      const ok = await new Promise((resolve) => {
        const timer = setTimeout(() => resolve(false), 3000);
        pongSink = (m) => { if (m.seq === i) { clearTimeout(timer); resolve(true); } };
        send({type: 'ping', seq: i});
      });
      if (!ok) break;
      lat.push(performance.now() - t);
    }
    pongSink = null;
    lat.sort((a, b) => a - b);
    return {count, received: lat.length, p50: pct(lat, 50), p95: pct(lat, 95), max: lat.at(-1) ?? null};
  },
  async navigate({to}) { setTimeout(() => { location.href = to; }, 0); await sleep(1500); return {still_here: true}; },
  async popup({to}) { const w = window.open(to, '_blank'); await sleep(1000); return {returned: w === null ? 'null' : 'a window'}; },
  async permissions() {
    const out = {};
    out.geolocation = await new Promise((res) => navigator.geolocation.getCurrentPosition(
      () => res('granted'), (e) => res('refused (code ' + e.code + ')'), {timeout: 8000}));
    try { out.notifications = await Notification.requestPermission(); } catch (e) { out.notifications = 'error ' + e.name; }
    try { const s = await navigator.mediaDevices.getUserMedia({audio: true}); s.getTracks().forEach((t) => t.stop()); out.microphone = 'granted'; }
    catch (e) { out.microphone = 'refused (' + e.name + ')'; }
    try { await navigator.clipboard.readText(); out.clipboard = 'granted'; } catch (e) { out.clipboard = 'refused (' + e.name + ')'; }
    return out;
  },
  async download() {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(['lag-doctor spike'], {type: 'text/plain'}));
    a.download = 'lag-doctor-spike.txt';
    document.body.append(a);
    a.click();
    a.remove();
    await sleep(1500);
    return {clicked: true};
  },
  async dialogs() {
    const t = performance.now();
    const answer = confirm('lag-doctor spike: this dialog must never show');
    return {answer, ms: performance.now() - t};
  },
  async ticks_start() { ticks = 0; clearInterval(ticker); ticker = setInterval(() => { ticks++; }, 50); return {ok: true}; },
  async ticks_read() { return {ticks, now: performance.now()}; },
  async keys_begin() { keysSeen = []; contextMenus = 0; ctrlWheels = 0; keysWatch = true; return {ok: true}; },
  async keys_end() { keysWatch = false; return {keys: keysSeen, context_menus: contextMenus, ctrl_wheels: ctrlWheels, dpr: devicePixelRatio}; },
  async state() { return {dpr: devicePixelRatio, w: innerWidth, h: innerHeight}; },
  async raise_then_ping() {
    send({type: 'raise'});
    return await tests.pingpong({count: 3});
  },
  async raise_unwrapped_then_ping() {
    send({type: 'raise_unwrapped'});
    return await tests.pingpong({count: 3});
  },
  async ping2({count}) {
    let got = 0;
    pong2Sink = () => { got++; };
    for (let i = 0; i < count; i++) { send({type: 'ping2', seq: i}); await sleep(100); }
    await sleep(1500);
    pong2Sink = null;
    return {count, received: got};
  },
};

watchDpr();
send({type: 'ready', instance, origin: location.origin, dpr: devicePixelRatio, w: innerWidth, h: innerHeight,
      css: getComputedStyle(document.documentElement).getPropertyValue('--spike-css').trim()});
"""


def write_site(run_dir: Path) -> Path:
    site = run_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    for name, text in (("index.html", INDEX_HTML), ("other.html", OTHER_HTML), ("style.css", STYLE_CSS),
                       ("app.js", APP_JS), ("data.json", json.dumps({"marker": "spike-data"}))):
        (site / name).write_bytes(text.encode("utf-8"))
    (run_dir / "secret.txt").write_bytes(b"secret-outside-the-site")   # beside the site, never mapped
    return site


# ------------------------------------------------------------------------------------------------- the host --

class Spike:
    """One window, one WebView2, and a check script driven by the window's own message loop."""

    def __init__(self, mode: str, run_dir: Path, interactive: bool, log) -> None:
        self.mode = mode                      # main, pythonw, droprefs
        self.run_dir = run_dir
        self.interactive = interactive
        self._log = log
        self.t0 = time.perf_counter()
        self.marks: dict[str, float] = {}
        self.results: list[Result] = []
        self.keep: list = []                  # every handler WebView2 may call, for the window's whole life
        self.calls: collections.deque = collections.deque()
        self.timers: dict[int, object] = {}
        self.next_timer = 100
        self.hwnd = 0
        self.ui_thread = GetCurrentThreadId()
        self.stop_requested = False
        self.closing = False
        self.closed_early = False
        self.env = Com(0, "ICoreWebView2Environment")
        self.env5 = Com(0, "ICoreWebView2Environment5")
        self.controller = Com(0, "ICoreWebView2Controller")
        self.controller3 = Com(0, "ICoreWebView2Controller3")
        self.webview = Com(0, "ICoreWebView2")
        self.webview3 = Com(0, "ICoreWebView2_3")
        self.webview4 = Com(0, "ICoreWebView2_4")
        self.webview18 = Com(0, "ICoreWebView2_18")
        self.settings: dict[str, Com] = {}
        self.options: EnvOptions | None = None
        self.use_options = True
        self.runtime: dict | None = None
        self.create_fn = None
        self.browser_pid = 0
        self.browser_pids_seen: list[int] = []
        self.first_navigation = True
        self.ready_count = 0
        self.instance = None
        self.page_dpr = None
        self.page_dpr_events: list[dict] = []
        self.pending_pages: dict[int, object] = {}
        self.pending_owner: dict[str, object] = {}
        self.next_token = 1
        self._script = None
        self.recreating = False
        self.recreate_started = 0.0
        self.env_generation = 0
        # counters the checks read
        self.msgs_received = 0
        self.msgs_bad_origin = 0
        self.msgs_unknown = 0
        self.nav_events: list[dict] = []
        self.frame_nav_events: list[dict] = []
        self.new_windows: list[dict] = []
        self.permission_events: list[dict] = []
        self.downloads: list[dict] = []
        self.external_schemes: list[dict] = []
        self.dialogs: list[dict] = []
        self.accel_keys: list[dict] = []
        self.zoom_events = 0
        self.raster_events = 0
        self.process_failures: list[dict] = []
        self.browser_exits: list[dict] = []
        self.dpi_events: list[dict] = []
        self.monitor_moves = 0
        self.last_monitor = None
        self.drag = {"entered": 0, "exited": 0, "seconds": 0.0, "_t": None}
        self.loop_spans: list[list] = []        # [entered, exited] of each of Windows' move loops
        self.worker_posts: dict[str, list] = {"window": [], "thread": []}    # (seq, when) of each post
        self.worker_got: dict[str, set] = {"window": set(), "thread": set()}  # seqs that arrived
        self.workers_stop = threading.Event()
        self.watch_stop = threading.Event()
        self.watched_windows: list[dict] = []
        self.watch_base = 0
        self.raw_raised = 0
        self.rehearse = False
        self.icons: list[int] = []
        self.snapshotter = ntquery.Snapshotter()
        self.env_before: dict | None = None    # WebView2's variables as they were, put back once the view exists
        self.env_restored: bool | None = None
        self.fatal_events: list[str] = []      # access violations ctypes turned into OSError inside a callback
        self.dpi_samples: list[dict] = []      # the view and the page, read 1.5 s after each scale change
        self.pending_prompt: dict | None = None
        self.script_done = False
        self.options_asked: list[list[str]] = []   # what the runtime asked each options object, per environment

    # ---------------------------------------------------------------- logging + results

    def log(self, text: str) -> None:
        self._log(f"{time.perf_counter() - self.t0:8.3f} {text}")

    def log_exception(self, where: str) -> None:
        self.log(f"exception in {where}:\n{traceback.format_exc()}")

    def mark(self, name: str) -> None:
        self.marks[name] = round(time.perf_counter() - self.t0, 3)
        self.log(f"mark {name}")

    def add(self, id_: str, name: str, ok: bool | None, measured: str, **detail) -> None:
        status = "NOT RUN" if ok is None else ("PASS" if ok else "FAIL")
        if self.rehearse and id_ in OWNER_IDS:          # nobody answered: the code ran, the check did not
            status, measured = "NOT RUN", "rehearsal: " + measured
        self._add(Result(id_, name, status, measured, detail))

    def info(self, id_: str, name: str, measured: str, **detail) -> None:
        self._add(Result(id_, name, "INFO", measured, detail))

    def _add(self, r: Result) -> None:
        self.results.append(r)
        self.log(f"RESULT {r.status} {r.id}: {r.measured} {json.dumps(r.detail, default=str)[:2000]}")
        if self.mode == "main":
            say(f"{r.status:8s} {r.name}: {r.measured}")
            self.to_page({"type": "result", "name": r.name, "status": r.status, "measured": r.measured})

    # ---------------------------------------------------------------- guarded entry points from C

    def fault(self, where: str, exc: BaseException) -> None:
        """Log an exception from code C called. An access violation that ctypes turned into an OSError (its text
        starts "exception") means memory went wrong: it fails the run and stops it, never just a log line."""
        self.log_exception(where)
        if isinstance(exc, OSError) and not isinstance(exc, ComError) and str(exc).startswith("exception"):
            self.fatal_events.append(f"{where}: {exc}")
            self.stop_requested = True

    def guard(self, name: str, fn):
        def call(this, a, b):
            try:
                fn(a, b)
                return S_OK
            except Exception as exc:  # noqa: BLE001 - logged, then the handler returns normally
                self.fault(name, exc)
                return E_FAIL
            except BaseException:  # noqa: BLE001 - Ctrl+C or exit cannot cross into C: stop at the next message
                self.stop_requested = True
                return E_FAIL
        return call

    def handler(self, iface: str, proto, fn, keep: bool = True) -> Handler:
        h = Handler(iface, proto, fn, self.guard)
        if keep:
            self.keep.append(h)
        return h

    def add_event(self, com: Com, method: str, iface: str, proto, fn) -> None:
        h = self.handler(iface, proto, fn)
        token = ctypes.c_int64()
        com.do(method, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)), h.ptr, ctypes.byref(token))

    def post_call(self, fn) -> None:
        """Run fn on the UI thread from the message loop: never inside a COM callback, never from a worker."""
        self.calls.append(fn)
        if self.hwnd:
            PostMessageW(self.hwnd, WM_APP_CALL, 0, 0)

    def after(self, ms: int, fn) -> None:
        tid = self.next_timer
        self.next_timer += 1
        self.timers[tid] = fn
        SetTimer(self.hwnd, tid, max(1, int(ms)), None)

    # ---------------------------------------------------------------- the window

    def make_icon(self, size: int) -> int:
        """An orange square with a white L: the spike's own icon, so the taskbar can be read at a glance."""
        bmi = BITMAPINFOHEADER(40, size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        color = CreateDIBSection(None, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        px = bytearray(size * size * 4)
        stem, foot = max(2, size // 6), max(2, size // 6)
        for y in range(size):
            for x in range(size):
                i = (y * size + x) * 4
                edge = min(x, y, size - 1 - x, size - 1 - y)
                if edge < max(1, size // 16):
                    b, g, r = 0x08, 0x47, 0xB4
                else:
                    b, g, r = 0x0C, 0x59, 0xE8
                in_l = (size * 3 // 10 <= x < size * 3 // 10 + stem and size // 5 <= y < size * 4 // 5) or \
                       (size * 3 // 10 <= x < size * 7 // 10 and size * 4 // 5 - foot <= y < size * 4 // 5)
                if in_l:
                    b, g, r = 0xFF, 0xFF, 0xFF
                px[i:i + 4] = bytes((b, g, r, 0xFF))
        ctypes.memmove(bits, bytes(px), len(px))
        mask = CreateBitmap(size, size, 1, 1, ctypes.create_string_buffer(((size + 15) // 16) * 2 * size))
        info = ICONINFO(True, 0, 0, mask, color)
        icon = CreateIconIndirect(ctypes.byref(info))
        DeleteObject(mask)
        DeleteObject(color)
        if icon:
            self.icons.append(icon)
        return icon or 0

    def create_window(self) -> None:
        self._wndproc = WNDPROC(self.wndproc)
        hinst = GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = LoadCursorW(None, ctypes.c_void_p(32512))       # IDC_ARROW
        wc.hbrBackground = CreateSolidBrush(0x00F1F4F6)                # the page's background, before it paints
        wc.lpszClassName = f"LagDoctorSpike{os.getpid()}"
        if not RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        dpi = GetDpiForSystem() or 96
        w, h = 980 * dpi // 96, 760 * dpi // 96
        self.hwnd = CreateWindowExW(0, wc.lpszClassName, WINDOW_TITLE, WS_OVERLAPPEDWINDOW, CW_USEDEFAULT,
                                    CW_USEDEFAULT, w, h, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        wdpi = GetDpiForWindow(self.hwnd) or dpi
        big = self.make_icon(GetSystemMetricsForDpi(SM_CXICON, wdpi) or 32)
        small = self.make_icon(GetSystemMetricsForDpi(SM_CXSMICON, wdpi) or 16)
        SendMessageW(self.hwnd, WM_SETICON, ICON_BIG, big)
        SendMessageW(self.hwnd, WM_SETICON, ICON_SMALL, small)
        person = self.interactive and not self.rehearse
        ShowWindow(self.hwnd, SW_SHOWNORMAL if person else SW_SHOWNOACTIVATE)
        if person:
            SetForegroundWindow(self.hwnd)
        SetTimer(self.hwnd, HEARTBEAT_TIMER, 200, None)               # wakes the loop, so Ctrl+C is noticed
        self.last_monitor = MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST)
        self.mark("window")

    def client_rect(self) -> wintypes.RECT:
        rc = wintypes.RECT()
        GetClientRect(self.hwnd, ctypes.byref(rc))
        return rc

    def wndproc(self, hwnd, msg, wp, lp):
        try:
            r = self.on_message(hwnd, msg, wp, lp)
            if r is not None:
                return r
        except Exception as exc:  # noqa: BLE001
            self.fault(f"window message 0x{msg:04X}", exc)
        except BaseException:  # noqa: BLE001
            self.stop_requested = True
        return DefWindowProcW(hwnd, msg, wp, lp)

    def on_message(self, hwnd, msg, wp, lp):
        if msg == WM_APP_CALL:
            while self.calls:
                fn = self.calls.popleft()
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self.fault("a queued call", exc)
            return 0
        if msg == WM_APP_WORKER:
            self.worker_got["window"].add(wp)
            return 0
        if msg == WM_TIMER:
            if wp == HEARTBEAT_TIMER:
                if self.stop_requested and not self.closing:
                    self.log("stop requested (Ctrl+C, or a fault)")
                    self.closed_early = True
                    say("\nStopping: the run is cut short.")
                    self.begin_close()
                return 0
            fn = self.timers.pop(wp, None)
            KillTimer(hwnd, wp)
            if fn:
                fn()
            return 0
        if msg == WM_SIZE:
            if self.controller:
                self.controller.do("put_Bounds", (wintypes.RECT,), self.client_rect())
            return 0
        if msg in (WM_MOVE, WM_MOVING):
            if self.controller:
                self.controller.do("NotifyParentWindowPositionChanged")
            mon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if self.last_monitor is not None and mon != self.last_monitor:   # None until the window is shown
                self.monitor_moves += 1
                self.last_monitor = mon
                self.after(1500, lambda: self.sample_view("after a move to another monitor"))
            return None
        if msg == WM_DPICHANGED:
            rc = wintypes.RECT.from_address(lp)
            dpi = wp & 0xFFFF
            self.dpi_events.append({"t": round(time.perf_counter() - self.t0, 3), "dpi": dpi})
            SetWindowPos(hwnd, None, rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top,
                         SWP_NOZORDER | SWP_NOACTIVATE)
            self.after(1500, lambda: self.sample_view(f"1.5 s after a change to {dpi} DPI"))
            return 0
        if msg == WM_ENTERSIZEMOVE:
            self.drag["entered"] += 1
            self.drag["_t"] = time.perf_counter()
            self.loop_spans.append([self.drag["_t"], None])
            return None
        if msg == WM_EXITSIZEMOVE:
            self.drag["exited"] += 1
            if self.drag["_t"] is not None:
                self.drag["seconds"] += time.perf_counter() - self.drag["_t"]
                self.drag["_t"] = None
            if self.loop_spans and self.loop_spans[-1][1] is None:
                self.loop_spans[-1][1] = time.perf_counter()
            return None
        if msg == WM_SETFOCUS:
            if self.controller and not self.recreating and not self.closing:
                hr = self.controller.call("MoveFocus", (ctypes.c_int,), MOVE_FOCUS_PROGRAMMATIC)
                if hr < 0:      # 0x8007139F while the browser process is gone: the view is closed, not broken
                    self.log(f"MoveFocus: 0x{hr & 0xFFFFFFFF:08X}")
            return 0
        if msg == WM_GETMINMAXINFO:
            mmi = MINMAXINFO.from_address(lp)
            mmi.ptMinTrackSize.x, mmi.ptMinTrackSize.y = 480, 360
            return 0
        if msg == WM_CLOSE:
            if not self.closing:
                if self.script_done:              # during the closing notice: the close the script was about to do
                    self.log("the window was closed during the closing notice")
                else:
                    self.closed_early = True
                    self.log("the window was closed before the checks finished")
                    say("\nThe window was closed before the checks finished: the run is cut short.")
                self.begin_close()
            return 0
        if msg == WM_DESTROY:
            PostQuitMessage(0)
            return 0
        return None

    def loop(self) -> None:
        msg = wintypes.MSG()
        while True:
            r = GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0:
                break
            if r == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            if not msg.hWnd and msg.message == WM_APP_THREAD:
                self.worker_got["thread"].add(msg.wParam)
                continue
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    # ---------------------------------------------------------------- WebView2

    def create_environment(self) -> None:
        user_data = self.run_dir / "webview-data"          # recovery reuses the same folder
        self.env_generation += 1
        options_ptr = None
        if self.use_options:
            self.options = EnvOptions(self.runtime["version"])
            self.keep.append(self.options)                 # the runtime may hold it as long as the environment lives
            self.options_asked.append(self.options.asked)
            options_ptr = self.options.ptr
        if self.env_generation > 1:                        # a recovery: the same hardening, put back afterwards
            self.env_before = harden_env()["before"]
        done = self.handler("ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler", DONE_PTR,
                            self.on_environment)
        hr = self.create_fn(True, RUNTIME_INSTALLED, str(user_data), options_ptr, done.ptr)
        self.log(f"{EXPORT} returned 0x{hr & 0xFFFFFFFF:08X} (options {'on' if options_ptr else 'off'})")
        if hr < 0:
            raise ComError(EXPORT, hr)

    def on_environment(self, hr, env_ptr) -> None:
        if hr < 0 or not env_ptr:
            self.log(f"environment {self.env_generation} failed: 0x{hr & 0xFFFFFFFF:08X}")
            if self.use_options and self.env_generation == 1:
                self.use_options = False                   # first start only: is the options object the cause?
                self.post_call(self.create_environment)
                return
            if self.env_generation == 1:
                self.post_call(lambda: self.fail_startup(f"the environment was not created (HRESULT 0x{hr & 0xFFFFFFFF:08X})"))
            return                                         # a recovery never drops the options: it fails instead
        if self.options is not None:
            self.log(f"options object: {self.options.refs} references after creation")
        self.env = Com(env_ptr, "ICoreWebView2Environment", owned=False).qi("ICoreWebView2Environment")
        self.env5 = self.env.qi("ICoreWebView2Environment5")
        if self.env5:
            self.add_event(self.env5, "add_BrowserProcessExited", "ICoreWebView2BrowserProcessExitedEventHandler",
                           EVENT, self.on_browser_exited)
        self.mark("environment" if self.env_generation == 1 else f"environment {self.env_generation}")
        done = self.handler("ICoreWebView2CreateCoreWebView2ControllerCompletedHandler", DONE_PTR,
                            self.on_controller)
        self.env.do("CreateCoreWebView2Controller", (wintypes.HWND, ctypes.c_void_p), self.hwnd, done.ptr)

    def on_controller(self, hr, ctl_ptr) -> None:
        if hr < 0 or not ctl_ptr:
            self.post_call(lambda: self.fail_startup(f"the controller was not created (HRESULT 0x{hr & 0xFFFFFFFF:08X})"))
            return
        self.controller = Com(ctl_ptr, "ICoreWebView2Controller", owned=False).qi("ICoreWebView2Controller")
        self.controller3 = self.controller.qi("ICoreWebView2Controller3")
        p = ctypes.c_void_p()
        self.controller.do("get_CoreWebView2", (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(p))
        self.webview = Com(p.value, "ICoreWebView2")
        self.webview3 = self.webview.qi("ICoreWebView2_3")
        self.webview4 = self.webview.qi("ICoreWebView2_4")
        self.webview18 = self.webview.qi("ICoreWebView2_18")
        self.mark("controller" if self.ready_count == 0 else "controller again")
        if self.env_before is not None:                    # the browser process exists: give the variables back
            self.env_restored = restore_env(self.env_before)
            self.log(f"WebView2's environment variables put back as they were: {self.env_restored}")
        self.configure()
        self.first_navigation = True
        self.webview.do("Navigate", (wintypes.LPCWSTR,), ENTRY)
        self.mark("navigate" if self.ready_count == 0 else "navigate again")

    def configure(self) -> None:
        wv, ctl = self.webview, self.controller
        if self.controller3:
            self.controller3.do("put_DefaultBackgroundColor", (COREWEBVIEW2_COLOR,), COREWEBVIEW2_COLOR(255, 0xF6, 0xF4, 0xF1))
        p = ctypes.c_void_p()
        wv.do("get_Settings", (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(p))
        s = Com(p.value, "ICoreWebView2Settings")
        for old in self.settings.values():
            old.release()
        self.settings = {"ICoreWebView2Settings": s}
        for name in ("ICoreWebView2Settings3", "ICoreWebView2Settings4", "ICoreWebView2Settings5",
                     "ICoreWebView2Settings6", "ICoreWebView2Settings8"):
            self.settings[name] = s.qi(name)
        wanted = [("ICoreWebView2Settings", "IsScriptEnabled", True),
                  ("ICoreWebView2Settings", "IsWebMessageEnabled", True),
                  ("ICoreWebView2Settings", "AreDefaultScriptDialogsEnabled", False),
                  ("ICoreWebView2Settings", "IsStatusBarEnabled", False),
                  ("ICoreWebView2Settings", "AreDevToolsEnabled", False),
                  ("ICoreWebView2Settings", "AreDefaultContextMenusEnabled", False),
                  ("ICoreWebView2Settings", "AreHostObjectsAllowed", False),
                  ("ICoreWebView2Settings", "IsZoomControlEnabled", False),
                  ("ICoreWebView2Settings", "IsBuiltInErrorPageEnabled", False),
                  ("ICoreWebView2Settings3", "AreBrowserAcceleratorKeysEnabled", False),
                  ("ICoreWebView2Settings4", "IsPasswordAutosaveEnabled", False),
                  ("ICoreWebView2Settings4", "IsGeneralAutofillEnabled", False),
                  ("ICoreWebView2Settings5", "IsPinchZoomEnabled", False),
                  ("ICoreWebView2Settings6", "IsSwipeNavigationEnabled", False),
                  ("ICoreWebView2Settings8", "IsReputationCheckingRequired", False)]
        self.settings_readback = {}
        for iface, prop, value in wanted:
            com = self.settings[iface]
            if not com:
                self.settings_readback[prop] = "interface missing"
                continue
            com.put_bool("put_" + prop, value)
            back = com.get_bool("get_" + prop)
            self.settings_readback[prop] = "ok" if back == value else f"reads back {back}"
        self.add_event(wv, "add_WebMessageReceived", "ICoreWebView2WebMessageReceivedEventHandler", EVENT,
                       self.on_web_message)
        self.add_event(wv, "add_NavigationStarting", "ICoreWebView2NavigationStartingEventHandler", EVENT,
                       self.on_navigation_starting)
        self.add_event(wv, "add_NavigationCompleted", "ICoreWebView2NavigationCompletedEventHandler", EVENT,
                       self.on_navigation_completed)
        self.add_event(wv, "add_FrameNavigationStarting", "ICoreWebView2NavigationStartingEventHandler", EVENT,
                       self.on_frame_navigation_starting)
        self.add_event(wv, "add_NewWindowRequested", "ICoreWebView2NewWindowRequestedEventHandler", EVENT,
                       self.on_new_window)
        self.add_event(wv, "add_PermissionRequested", "ICoreWebView2PermissionRequestedEventHandler", EVENT,
                       self.on_permission)
        self.add_event(wv, "add_ProcessFailed", "ICoreWebView2ProcessFailedEventHandler", EVENT,
                       self.on_process_failed)
        self.add_event(wv, "add_ScriptDialogOpening", "ICoreWebView2ScriptDialogOpeningEventHandler", EVENT,
                       self.on_script_dialog)
        if self.webview4:
            self.add_event(self.webview4, "add_DownloadStarting", "ICoreWebView2DownloadStartingEventHandler",
                           EVENT, self.on_download)
        if self.webview18:
            self.add_event(self.webview18, "add_LaunchingExternalUriScheme",
                           "ICoreWebView2LaunchingExternalUriSchemeEventHandler", EVENT, self.on_external_scheme)
        self.add_event(ctl, "add_AcceleratorKeyPressed", "ICoreWebView2AcceleratorKeyPressedEventHandler", EVENT,
                       self.on_accelerator)
        self.add_event(ctl, "add_ZoomFactorChanged", "ICoreWebView2ZoomFactorChangedEventHandler", EVENT,
                       self.on_zoom)
        if self.controller3:
            self.add_event(self.controller3, "add_RasterizationScaleChanged",
                           "ICoreWebView2RasterizationScaleChangedEventHandler", EVENT, self.on_raster)
        if self.mode == "pythonw":
            self.add_unwrapped_handler()
        if not self.webview3:
            raise ComError("ICoreWebView2_3 (virtual host mapping)", E_NOINTERFACE)
        self.webview3.do("SetVirtualHostNameToFolderMapping", (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int),
                         APP_HOST, str(self.run_dir / "site"), ACCESS_DENY)
        ctl.do("put_Bounds", (wintypes.RECT,), self.client_rect())
        ctl.put_bool("put_IsVisible", True)
        self.browser_pid = self.webview.get_u32("get_BrowserProcessId")
        self.browser_pids_seen.append(self.browser_pid)
        me = self.snapshotter.snapshot().get(self.browser_pid)
        self.browser_created = me.create_time if me else None     # with the pid, the process's identity

    # ---------------------------------------------------------------- WebView2 events

    def on_web_message(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2WebMessageReceivedEventArgs", owned=False)
        source = a.get_str("get_Source")
        self.msgs_received += 1
        if not origin_ok(source):
            self.msgs_bad_origin += 1
            self.log(f"refused a message from {source!r}")
            return
        msg = json.loads(a.get_str("get_WebMessageAsJson") or "null")
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "echo":
            self.to_page({"type": "echo", "seq": msg.get("seq")})
        elif kind == "ping":
            self.to_page({"type": "pong", "seq": msg.get("seq")})
        elif kind == "report":
            waiter = self.pending_pages.pop(msg.get("token"), None)
            if waiter:
                self.post_call(lambda: waiter(msg))
        elif kind == "answer":
            waiter = self.pending_owner.pop(msg.get("id"), None)
            if waiter:
                self.post_call(lambda: waiter(msg.get("button")))
        elif kind == "ready":
            self.on_ready(msg)
        elif kind == "dpr":
            self.page_dpr = msg.get("dpr")
            if len(self.page_dpr_events) < 1000:        # a page cannot grow this without bound
                self.page_dpr_events.append({"t": round(time.perf_counter() - self.t0, 3), "dpr": msg.get("dpr")})
        elif kind == "raise":
            raise RuntimeError("a deliberate exception in a message handler (the pythonw check)")
        elif kind in ("raise_unwrapped", "ping2"):
            pass                                  # handled by the pythonw and drop-refs handlers
        else:
            self.msgs_unknown += 1

    def on_ready(self, msg: dict) -> None:
        self.ready_count += 1
        self.instance = msg.get("instance")
        self.page_dpr = msg.get("dpr")
        self.ready_msg = msg
        if self.ready_count == 1:
            self.ready_wall = time.time()
            self.mark("ready")
            self.post_call(self.start_script)
        else:
            self.mark("ready again")
            self.post_call(self.refill_page)

    def refill_page(self) -> None:
        """A reloaded page starts empty: give it back the results so far, the status line and any open prompt."""
        if self.mode != "main":
            return
        for r in self.results:
            self.to_page({"type": "result", "name": r.name, "status": r.status, "measured": r.measured})
        if getattr(self, "last_status", None):
            self.to_page({"type": "status", "text": self.last_status})
        if self.pending_prompt:
            self.to_page(self.pending_prompt)

    def on_navigation_starting(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NavigationStartingEventArgs", owned=False)
        a.put_bool("put_Cancel", True)           # refused first: if anything below raises, the refusal stands
        first, self.first_navigation = self.first_navigation, False
        uri = a.get_str("get_Uri")
        allowed = navigation_allowed(uri, first)
        if allowed:
            a.put_bool("put_Cancel", False)
        self.nav_events.append({"uri": uri, "allowed": allowed, "user": a.get_bool("get_IsUserInitiated")})
        self.log(f"navigation {'allowed' if allowed else 'cancelled'}: {uri}")

    def on_frame_navigation_starting(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NavigationStartingEventArgs", owned=False)
        a.put_bool("put_Cancel", True)
        self.frame_nav_events.append({"uri": a.get_str("get_Uri")})

    def on_navigation_completed(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NavigationCompletedEventArgs", owned=False)
        ok = a.get_bool("get_IsSuccess")
        status = a.get_int("get_WebErrorStatus")
        self.log(f"navigation completed: success={ok} status={status}")
        if ok and "first page" not in self.marks:
            self.mark("first page")

    def on_new_window(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NewWindowRequestedEventArgs", owned=False)
        a.put_bool("put_Handled", True)          # handled with no new window: window.open returns null
        self.new_windows.append({"uri": a.get_str("get_Uri")})

    def on_permission(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2PermissionRequestedEventArgs", owned=False)
        a.do("put_State", (ctypes.c_int,), PERMISSION_DENY)      # refused first, read afterwards
        kind = a.get_int("get_PermissionKind")
        self.permission_events.append({"kind": PERMISSION_KINDS.get(kind, str(kind)), "uri": a.get_str("get_Uri")})

    def on_download(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2DownloadStartingEventArgs", owned=False)
        try:                                     # if the cancel below ever failed, the file lands in the run folder
            a.do("put_ResultFilePath", (wintypes.LPCWSTR,), str(self.run_dir / "refused-download.txt"))
        except ComError as exc:
            self.log(f"redirecting the download: {exc}")
        a.put_bool("put_Cancel", True)
        a.put_bool("put_Handled", True)
        self.downloads.append({"t": round(time.perf_counter() - self.t0, 3)})

    def on_external_scheme(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2LaunchingExternalUriSchemeEventArgs", owned=False)
        a.put_bool("put_Cancel", True)
        self.external_schemes.append({"uri": a.get_str("get_Uri")})

    def on_script_dialog(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2ScriptDialogOpeningEventArgs", owned=False)
        self.dialogs.append({"kind": a.get_int("get_Kind")})       # never Accept: the dialog is dismissed

    def on_accelerator(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2AcceleratorKeyPressedEventArgs", owned=False)
        self.accel_keys.append({"kind": a.get_int("get_KeyEventKind"), "vk": a.get_u32("get_VirtualKey")})

    def on_zoom(self, sender, args) -> None:
        self.zoom_events += 1

    def on_raster(self, sender, args) -> None:
        self.raster_events += 1

    def on_process_failed(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2ProcessFailedEventArgs", owned=False)
        kind = a.get_int("get_ProcessFailedKind")
        self.process_failures.append({"kind": kind, "t": round(time.perf_counter() - self.t0, 3)})
        self.log(f"process failed, kind {kind}")
        if kind == FAILED_BROWSER_EXITED and not self.recreating and not self.closing:
            self.recreating = True
            self.post_call(self.drop_view)
            # Recreate on BrowserProcessExited, which comes once the data folder is released (0.1-0.5 s later here):
            # sooner, the exclusive folder would refuse. Only a runtime without that event gets a timer instead.
            if not self.env5:
                self.after(3000, self.recreate_if_waiting)

    def on_browser_exited(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2BrowserProcessExitedEventArgs", owned=False)
        self.browser_exits.append({"kind": a.get_int("get_BrowserProcessExitKind"),
                                   "pid": a.get_u32("get_BrowserProcessId"),
                                   "t": round(time.perf_counter() - self.t0, 3)})
        if self.recreating:
            self.post_call(self.recreate_if_waiting)

    def drop_view(self) -> None:
        for com in [*self.settings.values(), self.webview18, self.webview4, self.webview3, self.webview,
                    self.controller3]:
            com.release()
        self.settings = {}
        if self.controller:
            try:
                self.controller.do("Close")
            except ComError as exc:
                self.log(f"closing the dead controller: {exc}")
            self.controller.release()

    def recreate_if_waiting(self) -> None:
        if not self.recreating or self.closing:
            return
        self.recreating = False
        self.env5.release()
        self.env.release()
        self.log("recreating the environment after the browser process ended")
        self.create_environment()

    def add_unwrapped_handler(self) -> None:
        def raw(sender, args):
            a = Com(args, "ICoreWebView2WebMessageReceivedEventArgs", owned=False)
            msg = json.loads(a.get_str("get_WebMessageAsJson") or "null")
            if isinstance(msg, dict) and msg.get("type") == "raise_unwrapped":
                self.raw_raised += 1                       # proof it ran: the raise itself leaves no trace
                raise RuntimeError("a deliberate unwrapped exception (the pythonw control)")
        h = Handler("ICoreWebView2WebMessageReceivedEventHandler", EVENT, raw, guard=None)
        self.keep.append(h)
        token = ctypes.c_int64()
        self.webview.do("add_WebMessageReceived", (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)), h.ptr,
                        ctypes.byref(token))

    def to_page(self, obj: dict) -> bool:
        """Post to the page; False (and a log line for anything but a result) when no live view can take it."""
        if self.webview and not self.recreating:
            try:
                self.webview.do("PostWebMessageAsJson", (wintypes.LPCWSTR,), json.dumps(obj))
                return True
            except ComError as exc:
                self.log(f"posting to the page: {exc}")
                return False
        if obj.get("type") != "result":
            self.log(f"not posted, no live view: {obj.get('type')} {obj.get('test') or obj.get('title') or ''}")
        return False

    def status(self, text: str) -> None:
        self.last_status = text
        self.to_page({"type": "status", "text": text})

    def fail_startup(self, why: str) -> None:
        self.add("startup", "Window, environment, controller and first page up within 10 s", False, why,
                 marks=self.marks)
        self.begin_close()

    # ---------------------------------------------------------------- the script driver

    def start_script(self) -> None:
        if self._script is None:
            self._script = {"main": self.script_main, "pythonw": self.script_pythonw,
                            "droprefs": self.script_droprefs}[self.mode]()
            self.resume(None)

    def resume(self, value) -> None:
        if self._script is None or self.closing:
            return
        try:
            wait = self._script.send(value)
        except StopIteration:
            self._script = None
            return
        except Exception:  # noqa: BLE001
            self.log_exception("the check script")
            self._script = None
            self.add("script", "The check script ran to the end", False, "it raised; see the log")
            self.begin_close()
            return
        self.arm(wait)

    def arm(self, w) -> None:
        resume = self.resume
        if isinstance(w, Sleep):
            self.after(w.ms, lambda: resume(None))
        elif isinstance(w, Page):
            token = self.next_token
            self.next_token += 1
            done = {"v": False}

            def finish(report, done=done):
                if not done["v"]:
                    done["v"] = True
                    self.pending_pages.pop(token, None)
                    resume(report)
            self.pending_pages[token] = finish
            self.after(w.timeout_ms, lambda: finish(None))
            self.to_page({"type": "run", "test": w.test, "args": w.args, "token": token})
        elif isinstance(w, Until):
            deadline = time.monotonic() + w.timeout_ms / 1000

            def poll():
                if w.pred():
                    resume(True)
                elif time.monotonic() >= deadline:
                    resume(False)
                else:
                    self.after(w.poll_ms, poll)
            poll()
        elif isinstance(w, Owner):
            pid = uuid.uuid4().hex
            prompt = {"type": "prompt", "id": pid, "title": w.title, "text": w.text, "buttons": w.buttons}
            if self.rehearse:
                prompt["auto"] = w.buttons[0]           # the page clicks it itself, through the same path
            done = {"v": False}

            def answer(button, done=done):
                if not done["v"]:
                    done["v"] = True
                    self.pending_owner.pop(pid, None)
                    self.pending_prompt = None
                    resume(button)
            self.pending_owner[pid] = answer
            self.pending_prompt = prompt                # shown again if the page reloads before an answer
            if not self.to_page(prompt):
                self.log("the prompt waits for the page to come back")
            if self.rehearse:                           # nobody else will answer: never wait forever
                self.after(REHEARSAL_ANSWER_S * 1000, lambda: answer("no answer"))
            say(f"\n>>> In the spike's window: {w.title}")
        elif isinstance(w, Work):
            def run():
                try:
                    value = w.fn()
                except Exception as exc:  # noqa: BLE001
                    value = exc
                self.post_call(lambda: resume(value))
            threading.Thread(target=run, name="spike-work", daemon=True).start()
        elif isinstance(w, Callback):
            done = {"v": False}

            def finish(value, done=done):
                if not done["v"]:
                    done["v"] = True
                    resume(value)
            self.after(w.timeout_ms, lambda: finish(None))
            w.start(lambda value: self.post_call(lambda: finish(value)))
        else:
            raise TypeError(f"unknown wait {w!r}")

    # ---------------------------------------------------------------- checks: the main script

    def script_main(self):
        self.status("Automated checks for about 40 seconds: please leave the mouse and keyboard alone.")
        self.check_startup()
        self.check_settings("settings", "Every setting reads back as set")
        self.check_view("view", "at the first page")
        self.footprint("footprint-1", "at the first page")
        r = yield Page("assets", {"secret": str(self.run_dir / "secret.txt")})
        self.check_assets(r)
        self.check_sockets("sockets-1", "after the first page")
        r = yield Page("csp")
        self.check_csp(r)
        r = yield Page("burst", {"count": 1000}, timeout_ms=40_000)
        p = yield Page("pingpong", {"count": 200}, timeout_ms=40_000)
        self.check_messages(r, p)
        self.start_window_watch()
        self.status("A DevTools window opens and closes on purpose: the check that the window watch works.")
        yield from self.check_watch_control()
        self.status("Navigation, new windows, permissions, downloads and dialogs must all be refused.")
        yield from self.check_blocks()
        self.footprint("footprint-2", "after the page checks")
        self.status("The window moves itself for about 3 seconds (Windows' move mode).")
        yield from self.check_modal_loop()
        self.status("The view hides and suspends: blank for about 6 seconds.")
        yield from self.check_suspend()
        self.status("The spike ends its own browser process, and the page reloads.")
        yield from self.check_recovery()
        self.check_options_again()
        self.check_settings("settings-again", "Every setting reads back as set after the recovery")
        self.check_sockets("sockets-2", "after the recovery")
        self.check_env_back()
        if self.interactive:
            yield from self.owner_checks()
        self.stop_window_watch()
        self.script_done = True
        self.status("Done. The window closes in two seconds.")
        yield Sleep(2000)
        self.begin_close()

    def check_startup(self) -> None:
        me = self.snapshotter.snapshot().get(os.getpid())
        since_launch = (round(self.ready_wall - (me.create_time - FILETIME_EPOCH) / 1e7, 2)
                        if me is not None and getattr(self, "ready_wall", None) else None)
        ready = since_launch if since_launch is not None else self.marks.get("ready")
        ok = ready is not None and ready <= STARTUP_LIMIT_S
        stages = {k: self.marks[k] for k in ("runtime", "window", "environment", "controller", "navigate",
                                             "first page", "ready") if k in self.marks}
        self.add("startup", "Window, environment, controller and first page up within 10 s", ok,
                 (f"page ready {ready:.2f} s after this Python process started "
                  f"({self.marks.get('ready', 0):.2f} s after its imports)") if ready is not None
                 else "the page never became ready",
                 stages=stages, since_process_start=since_launch, other_webview2_processes_before=self.prior_webview2)
        need = {"ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled"}
        asked = set(self.options_asked[0]) if self.options_asked else set()
        self.add("options", "The runtime asked lag-doctor's options (own data folder, crash dumps kept here)",
                 self.use_options and need <= asked,
                 f"it asked for {len(asked)} values" if self.use_options else
                 "the environment failed with them and was created without them", asked=sorted(asked))

    def check_env_back(self) -> None:
        """WebView2's variables, set for each environment's creation, must be as they were once each view exists:
        processes lag-doctor starts later (a store's handler, a game) inherit the user's own environment."""
        before = self.env_before or {}
        now = {name: env_state(name) for name in before}
        back = sum(now[name] == value for name, value in before.items())
        self.add("env-back", "WebView2's environment variables are back as they were once the view exists",
                 self.env_restored is True and bool(before) and back == len(before),
                 f"{back} of {len(before)} as before (an absent one absent again), after the first start and the "
                 f"recovery", before={k: ("absent" if v is None else "set") for k, v in before.items()})

    def check_options_again(self) -> None:
        need = {"ExclusiveUserDataFolderAccess", "IsCustomCrashReportingEnabled"}
        again = len(self.options_asked) >= 2
        asked = set(self.options_asked[-1]) if again else set()
        self.add("options-again", "After the recovery, the runtime asked lag-doctor's options again",
                 again and need <= asked,
                 f"the new environment asked for {len(asked)} values" if again else
                 "no environment was created again with the options", asked=sorted(asked))

    def check_settings(self, id_: str, name: str) -> None:
        readback = dict(getattr(self, "settings_readback", {}))
        bad = {k: v for k, v in readback.items() if v != "ok"}
        self.add(id_, name, bool(readback) and not bad,
                 (f"all {len(readback)} as set" if readback and not bad else
                  "the view was never configured" if not readback else
                  "; ".join(f"{k}: {v}" for k, v in bad.items())), readback=readback)

    def view_state(self) -> dict:
        """The window's DPI, the view's scale and bounds and the page's devicePixelRatio, against each other.
        WebView2's rasterization scale is the monitor's scale times Windows' Text size, and the page's ratio
        follows it."""
        dpi = GetDpiForWindow(self.hwnd) or 96
        text = text_scale()
        expected = dpi / 96 * text
        raster = self.controller3.get_double("get_RasterizationScale") if self.controller3 else None
        b = wintypes.RECT()
        self.controller.do("get_Bounds", (ctypes.POINTER(wintypes.RECT),), ctypes.byref(b))
        rc = self.client_rect()
        fills = (b.left, b.top, b.right, b.bottom) == (rc.left, rc.top, rc.right, rc.bottom)
        dpr = self.page_dpr
        ok = (raster is not None and abs(raster - expected) < 0.02 and isinstance(dpr, (int, float))
              and abs(dpr - expected) < 0.02 and fills)
        shown = "none" if raster is None else f"{raster:.3f}"
        return {"ok": ok, "dpi": dpi, "text_scale": text, "expected": round(expected, 3), "view": raster,
                "page": dpr, "fills": fills, "t": round(time.perf_counter() - self.t0, 3),
                "text": (f"{dpi} DPI, text size {text:g}: view {shown}, page {dpr}, expected {expected:.3f}; "
                         f"the view {'fills' if fills else 'does NOT fill'} the window")}

    def check_view(self, id_: str, when: str) -> None:
        s = self.view_state()
        text, ok = s.pop("text"), s.pop("ok")
        self.add(id_, f"The view fills the window at the display's scale ({when})", ok, text, **s)

    def sample_view(self, why: str) -> None:
        """Read the view while a new scale is on: judged in the owner's display step."""
        if self.controller and not self.recreating and not self.closing:
            s = self.view_state()
            s["why"] = why
            self.dpi_samples.append(s)
            self.log(f"view sample ({why}): {s['text']}")

    def check_assets(self, r) -> None:
        if not r:
            self.add("assets", "Assets come from the virtual host; nothing outside it", False, "no report")
            return
        tries = r.get("traversal", [])
        leaked = [t for t in tries if "secret-outside" in str(t.get("body"))]
        ok = (r.get("origin") == APP_ORIGIN and r.get("script") == APP_ORIGIN and r.get("css") == "loaded"
              and r.get("data_ok") is True and len(tries) == 9 and not leaked)
        self.add("assets", "Assets come from the virtual host; nothing outside it", ok,
                 f"origin {r.get('origin')}, CSS {r.get('css') or 'missing'}, {len(tries)} escape attempts "
                 f"(encoded slashes and backslashes, the file's absolute path), {len(leaked)} read the file",
                 report=r)

    def check_sockets(self, id_: str, when: str) -> None:
        """No server: no listening TCP socket and no bound UDP endpoint in the spike or its WebView2 processes.
        WebView2's own outgoing connections are counted, not failed: they are the runtime's, not the app's."""
        own = os.getpid()
        tree = set(process_tree(self.snapshotter.snapshot(), self.browser_pid)) if self.browser_pid else set()
        # The control: a listener, a connected pair and UDP, IPv4 and IPv6, all the spike's own, must be read back
        # with their states, or a clean result proves nothing.
        made, expected = [], set()
        try:
            for family, host, proto in ((socket.AF_INET, "127.0.0.1", "4"), (socket.AF_INET6, "::1", "6")):
                lst = socket.socket(family, socket.SOCK_STREAM)
                made.append(lst)
                lst.bind((host, 0))
                lst.listen(1)
                expected.add(("tcp" + proto, lst.getsockname()[1], "listen"))
                udp = socket.socket(family, socket.SOCK_DGRAM)
                made.append(udp)
                udp.bind((host, 0))
                expected.add(("udp" + proto, udp.getsockname()[1], "bound"))
                if proto == "4":
                    cli = socket.create_connection((host, lst.getsockname()[1]), timeout=5)
                    made.append(cli)
                    srv, _ = lst.accept()
                    made.append(srv)
                    expected.add(("tcp4", cli.getsockname()[1], "established"))
            seen = {(x["proto"], x["port"], x["state"]) for x in open_sockets({own})}
            control_ok = expected <= seen
        finally:
            for s in made:
                s.close()
        control_ports = {port for _, port, _ in expected}
        found = open_sockets({own} | tree)
        servers = [x for x in found if x["state"] in ("listen", "bound") and x["pid"] != own]
        mine = [x for x in found if x["pid"] == own and x["port"] not in control_ports]
        outgoing = [x for x in found if x["pid"] != own and x not in servers]
        ok = control_ok and self.browser_pid in tree and not servers and not mine
        self.add(id_, f"No listening socket in lag-doctor or its WebView2 processes ({when})", ok,
                 (f"{len(servers)} listening or bound across {len(tree)} WebView2 processes, {len(mine)} sockets in "
                  f"the spike; WebView2's own outgoing connections: {len(outgoing)}; the control's "
                  f"{len(expected)} sockets {'were' if control_ok else 'were NOT'} read back"),
                 servers=servers, mine=mine, outgoing=[{"proto": x["proto"], "state": x["state"],
                                                        "remote_port": x.get("remote_port")} for x in outgoing])

    def check_csp(self, r) -> None:
        if not r:
            self.add("csp", "The page's CSP blocks inline script and outside fetches", False, "no report")
            return
        v = r.get("violations") or []
        script_v = [x for x in v if str(x.get("directive", "")).startswith("script-src")]
        connect_v = [x for x in v if x.get("directive") == "connect-src" and "example.com" in str(x.get("blocked"))]
        ok = (r.get("inline_ran") is False and bool(script_v) and bool(connect_v)
              and r.get("outside_fetch") == "blocked")
        self.add("csp", "The page's CSP blocks inline script and outside fetches", ok,
                 f"inline handler {'ran' if r.get('inline_ran') else 'blocked'} ({len(script_v)} script-src "
                 f"reports), outside fetch {r.get('outside_fetch')} ({len(connect_v)} connect-src reports)", report=r)

    def check_messages(self, burst, ping) -> None:
        if not burst or not ping:
            self.add("messages", "1,000 messages round-trip in order from the right origin", False,
                     "no report", burst=burst, ping=ping)
            return
        ok = (burst.get("received") == 1000 and burst.get("in_order") is True and self.msgs_bad_origin == 0
              and ping.get("received") == 200)
        self.add("messages", "1,000 messages round-trip in order from the right origin", ok,
                 f"{burst.get('received')}/1000 back, in order: {burst.get('in_order')}; burst p50 "
                 f"{ms(burst.get('p50'))} p95 {ms(burst.get('p95'))}; one at a time p50 {ms(ping.get('p50'))} "
                 f"p95 {ms(ping.get('p95'))}; {self.msgs_bad_origin} refused",
                 burst=burst, ping=ping, checked=self.msgs_received, refused=self.msgs_bad_origin)

    def check_blocks(self):
        inst = self.instance
        for to, id_, label in (("https://example.com/", "nav-site", "another site"),
                               ("/other.html", "nav-own", "another page of its own"),
                               ("lagdoctor-spike-test:nothing", "nav-app", "a link to another app")):
            n0, x0 = len(self.nav_events), len(self.external_schemes)
            r = yield Page("navigate", {"to": to}, timeout_ms=6000)
            events = self.nav_events[n0:]
            cancelled = bool(events) and all(not e["allowed"] for e in events)
            schemes = self.external_schemes[x0:]
            ok = bool(r and r.get("still_here") and r.get("instance") == inst) and (cancelled or bool(schemes))
            self.add(id_, f"Navigation to {label} is refused", ok,
                     f"{len(events)} navigation events, all refused: {cancelled}; requests to start another app "
                     f"refused: {len(schemes)}; the page {'stayed' if r else 'was lost'}", events=events,
                     schemes=schemes)
        n0, w0 = len(self.new_windows), len(self.watched_windows)
        r = yield Page("popup", {"to": "https://example.com/"})
        ok = bool(r and r.get("returned") == "null") and len(self.new_windows) > n0 and len(self.watched_windows) == w0
        self.add("popup", "New windows are refused", ok,
                 f"window.open returned {r.get('returned') if r else 'nothing'}; {len(self.new_windows) - n0} refused",
                 events=self.new_windows[n0:])
        p0, w0 = len(self.permission_events), len(self.watched_windows)
        r = yield Page("permissions", timeout_ms=20_000)
        answers = {k: v for k, v in (r or {}).items() if k in ("geolocation", "notifications", "microphone", "clipboard")}
        granted = [k for k, v in answers.items() if v == "granted"]
        kinds = sorted({e["kind"] for e in self.permission_events[p0:]})
        ok = len(answers) == 4 and not granted and len(kinds) >= 1 and len(self.watched_windows) == w0
        self.add("permissions", "Permission requests are refused, with no prompt", ok,
                 f"{len(self.permission_events) - p0} requests refused ({', '.join(kinds) or 'none reached the host'}); "
                 f"granted: {', '.join(granted) or 'none'}", page=answers, events=self.permission_events[p0:])
        d0 = len(self.downloads)
        downloads = Path(os.environ.get("USERPROFILE", "")) / "Downloads"
        before = sorted(p.name for p in downloads.glob("lag-doctor-spike*")) if downloads.is_dir() else []
        r = yield Page("download")
        after = sorted(p.name for p in downloads.glob("lag-doctor-spike*")) if downloads.is_dir() else []
        written = (self.run_dir / "refused-download.txt").exists() or after != before
        ok = bool(r) and len(self.downloads) > d0 and not written
        self.add("download", "Downloads are refused, and no file is written", ok,
                 f"{len(self.downloads) - d0} refused; a file was {'WRITTEN' if written else 'not written'}")
        g0, w0 = len(self.dialogs), len(self.watched_windows)
        r = yield Page("dialogs", timeout_ms=8000)
        ok = bool(r) and r.get("answer") is False and len(self.dialogs) > g0 and len(self.watched_windows) == w0
        self.add("dialogs", "Script dialogs never show", ok,
                 f"confirm() returned {r.get('answer') if r else 'nothing'} after "
                 f"{(r or {}).get('ms', 0):.0f} ms; {len(self.dialogs) - g0} dismissed by the host")
        appeared = self.watched_windows[self.watch_base:]
        self.add("windows", "No browser window, prompt or menu appeared during the page checks",
                 not appeared, f"{len(appeared)} seen", windows=appeared)

    def footprint(self, id_: str, when: str) -> None:
        snap = self.snapshotter.snapshot()
        tree = process_tree(snap, self.browser_pid)
        ws = sum(p.ws_bytes for p in tree.values())
        private = sum(p.private_bytes for p in tree.values())
        me = snap.get(os.getpid())
        self.info(id_, f"Footprint {when}",
                  f"{len(tree)} WebView2 processes, {ws / 2**20:.0f} MB working set, {private / 2**20:.0f} MB private; "
                  f"the Python process {me.ws_bytes / 2**20:.0f} MB" if me else f"{len(tree)} WebView2 processes",
                  processes=len(tree), ws_mb=round(ws / 2**20, 1), private_mb=round(private / 2**20, 1))

    def check_suspend(self):
        """Hidden, then suspended, 3 s each. The control is the hidden stretch: Chromium slows a hidden page's
        timers (to about once a second) but still runs them, so only a suspension stops them."""
        if not self.webview3:
            self.add("suspend", "The view hides, suspends and resumes", False, "ICoreWebView2_3 missing")
            return
        yield Page("ticks_start")
        yield Sleep(300)
        r0 = yield Page("ticks_read")
        self.controller.put_bool("put_IsVisible", False)
        yield Sleep(3000)
        r1 = yield Page("ticks_read")

        def start(resume):
            h = self.handler("ICoreWebView2TrySuspendCompletedHandler", DONE_BOOL, lambda hr, ok: resume((hr, ok)))
            self.webview3.do("TrySuspend", (ctypes.c_void_p,), h.ptr)
        result = yield Callback(start, 10_000)
        suspended = self.webview3.get_bool("get_IsSuspended")
        yield Sleep(3000)
        self.webview3.do("Resume")
        r2 = yield Page("ticks_read")
        self.controller.put_bool("put_IsVisible", True)
        hidden = (r1 or {}).get("ticks", 0) - (r0 or {}).get("ticks", 0)
        frozen = (r2 or {}).get("ticks", 0) - (r1 or {}).get("ticks", 0)
        ok = (bool(result and result[0] >= 0 and result[1]) and suspended and None not in (r0, r1, r2)
              and hidden >= 2 and frozen <= 1)
        self.add("suspend", "The view hides, suspends and resumes", ok,
                 f"TrySuspend {'succeeded' if result and result[1] else 'failed'}, suspended={suspended}; the page's "
                 f"50 ms timer ran {frozen} times in 3 s suspended, against {hidden} in 3 s only hidden (the control)"
                 + ("" if hidden >= 2 else ": the control did not run, so this cannot tell the two apart"))

    def check_watch_control(self):
        """The window watch must see a browser window that is really there: DevTools, opened on purpose and closed.
        First with DevTools turned off (does the API obey the setting?), then on, for the control itself."""
        s = self.settings["ICoreWebView2Settings"]
        w0 = len(self.watched_windows)
        self.webview.do("OpenDevToolsWindow")
        off_opened = yield Until(lambda: len(self.watched_windows) > w0, timeout_ms=2500)
        if not off_opened:
            s.put_bool("put_AreDevToolsEnabled", True)
            self.webview.do("OpenDevToolsWindow")
            seen = yield Until(lambda: len(self.watched_windows) > w0, timeout_ms=8000)
        else:
            seen = True
        new = self.watched_windows[w0:]
        for w in new:
            PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
        gone = yield Until(lambda: not any(IsWindowVisible(w["hwnd"]) for w in new), timeout_ms=5000)
        s.put_bool("put_AreDevToolsEnabled", False)
        off_again = s.get_bool("get_AreDevToolsEnabled") is False
        yield Sleep(500)
        self.watch_base = len(self.watched_windows)
        self.add("watch", "The window watch sees a browser window (the control: DevTools opened on purpose)",
                 bool(seen) and bool(gone) and off_again,
                 f"{'seen' if seen else 'NOT seen'} and {'closed' if gone else 'still open'}; with DevTools turned off "
                 f"the API {'still opened it' if off_opened else 'opened nothing'}", windows=new)

    def worker_tally(self, spans: list) -> dict:
        """Each worker's posts, split by whether they were posted while one of Windows' move loops ran."""
        def inside(t):
            return any(a <= t <= (b if b is not None else float("inf")) for a, b in spans)
        out = {}
        for kind in ("window", "thread"):
            posts, got = list(self.worker_posts[kind]), self.worker_got[kind]
            inn = [s for s, t in posts if inside(t)]
            outn = [s for s, t in posts if not inside(t)]
            out[kind] = {"in_loop": len(inn), "in_loop_arrived": sum(1 for s in inn if s in got),
                         "outside": len(outn), "outside_arrived": sum(1 for s in outn if s in got)}
        return out

    def check_modal_loop(self):
        """Windows' own move loop, entered through SC_MOVE and left with Esc: the modal loop a drag runs, without a
        person. Every result posted to the window while it runs must arrive; the control, posted to the thread
        meanwhile, must lose some. Keyboard move mode puts the mouse pointer on the title bar, so the spike puts it
        back. A loop that something else ended before the spike's Esc (a click, a key) proves nothing: NOT RUN."""
        self.drag.update(entered=0, exited=0, seconds=0.0, _t=None)
        span0 = len(self.loop_spans)
        point, mid = wintypes.POINT(), wintypes.POINT()
        had_point = bool(GetCursorPos(ctypes.byref(point)))
        self.start_workers()
        yield Sleep(300)
        PostMessageW(self.hwnd, WM_SYSCOMMAND, SC_MOVE, 0)
        esc = {"t": None, "pointer_moved": None}

        def peek():
            if had_point and GetCursorPos(ctypes.byref(mid)):
                esc["pointer_moved"] = (mid.x, mid.y) != (point.x, point.y)

        def press_esc():
            esc["t"] = time.perf_counter()
            PostMessageW(self.hwnd, WM_KEYDOWN, VK_ESCAPE, 0x00010001)

        def force_end():                       # if Esc did not end it, losing the capture does
            if self.drag["entered"] > self.drag["exited"]:
                self.log("the move loop ignored Esc; releasing the capture")
                ReleaseCapture()
                PostMessageW(self.hwnd, WM_CANCELMODE, 0, 0)
        self.after(1000, peek)
        self.after(2500, press_esc)
        self.after(6000, force_end)
        ended = yield Until(lambda: self.drag["exited"] >= 1, timeout_ms=12_000)
        if had_point:
            SetCursorPos(point.x, point.y)
        yield Sleep(300)
        self.workers_stop.set()
        yield Sleep(400)
        spans = self.loop_spans[span0:]
        tally = self.worker_tally(spans)
        w, t, d = tally["window"], tally["thread"], self.drag
        detail = dict(tally=tally, seconds=round(d["seconds"], 2), pointer_moved=esc["pointer_moved"])
        if spans and spans[0][1] is not None and (esc["t"] is None or spans[0][1] < esc["t"]):
            self.add("modal", "Worker results posted to the window survive Windows' move loop", None,
                     f"the move loop ended after {d['seconds']:.1f} s, before the spike's Esc (a click or a key?)",
                     **detail)
            return
        ok = (bool(ended) and d["seconds"] >= 1.0 and w["in_loop"] > 0 and w["in_loop_arrived"] == w["in_loop"]
              and w["outside_arrived"] == w["outside"] and t["in_loop"] > 0 and t["in_loop_arrived"] < t["in_loop"])
        self.add("modal", "Worker results posted to the window survive Windows' move loop", ok,
                 f"{d['seconds']:.1f} s in the move loop; posted to the window during it, {w['in_loop_arrived']} of "
                 f"{w['in_loop']} arrived; the control, posted to the thread during it, {t['in_loop_arrived']} of "
                 f"{t['in_loop']}" + ("" if t["in_loop_arrived"] < t["in_loop"] else
                                      " (the control lost nothing, so this run proves nothing)"), **detail)

    def check_recovery(self):
        before = self.ready_count
        old, created = self.browser_pid, getattr(self, "browser_created", None)
        snap = self.snapshotter.snapshot()
        if old not in snap or created is None or snap[old].create_time != created:
            self.add("recovery", "A killed browser process is recovered within 10 s", False,
                     "the browser process the view started is not the one running now, so nothing was ended")
            return
        t = time.perf_counter()
        out = yield Work(lambda: subprocess.run(["taskkill", "/PID", str(old), "/F"], capture_output=True,
                                                text=True, timeout=15, creationflags=CREATE_NO_WINDOW))
        ok = yield Until(lambda: self.ready_count > before, timeout_ms=int(RECOVERY_LIMIT_S * 1000) + 5000)
        took = time.perf_counter() - t
        good = bool(ok) and took <= RECOVERY_LIMIT_S and self.browser_pid != old
        self.add("recovery", "A killed browser process is recovered within 10 s", good,
                 f"page back after {took:.2f} s" if ok else f"not back after {took:.1f} s",
                 taskkill=getattr(out, "stdout", str(out)).strip()[:200], failures=self.process_failures,
                 exits=self.browser_exits)

    # ---------------------------------------------------------------- owner steps

    def owner_checks(self):
        say("\nFour short steps need you now; the window shows each one.")
        self.status("Four short steps need you: follow the box below.")
        yield from self.owner_drag()
        yield from self.owner_keys()
        yield from self.owner_dpi()
        yield from self.owner_taskbar()

    def start_workers(self) -> None:
        """Two threads post a numbered message every 5 ms: one to the window, one to the UI thread (the control)."""
        self.workers_stop.clear()
        self.worker_posts = {"window": [], "thread": []}
        self.worker_got = {"window": set(), "thread": set()}
        hwnd, tid = self.hwnd, self.ui_thread

        def run(kind):
            seq = 0
            while not self.workers_stop.is_set():
                seq += 1
                t = time.perf_counter()
                posted = (PostMessageW(hwnd, WM_APP_WORKER, seq, 0) if kind == "window" else
                          PostThreadMessageW(tid, WM_APP_THREAD, seq, 0))
                if posted:
                    self.worker_posts[kind].append((seq, t))
                time.sleep(0.005)
        for kind in ("window", "thread"):
            threading.Thread(target=run, args=(kind,), daemon=True, name=f"spike-worker-{kind}").start()

    def owner_drag(self):
        self.drag.update(entered=0, exited=0, seconds=0.0, _t=None)
        span0 = len(self.loop_spans)
        self.start_workers()
        ans = yield Owner("Step 1 of 4: drag the window",
                          "Hold this window's title bar (the strip along its very top) and drag the window around "
                          "for about five seconds, then let go and click Done.", ["Done", "Skip"])
        self.workers_stop.set()
        yield Sleep(400)                                     # the last posts arrive
        d = self.drag
        if ans != "Done" or d["seconds"] < 1.0:
            self.add("drag", "Worker results posted to the window survive a drag", None,
                     ("skipped" if ans == "Skip" else f"no answer ({ans})") if ans != "Done" else
                     f"no drag seen ({d['seconds']:.1f} s in the move loop)")
            return
        tally = self.worker_tally(self.loop_spans[span0:])
        w, t = tally["window"], tally["thread"]
        ok = (w["in_loop"] > 0 and w["in_loop_arrived"] == w["in_loop"] and w["outside_arrived"] == w["outside"]
              and t["in_loop"] > 0 and t["in_loop_arrived"] < t["in_loop"])
        self.add("drag", "Worker results posted to the window survive a drag", ok,
                 f"{d['seconds']:.1f} s dragged; posted to the window while dragging, {w['in_loop_arrived']} of "
                 f"{w['in_loop']} arrived; the control, posted to the thread meanwhile, {t['in_loop_arrived']} of "
                 f"{t['in_loop']}" + ("" if t["in_loop_arrived"] < t["in_loop"] else
                                      " (the control lost nothing, so this run proves nothing)"), tally=tally)

    def owner_keys(self):
        yield Page("keys_begin")
        zoom0 = self.controller.get_double("get_ZoomFactor")
        n0, z0, w0, a0, inst = (len(self.nav_events), self.zoom_events, len(self.watched_windows),
                                len(self.accel_keys), self.instance)
        ans = yield Owner("Step 2 of 4: keys and right-click",
                          "Click on this text, then press each of these: Ctrl+R, Ctrl+P, Ctrl+F, Ctrl+S, "
                          "Ctrl+Shift+I, Ctrl and +, Ctrl and -, and Alt with the left arrow. Then turn the mouse "
                          "wheel while holding Ctrl, and right-click anywhere. (No F-keys: on many laptops they "
                          "change hardware settings.) Did anything open (a print or save dialog, a find bar, "
                          "DevTools, a menu), or did the page zoom or reload?",
                          ["Nothing happened", "Something happened", "Skip"])
        r = yield Page("keys_end")
        zoom1 = self.controller.get_double("get_ZoomFactor") if self.controller else None
        name = "Browser keys, DevTools, the context menu and zoom do nothing"
        if ans not in ("Nothing happened", "Something happened"):
            self.add("keys", name, None, "skipped" if ans == "Skip" else f"no answer ({ans})")
            return
        navs = self.nav_events[n0:]
        windows = self.watched_windows[w0:]
        detail = dict(navigations=navs, windows=windows, accelerator_events=len(self.accel_keys) - a0,
                      keys=(r or {}).get("keys", []))
        if r is None or r.get("instance") != inst:
            self.add("keys", name, False, "the page reloaded or stopped answering during the step", **detail)
            return
        changed = (zoom1 != zoom0 or self.zoom_events != z0 or any(e["allowed"] for e in navs) or bool(windows))
        combos = set(r.get("keys", []))
        reached = [label for key, label in OWNER_KEYS.items() if key in combos]
        missing = [label for key, label in OWNER_KEYS.items() if key not in combos]
        menus = r.get("context_menus", 0)
        tried = len(reached) >= 6 and menus >= 1
        if not changed and ans == "Nothing happened" and not tried:
            self.add("keys", name, None, f"too few of the keys reached the page ({len(reached)} of {len(OWNER_KEYS)}, "
                     f"{menus} right-clicks; missing: {', '.join(missing) or 'none'})", **detail)
            return
        self.add("keys", name, ans == "Nothing happened" and not changed,
                 f"you saw {ans.lower()}; {len(reached)} of {len(OWNER_KEYS)} keys reached the page"
                 f"{' (missing: ' + ', '.join(missing) + ')' if missing else ''} and {menus} right-clicks; zoom "
                 f"{zoom0:g} to {zoom1:g}; {len(navs)} navigations, {sum(e['allowed'] for e in navs)} allowed; "
                 f"{len(windows)} browser windows seen", **detail)

    def owner_dpi(self):
        e0, m0, s0 = len(self.dpi_events), self.monitor_moves, len(self.dpi_samples)
        start_dpi = GetDpiForWindow(self.hwnd)
        start_monitor = MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST)
        start = self.view_state()                           # read in every mode, rehearsals included
        ans = yield Owner("Step 3 of 4: display scale or a second monitor",
                          "If you have a second monitor, drag this window onto it, wait two seconds, and drag it "
                          "back. Otherwise open Settings, System, Display, set Scale to the next value, wait two "
                          "seconds, and set it back to what it was. Then click Done. Skip if you'd rather not.",
                          ["Done", "Skip"])
        yield Sleep(1700)                                    # the last change's reading comes 1.5 s after it
        events, samples = self.dpi_events[e0:], self.dpi_samples[s0:]
        end_dpi = GetDpiForWindow(self.hwnd)
        reminder = ""
        if MonitorFromWindow(self.hwnd, MONITOR_DEFAULTTONEAREST) == start_monitor and end_dpi != start_dpi:
            reminder = (f"; your display scale is now {round(end_dpi / 96 * 100)}% and was "
                        f"{round(start_dpi / 96 * 100)}%: set it back in Settings, System, Display")
            say(f"\nYour display scale is now {round(end_dpi / 96 * 100)}%; it was {round(start_dpi / 96 * 100)}% "
                f"before this step. Set it back in Settings, System, Display.")
        name = "The view and the page follow display-scale changes and monitor moves"
        detail = dict(start=start, events=events, samples=samples)
        if ans != "Done" or (not events and self.monitor_moves == m0):
            self.add("dpi", name, None, (("skipped" if ans == "Skip" else f"no answer ({ans})") if ans != "Done"
                                         else "no scale change or monitor move was seen") + reminder, **detail)
            return
        at_new_scale = [s for s in samples if s["dpi"] != start_dpi]
        ok = bool(samples) and all(s["ok"] for s in samples) and (bool(at_new_scale) or not events)
        self.add("dpi", name, ok,
                 f"{len(events)} scale changes and {self.monitor_moves - m0} monitor moves; "
                 f"{sum(s['ok'] for s in samples)} of {len(samples)} readings had the view and the page at the "
                 f"window's scale ({len(at_new_scale)} read while a new scale was on)" + reminder, **detail)

    def owner_taskbar(self):
        p = ctypes.c_void_p()
        hr = GetCurrentProcessExplicitAppUserModelID(ctypes.byref(p))
        aumid = take_wstr(p) if hr == S_OK else None
        ans = yield Owner("Step 4 of 4: the taskbar",
                          "Look at this window's button on the taskbar. Does it show an orange square with a white "
                          "L, rather than the Python logo?", ["Yes, the orange L", "No, something else", "Skip"])
        self.add("taskbar", "The taskbar shows the spike's own icon",
                 None if ans not in ("Yes, the orange L", "No, something else") else ans.startswith("Yes"),
                 f"you answered: {ans}", app_id=aumid)

    # ---------------------------------------------------------------- window watch (background)

    def start_window_watch(self) -> None:
        self.watch_stop.clear()

        def watch():
            pids: set[int] = set()
            last = 0.0
            seen: set[int] = set()
            while not self.watch_stop.is_set():
                try:
                    if time.monotonic() - last > 0.5:
                        pids = set(process_tree(self.snapshotter.snapshot(), self.browser_pid)) if self.browser_pid else set()
                        last = time.monotonic()
                    for w in visible_windows(pids, skip=self.hwnd):
                        if w["hwnd"] not in seen:              # each window counts once, by its handle
                            seen.add(w["hwnd"])
                            w["t"] = round(time.perf_counter() - self.t0, 3)
                            self.watched_windows.append(w)
                            self.log(f"a browser window appeared: {w}")
                except Exception:  # noqa: BLE001
                    self.log_exception("the window watch")
                time.sleep(0.1)
        threading.Thread(target=watch, daemon=True, name="spike-window-watch").start()

    def stop_window_watch(self) -> None:
        self.watch_stop.set()

    # ---------------------------------------------------------------- the child scripts

    def script_pythonw(self):
        r1 = yield Page("raise_then_ping", timeout_ms=15_000)
        r2 = yield Page("raise_unwrapped_then_ping", timeout_ms=15_000)
        self.child_result = {"wrapped": r1, "unwrapped": r2, "unwrapped_raised": self.raw_raised,
                             "default_unraisablehook": sys.unraisablehook is sys.__unraisablehook__}
        self.begin_close()

    def script_droprefs(self):
        """The sabotage. A baseline first: the same handler, kept, must answer every ping, or a silent result after
        the drop would prove nothing. Then the handler's last reference is dropped and its memory reused."""
        yield Sleep(300)
        result_file = self.run_dir / "droprefs.json"

        def answer(sender, args):
            a = Com(args, "ICoreWebView2WebMessageReceivedEventArgs", owned=False)
            msg = json.loads(a.get_str("get_WebMessageAsJson") or "null")
            if isinstance(msg, dict) and msg.get("type") == "ping2":
                self.to_page({"type": "pong2", "seq": msg.get("seq")})
        kept = self.handler("ICoreWebView2WebMessageReceivedEventHandler", EVENT, answer)
        token = ctypes.c_int64()
        self.webview.do("add_WebMessageReceived", (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)), kept.ptr,
                        ctypes.byref(token))
        baseline = yield Page("ping2", {"count": 20}, timeout_ms=15_000)
        self.webview.do("remove_WebMessageReceived", (ctypes.c_int64,), token.value)
        result_file.write_text(json.dumps({"pid": os.getpid(), "stage": "about to drop the handler",
                                           "baseline": baseline}), "utf-8")
        h = self.handler("ICoreWebView2WebMessageReceivedEventHandler", EVENT, answer, keep=False)
        self.webview.do("add_WebMessageReceived", (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)), h.ptr,
                        ctypes.byref(token))
        del h                                             # the sabotage: nothing keeps this handler alive now
        gc.collect()
        self.junk = [ctypes.create_string_buffer(b"A" * n) for n in (15, 31, 47, 63, 95, 127, 191, 255)
                     for _ in range(4000)]               # reuse the freed memory, so a call through it fails loudly
        r = yield Page("ping2", {"count": 20}, timeout_ms=15_000)
        result_file.write_text(json.dumps({"pid": os.getpid(), "stage": "survived", "baseline": baseline,
                                           "report": r}), "utf-8")
        self.begin_close()

    # ---------------------------------------------------------------- closing

    def _quietly(self, what: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - each step on its own: one failure must not keep the window open
            self.fault(what, exc)

    def begin_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.close_started = time.perf_counter()
        self.workers_stop.set()
        self.stop_window_watch()
        self.final_tree = {}
        try:
            self._quietly("the snapshot before closing", lambda: setattr(
                self, "final_tree", process_tree(self.snapshotter.snapshot(), self.browser_pid) if self.browser_pid
                else {}))
            for com in [*self.settings.values(), self.webview18, self.webview4, self.webview3, self.webview,
                        self.controller3]:
                self._quietly("releasing a view interface", com.release)
            if self.controller:
                self._quietly("closing the controller", lambda: self.controller.do("Close"))
                self._quietly("releasing the controller", self.controller.release)
            self._quietly("releasing the environment", self.env5.release)
            self._quietly("releasing the environment", self.env.release)
        finally:
            if self.hwnd:
                DestroyWindow(self.hwnd)

    def check_close(self) -> None:
        name = "Closing the window ends every WebView2 process within 5 s"
        pids = set(self.final_tree)
        if not pids:
            self.add("close", name, False, "no WebView2 process was known at close")
            return
        deadline = self.close_started + CLOSE_LIMIT_S + 10
        left = set(pids)
        exits: dict[int, float] = {}
        while time.perf_counter() < deadline:
            snap = self.snapshotter.snapshot()
            now = round(time.perf_counter() - self.close_started, 2)
            still = {p for p in left if p in snap and snap[p].create_time == self.final_tree[p].create_time}
            for p in left - still:
                exits[p] = now
            left = still
            if not left:
                break
            time.sleep(0.05)
        took = time.perf_counter() - self.close_started
        order = sorted(((exits.get(p), p == self.browser_pid) for p in pids), key=lambda x: (x[0] is None, x[0] or 0))
        self.add("close", name, not left and took <= CLOSE_LIMIT_S,
                 (f"all {len(pids)} gone {took:.2f} s after closing" if not left else
                  f"{len(left)} of {len(pids)} still running {took:.1f} s after closing")
                 + f" (the browser process at {exits.get(self.browser_pid, 'never')} s)",
                 exits=[{"s": s, "browser": b} for s, b in order])

    def check_fatal(self) -> None:
        """An access violation anywhere in this process, even one ctypes caught and a guard logged, fails the run."""
        log = (self.run_dir / "spike.log").read_text("utf-8", errors="replace")
        seen = log.count("Windows fatal exception")
        self.add("fatal", "No access violation in the spike's own process", not seen and not self.fatal_events,
                 f"{seen} logged by faulthandler, {len(self.fatal_events)} caught in a callback",
                 caught=self.fatal_events)


# ------------------------------------------------------------------------------------------------ plumbing --

_quiet = False


def ms(value) -> str:
    return f"{value:.2f} ms" if isinstance(value, (int, float)) else "n/a"


def say(text: str) -> None:
    if not _quiet and sys.stdout is not None:
        print(text, flush=True)


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(p))


def prune_runs(work: Path, now: float | None = None) -> list[str]:
    """Bound what the runs leave in %TEMP% without losing their evidence.

    Only folders holding RUN_MARKER (the spike made them) are considered, links never, and the marker goes last.
    The newest KEEP_RUNS - 1 stay whole (the new run makes KEEP_RUNS). Older finished runs lose their WebView2
    data folders, most of their size, and keep results.json and the log. Past KEEP_EVIDENCE runs the oldest finished
    ones go entirely. A run with no results.json younger than LIVE_GRACE_S may still be running and is left alone."""
    now = time.time() if now is None else now
    runs = sorted((p for p in work.iterdir() if p.is_dir() and not _is_link(p) and (p / RUN_MARKER).is_file()),
                  key=lambda p: (p / RUN_MARKER).stat().st_mtime)

    def finished(p: Path) -> bool:
        return (p / "results.json").is_file() or now - (p / RUN_MARKER).stat().st_mtime > LIVE_GRACE_S

    done = []
    for p in runs[:max(0, len(runs) - (KEEP_RUNS - 1))]:
        if finished(p):
            for data in [p / "webview-data", *p.glob("child-*/webview-data")]:
                if data.is_dir() and not _is_link(data):
                    shutil.rmtree(data, ignore_errors=True)
                    done.append(str(data))
    for p in runs[:max(0, len(runs) - (KEEP_EVIDENCE - 1))]:
        if not finished(p):
            continue
        for child in p.iterdir():
            if child.name == RUN_MARKER or _is_link(child):
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        if [c.name for c in p.iterdir()] == [RUN_MARKER]:
            (p / RUN_MARKER).unlink()
            p.rmdir()
            done.append(str(p))
    return done


def new_run_dir(parent: Path | None, mode: str, tag: str = "") -> Path:
    if parent is not None:
        d = parent / f"child-{mode}{'-' + tag if tag else ''}"
        d.mkdir(parents=True, exist_ok=True)
        return d
    WORK.mkdir(parents=True, exist_ok=True)
    prune_runs(WORK)
    d = WORK / time.strftime(f"run-%Y%m%d-%H%M%S-{os.getpid()}")
    d.mkdir(parents=True, exist_ok=True)
    (d / RUN_MARKER).write_text("made by scripts/webview_spike.py\n", "utf-8")
    return d


def open_log(path: Path):
    f = open(path, "a", encoding="utf-8", buffering=1)
    lock = threading.Lock()

    def write(line: str) -> None:
        with lock:
            f.write(line + "\n")
    return f, write


def prepare(spike: Spike) -> int | None:
    """The runtime, the export, the environment variables and COM, before any window. None to go on, or an exit
    code."""
    rt = find_runtime()
    if rt is None:
        spike.add("runtime", "WebView2 Runtime found and signed by Microsoft", False,
                  "no WebView2 Runtime is registered on this PC")
        return 3
    spike.runtime = rt
    hardened = harden_env()                     # before the runtime's DLL is even loaded
    spike.env_before = hardened["before"]       # put back once the view exists (on_controller)
    env_ok = hardened["ok"] and hardened["reader_ok"] and hardened["os_environ_empty_is"] == "removed"
    spike.add("env", "WebView2's own environment variables are set in this process before the runtime loads", env_ok,
              f"{len(HARDENED_ENV)} set; the controls: the reader tells absent from empty: {hardened['reader_ok']}; "
              f"an empty os.environ value is {hardened['os_environ_empty_is']}",
              values=hardened["values"],
              before={k: ("absent" if v is None else "set") for k, v in hardened["before"].items()})
    sigs = [signature(rt["dll"]), signature(rt["exe"])] if Path(rt["dll"]).is_file() and Path(rt["exe"]).is_file() else []
    try:
        dll = ctypes.WinDLL(rt["dll"])
        spike.create_fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_bool, ctypes.c_int, wintypes.LPCWSTR,
                                             ctypes.c_void_p, ctypes.c_void_p)((EXPORT, dll))
        export_found = True
    except (OSError, AttributeError) as exc:
        spike.log(f"loading {rt['dll']}: {exc}")
        export_found = False
    signed = len(sigs) == 2 and all(s["trusted"] and s["signer"] == MICROSOFT for s in sigs)
    ok = export_found and signed and rt["build"] >= MIN_BUILD
    spike.add("runtime", "WebView2 Runtime found and signed by Microsoft", ok,
              f"version {rt['version']}, export {'found' if export_found else 'MISSING'}, "
              f"signatures {', '.join((s['signer'] or 'none') + ('' if s['trusted'] else ' (untrusted)') for s in sigs) or 'not checked'}",
              runtime=rt, signatures=sigs)
    spike.mark("runtime")
    if not export_found:
        return 1
    hr = SetCurrentProcessExplicitAppUserModelID(AUMID)
    spike.log(f"app id set: 0x{hr & 0xFFFFFFFF:08X}")
    if not SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
        spike.log(f"DPI awareness not set: error {ctypes.get_last_error()}")
    hr = CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr < 0:
        spike.add("com", "COM single-threaded apartment", False, f"CoInitializeEx 0x{hr & 0xFFFFFFFF:08X}")
        return 1
    return None


def run_window(spike: Spike) -> None:
    snap = spike.snapshotter.snapshot()
    spike.prior_webview2 = sum(1 for p in snap.values() if p.name.lower() == "msedgewebview2.exe")
    write_site(spike.run_dir)
    spike.create_window()
    watchdog_s = 60

    def watchdog():
        if spike.ready_count == 0 and not spike.closing:
            spike.fail_startup(f"the page was not ready after {watchdog_s} s (stages reached: {', '.join(spike.marks)})")
    spike.after(watchdog_s * 1000, watchdog)
    try:
        spike.create_environment()
    except ComError as exc:
        spike.fail_startup(str(exc))
    try:
        spike.loop()
    except KeyboardInterrupt:
        spike.closed_early = True
        spike.begin_close()
        spike.loop()


def filetime_now() -> int:
    return int(time.time() * 1e7) + FILETIME_EPOCH


def run_child(kind: str, run_dir: Path, python: Path, tag: str = "") -> dict:
    """Run the pythonw or drop-refs child and read what it left. Never raises.

    Under pythonw.exe the child gets no standard handles at all, as a Start-menu shortcut starts it: given pipes (or
    NUL), pythonw's sys.stderr would be a real stream and the check would not be about pythonw. Under python.exe its
    stderr is captured."""
    child_dir = run_dir / f"child-{kind}{'-' + tag if tag else ''}"
    cmd = [str(python), str(Path(__file__).resolve()), "--child", kind, "--run-dir", str(run_dir)]
    if tag:
        cmd += ["--tag", tag]
    t = time.perf_counter()
    stderr = ""
    try:
        if python.name.lower() == "pythonw.exe":
            si = subprocess.STARTUPINFO()
            si.dwFlags = subprocess.STARTF_USESTDHANDLES
            si.hStdInput = si.hStdOutput = si.hStdError = 0
            proc = subprocess.run(cmd, startupinfo=si, timeout=CHILD_TIMEOUT_S)
        else:
            proc = subprocess.run(cmd, capture_output=True, timeout=CHILD_TIMEOUT_S, creationflags=CREATE_NO_WINDOW)
            stderr = proc.stderr.decode("utf-8", "replace")
        code = proc.returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        code = None
        stderr = f"{type(exc).__name__}: {exc}"
    took = round(time.perf_counter() - t, 2)
    data = {}
    for name in ("result.json", "droprefs.json"):
        f = child_dir / name
        if f.is_file():
            try:
                value = json.loads(f.read_text("utf-8"))
                data[name] = value if isinstance(value, dict) else {}
            except ValueError:
                data[name] = {}
    log = ""
    lf = child_dir / "spike.log"
    if lf.is_file():
        log = lf.read_text("utf-8", errors="replace")
    return {"exit_code": code, "seconds": took, "files": data, "log": log, "stderr": stderr,
            "log_tail": log[-3000:], "stderr_tail": stderr[-3000:]}


def end_orphans(child_pid: int, launched: int, snapshotter: ntquery.Snapshotter) -> list[str]:
    """WebView2 browser processes a crashed child left behind: wait for them to end, end them if they do not, and
    say so. A candidate is matched by pid, parent and a creation after the child was launched, and matched again by
    pid and creation time just before it is ended: a pid Windows has since reused is never touched."""
    snap = snapshotter.snapshot()
    roots = {p.pid: p.create_time for p in snap.values()
             if p.ppid == child_pid and p.name.lower() == "msedgewebview2.exe" and p.create_time >= launched}
    deadline = time.monotonic() + 10
    while roots and time.monotonic() < deadline:
        time.sleep(0.25)
        snap = snapshotter.snapshot()
        roots = {pid: ct for pid, ct in roots.items() if pid in snap and snap[pid].create_time == ct}
    ended = []
    for pid, ct in roots.items():
        snap = snapshotter.snapshot()
        if pid in snap and snap[pid].create_time == ct:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
            ended.append(str(pid))
    return ended


def child_checks(spike: Spike) -> None:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    name = "Under pythonw, a handler's exception is logged and the next message works"
    say("\nThree short windows open and close now: the last checks.")
    if not pythonw.is_file():
        spike.add("pythonw", name, None, f"no pythonw.exe beside {exe.name}")
    else:
        res = run_child("pythonw", spike.run_dir, pythonw)
        console = run_child("pythonw", spike.run_dir, exe, tag="console")     # the control, under python.exe
        data = res["files"].get("result.json", {})
        wrapped = data.get("wrapped") if isinstance(data.get("wrapped"), dict) else {}
        unwrapped = data.get("unwrapped") if isinstance(data.get("unwrapped"), dict) else {}
        log = res.pop("log")
        res.pop("stderr")
        console.pop("log")
        control_err = console.pop("stderr")
        logged = "deliberate exception in a message handler" in log
        raised = data.get("unwrapped_raised", 0)
        # Where an unwrapped callback's exception goes: sys.unraisablehook, which writes to sys.stderr. Under
        # python.exe that is the captured pipe (the control must show it there); under pythonw it is None, and
        # with the default hook the exception goes nowhere.
        control_seen = "Exception ignored" in control_err and "deliberate unwrapped exception" in control_err
        ok = (res["exit_code"] == 0 and logged and wrapped.get("received") == 3 and raised >= 1
              and unwrapped.get("received") == 3 and data.get("stderr_is_none") is True
              and data.get("default_unraisablehook") is True and control_seen)
        spike.add("pythonw", name, ok,
                  (f"the guarded handler's exception was {'logged' if logged else 'NOT logged'}, and the next "
                   f"messages arrived ({wrapped.get('received')}/3); an unguarded one raised {raised} time(s) with "
                   f"sys.stderr None: {data.get('stderr_is_none')}, so it went nowhere, and the next messages arrived "
                   f"({unwrapped.get('received')}/3); the control, the same raise under python.exe, "
                   f"{'reached' if control_seen else 'did NOT reach'} stderr"), child=res, control=console)
    if spike.stop_requested:
        spike.closed_early = True
        return
    launched = filetime_now()
    res = run_child("droprefs", spike.run_dir, exe)
    log = res.pop("log")
    stderr = res.pop("stderr")
    marker = res["files"].get("droprefs.json", {})
    baseline = marker.get("baseline") if isinstance(marker.get("baseline"), dict) else {}
    base_ok = baseline.get("count") == 20 and baseline.get("received") == 20
    # The evidence of a crash is the access violation itself (faulthandler's line, or ctypes' OSError around the
    # call that reached the freed handler), never a non-zero exit alone, which any Python error also gives.
    violation = "access violation" in log.lower() or "access violation" in stderr.lower()
    code = res["exit_code"]
    crashed = code not in (0, None) and marker.get("stage") == "about to drop the handler" and violation
    rep = marker.get("report") if marker.get("stage") == "survived" and isinstance(marker.get("report"), dict) else None
    silent = rep is not None and rep.get("count") == 20 and rep.get("received") == 0
    orphans = end_orphans(marker["pid"], launched, spike.snapshotter) if isinstance(marker.get("pid"), int) else []
    shown = f"0x{code & 0xFFFFFFFF:08X}" if isinstance(code, int) else str(code)
    spike.add("keepalive", "Dropping a handler's references breaks it (the sabotage fails as predicted)",
              base_ok and (crashed or silent),
              (f"kept, the handler answered {baseline.get('received')} of 20; dropped, " +
               (f"it ended its process with an access violation (exit code {shown})" if crashed else
                "it went silent (0 of 20) without a crash" if silent else
                f"it did not break ({rep.get('received') if rep else 'no report'} answered, exit {shown})")),
              child=res, orphans_ended=orphans)


def report(spike: Spike, run_dir: Path, ci: bool) -> int:
    expected = list(AUTO_IDS) if spike.mode == "main" else []
    if spike.mode == "main" and spike.interactive:
        at = expected.index("env-back") + 1
        expected[at:at] = OWNER_ORDER
    have = {r.id for r in spike.results}
    for missing in (i for i in expected if i not in have):
        spike.results.append(Result(missing, f"(the {missing} check)", "NOT RUN",
                                    "not reached: the run stopped before it" if spike.closed_early else "not reached"))
    fails = [r for r in spike.results if r.status == "FAIL"]
    not_run = [r for r in spike.results if r.status == "NOT RUN"]
    passed = sum(r.status == "PASS" for r in spike.results)
    if spike.closed_early:
        verdict = "CUT SHORT (the window closed, Ctrl+C or a fault, before the end)" + (
            f", with {len(fails)} failed" if fails else "")
    elif fails:
        verdict = "FAIL"
    elif spike.rehearse:
        verdict = "REHEARSAL (the owner's steps ran with automatic answers and checked nothing)"
    elif not_run:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS" if spike.interactive else "PASS (automated checks only)"
    out = {"mode": "rehearsal" if spike.rehearse else "interactive" if spike.interactive else "automated",
           "verdict": verdict, "marks": spike.marks, "results": [r.__dict__ for r in spike.results],
           "closed_early": spike.closed_early, "python": sys.version, "executable": sys.executable}
    (run_dir / "results.json").write_text(json.dumps(out, indent=1, default=str), "utf-8")
    say("\n" + "=" * 78)
    for r in spike.results:
        say(f"{r.status:8s} {r.name}: {r.measured}")
    say("=" * 78)
    say(f"{passed} passed, {len(fails)} failed, {len(not_run)} not run: {verdict}")
    say(f"Results and log: {run_dir}")
    if ci and os.environ.get("GITHUB_STEP_SUMMARY"):
        title = "a rehearsal of the owner's steps" if spike.rehearse else "the automated checks"
        lines = [f"## WebView2 window spike on the runner: {title}", "",
                 f"**{verdict}**: {passed} passed, {len(fails)} failed, {len(not_run)} not run", "",
                 "| result | check | measured |", "|---|---|---|"]
        for r in spike.results:
            lines.append(f"| {r.status} | {r.name} | {r.measured.replace('|', '/')} |")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if spike.closed_early:
        return 4
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    global _quiet, WORK
    ap = argparse.ArgumentParser(
        description="M0 (docs/LAUNCHER-PLAN.md): lag-doctor's own window, drawn by WebView2. With no option this is "
                    "the owner's run: about three minutes, with four steps that need a person.")
    ap.add_argument("--auto", action="store_true", help="the automated checks alone (agents and CI)")
    ap.add_argument("--ci", action="store_true", help="like --auto, and write GitHub's job summary")
    ap.add_argument("--rehearse", action="store_true",
                    help="walk the owner's steps with automatic answers (runs their code; checks nothing)")
    ap.add_argument("--work-dir", type=Path, help="where the run folders go (default %%TEMP%%\\lag-doctor-webview-spike)")
    ap.add_argument("--child", choices=("pythonw", "droprefs"), help=argparse.SUPPRESS)
    ap.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--tag", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child and not args.run_dir:
        ap.error("--child needs --run-dir")
    if args.work_dir:
        WORK = args.work_dir.resolve()
    mode = args.child or "main"
    interactive = mode == "main" and (args.rehearse or not (args.auto or args.ci))
    _quiet = mode != "main"
    run_dir = new_run_dir(args.run_dir if args.child else None, mode, args.tag)
    logf, log = open_log(run_dir / "spike.log")
    faulthandler.enable(file=logf, all_threads=True)
    if mode == "droprefs":
        SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)     # a crash here is the expected result
    spike = Spike(mode, run_dir, interactive, log)
    spike.rehearse = bool(args.rehearse)
    spike.log(f"mode {mode}, interactive {interactive}, rehearse {spike.rehearse}, python {sys.version.split()[0]} "
              f"at {sys.executable}")
    if mode == "main":
        # Ctrl+C sets a flag the window's heartbeat acts on: raised inside a ctypes callback it would be swallowed.
        signal.signal(signal.SIGINT, lambda *_: setattr(spike, "stop_requested", True))
        kind = ("a rehearsal of the owner's steps" if spike.rehearse else "your run" if interactive else
                "the automated checks only")
        say(f"lag-doctor window spike: {kind}. Results and log: {run_dir}")
        say("For about 40 seconds the checks run on their own: leave the mouse and keyboard alone. The window opens "
            "DevTools on purpose, goes blank for a few seconds, reloads, and moves itself for a moment; all of that "
            "is expected." + (" Then four short steps need you." if interactive and not spike.rehearse else ""))
    if mode != "main":
        # A child's crash is its result: let it end the process (exit 1, ctypes' OSError and faulthandler's line),
        # never catch it here.
        code = prepare(spike)
        if code is not None:
            return code
        run_window(spike)
        if mode == "pythonw":
            res = dict(getattr(spike, "child_result", {}) or {})
            res["stderr_is_none"] = sys.stderr is None
            (run_dir / "result.json").write_text(json.dumps(res), "utf-8")
        return 0
    code = None
    rc = 1
    try:
        code = prepare(spike)
        if code is None:
            run_window(spike)
            spike.check_close()
            if not spike.closed_early and not spike.stop_requested:
                child_checks(spike)
            spike.check_fatal()
            CoUninitialize()
            for icon in spike.icons:
                DestroyIcon(icon)
    except Exception:  # noqa: BLE001 - the summary below must still be written
        spike.log_exception("the run")
        spike.add("script", "The spike ran to the end", False, "it raised; see the log")
    finally:
        rc = report(spike, run_dir, args.ci)
    return 3 if code == 3 else rc


if __name__ == "__main__":
    sys.exit(main())
