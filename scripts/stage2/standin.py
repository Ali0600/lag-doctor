"""A stand-in for a game on a machine without a GPU: a window drawn with Direct3D 11 on WARP, presented every frame.

GitHub's runners have no GPU (CLAUDE.md), so LagLab's OpenGL 3.3 cannot draw there. Direct3D 11 can, on WARP,
Windows' software rasterizer, and its swap chain makes the DXGI presents PresentMon times. The stand-in also sends
one small DNS query a second over UDP to 1.1.1.1, so the network tracker sees traffic from a process other than
its own, as it would from a game.

    uv run python scripts/stage2/standin.py --seconds 60 --status <file> [--driver warp|hardware]

It changes nothing on the machine. The status file is written (replaced whole) once the first frame was presented
("ready": true) and again at the end, with this process's own PID: the venv's python.exe is a launcher, so the PID
a parent's Popen returns is not this process (CLAUDE.md). Layouts and vtable slots from d3d11.h, dxgi.h and
winuser.h (Windows SDK 10.0.19041), x64. Exit codes: 0 it presented frames, 1 it could not, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import secrets
import socket
import struct
import sys
import threading
import time
import traceback
import uuid
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lagdoctor import clock  # noqa: E402

LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800
DRIVERS = {"hardware": 1, "warp": 5}             # D3D_DRIVER_TYPE (d3dcommon.h)
D3D11_SDK_VERSION = 7
DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_USAGE_RENDER_TARGET_OUTPUT = 0x20
SWAP_EFFECTS = (("flip_discard", 4, 2), ("discard", 0, 1))   # (name, DXGI_SWAP_EFFECT, buffer count): flip first
DXGI_STATUS_OCCLUDED = 0x087A0001
WS_OVERLAPPEDWINDOW, WS_VISIBLE, CW_USEDEFAULT = 0x00CF0000, 0x10000000, -2147483648
WM_DESTROY, WM_CLOSE, PM_REMOVE, SW_SHOW = 0x0002, 0x0010, 0x1, 5
WIDTH, HEIGHT, FPS_CAP = 640, 480, 60
DNS_SERVER = ("1.1.1.1", 53)

# vtable slots, counted from the C ...Vtbl structs of the SDK headers
RELEASE, QUERY_INTERFACE = 2, 0
SWAPCHAIN_PRESENT, SWAPCHAIN_GET_BUFFER = 8, 9                 # IDXGISwapChain
DEVICE_CREATE_RENDER_TARGET_VIEW = 9                           # ID3D11Device
CONTEXT_CLEAR_RENDER_TARGET_VIEW = 50                          # ID3D11DeviceContext
DXGI_DEVICE_GET_ADAPTER, DXGI_ADAPTER_GET_DESC = 7, 8          # IDXGIDevice, IDXGIAdapter


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> GUID:
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g


IID_ID3D11Texture2D = GUID.of("6f15aaf2-d208-4e89-9ab4-489535d34f9c")    # d3d11.h
IID_IDXGIDevice = GUID.of("54ec77fa-1377-44e6-8c32-88fd5f44c84c")        # dxgi.h


class DXGI_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class DXGI_MODE_DESC(ctypes.Structure):
    _fields_ = [("Width", wintypes.UINT), ("Height", wintypes.UINT), ("RefreshRate", DXGI_RATIONAL),
                ("Format", ctypes.c_int), ("ScanlineOrdering", ctypes.c_int), ("Scaling", ctypes.c_int)]


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", wintypes.UINT), ("Quality", wintypes.UINT)]


class DXGI_SWAP_CHAIN_DESC(ctypes.Structure):
    _fields_ = [("BufferDesc", DXGI_MODE_DESC), ("SampleDesc", DXGI_SAMPLE_DESC), ("BufferUsage", wintypes.UINT),
                ("BufferCount", wintypes.UINT), ("OutputWindow", wintypes.HWND), ("Windowed", wintypes.BOOL),
                ("SwapEffect", ctypes.c_int), ("Flags", wintypes.UINT)]


class DXGI_ADAPTER_DESC(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", ctypes.c_uint64)]


WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(DXGI_MODE_DESC) == 28
assert ctypes.sizeof(DXGI_SWAP_CHAIN_DESC) == 72
assert ctypes.sizeof(DXGI_ADAPTER_DESC) == 304
assert ctypes.sizeof(WNDCLASSEXW) == 80
assert ctypes.sizeof(wintypes.MSG) == 48

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = wintypes.LPARAM
_user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                    wintypes.HINSTANCE, ctypes.c_void_p]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                 wintypes.UINT]
_user32.PeekMessageW.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = wintypes.LPARAM
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

_RELEASE_FN = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_QI_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_PRESENT_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
_GET_BUFFER_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(GUID),
                                    ctypes.POINTER(ctypes.c_void_p))
_CREATE_RTV_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_void_p))
_CLEAR_RTV_FN = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float))
_GET_ADAPTER_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
_GET_DESC_FN = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC))


def _method(obj: int, slot: int, proto):
    """The COM method in `slot` of obj's vtable, bound to obj."""
    vtable = ctypes.cast(ctypes.c_void_p(obj), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    fn = proto(vtable[slot])
    return lambda *args: fn(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj.value:
        _method(obj.value, RELEASE, _RELEASE_FN)()
        obj.value = None


def _hr(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08X}"


@WNDPROC
def _wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_DESTROY:
        _user32.PostQuitMessage(0)
        return 0
    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _window() -> int:
    hinstance = _kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=_wndproc, hInstance=hinstance,
                     lpszClassName="lagdoctor-standin")
    if not _user32.RegisterClassExW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = _user32.CreateWindowExW(0, "lagdoctor-standin", "lag-doctor stand-in", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                   CW_USEDEFAULT, CW_USEDEFAULT, WIDTH, HEIGHT, None, None, hinstance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    _user32.ShowWindow(hwnd, SW_SHOW)
    return hwnd


def _pump() -> bool:
    """Handle pending window messages; False once the window was closed."""
    msg = wintypes.MSG()
    while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
        if msg.message == 0x0012:                                # WM_QUIT
            return False
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))
    return True


