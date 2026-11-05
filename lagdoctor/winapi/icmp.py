"""ICMP echo (ping) without ping.exe: IcmpSendEcho2 for IPv4, Icmp6SendEcho2 for IPv6. No admin needed.

Layouts and behaviour come from docs/research/network-and-etw.md (cited as NE) section 1 and its
verification log:

- On x64 ICMP_ECHO_REPLY is 40 bytes with an 8-byte Data pointer and the echoed payload sits at
  reply + 40. Learn's advice to use ICMP_ECHO_REPLY32 is wrong for a 64-bit process (NE 1.1, log #6).
- A reply count of 1 is not success: ICMP errors (TTL expired 11013, net unreachable 11002) also
  return 1, so success means Status == 0 (NE 0 item 2, log #10).
- Timeout is not a deadline: a late reply can still come back as success, and a probe with no reply
  waited 210-1004 ms for Timeouts of 20-1000 ms (NE 1.3, log #12-13). So the RTT here is wall time
  (perf_counter around the call) and the caller applies its own deadline.
"""
from __future__ import annotations

import ctypes
import ipaddress
import threading
import time
from ctypes import wintypes

IP_SUCCESS = 0
IP_REQ_TIMED_OUT = 11010          # NE 1.1 status table (SDK um\IPExport.h:169-237)
IP_TTL_EXPIRED_TRANSIT = 11013
IP_GENERAL_FAILURE = 11050
ERROR_NETWORK_UNREACHABLE = 1231  # seen for IPv6 with no default route (NE 1.4)
ERROR_HOST_UNREACHABLE = 1232
WSAENETUNREACH, WSAEHOSTUNREACH = 10051, 10065

AF_INET6 = 23
PAYLOAD = b"lagdoctor-icmp-probe-32-bytes!!!"   # 32 bytes, ping.exe's default size (NE 1.2)
MAX_TIMEOUT_MS = 60_000


def _check_size(struct: type, expected: int, source: str) -> None:
    """Refuse to load with a layout that differs from the size the research measured."""
    got = ctypes.sizeof(struct)
    if got != expected:
        raise RuntimeError(f"{struct.__name__} is {got} bytes, expected {expected} ({source}); "
                           "the ctypes layout no longer matches Windows")


class IP_OPTION_INFORMATION(ctypes.Structure):      # 16 bytes on x64 (NE 1.1)
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class ICMP_ECHO_REPLY(ctypes.Structure):            # 40 bytes on x64: Data @16, Options @24 (NE 1.1)
    _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong),
                ("RoundTripTime", ctypes.c_ulong), ("DataSize", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                ("Options", IP_OPTION_INFORMATION)]


class IPV6_ADDRESS_EX(ctypes.Structure):            # packed (packon.h), 26 bytes (NE 1.4, log #4)
    _pack_ = 1
    _fields_ = [("sin6_port", ctypes.c_ushort), ("sin6_flowinfo", ctypes.c_ulong),
                ("sin6_addr", ctypes.c_ubyte * 16), ("sin6_scope_id", ctypes.c_ulong)]


class ICMPV6_ECHO_REPLY(ctypes.Structure):          # 36 bytes: Status @28, RTT @32, data follows (NE 1.1)
    _fields_ = [("Address", IPV6_ADDRESS_EX), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_uint)]


class SOCKADDR_IN6(ctypes.Structure):               # 28 bytes (NE 1.5)
    _fields_ = [("sin6_family", ctypes.c_ushort), ("sin6_port", ctypes.c_ushort),
                ("sin6_flowinfo", ctypes.c_ulong), ("sin6_addr", ctypes.c_ubyte * 16),
                ("sin6_scope_id", ctypes.c_ulong)]


_check_size(IP_OPTION_INFORMATION, 16, "NE 1.1")
_check_size(ICMP_ECHO_REPLY, 40, "NE 1.1")
_check_size(IPV6_ADDRESS_EX, 26, "NE log #5")
_check_size(ICMPV6_ECHO_REPLY, 36, "NE 1.1")
_check_size(SOCKADDR_IN6, 28, "NE 1.5")

