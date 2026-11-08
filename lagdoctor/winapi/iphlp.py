"""Routes, interface counters and this PC's own addresses from iphlpapi, no admin needed.

Layouts from docs/research/network-and-etw.md (NE) 1.5 and its verification log (#20-#24), and
windows-telemetry.md (WT) 11.1 item 4: MIB_IF_ROW2 is 1,352 bytes, InterfaceIndex @8 (set it, leave
InterfaceLuid @0 zero), InOctets @1208, OutOctets @1280. GetAdaptersAddresses' entries are from the SDK
(um\\IPTypes.h:113-133 and 286-350, shared\\ws2def.h:258-269): Windows writes Length 448 into every adapter entry
and 64 into every unicast address entry (seen here, 2026-11-07, 11 adapters).

The route toward a destination comes from GetBestRoute2 every time, never from a cache: a route can
change between lookups (NE 1.3, log #13), and VPNs add split routes (NE 0 item 3).
"""
from __future__ import annotations

import ctypes
import ipaddress
import threading
import uuid
from ctypes import wintypes

AF_UNSPEC, AF_INET, AF_INET6 = 0, 2, 23
ERROR_FILE_NOT_FOUND = 2               # GetIfEntry2 on an index that does not exist (seen here)
ERROR_INVALID_DATA = 13
ERROR_BUFFER_OVERFLOW = 111            # SDK shared\winerror.h:954; GetAdaptersAddresses: the buffer is too small
ERROR_NO_DATA = 232                    # winerror.h:1845; GetAdaptersAddresses: no adapter has an address
ERROR_NOT_FOUND = 1168
ERROR_NETWORK_UNREACHABLE = 1231       # GetBestRoute2 with no route (NE 1.4)
ERROR_HOST_UNREACHABLE = 1232
NO_ROUTE = (ERROR_NOT_FOUND, ERROR_NETWORK_UNREACHABLE, ERROR_HOST_UNREACHABLE)

IF_TYPE_ETHERNET_CSMACD = 6            # SDK shared\ipifcons.h:49
IF_TYPE_PPP = 23                       # ipifcons.h:66
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_PROP_VIRTUAL = 53              # ipifcons.h:96; TAP, WireGuard-type and OpenVPN DCO tunnels report it
IF_TYPE_IEEE80211 = 71                 # ipifcons.h:114 (NE 1.5)
IF_TYPE_TUNNEL = 131                   # ipifcons.h:174; WAN miniports (SSTP, L2TP, IKEv2), Teredo, 6to4
NDIS_MEDIUM_IP = 19                    # SDK shared\ntddndis.h NDIS_MEDIUM: layer-3 tunnels (WireGuard-type)
IF_OPER_STATUS_UP = 1

_TUNNEL_TYPES = frozenset({IF_TYPE_PPP, IF_TYPE_PROP_VIRTUAL, IF_TYPE_TUNNEL})
# VPN clients that present an Ethernet-type adapter are only recognisable by name.
_VPN_WORDS = ("vpn", "tap-", "tunnel", "wireguard", "wintun", "openvpn", "nordlynx", "tailscale",
              "zerotier", "anyconnect", "globalprotect", "pangp", "fortinet")

# Internet addresses used only to ask "which way out would traffic go" (no packet is sent).
ROUTE_PROBES = ("1.1.1.1", "2606:4700:4700::1111")


def _check_size(struct: type, expected: int, source: str) -> None:
    got = ctypes.sizeof(struct)
    if got != expected:
        raise RuntimeError(f"{struct.__name__} is {got} bytes, expected {expected} ({source}); "
                           "the ctypes layout no longer matches Windows")


class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_ubyte * 4), ("sin_zero", ctypes.c_char * 8)]


class SOCKADDR_IN6(ctypes.Structure):
    _fields_ = [("sin6_family", ctypes.c_ushort), ("sin6_port", ctypes.c_ushort),
                ("sin6_flowinfo", ctypes.c_ulong), ("sin6_addr", ctypes.c_ubyte * 16),
                ("sin6_scope_id", ctypes.c_ulong)]