class _Udp:
    """One DNS query a second over UDP, counted, on a background thread."""

    def __init__(self) -> None:
        self.sent = self.replies = 0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="standin-udp")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(3.0)
        return {"server": f"{DNS_SERVER[0]}:{DNS_SERVER[1]}", "sent": self.sent, "replies": self.replies,
                "errors": self.errors[:5]}

    def _run(self) -> None:
        qname = b"".join(bytes([len(p)]) + p for p in (b"example", b"com")) + b"\x00"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.5)
            while not self._stop.wait(1.0):
                txid = secrets.randbits(16)
                query = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
                try:
                    s.sendto(query, DNS_SERVER)
                    self.sent += 1
                    data, _ = s.recvfrom(4096)
                    self.replies += data[:2] == query[:2]
                except OSError as exc:
                    if len(self.errors) < 20:
                        self.errors.append(str(exc))


def _write_status(path: Path, status: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
    os.replace(tmp, path)                                        # a reader never sees half a file


def _session_id() -> int | None:
    sid = wintypes.DWORD()
    return int(sid.value) if _kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)) else None


def run(seconds: float, status_path: Path, driver: str) -> int:
    status: dict = {"pid": os.getpid(), "session_id": _session_id(), "ready": False, "driver": driver,
                    "started_qpc_s": clock.now(), "presents": {"ok": 0, "occluded": 0, "failed": 0},
                    "first_failure": None}
    d3d11 = ctypes.WinDLL("d3d11", winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
    d3d11.D3D11CreateDeviceAndSwapChain.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ctypes.POINTER(DXGI_SWAP_CHAIN_DESC), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_void_p)]
    d3d11.D3D11CreateDeviceAndSwapChain.restype = ctypes.c_long
    swapchain, device, context = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    texture, rtv = ctypes.c_void_p(), ctypes.c_void_p()
    udp = _Udp()
    hwnd = None
    try:
        hwnd = _window()
        level = ctypes.c_int(0)
        attempts = []
        for name, effect, buffers in SWAP_EFFECTS:
            desc = DXGI_SWAP_CHAIN_DESC()
            desc.BufferDesc.Width, desc.BufferDesc.Height = WIDTH, HEIGHT
            desc.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM
            desc.SampleDesc.Count = 1
            desc.BufferUsage, desc.BufferCount = DXGI_USAGE_RENDER_TARGET_OUTPUT, buffers
            desc.OutputWindow, desc.Windowed, desc.SwapEffect = hwnd, True, effect
            hr = d3d11.D3D11CreateDeviceAndSwapChain(None, DRIVERS[driver], None, 0, None, 0, D3D11_SDK_VERSION,
                                                     ctypes.byref(desc), ctypes.byref(swapchain), ctypes.byref(device),
                                                     ctypes.byref(level), ctypes.byref(context))
            attempts.append({"swap_effect": name, "hr": _hr(hr)})
            if hr >= 0:
                status["swap_effect"] = name
                break
        status["device_attempts"] = attempts
        if not swapchain.value:
            status["error"] = "D3D11CreateDeviceAndSwapChain failed for every swap effect"
            return 1
        status["feature_level"] = f"0x{level.value:04X}"
        status["adapter"] = _adapter(device.value)
        hr = _method(swapchain.value, SWAPCHAIN_GET_BUFFER, _GET_BUFFER_FN)(0, ctypes.byref(IID_ID3D11Texture2D),
                                                                            ctypes.byref(texture))
        if hr < 0:
            status["error"] = f"GetBuffer failed: {_hr(hr)}"
            return 1
        hr = _method(device.value, DEVICE_CREATE_RENDER_TARGET_VIEW, _CREATE_RTV_FN)(texture, None, ctypes.byref(rtv))
        if hr < 0:
            status["error"] = f"CreateRenderTargetView failed: {_hr(hr)}"
            return 1
        present = _method(swapchain.value, SWAPCHAIN_PRESENT, _PRESENT_FN)
        clear = _method(context.value, CONTEXT_CLEAR_RENDER_TARGET_VIEW, _CLEAR_RTV_FN)
        udp.start()
        frame, start = 0, time.perf_counter()
        while time.perf_counter() - start < seconds and _pump():
            color = (ctypes.c_float * 4)((frame % 120) / 120.0, 0.25, 0.5, 1.0)
            clear(rtv, color)
            hr = present(1, 0)
            if hr == DXGI_STATUS_OCCLUDED:
                status["presents"]["occluded"] += 1
            elif hr < 0:
                status["presents"]["failed"] += 1
                status["first_failure"] = status["first_failure"] or _hr(hr)
            else:
                status["presents"]["ok"] += 1
                if not status["ready"]:
                    status["ready"], status["first_present_qpc_s"] = True, clock.now()
                    _write_status(status_path, status)
            frame += 1
            wait = start + frame / FPS_CAP - time.perf_counter()  # never faster than FPS_CAP
            if wait > 0:
                time.sleep(wait)
        return 0 if status["presents"]["ok"] else 1
    except Exception:  # noqa: BLE001 - the status file must say what broke
        status["error"] = traceback.format_exc()
        return 1
    finally:
        status["udp"] = udp.stop() if udp._thread.is_alive() else {"sent": 0, "replies": 0, "errors": []}
        for obj in (rtv, texture, context, device, swapchain):
            _release(obj)
        if hwnd:
            _user32.DestroyWindow(hwnd)
        status["ended_qpc_s"] = clock.now()
        _write_status(status_path, status)