# Reply buffers: the reply struct + payload + 8 for an ICMP error message (NE 1.1), plus 8 spare as in
# the tested recipe (NE 1.2); IPv6 also needs room for an IO_STATUS_BLOCK, 16 bytes on x64 (NE 1.4).
REPLY4_SIZE = ctypes.sizeof(ICMP_ECHO_REPLY) + len(PAYLOAD) + 8 + 8
REPLY6_SIZE = ctypes.sizeof(ICMPV6_ECHO_REPLY) + len(PAYLOAD) + 8 + 16
DATA_OFFSET4 = ctypes.sizeof(ICMP_ECHO_REPLY)     # the payload sits right after the reply (NE 1.3)
DATA_OFFSET6 = ctypes.sizeof(ICMPV6_ECHO_REPLY)

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
_iphlpapi.IcmpCreateFile.argtypes = []
_iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
_iphlpapi.Icmp6CreateFile.argtypes = []
_iphlpapi.Icmp6CreateFile.restype = wintypes.HANDLE
_iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
_iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
_iphlpapi.IcmpSendEcho2.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ushort,
                                    ctypes.POINTER(IP_OPTION_INFORMATION), ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD]
_iphlpapi.IcmpSendEcho2.restype = wintypes.DWORD
_iphlpapi.Icmp6SendEcho2.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(SOCKADDR_IN6), ctypes.POINTER(SOCKADDR_IN6),
                                     ctypes.c_void_p, ctypes.c_ushort,
                                     ctypes.POINTER(IP_OPTION_INFORMATION), ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD]
_iphlpapi.Icmp6SendEcho2.restype = wintypes.DWORD

_INVALID_HANDLE = ctypes.c_void_p(-1).value


def is_network_status(code: int) -> bool:
    """True for codes that describe the network (no reply, unreachable ...), False for API misuse.

    IP_STATUS codes are 11000 and up (NE 1.1). The Win32 unreachable codes are how the call reports
    "no route" (1231 for IPv6 here, NE 1.4). Anything else (87 bad parameter, 6 bad handle) is a bug
    or a broken system and is raised, not counted as a lost probe.
    """
    return 11000 <= code <= 11999 or code in (ERROR_NETWORK_UNREACHABLE, ERROR_HOST_UNREACHABLE,
                                              WSAENETUNREACH, WSAEHOSTUNREACH)


def check_target(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address we are willing to ping. Raises ValueError for anything else.

    Unspecified, multicast and limited-broadcast addresses are refused: they mean "nobody" or
    "everybody". An IPv6 scope must be a numeric interface index ("fe80::1%8").
    """
    if not isinstance(ip, str) or not 0 < len(ip) <= 64:
        raise ValueError(f"not an IP address: {ip!r}")
    addr = ipaddress.ip_address(ip.strip())
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_unspecified or addr.is_multicast or addr == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError(f"refusing to ping {addr}: not a single host")
    if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id is not None and not addr.scope_id.isdigit():
        raise ValueError(f"IPv6 scope must be an interface index: {ip!r}")
    return addr


def _check_int(name: str, value: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer from {lo} to {hi}, got {value!r}")
    return value


class Pinger:
    """Reusable ICMP handles and buffers. One thread at a time (calls are serialised by a lock)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._h4: int | None = None
        self._h6: int | None = None
        self._request = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))
        self._reply4 = ctypes.create_string_buffer(REPLY4_SIZE)
        self._reply6 = ctypes.create_string_buffer(REPLY6_SIZE)
        self._options = IP_OPTION_INFORMATION()
        self._src6 = SOCKADDR_IN6()      # all zero with the family set: "any source" (NE 1.4)
        self._src6.sin6_family = AF_INET6
        self._dst6 = SOCKADDR_IN6()
        self._closed = False

    def __enter__(self) -> "Pinger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for h in (self._h4, self._h6):
                if h:
                    _iphlpapi.IcmpCloseHandle(h)
            self._h4 = self._h6 = None

    def ping(self, ip: str, timeout_ms: int = 1000, ttl: int = 128) -> tuple[int, float | None, str | None]:
        """(status, rtt_ms, replying address). status 0 = echo reply; rtt_ms is None when nothing replied.

        An ICMP error reply (TTL expired, unreachable) returns its status, the time it took and the
        router that sent it. Raises ValueError for bad arguments and OSError when the API itself fails.
        """
        addr = check_target(ip)
        _check_int("timeout_ms", timeout_ms, 1, MAX_TIMEOUT_MS)
        _check_int("ttl", ttl, 1, 255)
        with self._lock:
            if self._closed:
                raise ValueError("Pinger is closed")
            self._options.Ttl = ttl
            if addr.version == 4:
                return self._ping4(addr, timeout_ms)
            return self._ping6(addr, timeout_ms)

    # ---- internals (lock held) ------------------------------------------
    def _handle(self, version: int) -> int:
        if version == 4:
            if not self._h4:
                self._h4 = _open(_iphlpapi.IcmpCreateFile)
            return self._h4
        if not self._h6:
            self._h6 = _open(_iphlpapi.Icmp6CreateFile)
        return self._h6

    def _ping4(self, addr: ipaddress.IPv4Address, timeout_ms: int) -> tuple[int, float | None, str | None]:
        h = self._handle(4)
        ctypes.memset(self._reply4, 0, REPLY4_SIZE)     # a stale reply must never read as a fresh one
        dest = int.from_bytes(addr.packed, "little")     # IPAddr: network-order bytes read as a ULONG (NE 1.1)
        t0 = time.perf_counter()
        n = _iphlpapi.IcmpSendEcho2(h, None, None, None, dest, self._request, len(PAYLOAD),
                                    ctypes.byref(self._options), self._reply4, REPLY4_SIZE, timeout_ms)
        rtt_ms = (time.perf_counter() - t0) * 1000.0
        if n == 0:
            return _no_reply(ctypes.get_last_error())
        reply = ICMP_ECHO_REPLY.from_buffer(self._reply4)
        frm = str(ipaddress.IPv4Address(reply.Address.to_bytes(4, "little")))
        return int(reply.Status), rtt_ms, frm

    def _ping6(self, addr: ipaddress.IPv6Address, timeout_ms: int) -> tuple[int, float | None, str | None]:
        h = self._handle(6)
        ctypes.memset(self._reply6, 0, REPLY6_SIZE)
        self._dst6.sin6_family = AF_INET6
        self._dst6.sin6_addr[:] = list(addr.packed)
        self._dst6.sin6_scope_id = int(addr.scope_id) if addr.scope_id else 0
        t0 = time.perf_counter()
        n = _iphlpapi.Icmp6SendEcho2(h, None, None, None, ctypes.byref(self._src6), ctypes.byref(self._dst6),
                                     self._request, len(PAYLOAD), ctypes.byref(self._options),
                                     self._reply6, REPLY6_SIZE, timeout_ms)
        rtt_ms = (time.perf_counter() - t0) * 1000.0
        if n == 0:
            return _no_reply(ctypes.get_last_error())
        reply = ICMPV6_ECHO_REPLY.from_buffer(self._reply6)
        frm = ipaddress.IPv6Address(bytes(reply.Address.sin6_addr))   # bytes 6..22 of Address (NE 1.4)
        text = str(frm)
        if frm.is_link_local and reply.Address.sin6_scope_id:
            text += f"%{reply.Address.sin6_scope_id}"
        return int(reply.Status), rtt_ms, text

    # Test hook for the layout self-check: the raw reply buffers of the last call.
    def _last_reply(self, version: int) -> ctypes.Array:
        return self._reply4 if version == 4 else self._reply6


