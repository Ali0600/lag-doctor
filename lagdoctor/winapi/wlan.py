"""Wi-Fi link quality without admin and without location permission (Native Wifi, wlanapi.dll).

From docs/research/network-and-etw.md (NE) section 3 and its verification log:
- Opcode 19 (realtime connection quality) needs no location consent; it fails while the interface
  is not connected, so that is reported as "no reading", not as an error (NE 3.3, log #46).
- Its rates are kbit/s and its "kHz" frequency is really MHz: 5180 on channel 36 (NE 3.3, log #48-49).
- Driver statistics give a cumulative retry counter; PHY entry 0 is used (NE 3.4).
- The BSSID query (current_connection, opcode 7) is location-gated: without consent it fails with
  error 5, and a desktop app's first call can raise a consent prompt (NE 3.1). So it is only ever
  made when the consent store already says Allow, read from the registry first; never to find out.
- ACM scan_complete notifications (code 7) arrive on a thread Python did not create (NE 3.5,
  log #63): the callback must not block and must not take a lock that a thread calling into WLAN holds.
"""
from __future__ import annotations

import ctypes
import struct
import uuid
import winreg
from ctypes import wintypes
from typing import Callable

from lagdoctor import clock

WLAN_CLIENT_VERSION = 2                  # negotiated version 2 here (NE 3.2)
WLAN_MAX_NAME_LENGTH = 256
OPCODE_INTERFACE_STATE = 6               # SDK um\wlanapi.h:973-1001 (NE 3.2)
OPCODE_CURRENT_CONNECTION = 7            # location-gated (NE 3.1)
OPCODE_CHANNEL_NUMBER = 8
OPCODE_REALTIME_QUALITY = 19             # newer than SDK 19041; value from Learn's enum (NE 3.2, log #44)
OPCODE_STATISTICS = 0x10000101
STATE_CONNECTED = 1                      # wlan_interface_state_connected
WLAN_NOTIFICATION_SOURCE_NONE = 0
WLAN_NOTIFICATION_SOURCE_ACM = 0x8       # L2_NOTIFICATION_SOURCE_WLAN_ACM (NE 3.5, log #53); MSM is location-gated
ACM_SCAN_COMPLETE = 7                    # wlan_notification_acm_scan_complete (NE 3.5, log #54)
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_DATA = 13
ERROR_SERVICE_NOT_ACTIVE = 1062
MAX_QUERY_BYTES = 65_536                 # statistics for 64 PHYs is 9,440 bytes; anything bigger is junk
MAX_INTERFACES = 64

# DOT11_PHY_TYPE (SDK shared\windot11.h:79-91; NE 3.3, log #51)
_PHY_NAMES = {1: "FHSS", 2: "DSSS", 3: "IR", 4: "802.11a", 5: "802.11b", 6: "802.11g", 7: "802.11n",
              8: "802.11ac", 9: "802.11ad", 10: "802.11ax"}

_CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\location"


def _check_size(struct_type: type, expected: int, source: str) -> None:
    got = ctypes.sizeof(struct_type)
    if got != expected:
        raise RuntimeError(f"{struct_type.__name__} is {got} bytes, expected {expected} ({source}); "
                           "the ctypes layout no longer matches Windows")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class WLAN_INTERFACE_INFO(ctypes.Structure):       # 532 bytes (NE 3.3)
    _fields_ = [("InterfaceGuid", GUID), ("strInterfaceDescription", ctypes.c_wchar * WLAN_MAX_NAME_LENGTH),
                ("isState", ctypes.c_int)]


class WLAN_RATE_SET(ctypes.Structure):             # DOT11_RATE_SET_MAX_LENGTH = 126 (NE 3.3)
    _fields_ = [("uRateSetLength", ctypes.c_ulong), ("usRateSet", ctypes.c_ushort * 126)]


class WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO(ctypes.Structure):   # 272 bytes (NE 3.3)
    _fields_ = [("ucLinkID", ctypes.c_ubyte), ("ulChannelCenterFrequencyMhz", ctypes.c_ulong),
                ("ulBandwidth", ctypes.c_ulong), ("lRssi", ctypes.c_long), ("wlanRateSet", WLAN_RATE_SET)]