class SOCKADDR_INET(ctypes.Union):                 # 28 bytes (NE 1.5)
    _fields_ = [("Ipv4", SOCKADDR_IN), ("Ipv6", SOCKADDR_IN6), ("si_family", ctypes.c_ushort)]


class IP_ADDRESS_PREFIX(ctypes.Structure):         # 32 bytes (NE 1.5)
    _fields_ = [("Prefix", SOCKADDR_INET), ("PrefixLength", ctypes.c_ubyte)]


class MIB_IPFORWARD_ROW2(ctypes.Structure):        # 104 bytes (NE 1.5)
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong),
                ("DestinationPrefix", IP_ADDRESS_PREFIX), ("NextHop", SOCKADDR_INET),
                ("SitePrefixLength", ctypes.c_ubyte), ("ValidLifetime", ctypes.c_ulong),
                ("PreferredLifetime", ctypes.c_ulong), ("Metric", ctypes.c_ulong), ("Protocol", ctypes.c_int),
                ("Loopback", ctypes.c_ubyte), ("AutoconfigureAddress", ctypes.c_ubyte),
                ("Publish", ctypes.c_ubyte), ("Immortal", ctypes.c_ubyte), ("Age", ctypes.c_ulong),
                ("Origin", ctypes.c_int)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


class MIB_IF_ROW2(ctypes.Structure):               # 1,352 bytes (SDK shared\netioapi.h:154-219; NE 1.5)
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong), ("InterfaceGuid", GUID),
                ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
                ("PhysicalAddressLength", ctypes.c_ulong), ("PhysicalAddress", ctypes.c_ubyte * 32),
                ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong),
                ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
                ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int),
                ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),     # 8 BOOLEAN bit-fields; bit 0 = hardware
                ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
                ("NetworkGuid", GUID), ("ConnectionType", ctypes.c_int),
                ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64)] + \
               [(n, ctypes.c_uint64) for n in (
                   "InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards", "InErrors", "InUnknownProtos",
                   "InUcastOctets", "InMulticastOctets", "InBroadcastOctets", "OutOctets", "OutUcastPkts",
                   "OutNUcastPkts", "OutDiscards", "OutErrors", "OutUcastOctets", "OutMulticastOctets",
                   "OutBroadcastOctets", "OutQLen")]


class SOCKET_ADDRESS(ctypes.Structure):            # 16 bytes (SDK shared\ws2def.h:258-269)
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class IP_ADAPTER_UNICAST_ADDRESS_LH(ctypes.Structure):    # 64 bytes (SDK um\IPTypes.h:113-133)
    _fields_ = [("Length", ctypes.c_ulong), ("Flags", wintypes.DWORD), ("Next", ctypes.c_void_p),
                ("Address", SOCKET_ADDRESS), ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int),
                ("DadState", ctypes.c_int), ("ValidLifetime", ctypes.c_ulong), ("PreferredLifetime", ctypes.c_ulong),
                ("LeaseLifetime", ctypes.c_ulong), ("OnLinkPrefixLength", ctypes.c_ubyte)]


