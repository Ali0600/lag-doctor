"""lag-doctor's window: one top-level window holding one WebView2 view, its policy, and the message loop.

Ported from M0's spike (scripts/webview_spike.py), keeping what its runs measured (docs/research/desktop-app.md,
section 11):
- every handler, and the environment's options object, stays referenced for the window's whole life (self.keep);
- every handler is guarded: nothing raises into C, and an access violation ctypes turned into an OSError closes the
  window with a message instead of carrying on with memory gone wrong;
- each refusing handler refuses first and reads the event afterwards, so a failure while reading leaves the refusal;
- WebView2's own environment variables are set only around the environment's creation, then put back;
- a browser process that ends is recovered once BrowserProcessExited says its data folder is free, never by retrying
  without the options object;
- workers post their results to the window, never to the thread (Windows' modal loops drop thread messages);
- the app never calls OpenDevToolsWindow (it opens DevTools even with DevTools turned off).
The page may only talk JSON with this window (bridge.py); it never reaches a file, a process or the network.
"""
from __future__ import annotations

import collections
import ctypes
import json
import queue
import threading
import traceback
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from lagdoctor.app import bridge
from lagdoctor.winapi import webview2 as wv
from lagdoctor.winapi.webview2 import Com, ComError, Handler

WINDOW_TITLE = "lag-doctor"
WM_APP_CALL = wv.WM_APP + 1          # run queued calls on the UI thread, outside any COM callback
HEARTBEAT_TIMER = 1                  # wakes the loop now and then, so a stop request is noticed
WINDOW_DIP = (1180, 800)             # the first size, in 96-DPI pixels
MIN_DIP = (720, 480)
BACKGROUND = (0xF6, 0xF4, 0xF1)      # the page's own background, shown before it paints
FAILED_RENDER_EXITED = 1             # COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_EXITED
WORKERS = 2                          # threads that load sessions: one capture load cannot hold up the library
MAX_QUEUED = 32                      # requests waiting for a worker before more are turned away

# Environment variables WebView2 reads itself, even without Microsoft's loader. Set, not removed: Wails' loader notes
# that WebView2 checks they exist, so an empty one also stops the matching registry override. Set only around the
# environment's creation and restored after, so processes started later inherit the user's own.
HARDENED_ENV = {
    "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER": "",
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "",
    "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE": "0",
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": "",
    "WEBVIEW2_USER_DATA_FOLDER": "",
}

# Every setting the view gets, and the value it must read back. Script and web messages on; everything a browser
# would offer beyond the page off.
SETTINGS = (
    ("ICoreWebView2Settings", "IsScriptEnabled", True),
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
    ("ICoreWebView2Settings8", "IsReputationCheckingRequired", False),
)

# The options object: exclusive use of the data folder, crash dumps kept on the PC (custom crash reporting).
OPTION_BOOLS = {"AllowSingleSignOnUsingOSPrimaryAccount": False, "ExclusiveUserDataFolderAccess": True,
                "IsCustomCrashReportingEnabled": True}


def harden_env() -> dict[str, str | None]:
    """Set WebView2's own variables in this process and return what each was (None: absent)."""
    before = {name: wv.env_state(name) for name in HARDENED_ENV}
    for name, value in HARDENED_ENV.items():
        wv.set_env(name, value)
    return before


def restore_env(before: dict[str, str | None]) -> bool:
    """Put each variable back as it was (an absent one is removed again); True when every one reads back so."""
    for name, value in before.items():
        wv.set_env(name, value)
    return all(wv.env_state(name) == value for name, value in before.items())


def is_access_violation(exc: BaseException) -> bool:
    """ctypes turns an access violation inside a call into an OSError whose text starts "exception": memory went
    wrong, which no guard may treat as an ordinary error (the spike's drop-refs check)."""
    return isinstance(exc, OSError) and not isinstance(exc, ComError) and str(exc).startswith("exception")