class WLAN_REALTIME_CONNECTION_QUALITY(ctypes.Structure):             # 296 bytes with one link; links @24
    _fields_ = [("dot11PhyType", ctypes.c_int), ("ulLinkQuality", ctypes.c_ulong),
                ("ulRxRate", ctypes.c_ulong), ("ulTxRate", ctypes.c_ulong),
                ("bIsMLOConnection", wintypes.BOOL), ("ulNumLinks", ctypes.c_ulong),
                ("linksInfo", WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO * 1)]


class WLAN_PHY_FRAME_STATISTICS(ctypes.Structure):  # 18 ULONGLONGs = 144 bytes (SDK um\wlanapi.h:1041-1096)
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ullTransmittedFrameCount", "ullMulticastTransmittedFrameCount", "ullFailedCount", "ullRetryCount",
        "ullMultipleRetryCount", "ullMaxTXLifetimeExceededCount", "ullTransmittedFragmentCount",
        "ullRTSSuccessCount", "ullRTSFailureCount", "ullACKFailureCount", "ullReceivedFrameCount",
        "ullMulticastReceivedFrameCount", "ullPromiscuousReceivedFrameCount", "ullMaxRXLifetimeExceededCount",
        "ullFrameDuplicateCount", "ullReceivedFragmentCount", "ullPromiscuousReceivedFragmentCount",
        "ullFCSErrorCount")]


class WLAN_MAC_FRAME_STATISTICS(ctypes.Structure):  # 12 ULONGLONGs = 96 bytes
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ullTransmittedFrameCount", "ullReceivedFrameCount", "ullWEPExcludedCount", "ullTKIPLocalMICFailures",
        "ullTKIPReplays", "ullTKIPICVErrorCount", "ullCCMPReplays", "ullCCMPDecryptErrors",
        "ullWEPUndecryptableCount", "ullWEPICVErrorCount", "ullDecryptSuccessCount", "ullDecryptFailureCount")]


class WLAN_STATISTICS(ctypes.Structure):           # 368 bytes, PhyCounters @224 (NE 3.4, log #52)
    _fields_ = [("ullFourWayHandshakeFailures", ctypes.c_uint64), ("ullTKIPCounterMeasuresInvoked", ctypes.c_uint64),
                ("ullReserved", ctypes.c_uint64), ("MacUcastCounters", WLAN_MAC_FRAME_STATISTICS),
                ("MacMcastCounters", WLAN_MAC_FRAME_STATISTICS), ("dwNumberOfPhys", wintypes.DWORD),
                ("PhyCounters", WLAN_PHY_FRAME_STATISTICS * 1)]


class L2_NOTIFICATION_DATA(ctypes.Structure):      # 40 bytes (SDK um\l2cmn.h:91-100; NE 3.5)
    _fields_ = [("NotificationSource", wintypes.DWORD), ("NotificationCode", wintypes.DWORD),
                ("InterfaceGuid", GUID), ("dwDataSize", wintypes.DWORD), ("pData", ctypes.c_void_p)]


class DOT11_SSID(ctypes.Structure):                # SDK shared\wlantypes.h:30-36
    _fields_ = [("uSSIDLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):   # SDK um\wlanapi.h:584-593
    _fields_ = [("dot11Ssid", DOT11_SSID), ("dot11BssType", ctypes.c_int), ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11PhyType", ctypes.c_int), ("uDot11PhyIndex", ctypes.c_ulong),
                ("wlanSignalQuality", ctypes.c_ulong), ("ulRxRate", ctypes.c_ulong), ("ulTxRate", ctypes.c_ulong)]


class WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):  # SDK um\wlanapi.h:595-600
    _fields_ = [("bSecurityEnabled", wintypes.BOOL), ("bOneXEnabled", wintypes.BOOL),
                ("dot11AuthAlgorithm", ctypes.c_int), ("dot11CipherAlgorithm", ctypes.c_int)]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):    # SDK um\wlanapi.h:603-609
    _fields_ = [("isState", ctypes.c_int), ("wlanConnectionMode", ctypes.c_int),
                ("strProfileName", ctypes.c_wchar * WLAN_MAX_NAME_LENGTH),
                ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
                ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES)]