def _open(create) -> int:
    h = create()
    if not h or h == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _no_reply(err: int) -> tuple[int, None, None]:
    if is_network_status(err):
        return err, None, None
    raise ctypes.WinError(err)


def ping(ip: str, timeout_ms: int = 1000, ttl: int = 128) -> tuple[int, float | None, str | None]:
    """One echo request: (ICMP status, RTT ms timed around the call, replying address). Success = status 0."""
    with Pinger() as p:
        return p.ping(ip, timeout_ms, ttl)


def self_check(version: int = 4) -> None:
    """NE 8.3: a loopback echo must succeed with our payload exactly where the x64 layout says.

    version 4: 127.0.0.1 must answer with Status 0, Data == reply buffer + 40 and the 32 payload bytes
    there. version 6: ::1 must answer with Status 0 and the payload right after the 36-byte reply.
    Raises RuntimeError when the layout drifted and OSError when the call itself fails.
    """
    loopback = {4: "127.0.0.1", 6: "::1"}[version]
    with Pinger() as p:
        status, _rtt, frm = p.ping(loopback, 1000)
        buf = p._last_reply(version)
        if status != IP_SUCCESS or frm != loopback:
            raise RuntimeError(f"loopback IPv{version} echo failed: status {status}, from {frm}")
        if version == 4:
            reply = ICMP_ECHO_REPLY.from_buffer(buf)
            want = ctypes.addressof(buf) + DATA_OFFSET4
            echoed = buf.raw[DATA_OFFSET4:DATA_OFFSET4 + len(PAYLOAD)]
            if reply.Data != want or reply.DataSize != len(PAYLOAD) or echoed != PAYLOAD:
                raise RuntimeError(f"ICMP_ECHO_REPLY layout drifted: Data={reply.Data or 0:#x} (want {want:#x}), "
                                   f"DataSize={reply.DataSize}")
        elif buf.raw[DATA_OFFSET6:DATA_OFFSET6 + len(PAYLOAD)] != PAYLOAD:
            raise RuntimeError("ICMPV6_ECHO_REPLY layout drifted: the payload is not right after the reply")