class IP_ADAPTER_ADDRESSES_LH(ctypes.Structure):          # 448 bytes (SDK um\IPTypes.h:286-350)
    _fields_ = [("Length", ctypes.c_ulong), ("IfIndex", ctypes.c_ulong), ("Next", ctypes.c_void_p),
                ("AdapterName", ctypes.c_void_p), ("FirstUnicastAddress", ctypes.c_void_p),
                ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
                ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_void_p),
                ("Description", ctypes.c_void_p), ("FriendlyName", ctypes.c_void_p),
                ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", ctypes.c_ulong),
                ("Flags", ctypes.c_ulong), ("Mtu", ctypes.c_ulong), ("IfType", ctypes.c_ulong),
                ("OperStatus", ctypes.c_int), ("Ipv6IfIndex", ctypes.c_ulong), ("ZoneIndices", ctypes.c_ulong * 16),
                ("FirstPrefix", ctypes.c_void_p), ("TransmitLinkSpeed", ctypes.c_uint64),
                ("ReceiveLinkSpeed", ctypes.c_uint64), ("FirstWinsServerAddress", ctypes.c_void_p),
                ("FirstGatewayAddress", ctypes.c_void_p), ("Ipv4Metric", ctypes.c_ulong),
                ("Ipv6Metric", ctypes.c_ulong), ("Luid", ctypes.c_uint64), ("Dhcpv4Server", SOCKET_ADDRESS),
                ("CompartmentId", ctypes.c_uint32), ("NetworkGuid", GUID), ("ConnectionType", ctypes.c_int),
                ("TunnelType", ctypes.c_int), ("Dhcpv6Server", SOCKET_ADDRESS),
                ("Dhcpv6ClientDuid", ctypes.c_ubyte * 130), ("Dhcpv6ClientDuidLength", ctypes.c_ulong),
                ("Dhcpv6Iaid", ctypes.c_ulong), ("FirstDnsSuffix", ctypes.c_void_p)]


_check_size(SOCKADDR_INET, 28, "NE 1.5")
_check_size(IP_ADDRESS_PREFIX, 32, "NE 1.5")
_check_size(MIB_IPFORWARD_ROW2, 104, "NE 1.5")
_check_size(MIB_IF_ROW2, 1352, "NE 1.5, WT 11.1")
_check_size(SOCKET_ADDRESS, 16, "SDK shared\\ws2def.h")
_check_size(IP_ADAPTER_UNICAST_ADDRESS_LH, 64, "SDK um\\IPTypes.h")
_check_size(IP_ADAPTER_ADDRESSES_LH, 448, "SDK um\\IPTypes.h")
for _struct, _field, _off in (
        (MIB_IF_ROW2, "InterfaceIndex", 8), (MIB_IF_ROW2, "Alias", 28), (MIB_IF_ROW2, "Type", 1128),
        (MIB_IF_ROW2, "OperStatus", 1156), (MIB_IF_ROW2, "TransmitLinkSpeed", 1192), (MIB_IF_ROW2, "InOctets", 1208),
        (MIB_IF_ROW2, "OutOctets", 1280), (MIB_IF_ROW2, "OutQLen", 1344),        # NE 1.5 / log #24 and WT 11.1
        (IP_ADAPTER_UNICAST_ADDRESS_LH, "Next", 8), (IP_ADAPTER_UNICAST_ADDRESS_LH, "Address", 16),
        (IP_ADAPTER_ADDRESSES_LH, "Next", 8), (IP_ADAPTER_ADDRESSES_LH, "FirstUnicastAddress", 24),
        (IP_ADAPTER_ADDRESSES_LH, "FriendlyName", 72), (IP_ADAPTER_ADDRESSES_LH, "Ipv6IfIndex", 108),
        (IP_ADAPTER_ADDRESSES_LH, "Luid", 224), (IP_ADAPTER_ADDRESSES_LH, "FirstDnsSuffix", 440)):  # the SDK's
    if getattr(_struct, _field).offset != _off:
        raise RuntimeError(f"{_struct.__name__}.{_field} is at {getattr(_struct, _field).offset}, expected {_off}")

_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
_iphlpapi.GetBestRoute2.argtypes = [ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p,
                                    ctypes.POINTER(SOCKADDR_INET), wintypes.ULONG,
                                    ctypes.POINTER(MIB_IPFORWARD_ROW2), ctypes.POINTER(SOCKADDR_INET)]