_check_size(WLAN_INTERFACE_INFO, 532, "NE 3.3")
_check_size(WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO, 272, "NE 3.3")
_check_size(WLAN_REALTIME_CONNECTION_QUALITY, 296, "NE 3.3")
_check_size(WLAN_PHY_FRAME_STATISTICS, 144, "NE 3.3")
_check_size(WLAN_STATISTICS, 368, "NE 3.4")
_check_size(L2_NOTIFICATION_DATA, 40, "NE 3.5")
# Not in the research (the call is location-gated and was never made there): 604 is the size the
# SDK header gives on x64, 520 + 68 + 16, with dot11Bssid at 520 + 40.
_check_size(WLAN_CONNECTION_ATTRIBUTES, 604, "SDK um\\wlanapi.h:584-609")
if WLAN_REALTIME_CONNECTION_QUALITY.linksInfo.offset != 24 or WLAN_STATISTICS.PhyCounters.offset != 224:
    raise RuntimeError("WLAN struct offsets drifted (NE 3.3: linksInfo @24, PhyCounters @224)")

WLAN_NOTIFICATION_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(L2_NOTIFICATION_DATA), ctypes.c_void_p)

_dll_cache: list = []


def _dll():
    """wlanapi.dll, loaded on first use (it is missing on some Windows editions without Wi-Fi support)."""
    if _dll_cache:
        return _dll_cache[0]
    dll = ctypes.WinDLL("wlanapi", use_last_error=True)
    dll.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                   ctypes.POINTER(wintypes.HANDLE)]
    dll.WlanOpenHandle.restype = wintypes.DWORD
    dll.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    dll.WlanCloseHandle.restype = wintypes.DWORD
    dll.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    dll.WlanEnumInterfaces.restype = wintypes.DWORD
    dll.WlanQueryInterface.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID), ctypes.c_int, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(ctypes.c_int)]
    dll.WlanQueryInterface.restype = wintypes.DWORD
    dll.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    dll.WlanFreeMemory.restype = None
    dll.WlanRegisterNotification.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                                             WLAN_NOTIFICATION_CALLBACK, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.POINTER(wintypes.DWORD)]
    dll.WlanRegisterNotification.restype = wintypes.DWORD
    _dll_cache.append(dll)
    return dll


def _guid_text(g: GUID) -> str:
    return "{" + str(uuid.UUID(bytes_le=bytes(g))).upper() + "}"


def _guid_struct(text: str) -> GUID:
    if not isinstance(text, str) or len(text) > 40:
        raise ValueError(f"not an interface GUID: {text!r}")
    return GUID.from_buffer_copy(uuid.UUID(text.strip()).bytes_le)    # ValueError for junk


def _read_consent(root: int, path: str) -> str | None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, "Value")
    except OSError:
        return None
    return value if kind == winreg.REG_SZ and isinstance(value, str) else None


def location_allowed() -> bool:
    """True only when Windows already lets desktop apps use location: the system switch, the user's
    switch and "let desktop apps access your location" all read Allow (NE 3.1). Read-only; never asks."""
    return (_read_consent(winreg.HKEY_LOCAL_MACHINE, _CONSENT_KEY) == "Allow"
            and _read_consent(winreg.HKEY_CURRENT_USER, _CONSENT_KEY) == "Allow"
            and _read_consent(winreg.HKEY_CURRENT_USER, _CONSENT_KEY + r"\NonPackaged") == "Allow")


def _mhz(value: int) -> int | None:
    """The link's centre frequency in MHz. Learn says kHz but this driver gives MHz (NE log #48);
    a kHz-sized value is scaled so either reading lands on the same number."""
    if value <= 0:
        return None
    if value > 100_000:
        value //= 1000
    return value if 2_000 <= value <= 8_000 else None


def phy_name(phy: int) -> str:
    return _PHY_NAMES.get(phy, f"PHY type {phy}")