def _adapter(device: int) -> dict | None:
    """The adapter WARP or the hardware driver gave this device: its description and ids."""
    dxgi_device, adapter = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        if _method(device, QUERY_INTERFACE, _QI_FN)(ctypes.byref(IID_IDXGIDevice), ctypes.byref(dxgi_device)) < 0:
            return None
        if _method(dxgi_device.value, DXGI_DEVICE_GET_ADAPTER, _GET_ADAPTER_FN)(ctypes.byref(adapter)) < 0:
            return None
        desc = DXGI_ADAPTER_DESC()
        if _method(adapter.value, DXGI_ADAPTER_GET_DESC, _GET_DESC_FN)(ctypes.byref(desc)) < 0:
            return None
        return {"description": desc.Description, "vendor_id": f"0x{desc.VendorId:04X}",
                "device_id": f"0x{desc.DeviceId:04X}"}
    finally:
        _release(adapter)
        _release(dxgi_device)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--status", type=Path, required=True, help="where to write the status JSON")
    ap.add_argument("--driver", choices=sorted(DRIVERS), default="warp")
    args = ap.parse_args(argv)
    if not 0 < args.seconds <= 600:
        print("--seconds must be between 0 and 600")
        return 2
    return run(args.seconds, args.status.resolve(), args.driver)


if __name__ == "__main__":
    raise SystemExit(main())