_iphlpapi.GetBestRoute2.restype = wintypes.DWORD
_iphlpapi.GetIfEntry2.argtypes = [ctypes.POINTER(MIB_IF_ROW2)]
_iphlpapi.GetIfEntry2.restype = wintypes.DWORD
_iphlpapi.GetIpForwardTable2.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)]
_iphlpapi.GetIpForwardTable2.restype = wintypes.DWORD
_iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]
_iphlpapi.FreeMibTable.restype = None
_iphlpapi.GetAdaptersAddresses.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.POINTER(wintypes.ULONG)]
_iphlpapi.GetAdaptersAddresses.restype = wintypes.ULONG

_tls = threading.local()         # one reusable MIB_IF_ROW2 per calling thread
MAX_ROUTES = 65_536              # sanity bound on the route table walk

# Only the unicast lists are read, so the others are left out (SDK um\IPTypes.h:403-405).
GAA_FLAG_SKIP_ANYCAST, GAA_FLAG_SKIP_MULTICAST, GAA_FLAG_SKIP_DNS_SERVER = 0x0002, 0x0004, 0x0008
_GAA_FLAGS = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
GAA_START_BYTES = 16 * 1024      # Microsoft's advice is to start at 15 KB; about 17 KB was needed here (11 adapters)
GAA_MAX_BYTES = 16 << 20
GAA_HEADROOM = 4096              # adapters can appear between the call that was too small and the retry
MAX_ADAPTERS = 4096              # sanity bounds on the two list walks
MAX_ADAPTER_ADDRESSES = 4096
NAME_CHARS = 257                 # IF_MAX_STRING_SIZE + 1, the size of MIB_IF_ROW2.Alias
# The fields read from each entry end here, so the Length Windows writes into it must reach at least this far.
_ADAPTER_READ = IP_ADAPTER_ADDRESSES_LH.Ipv6IfIndex.offset + ctypes.sizeof(ctypes.c_ulong)
_UNICAST_READ = IP_ADAPTER_UNICAST_ADDRESS_LH.Address.offset + ctypes.sizeof(SOCKET_ADDRESS)
_SOCKADDR_SIZE = {AF_INET: ctypes.sizeof(SOCKADDR_IN), AF_INET6: ctypes.sizeof(SOCKADDR_IN6)}