class Wlan:
    """A WLAN client handle. Use from one thread; only the scan callback runs elsewhere."""

    open_error: str | None = None    # why the last open() returned None

    def __init__(self, handle: int) -> None:
        self._h = handle
        self._guids: dict[str, GUID] = {}
        self._callbacks: list = []   # every registered thunk stays alive until the handle is closed
        self.notifications = 0
        self.callback_errors = 0

    @classmethod
    def open(cls) -> "Wlan | None":
        """None when wlanapi.dll is missing or the WLAN service will not give us a handle (see open_error)."""
        try:
            dll = _dll()
        except OSError as exc:
            cls.open_error = f"wlanapi.dll is not available ({exc})"
            return None
        version, handle = wintypes.DWORD(), wintypes.HANDLE()
        rc = dll.WlanOpenHandle(WLAN_CLIENT_VERSION, None, ctypes.byref(version), ctypes.byref(handle))
        if rc:
            cls.open_error = ("the WLAN AutoConfig service is not running (error 1062)" if rc == ERROR_SERVICE_NOT_ACTIVE
                              else f"WlanOpenHandle failed (error {rc})")
            return None
        cls.open_error = None
        return cls(handle.value)

    def __enter__(self) -> "Wlan":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Unregister notifications (waits for a running callback) and close the handle. Never raises."""
        h, self._h = self._h, None
        if not h:
            return
        dll = _dll()
        if self._callbacks:
            dll.WlanRegisterNotification(h, WLAN_NOTIFICATION_SOURCE_NONE, True, WLAN_NOTIFICATION_CALLBACK(),
                                         None, None, None)
        dll.WlanCloseHandle(h, None)

    # ---- queries -----------------------------------------------------------
    def interfaces(self) -> list[dict]:
        """[{guid, description, connected, state}] for every Wi-Fi adapter."""
        lst = ctypes.c_void_p()
        rc = _dll().WlanEnumInterfaces(self._handle(), None, ctypes.byref(lst))
        if rc:
            raise ctypes.WinError(rc)
        try:
            n = ctypes.c_ulong.from_address(lst.value).value
            if n > MAX_INTERFACES:
                raise OSError(f"WlanEnumInterfaces claims {n} interfaces")
            # DWORD dwNumberOfItems, DWORD dwIndex, then the items (NE 3.3)
            items = (WLAN_INTERFACE_INFO * n).from_address(lst.value + 8)
            return [{"guid": _guid_text(i.InterfaceGuid), "description": i.strInterfaceDescription,
                     "connected": i.isState == STATE_CONNECTED, "state": int(i.isState)} for i in items]
        finally:
            _dll().WlanFreeMemory(lst)

    def state(self, guid: str) -> int | None:
        """WLAN_INTERFACE_STATE (1 = connected), None if it cannot be read."""
        rc, raw = self._query(guid, OPCODE_INTERFACE_STATE)
        if rc or len(raw) < 4:
            return None
        return struct.unpack_from("<i", raw)[0]

    def quality(self, guid: str) -> dict | None:
        """Opcode 19: {rssi_dbm, signal_pct, rx_mbps, tx_mbps, freq_mhz, channel, phy}; None when not connected."""
        rc, raw = self._query(guid, OPCODE_REALTIME_QUALITY)
        if rc:
            return self._none_if_disconnected(guid, rc)
        if len(raw) < WLAN_REALTIME_CONNECTION_QUALITY.linksInfo.offset:
            raise OSError(f"realtime connection quality returned {len(raw)} bytes")
        phy, link_quality, rx_kbps, tx_kbps, _mlo, n_links = struct.unpack_from("<iIIIiI", raw, 0)
        rssi = freq = None
        link_size = ctypes.sizeof(WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO)
        if n_links >= 1 and len(raw) >= 24 + link_size:
            link = WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO.from_buffer_copy(raw, 24)
            rssi = int(link.lRssi) if -128 <= link.lRssi < 0 else None
            freq = _mhz(int(link.ulChannelCenterFrequencyMhz))
        return {"rssi_dbm": rssi, "signal_pct": int(link_quality) if link_quality <= 100 else None,
                "rx_mbps": rx_kbps / 1000.0, "tx_mbps": tx_kbps / 1000.0,      # kbit/s (NE log #49)
                "freq_mhz": freq, "channel": self._channel(guid), "phy": phy_name(phy)}

    def retries(self, guid: str) -> int | None:
        """Cumulative driver retry count (PHY 0, ullRetryCount); None when not connected."""
        rc, raw = self._query(guid, OPCODE_STATISTICS)
        if rc:
            return self._none_if_disconnected(guid, rc)
        if len(raw) < ctypes.sizeof(WLAN_STATISTICS):
            raise OSError(f"driver statistics returned {len(raw)} bytes")
        stats = WLAN_STATISTICS.from_buffer_copy(raw, 0)
        if stats.dwNumberOfPhys < 1:
            return None
        return int(stats.PhyCounters[0].ullRetryCount)

    def bssid(self, guid: str) -> str | None:
        """The access point's MAC, or None. The query is location-gated, so it is only made when Windows
        already allows location for desktop apps; otherwise this returns None without calling anything."""
        if not location_allowed():
            return None
        rc, raw = self._query(guid, OPCODE_CURRENT_CONNECTION)
        if rc == ERROR_ACCESS_DENIED:
            return None
        if rc:
            return self._none_if_disconnected(guid, rc)
        if len(raw) < ctypes.sizeof(WLAN_CONNECTION_ATTRIBUTES):
            raise OSError(f"current connection returned {len(raw)} bytes")
        attrs = WLAN_CONNECTION_ATTRIBUTES.from_buffer_copy(raw, 0)
        mac = bytes(attrs.wlanAssociationAttributes.dot11Bssid)
        if attrs.isState != STATE_CONNECTED or not any(mac):
            return None
        return ":".join(f"{b:02x}" for b in mac)

    def on_scan_complete(self, callback: Callable[[float, str], None]) -> None:
        """Call callback(clock.now(), interface guid) on each ACM scan_complete (code 7).

        It runs on a WLAN service thread Python did not create (NE 3.5): keep it to an append, never
        block in it and never take a lock that a thread calling Wlan methods (or close) might hold.
        Exceptions are counted in callback_errors, never let through to ctypes.
        """
        def on_notify(pdata, _ctx) -> None:
            try:
                self.notifications += 1
                data = pdata.contents
                if data.NotificationSource == WLAN_NOTIFICATION_SOURCE_ACM and data.NotificationCode == ACM_SCAN_COMPLETE:
                    callback(clock.now(), _guid_text(data.InterfaceGuid))
            except BaseException:  # noqa: BLE001 - nothing may escape into the WLAN service's thread
                self.callback_errors += 1

        thunk = WLAN_NOTIFICATION_CALLBACK(on_notify)
        previous = wintypes.DWORD()
        rc = _dll().WlanRegisterNotification(self._handle(), WLAN_NOTIFICATION_SOURCE_ACM, True, thunk, None, None,
                                             ctypes.byref(previous))
        if rc:
            raise ctypes.WinError(rc)
        self._callbacks.append(thunk)

    # ---- internals -----------------------------------------------------------
    def _handle(self) -> int:
        if not self._h:
            raise ValueError("the WLAN handle is closed")
        return self._h

    def _query(self, guid: str, opcode: int) -> tuple[int, bytes]:
        """(error code, bytes copied out of the WLAN buffer); the buffer is freed before returning."""
        g = self._guids.get(guid)
        if g is None:
            g = _guid_struct(guid)
            if len(self._guids) < MAX_INTERFACES:
                self._guids[guid] = g
        size, data, value_type = wintypes.DWORD(), ctypes.c_void_p(), ctypes.c_int()
        rc = _dll().WlanQueryInterface(self._handle(), ctypes.byref(g), opcode, None, ctypes.byref(size),
                                       ctypes.byref(data), ctypes.byref(value_type))
        if rc:
            return rc, b""
        try:
            if not data.value or size.value > MAX_QUERY_BYTES:
                return ERROR_INVALID_DATA, b""
            return 0, ctypes.string_at(data.value, size.value)
        finally:
            if data.value:
                _dll().WlanFreeMemory(data)

    def _channel(self, guid: str) -> int | None:
        rc, raw = self._query(guid, OPCODE_CHANNEL_NUMBER)
        if rc or len(raw) < 4:
            return None
        channel = struct.unpack_from("<I", raw)[0]
        return channel if 1 <= channel <= 233 else None

    def _none_if_disconnected(self, guid: str, rc: int) -> None:
        """A failed query on an interface that is not connected is "no reading"; otherwise it is an error."""
        state = self.state(guid)
        if state is not None and state != STATE_CONNECTED:
            return None
        raise ctypes.WinError(rc)
