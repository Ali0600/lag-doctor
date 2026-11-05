"""GPU adapters by LUID: name, vendor and VRAM (DXGI), integrated or not (DXCore), HAGS state, hybrid role
and PCI address (D3DKMT).

Everything here is a system-wide query; no process is opened. Layouts, vtable slots and constants come from
docs/research/windows-telemetry.md (WT) section 5 and were re-read in the Windows SDK 10.0.19041 headers.
LUIDs are only valid until the next reboot or driver restart (WT 5.1), so callers rebuild the list when they
meet a LUID they do not know.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800       # load DLLs from System32 only, never the current directory

DXGI_ERROR_NOT_FOUND = 0x887A0002               # WT 5.1 (winerror.h)
DXGI_ADAPTER_FLAG_SOFTWARE = 2                  # WT 5.1 (dxgi.h)
MAX_ADAPTERS = 64                               # bound for EnumAdapters1

# vtable slots, counted from the C ...Vtbl structs (WT 5.1, 5.2; verify log #55, #57)
_RELEASE = 2
_FACTORY1_ENUM_ADAPTERS1 = 12
_ADAPTER1_GET_DESC1 = 10
_DXCORE_FACTORY_GET_ADAPTER_BY_LUID = 4
_DXCORE_ADAPTER_GET_PROPERTY = 6
DXCORE_IS_INTEGRATED = 12                       # DXCoreAdapterProperty::IsIntegrated, a 1-byte bool (WT 5.2)

# D3DKMT (WT 5.3; d3dkmthk.h, d3dkmdt.h)
KMTQAITYPE_ADAPTERADDRESS = 6
KMTQAITYPE_ADAPTERTYPE = 15
KMTQAITYPE_WDDM_2_7_CAPS = 70
ADAPTERTYPE_SOFTWARE_DEVICE = 1 << 2
ADAPTERTYPE_HYBRID_DISCRETE = 1 << 4
ADAPTERTYPE_HYBRID_INTEGRATED = 1 << 5
WDDM_2_7_HWSCH_ENABLED = 1 << 1                 # bit 0 is HwSchSupported; bit 1 is "currently enabled"

# PCI vendor ids. NVIDIA, Intel and Microsoft as seen in WT 5.1; AMD's graphics id is 0x1002.
_VENDORS = {0x10DE: "nvidia", 0x1002: "amd", 0x8086: "intel", 0x1414: "microsoft"}


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", wintypes.UINT), ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID), ("Flags", wintypes.UINT)]


class D3DKMT_OPENADAPTERFROMLUID(ctypes.Structure):
    _fields_ = [("AdapterLuid", LUID), ("hAdapter", wintypes.UINT)]


class D3DKMT_QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT), ("Type", ctypes.c_int), ("pPrivateDriverData", ctypes.c_void_p),
                ("PrivateDriverDataSize", wintypes.UINT)]


class D3DKMT_CLOSEADAPTER(ctypes.Structure):
    _fields_ = [("hAdapter", wintypes.UINT)]


class D3DKMT_ADAPTERADDRESS(ctypes.Structure):
    _fields_ = [("BusNumber", wintypes.UINT), ("DeviceNumber", wintypes.UINT), ("FunctionNumber", wintypes.UINT)]


# The notes give the field layouts (WT 5.1, 5.3); these are the x64 sizes those layouts produce.
assert ctypes.sizeof(LUID) == 8
assert ctypes.sizeof(GUID) == 16
assert ctypes.sizeof(DXGI_ADAPTER_DESC1) == 312
assert ctypes.sizeof(D3DKMT_OPENADAPTERFROMLUID) == 12
assert ctypes.sizeof(D3DKMT_QUERYADAPTERINFO) == 24
assert ctypes.sizeof(D3DKMT_CLOSEADAPTER) == 4
assert ctypes.sizeof(D3DKMT_ADAPTERADDRESS) == 12


def _guid(d1: int, d2: int, d3: int, *d4: int) -> GUID:
    return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


IID_IDXGIFactory1 = _guid(0x770AAE78, 0xF26F, 0x4DBA, 0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)          # WT 5.1
IID_IDXCoreAdapterFactory = _guid(0x78EE5945, 0xC36E, 0x4B13, 0xA6, 0x69, 0x00, 0x5D, 0xD1, 0x1C, 0x0F, 0x06)  # WT 5.2
IID_IDXCoreAdapter = _guid(0xF0DB4C7F, 0xFE5A, 0x42A2, 0xBD, 0x62, 0xF2, 0xA6, 0xCF, 0x6F, 0xC8, 0x3E)         # WT 5.2

_dxgi = ctypes.WinDLL("dxgi", use_last_error=True, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
_dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
_dxgi.CreateDXGIFactory1.restype = ctypes.c_long

_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_gdi32.D3DKMTOpenAdapterFromLuid.argtypes = [ctypes.POINTER(D3DKMT_OPENADAPTERFROMLUID)]
_gdi32.D3DKMTOpenAdapterFromLuid.restype = ctypes.c_long          # NTSTATUS
_gdi32.D3DKMTQueryAdapterInfo.argtypes = [ctypes.POINTER(D3DKMT_QUERYADAPTERINFO)]
_gdi32.D3DKMTQueryAdapterInfo.restype = ctypes.c_long
_gdi32.D3DKMTCloseAdapter.argtypes = [ctypes.POINTER(D3DKMT_CLOSEADAPTER)]
_gdi32.D3DKMTCloseAdapter.restype = ctypes.c_long

# COM method prototypes: (this, args...) -> HRESULT / ULONG
_Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_EnumAdapters1 = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
_GetDesc1 = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(DXGI_ADAPTER_DESC1))
_GetAdapterByLuid = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(LUID), ctypes.POINTER(GUID),
                                       ctypes.POINTER(ctypes.c_void_p))
_GetProperty = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_void_p)


# ---------------------------------------------------------------- pure helpers --

def luid_str(high: int, low: int) -> str:
    """PDH's order and DESIGN's format: HighPart then LowPart, 8 lower-case hex digits each.

    PDH instance names spell the same digits upper-case (luid_0x00000000_0x00010F83, WT 2.2), so compare
    case-insensitively.
    """
    return f"0x{high & 0xFFFFFFFF:08x}_0x{low & 0xFFFFFFFF:08x}"


def vendor_name(vendor_id: int) -> str:
    return _VENDORS.get(vendor_id, "other")


def pci_str(bus: int, device: int, function: int) -> str | None:
    """'BB:DD.F' in upper-case hex, the tail of NVML's busId ('00000000:01:00.0' -> '01:00.0', WT 4.5).

    D3DKMT gives no PCI domain, so none is invented. The software adapter answers 0xFFFFFFFF/0xFFFF (no PCI
    location, WT 5.3), and anything outside PCI's ranges is treated the same way.
    """
    if not (0 <= bus <= 0xFF and 0 <= device <= 0x1F and 0 <= function <= 0x7):
        return None
    return f"{bus:02X}:{device:02X}.{function:X}"


def hybrid_role(adapter_type: int) -> str | None:
    discrete = bool(adapter_type & ADAPTERTYPE_HYBRID_DISCRETE)
    integrated = bool(adapter_type & ADAPTERTYPE_HYBRID_INTEGRATED)
    if discrete == integrated:          # neither, or (never seen) both: no claim
        return None
    return "discrete" if discrete else "integrated"


def hags_enabled(wddm27_caps: int) -> bool:
    return bool(wddm27_caps & WDDM_2_7_HWSCH_ENABLED)


def is_software(dxgi_flags: int, adapter_type: int | None) -> bool:
    return bool(dxgi_flags & DXGI_ADAPTER_FLAG_SOFTWARE) or bool(
        adapter_type is not None and adapter_type & ADAPTERTYPE_SOFTWARE_DEVICE)


# ---------------------------------------------------------------- COM plumbing --

def _method(obj: ctypes.c_void_p, slot: int, proto):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(vtbl[slot])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _method(obj, _RELEASE, _Release)(obj)


def _hr_error(hr: int, what: str) -> OSError:
    return ctypes.WinError(hr, f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


# ---------------------------------------------------------------- DXCore ---------

class _DXCore:
    """IDXCoreAdapterFactory, if dxcore.dll exists (Windows 10 2004+); otherwise every answer is None."""

    def __init__(self) -> None:
        self.factory = ctypes.c_void_p()
        try:
            dll = ctypes.WinDLL("dxcore", use_last_error=True, winmode=LOAD_LIBRARY_SEARCH_SYSTEM32)
            create = dll.DXCoreCreateAdapterFactory
        except (OSError, AttributeError):
            return
        create.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        create.restype = ctypes.c_long
        if create(ctypes.byref(IID_IDXCoreAdapterFactory), ctypes.byref(self.factory)) < 0:
            self.factory = ctypes.c_void_p()

    def is_integrated(self, luid: LUID) -> bool | None:
        if not self.factory:
            return None
        adapter = ctypes.c_void_p()
        get = _method(self.factory, _DXCORE_FACTORY_GET_ADAPTER_BY_LUID, _GetAdapterByLuid)
        if get(self.factory, ctypes.byref(luid), ctypes.byref(IID_IDXCoreAdapter), ctypes.byref(adapter)) < 0:
            return None
        try:
            value = ctypes.c_ubyte(0)
            prop = _method(adapter, _DXCORE_ADAPTER_GET_PROPERTY, _GetProperty)
            if prop(adapter, DXCORE_IS_INTEGRATED, 1, ctypes.byref(value)) < 0:
                return None
            return bool(value.value)
        finally:
            _release(adapter)

    def close(self) -> None:
        _release(self.factory)
        self.factory = ctypes.c_void_p()


# ---------------------------------------------------------------- D3DKMT ---------

def _kmt_query(handle: int, kind: int, out: ctypes.Structure | ctypes._SimpleCData) -> bool:
    q = D3DKMT_QUERYADAPTERINFO(handle, kind, ctypes.cast(ctypes.byref(out), ctypes.c_void_p), ctypes.sizeof(out))
    return _gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(q)) == 0


def _kmt_info(luid: LUID) -> dict:
    """Adapter type, WDDM 2.7 caps and PCI address; a field that cannot be read stays None."""
    info: dict = {"type": None, "hags": None, "pci": None}
    req = D3DKMT_OPENADAPTERFROMLUID(AdapterLuid=luid)
    if _gdi32.D3DKMTOpenAdapterFromLuid(ctypes.byref(req)) != 0:
        return info
    try:
        kind = wintypes.UINT(0)
        if _kmt_query(req.hAdapter, KMTQAITYPE_ADAPTERTYPE, kind):
            info["type"] = kind.value
        caps = wintypes.UINT(0)
        if _kmt_query(req.hAdapter, KMTQAITYPE_WDDM_2_7_CAPS, caps):
            info["hags"] = hags_enabled(caps.value)
        addr = D3DKMT_ADAPTERADDRESS()
        if _kmt_query(req.hAdapter, KMTQAITYPE_ADAPTERADDRESS, addr):
            info["pci"] = pci_str(addr.BusNumber, addr.DeviceNumber, addr.FunctionNumber)
    finally:
        _gdi32.D3DKMTCloseAdapter(ctypes.byref(D3DKMT_CLOSEADAPTER(req.hAdapter)))
    return info


# ---------------------------------------------------------------- public ---------

def list_adapters() -> list[dict]:
    """Every DXGI adapter, software ones included (marked `software`), in DXGI's order.

    Raises OSError when DXGI itself fails; a DXCore or D3DKMT detail that cannot be read is None.
    """
    factory = ctypes.c_void_p()
    hr = _dxgi.CreateDXGIFactory1(ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory))
    if hr < 0:
        raise _hr_error(hr, "CreateDXGIFactory1")
    dxcore = _DXCore()
    out: list[dict] = []
    try:
        enum = _method(factory, _FACTORY1_ENUM_ADAPTERS1, _EnumAdapters1)
        desc = DXGI_ADAPTER_DESC1()
        for index in range(MAX_ADAPTERS):
            adapter = ctypes.c_void_p()
            hr = enum(factory, index, ctypes.byref(adapter))
            if hr & 0xFFFFFFFF == DXGI_ERROR_NOT_FOUND:
                break
            if hr < 0:
                raise _hr_error(hr, f"IDXGIFactory1::EnumAdapters1({index})")
            try:
                hr = _method(adapter, _ADAPTER1_GET_DESC1, _GetDesc1)(adapter, ctypes.byref(desc))
                if hr < 0:
                    raise _hr_error(hr, f"IDXGIAdapter1::GetDesc1 (adapter {index})")
            finally:
                _release(adapter)
            luid = LUID(desc.AdapterLuid.LowPart, desc.AdapterLuid.HighPart)
            kmt = _kmt_info(luid)
            out.append({
                "luid": luid_str(luid.HighPart, luid.LowPart),
                "name": desc.Description,
                "vendor": vendor_name(desc.VendorId),
                "vendor_id": desc.VendorId,
                "vram_mb": desc.DedicatedVideoMemory // (1024 * 1024),
                "integrated": dxcore.is_integrated(luid),
                "software": is_software(desc.Flags, kmt["type"]),
                "hags": kmt["hags"],
                "hybrid": hybrid_role(kmt["type"]) if kmt["type"] is not None else None,
                "pci": kmt["pci"],
            })
    finally:
        dxcore.close()
        _release(factory)
    return out