class AppWindow:
    """The window, its view and its loop. `on_message(request)` gets every checked message from the page, on the UI
    thread; `on_failed(why)` is told when the view could not be made at all; `log` writes a line to the app's log."""

    def __init__(self, *, runtime: dict, create_fn, data_folder: Path, assets: Path, icon: Path | None,
                 class_name: str, on_message: Callable[[bridge.Request], None], on_failed: Callable[[str], None],
                 log: Callable[[str], None], activate: bool = True) -> None:
        self.runtime = runtime
        self.create_fn = create_fn
        self.data_folder = data_folder
        self.assets = assets
        self.icon_path = icon
        self.class_name = class_name
        self.on_message = on_message
        self.on_failed = on_failed
        self.log = log
        self.activate = activate
        self.keep: list = []                  # every handler and options object WebView2 may call, for the whole life
        self.calls: collections.deque = collections.deque()
        self.calls_lock = threading.Lock()
        self.jobs: queue.Queue = queue.Queue()
        self.workers: list[threading.Thread] = []
        self.timers: dict[int, Callable[[], None]] = {}
        self.next_timer = 100
        self.hwnd = 0
        self.icons: list[int] = []
        self.closing = False
        self.stop_requested = False
        self.fatal: list[str] = []
        self.env = Com(0, "ICoreWebView2Environment")
        self.env5 = Com(0, "ICoreWebView2Environment5")
        self.env12 = Com(0, "ICoreWebView2Environment12")
        self.controller = Com(0, "ICoreWebView2Controller")
        self.controller3 = Com(0, "ICoreWebView2Controller3")
        self.webview = Com(0, "ICoreWebView2")
        self.webview3 = Com(0, "ICoreWebView2_3")
        self.webview4 = Com(0, "ICoreWebView2_4")
        self.webview17 = Com(0, "ICoreWebView2_17")
        self.webview18 = Com(0, "ICoreWebView2_18")
        self.settings: dict[str, Com] = {}
        self.settings_readback: dict[str, str] = {}
        self.env_before: dict | None = None
        self.env_restored: bool | None = None
        self.options: wv.EnvOptions | None = None
        self.options_asked: list[list[str]] = []
        self.generation = 0
        self.first_navigation = True
        self.recreating = False
        self.ready = False
        self.browser_pid = 0
        self.buffers: dict[int, Com] = {}     # shared buffers the page has not let go of yet, by token
        self.next_token = 1
        self.refused: collections.Counter = collections.Counter()   # what the policy turned away, by kind

    # ---------------------------------------------------------------- guarded entry points from C

    def fault(self, where: str, exc: BaseException) -> None:
        self.log(f"exception in {where}:\n{''.join(traceback.format_exception(exc))}")
        if is_access_violation(exc):
            self.fatal.append(f"{where}: {exc}")
            self.stop_requested = True

    def guard(self, name: str, fn):
        def call(this, a, b):
            try:
                fn(a, b)
                return wv.S_OK
            except Exception as exc:  # noqa: BLE001 - logged, then the handler returns normally
                self.fault(name, exc)
                return wv.E_FAIL
            except BaseException:  # noqa: BLE001 - Ctrl+C or exit cannot cross into C: stop at the next message
                self.stop_requested = True
                return wv.E_FAIL
        return call

    def handler(self, iface: str, proto, fn) -> Handler:
        h = Handler(iface, proto, fn, self.guard)
        self.keep.append(h)
        return h

    def add_event(self, com: Com, method: str, iface: str, fn) -> None:
        h = self.handler(iface, wv.EVENT, fn)
        token = ctypes.c_int64()
        com.do(method, (ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)), h.ptr, ctypes.byref(token))

    def call_soon(self, fn: Callable[[], None]) -> None:
        """Run fn on the UI thread from the message loop: never inside a COM callback. Safe from any thread."""
        with self.calls_lock:
            self.calls.append(fn)
        if self.hwnd:
            wv.PostMessageW(self.hwnd, WM_APP_CALL, 0, 0)

    def submit(self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None]) -> bool:
        """Run work() on one of WORKERS threads; done(result, error) runs on the UI thread afterwards, posted to the
        window (a thread message would be lost in a modal loop). False, and nothing runs, when MAX_QUEUED jobs
        already wait: a page cannot pile up work without bound."""
        if self.jobs.qsize() >= MAX_QUEUED:
            return False
        while len(self.workers) < WORKERS:
            t = threading.Thread(target=self._work, name=f"lagdoctor-app-worker-{len(self.workers) + 1}", daemon=True)
            self.workers.append(t)
            t.start()
        self.jobs.put((work, done))
        return True

    def _work(self) -> None:
        while True:
            work, done = self.jobs.get()
            try:
                result, error = work(), None
            except BaseException as exc:  # noqa: BLE001 - handed to done(), on the UI thread
                result, error = None, exc
            self.call_soon(lambda done=done, result=result, error=error: done(result, error))

    def after(self, ms: int, fn: Callable[[], None]) -> None:
        tid = self.next_timer
        self.next_timer += 1
        self.timers[tid] = fn
        wv.SetTimer(self.hwnd, tid, max(1, int(ms)), None)

    # ---------------------------------------------------------------- the window

    def load_icon(self, size: int) -> int:
        if self.icon_path is None or not self.icon_path.is_file():
            return 0
        icon = wv.LoadImageW(None, str(self.icon_path), wv.IMAGE_ICON, size, size, wv.LR_LOADFROMFILE)
        if icon:
            self.icons.append(icon)
        return icon or 0

    def create_window(self) -> None:
        self._wndproc = wv.WNDPROC(self.wndproc)
        hinst = wv.GetModuleHandleW(None)
        wc = wv.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wv.WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = wv.LoadCursorW(None, ctypes.c_void_p(wv.IDC_ARROW))
        r, g, b = BACKGROUND
        wc.hbrBackground = wv.CreateSolidBrush(r | (g << 8) | (b << 16))
        wc.lpszClassName = self.class_name
        if not wv.RegisterClassExW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        dpi = wv.GetDpiForSystem() or 96
        w, h = (v * dpi // 96 for v in WINDOW_DIP)
        self.hwnd = wv.CreateWindowExW(0, self.class_name, WINDOW_TITLE, wv.WS_OVERLAPPEDWINDOW, wv.CW_USEDEFAULT,
                                       wv.CW_USEDEFAULT, w, h, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        wdpi = wv.GetDpiForWindow(self.hwnd) or dpi
        big = self.load_icon(wv.GetSystemMetricsForDpi(wv.SM_CXICON, wdpi) or 32)
        small = self.load_icon(wv.GetSystemMetricsForDpi(wv.SM_CXSMICON, wdpi) or 16)
        if big:
            wv.SendMessageW(self.hwnd, wv.WM_SETICON, wv.ICON_BIG, big)
        if small:
            wv.SendMessageW(self.hwnd, wv.WM_SETICON, wv.ICON_SMALL, small)
        wv.ShowWindow(self.hwnd, wv.SW_SHOWNORMAL if self.activate else wv.SW_SHOWNOACTIVATE)
        if self.activate:
            wv.SetForegroundWindow(self.hwnd)
        wv.SetTimer(self.hwnd, HEARTBEAT_TIMER, 250, None)

    def client_rect(self) -> wintypes.RECT:
        rc = wintypes.RECT()
        wv.GetClientRect(self.hwnd, ctypes.byref(rc))
        return rc

    def wndproc(self, hwnd, msg, wp, lp):
        try:
            r = self.on_window_message(hwnd, msg, wp, lp)
            if r is not None:
                return r
        except Exception as exc:  # noqa: BLE001
            self.fault(f"window message 0x{msg:04X}", exc)
        except BaseException:  # noqa: BLE001
            self.stop_requested = True
        return wv.DefWindowProcW(hwnd, msg, wp, lp)

    def on_window_message(self, hwnd, msg, wp, lp):
        if msg == WM_APP_CALL:
            while True:
                with self.calls_lock:
                    if not self.calls:
                        break
                    fn = self.calls.popleft()
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self.fault("a queued call", exc)
            return 0
        if msg == wv.WM_TIMER:
            if wp == HEARTBEAT_TIMER:
                if self.stop_requested and not self.closing:
                    self.log("stop requested" + (f" after a fault: {self.fatal}" if self.fatal else ""))
                    self.begin_close()
                return 0
            fn = self.timers.pop(wp, None)
            wv.KillTimer(hwnd, wp)
            if fn:
                fn()
            return 0
        if msg == wv.WM_SIZE:
            if self.controller and not self.recreating:
                self.controller.do("put_Bounds", (wintypes.RECT,), self.client_rect())
            return 0
        if msg in (wv.WM_MOVE, wv.WM_MOVING):
            if self.controller and not self.recreating:
                self.controller.do("NotifyParentWindowPositionChanged")
            return None
        if msg == wv.WM_DPICHANGED:
            rc = wintypes.RECT.from_address(lp)
            wv.SetWindowPos(hwnd, None, rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top,
                            wv.SWP_NOZORDER | wv.SWP_NOACTIVATE)
            return 0
        if msg == wv.WM_SETFOCUS:
            if self.controller and not self.recreating and not self.closing:
                hr = self.controller.call("MoveFocus", (ctypes.c_int,), wv.MOVE_FOCUS_PROGRAMMATIC)
                if hr < 0:      # 0x8007139F while the browser process is gone: the view is closed, not broken
                    self.log(f"MoveFocus: 0x{hr & 0xFFFFFFFF:08X}")
            return 0
        if msg == wv.WM_GETMINMAXINFO:
            mmi = wv.MINMAXINFO.from_address(lp)
            dpi = wv.GetDpiForWindow(hwnd) or 96
            mmi.ptMinTrackSize.x, mmi.ptMinTrackSize.y = (v * dpi // 96 for v in MIN_DIP)
            return 0
        if msg == wv.WM_CLOSE:
            self.begin_close()
            return 0
        if msg == wv.WM_DESTROY:
            wv.PostQuitMessage(0)
            return 0
        return None

    def loop(self) -> None:
        msg = wintypes.MSG()
        while True:
            r = wv.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0:
                break
            if r == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            wv.TranslateMessage(ctypes.byref(msg))
            wv.DispatchMessageW(ctypes.byref(msg))

    def run(self) -> None:
        """Make the window and the view, then pump messages until the window closes."""
        self.create_window()
        try:
            self.create_environment()
        except (ComError, OSError) as exc:
            self.fail(f"WebView2 could not start ({exc})")
        try:
            self.loop()
        except KeyboardInterrupt:
            self.begin_close()
            self.loop()

    # ---------------------------------------------------------------- WebView2

    def create_environment(self) -> None:
        self.generation += 1
        self.options = wv.EnvOptions({"AdditionalBrowserArguments": "", "Language": "",
                                      "TargetCompatibleBrowserVersion": self.runtime["version"]}, OPTION_BOOLS)
        self.keep.append(self.options)                 # the runtime may hold it as long as the environment lives
        self.options_asked.append(self.options.asked)
        self.env_before = harden_env()                 # before the runtime reads them; put back once the view exists
        done = self.handler("ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler", wv.DONE_PTR,
                            self.on_environment)
        self.data_folder.mkdir(parents=True, exist_ok=True)
        hr = self.create_fn(True, wv.RUNTIME_INSTALLED, str(self.data_folder), self.options.ptr, done.ptr)
        self.log(f"{wv.EXPORT} returned 0x{hr & 0xFFFFFFFF:08X} (environment {self.generation})")
        if hr < 0:
            self.give_env_back()
            raise ComError(wv.EXPORT, hr)

    def give_env_back(self) -> None:
        if self.env_before is not None:
            self.env_restored = restore_env(self.env_before)
            self.env_before = None

    def on_environment(self, hr, env_ptr) -> None:
        if hr < 0 or not env_ptr:
            self.give_env_back()
            self.call_soon(lambda: self.fail(f"WebView2 did not start (HRESULT 0x{hr & 0xFFFFFFFF:08X})"))
            return
        self.env = Com(env_ptr, "ICoreWebView2Environment", owned=False).qi("ICoreWebView2Environment")
        self.env5 = self.env.qi("ICoreWebView2Environment5")
        self.env12 = self.env.qi("ICoreWebView2Environment12")
        if self.env5:
            self.add_event(self.env5, "add_BrowserProcessExited", "ICoreWebView2BrowserProcessExitedEventHandler",
                           self.on_browser_exited)
        done = self.handler("ICoreWebView2CreateCoreWebView2ControllerCompletedHandler", wv.DONE_PTR,
                            self.on_controller)
        self.env.do("CreateCoreWebView2Controller", (wintypes.HWND, ctypes.c_void_p), self.hwnd, done.ptr)

    def on_controller(self, hr, ctl_ptr) -> None:
        self.give_env_back()                           # the browser process exists: processes started later
        self.log(f"WebView2's environment variables put back as they were: {self.env_restored}")
        if hr < 0 or not ctl_ptr:
            self.call_soon(lambda: self.fail(f"WebView2's view was not made (HRESULT 0x{hr & 0xFFFFFFFF:08X})"))
            return
        self.controller = Com(ctl_ptr, "ICoreWebView2Controller", owned=False).qi("ICoreWebView2Controller")
        self.controller3 = self.controller.qi("ICoreWebView2Controller3")
        self.webview = self.controller.get_ptr("get_CoreWebView2", "ICoreWebView2")
        self.webview3 = self.webview.qi("ICoreWebView2_3")
        self.webview4 = self.webview.qi("ICoreWebView2_4")
        self.webview17 = self.webview.qi("ICoreWebView2_17")
        self.webview18 = self.webview.qi("ICoreWebView2_18")
        missing = [c.iface for c in (self.webview3, self.webview17, self.env12) if not c]
        if missing:
            self.call_soon(lambda: self.fail(f"this WebView2 Runtime is too old (it lacks {', '.join(missing)})"))
            return
        self.configure()
        self.first_navigation = True
        self.webview.do("Navigate", (wintypes.LPCWSTR,), bridge.ENTRY)

    def configure(self) -> None:
        wvw, ctl = self.webview, self.controller
        if self.controller3:
            r, g, b = BACKGROUND
            self.controller3.do("put_DefaultBackgroundColor", (wv.COREWEBVIEW2_COLOR,),
                                wv.COREWEBVIEW2_COLOR(255, r, g, b))
        s = wvw.get_ptr("get_Settings", "ICoreWebView2Settings")
        for old in self.settings.values():
            old.release()
        self.settings = {"ICoreWebView2Settings": s}
        for name in {iface for iface, _, _ in SETTINGS} - {"ICoreWebView2Settings"}:
            self.settings[name] = s.qi(name)
        self.settings_readback = {}
        for iface, prop, value in SETTINGS:
            com = self.settings[iface]
            if not com:
                self.settings_readback[prop] = "interface missing"
                continue
            com.put_bool("put_" + prop, value)
            back = com.get_bool("get_" + prop)
            self.settings_readback[prop] = "ok" if back == value else f"reads back {back}"
        wrong = {k: v for k, v in self.settings_readback.items() if v != "ok"}
        if wrong:
            self.log(f"settings that did not read back as set: {wrong}")
        self.add_event(wvw, "add_WebMessageReceived", "ICoreWebView2WebMessageReceivedEventHandler",
                       self.on_web_message)
        self.add_event(wvw, "add_NavigationStarting", "ICoreWebView2NavigationStartingEventHandler",
                       self.on_navigation_starting)
        self.add_event(wvw, "add_NavigationCompleted", "ICoreWebView2NavigationCompletedEventHandler",
                       self.on_navigation_completed)
        self.add_event(wvw, "add_FrameNavigationStarting", "ICoreWebView2NavigationStartingEventHandler",
                       self.on_frame_navigation_starting)
        self.add_event(wvw, "add_NewWindowRequested", "ICoreWebView2NewWindowRequestedEventHandler",
                       self.on_new_window)
        self.add_event(wvw, "add_PermissionRequested", "ICoreWebView2PermissionRequestedEventHandler",
                       self.on_permission)
        self.add_event(wvw, "add_ProcessFailed", "ICoreWebView2ProcessFailedEventHandler", self.on_process_failed)
        self.add_event(wvw, "add_ScriptDialogOpening", "ICoreWebView2ScriptDialogOpeningEventHandler",
                       self.on_script_dialog)
        if self.webview4:
            self.add_event(self.webview4, "add_DownloadStarting", "ICoreWebView2DownloadStartingEventHandler",
                           self.on_download)
        if self.webview18:
            self.add_event(self.webview18, "add_LaunchingExternalUriScheme",
                           "ICoreWebView2LaunchingExternalUriSchemeEventHandler", self.on_external_scheme)
        self.webview3.do("SetVirtualHostNameToFolderMapping", (wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int),
                         bridge.APP_HOST, str(self.assets), wv.ACCESS_DENY)
        ctl.do("put_Bounds", (wintypes.RECT,), self.client_rect())
        ctl.put_bool("put_IsVisible", True)
        self.browser_pid = self.webview.get_u32("get_BrowserProcessId")

    # ---------------------------------------------------------------- WebView2 events

    def on_web_message(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2WebMessageReceivedEventArgs", owned=False)
        source = a.get_str("get_Source")
        if not bridge.origin_ok(source):
            self.refused["message from another origin"] += 1
            self.log(f"refused a message from {source!r}")
            return
        req = bridge.parse(a.get_str("get_WebMessageAsJson"))
        if req is None:
            self.refused["malformed message"] += 1
            return
        if req.kind == "ready":
            self.ready = True
        elif req.kind == "frames_done":
            self.call_soon(lambda: self.close_buffer(req.args["token"]))
            return
        self.call_soon(lambda: self.on_message(req))

    def on_navigation_starting(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NavigationStartingEventArgs", owned=False)
        a.put_bool("put_Cancel", True)           # refused first: if anything below raises, the refusal stands
        first, self.first_navigation = self.first_navigation, False
        uri = a.get_str("get_Uri")
        if bridge.navigation_allowed(uri, first):
            a.put_bool("put_Cancel", False)
        else:
            self.refused["navigation"] += 1
            self.log(f"navigation refused: {uri!r}")

    def on_frame_navigation_starting(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NavigationStartingEventArgs", owned=False)
        a.put_bool("put_Cancel", True)           # the page has no frames: none may load
        self.refused["frame navigation"] += 1

    def on_navigation_completed(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NavigationCompletedEventArgs", owned=False)
        ok = a.get_bool("get_IsSuccess")
        if not ok:
            self.log(f"navigation did not complete: web error {a.get_int('get_WebErrorStatus')}")

    def on_new_window(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2NewWindowRequestedEventArgs", owned=False)
        a.put_bool("put_Handled", True)          # handled with no new window: window.open returns null
        self.refused["new window"] += 1

    def on_permission(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2PermissionRequestedEventArgs", owned=False)
        a.do("put_State", (ctypes.c_int,), wv.PERMISSION_DENY)      # refused first, read afterwards
        self.refused["permission"] += 1
        self.log(f"permission refused: kind {a.get_int('get_PermissionKind')}")

    def on_download(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2DownloadStartingEventArgs", owned=False)
        a.put_bool("put_Cancel", True)
        a.put_bool("put_Handled", True)
        self.refused["download"] += 1

    def on_external_scheme(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2LaunchingExternalUriSchemeEventArgs", owned=False)
        a.put_bool("put_Cancel", True)
        self.refused["external scheme"] += 1

    def on_script_dialog(self, sender, args) -> None:
        self.refused["script dialog"] += 1       # never Accept: with the default dialogs off it is dismissed

    def on_process_failed(self, sender, args) -> None:
        a = Com(args, "ICoreWebView2ProcessFailedEventArgs", owned=False)
        kind = a.get_int("get_ProcessFailedKind")
        self.log(f"a WebView2 process failed, kind {kind}")
        if self.closing:
            return
        if kind == wv.FAILED_BROWSER_EXITED and not self.recreating:
            self.recreating = True
            self.ready = False
            self.call_soon(self.drop_view)
            # Recreated on BrowserProcessExited, which comes once the exclusive data folder is free; only a runtime
            # without that event gets a timer instead.
            if not self.env5:
                self.after(3000, self.recreate_if_waiting)
        elif kind == FAILED_RENDER_EXITED and not self.recreating:
            self.ready = False
            self.first_navigation = True         # the page is gone: load the entry page again, nothing else
            self.call_soon(lambda: self.webview.do("Navigate", (wintypes.LPCWSTR,), bridge.ENTRY))

    def on_browser_exited(self, sender, args) -> None:
        if self.recreating:
            self.call_soon(self.recreate_if_waiting)

    def release_view(self) -> None:
        for token in list(self.buffers):
            self.close_buffer(token)
        for com in [*self.settings.values(), self.webview18, self.webview17, self.webview4, self.webview3,
                    self.webview, self.controller3]:
            com.release()
        self.settings = {}

    def drop_view(self) -> None:
        self.release_view()
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
        for com in (self.env12, self.env5, self.env):
            com.release()
        self.log("making the environment again after the browser process ended")
        try:
            self.create_environment()
        except (ComError, OSError) as exc:
            self.fail(f"WebView2 could not start again ({exc})")

    # ---------------------------------------------------------------- talking to the page

    def post(self, obj: dict) -> bool:
        """Post to the page; False when no live view can take it."""
        if self.webview and not self.recreating and not self.closing:
            try:
                self.webview.do("PostWebMessageAsJson", (wintypes.LPCWSTR,), json.dumps(obj, allow_nan=False))
                return True
            except (ComError, ValueError) as exc:
                self.log(f"posting to the page: {exc}")
        return False

    def post_buffer(self, data: bytes, meta: dict) -> int | None:
        """Hand the page `data` read-only in a shared buffer, with `meta` (plus its token) as the event's additional
        data. The token names the buffer until the page lets go of it (a frames_done notice) or the view goes."""
        if not (self.webview17 and self.env12) or self.recreating or self.closing or not data:
            return None
        p = ctypes.c_void_p()
        self.env12.do("CreateSharedBuffer", (ctypes.c_uint64, ctypes.POINTER(ctypes.c_void_p)), len(data),
                      ctypes.byref(p))
        buf = Com(p.value, "ICoreWebView2SharedBuffer")
        token = self.next_token
        self.next_token += 1
        self.buffers[token] = buf
        try:
            base = ctypes.c_void_p()
            buf.do("get_Buffer", (ctypes.POINTER(ctypes.c_void_p),), ctypes.byref(base))
            if buf.get_u64("get_Size") < len(data) or not base.value:
                raise ComError("ICoreWebView2SharedBuffer is smaller than asked", wv.E_OUTOFMEMORY)
            ctypes.memmove(base.value, data, len(data))
            self.webview17.do("PostSharedBufferToScript", (ctypes.c_void_p, ctypes.c_int, wintypes.LPCWSTR),
                              buf.ptr, wv.SHARED_BUFFER_READ_ONLY, json.dumps(dict(meta, token=token)))
        except Exception:
            self.close_buffer(token)
            raise
        return token

    def close_buffer(self, token: int) -> None:
        buf = self.buffers.pop(token, None)
        if buf is None:
            return
        try:
            buf.do("Close")
        except ComError as exc:
            self.log(f"closing a shared buffer: {exc}")
        buf.release()

    def execute_script(self, script: str, done: Callable[[int, str | None], None]) -> None:
        """The host's own script hook: run `script` in the page and hand done(hresult, result as JSON) its answer, on
        the UI thread. The app's own tests drive the page with it (WebDriver for WebView2 is broken since runtime
        133); the app itself never runs script it did not ship."""
        h = self.handler("ICoreWebView2ExecuteScriptCompletedHandler", wv.DONE_STR,
                         lambda hr, result: self.call_soon(lambda: done(hr, result)))
        self.webview.do("ExecuteScript", (wintypes.LPCWSTR, ctypes.c_void_p), script, h.ptr)

    # ---------------------------------------------------------------- closing

    def fail(self, why: str) -> None:
        """The view could not be made: say so through on_failed, once, and close."""
        if self.closing:
            return
        self.log(f"the window failed: {why}")
        try:
            self.on_failed(why)
        finally:
            self.begin_close()

    def _quietly(self, what: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - each step on its own: one failure must not keep the window open
            self.fault(what, exc)

    def begin_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        try:
            self._quietly("releasing the view", self.release_view)
            if self.controller:
                self._quietly("closing the controller", lambda: self.controller.do("Close"))
                self._quietly("releasing the controller", self.controller.release)
            for com in (self.env12, self.env5, self.env):
                self._quietly("releasing the environment", com.release)
        finally:
            if self.hwnd:
                wv.DestroyWindow(self.hwnd)

    def destroy_icons(self) -> None:
        for icon in self.icons:
            wv.DestroyIcon(icon)
        self.icons = []