def _parse_ip(ip: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not isinstance(ip, str) or not 0 < len(ip) <= 64:
        raise ValueError(f"not an IP address: {ip!r}")
    return ipaddress.ip_address(ip.strip())


def _sockaddr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> SOCKADDR_INET:
    sa = SOCKADDR_INET()
    if addr.version == 4:
        sa.Ipv4.sin_family = AF_INET
        sa.Ipv4.sin_addr[:] = list(addr.packed)
    else:
        sa.Ipv6.sin6_family = AF_INET6
        sa.Ipv6.sin6_addr[:] = list(addr.packed)
        if addr.scope_id and addr.scope_id.isdigit():
            sa.Ipv6.sin6_scope_id = int(addr.scope_id)
    return sa


def _sockaddr_text(sa: SOCKADDR_INET) -> str | None:
    """The address as text, None for an unspecified one (an on-link route has next hop 0.0.0.0)."""
    if sa.si_family == AF_INET:
        addr = ipaddress.IPv4Address(bytes(sa.Ipv4.sin_addr))
    elif sa.si_family == AF_INET6:
        addr = ipaddress.IPv6Address(bytes(sa.Ipv6.sin6_addr))
    else:
        return None
    if addr.is_unspecified:
        return None
    if addr.version == 6 and addr.is_link_local and sa.Ipv6.sin6_scope_id:
        return f"{addr}%{sa.Ipv6.sin6_scope_id}"
    return str(addr)


def _guid_text(g: GUID) -> str:
    return "{" + str(uuid.UUID(bytes_le=bytes(g))).upper() + "}"


def is_tunnel(if_type: int, media_type: int, description: str) -> bool:
    """A VPN or other tunnel, judged by interface type, NDIS medium and, failing those, the adapter name."""
    if if_type in _TUNNEL_TYPES or media_type == NDIS_MEDIUM_IP:
        return True
    desc = (description or "").lower()
    return any(w in desc for w in _VPN_WORDS)


def best_route(dest_ip: str) -> dict | None:
    """The route the stack would use toward dest_ip: {if_index, next_hop, source}; None when there is none.

    next_hop is None for an on-link destination (no gateway in between).
    """
    dest = _sockaddr(_parse_ip(dest_ip))
    row, src = MIB_IPFORWARD_ROW2(), SOCKADDR_INET()
    rc = _iphlpapi.GetBestRoute2(None, 0, None, ctypes.byref(dest), 0, ctypes.byref(row), ctypes.byref(src))
    if rc in NO_ROUTE:
        return None
    if rc:
        raise ctypes.WinError(rc)
    return {"if_index": int(row.InterfaceIndex), "next_hop": _sockaddr_text(row.NextHop),
            "source": _sockaddr_text(src)}


def if_row(if_index: int) -> dict:
    """GetIfEntry2 for one interface. Raises OSError (winerror 2) when the index does not exist.

    {in_octets, out_octets, type, description, oper_status, tunnel, alias, guid, media_type,
     physical_medium, hardware, tx_link_bps, rx_link_bps}; link speeds are bit/s.
    """
    if isinstance(if_index, bool) or not isinstance(if_index, int) or not 0 < if_index <= 0xFFFFFFFF:
        raise ValueError(f"interface index must be 1..0xFFFFFFFF, got {if_index!r}")
    row = getattr(_tls, "row", None)
    if row is None:
        row = _tls.row = MIB_IF_ROW2()
    ctypes.memset(ctypes.byref(row), 0, ctypes.sizeof(row))     # InterfaceLuid must be zero (WT 11.1)
    row.InterfaceIndex = if_index
    rc = _iphlpapi.GetIfEntry2(ctypes.byref(row))
    if rc:
        raise ctypes.WinError(rc)
    return {"in_octets": int(row.InOctets), "out_octets": int(row.OutOctets), "type": int(row.Type),
            "description": row.Description, "oper_status": int(row.OperStatus),
            "tunnel": is_tunnel(row.Type, row.MediaType, row.Description), "alias": row.Alias,
            "guid": _guid_text(row.InterfaceGuid), "media_type": int(row.MediaType),
            "physical_medium": int(row.PhysicalMediumType),
            "hardware": bool(row.InterfaceAndOperStatusFlags & 1),
            "tx_link_bps": int(row.TransmitLinkSpeed), "rx_link_bps": int(row.ReceiveLinkSpeed)}


def default_routes() -> list[dict]:
    """Every /0 route with a gateway: [{family, if_index, next_hop, metric}], IPv4 and IPv6 (NE 1.5)."""
    table = ctypes.c_void_p()
    rc = _iphlpapi.GetIpForwardTable2(AF_UNSPEC, ctypes.byref(table))
    if rc:
        raise ctypes.WinError(rc)
    try:
        n = ctypes.c_ulong.from_address(table.value).value
        if n > MAX_ROUTES:
            raise OSError(f"route table claims {n} rows")
        # ULONG NumEntries, then the rows 8-byte aligned at +8 (NE 1.5, fact-check: not +4).
        rows = (MIB_IPFORWARD_ROW2 * n).from_address(table.value + 8)
        out = []
        for r in rows:
            if r.DestinationPrefix.PrefixLength != 0:
                continue
            hop = _sockaddr_text(r.NextHop)
            if hop is None:
                continue
            out.append({"family": 4 if r.DestinationPrefix.Prefix.si_family == AF_INET else 6,
                        "if_index": int(r.InterfaceIndex), "next_hop": hop, "metric": int(r.Metric)})
        return out
    finally:
        _iphlpapi.FreeMibTable(table)


def pick_default_route(best: dict | None, best_tunnel: bool, routes: list[dict],
                       usable: dict[int, bool]) -> dict | None:
    """Choose the router: {next_hop, if_index}.

    `best` is GetBestRoute2 toward the internet. When it leaves through a real interface via a gateway,
    that gateway is the answer (the stack has already weighed every metric). When it leaves through a
    VPN, the router is still the gateway of the physical link, so the /0 routes are searched for one on
    a usable (up, not tunnel) interface, IPv4 first, then by route metric. Interface metrics are not
    read, so with a VPN up and two physical links the choice between those two is approximate.
    """
    if best and best.get("next_hop") and not best_tunnel:
        return {"next_hop": best["next_hop"], "if_index": best["if_index"]}
    candidates = [r for r in routes if usable.get(r["if_index"])]
    if not candidates:
        return None
    r = min(candidates, key=lambda r: (r["family"], r["metric"], r["if_index"]))
    return {"next_hop": r["next_hop"], "if_index": r["if_index"]}


def default_route(toward: tuple[str, ...] = ROUTE_PROBES) -> dict | None:
    """The router traffic to the internet goes through, outside any VPN: {next_hop, if_index} or None."""
    best = None
    for dest in toward:
        best = best_route(dest)
        if best:
            break
    best_tunnel = False
    if best:
        try:
            best_tunnel = if_row(best["if_index"])["tunnel"]
        except OSError:
            best_tunnel = True          # cannot tell: fall back to the route table
    if best and best["next_hop"] and not best_tunnel:
        return pick_default_route(best, False, [], {})
    routes = default_routes()
    usable: dict[int, bool] = {}
    for r in routes:
        idx = r["if_index"]
        if idx in usable:
            continue
        try:
            row = if_row(idx)
            usable[idx] = row["oper_status"] == IF_OPER_STATUS_UP and not row["tunnel"]
        except OSError:
            usable[idx] = False
    return pick_default_route(best, best_tunnel, routes, usable)


def default_gateway() -> str | None:
    """The default gateway (your router) as text, or None when there is none."""
    route = default_route()
    return route["next_hop"] if route else None


# ---------------------------------------------------------------- this PC's addresses ---

def _layout_error(msg: str) -> OSError:
    err = OSError(0, f"GetAdaptersAddresses returned data that does not fit the expected layout: {msg}")
    err.winerror = ERROR_INVALID_DATA
    return err


def adapter_addresses(*, start_bytes: int = GAA_START_BYTES) -> list[dict]:
    """Every adapter's unicast IPv4 and IPv6 addresses (GetAdaptersAddresses; no process handles), in Windows' order:
    [{alias, if_index, addresses: [text]}]. Adapters that are down are listed too, as psutil.net_if_addrs() lists
    them. An IPv6 link-local address carries its zone ("fe80::1%8"), as best_route() writes one; if_index is 0
    when neither IPv4 nor IPv6 is bound to the adapter."""
    if isinstance(start_bytes, bool) or not isinstance(start_bytes, int) or not 0 < start_bytes <= GAA_MAX_BYTES:
        raise ValueError(f"start_bytes must be 1..{GAA_MAX_BYTES}, got {start_bytes!r}")
    size = start_bytes
    for _ in range(4):
        buf = (ctypes.c_ulonglong * ((size + 7) // 8))()      # 8-byte aligned: the entries hold pointers
        have = ctypes.sizeof(buf)
        asked = wintypes.ULONG(have)
        rc = _iphlpapi.GetAdaptersAddresses(AF_UNSPEC, _GAA_FLAGS, None, buf, ctypes.byref(asked))
        if rc == 0:
            return _parse_adapters(ctypes.addressof(buf), have)
        if rc == ERROR_NO_DATA:
            return []
        if rc != ERROR_BUFFER_OVERFLOW:
            raise ctypes.WinError(rc)
        need = asked.value if asked.value > have else have * 2
        size = need + GAA_HEADROOM
        if size > GAA_MAX_BYTES:
            raise OSError(0, f"GetAdaptersAddresses needs {need} bytes, over the {GAA_MAX_BYTES}-byte cap")
    raise OSError(0, "GetAdaptersAddresses kept asking for a bigger buffer")


def _parse_adapters(base: int, size: int) -> list[dict]:
    """Walk the lists GetAdaptersAddresses wrote into the buffer at `base`. Every entry, string and socket
    address it points to lies inside that buffer, so a pointer that leads outside it means the layout drifted:
    that is an OSError, never a read of memory that is not ours."""
    end = base + size

    def entry(ptr: int, read: int, what: str) -> int:
        """The Length of the entry at ptr, once the entry is known to lie inside the buffer."""
        if ptr < base or ptr + read > end:
            raise _layout_error(f"{what} at offset {ptr - base} is outside the {size}-byte buffer")
        if (ptr - base) % 8:
            raise _layout_error(f"{what} at offset {ptr - base} is not 8-byte aligned")
        length = ctypes.c_ulong.from_address(ptr).value
        if length < read or ptr + length > end:
            raise _layout_error(f"{what} at offset {ptr - base} claims {length} bytes")
        return length

    out: list[dict] = []
    ptr = base                              # the first adapter starts the buffer
    for _ in range(MAX_ADAPTERS):
        entry(ptr, _ADAPTER_READ, "an adapter")
        a = IP_ADAPTER_ADDRESSES_LH.from_address(ptr)
        addresses: list[str] = []
        u_ptr = a.FirstUnicastAddress or 0
        for _ in range(MAX_ADAPTER_ADDRESSES + 1):     # the last turn only sees the list end
            if not u_ptr:
                break
            entry(u_ptr, _UNICAST_READ, "a unicast address")
            u = IP_ADAPTER_UNICAST_ADDRESS_LH.from_address(u_ptr)
            text = _socket_address_text(u.Address, base, end)
            if text is not None:
                addresses.append(text)
            u_ptr = u.Next or 0
        else:
            raise _layout_error("an adapter's address list never ended")
        out.append({"alias": _wide_text(a.FriendlyName or 0, base, end), "if_index": int(a.IfIndex or a.Ipv6IfIndex),
                    "addresses": addresses})
        ptr = a.Next or 0
        if not ptr:
            return out
    raise _layout_error("the adapter list never ended")


def _socket_address_text(sa: SOCKET_ADDRESS, base: int, end: int) -> str | None:
    """An IPv4 or IPv6 address as text; None for another family or an unspecified address (psutil skips the
    first; Windows assigns no interface the second)."""
    ptr, length = sa.lpSockaddr or 0, sa.iSockaddrLength
    if length < 2:
        raise _layout_error(f"a socket address claims {length} bytes")
    if not ptr or ptr < base or ptr + length > end:
        raise _layout_error(f"a {length}-byte socket address points outside the buffer")
    family = ctypes.c_ushort.from_address(ptr).value
    need = _SOCKADDR_SIZE.get(family)
    if need is None:
        return None
    if length < need:
        raise _layout_error(f"a family {family} socket address is {length} bytes, under {need}")
    inet = SOCKADDR_INET()
    ctypes.memmove(ctypes.byref(inet), ptr, need)
    return _sockaddr_text(inet)


def _wide_text(ptr: int, base: int, end: int) -> str:
    """A NUL-terminated UTF-16 string inside the buffer, at most NAME_CHARS long with its NUL; "" for NULL."""
    if not ptr:
        return ""
    if ptr < base or ptr >= end or (ptr - base) % 2:
        raise _layout_error(f"a name at offset {ptr - base} is outside the buffer")
    raw = ctypes.string_at(ptr, min(end - ptr, 2 * NAME_CHARS))
    text = raw.decode("utf-16-le", errors="replace")
    nul = text.find("\0")
    if nul < 0:
        raise _layout_error(f"a name at offset {ptr - base} has no end within {NAME_CHARS} characters")
    return text[:nul]
