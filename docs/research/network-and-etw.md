# Network lag diagnosis and real-time ETW from Python (research)

Researched 2026-11-04 on the target machine (Windows 11 Home 10.0.26200, i7-12650H, 16 logical CPUs, Intel Wi-Fi 6
AX201 on Wi-Fi, Python 3.13.3 x64 from the project's uv venv, **unelevated, Medium integrity, not in Performance Log
Users**). Everything below was measured read-only. Nothing was changed on the system.

**Fact-checked the same day** by a second, adversarial pass on the same machine, also unelevated: every URL was
re-fetched, every SDK line re-read, and every probe re-run from fresh scripts (the doc's own code blocks were executed
from this markdown). Corrections are marked **(fact-check)** in the text; the evidence for each claim is in the
[Verification log](#verification-log) at the end. Fact-check scripts live in
`<SCRATCH>-verify` (same parent folder as `<SCRATCH>`, also temporary).

## How to read this document

- `[verified: …]` names the URL (with the docs commit id where Learn shows one) or the exact local command that
  produced the fact. `[inferred]` is my reasoning from verified facts. `[unverified]` could not be checked here.
- "SDK" means the Windows SDK headers installed on this machine:
  `C:\Program Files (x86)\Windows Kits\10\Include\10.0.19041.0\` (file and line given in each tag).
- "run X.py" means: `cd <repo>; uv run python -I <SCRATCH>\X.py` where `<repo>` is the lag-doctor folder and
  `<SCRATCH>` = `%TEMP%\claude\…\scratchpad\research-network-and-etw`.
  That folder is temporary, so every snippet the implementation needs is reproduced here.
- Public addresses of my ISP's routers, the SSID, BSSID and MAC addresses are redacted (`<ISP hop 2>` etc.);
  the home network's addresses read `192.168.0.x`, and adapter GUIDs `{…}`.

## 0. Key findings

1. **ICMP without ping.exe works unelevated.** `IcmpSendEcho2` (iphlpapi.dll) gave gateway RTT 1-4 ms and 1.1.1.1
   RTT 5-7 ms here. The x64 `ICMP_ECHO_REPLY` is 40 bytes with an 8-byte `Data` pointer; the Learn remark that 64-bit
   callers "should" use `ICMP_ECHO_REPLY32` is wrong for a 64-bit process (tested). `Timeout` is not a hard deadline.
   [verified: run net_probe.py, net_probe2.py, net_probe4.py] **(fact-check)** Re-confirmed (gateway 1-2 ms,
   1.1.1.1 6-7 ms, payload at +40). With no reply, the wait was 210-1004 ms for Timeouts of 20-1000 ms, not the
   fixed ~500 ms first reported (1.3) [verified: run v_net.py, v_timeout.py].
2. **A reply count of 1 is not success.** `IcmpSendEcho2` returns 1 for ICMP errors too (TTL expired 11013, net
   unreachable 11002); check `Status == 0`. [verified: run net_probe.py]
3. **Default gateway and egress interface: `GetBestRoute2` toward the actual destination**, unelevated, returns
   ifIndex 8 (Wi-Fi), next hop 192.168.0.1, source 192.168.0.225. A machine can also have VPN tunnel adapters, so
   always route per destination. [verified: run net_probe.py; `Get-NetAdapter`]
4. **Wi-Fi: location permission is off on this machine, so the classic calls are blocked.** `netsh wlan show
   interfaces` prints "Network shell commands need location permission…" and "WlanQueryInterface returns error 5".
   But `wlan_intf_opcode_realtime_connection_quality` (opcode 19) works unelevated without location: RSSI -53 dBm,
   link quality 86, Rx 173.3 / Tx 433.3 Mbit/s, 5180 MHz, PHY 802.11ac. [verified: `netsh wlan show interfaces`; run wlan_probe.py]
5. **The "spike every 60 s" scan is visible without admin.** Registering for ACM notifications delivered
   `wlan_notification_acm_scan_complete` at t = 19.4, 79.4, 139.4, 199.4 s: exactly every 60.0 s while connected.
   [verified: run wlan_probe.py 240; fact-check re-run v_wlan.py 135: t = 28.1, 88.1 s]
6. **Microsoft-Windows-Kernel-Network** = `{7dd42a49-5329-4832-8dfd-43d979153a88}`, keywords IPV4 `0x10`, IPV6
   `0x20`, level 4, registered by the kernel (PID 0). UDPv4 send/recv = events 42/43, UDPv6 = 58/59, TCPv4 send/recv
   = 10/11, TCPv6 = 26/27, TCP retransmit = 14/30. Ports are big-endian. [verified: `Get-WinEvent -ListProvider Microsoft-Windows-Kernel-Network`; `logman query providers "Microsoft-Windows-Kernel-Network"`]
7. **ETW from ctypes works end to end**, proven unelevated with a private in-process session: start, enable, write
   100,000 events shaped exactly like Kernel-Network event 43, stop, consume with `OpenTraceW`/`ProcessTrace` on a
   worker thread, payload byte-exact, 0 events lost, Python decode rate 46,323 events/s.
   [verified: run etw_probe.py]
8. **Negative test:** unelevated `StartTraceW` for a real-time session returns **5 (ERROR_ACCESS_DENIED)**.
   [verified: run etw_probe.py]
9. **Performance Log Users (S-1-5-32-559) is the documented non-admin route** for starting sessions, enabling
   providers and consuming real time. The provider-specific ACL could not be read unelevated (neither the registry
   key nor `EventAccessQuery`, which returns 5), so it is untested for Kernel-Network.
   [verified: learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-starttracew @2d357572, …/nf-evntrace-enabletraceex2 @540c1f2d, …/nf-evntrace-opentracew @2d357572; registry read of `HKLM:\SYSTEM\CurrentControlSet\Control\WMI\Security` → "Requested registry access is not allowed."; run v_access.py → `EventAccessQuery` rc 5]
10. **Without ETW you cannot see a game's UDP peer.** `psutil.net_connections()` gives PIDs for all 728 sockets
    unelevated, but 0 of 46 UDP sockets have a remote address. Process I/O counters are not a byte meter for sockets.
    [verified: psutil one-liner; run iocounters_probe.py]
11. **Background downloads are visible unelevated:** Steam's `logs\content_log.txt` logs "Current download rate:
    98.213 Mbps" and "update canceled : Disabled (Suspended)"; `Get-DeliveryOptimizationPerfSnap` reports
    background download counts unelevated. [verified: local files and cmdlets, section 6]
12. **(fact-check) Which payload address is the remote peer probably differs between TCP and UDP.** System Informer's
    parser for the classic kernel events (same 28-byte layout) treats `saddr` as local for TCP send *and* receive, but
    swaps the endpoints for UDP receive ("The destination endpoint corresponds to the local socket"). Kernel-Network
    itself is still unverified here, so classify per event by the local-address set (4.4).
    [verified: System Informer source @0a4975a4, see 4.4; unverified: Kernel-Network semantics]
13. **(fact-check) Disabling Wi-Fi background scan is permitted for standard users:** the DACL on
    `wlan_secure_bc_scan_enabled` grants `BUILTIN\Users` write access, and `WlanGetSecuritySettings` reports granted
    access 0x70023 (`WLAN_WRITE_ACCESS`) for this unelevated token. Not executed (it changes state).
    [verified: run v_wlan.py; learn.microsoft.com/en-us/windows/win32/api/wlanapi/ne-wlanapi-wlan_securable_object @92672624]

---

## 1. ICMP latency with IcmpSendEcho2 (no localized ping output)

### 1.1 Signatures and structures

| Item | Definition | Source |
|---|---|---|
| `HANDLE IcmpCreateFile(void)` | `INVALID_HANDLE_VALUE` on failure | [verified: SDK um\icmpapi.h:79-81] |
| `DWORD IcmpSendEcho2(HANDLE IcmpHandle, HANDLE Event, PIO_APC_ROUTINE/FARPROC ApcRoutine, PVOID ApcContext, IPAddr DestinationAddress, LPVOID RequestData, WORD RequestSize, PIP_OPTION_INFORMATION RequestOptions, LPVOID ReplyBuffer, DWORD ReplySize, DWORD Timeout)` | synchronous when `Event` and `ApcRoutine` are NULL | [verified: SDK um\icmpapi.h:287-306; learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmpsendecho2 @92672624] |
| `ReplySize` minimum | `sizeof(ICMP_ECHO_REPLY) + RequestSize + 8` (8 = size of an ICMP error message) | [verified: SDK um\icmpapi.h:303; same Learn page] |
| Return value | number of replies stored; 0 → `GetLastError()` | [verified: same Learn page] |
| `IP_OPTION_INFORMATION` | `UCHAR Ttl, Tos, Flags, OptionsSize; PUCHAR OptionsData` | [verified: SDK um\IPExport.h:53-60] |
| `ICMP_ECHO_REPLY` | `IPAddr Address; ULONG Status; ULONG RoundTripTime /*ms*/; USHORT DataSize; USHORT Reserved; PVOID Data; IP_OPTION_INFORMATION Options` | [verified: SDK um\IPExport.h:79-88] |
| `Icmp6CreateFile`, `Icmp6SendEcho2(…, sockaddr_in6 *SourceAddress, sockaddr_in6 *DestinationAddress, …)` | IPv6 variant | [verified: SDK um\icmpapi.h:110, 349-366] |
| `ICMPV6_ECHO_REPLY` | `IPV6_ADDRESS_EX Address` (packed, 26 bytes); `ULONG Status`; `unsigned int RoundTripTime`; reply data follows | [verified: SDK um\IPExport.h:104-122] |
| `IPAddr` | the 4 address bytes in network order, read as a little-endian ULONG | [verified: replies decoded to the right addresses in run net_probe.py] |

Sizes and offsets on x64 Python 3.13:

| Struct | `ctypes.sizeof` | Offsets | How the layout was proven |
|---|---|---|---|
| `ICMP_ECHO_REPLY` | 40 | `Data` @16, `Options` @24 | `Data` pointed at buffer+40 and the bytes there equalled the 32-byte payload, on every reply [verified: run net_probe.py] |
| `IP_OPTION_INFORMATION` | 16 | — | reply TTL read as 64 (gateway) and 59 (1.1.1.1) [verified: run net_probe.py] |
| `ICMPV6_ECHO_REPLY` | 36 | `Status` @28, `RoundTripTime` @32 | reply from `::1` decoded as `::1`, echo data directly after byte 36 [verified: run icmp6_probe.py] |

**The `ICMP_ECHO_REPLY32` remark.** Learn says "On a 64-bit platform, the ICMP_ECHO_REPLY32 structure should be
used" [verified: learn.microsoft.com/en-us/windows/win32/api/ipexport/ns-ipexport-icmp_echo_reply @92672624]. For a
64-bit process this is wrong: the buffer holds the native 40-byte layout with an 8-byte pointer
[verified: run net_probe.py, `data_off=40, echo_ok=True`]. Use the 32-bit variant only for a 32-bit process [inferred].

IP_STATUS codes you will see (from `IP_STATUS_BASE` = 11000) [verified: SDK um\IPExport.h:169-237]:

| Code | Name | Meaning in practice |
|---|---|---|
| 0 | `IP_SUCCESS` | echo reply |
| 11001 | `IP_BUF_TOO_SMALL` | reply buffer too small |
| 11002 | `IP_DEST_NET_UNREACHABLE` (= `IP_DEST_NO_ROUTE`) | a router said "no route"; seen from `<ISP hop 2>` for 192.0.2.1 [verified: run net_probe.py] |
| 11003 | `IP_DEST_HOST_UNREACHABLE` (= `IP_DEST_ADDR_UNREACHABLE`) | host unreachable |
| 11010 | `IP_REQ_TIMED_OUT` | no reply before the timeout (also returned via `GetLastError`) |
| 11013 | `IP_TTL_EXPIRED_TRANSIT` (= `IP_HOP_LIMIT_EXCEEDED`) | TTL-limited probe hit a router; `Address` = that router |
| 11050 | `IP_GENERAL_FAILURE` | malformed reply and other failures. **(fact-check)** A reply buffer of exactly `sizeof(ICMP_ECHO_REPLY)` (40 bytes: large enough to pass the size check, too small for the data) gives `n=0`, `GetLastError` = 11050, not 11001 or 87 [verified: run v_net.py] |
| 11040-11045 | `IP_DEST_UNREACHABLE` … `IP_DEST_SCOPE_MISMATCH` | "IPv6-only status codes" (`Icmp6SendEcho2`) [verified: SDK um\IPExport.h:204-212] |

`GetIpErrorString(code, buf, &size)` turns a code into text; `size` is in characters and the buffer needs `size + 1`
[verified: SDK um\iphlpapi.h:1136-1140]. It returned "Request timed out.", "Hop limit exceeded.", "Destination
address unreachable.", "General failure.", "Buffer too small." [verified: run net_probe.py]. Branch on the numeric
code, never on the text: the strings are localized [inferred].

### 1.2 Tested recipe

```python
import ctypes, ctypes.wintypes as wt, socket, time

iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)

class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

class ICMP_ECHO_REPLY(ctypes.Structure):          # x64 layout: 40 bytes
    _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong),
                ("RoundTripTime", ctypes.c_ulong), ("DataSize", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                ("Options", IP_OPTION_INFORMATION)]

iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
iphlpapi.IcmpSendEcho2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ushort,
                                   ctypes.POINTER(IP_OPTION_INFORMATION), ctypes.c_void_p,
                                   ctypes.c_ulong, ctypes.c_ulong]
iphlpapi.IcmpSendEcho2.restype = ctypes.c_ulong

PAYLOAD = b"lagdoctor-icmp-probe-32-bytes!!!"         # 32 bytes, ping.exe's default size

def ping(h, ip, timeout_ms=1000, ttl=None):
    req = ctypes.create_string_buffer(PAYLOAD, len(PAYLOAD))
    size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(PAYLOAD) + 8 + 8
    reply = ctypes.create_string_buffer(size)
    opts = None
    if ttl is not None:                              # TTL-limited probe (hop discovery)
        o = IP_OPTION_INFORMATION(Ttl=ttl)
        opts = ctypes.byref(o)
    t0 = time.perf_counter()
    n = iphlpapi.IcmpSendEcho2(h, None, None, None,
                               int.from_bytes(socket.inet_aton(ip), "little"),   # IPAddr
                               req, len(PAYLOAD), opts, reply, size, timeout_ms)
    wall_ms = (time.perf_counter() - t0) * 1000
    err = ctypes.get_last_error() if n == 0 else 0
    r = ICMP_ECHO_REPLY.from_buffer(reply)
    return dict(n=n, err=err, status=r.Status, rtt=r.RoundTripTime, wall=round(wall_ms, 2),
                addr=socket.inet_ntoa(r.Address.to_bytes(4, "little")), ttl=r.Options.Ttl)

h = iphlpapi.IcmpCreateFile()
try:
    print(ping(h, "1.1.1.1"))
finally:
    iphlpapi.IcmpCloseHandle(h)
```

This block (and the route block in 1.5) was re-run straight from this document: one echo to 1.1.1.1 returned
`status 0, rtt 6`, and the TTL path (TTL=1 to the gateway, one hop away) returned an echo reply
[verified: run doc_blocks_check.py on this file]. `ping.exe`'s default payload is
32 bytes [verified: `ping -n 1 127.0.0.1` → "Pinging 127.0.0.1 with 32 bytes of data"]. ctypes releases the GIL around calls into `WinDLL` functions, so a blocking `IcmpSendEcho2` on a worker thread
does not stall Python [verified: docs.python.org/3.13/library/ctypes.html, WinDLL entry].

### 1.3 Real output (2026-11-04, Wi-Fi, unelevated) [verified: run net_probe.py, net_probe2.py, net_probe4.py]

```
python 3.13.3 pointer bits 64
sizeof SOCKADDR_INET 28 IP_ADDRESS_PREFIX 32 MIB_IPFORWARD_ROW2 104 MIB_IF_ROW2 1352 ICMP_ECHO_REPLY 40 IP_OPTION_INFORMATION 16
GetIpForwardTable2 rc 0 rows 58
  default-ish route 0.0.0.0/0 via 192.168.0.1 if=8 metric=0
GetBestRoute2(1.1.1.1) rc=0
  ifIndex=8 nexthop=192.168.0.1 prefix=0.0.0.0/0 metric=0 src=192.168.0.225
  GetIfEntry2 rc=0 alias='Wi-Fi' desc='Intel(R) Wi-Fi 6 AX201 160MHz' type=71 phys=9 oper=1 tx=433300000 rx=173300000 inOctets=117134921012 outOctets=7024341865 inDisc=0 inErr=0 outDisc=0 outQLen=0
GetBestRoute2(2606:4700:4700::1111) rc=1231
  ping 192.168.0.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 1, 'wall': 2.4, 'addr': '192.168.0.1', 'ttl': 64, 'datasize': 32, 'data_off': 40, 'echo_ok': True}
  ping 192.168.0.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 4, 'wall': 4.69, ...}
  ping 192.168.0.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 1, 'wall': 2.19, ...}
  ping 192.168.0.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 2, 'wall': 2.54, ...}
  ping 1.1.1.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 7, 'wall': 7.87, 'addr': '1.1.1.1', 'ttl': 59, 'datasize': 32, 'data_off': 40, 'echo_ok': True}
  ping 1.1.1.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 6, 'wall': 7.32, ...}
  ping 1.1.1.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 5, 'wall': 6.28, ...}
  ping 1.1.1.1: {'n': 1, 'err': 0, 'status': 0, 'rtt': 6, 'wall': 7.43, ...}
  ttl=1 -> {'n': 1, 'err': 0, 'status': 11013, 'rtt': 1, 'wall': 2.13, 'addr': '192.168.0.1', 'ttl': 64, 'datasize': 0}
  ttl=2 -> {'n': 1, 'err': 0, 'status': 11013, 'rtt': 7, 'wall': 7.47, 'addr': '<ISP hop 2>', 'ttl': 254, 'datasize': 0}
  ttl=3 -> {'n': 1, 'err': 0, 'status': 11013, 'rtt': 6, 'wall': 6.99, 'addr': '<ISP hop 3>', 'ttl': 253, 'datasize': 0}
  ttl=4 -> {'n': 1, 'err': 0, 'status': 11013, 'rtt': 8, 'wall': 8.48, 'addr': '<ISP hop 4>', 'ttl': 251, 'datasize': 0}
  ping 192.0.2.1 (timeout 500 ms): {'n': 1, 'err': 0, 'status': 11002, 'rtt': 7, 'wall': 7.71, 'addr': '<ISP hop 2>', 'ttl': 254}
  tiny reply buffer: n=0 GetLastError=87
timeout=1ms -> {'n': 1, 'err': 0, 'status': 0, 'rtt': 7, 'wall': 7.61, 'addr': '1.1.1.1', ...}
timeout=3ms -> {'n': 1, 'err': 0, 'status': 0, 'rtt': 15, 'wall': 15.95, 'addr': '1.1.1.1', ...}
10.99.0.77 timeout=1000ms -> n=0 err=11010 status=11010 wall=525.03ms
10.99.0.77 timeout=500ms -> n=0 err=11010 status=11010 wall=502.91ms
10.99.0.77 timeout=100ms -> n=0 err=11010 status=11010 wall=496.94ms
10.99.0.77 timeout=20ms -> n=0 err=11010 status=11010 wall=495.17ms
```

What this shows:

- `rtt` has 1 ms resolution; `wall` (QPC around the call) exceeds it by 0.5-1.4 ms, part of which is `rtt`'s
  truncation to whole milliseconds. For gateway jitter (1-4 ms RTTs), use `wall` and treat sub-millisecond detail as
  noise [verified: output above; inferred: interpretation].
- `Timeout` is not a deadline. With `Timeout`=1 ms and 3 ms the call still waited for, and returned, a 7.6 ms
  and a 16 ms reply as success (fact-check re-run: `Timeout`=1 ms → a 5.6 ms reply, status 0)
  [verified: output above; run v_net.py].
- **(fact-check) With no reply at all, the wait is not `Timeout` either, and it is not stable.** In the original run
  (`10.99.0.77`, an empty subnet on a virtual adapter) every Timeout from 20 to 1000 ms returned
  `IP_REQ_TIMED_OUT` after about 500 ms. In the fact-check re-run the same address no longer went out of that virtual
  adapter: `GetBestRoute2` and `Find-NetRoute` both returned the **Wi-Fi default route** for it (its neighbour entry
  on that adapter was `Incomplete`, while unprobed `10.99.0.78` still routed on-link), so those probes went
  to the home router and were dropped. Measured waits: Timeout 20 → 210.6 ms, 100 → 369.7 ms, 500 → 486.5 ms,
  1000 → 1003.9 ms [verified: run v_net.py, v_timeout.py, v_route.py; `Find-NetRoute -RemoteIPAddress 10.99.0.77`;
  `Get-NetNeighbor`]. The original ARP explanation is therefore unproven [unverified], and the route-switch shows
  that the route for one destination can change between lookups. **Design rule:** apply your own deadline from the
  wall clock, run each target on its own thread, and re-run `GetBestRoute2` rather than caching the route [inferred].
- A small reply buffer gives `n=0, GetLastError=87` (`ERROR_INVALID_PARAMETER`), as Learn documents for `ReplySize <
  sizeof(ICMP_ECHO_REPLY)` [verified: output above; learn.microsoft.com/…/nf-icmpapi-icmpsendecho2 @92672624].
- TTL-limited probes find the first hops without admin. Hop 1 is the home router, hop 2 the ISP's first router
  [verified: output above]. Routers rate-limit the ICMP errors they generate ("A router SHOULD also be able to limit
  the rate at which it sends other sorts of ICMP error messages"), so TTL-probe latency per hop is noisy
  [verified: www.rfc-editor.org/rfc/rfc1812.txt §4.3.2.8].

### 1.4 IPv6

`Icmp6SendEcho2` with `SourceAddress` = an all-zero `sockaddr_in6` (family 23) and destination `::1` returned
`n=1, status=0, from=::1`. To `2606:4700:4700::1111` it returned `n=0, err=1231` (`ERROR_NETWORK_UNREACHABLE`)
in 0.1 ms, because this machine has no IPv6 default route (`GetBestRoute2` also returns 1231)
[verified: run icmp6_probe.py; run net_probe.py; SDK shared\winerror.h:7548]. Reply buffer: `sizeof(ICMPV6_ECHO_REPLY)
+ RequestSize + 8 + room for an IO_STATUS_BLOCK` [verified: learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmp6sendecho2 @c2c2af31].

```python
class IPV6_ADDRESS_EX(ctypes.Structure):          # ipexport.h wraps this one in packon.h
    _pack_ = 1
    _fields_ = [("sin6_port", ctypes.c_ushort), ("sin6_flowinfo", ctypes.c_ulong),
                ("sin6_addr", ctypes.c_ushort * 8), ("sin6_scope_id", ctypes.c_ulong)]

class ICMPV6_ECHO_REPLY(ctypes.Structure):        # 36 bytes; reply data follows at byte 36
    _fields_ = [("Address", IPV6_ADDRESS_EX), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_uint)]
# replying address = ipaddress.IPv6Address(bytes(reply.Address)[6:22])
```

### 1.5 Default gateway and active interface without admin

| API | What it gives | Tested here |
|---|---|---|
| `GetBestRoute2(NULL, 0, NULL, &dest, 0, &row, &bestSrc)` | the route the stack will use **to that destination**: `InterfaceIndex`, `NextHop` (gateway), source address. Handles VPN split tunnels | yes, rc 0 unelevated [verified: run net_probe.py; SDK shared\netioapi.h:1498-1506] |
| `GetIpForwardTable2(AF_UNSPEC, &table)` + `FreeMibTable` | all routes; filter `DestinationPrefix` /0 (and /1, which VPNs add as split defaults) | yes, 58 rows [verified: run net_probe.py; SDK shared\netioapi.h:1597] |
| `GetIfEntry2(&row)` | alias, description, `Type` (71 = `IF_TYPE_IEEE80211`, 6 = Ethernet), `PhysicalMediumType` (9 = Native 802.11, 14 = 802.3), link speeds, byte/discard/error counters | yes [verified: run net_probe.py; SDK shared\ipifcons.h:49,114; shared\ntddndis.h:2325-2349] |
| `GetAdaptersAddresses(…, GAA_FLAG_INCLUDE_GATEWAYS = 0x0080, …)` | per-adapter unicast addresses, DNS servers, gateways | not called [verified flag value: SDK um\IPTypes.h:409] |
| `psutil.net_if_addrs()` | local unicast addresses per interface (needed to tell local from remote in ETW events) | yes: Wi-Fi = 192.168.0.225 only [verified: psutil 7.2.2 one-liner] |

Struct sizes (x64): `SOCKADDR_INET` 28, `IP_ADDRESS_PREFIX` 32, `MIB_IPFORWARD_ROW2` 104, `MIB_IF_ROW2` 1352
(`Alias` @28, `Type` @1128, `OperStatus` @1156, `TransmitLinkSpeed` @1192, `InOctets` @1208, `OutQLen` @1344). The
104-byte stride walked all 58 route rows to valid families and interface indexes; `GetIfEntry2` decoded alias "Wi-Fi"
and link speeds that match the Wi-Fi rates in section 3 [verified: run net_probe.py].

```python
class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_ubyte * 4), ("sin_zero", ctypes.c_char * 8)]
class SOCKADDR_IN6(ctypes.Structure):
    _fields_ = [("sin6_family", ctypes.c_ushort), ("sin6_port", ctypes.c_ushort),
                ("sin6_flowinfo", ctypes.c_ulong), ("sin6_addr", ctypes.c_ubyte * 16),
                ("sin6_scope_id", ctypes.c_ulong)]
class SOCKADDR_INET(ctypes.Union):
    _fields_ = [("Ipv4", SOCKADDR_IN), ("Ipv6", SOCKADDR_IN6), ("si_family", ctypes.c_ushort)]
class IP_ADDRESS_PREFIX(ctypes.Structure):
    _fields_ = [("Prefix", SOCKADDR_INET), ("PrefixLength", ctypes.c_ubyte)]
class MIB_IPFORWARD_ROW2(ctypes.Structure):       # 104 bytes
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong),
                ("DestinationPrefix", IP_ADDRESS_PREFIX), ("NextHop", SOCKADDR_INET),
                ("SitePrefixLength", ctypes.c_ubyte), ("ValidLifetime", ctypes.c_ulong),
                ("PreferredLifetime", ctypes.c_ulong), ("Metric", ctypes.c_ulong), ("Protocol", ctypes.c_int),
                ("Loopback", ctypes.c_ubyte), ("AutoconfigureAddress", ctypes.c_ubyte),
                ("Publish", ctypes.c_ubyte), ("Immortal", ctypes.c_ubyte), ("Age", ctypes.c_ulong),
                ("Origin", ctypes.c_int)]
iphlpapi.GetBestRoute2.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                   ctypes.POINTER(SOCKADDR_INET), ctypes.c_ulong,
                                   ctypes.POINTER(MIB_IPFORWARD_ROW2), ctypes.POINTER(SOCKADDR_INET)]
iphlpapi.GetBestRoute2.restype = ctypes.c_ulong

def best_route(dest_ip):                          # IPv4 shown; IPv6 sets Ipv6.sin6_family = 23
    dest = SOCKADDR_INET(); dest.Ipv4.sin_family = 2
    dest.Ipv4.sin_addr[:] = list(socket.inet_aton(dest_ip))
    row, src = MIB_IPFORWARD_ROW2(), SOCKADDR_INET()
    rc = iphlpapi.GetBestRoute2(None, 0, None, ctypes.byref(dest), 0, ctypes.byref(row), ctypes.byref(src))
    return rc, row.InterfaceIndex, bytes(row.NextHop.Ipv4.sin_addr), bytes(src.Ipv4.sin_addr)

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
class MIB_IF_ROW2(ctypes.Structure):              # 1352 bytes (SDK shared\netioapi.h:154-219)
    _fields_ = [("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong), ("InterfaceGuid", GUID),
                ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
                ("PhysicalAddressLength", ctypes.c_ulong), ("PhysicalAddress", ctypes.c_ubyte * 32),
                ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong),
                ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
                ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int),
                ("InterfaceAndOperStatusFlags", ctypes.c_ubyte),     # 8 BOOLEAN bit-fields in one byte
                ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
                ("NetworkGuid", GUID), ("ConnectionType", ctypes.c_int),
                ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64)] + \
               [(n, ctypes.c_uint64) for n in (
                   "InOctets", "InUcastPkts", "InNUcastPkts", "InDiscards", "InErrors", "InUnknownProtos",
                   "InUcastOctets", "InMulticastOctets", "InBroadcastOctets", "OutOctets", "OutUcastPkts",
                   "OutNUcastPkts", "OutDiscards", "OutErrors", "OutUcastOctets", "OutMulticastOctets",
                   "OutBroadcastOctets", "OutQLen")]
iphlpapi.GetIfEntry2.argtypes = [ctypes.POINTER(MIB_IF_ROW2)]
iphlpapi.GetIfEntry2.restype = ctypes.c_ulong
iphlpapi.GetIpForwardTable2.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.c_void_p)]
iphlpapi.GetIpForwardTable2.restype = ctypes.c_ulong
iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]
iphlpapi.FreeMibTable.restype = None

def if_row(if_index):                             # alias, Type, link speeds (bit/s), octet counters
    r = MIB_IF_ROW2(); r.InterfaceIndex = if_index
    return iphlpapi.GetIfEntry2(ctypes.byref(r)), r

def default_routes():                             # /0 and /1 (VPN split-default) rows, IPv4 and IPv6
    p = ctypes.c_void_p()
    rc = iphlpapi.GetIpForwardTable2(0, ctypes.byref(p))          # 0 = AF_UNSPEC
    if rc:
        return rc, []
    try:
        n = ctypes.c_ulong.from_address(p.value).value          # ULONG NumEntries, then the rows,
        rows = (MIB_IPFORWARD_ROW2 * n).from_address(p.value + 8)  # 8-byte aligned, so they start at +8
        return 0, [(r.DestinationPrefix.Prefix.si_family, r.DestinationPrefix.PrefixLength, r.InterfaceIndex,
                    r.Metric) for r in rows if r.DestinationPrefix.PrefixLength in (0, 1)]
    finally:
        iphlpapi.FreeMibTable(p)
```

**(fact-check)** The `MIB_IF_ROW2`, `if_row` and `default_routes` additions were run from this document: sizeof 1352,
`Alias` @28, `Type` @1128, `OperStatus` @1156, `TransmitLinkSpeed` @1192, `InOctets` @1208, `OutQLen` @1344;
`if_row(8)` → alias 'Wi-Fi', Type 71, PhysicalMediumType 9, link speeds 433,300,000 / 173,300,000 bit/s; the table had
58 rows (35 IPv4, 23 IPv6) and exactly one /0 route (IPv4, ifIndex 8). Rows start at +8, not +4, because
`MIB_IPFORWARD_ROW2` begins with a 64-bit `NET_LUID` [verified: run v_net.py, v_blocks2.py]. `GetAdaptersAddresses`
is not needed: `GetBestRoute2` already returns the next hop.

---

## 2. Measuring quality properly

### 2.1 Definitions to implement

| Metric | Definition | Use it for | Source |
|---|---|---|---|
| RTT percentiles | p50/p95/p99 of `wall` per window | headline "ping" | [inferred] |
| Loss | probes with `Status != 0` or no reply ÷ probes sent, per 15 s window | loss | [inferred] |
| Burst loss | lost ÷ sent inside any 200 ms window | short outages | Teams uses "any 200-ms interval" [verified: learn.microsoft.com/en-us/training/modules/plan-optimize-network-performance-for-teams-phone-system/2-understand-teams-network-requirements @3314c772] |
| RFC 3550 interarrival jitter | `D(i,j) = (Rj - Ri) - (Sj - Si)`; `J(i) = J(i-1) + (\|D(i-1,i)\| - J(i-1))/16`. The 1/16 gain "gives a good noise reduction ratio while maintaining a reasonable rate of convergence" | the game's server→client stream (S = nominal send times, see 4.6) | [verified: www.rfc-editor.org/rfc/rfc3550.txt §6.4.1] |
| IPDV | `IPDV(i) = D(i) - D(i-1)`, two-sided | packet-to-packet variation | [verified: www.rfc-editor.org/rfc/rfc5481.txt] |
| PDV | `PDV(i) = D(i) - D(min)`, one-sided; the 99.9th percentile of PDV relates to the de-jitter buffer needed | "how late do packets get": best single number | [verified: www.rfc-editor.org/rfc/rfc5481.txt] |
| Mean consecutive difference | mean of \|RTT(i) - RTT(i-1)\| | what most consumer "jitter" readouts mean; cheap for ICMP | [inferred] |
| Standard deviation | of RTT | avoid as the headline: one spike inflates it and it mixes path changes with queueing | [inferred] |

Recommendation: report p50 RTT, PDV p99 (RTT p99 - window min), mean consecutive difference as "jitter", loss and
burst loss over 15 s windows. Use RFC 3550 J only on the game flow, where packets are sent at a fixed tick [inferred].

### 2.2 What levels are noticeable

**(fact-check)** One primary source is reachable: the Claypools' 2006 WPI technical report (the Claypool row below).
It gives latency tolerance per game class, not jitter or loss thresholds. The other anchors are non-game or vendor
targets:

| Source | Numbers |
|---|---|
| Microsoft Teams, full path client → Microsoft edge (includes Wi-Fi) | one-way < 50 ms, RTT < 100 ms, burst loss < 10 % in any 200 ms, loss < 1 % in any 15 s, inter-arrival jitter < 30 ms in any 15 s, reorder < 0.05 % [verified: Teams training page @3314c772] |
| Microsoft Teams, network edge → Microsoft edge (ISP leg only) | one-way < 30 ms, RTT < 60 ms, burst loss < 1 % in 200 ms, loss < 0.1 % in 15 s, jitter < 15 ms in 15 s, reorder < 0.01 % [verified: same page] |
| Riot, VALORANT netcode (dated 2020-07-28 originally; the fetched page shows no article date, so the date is [unverified]) | servers at "128 server tickrate"; goal "35ms ping to 70% of our player base" [verified: www.riotgames.com/en/news/peeking-valorants-netcode, re-fetched] |
| NVIDIA GeForce NOW | "less than 80ms of network latency from an NVIDIA data center" [verified: www.nvidia.com/en-us/geforce-now/system-reqs/] |
| RFC 8289 CoDel | "TARGET SHOULD be set to 5 ms for normal Internet traffic", TARGET being "the maximum acceptable persistent queue delay"; "INTERVAL SHOULD have a value of 100 ms" [verified: www.rfc-editor.org/rfc/rfc8289.txt §4.2-4.3] |
| Claypool & Claypool, "On Latency and Player Actions in Online Games", dated July 8, 2006, WPI CS technical report 06-13 (whether this is the same text as their 2006 CACM article: [unverified]) | Table 1: first-person avatar games (FPS, racing) sensitivity high, threshold **100 ms**; third-person (sports, RPG) medium, **500 ms**; omnipresent (RTS, sim) low, **1000 ms**. FPS hit fraction shows "a sharp drop (about 35%) in accuracy at 100 milliseconds"; racing lap time rises steeply "above 50 milliseconds and again above 150 milliseconds" [verified: ftp.cs.wpi.edu/pub/techreports/pdf/06-13.pdf, text extracted with pdf_text.py] |

Thresholds I recommend from these (all [inferred]):

| Metric (15 s window) | Good | Noticeable | Bad |
|---|---|---|---|
| RTT to game server / internet anchor (p50) | < 60 ms | 60-100 ms | > 100 ms |
| Jitter (mean consecutive difference or RFC 3550 J) | < 15 ms | 15-30 ms | > 30 ms |
| Loss | 0 | < 1 % | ≥ 1 %, or ≥ 10 % in any 200 ms |
| RTT rise under load (loaded p50 - idle min) | < 30 ms (a well-managed queue stays near 5 ms) | 30-100 ms | ≥ 100 ms |
| Gateway RTT on Wi-Fi (p95) | < 10 ms | 10-30 ms | > 30 ms or any loss (measured here: 1-4 ms) |

### 2.3 Latency under load (bufferbloat), spotted passively

Bufferbloat is "the undesirable latency that comes from a router or other network equipment buffering too much data"
[verified: www.bufferbloat.net/projects/bloat/wiki/Introduction/]. Passive recipe [inferred unless tagged]:

1. Sample `GetIfEntry2` `InOctets`/`OutOctets` on the egress interface at 1 Hz → throughput in each direction
   [verified that the counters are readable: run net_probe.py].
2. Learn the link's real capacity. Wi-Fi PHY rate (433/173 Mbit/s here) is not the ISP's rate. Use the session's
   peak throughput, and Steam's own measurement: `config\config.vdf` `"RecentDownloadRate" "12340032"` (about
   98.7 Mbit/s if bytes/s; this matches the log line "Current download rate: 98.213 Mbps") [verified: local file
   grep; inferred: the unit]. **(fact-check)** It is the rate of the most recent download, not the link's capacity:
   the same `content_log.txt` holds 643 rate lines peaking at 266.312 Mbps, so use it only as a lower bound
   [verified: `Select-String` over `logs\content_log.txt`].
3. Keep an idle baseline = minimum anchor RTT over the last 120 s while throughput < 20 % of capacity.
4. Flag bufferbloat when throughput ≥ 70-80 % of capacity in either direction AND anchor RTT p50 - baseline ≥ 30 ms
   AND gateway RTT stays near its baseline. If gateway RTT rises too, the queue is in the Wi-Fi/AP hop instead.
5. Name the hog with ETW per-PID bytes (section 4) or the log/cmdlet sources (section 6).

### 2.4 Why many game servers do not answer ICMP

- Microsoft's PlayFab game backend measures latency with UDP beacons on port 3075, not ICMP, because "Internet
  service providers and other elements of the Internet ecosystem may deliver differentiated performance for UDP vs.
  TCP vs. ICMP flows" [verified: learn.microsoft.com/en-us/xbox/playfab/multiplayer/servers/using-qos-beacons-to-measure-player-latency-to-azure @4ffe64e5; the `/gaming/playfab/` path the original cited redirects there].
- Valve's Steam Datagram Relay carries game traffic through relays so "IP addresses are never revealed"; the UDP peer
  you see for many Steam games is a Valve relay, not the game server [verified: partner.steamgames.com/doc/features/multiplayer/steamdatagramrelay].
- Routers may rate-limit ICMP errors [verified: RFC 1812 §4.3.2.8].
- Hosts that block inbound ICMP by firewall default: [unverified] (no vendor doc fetched). Note microsoft.com,
  github.com and store.steampowered.com all answered in the original run [verified by the original researcher only:
  run net_probe2.py, net_probe3.py; not re-run in the fact-check, to stay within the ping budget].
- Consequence: ICMP to the anchor and gateway localizes problems; the game's own UDP stream (ETW) is the ground truth
  for the game path [inferred].

---

## 3. Wi-Fi diagnostics without admin

### 3.1 The location gate on this machine

- Windows now requires precise-location consent for Wi-Fi APIs that reveal BSSIDs. Without it, `WlanGetAvailableNetworkList`,
  `WlanGetNetworkBssList`, `WlanScan` and `WlanQueryInterface(wlan_intf_opcode_current_connection)` return
  `ERROR_ACCESS_DENIED`. A one-time consent prompt appears "if the process is running within the user's context and
  outside of the C:\Windows\System32 folder". `WlanRegisterNotification` with `WLAN_NOTIFICATION_SOURCE_MSM` needs
  the `wiFiControl` capability [verified: learn.microsoft.com/en-us/windows/win32/nativewifi/wi-fi-access-location-changes @4b1605e3].
- Here: `HKLM\…\CapabilityAccessManager\ConsentStore\location` Value = Deny, HKCU Value = Deny, NonPackaged Value =
  Allow [verified: `Get-ItemProperty` on those keys]. `netsh wlan show interfaces` (in System32, so no prompt) prints
  "Network shell commands need location permission to access WLAN information" and "Function WlanQueryInterface
  returns error 5" [verified: `netsh wlan show interfaces`]. **(fact-check)** netsh then explains error 5 as "The
  requested operation requires elevation (Run as administrator)", which is misleading here: the cause is location
  consent, so never tell the user to elevate for this [verified: same command, re-run].
- Learn also says calls to the gated APIs "will appear in the in-use system tray and location recent activity" and
  that apps "should throttle requests to reasonable levels, so that the location-in-use icon doesn't appear in the
  system tray frequently". Once consent exists, poll `current_connection` slowly (for example every 10-30 s), not at
  1 Hz [verified: wi-fi-access-location-changes @4b1605e3; inferred: the cadence].
- **I deliberately did not call the location-gated APIs from Python**, to avoid spending the one-time prompt on
  python.exe during research [inferred: per the doc above, a call from python.exe would have shown it].

### 3.2 What works without location (tested)

`WlanOpenHandle(2, …)` negotiated version 2; `WlanEnumInterfaces` found 1 interface, state 1 (connected)
[verified: run wlan_probe.py]. Opcode values are from SDK um\wlanapi.h:973-1001 except 19, which is newer than
SDK 19041 and comes from the current enum order on Learn
[verified: learn.microsoft.com/en-us/windows/win32/api/wlanapi/ne-wlanapi-wlan_intf_opcode @a315d8e1].
Opcode 19 worked here and returned a struct of the documented size, which confirms the value.

| Opcode (value) | Type | Here | Source |
|---|---|---|---|
| `wlan_intf_opcode_autoconf_enabled` (1) | BOOL | 1 | [verified: run wlan_probe.py] |
| `wlan_intf_opcode_background_scan_enabled` (2) | BOOL | 1 | same |
| `wlan_intf_opcode_media_streaming_mode` (3) | BOOL | 0 | same |
| `wlan_intf_opcode_radio_state` (4) | `WLAN_RADIO_STATE`, 772 bytes | 6 PHYs, software and hardware radio on | same |
| `wlan_intf_opcode_bss_type` (5) | `DOT11_BSS_TYPE` | 3 | same |
| `wlan_intf_opcode_interface_state` (6) | `WLAN_INTERFACE_STATE` | 1 (connected) | same; enum at SDK um\wlanapi.h:551-561 |
| `wlan_intf_opcode_channel_number` (8) | ULONG | 36 | same |
| `wlan_intf_opcode_current_operation_mode` (12) | ULONG | 4 | same |
| `wlan_intf_opcode_realtime_connection_quality` (19) | `WLAN_REALTIME_CONNECTION_QUALITY`, 296 bytes | see 3.3 | same |
| `wlan_intf_opcode_qos_info` (20) | `WLAN_QOS_INFO` | **(fact-check)** rc 0, 52 bytes, unelevated, no location (layout not decoded) | [verified: run v_wlan.py] |
| `wlan_intf_opcode_statistics` (0x10000101) | `WLAN_STATISTICS`, 1088 bytes (6 PHYs) | see 3.4 | same |
| `wlan_intf_opcode_rssi` (0x10000102) | LONG dBm | -53 | same |
| `wlan_intf_opcode_current_connection` (7) | `WLAN_CONNECTION_ATTRIBUTES` (SSID, BSSID, PHY, quality, rates) | not called: location-gated | [verified: location doc above] |

Free every returned buffer with `WlanFreeMemory` [verified: learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlanqueryinterface @d7843384].

### 3.3 Realtime connection quality (the best no-location source)

Learn says this opcode "omits location-sensitive information, and for that reason it doesn't require location access
privileges", and that it "will fail if the interface isn't connected", so treat a non-zero return while
`interface_state` (6) is not 1 as "disconnected", not as an error [verified: WLAN_INTF_OPCODE page @a315d8e1;
inferred: the handling].

Opening the client handle and finding the interface (the part the snippet below assumes). This block and the next
were executed as written from this document: open rc 0, version 2, 1 interface, then opcode 19 rc 0, 296 bytes,
PHY 8, quality 86, Rx 173300, Tx 433300, 5180, -53 dBm [verified: run v_blocks3.py]:

```python
import ctypes, ctypes.wintypes as wt
wlanapi = ctypes.WinDLL("wlanapi", use_last_error=True)
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
class WLAN_INTERFACE_INFO(ctypes.Structure):      # 532 bytes
    _fields_ = [("InterfaceGuid", GUID), ("strInterfaceDescription", ctypes.c_wchar * 256), ("isState", ctypes.c_int)]
class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wt.DWORD), ("dwIndex", wt.DWORD), ("InterfaceInfo", WLAN_INTERFACE_INFO * 1)]
wlanapi.WlanOpenHandle.argtypes = [wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.HANDLE)]
wlanapi.WlanEnumInterfaces.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST))]
wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]
wlanapi.WlanCloseHandle.argtypes = [wt.HANDLE, ctypes.c_void_p]
ver, h = wt.DWORD(), wt.HANDLE()
rc = wlanapi.WlanOpenHandle(2, None, ctypes.byref(ver), ctypes.byref(h))          # rc 0, ver 2
pl = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
rc = wlanapi.WlanEnumInterfaces(h, None, ctypes.byref(pl))
n = pl.contents.dwNumberOfItems
infos = (WLAN_INTERFACE_INFO * n).from_address(ctypes.addressof(pl.contents.InterfaceInfo))
iface_guid = GUID(); ctypes.memmove(ctypes.byref(iface_guid), ctypes.byref(infos[0].InterfaceGuid), 16)
wlanapi.WlanFreeMemory(pl)                         # copy what you need first
# ... queries ..., then wlanapi.WlanCloseHandle(h, None)
```

```python
class WLAN_RATE_SET(ctypes.Structure):           # DOT11_RATE_SET_MAX_LENGTH = 126 (SDK shared\windot11.h:95)
    _fields_ = [("uRateSetLength", ctypes.c_ulong), ("usRateSet", ctypes.c_ushort * 126)]
class WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO(ctypes.Structure):          # 272 bytes
    _fields_ = [("ucLinkID", ctypes.c_ubyte), ("ulChannelCenterFrequencyMhz", ctypes.c_ulong),
                ("ulBandwidth", ctypes.c_ulong), ("lRssi", ctypes.c_long), ("wlanRateSet", WLAN_RATE_SET)]
class WLAN_REALTIME_CONNECTION_QUALITY(ctypes.Structure):                    # 296 bytes with one link
    _fields_ = [("dot11PhyType", ctypes.c_int), ("ulLinkQuality", ctypes.c_ulong),
                ("ulRxRate", ctypes.c_ulong), ("ulTxRate", ctypes.c_ulong),
                ("bIsMLOConnection", wt.BOOL), ("ulNumLinks", ctypes.c_ulong),
                ("linksInfo", WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO * 1)]   # linksInfo @24

wlanapi = ctypes.WinDLL("wlanapi", use_last_error=True)
wlanapi.WlanQueryInterface.argtypes = [wt.HANDLE, ctypes.POINTER(GUID), ctypes.c_int, ctypes.c_void_p,
                                       ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(ctypes.c_int)]
wlanapi.WlanQueryInterface.restype = wt.DWORD
size, p, vt = wt.DWORD(), ctypes.c_void_p(), ctypes.c_int()
rc = wlanapi.WlanQueryInterface(h, ctypes.byref(iface_guid), 19, None,
                                ctypes.byref(size), ctypes.byref(p), ctypes.byref(vt))
q = WLAN_REALTIME_CONNECTION_QUALITY.from_address(p.value)   # read links: linksInfo + i * 272, i < ulNumLinks
# ... copy what you need, then wlanapi.WlanFreeMemory(p)
```

Struct layouts follow the Learn pages [verified: learn.microsoft.com/en-us/windows/win32/api/wlanapi/ns-wlanapi-wlan_realtime_connection_quality @a315d8e1; …/ns-wlanapi-wlan_realtime_connection_quality_link_info @477e1c1d].
`GUID` and `WlanOpenHandle`/`WlanEnumInterfaces` bindings are in the SDK at um\wlanapi.h:1177-1195 and are ordinary.

Real output [verified: run wlan_probe.py]:

```
sizeof WLAN_INTERFACE_INFO 532 WLAN_REALTIME_CONNECTION_QUALITY 296 LINK_INFO 272 linksInfo offset 24 WLAN_STATISTICS 368 PhyCounters offset 224 WLAN_PHY_FRAME_STATISTICS 144 L2_NOTIFICATION_DATA 40
WlanOpenHandle rc 0 negotiated version 2
WlanEnumInterfaces rc 0 items 1
  interface {…} desc='Intel(R) Wi-Fi 6 AX201 160MHz' state=1
  query realtime_connection_quality  rc=0 size=296 value={'phy': 8, 'quality': 86, 'rx': 173300, 'tx': 433300, 'mlo': 0, 'links': [{'id': 0, 'freq': 5180, 'bw': 0, 'rssi': -53, 'nrates': 8}]}
  query rssi                         rc=0 size=4   value=-53
  query channel_number               rc=0 size=4   value=36
```

Unit checks against independent readouts:

| Field | Doc says | Measured | Verdict |
|---|---|---|---|
| `ulRxRate` / `ulTxRate` | unit not stated | 173300 / 433300 (fact-check 135 s listen: Rx also 144400) | **kbit/s**: `GetIfEntry2` gives 173,300,000 / 433,300,000 bit/s [verified: run net_probe.py, v_net.py]. The SDK states the same unit for the older `WLAN_ASSOCIATION_ATTRIBUTES` rates: "The unit for Rx/Tx rate is Kbits/second" [verified: SDK um\wlanapi.h:582-583] |
| `ulChannelCenterFrequencyMhz` | "in units of kilohertz (kHz)" | 5180 | **MHz**: channel 36 = 5180 MHz, and opcode 8 returned channel 36 [verified: run wlan_probe.py; channel-to-frequency mapping inferred] |
| `ulLinkQuality` | 0 = -100 dBm, 100 = -50 dBm, linear in between | 86 at -53 dBm (linear would be 94); 86 at -54 too | do not convert between them; the driver's quality is coarse [verified: run wlan_probe.py 240] |
| `ulBandwidth` | link bandwidth | 0 | this driver does not report it [verified: same] |
| `dot11PhyType` | `DOT11_PHY_TYPE` | 8 = `dot11_phy_type_vht` (802.11ac); 10 would be `he` (802.11ax) | the Wi-Fi 6 card is associating in 802.11ac mode. The Event Log agrees: `PHYType = 802.11ac` [verified: SDK shared\windot11.h:79-91; event 8001] |

### 3.4 Driver statistics (retries)

`WLAN_STATISTICS` = 3 ULONGLONGs, 2 × `WLAN_MAC_FRAME_STATISTICS` (12 ULONGLONGs each), `dwNumberOfPhys`, then
`WLAN_PHY_FRAME_STATISTICS[n]` (18 ULONGLONGs each, `PhyCounters` @224)
[verified: SDK um\wlanapi.h:1041-1096; run wlan_probe.py]. This driver reports 6 PHY entries with identical counters;
use entry 0. Useful deltas per second: `ullRetryCount`, `ullMultipleRetryCount`, `ullFailedCount`,
`ullACKFailureCount`, `ullFCSErrorCount` against `ullTransmittedFrameCount`. Here at 1 Hz: tx 13-163 frames/s,
retries 0-1/s, failed 0, FCS 0 [verified: run wlan_probe.py 240].

### 3.5 Background scans: the "spike every 60 seconds"

Register for ACM notifications only (`WLAN_NOTIFICATION_SOURCE_ACM` = 0x8; MSM = 0x10 is location-gated)
[verified: SDK um\wlanapi.h:868-869 defines them as `L2_NOTIFICATION_SOURCE_WLAN_ACM`/`_MSM`, = 0x8/0x10 at
um\l2cmn.h:48-49; location doc above]:

```python
class L2_NOTIFICATION_DATA(ctypes.Structure):     # 40 bytes (SDK um\l2cmn.h:91-100)
    _fields_ = [("NotificationSource", wt.DWORD), ("NotificationCode", wt.DWORD),
                ("InterfaceGuid", GUID), ("dwDataSize", wt.DWORD), ("pData", ctypes.c_void_p)]
WLAN_NOTIFICATION_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(L2_NOTIFICATION_DATA), ctypes.c_void_p)
wlanapi.WlanRegisterNotification.argtypes = [wt.HANDLE, wt.DWORD, wt.BOOL, WLAN_NOTIFICATION_CALLBACK,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]

@WLAN_NOTIFICATION_CALLBACK                        # keep a reference for as long as you are registered
def on_notify(pdata, ctx):
    d = pdata.contents
    if d.NotificationSource == 0x8 and d.NotificationCode == 7:   # wlan_notification_acm_scan_complete
        scan_times.append(time.monotonic())

prev = wt.DWORD()
wlanapi.WlanRegisterNotification(h, 0x8, True, on_notify, None, None, ctypes.byref(prev))
# unregister: WlanRegisterNotification(h, 0, True, WLAN_NOTIFICATION_CALLBACK(), None, None, None)
```

ACM codes count up from 0: 7 = `scan_complete`, 8 = `scan_fail`, 10 = `connection_complete`, 21 = `disconnected`
[verified: SDK um\wlanapi.h:880-911; L2_NOTIFICATION_CODE_PUBLIC_BEGIN = 0 at um\l2cmn.h:59]. **(fact-check)** Also
3 = `background_scan_enabled` and 4 = `background_scan_disabled`, which announce any client toggling the scan (the
tool's own fix included) [verified: same header lines].

Callback threading **(fact-check)**: the callback arrives on an OS thread that Python did not create (the
`scan_complete` at t = 52.8 s ran on OS thread 16320, not the main thread 25828, and Python named it `Dummy-1`)
[verified: run v_wlanthread.py]. For such threads "ctypes creates a new dummy Python thread on every invocation",
so `threading.local` values do not persist between callbacks [verified: docs.python.org/3.13/library/ctypes.html,
callback section]. Learn: "Don't call
WlanRegisterNotification from a callback function", unregistering waits for a running callback, and "If both the
callback function and the thread that unregisters from notifications try to acquire the same lock, a deadlock may
occur" [verified: learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlanregisternotification @92672624].
So the callback only appends to a `queue.SimpleQueue`/list and never takes a lock the stopping thread holds
[inferred]. Registrations are undone automatically by `WlanCloseHandle` or process exit [verified: same page].

Real output, 240 s listen, connected and idle, location services off [verified: run wlan_probe.py 240]:

```
WlanRegisterNotification(ACM) rc 0
    NOTIFY t=  19.4s src=0x8 code=7 (scan_complete) datasize=0
    NOTIFY t=  79.4s src=0x8 code=7 (scan_complete) datasize=0
    NOTIFY t= 139.4s src=0x8 code=7 (scan_complete) datasize=0
    NOTIFY t= 199.4s src=0x8 code=7 (scan_complete) datasize=0
unregister rc 0 total notifications 4
```

- The periodic background scan is real and exactly 60.0 s apart here [verified: output above].
- **(fact-check)** Re-run with a 135 s listen: `scan_complete` at t = 28.1 and 88.1 s, again exactly 60.0 s apart.
  1 Hz polling of opcode 19 and the driver statistics showed no change around either scan (RSSI -53, quality 86,
  retries 0/s), so the scan is invisible in those counters; only RTT probing can show its cost
  [verified: run v_wlan.py 135].
- That these scans cause ping spikes is reported in user forums, not by Microsoft or Intel. The thread cited here
  reports router-ping spikes "every 60 seconds" that users blame on background scanning; its workarounds are
  restarting WLAN AutoConfig or a third-party tool's "streaming mode"; disabling location services alone "didn't
  work" [unverified, anecdotal: community.intel.com/t/frequent-ping-spikes/20961, which the original
  `…/t5/Wireless/Frequent-ping-spikes/td-p/426779` link now redirects to]. Neither pass pinged continuously, so
  the effect on RTT is not measured. **The tool should measure it:** for each `scan_complete`, compare gateway RTT
  in [-3 s, +1 s] with the rest [inferred].
- Possible reversible fix, needs consent: `WlanSetInterface(wlan_intf_opcode_background_scan_enabled, FALSE)` while
  connected. Learn: "Background scan can only be disabled when the interface is in the connected state. Background
  scan is disabled if at least one client disables it. If the interface gets disconnected, background scan will be
  enabled automatically." `media_streaming_mode` behaves the same way [verified: WLAN_INTF_OPCODE page @a315d8e1;
  learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlansetinterface @2f7164c7].
- **(fact-check) Permission is granted to standard users.** For `WlanSetInterface` with this opcode "the DACL must
  contain an ACE that grants WLAN_WRITE_ACCESS permission to the access token of the calling thread"; the DACL is
  that of securable object `wlan_secure_bc_scan_enabled` (enum value 3) [verified:
  learn.microsoft.com/en-us/windows/win32/api/wlanapi/ne-wlanapi-wlan_securable_object @92672624]. Read-only
  `WlanGetSecuritySettings` on this machine, unelevated:

  ```
  wlan_secure_bc_scan_enabled (3)              granted=0x70023  sddl=O:SYG:SYD:(A;;CCRC;;;BU)(A;;CCDCWPSDRCWD;;;BU)(A;;CCRC;;;NO)(A;;CCDCWPSDRCWD;;;NO)(A;;CCRC;;;BA)(A;;CCDCWPSDRCWD;;;BA)(D;;FA;;;WD)
  wlan_secure_media_streaming_mode_enabled (11) granted=0x70023  (same shape, plus AC)
  wlan_secure_interface_properties (6)          granted=0x20001  (BU read only)
  ```

  0x70023 is `WLAN_WRITE_ACCESS` (`WLAN_READ_ACCESS | WLAN_EXECUTE_ACCESS | STANDARD_RIGHTS_WRITE | FILE_WRITE_DATA
  | DELETE | WRITE_DAC`) [verified: run v_wlan.py; SDK um\wlanapi.h:1103-1105]. `WlanSetInterface` is not in
  Learn's list of location-gated calls [verified: location doc above]. Whether the call then succeeds, and whether
  the setting reverts when the client handle closes: [unverified] (not tried, because it changes state). The
  `WlanSetInterface` page's return-code table is empty on Learn [verified: page HTML @2f7164c7].

### 3.6 Roaming (BSSID changes)

- With location consent: poll `current_connection` and compare `wlanAssociationAttributes.dot11Bssid` (6 bytes;
  layout at SDK um\wlanapi.h:584-609), or register for MSM `roaming_start`/`roaming_end` (MSM codes 5/6, um\wlanapi.h:920-926)
  [verified: header; unverified: not called].
- Without consent (this machine): treat as a roam any change in `ulChannelCenterFrequencyMhz`/channel, a step in RSSI,
  or Event Log association events (below) without a disconnect [inferred].
- `Microsoft-Windows-WLAN-AutoConfig/Operational` is enabled and readable by interactive users (`channelAccess`
  includes `(A;;0x3;;;IU)`). Over the last 300 events, IDs seen: 11010/11004/11005 (security started/stopped/succeeded,
  about 70 each), 8000/8001 (connect started/succeeded), 11000/11001 (association started/succeeded), 8003
  (disconnected), 8002 (failed). The events carry SSID and local MAC but no peer BSSID
  [verified: `wevtutil gl Microsoft-Windows-WLAN-AutoConfig/Operational`; `Get-WinEvent -LogName 'Microsoft-Windows-WLAN-AutoConfig/Operational' -MaxEvents 300`].
  **(fact-check)** Field names, read from `ToXml()`: 11000/11001/11004/11005/11010 carry `Adapter, DeviceGuid,
  LocalMac, SSID, BSSType, …, ConnectionId`; 8001 carries `ProfileName, SSID, BSSType, PHYType, …`; 8003
  (disconnected) carries `Reason` and `ReasonCode`, the field to report for "why did Wi-Fi drop". No event has a
  peer-BSSID field [verified: `Get-WinEvent … -FilterXPath "*[System[EventID=N]]"` per ID].
  Never store or upload SSIDs or BSSIDs: they locate the user [inferred].

---

## 4. Per-process traffic and the game server: Microsoft-Windows-Kernel-Network

### 4.1 Provider identity (this machine)

| Field | Value | Source |
|---|---|---|
| GUID | `{7dd42a49-5329-4832-8dfd-43d979153a88}` | [verified: `wevtutil gp Microsoft-Windows-Kernel-Network /ge /gm:true /f:xml`] |
| Keywords | `KERNEL_NETWORK_KEYWORD_IPV4` = 0x10, `KERNEL_NETWORK_KEYWORD_IPV6` = 0x20, Analytic channel bit 0x8000000000000000 | [verified: `logman query providers "Microsoft-Windows-Kernel-Network"`] |
| Level | 4 (`win:Informational`) on every event | [verified: wevtutil output] |
| Registered by | PID 0 (kernel mode) | [verified: logman output "PID 0x00000000"] |
| Tasks | 10 = `KERNEL_NETWORK_TASK_TCPIP`, 11 = `KERNEL_NETWORK_TASK_UDPIP` | [verified: wevtutil output] |
| Opcode display | wevtutil prints opcode × 65536 (655360 = opcode 10, 2752512 = 42) | [verified: wevtutil output] |

### 4.2 Event IDs (all version 0)

| ID | Opcode | Meaning (manifest message) | Keyword |
|---|---|---|---|
| 10 / 26 | 10 | TCPv4 / TCPv6: "%2 bytes transmitted from %4:%6 to %3:%5." | IPV4 / IPV6 |
| 11 / 27 | 11 | TCP: "%2 bytes received from %4:%6 to %3:%5." | IPV4 / IPV6 |
| 12 / 28 | 12 | TCP connection attempted | IPV4 / IPV6 |
| 13 / 29 | 13 | TCP connection closed | IPV4 / IPV6 |
| 14 / 30 | 14 | TCP "%2 bytes retransmitted" | IPV4 / IPV6 |
| 15 / 31 | 15 | TCP connection established (accept) | IPV4 / IPV6 |
| 16 / 32 | 16 | TCP reconnect attempt | IPV4 / IPV6 |
| 17 | 17 | TCP "Connection attempt failed with error code %2" | both |
| 18 / 34 | 18 | TCP "bytes copied in protocol on behalf of user" | IPV4 / IPV6 |
| 42 / 58 | 42 | UDPv4 / UDPv6: "%2 bytes transmitted from %4:%6 to %3:%5." | IPV4 / IPV6 |
| 43 / 59 | 43 | UDP: "%2 bytes received from %4:%6 to %3:%5." | IPV4 / IPV6 |
| 49 | 49 | UDP "Connection attempt failed with error code %2" | both |

[verified: `wevtutil gp Microsoft-Windows-Kernel-Network /ge /gm:true /f:xml`]. `wevtutil` does not print templates;
`Get-WinEvent -ListProvider` does.

### 4.3 Templates, pasted from this machine

[verified: `$p = Get-WinEvent -ListProvider Microsoft-Windows-Kernel-Network; $p.Events | ? Id -in 10,11,12,14,42,43,58,59 | % { $_.Template }`]

UDPv4 send 42 and receive 43 (same template; TCPv4 11, 13, 14, 16, 18 are identical):

```xml
<data name="PID" inType="win:UInt32" outType="xs:unsignedInt"/>
<data name="size" inType="win:UInt32" outType="xs:unsignedInt"/>
<data name="daddr" inType="win:UInt32" outType="win:IPv4"/>
<data name="saddr" inType="win:UInt32" outType="win:IPv4"/>
<data name="dport" inType="win:UInt16" outType="win:Port"/>
<data name="sport" inType="win:UInt16" outType="win:Port"/>
<data name="seqnum" inType="win:UInt32" outType="xs:unsignedInt"/>
<data name="connid" inType="win:UInt32" outType="xs:unsignedInt"/>
```

UDPv6 58/59 (TCPv6 27, 29, 30, 32, 34 identical):

```xml
<data name="PID" inType="win:UInt32" outType="xs:unsignedInt"/>
<data name="size" inType="win:UInt32" outType="xs:unsignedInt"/>
<data name="daddr" inType="win:Binary" outType="win:IPv6" length="16"/>
<data name="saddr" inType="win:Binary" outType="win:IPv6" length="16"/>
<data name="dport" inType="win:UInt16" outType="win:Port"/>
<data name="sport" inType="win:UInt16" outType="win:Port"/>
<data name="seqnum" inType="win:UInt32" outType="xs:unsignedInt"/>
<data name="connid" inType="win:UInt32" outType="xs:unsignedInt"/>
```

TCP send 10/26 add `startime`, `endtime` (UInt32 each) after `sport`. Connect/accept 12/15/28/31 add `mss`,
`sackopt`, `tsopt`, `wsopt` (UInt16), `rcvwin` (UInt32), `rcvwinscale`, `sndwinscale` (UInt16) before
`seqnum`/`connid`. Events 17/49 carry only `Proto` and `FailureCode` (UInt16 each). [verified: same command]

Byte offsets and expected `UserDataLength` [inferred from the templates; the 28-byte decoder is verified end to end
with synthetic events in 5.6, and the 52-byte IPv6 decoder by a round trip in 5.2]:

| Events | Layout (little-endian unless noted) | UserDataLength |
|---|---|---|
| 11, 13, 14, 16, 18, 42, 43 | PID@0 u32, size@4 u32, daddr@8 (4 bytes, network order), saddr@12, dport@16 u16 **big-endian**, sport@18 u16 big-endian, seqnum@20, connid@24 | 28 |
| 10 | as above + startime@20, endtime@24, seqnum@28, connid@32 | 36 |
| 12, 15 | ports, then mss@20, sackopt@22, tsopt@24, wsopt@26, rcvwin@28, rcvwinscale@32, sndwinscale@34, seqnum@36, connid@40 | 44 |
| 27, 29, 30, 32, 34, 58, 59 | PID@0, size@4, daddr@8 (16 bytes), saddr@24 (16), dport@40 BE, sport@42 BE, seqnum@44, connid@48 | 52 |
| 26 | IPv6 send + startime/endtime | 60 |
| 28, 31 | IPv6 connect/accept | 68 |
| 17, 49 | Proto@0 u16, FailureCode@2 u16 | 4 |

- `win:Port` is network byte order. Primary source **(fact-check)**: for `win:UInt16`, "When used with the win:Port
  output type, the data is treated as big-endian (network byte order)"; for `win:IPv4`, "The value must be in network
  byte order … The low-order byte contains the value for p1" [verified:
  learn.microsoft.com/en-us/windows/win32/wes/eventmanifestschema-inputtype-complextype @c4c9c064,
  …/eventmanifestschema-outputtype-complextype @1ba36e36]. PerfView agrees: it reads `dport` as
  `ByteSwap16(GetInt16At(16))` and `daddr` as the 4 bytes at offset 8 [verified:
  raw.githubusercontent.com/microsoft/perfview/3d89ee8570a943290c4b517663f87493cfbaa35b/src/TraceEvent/Parsers/KernelTraceEventParser.cs
  lines 6568, 7270, 7274 (`TcpIpTraceData`/`UdpIpTraceData`); the original cited branch `main`, pinned here to the
  latest commit touching the file, 2026-10-07].
- **Assert the layout at runtime:** if `EventDescriptor.Version != 0` or `UserDataLength` differs from the table,
  count the event as "unknown layout" and surface it [inferred]. PerfView's parser is for the classic kernel-logger
  `TcpIp` event and reads `connid` as pointer-sized via `GetAddressAt(HostOffset(20, 1))` (line 6570); the
  Kernel-Network manifest here says `seqnum`@20 then `connid`@24, both UInt32. Use the manifest, not PerfView, past
  offset 20 [verified: both sources above].

### 4.4 Which address is the remote one (unverified)

The MOF docs only say "Destination IP address" / "Source IP address"
[verified: learn.microsoft.com/en-us/windows/win32/etw/udpip-typegroup1 @4d5f26c6, …/tcpip-typegroup1 @4d5f26c6].
The manifest message for receive reads "received from %4:%6 to %3:%5", i.e. from `saddr` to `daddr`, which suggests
`saddr` is the remote peer on receive [verified: wevtutil output; inferred: the reading]. I could not capture a real
event unelevated, so this is **[unverified]**.

**(fact-check) Secondary evidence that TCP and UDP differ.** System Informer (formerly Process Hacker) parses the
classic kernel-logger `TcpIp`/`UdpIp` events, whose payload structs `WMI_TCPIP_V4`/`WMI_UDP_V4` have exactly this
manifest's field order and 28-byte size (`ProcessId, TransferSize, DestinationAddress[4], SourceAddress[4],
DestinationPort, SourcePort, SequenceNumber, ConnectionId`). For TCP send **and** receive it maps
`LocalEndpoint = Source…` and `RemoteEndpoint = Destination…`, i.e. `saddr` is always local. For UDP receive it
swaps them, with the comment "The endpoints are swapped for incoming UDP packets. The destination endpoint
corresponds to the local socket not the source endpoint." [verified:
raw.githubusercontent.com/winsiderss/systeminformer/0a4975a4763ad202747397ef5daf5893849b5b00/plugins/ExtendedTools/etwmon.c
lines 416-437 (TCP receive) and 525-547 (UDP receive, comment at 532-533); …/phnt/include/ntwmi.h lines 2729-2739,
2834-2844]. If Kernel-Network logs the same way (same instrumentation, [inferred]), then for the game's UDP
receive events `saddr:sport` is the server and for TCP receive events it is the local socket, which is the
opposite of what the manifest's TCP message suggests. Kernel-Network itself remains **[unverified]**.

Robust rule [inferred]: classify per event; the remote end is the address that is not in `psutil.net_if_addrs()`
(for loopback, where both are local, use the port the PID does not own), and the local port is the one owned by the
PID (`psutil.net_connections`).

Elevated self-test to settle it (run once, as admin or Performance Log Users) [inferred]: start the session from
section 5, send one UDP DNS query to the gateway from a socket bound to a known local port **and** open one TCP
connection to a known host, then check which of `daddr`/`saddr` holds the remote address in events 42, 43, 10 and 11.

### 4.5 Filtering

- **Do not use the PID scope filter for this provider.** "In case a process ID filter is provided, then the provider
  will be enabled in the user-mode processes only. In case, the same provider is registered by a kernel-mode driver,
  it will not be enabled." Kernel-Network is registered by PID 0 [verified: learn.microsoft.com/en-us/windows/win32/api/evntprov/ns-evntprov-event_filter_descriptor @92672624; logman output].
  Filter on the payload `PID` in the callback instead [inferred].
- `EVENT_FILTER_TYPE_EVENT_ID` (0x80000200, up to 64 IDs) can cut volume [verified: same page]; whether this kernel
  provider honours it: [unverified].
- `MatchAnyKeyword = 0x30` enables IPv4 + IPv6; for manifest providers 0 means "all keywords"
  [verified: learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-enabletraceex2 @540c1f2d].

### 4.6 Identifying the game server and detecting packet gaps

All [inferred] design, built on the verified event semantics above:

1. **Game PIDs**: the game process plus children (psutil). Anti-cheat services are separate processes; ignore them.
2. **Flow key**: `(pid, proto, remote_ip, remote_port)`. Keep per-flow rx/tx packet and byte counts and the last 512
   rx timestamps (`EventHeader.TimeStamp`, FILETIME in 100 ns units, see 5.4).
3. **Game server flow** = the UDP flow of a game PID with the most rx packets in a sliding 10 s window that is
   bidirectional (rx and tx ≥ 10 packets/s). Skip remote port 53. In-game voice (for example 50 packets/s of 20 ms
   audio frames) can be a second flow; prefer the flow with the higher rx rate, then the higher rx bytes. For Steam
   games behind SDR the "server" is a Valve relay (2.4); that is fine for timing.
4. **Nominal tick** = 1 / median rx inter-arrival over 10 s. Riot's servers run at 128 Hz (7.8 ms) [verified: Riot
   blog]; others will differ. Re-estimate continuously.
5. **Gap** = rx inter-arrival > max(3 × nominal, nominal + 50 ms). Severity: ≥ 100 ms visible, ≥ 250 ms severe.
   These numbers are a starting point to tune against recordings.
6. **Classify each gap:**
   - Burst after the gap (≥ 3 packets within 0.25 × nominal): packets were delayed, then flushed together. Queueing:
     Wi-Fi retries, AP power save, bufferbloat.
   - No burst, missing count ≈ gap / nominal - 1: true loss upstream or a server stall.
   - The client's own tx stream to the server also paused: local hitch (game or OS stall), not the network.
     Cross-check PresentMon frame times.
7. **Loss estimate** per 15 s = Σ missing ÷ (Σ missing + received), compared with Teams' 1 % / 15 s and 10 % / 200 ms.
   **Jitter** = RFC 3550 J using `S_i = i × nominal` [verified: formula; inferred: the substitution].
8. **Ordering**: ProcessTrace "attempts to deliver events in order based on the event's timestamp… In certain cases,
   ProcessTrace might deliver events out of order" [verified: learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-processtrace @540c1f2d].
   Keep a 2 s reorder buffer per flow and compute inter-arrival on sorted timestamps [inferred].

### 4.7 Fallbacks without ETW (measured)

| Source | Result here | Verdict |
|---|---|---|
| `psutil.net_connections(kind='inet')` | 728 sockets, every PID present, 68 TCP established; **0 of 46 UDP sockets have a remote address** (fact-check re-run: 158 sockets, all with PIDs, 62 TCP established, 0 of 46 UDP with a remote address) | gives TCP peers per process only; cannot find a UDP game server [verified: psutil one-liner; run v_psutil.py]. **(fact-check)** This is structural, not incidental: the Windows UDP table row `MIB_UDPROW_OWNER_PID` has only `dwLocalAddr`, `dwLocalPort`, `dwOwningPid` [verified: learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udprow_owner_pid @92672624] |
| `psutil.Process().io_counters()` | 20 MiB sent and received over TCP loopback inside one process → `other_bytes` +17.6 MB (of 40 MiB moved); the same over UDP in 1,200-byte datagrams → `other_count` +17,486, `other_bytes` only +559 KB (32 bytes per operation). Fact-check re-run (10 MiB each way): UDP 8,738 datagrams → `other_count` +17,486 (one per send and one per receive), `other_bytes` +419,544 (24 B per operation); TCP → `other_bytes` +8,978,432 of 20 MiB moved | not a byte meter for sockets; `other_count` rate is at best a rough packet-rate hint [verified: run iocounters_probe.py; run v_io.py] |

---

## 5. Real-time ETW from Python via ctypes (tested recipe)

### 5.1 Structure layouts (x64) and how each was proven

Definitions: SDK shared\wmistr.h:30-54 (`WNODE_HEADER`), shared\evntrace.h:544-579, 697-738, 862-888, 1051-1062,
1131-1165, 1556-1571, 972-981; um\evntcons.h:71-83, 148-185; shared\evntprov.h:204-319 [verified: SDK files]. Learn
pages fetched for these structs give no byte sizes, so sizes are checked by arithmetic from the headers and by APIs
accepting and filling the structs [verified: pages listed in Sources].

| Struct | sizeof | Key offsets | Runtime proof |
|---|---|---|---|
| `WNODE_HEADER` | 48 | `Guid` @24, `ClientContext` @40, `Flags` @44 | private session started with our provider GUID [verified: run etw_probe.py] |
| `EVENT_TRACE_PROPERTIES` | 120 | `BufferSize` @48, `LogFileMode` @64, `FlushTimer` @68, `EventsLost` @88, `RealTimeBuffersLost` @100, `LoggerThreadId` @104, `LogFileNameOffset` @112, `LoggerNameOffset` @116 | `StartTraceW` accepted it; `ControlTraceW(QUERY)` filled sane values and the session name at `LoggerNameOffset` [verified: run etw_probe.py] |
| `EVENT_TRACE_HEADER` | 48 | — | part of `EVENT_TRACE` |
| `EVENT_TRACE` | 88 | `MofData` @72 | part of `EVENT_TRACE_LOGFILEW` |
| `TIME_ZONE_INFORMATION` | 172 | — | `TimeZone.StandardName` decoded as text `'@tzres.dll,-212'` |
| `TRACE_LOGFILE_HEADER` | 280 | `PointerSize` @44, `LoggerName` @56, `TimeZone` @72, `BootTime` @248, `PerfFreq` @256, `StartTime` @264, `ReservedFlags` @272 | `OpenTraceW` filled PointerSize = 8, NumberOfProcessors = 16, ProviderVersion = 26200, PerfFreq = 10,000,000, plausible boot/start/end times; on ExplorerStartupLog.etl it equalled the header copy carried by the file's first event [verified: run etw_probe.py] |
| `EVENT_TRACE_LOGFILEW` | 448 | `LoggerName` @8, `ProcessTraceMode` @28, `CurrentEvent` @32, `LogfileHeader` @120, `BufferCallback` @400, `EventRecordCallback` @424, `IsKernelTrace` @432, `Context` @440 | `OpenTraceW` read the name and mode and called our callback for every event [verified: run etw_probe.py] |
| `EVENT_DESCRIPTOR` | 16 | — | Id 43, Opcode 43, Task 11, Keyword 0x10 read back |
| `EVENT_HEADER` | 80 | `ProcessId` @12, `TimeStamp` @16, `ProviderId` @24, `EventDescriptor` @40, `ActivityId` @64 | PID equalled our PID; timestamps matched the wall clock |
| `ETW_BUFFER_CONTEXT` | 4 | — | — |
| `EVENT_RECORD` | 112 | `BufferContext` @80, `UserDataLength` @86, `ExtendedData` @88, `UserData` @96, `UserContext` @104 | 100,000 payloads read byte-exact, `UserDataLength` = 28, `EventHeader.Size` = 108 = 80 + 28 |
| `EVENT_HEADER_EXTENDED_DATA_ITEM` | 16 | — | not exercised |
| `ENABLE_TRACE_PARAMETERS` | 48 | `EnableFilterDesc` @32, `FilterDescCount` @40 | not exercised (passed NULL) |
| `EVENT_FILTER_DESCRIPTOR`, `EVENT_DATA_DESCRIPTOR` | 16 each | — | `EventWrite` with one descriptor succeeded 100,000 times |

Constants [verified: SDK shared\evntrace.h:130-474, 1067-1069, 1553-1554, 2291; um\evntcons.h:135-209; shared\wmistr.h:110,128]:
`WNODE_FLAG_TRACED_GUID` 0x00020000; `EVENT_TRACE_REAL_TIME_MODE` 0x100; `EVENT_TRACE_FILE_MODE_SEQUENTIAL` 0x1;
`EVENT_TRACE_PRIVATE_LOGGER_MODE` 0x800; `EVENT_TRACE_PRIVATE_IN_PROC` 0x20000;
`EVENT_TRACE_INDEPENDENT_SESSION_MODE` 0x08000000; `EVENT_TRACE_CONTROL_QUERY/STOP/UPDATE/FLUSH` 0/1/2/3;
`EVENT_CONTROL_CODE_ENABLE_PROVIDER` 1; `TRACE_LEVEL_INFORMATION` 4; `PROCESS_TRACE_MODE_REAL_TIME` 0x100;
`PROCESS_TRACE_MODE_RAW_TIMESTAMP` 0x1000; `PROCESS_TRACE_MODE_EVENT_RECORD` 0x10000000;
`INVALID_PROCESSTRACE_HANDLE` = `(TRACEHANDLE)INVALID_HANDLE_VALUE` = 0xFFFFFFFFFFFFFFFF on 64-bit
[verified: also learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-opentracew @2d357572]. Error codes
[verified: SDK shared\winerror.h]: 5 `ERROR_ACCESS_DENIED`, 24 `ERROR_BAD_LENGTH`, 87 `ERROR_INVALID_PARAMETER`,
183 `ERROR_ALREADY_EXISTS`, 1223 `ERROR_CANCELLED`, 1450 `ERROR_NO_SYSTEM_RESOURCES`, 4201
`ERROR_WMI_INSTANCE_NOT_FOUND`, 7007 `ERROR_CTX_CLOSE_PENDING`.

### 5.2 The module (tested)

This is the module that produced section 5.6, minus the probe-only bindings (`EventRegister`, `EventWrite`,
`EventEnabled`, `QueryAllTracesW`, `EVENT_DATA_DESCRIPTOR`). The block below was also re-run straight from this
document: every size matched 5.1, the parser round-tripped an IPv4 (28-byte) and an IPv6 (52-byte) payload, and
`KernelNetworkSession.start()` from 5.5 raised errno 5 [verified: run doc_blocks_check.py on this file].

```python
"""etw_ctypes.py: minimal ETW controller/consumer bindings for ctypes (x64)."""
import ctypes, ctypes.wintypes as wt, socket, struct, uuid

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
ULONG, USHORT, UCHAR = ctypes.c_ulong, ctypes.c_ushort, ctypes.c_ubyte
TRACEHANDLE = ctypes.c_uint64

WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_FILE_MODE_SEQUENTIAL, EVENT_TRACE_REAL_TIME_MODE = 0x1, 0x100
EVENT_TRACE_PRIVATE_LOGGER_MODE, EVENT_TRACE_PRIVATE_IN_PROC = 0x800, 0x20000
EVENT_TRACE_CONTROL_QUERY, EVENT_TRACE_CONTROL_STOP = 0, 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
TRACE_LEVEL_INFORMATION, TRACE_LEVEL_VERBOSE = 4, 5
PROCESS_TRACE_MODE_REAL_TIME, PROCESS_TRACE_MODE_EVENT_RECORD = 0x100, 0x10000000
INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ULONG), ("Data2", USHORT), ("Data3", USHORT), ("Data4", UCHAR * 8)]
    @classmethod
    def of(cls, s):
        g = cls(); ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16); return g
    def uuid(self):
        return uuid.UUID(bytes_le=bytes(self))

class WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", ULONG), ("ProviderId", ULONG), ("HistoricalContext", ctypes.c_uint64),
                ("TimeStamp", ctypes.c_int64), ("Guid", GUID), ("ClientContext", ULONG), ("Flags", ULONG)]

class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", ULONG), ("MinimumBuffers", ULONG),
                ("MaximumBuffers", ULONG), ("MaximumFileSize", ULONG), ("LogFileMode", ULONG),
                ("FlushTimer", ULONG), ("EnableFlags", ULONG), ("FlushThreshold", ctypes.c_long),
                ("NumberOfBuffers", ULONG), ("FreeBuffers", ULONG), ("EventsLost", ULONG),
                ("BuffersWritten", ULONG), ("LogBuffersLost", ULONG), ("RealTimeBuffersLost", ULONG),
                ("LoggerThreadId", wt.HANDLE), ("LogFileNameOffset", ULONG), ("LoggerNameOffset", ULONG)]

class EVENT_TRACE_HEADER(ctypes.Structure):
    _fields_ = [("Size", USHORT), ("FieldTypeFlags", USHORT), ("Version", ULONG), ("ThreadId", ULONG),
                ("ProcessId", ULONG), ("TimeStamp", ctypes.c_int64), ("Guid", GUID), ("ProcessorTime", ctypes.c_uint64)]

class EVENT_TRACE(ctypes.Structure):
    _fields_ = [("Header", EVENT_TRACE_HEADER), ("InstanceId", ULONG), ("ParentInstanceId", ULONG),
                ("ParentGuid", GUID), ("MofData", ctypes.c_void_p), ("MofLength", ULONG), ("ClientContext", ULONG)]

class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wt.WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds")]

class TIME_ZONE_INFORMATION(ctypes.Structure):
    _fields_ = [("Bias", ctypes.c_long), ("StandardName", ctypes.c_wchar * 32), ("StandardDate", SYSTEMTIME),
                ("StandardBias", ctypes.c_long), ("DaylightName", ctypes.c_wchar * 32), ("DaylightDate", SYSTEMTIME),
                ("DaylightBias", ctypes.c_long)]

class TRACE_LOGFILE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", ULONG), ("Version", ULONG), ("ProviderVersion", ULONG), ("NumberOfProcessors", ULONG),
                ("EndTime", ctypes.c_int64), ("TimerResolution", ULONG), ("MaximumFileSize", ULONG),
                ("LogFileMode", ULONG), ("BuffersWritten", ULONG),
                ("StartBuffers", ULONG), ("PointerSize", ULONG), ("EventsLost", ULONG), ("CpuSpeedInMHz", ULONG),
                ("LoggerName", ctypes.c_void_p), ("LogFileName", ctypes.c_void_p), ("TimeZone", TIME_ZONE_INFORMATION),
                ("BootTime", ctypes.c_int64), ("PerfFreq", ctypes.c_int64), ("StartTime", ctypes.c_int64),
                ("ReservedFlags", ULONG), ("BuffersLost", ULONG)]

class EVENT_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Id", USHORT), ("Version", UCHAR), ("Channel", UCHAR), ("Level", UCHAR), ("Opcode", UCHAR),
                ("Task", USHORT), ("Keyword", ctypes.c_uint64)]

class EVENT_HEADER(ctypes.Structure):
    _fields_ = [("Size", USHORT), ("HeaderType", USHORT), ("Flags", USHORT), ("EventProperty", USHORT),
                ("ThreadId", ULONG), ("ProcessId", ULONG), ("TimeStamp", ctypes.c_int64), ("ProviderId", GUID),
                ("EventDescriptor", EVENT_DESCRIPTOR), ("ProcessorTime", ctypes.c_uint64), ("ActivityId", GUID)]

class ETW_BUFFER_CONTEXT(ctypes.Structure):
    _fields_ = [("ProcessorNumber", UCHAR), ("Alignment", UCHAR), ("LoggerId", USHORT)]

class EVENT_HEADER_EXTENDED_DATA_ITEM(ctypes.Structure):
    _fields_ = [("Reserved1", USHORT), ("ExtType", USHORT), ("Linkage", USHORT), ("DataSize", USHORT),
                ("DataPtr", ctypes.c_uint64)]

class EVENT_RECORD(ctypes.Structure):
    _fields_ = [("EventHeader", EVENT_HEADER), ("BufferContext", ETW_BUFFER_CONTEXT),
                ("ExtendedDataCount", USHORT), ("UserDataLength", USHORT),
                ("ExtendedData", ctypes.POINTER(EVENT_HEADER_EXTENDED_DATA_ITEM)),
                ("UserData", ctypes.c_void_p), ("UserContext", ctypes.c_void_p)]

EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))
BUFFER_CALLBACK = ctypes.WINFUNCTYPE(ULONG, ctypes.c_void_p)

class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    _fields_ = [("LogFileName", ctypes.c_wchar_p), ("LoggerName", ctypes.c_wchar_p), ("CurrentTime", ctypes.c_int64),
                ("BuffersRead", ULONG), ("ProcessTraceMode", ULONG), ("CurrentEvent", EVENT_TRACE),
                ("LogfileHeader", TRACE_LOGFILE_HEADER), ("BufferCallback", BUFFER_CALLBACK),
                ("BufferSize", ULONG), ("Filled", ULONG), ("EventsLost", ULONG),
                ("EventRecordCallback", EVENT_RECORD_CALLBACK), ("IsKernelTrace", ULONG), ("Context", ctypes.c_void_p)]

class EVENT_FILTER_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Ptr", ctypes.c_uint64), ("Size", ULONG), ("Type", ULONG)]

class ENABLE_TRACE_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", ULONG), ("EnableProperty", ULONG), ("ControlFlags", ULONG), ("SourceId", GUID),
                ("EnableFilterDesc", ctypes.POINTER(EVENT_FILTER_DESCRIPTOR)), ("FilterDescCount", ULONG)]

advapi32.StartTraceW.argtypes = [ctypes.POINTER(TRACEHANDLE), ctypes.c_wchar_p, ctypes.c_void_p]
advapi32.StartTraceW.restype = ULONG
advapi32.ControlTraceW.argtypes = [TRACEHANDLE, ctypes.c_wchar_p, ctypes.c_void_p, ULONG]
advapi32.ControlTraceW.restype = ULONG
advapi32.EnableTraceEx2.argtypes = [TRACEHANDLE, ctypes.POINTER(GUID), ULONG, UCHAR, ctypes.c_uint64,
                                    ctypes.c_uint64, ULONG, ctypes.POINTER(ENABLE_TRACE_PARAMETERS)]
advapi32.EnableTraceEx2.restype = ULONG
advapi32.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
advapi32.OpenTraceW.restype = TRACEHANDLE
advapi32.ProcessTrace.argtypes = [ctypes.POINTER(TRACEHANDLE), ULONG, ctypes.c_void_p, ctypes.c_void_p]
advapi32.ProcessTrace.restype = ULONG
advapi32.CloseTrace.argtypes = [TRACEHANDLE]
advapi32.CloseTrace.restype = ULONG

NAME_CHARS = 1024   # session-name and log-file-name limits (StartTraceW / EVENT_TRACE_PROPERTIES docs)

class Properties:
    """EVENT_TRACE_PROPERTIES followed by room for the session name, then the log file name."""
    def __init__(self, logfile=None, mode=EVENT_TRACE_REAL_TIME_MODE, guid=None, buffer_kb=64,
                 min_buffers=0, max_buffers=0, flush_timer_s=1, clock=1):
        base = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
        total = base + 2 * NAME_CHARS * ctypes.sizeof(ctypes.c_wchar)
        self.buf = ctypes.create_string_buffer(total)          # zero-filled, as the docs require
        p = self.p = EVENT_TRACE_PROPERTIES.from_buffer(self.buf)
        p.Wnode.BufferSize = total
        p.Wnode.Flags = WNODE_FLAG_TRACED_GUID
        p.Wnode.ClientContext = clock                            # 1 = QPC
        if guid is not None:
            p.Wnode.Guid = guid
        p.BufferSize, p.MinimumBuffers, p.MaximumBuffers = buffer_kb, min_buffers, max_buffers
        p.FlushTimer, p.LogFileMode = flush_timer_s, mode
        p.LoggerNameOffset = base                                # StartTraceW copies the name here
        if logfile:                                              # you copy the file name yourself
            p.LogFileNameOffset = base + NAME_CHARS * ctypes.sizeof(ctypes.c_wchar)
            raw = logfile.encode("utf-16-le") + b"\0\0"
            ctypes.memmove(ctypes.addressof(self.buf) + p.LogFileNameOffset, raw, len(raw))
    def ptr(self):
        return ctypes.addressof(self.buf)
    def string_at_offset(self, off):
        return ctypes.wstring_at(ctypes.addressof(self.buf) + off) if off else ""

def stop_session_by_name(name):
    """Orphan cleanup. Returns 0 = stopped one, 4201 = none running, 5 = no rights."""
    props = Properties()
    return advapi32.ControlTraceW(0, name, props.ptr(), EVENT_TRACE_CONTROL_STOP), props

KERNEL_NETWORK = GUID.of("7dd42a49-5329-4832-8dfd-43d979153a88")
KW_IPV4, KW_IPV6 = 0x10, 0x20
_V4 = {10, 11, 12, 13, 14, 15, 16, 18, 42, 43}
_V6 = {26, 27, 28, 29, 30, 31, 32, 34, 58, 59}
_CONNECT, _TCP_SEND = {12, 15, 28, 31}, {10, 26}

def parse_kernel_network(event_id, data):
    if event_id in _V4:   alen, fam = 4, socket.AF_INET
    elif event_id in _V6: alen, fam = 16, socket.AF_INET6
    else:                 return None                 # 17/49 carry Proto, FailureCode only
    pid, size = struct.unpack_from("<II", data, 0); off = 8
    daddr = socket.inet_ntop(fam, data[off:off + alen]); off += alen
    saddr = socket.inet_ntop(fam, data[off:off + alen]); off += alen
    dport, sport = struct.unpack_from(">HH", data, off); off += 4      # win:Port = network byte order
    extra = {}
    if event_id in _TCP_SEND:
        extra["startime"], extra["endtime"] = struct.unpack_from("<II", data, off); off += 8
    elif event_id in _CONNECT:
        mss, _sack, _ts, _ws, rcvwin, _rws, _sws = struct.unpack_from("<HHHHIHH", data, off); off += 16
        extra.update(mss=mss, rcvwin=rcvwin)
    seqnum, connid = struct.unpack_from("<II", data, off); off += 8
    return dict(pid=pid, size=size, daddr=daddr, saddr=saddr, dport=dport, sport=sport,
                seqnum=seqnum, connid=connid, consumed=off, **extra)

FILETIME_EPOCH_DELTA_100NS = 116444736000000000
def filetime_to_unix(ft):
    return (ft - FILETIME_EPOCH_DELTA_100NS) / 1e7
```

### 5.3 Controller: buffer sizing and parameters

- `BufferSize`: KB per buffer, 4 to 16384; "Most trace sessions should use a buffer size of 64KB or less"; small
  events at a moderate rate → 16-32 KB; high rate → 64-128 KB. ETW cannot collect events larger than 64 KB
  [verified: learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-event_trace_properties @92672624].
  Use 64 KB [inferred].
- `MinimumBuffers`: ETW reserves at least 2 per logical processor without `EVENT_TRACE_NO_PER_PROCESSOR_BUFFERING`
  [verified: same page]. Measured: requested 4, got 32 (16 logical CPUs × 2) [verified: run etw_probe.py,
  `ControlTraceW(QUERY) … Min=32`]. Pass 0 and let ETW choose. Memory = 32 × 64 KB = 2 MiB here [inferred].
- `MaximumBuffers`: "start tuning … by setting MinimumBuffers and MaximumBuffers to the same value"; raise
  `MaximumBuffers` if events drop at peaks [verified: same page]. Start with 0 (ETW default) or 64 [inferred].
  **(fact-check)** With 0 passed, ETW chose `Max=54` in the re-run (16 logical CPUs); the original run's output
  shows 64 [verified: run v_etw.py, `ControlTraceW(QUERY) … Min=32 Max=54`].
- **(fact-check) Pre-allocate for a kernel provider.** Learn: "ETW cannot allocate on-demand buffers if the event is
  generated by a driver running at high IRQL. If your trace session needs to record events from high-IRQL
  kernel-mode providers, it may need to use a higher value of MinimumBuffers to force the buffers to be
  preallocated" [verified: EVENT_TRACE_PROPERTIES page @92672624]. Whether tcpip logs Kernel-Network events at
  raised IRQL is [unverified]. Start with the defaults (0/0, as the 5.5 class does); if `EventsLost` or
  `RealTimeBuffersLost` rises, restart the session with `MinimumBuffers = MaximumBuffers = 64` (4 MiB at 64 KB)
  [inferred].
- `FlushTimer`: for real time, 0 means a default of 1 s; "Most real-time traces should start with a timer of 5 or 10
  seconds" [verified: same page]. Use 1 s so gaps show within about a second; analysis uses event timestamps, so
  delivery latency does not distort the measurements [inferred].
- `Wnode.ClientContext = 1` (QPC): unaffected by system clock changes; the default since Vista [verified:
  learn.microsoft.com/en-us/windows/win32/etw/wnode-header @0d36b58c].
- `Wnode.Guid`: leave zero for a normal session; ETW generates one [verified: StartTraceW page @2d357572].
- Real-time without a consumer wastes resources and, when ETW's buffering limit is hit, "new events will be ignored"
  [verified: EVENT_TRACE_PROPERTIES page]. So start the consumer straight after `EnableTraceEx2`.
- **Session name**: descriptive and fixed (`lag-doctor-net`). Learn: "Do not append random digits… If your component's
  session is already running when your component starts, your component should clean up the orphaned session rather
  than creating a second session" [verified: StartTraceW page @2d357572]. Up to 64 sessions system-wide on most
  systems [verified: same page].
- `EnableTraceEx2(session, &KERNEL_NETWORK, EVENT_CONTROL_CODE_ENABLE_PROVIDER, TRACE_LEVEL_INFORMATION (4),
  MatchAnyKeyword = 0x30, MatchAllKeyword = 0, Timeout = 0, NULL)`. `Timeout` 0 = asynchronous
  [verified: EnableTraceEx2 page @540c1f2d]. Up to 8 sessions can enable one manifest provider; "A provider remains
  enabled for the session until the session disables the provider" [verified: same page].

### 5.4 Consumer, threading, timestamps

- `EVENT_TRACE_LOGFILEW`: set `LoggerName` (real time) or `LogFileName` (file), `ProcessTraceMode =
  PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD` (real time), `EventRecordCallback`; then
  `OpenTraceW`. Failure = `INVALID_PROCESSTRACE_HANDLE` plus `GetLastError()` [verified: OpenTraceW page @2d357572].
- **OpenTraceW succeeding proves nothing for real time**: on a session name that does not exist it returned a valid
  handle (0x101) with `GetLastError` 0, and `ProcessTrace` then returned 4201 after 3 ms. Always check
  `ProcessTrace`'s return code [verified: run etw_probe.py].
- `ProcessTrace` blocks; run it on a worker thread. One real-time handle per call, or up to 64 file handles
  [verified: ProcessTrace page @540c1f2d]. With `WinDLL` the GIL is released during the call ("The Python global
  interpreter lock is released before calling any function exported by these libraries") [verified:
  docs.python.org/3.13/library/ctypes.html]; the callback then runs Python code on that same worker thread
  [verified: run v_etw.py; inferred: that it re-acquires the GIL, which the docs do not spell out].
- **(fact-check)** Other ProcessTrace return codes worth mapping: `ERROR_NOACCESS` (998) "An exception occurred in one
  of the callback functions"; `ERROR_CANCELLED` (1223) when a BufferCallback returned FALSE; 4201 "The trace
  collection session … is not running or does not have the real-time trace mode enabled" [verified: ProcessTrace
  page @540c1f2d; SDK shared\winerror.h:6122].
- Keep the `EVENT_RECORD_CALLBACK` object and the `EVENT_TRACE_LOGFILEW` alive while ProcessTrace runs: "Make sure you
  keep references to CFUNCTYPE() objects as long as they are used from C code" [verified: ctypes docs]. Catch every
  exception inside the callback. **(fact-check)** Measured: an exception that escapes is printed to stderr as
  "Exception ignored on calling ctypes callback function" with a full traceback, and ProcessTrace keeps going (a
  callback that raised on its first two events still received all 100,002 events and ProcessTrace returned 0). So
  an escaping exception does not stop the trace; it floods stderr at the event rate and the error goes uncounted
  [verified: run v_etw.py].
- **(fact-check) `ctypes.FormatError(0)` is not "success":** CPython substitutes `GetLastError()` for a code of 0,
  so formatting a successful return code printed "Access is denied." in the re-run. Format only non-zero codes
  [verified: run v_etw.py, `StartTraceW(...) -> 0 (Access is denied.)`].
- Timestamps: without `PROCESS_TRACE_MODE_RAW_TIMESTAMP`, "the ETW runtime will automatically convert the timestamp
  of each EVENT_RECORD into a FILETIME" [verified: WNODE_HEADER page @0d36b58c]. The first two events decoded
  25 µs apart (14.940314 → 14.940339), so the converted timestamps keep sub-millisecond resolution
  [verified: run etw_probe.py]. **(fact-check)** The converted value is a UTC FILETIME: the first event read
  `2026-11-05 00:45:07.970240` while `time.time()` just before the first `EventWrite` gave `00:45:07.970221` UTC, and
  consecutive events were 10.0 and 5.6 µs apart [verified: run v_etw.py]. Use `EventHeader.TimeStamp` for
  inter-arrival, never the callback's arrival time: real-time "Events are delivered when the buffers are flushed, not
  at the time the provider writes the event" [verified: learn.microsoft.com/en-us/windows/win32/etw/logging-mode-constants @9359c413].
- Throughput: the Python callback decoded 100,002 events in 2.159 s = **46,323 events/s** including
  `parse_kernel_network`, and 84,998 events/s on the Explorer file with a callback that does not parse payloads
  [verified: run etw_probe.py]. Fact-check re-run with a heavier callback (provider check, parse, payload compare):
  100,002 events in 1.435 s = 69,688 events/s [verified: run v_etw.py]. So budget 14-22 µs per event.
  At 1,000 events/s that is about 2 % of one core [inferred: 1000 × 21.6 µs].
- **(fact-check) The event rate is set by all traffic, not by the game.** The provider's only keywords are IPv4 and
  IPv6 (4.1), so TCP events cannot be switched off by keyword, and the payload-PID filter runs in Python after
  delivery. A background download can therefore add thousands of TCP events per second: at 10,000 events/s the
  callback alone costs 14-22 % of one core [inferred: arithmetic from the measured per-event cost; the per-event
  granularity of TCP receive events is [unverified]]. Mitigations: try `EVENT_FILTER_TYPE_EVENT_ID` for
  42/43/58/59 (+14/30) first and check it took effect from the event mix (4.5), drop unwanted IDs before parsing,
  and report "ETW sampling degraded" when `EventsLost` rises.
- Health: poll `ControlTraceW(session, NULL, props, EVENT_TRACE_CONTROL_QUERY)` every 10 s and report
  `EventsLost`/`RealTimeBuffersLost` > 0 as "degraded" [verified: EVENT_TRACE_PROPERTIES page; inferred: the cadence].
  Also report a consumer thread that has exited (`pump_alive` False with `process_trace_rc` set, in the class's
  `health()`) as "ETW consumer stopped", since ProcessTrace can return on its own (4201, 998, session stopped
  elsewhere) [inferred].

### 5.5 Stopping and cleanup

- Stop: `ControlTraceW(session, NULL, props, EVENT_TRACE_CONTROL_STOP)`. Then ProcessTrace "returns after the
  controller stops the trace session… there may be a delay of several seconds"; join the thread with a timeout, then
  `CloseTrace(consumer)` [verified: ProcessTrace page @540c1f2d; StartTraceW page].
- Alternatively `CloseTrace` first: it returns 7007 `ERROR_CTX_CLOSE_PENDING`, which means success; ProcessTrace drains
  what is queued and returns [verified: learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-closetrace @540c1f2d].
  CloseTrace closes only the consumer handle, so the session keeps running until `ControlTraceW(STOP)` [inferred from
  the same page: "Do not use this function to close the trace handle returned by StartTrace"].
- **Sessions outlive the process**: "The session remains active until the session is stopped, the computer is
  restarted, an I/O error occurs…" [verified: StartTraceW page]. So: (1) at startup call
  `stop_session_by_name("lag-doctor-net")` (0 = an orphan was stopped, 4201 = none); (2) on
  `ERROR_ALREADY_EXISTS` (183) stop once and retry; (3) register `atexit` and stop in `finally`. Manual cleanup for a
  user, elevated: `logman stop lag-doctor-net -ets` [verified: `logman stop /?` shows `-ets`].
- Unelevated, `logman query -ets` and `QueryAllTracesW` list only `MSDTC_TRACE_SESSION`, so orphan detection by
  enumeration only works elevated; stopping by name does not need enumeration [verified: `logman query -ets`; run etw_probe.py; run v_etw.py].
  **(fact-check)** `logman query MSDTC_TRACE_SESSION -ets` shows "File Mode: File", "Circular: On", so its
  ProcessTrace 4201 in 5.6 is the documented "does not have the real-time trace mode enabled" case, not an access
  problem [verified: that command; ProcessTrace page]. Learn marks `OpenTraceW` `ETW_APP_DECLSPEC_DEPRECATED`; in the
  SDK that macro only expands to a deprecation for Store-app/Games partitions, not desktop code [verified: SDK
  shared\evntrace.h:28-41].

The real-time class, ready for elevated use:

```python
import atexit, threading                   # plus everything defined in the 5.2 module

class KernelNetworkSession:
    """Real-time Kernel-Network session. Needs an elevated token or Performance Log Users."""
    SESSION_NAME = "lag-doctor-net"
    WANTED = {42, 43, 58, 59, 10, 11, 26, 27, 14, 30}

    def __init__(self, sink):
        self.sink = sink                       # sink(filetime_100ns, event_id, parsed_dict); must be fast
        self.session = TRACEHANDLE(0)
        self.consumer = INVALID_PROCESSTRACE_HANDLE
        self.thread = None
        self.process_trace_rc = None
        self.callback_errors = 0
        self._cb = EVENT_RECORD_CALLBACK(self._on_event)   # keep a reference

    def start(self):
        rc, _ = stop_session_by_name(self.SESSION_NAME)     # orphan cleanup
        if rc not in (0, 4201):
            raise OSError(rc, "orphan cleanup failed: " + ctypes.FormatError(rc).strip())
        self.props = Properties(mode=EVENT_TRACE_REAL_TIME_MODE, buffer_kb=64, flush_timer_s=1, clock=1)
        rc = advapi32.StartTraceW(ctypes.byref(self.session), self.SESSION_NAME, self.props.ptr())
        if rc == 183:                                        # ERROR_ALREADY_EXISTS: lost a race
            stop_session_by_name(self.SESSION_NAME)
            rc = advapi32.StartTraceW(ctypes.byref(self.session), self.SESSION_NAME, self.props.ptr())
        if rc:
            raise OSError(rc, "StartTraceW: " + ctypes.FormatError(rc).strip())
        atexit.register(self.stop)
        rc = advapi32.EnableTraceEx2(self.session, ctypes.byref(KERNEL_NETWORK), EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                     TRACE_LEVEL_INFORMATION, KW_IPV4 | KW_IPV6, 0, 0, None)
        if rc:
            self.stop()
            raise OSError(rc, "EnableTraceEx2: " + ctypes.FormatError(rc).strip())
        lf = self._logfile = EVENT_TRACE_LOGFILEW()
        lf.LoggerName = self.SESSION_NAME
        lf.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
        lf.EventRecordCallback = self._cb
        self.consumer = advapi32.OpenTraceW(ctypes.byref(lf))
        if self.consumer == INVALID_PROCESSTRACE_HANDLE:
            err = ctypes.get_last_error(); self.stop()
            raise OSError(err, "OpenTraceW: " + ctypes.FormatError(err).strip())
        self.thread = threading.Thread(target=self._pump, name="etw-kernel-network", daemon=True)
        self.thread.start()

    def _pump(self):
        self.process_trace_rc = advapi32.ProcessTrace(ctypes.byref(TRACEHANDLE(self.consumer)), 1, None, None)

    def _on_event(self, prec):
        try:
            r = prec.contents
            eid = r.EventHeader.EventDescriptor.Id
            if eid in self.WANTED:
                ev = parse_kernel_network(eid, ctypes.string_at(r.UserData, r.UserDataLength))
                if ev is not None:
                    self.sink(r.EventHeader.TimeStamp, eid, ev)
        except Exception:
            self.callback_errors += 1

    def health(self):
        q = Properties()
        rc = advapi32.ControlTraceW(self.session, None, q.ptr(), EVENT_TRACE_CONTROL_QUERY)
        return dict(rc=rc, events_lost=q.p.EventsLost, rt_buffers_lost=q.p.RealTimeBuffersLost,
                    callback_errors=self.callback_errors,
                    pump_alive=self.thread is not None and self.thread.is_alive(),   # False + rc set = consumer died
                    process_trace_rc=self.process_trace_rc)

    def stop(self):
        if self.session.value:
            advapi32.ControlTraceW(self.session, None, Properties().ptr(), EVENT_TRACE_CONTROL_STOP)
            self.session = TRACEHANDLE(0)
        if self.thread is not None:
            self.thread.join(10)
            self.thread = None
        if self.consumer != INVALID_PROCESSTRACE_HANDLE:
            advapi32.CloseTrace(self.consumer)
            self.consumer = INVALID_PROCESSTRACE_HANDLE
```

Run unelevated, this class takes the documented failure path: orphan cleanup returns 4201, then
`Kernel-Network session unavailable: errno=5 StartTraceW: Access is denied. -> fall back to ICMP/Wi-Fi/counter probes`,
exit 2 [verified: run kn_monitor.py 3]. **(fact-check)** Re-run straight from this document (with the `health()`
fields added in the fact-check): `start()` raised `OSError` errno 5 "StartTraceW: Access is denied.", no
`lag-doctor-net` session was left behind (QUERY → 4201), and on the never-started object `health()` returned
`rc` 87 with `pump_alive=False` and `stop()` was a no-op [verified: run v_etw.py, v_health.py]. **The real-time path
itself (events actually flowing from Kernel-Network) is [unverified] on this machine**; it needs an elevated or
Performance Log Users run.

### 5.6 Negative and positive tests (real output) [verified: run etw_probe.py]

```
== unelevated negative tests ==
  StartTraceW('lag-doctor-probe-rt', REAL_TIME) -> 5 (Access is denied.) handle=0
  StartTraceW with Wnode.BufferSize=sizeof(EVENT_TRACE_PROPERTIES) (no room for name) -> 24 (The program issued a command but the command length is incorrect.)
  ControlTraceW(0, 'lag-doctor-probe-rt', QUERY) -> 4201 (The instance name passed was not recognized as valid by a WMI data provider.)
  ControlTraceW(0, 'lag-doctor-probe-rt', STOP) (orphan cleanup path) -> 4201 (...)
  EnableTraceEx2(TraceHandle=0, Kernel-Network) -> 87 (The parameter is incorrect.)
  QueryAllTracesW -> 0 (success) LoggerCount=1 names(first 12)=['MSDTC_TRACE_SESSION']
  OpenTraceW(real-time 'lag-doctor-probe-rt') -> handle=0x101 GetLastError=0 (success)
   ProcessTrace -> 4201 (...) after 0.003s
   CloseTrace -> 0 (success)
  OpenTraceW(real-time existing 'MSDTC_TRACE_SESSION') -> handle=0x102 GetLastError=0 (success)
   ProcessTrace -> 4201 (...)        <- that session is not a real-time session
   CloseTrace -> 0 (success)

== private in-process session round trip (allowed for standard users since Windows 10 1703) ==
  provider GUID 9c99e80d-f677-5260-8435-5f47e666aed7 EventRegister -> 0 (success)
  StartTraceW('lag-doctor-probe-private', PRIVATE|IN_PROC|SEQUENTIAL) -> 0 (success) handle=0x1000008
  EnableTraceEx2(private session, own provider) -> 0 (success)
  EventEnabled(Id 43) -> 1
  EventWrite x100000 in 0.631s, failed writes=0
  ControlTraceW(QUERY) -> 0 (success) BufferSize=64KB Min=32 Max=64 NumberOfBuffers=32 Free=31 EventsLost=0 BuffersWritten=172 RealTimeBuffersLost=0 LogFileMode=0x20801 FlushTimer=0 ClientContext=1 LoggerThreadId=20832 LoggerName='lag-doctor-probe-private' LogFileName='<SCRATCH>\private-probe.etl'
  ControlTraceW(STOP) -> 0 (success) EventsLost=0 BuffersWritten=173
  EventUnregister -> 0 (success)
  etl size = 11,337,728 bytes
  OpenTraceW(file) -> ok GetLastError=0 (success)
   LogfileHeader: BufferSize=65536 Version=0x501000a ProviderVersion=26200 NumberOfProcessors=16 PointerSize=8 TimerResolution=156250 LogFileMode=0x20801 BuffersWritten=173 EventsLost=0 ReservedFlags(clock)=1 PerfFreq=10000000 BuffersLost=0
   StartTime=2026-11-04 15:55:14.933944 EndTime=2026-11-04 15:55:15.572121 BootTime=2026-10-18 02:47:27.500000 TimeZone.StandardName='@tzres.dll,-212' LoggerName=None
    record: Size=108 HeaderType=0 Flags=0x242 PID=15780 TID=12336 Id=43 v0 Level=4 Opcode=43 Task=11 Keyword=0x10 LoggerId=0 UserDataLength=28 ts=2026-11-04 15:55:14.940314
    parsed: {'pid': 15780, 'size': 1200, 'daddr': '192.168.0.225', 'saddr': '203.0.113.7', 'dport': 50000, 'sport': 27015, 'seqnum': 0, 'connid': 12648430, 'consumed': 28}
  ProcessTrace -> 0 (success) in 2.159s, events=100002 (46,323/s), CloseTrace -> 0 (success)
  own-provider events decoded=100000 payload byte-exact for samples=True parse length mismatches=0

== foreign file: ExplorerStartupLog.etl ==
   LogfileHeader: BufferSize=8192 Version=0x501000a ProviderVersion=26100 NumberOfProcessors=16 PointerSize=8 ... ReservedFlags(clock)=1 PerfFreq=10000000
  ProcessTrace -> 0 (success) in 0.140s, events=11911 (84,998/s), CloseTrace -> 0 (success)
  first event provider=68fdd900-4a3e-11d1-84f4-0000f80464e3 opcode=0 UserDataLength=476 Flags=0x342
  first event payload as TRACE_LOGFILE_HEADER: BufferSize=8192 Version=0x501000a ProviderVersion=26100 NumberOfProcessors=16
```

Reading it:

- **(fact-check)** A fresh script that executes this document's 5.2 module and 5.5 class reproduced every line
  above: 4201 / 4201 / 5 / 24 / 87, `QueryAllTracesW` → only `MSDTC_TRACE_SESSION`, real-time `OpenTraceW` on a
  missing session → handle 0x101 then `ProcessTrace` 4201, private+real-time → 87, and the private round trip
  (requested 4 → `Min=32`, 100,000 events, `EventsLost=0`, `LogFileMode=0x20801`, payloads byte-exact, sequence in
  order, header flags 0x242, `UserDataLength` 28, `Size` 108) [verified: run v_etw.py].
- The required negative test: **unelevated `StartTraceW` → 5**. The length check runs first (24 with a buffer too
  small for the name), so the full-size buffer passed validation [verified: output above].
- Querying or stopping a session that does not exist returns 4201 even unelevated, so orphan cleanup is safe to run
  every time [verified: output above].
- Private sessions cannot be real time: `StartTraceW(PRIVATE|IN_PROC|REAL_TIME)` → 87, matching "Private sessions
  cannot be used with real-time delivery" [verified: run priv_probe.py; logging-mode-constants page @9359c413]. Since
  Windows 10 1703 "low privilege users can now start a private logger session in processes they started"
  [verified: learn.microsoft.com/en-us/windows/win32/etw/configuring-and-starting-a-private-logger-session @9359c413].
  A private session only sees its own process's events, so it is a test harness, not a way to see the kernel provider
  [verified: logging-mode-constants page].
- In the private payload above, `daddr`/`saddr` are what my test wrote, not kernel semantics (see 4.4).
- Header flags 0x242 = `PRIVATE_SESSION` (0x2) | `64_BIT_HEADER` (0x40) | `PROCESSOR_INDEX` (0x200)
  [verified: SDK um\evntcons.h:135-144].

### 5.7 Who can run it

| Account state | Start session / enable provider / consume real time | Source |
|---|---|---|
| This shell: admin user, UAC-filtered token, Medium integrity | **no** (5) | [verified: run etw_probe.py; `whoami /groups` shows `BUILTIN\Administrators` "Group used for deny only", `S-1-16-8192`] |
| Elevated admin | yes | [verified: StartTraceW and EnableTraceEx2 pages] |
| **Performance Log Users** (S-1-5-32-559) | yes, documented: "Only users with administrative privileges, users in the Performance Log Users group, and services running as LocalSystem, LocalService, NetworkService can control event tracing sessions"; same for enabling providers to a cross-process session (EnableTraceEx2) and consuming real time (OpenTraceW) | [verified: StartTraceW @2d357572, EnableTraceEx2 @540c1f2d, OpenTraceW @2d357572, EventAccessControl @fa536415] |
| PLU for **this** provider | Learn says "You can also specify the provider's GUID to restrict who can register the provider and who can enable the provider"; the per-GUID descriptors live under `HKLM\SYSTEM\CurrentControlSet\Control\WMI\Security` (values named by GUID, self-relative security descriptors; `0811c1af-7a07-4a06-82ed-869455cdf713` = `DefaultTraceSecurityGuid` for everything without its own entry), which is unreadable unelevated. **(fact-check)** `EventAccessQuery` (the API that returns these descriptors) also returns 5 unelevated, for the default GUID, Kernel-Network and a random GUID alike | [verified: EventAccessControl page; `reg query` → "Access is denied."; run v_access.py; `DefaultTraceSecurityGuid` named at SDK shared\evntrace.h:105-108]; the registry layout is from a secondary source [verified: geoffchappell.com/notes/windows/etw/security.htm, a 2008 note, not Microsoft]; whether Kernel-Network carries a stricter ACL: **[unverified]** |
| NT Kernel Logger | admins and LocalSystem only (not needed; Kernel-Network is a normal provider) | [verified: StartTraceW page] |

- SID check: `S-1-5-32-559` = `BUILTIN\Performance Log Users`, 0 members here [verified:
  `(New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-559')).Translate([System.Security.Principal.NTAccount])`;
  `Get-LocalGroupMember -SID 'S-1-5-32-559'`].
- Whether UAC filters PLU out of the standard token: the UAC page fetched does not list filtered groups
  [unverified]. Test after adding: log off and on, then in a non-elevated shell `whoami /groups` must show
  `S-1-5-32-559` as "Enabled group", and `kn_monitor.py` must start [inferred].
- Capability check the tool should run at startup. Tested here: elevated False, Administrators False, PLU False; the
  same check returns True for `BUILTIN\Users` (S-1-5-32-545), so it can say yes
  [verified: run priv_probe.py and doc_blocks_check.py; `TokenElevation` = 20 from SDK um\winnt.h:10788-10808]:

```python
import ctypes, ctypes.wintypes as wt
adv = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
adv.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
adv.ConvertStringSidToSidW.restype = wt.BOOL
adv.CheckTokenMembership.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.BOOL)]
adv.CheckTokenMembership.restype = wt.BOOL
adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
adv.OpenProcessToken.restype = wt.BOOL
adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
adv.GetTokenInformation.restype = wt.BOOL
kernel32.GetCurrentProcess.restype = wt.HANDLE      # pseudo-handle -1: must not be truncated to a 32-bit int
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

def is_member(sid_str):                      # CheckTokenMembership(NULL, sid) ignores deny-only groups
    psid = ctypes.c_void_p()
    adv.ConvertStringSidToSidW(sid_str, ctypes.byref(psid))
    try:
        res = wt.BOOL()
        return bool(res.value) if adv.CheckTokenMembership(None, psid, ctypes.byref(res)) else None
    finally:
        kernel32.LocalFree(psid)

def is_elevated():                           # GetTokenInformation(TokenElevation=20) -> DWORD TokenIsElevated
    tok = wt.HANDLE()
    adv.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(tok))   # TOKEN_QUERY
    try:
        val, n = wt.DWORD(), wt.DWORD()
        ok = adv.GetTokenInformation(tok, 20, ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(n))
        return bool(val.value) if ok else None
    finally:
        kernel32.CloseHandle(tok)

etw_ok = is_elevated() or is_member("S-1-5-32-559")
```

---

## 6. Background bandwidth hogs

### 6.1 Detection sources

| Hog | Unelevated signal | Elevated / PLU signal | Source |
|---|---|---|---|
| Any process | — | per-PID rx/tx bytes from Kernel-Network events, 1 s buckets | section 4 |
| Link saturation | `GetIfEntry2` `InOctets`/`OutOctets` deltas at 1 Hz | same | [verified: run net_probe.py] |
| Steam | `C:\Program Files (x86)\Steam\logs\content_log.txt` lines `AppID N state changed : …Update Running,Update Started,`, `App update changed : Running Update,Downloading,Staging,`, `Current download rate: 98.213 Mbps`, `update canceled : Disabled (Suspended)`, `finished update`. Steam path from `HKCU\Software\Valve\Steam` value `SteamPath`. `config\config.vdf`: `"AllowDownloadsDuringGameplay" "0"`, `"DownloadThrottleKbps" "0"`, `"RecentDownloadRate" "12340032"` | process `steam.exe`/`steamwebhelper.exe` bytes | [verified: `Get-ItemProperty 'HKCU:\Software\Valve\Steam'`; `Select-String` on both files] |
| Epic | `%LOCALAPPDATA%\EpicGamesLauncher\Saved\Logs\EpicGamesLauncher.log`: `LogBuildPatchServices:` (1,197 lines) / `LogBPSInstallerConfig:` (49) lines mark installs and updates. **(fact-check, corrected)** The log does contain rate lines, but only as end-of-install summaries: `LogBuildPatchServices: Build Stat: AverageDownloadSpeed: 10,186,932.327 bytes (10.186 MB, 9.715 MiB) /sec` and `… PeakDownloadSpeed: …`. No live rate line was found | process `EpicGamesLauncher.exe` bytes | [verified: `Select-String` on the log, re-run] |
| Battle.net | `%ProgramData%\Battle.net` exists (`Agent\`, `Setup\`, `Uninstaller\`); `Agent\Logs` holds only 85-byte `Switcher-*.log` files where checked, so no download-rate format could be checked | process `Agent.exe`/`Battle.net.exe` bytes | [verified: `Test-Path`, `Get-ChildItem`]; format [unverified] |
| Windows Update / Store (Delivery Optimization) | `Get-DeliveryOptimizationPerfSnap` works unelevated: `BackgroundDownloadCount`, `ForegroundDownloadCount`, `…DownloadsPending`, `DownlinkUsageBps`, `CdnConnections` (66 here); `Get-DeliveryOptimizationStatus` lists files (18 here, all `Status` "Caching" in the re-run). **(fact-check)** Documented meanings: `DownlinkBps` = "Average download bandwidth usage currently seen across all network adapters", `DownlinkUsageBps` = "Average bandwidth currently used by Delivery Optimization for downloads", `BackgroundDownloadRatePct` = "Percentage of bandwidth to be used for background downloads" (45 here) | svchost hosting DoSvc, bytes per PID | [verified: both cmdlets run unelevated; learn.microsoft.com/en-us/windows/deployment/do/waas-delivery-optimization-monitor @49145d39]; field units (bits or bytes per second) [unverified] |
| OneDrive, browsers | process names (`OneDrive.exe`, `chrome.exe`, `msedge.exe`, `firefox.exe`) + TCP connection counts from `psutil.net_connections` | bytes per PID | [verified: psutil gives PIDs]; byte attribution needs ETW |

The Steam log shows Steam itself suspends downloads during play with this config: `update canceled : Disabled
(Suspended)` while a game ran [verified: content_log.txt lines]. **(fact-check)** The clearest
instance: `[<date> <time>] AppID <game> state changed : Fully Installed,App Running,` followed in the same
second by `AppID <another game> update canceled : Disabled (Suspended)`. The `…,App Running,` state line also tells the
tool which Steam AppID is running. Over the whole log the suspend reasons were `Disabled` ×15, `Shader Priority`
×3, `Priority` ×2 [verified: `Select-String` with context on content_log.txt].

### 6.2 What the tool can safely do

| Action | Type | Notes |
|---|---|---|
| Name the hog with numbers ("Steam is downloading at 98 Mbit/s, ping to 1.1.1.1 rose 80 ms") | advice | always allowed [inferred] |
| Open the right settings page: `ms-settings:delivery-optimization-advanced` (background bandwidth limits), `ms-settings:windowsupdate`, `ms-settings:network-wifi`, `ms-settings:privacy-location` | advice, the user acts | [verified: learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings @50281bfb; the original `…/launch-settings-app` URL redirects there; all four URIs present] |
| Steam: point to Settings → Downloads ("allow downloads during gameplay", download limit) | advice | config keys verified above; do not edit `config.vdf` while Steam runs (Steam rewrites it) [inferred] |
| Throttle an **upload** hog with `New-NetQosPolicy -Name … -AppPathNameMatchCondition app.exe -ThrottleRateActionBitsPerSecond … -PolicyStore ActiveStore`; undo with `Remove-NetQosPolicy` | consented fix, elevated, temporary: "If a policy is stored in ActiveStore, then the policy will not persist after reboot" | [verified: learn.microsoft.com/en-us/powershell/module/netqos/new-netqospolicy @0ef3f225]. **(fact-check)** Outbound only: policy-based QoS lets you "prioritize or manage the sending rate for outbound network traffic"; inbound needs the separate advanced setting for "inbound TCP traffic" [verified: learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2008-R2-and-2008/dd759093(v=ws.11) @0d8c9875, an older-version page]. That it needs admin: [unverified] |
| Disable Wi-Fi background scan / enable media streaming mode for the session (3.5) | consented fix, reverts on disconnect; **(fact-check)** the DACL grants standard users write access (3.5), so no elevation should be needed | [verified: doc semantics; `WlanGetSecuritySettings` granted 0x70023]; the call itself untested |
| Suspend or kill other processes, edit other apps' config files, change Windows Update policy | **don't** | risk of corrupting downloads and of hidden state changes [inferred] |

---

## 7. Where network lag comes from: decision table

Measure at the same instants: **G** = gateway RTT (ICMP, 2 Hz); **A** = internet anchor RTT (1.1.1.1, 1 Hz; optional
ISP hop 2 found with TTL = 2); **F** = game flow health (ETW: gaps, jitter, loss, tx pauses); **W** = Wi-Fi (RSSI,
quality, rates, retries/s, scan notifications, channel); **U** = link utilisation; **H** = hog list; **P** = PresentMon
frame times. All rows are [inferred] from the verified mechanisms in sections 1-6.

| # | Pattern | Most likely cause | Confidence | What to tell the user |
|---|---|---|---|---|
| 1 | G and A spike together in the window [-3 s, +1 s] around `scan_complete` (the scan runs before its completion notice; same window as 3.5, fact-check), roughly every 60 s; W otherwise healthy | Wi-Fi background scan (radio off-channel) | high when ≥ 3 aligned spikes | consented fix in 3.5, or Ethernet |
| 2 | G high or variable (p95 > 30 ms), W shows RSSI < -70 dBm, rising retries/s, falling rates | weak signal or interference on Wi-Fi | high | move closer, use 5 GHz, Ethernet |
| 3 | G flat, A rises ≥ 30 ms over the idle baseline while U ≥ 70-80 % of capacity, H names a download/upload | bufferbloat at the router/modem WAN queue | high | pause or limit the hog; enable SQM/QoS on the router |
| 4 | G and A rise together while U is high, Wi-Fi metrics fine | queue in the AP/router LAN side, or airtime shared with other devices | medium | as row 3, and check other devices on the network |
| 5 | G flat, A flat, F shows gaps/loss or high server RTT | game server or its route (overloaded server, bad peering, distant region, SDR relay) | medium | change region/server; nothing local to fix |
| 6 | A high all the time (not tied to U), ISP hop 2 also high, worse at evening peak | ISP access network congestion | medium | time-of-day evidence; contact the ISP |
| 7 | F gap where the client's own tx also stops, P shows a frame-time spike at the same moment | local hitch (CPU/GPU/driver stall), not the network | high | hand over to the FPS/stutter analysis |
| 8 | best route egresses a VPN/tunnel interface (`GetIfEntry2` type/description) | VPN adds a path and a hop | high | split-tunnel the game or disconnect |
| 9 | G loses probes but A and F are clean | router deprioritises ICMP to itself (control plane) | medium | ignore G loss; trust A and F |
| 10 | G and A clean but the game's ping is high | ICMP treated differently from UDP (PlayFab note), or the server is far or overloaded | medium | rely on F; ICMP cannot see it |
| 11 | F shows gaps followed by bursts, G spikes at the same times | packets delayed then flushed on the local hop (Wi-Fi retries or power save) | medium | as rows 1-2 |

Without ETW (unelevated), rows 5, 7, 10 and 11 cannot be told apart; say so instead of guessing [inferred].

---

## 8. Recommended network monitor design

### 8.1 Components and rates [inferred, built on verified pieces]

| Probe | Rate | Cost | Privilege |
|---|---|---|---|
| ICMP to gateway (`GetBestRoute2` next hop), 32-byte payload, 1000 ms timeout, own thread, `wall` timing | 2 Hz | about 1 kbit/s | none |
| ICMP to anchor 1.1.1.1 (configurable; 1-2 anchors) | 1 Hz | about 0.5 kbit/s | none |
| TTL-limited hop discovery (TTL 1-4 toward the anchor) | at start and every 5 min | 4 packets | none |
| Route/interface check (`GetBestRoute2` to the anchor and to the game server once known; VPN detection) | every 5 s | negligible | none |
| `GetIfEntry2` counters on the egress interface | 1 Hz | negligible | none |
| Wi-Fi: rssi (0x10000102), realtime quality (19), channel (8), statistics (0x10000101) | 1 Hz | negligible | none |
| Wi-Fi ACM notifications (scan_complete/fail, connection_complete, disconnected) | event-driven | none | none |
| WLAN-AutoConfig event log (8001/8003/11000/11001) | poll every 10 s | small | none |
| Hogs: process list + `psutil.net_connections` | every 5 s | small | none |
| Steam `content_log.txt` tail | every 2 s | small | none |
| `Get-DeliveryOptimizationPerfSnap` via PowerShell | every 30 s | one process spawn | none |
| Kernel-Network ETW: game flow table, per-PID bytes, TCP retransmits | continuous, 1 s aggregation | 14-22 µs of Python per delivered event: about 2 % of a core at 1,000 events/s, 14-22 % at 10,000 (a background download can cause that; see 5.4) | admin or Performance Log Users |
| ETW health (`EventsLost`, `RealTimeBuffersLost`, `pump_alive`, `process_trace_rc`) | every 10 s | negligible | same |

Windows: 15 s for loss/jitter/percentiles (Teams-style), 200 ms for burst loss, 120 s rolling minimum for the PDV
baseline. Thresholds: the table in 2.2 and the gap rules in 4.6. Session buffers **(fact-check)**: 64 KB,
`FlushTimer` 1 s, default buffer counts, switching to `MinimumBuffers = MaximumBuffers = 64` if events are lost (5.3).

### 8.2 Privilege tiers and fallbacks

1. **Unelevated (default):** ICMP, routes, interface counters, Wi-Fi without location, ACM scan notifications, the
   event log, Steam/Epic logs, Delivery Optimization cmdlets, TCP peers per process. It localises Wi-Fi vs
   router/bufferbloat vs ISP (rows 1-4, 6, 8, 9). It cannot see the game's UDP stream: report "game path not
   measured" rather than infer it. The consented Wi-Fi background-scan fix also belongs to this tier: the DACL
   grants standard users write access (3.5, fact-check), though the call itself is untested.
2. **Performance Log Users (one-time admin action by the user, then log off/on):** adds Kernel-Network ETW without UAC
   prompts in later runs. Documented to work; untested for this provider (5.7).
3. **Elevated run:** same as 2 for that run only.
4. **Location consent granted:** adds BSSID/SSID roaming detection and MSM notifications (3.6). Optional; the tool
   should explain why before asking.
5. Any tier that is missing announces itself in the report ("Wi-Fi roaming not measured: location permission off")
   [inferred].

### 8.3 Self-tests to ship with the implementation [inferred]

- Struct-size asserts (the sizes in 1.1, 1.5, 3.3 and 5.1) at import.
- An ICMP probe to `127.0.0.1` at startup must return `Status == 0`, and the reply's `Data` pointer must equal the
  reply buffer's address + 40 with the payload bytes there (the check run in 1.3).
- A private in-process ETW round trip (5.6) in the test suite: it runs unelevated and proves the consumer path.
- When ETW is available, the one-shot self-test in 4.4 (one UDP DNS query and one TCP connect) settles
  `daddr`/`saddr` semantics for UDP and TCP separately before the flow table trusts them; if it fails, mark
  game-flow results "unverified layout".
- An escaping-exception guard: a unit test that makes the parser raise and asserts `callback_errors` counts it
  (an escaped exception would not stop the trace, only print, 5.4).
- Orphan-session cleanup at every start, and a test that kills the process mid-session, then checks the next start
  stops `lag-doctor-net`.

---

## 9. Open questions and unverified items

1. Kernel-Network payload semantics for receive events (`daddr` vs `saddr`): needs one elevated run (4.4).
   Fact-check: System Informer's parser suggests UDP receive is packet-oriented and TCP connection-oriented, so the
   self-test must cover both.
2. Whether a Performance Log Users member can enable this kernel provider (per-GUID ACL unreadable unelevated, by
   registry and by `EventAccessQuery`) and whether UAC leaves PLU enabled in the standard token (5.7).
3. Whether the 60 s background scan actually raises RTT on this laptop: needs continuous gateway pings during a scan
   (3.5); the rule for both passes allowed only a handful. The driver counters showed nothing around two scans.
4. Whether `WlanSetInterface(background_scan_enabled = FALSE)` actually succeeds and reverts when the handle closes
   (3.5). Fact-check: the permission side is settled (Users have `WLAN_WRITE_ACCESS`); the call itself is untried.
5. Whether `EVENT_FILTER_TYPE_EVENT_ID` is honoured by Kernel-Network (4.5); it matters for CPU cost (5.4).
6. ~~Game-specific perceptibility thresholds from primary sources~~: fact-check found the Claypools' 2006 WPI report
   (100 / 500 / 1000 ms by game class, 2.2). Game-specific jitter and loss thresholds still have no primary source.
7. Whether any anti-cheat reacts to a system-wide Kernel-Network session: no source found [unverified].
8. (fact-check) Whether `New-NetQosPolicy` needs admin, and the units of the Delivery Optimization `*Bps` fields.

## Sources

Microsoft Learn (commit ids as shown on each page, fetched 2026-11-04):

- IcmpSendEcho2 — learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmpsendecho2 (9267262487657894a8af112d7165006fed5035a7)
- ICMP_ECHO_REPLY — learn.microsoft.com/en-us/windows/win32/api/ipexport/ns-ipexport-icmp_echo_reply (9267262487657894a8af112d7165006fed5035a7)
- Icmp6SendEcho2 — learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmp6sendecho2 (c2c2af312729d44b4c368831a1052a5e9e3a7a9f)
- StartTraceW — learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-starttracew (2d357572ac8c44db7c750edaf43bab4e1ca1a3ab)
- EnableTraceEx2 — learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-enabletraceex2 (540c1f2da94f9934d5d7e4321d4270e02fe917ec)
- OpenTraceW — learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-opentracew (2d357572ac8c44db7c750edaf43bab4e1ca1a3ab)
- ProcessTrace — learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-processtrace (540c1f2da94f9934d5d7e4321d4270e02fe917ec)
- CloseTrace — learn.microsoft.com/en-us/windows/win32/api/evntrace/nf-evntrace-closetrace (540c1f2da94f9934d5d7e4321d4270e02fe917ec)
- EVENT_TRACE_PROPERTIES — learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-event_trace_properties (9267262487657894a8af112d7165006fed5035a7)
- WNODE_HEADER — learn.microsoft.com/en-us/windows/win32/etw/wnode-header (0d36b58c25ba49ae0963a6a6a475d497c5d93895)
- Logging Mode Constants — learn.microsoft.com/en-us/windows/win32/etw/logging-mode-constants (9359c413c0871211bf1198df4c9955f3cf547e23)
- Private logger session — learn.microsoft.com/en-us/windows/win32/etw/configuring-and-starting-a-private-logger-session (9359c413c0871211bf1198df4c9955f3cf547e23)
- EventAccessControl — learn.microsoft.com/en-us/windows/win32/api/evntcons/nf-evntcons-eventaccesscontrol (fa53641576e3603fa7b66d3a4ad969d3ce49d6f3)
- EVENT_FILTER_DESCRIPTOR — learn.microsoft.com/en-us/windows/win32/api/evntprov/ns-evntprov-event_filter_descriptor (9267262487657894a8af112d7165006fed5035a7)
- UdpIp_TypeGroup1 / TcpIp_TypeGroup1 — learn.microsoft.com/en-us/windows/win32/etw/udpip-typegroup1, …/tcpip-typegroup1 (4d5f26c6be39ae31f663357fe190f3868a37f9be)
- Wi-Fi access and location — learn.microsoft.com/en-us/windows/win32/nativewifi/wi-fi-access-location-changes (4b1605e37433015369a0de737a0d4565202db75c)
- WLAN_INTF_OPCODE — learn.microsoft.com/en-us/windows/win32/api/wlanapi/ne-wlanapi-wlan_intf_opcode (a315d8e18e82a977e0cbbef37649b1d35f1785d9)
- WLAN_REALTIME_CONNECTION_QUALITY — learn.microsoft.com/en-us/windows/win32/api/wlanapi/ns-wlanapi-wlan_realtime_connection_quality (a315d8e18e82a977e0cbbef37649b1d35f1785d9); _LINK_INFO (477e1c1d252d1ae995e1b4b32f0ee281620b9937)
- WlanQueryInterface — learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlanqueryinterface (d784338449a2ee4436e4d5e68dc95a1656d677be)
- WlanSetInterface — learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlansetinterface (2f7164c7babdafcf7fbe4acf0e3bc1c2a298d6e1)
- Teams network requirements — learn.microsoft.com/en-us/training/modules/plan-optimize-network-performance-for-teams-phone-system/2-understand-teams-network-requirements (3314c77240a37194ea26902e2abedd43acf99aa1)
- PlayFab QoS beacons — learn.microsoft.com/en-us/xbox/playfab/multiplayer/servers/using-qos-beacons-to-measure-player-latency-to-azure (4ffe64e55bc587ca184dc0b51cb0ec0a95852d0f; the `/gaming/playfab/` path redirects here)
- ms-settings URIs — learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings (50281bfb3f838698752622b58ee53fbc24baafc3; `…/launch-settings-app` redirects here)
- New-NetQosPolicy — learn.microsoft.com/en-us/powershell/module/netqos/new-netqospolicy (0ef3f225d29e26d1cf3119f37dfff70bb6165746; served as `?view=windowsserver2025-ps`)

Added by the fact-check (fetched 2026-11-04):

- InputType / OutputType (manifest `win:Port`, `win:IPv4` byte order) — learn.microsoft.com/en-us/windows/win32/wes/eventmanifestschema-inputtype-complextype (c4c9c064c93fab40ba6c5269575348d89fb5b4a0), …/eventmanifestschema-outputtype-complextype (1ba36e367314861c0bebe07f2239e282cb3245b0)
- EventAccessQuery — learn.microsoft.com/en-us/windows/win32/api/evntcons/nf-evntcons-eventaccessquery (fa53641576e3603fa7b66d3a4ad969d3ce49d6f3)
- MIB_UDPROW_OWNER_PID — learn.microsoft.com/en-us/windows/win32/api/udpmib/ns-udpmib-mib_udprow_owner_pid (9267262487657894a8af112d7165006fed5035a7)
- WLAN_SECURABLE_OBJECT, WlanGetSecuritySettings — learn.microsoft.com/en-us/windows/win32/api/wlanapi/ne-wlanapi-wlan_securable_object, …/nf-wlanapi-wlangetsecuritysettings (9267262487657894a8af112d7165006fed5035a7)
- WlanRegisterNotification — learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlanregisternotification (9267262487657894a8af112d7165006fed5035a7)
- Delivery Optimization monitoring (PerfSnap field meanings) — learn.microsoft.com/en-us/windows/deployment/do/waas-delivery-optimization-monitor (49145d39e1438d140a1e6a0fed5721ba9fb6ae2f)
- Policy-based QoS overview (outbound only) — learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-server-2008-R2-and-2008/dd759093(v=ws.11) (0d8c9875db82577b0a84277ba0a7886a42b02f60)

Other:

- RFC 3550 §6.4.1, RFC 5481, RFC 8289 §4.2-4.3, RFC 1812 §4.3.2.8 — www.rfc-editor.org/rfc/rfc{3550,5481,8289,1812}.txt
- docs.python.org/3.13/library/ctypes.html (WinDLL GIL note, callback lifetime)
- PerfView TraceEvent — raw.githubusercontent.com/microsoft/perfview/3d89ee8570a943290c4b517663f87493cfbaa35b/src/TraceEvent/Parsers/KernelTraceEventParser.cs (latest commit touching the file, 2026-10-07; the original cited branch `main`)
- System Informer (fact-check) — raw.githubusercontent.com/winsiderss/systeminformer/0a4975a4763ad202747397ef5daf5893849b5b00/plugins/ExtendedTools/etwmon.c and …/phnt/include/ntwmi.h (master at fetch time)
- Claypool & Claypool, "On Latency and Player Actions in Online Games", July 8, 2006 (fact-check) — ftp.cs.wpi.edu/pub/techreports/pdf/06-13.pdf
- Geoff Chappell, "Event Trace Security" (2008 note, secondary; registry layout of ETW security descriptors) — geoffchappell.com/notes/windows/etw/security.htm
- Riot Games, "Peeking into VALORANT's Netcode" — www.riotgames.com/en/news/peeking-valorants-netcode (the 2020-07-28 date given originally is [unverified]: the fetched page shows no date for the article)
- NVIDIA GeForce NOW requirements — www.nvidia.com/en-us/geforce-now/system-reqs/
- Steamworks, Steam Datagram Relay — partner.steamgames.com/doc/features/multiplayer/steamdatagramrelay
- bufferbloat.net — www.bufferbloat.net/projects/bloat/wiki/Introduction/
- Velociraptor `Windows.ETW.KernelNetwork` artifact (handles events 10, 11, 12, 15, 42, 43) — docs.velociraptor.app/artifact_references/pages/windows.etw.kernelnetwork/
- Intel Community thread on 60 s ping spikes (anecdotal) — community.intel.com/t/frequent-ping-spikes/20961 (the original `community.intel.com/t5/Wireless/Frequent-ping-spikes/td-p/426779` redirects here)

Local (this machine): SDK headers under `C:\Program Files (x86)\Windows Kits\10\Include\10.0.19041.0\`;
`wevtutil gp Microsoft-Windows-Kernel-Network /ge /gm:true /f:xml`; `Get-WinEvent -ListProvider Microsoft-Windows-Kernel-Network`;
`logman query providers "Microsoft-Windows-Kernel-Network"`; `netsh wlan show interfaces`; scratch scripts
`net_probe.py`, `net_probe2.py`, `net_probe3.py`, `net_probe4.py`, `icmp6_probe.py`, `wlan_probe.py`, `etw_ctypes.py`,
`etw_probe.py`, `priv_probe.py`, `kn_monitor.py`, `iocounters_probe.py`, `doc_blocks_check.py` in `<SCRATCH>`.
Fact-check scripts in `<SCRATCH>-verify`: `fetch_text.py`, `v_net.py`, `v_route.py`, `v_timeout.py`, `v_blocks.py`,
`v_blocks2.py`, `v_blocks3.py`, `v_etw.py`, `v_health.py`, `v_access.py`, `v_psutil.py`, `v_io.py`, `v_wlan.py`,
`v_wlanphy.py`, `v_wlanthread.py`, `v_sid.py`, `pdf_text.py`.

---

## Verification log

Second-pass fact-check, 2026-11-04, same machine, unelevated (Medium integrity, Administrators deny-only, not in
Performance Log Users). Method: every Learn page re-fetched with its `git_commit_id` read from the page; RFCs from
rfc-editor.org; GitHub sources pinned to a commit; SDK 10.0.19041.0 headers re-read at the cited lines; every probe
re-run from new scripts in `<SCRATCH>-verify`, and the doc's own code blocks executed from this markdown.

Footprint: 14 ICMP echo requests left the PC (gateway ×3, 1.1.1.1 ×6 including one TTL=1 probe that stopped at the
gateway, 192.0.2.1 ×1, 10.99.0.77 ×4, which turned out to be routed to the gateway); the rest were loopback.
Two private in-process ETW sessions (stopped, `.etl` files in the scratch folder); real-time `StartTraceW` attempts
all failed (5, 24) as intended. WLAN calls were read-only (queries, `WlanGetSecuritySettings`, ACM notification
listens of 135 s and 70 s); no location-gated call was made. No settings, registry values, services, priorities or
group memberships were changed; nothing was installed. The only repo file edited is this document.

Verdicts: **confirmed** = re-checked and holds; **corrected** = old → new; **unverifiable** = could not be settled
here (tagged [unverified] in the text).

### 1. ICMP and routes

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `IcmpCreateFile`/`IcmpSendEcho2`/`Icmp6CreateFile`/`Icmp6SendEcho2` signatures at icmpapi.h:79-81, 287-306, 110, 349-366 | confirmed | SDK lines re-read |
| 2 | `ReplySize` ≥ `sizeof(ICMP_ECHO_REPLY) + RequestSize + 8` | confirmed | SDK `_In_range_` at icmpapi.h:303; Learn: "plus an additional 8 bytes of data (the size of an ICMP error message)" @92672624 |
| 3 | Synchronous when `Event` and `ApcRoutine` are NULL; return = replies stored, 0 → `GetLastError` | confirmed | Learn remarks and return-value text @92672624 |
| 4 | `IP_OPTION_INFORMATION`, `ICMP_ECHO_REPLY` layouts (IPExport.h:53-60, 79-88); `IPV6_ADDRESS_EX` packed (packon.h) | confirmed | SDK lines; `#include <packon.h>` at IPExport.h:105 |
| 5 | x64 sizes: `ICMP_ECHO_REPLY` 40 (`Data` @16, `Options` @24), `IP_OPTION_INFORMATION` 16, `IPV6_ADDRESS_EX` 26, `ICMPV6_ECHO_REPLY` 36 (`Status` @28, RTT @32) | confirmed | v_net.py `ctypes.sizeof`/offsets |
| 6 | Reply data sits at buffer+40 on x64; Learn's "On a 64-bit platform, the ICMP_ECHO_REPLY32 structure should be used" is wrong for a 64-bit process | confirmed | Learn text @92672624 re-read; v_net.py: `data_off=40`, payload equal, bytes at +28 (the ECHO_REPLY32 position) are not the payload, on loopback, gateway and 1.1.1.1 |
| 7 | IPv6 echo data follows byte 36; replying address = bytes 6-22 of `Address` | confirmed | v_net.py ping6 `::1`: status 0, `frm=::1`, `echo_after_36=True` |
| 8 | IP_STATUS codes 0, 11001-11003, 11010, 11013, 11050 and the IPv6-terminology aliases | confirmed | IPExport.h:169-237 |
| 9 | `GetIpErrorString`: size in characters, buffer needs size+1; returned strings | confirmed | iphlpapi.h:1136-1140 (`_Out_writes_opt_(*Size + 1)`); Learn "length, in characters … excluding the terminating null" @92672624; v_net.py strings (plus 11002 → "No route to destination.") |
| 10 | Reply count 1 for ICMP errors: TTL expired 11013 from the gateway, net unreachable 11002 from ISP hop 2 | confirmed | v_net.py: TTL=1 → n=1, status 11013, addr 192.168.0.1; 192.0.2.1 → n=1, status 11002 from hop 2 |
| 11 | Gateway RTT 1-4 ms, 1.1.1.1 RTT 5-7 ms, reply TTL 64 / 59 | confirmed | v_net.py: gateway rtt 1-2 (wall 1.9-3.1 ms), 1.1.1.1 rtt 6-7 (wall 6.7-7.5 ms), TTL 64 / 59 |
| 12 | `Timeout` is not a deadline when a reply arrives (1 ms → 7.6 ms reply) | confirmed | v_net.py: Timeout 1 ms → status 0, 5.6 ms |
| 13 | No reply: ~500 ms for every Timeout 20-1000 ms; packets "never left the PC"; ARP explanation | **corrected** | old: fixed ~500 ms, empty virtual-adapter subnet → new: on re-run the address routed via the Wi-Fi gateway (`GetBestRoute2`, `Find-NetRoute`; neighbour `Incomplete`), waits 210.6 / 369.7 / 486.5 / 1003.9 ms for Timeout 20 / 100 / 500 / 1000; ARP explanation unproven. v_net.py, v_timeout.py, v_route.py |
| 14 | Tiny reply buffer → n=0, `GetLastError` 87 | confirmed | v_net.py: 8-byte buffer → 87. Added: a 40-byte buffer → 11050 |
| 15 | RFC 1812 §4.3.2.8 quote on rate-limiting ICMP errors | confirmed | rfc1812.txt lines 3087-3093 |
| 16 | `ping.exe` default payload 32 bytes | confirmed | `ping -n 1 127.0.0.1` → "with 32 bytes of data" |
| 17 | ctypes releases the GIL for `WinDLL` calls | confirmed | docs.python.org/3.13 ctypes: "The Python global interpreter lock is released before calling any function exported by these libraries" |
| 18 | IPv6 to 2606:4700:4700::1111 → n=0, err 1231 in ~0.1 ms; `GetBestRoute2` IPv6 → 1231; 1231 = `ERROR_NETWORK_UNREACHABLE` at winerror.h:7548 | confirmed | v_net.py (0.08 ms); SDK line |
| 19 | Icmp6 reply buffer also needs room for an `IO_STATUS_BLOCK` | confirmed | Learn Icmp6SendEcho2 @c2c2af31 |
| 20 | `GetBestRoute2(1.1.1.1)` → ifIndex 8, next hop 192.168.0.1, source 192.168.0.225; declaration at netioapi.h:1498-1506 | confirmed | v_net.py, v_blocks.py (doc block); SDK lines |
| 21 | `GetIpForwardTable2` 58 rows; /0 and /1 rows | confirmed | v_net.py: 58 rows (35 IPv4, 23 IPv6), one /0 (IPv4 via 192.168.0.1, if 8), no /1 rows now |
| 22 | `IF_TYPE_IEEE80211` 71, Ethernet 6, `PhysicalMediumType` 9 = Native 802.11, 14 = 802.3 | confirmed | ipifcons.h:49, 114; ntddndis.h `NDIS_PHYSICAL_MEDIUM` enum order; v_net.py type 71, phys 9 |
| 23 | `GAA_FLAG_INCLUDE_GATEWAYS` = 0x0080 | confirmed | IPTypes.h:409 |
| 24 | Sizes `SOCKADDR_INET` 28, `IP_ADDRESS_PREFIX` 32, `MIB_IPFORWARD_ROW2` 104, `MIB_IF_ROW2` 1352 and its six offsets | confirmed | v_net.py, v_blocks2.py (full `MIB_IF_ROW2` now in the doc) |
| 25 | Tunnel adapters can be present while disconnected | confirmed | `Get-NetAdapter -IncludeHidden` |
| 26 | Doc code blocks 1.2 (ping recipe), 1.4 (IPv6 structs), 1.5 (routes) run as written | confirmed | v_blocks.py: one echo to 1.1.1.1 status 0 rtt 6; v_blocks2.py for the extended 1.5 block |

### 2. Quality metrics and thresholds

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 27 | RFC 3550 D(i,j) and J(i) formulas; "gives a good noise reduction ratio while maintaining a reasonable rate of convergence" | confirmed | rfc3550.txt lines 2199-2216 |
| 28 | RFC 5481 IPDV(i) = D(i)-D(i-1), PDV(i) = D(i)-D(min); 99.9th percentile of PDV relates to de-jitter buffer sizing | confirmed | rfc5481.txt lines 575, 623, 1333-1338 |
| 29 | Teams full path: one-way < 50 ms, RTT < 100 ms, burst loss < 10 % / 200 ms, loss < 1 % / 15 s, jitter < 30 ms / 15 s, reorder < 0.05 % | confirmed | Teams training page @3314c772 |
| 30 | Teams edge → Microsoft: < 30 ms, < 60 ms, < 1 %, < 0.1 %, < 15 ms, < 0.01 % | confirmed | same page |
| 31 | Riot: "128 server tickrate"; "35ms ping to 70% of our player base" | confirmed | riotgames.com page HTML re-fetched |
| 32 | Riot article date 2020-07-28 | unverifiable | fetched page shows no date for the article |
| 33 | GeForce NOW "less than 80ms of network latency from an NVIDIA data center" | confirmed | nvidia.com system-reqs page |
| 34 | CoDel TARGET 5 ms, INTERVAL 100 ms | confirmed; wording **corrected** | rfc8289.txt lines 769-778: old "acceptable standing queue delay" → new "the maximum acceptable persistent queue delay" |
| 35 | Claypool 2006: primary unreachable, ~100 ms FPS per secondary summary | **corrected** | old: [unverified] → new: WPI report 06-13 (July 8, 2006) Table 1: 100 / 500 / 1000 ms for first-person / third-person / omnipresent; text extracted with pdf_text.py from ftp.cs.wpi.edu/pub/techreports/pdf/06-13.pdf. Whether it equals the CACM text: unverifiable |
| 36 | Bufferbloat definition quote | confirmed | bufferbloat.net Introduction page |
| 37 | `RecentDownloadRate` 12340032 ≈ 98.7 Mbit/s matching "Current download rate: 98.213 Mbps" | confirmed; use **corrected** | values re-read; but the log has 643 rate lines peaking at 266.312 Mbps, so it is a lower bound, not capacity |
| 38 | PlayFab beacons on UDP port 3075; "differentiated performance for UDP vs. TCP vs. ICMP flows" | confirmed | page @4ffe64e5; URL **corrected** to the `/xbox/playfab/` path it redirects to |
| 39 | SDR: "IP addresses are never revealed" | confirmed | partner.steamgames.com SDR page |
| 40 | microsoft.com, github.com, store.steampowered.com answer ICMP | unverifiable | not re-run (ping budget); original run only |

### 3. Wi-Fi

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 41 | Location doc: the four `wlanapi.h` calls return `ERROR_ACCESS_DENIED` without consent; one-time prompt condition; MSM needs `wiFiControl` | confirmed | wi-fi-access-location-changes @4b1605e3. Added: calls show the location-in-use tray icon; throttle them |
| 42 | `ConsentStore\location` HKLM Deny, HKCU Deny, `NonPackaged` Allow | confirmed | `Get-ItemProperty` on the three keys |
| 43 | `netsh wlan show interfaces` location message and "WlanQueryInterface returns error 5" | confirmed | re-run; added: netsh misattributes error 5 to elevation |
| 44 | Opcode values 1-8, 12, 0x10000101, 0x10000102 from SDK; 19 from Learn's newer enum (SDK 19041 lacks 17-20) | confirmed | wlanapi.h:975-1000 (stops at 16, then the end markers); Learn enum @a315d8e1 lists 17-20 |
| 45 | Query results: autoconf 1, background scan 1, streaming 0, radio_state 772 bytes, bss_type 3, state 1, channel 36, op mode 4, opcode 19 296 bytes, statistics 1088 bytes, rssi -53 | confirmed | v_wlan.py (same values); added `qos_info` (20): rc 0, 52 bytes |
| 46 | Opcode 19 "omits location-sensitive information … doesn't require location access privileges" | confirmed | Learn @a315d8e1; added: "It will fail if the interface isn't connected" |
| 47 | `WLAN_REALTIME_CONNECTION_QUALITY` 296 (linksInfo @24), `…_LINK_INFO` 272, `WLAN_RATE_SET` 126 rates | confirmed | v_wlan.py sizes; Learn layouts @a315d8e1/@477e1c1d; windot11.h:95 |
| 48 | Learn says the frequency is "in units of kilohertz (kHz)"; it is MHz (5180 on channel 36) | confirmed | Learn LINK_INFO text re-read; opcode 19 freq 5180 with opcode 8 channel 36 |
| 49 | Rx/Tx rates are kbit/s | confirmed | `GetIfEntry2` 173,300,000 / 433,300,000 bit/s; added SDK comment wlanapi.h:582-583 "The unit for Rx/Tx rate is Kbits/second" (for the older struct) |
| 50 | `ulLinkQuality` mapping (0 = -100 dBm, 100 = -50 dBm, linear) and measured 86 at -53 dBm | confirmed | Learn text; v_wlan.py 86 at -53 throughout 135 s |
| 51 | `dot11PhyType` 8 = vht (802.11ac), 10 = he; event 8001 says 802.11ac | confirmed | windot11.h:79-91; latest 8001 event `PHY Type: 802.11ac` |
| 52 | `WLAN_STATISTICS` 368, `PhyCounters` @224, 144 per PHY, 6 identical PHY entries | confirmed | wlanapi.h:1041-1096; v_wlanphy.py: 1088 = 224 + 6 × 144, all blocks identical |
| 53 | ACM source 0x8, MSM 0x10 | confirmed; naming **refined** | wlanapi.h:868-869 define `WLAN_NOTIFICATION_SOURCE_ACM/MSM` as `L2_NOTIFICATION_SOURCE_WLAN_ACM/MSM` (l2cmn.h:48-49) |
| 54 | ACM codes 7 / 8 / 10 / 21; MSM roaming 5 / 6; `L2_NOTIFICATION_DATA` 40 bytes | confirmed | wlanapi.h:880-926; l2cmn.h:59, 91-100; v_wlan.py size 40. Added: ACM 3 / 4 = background scan enabled / disabled |
| 55 | `scan_complete` exactly every 60.0 s while connected | confirmed | v_wlan.py 135: t = 28.1, 88.1 s; v_wlanthread.py: t = 52.8 s |
| 56 | Scans cause ping spikes (forum report) | unverifiable | thread re-read (anecdotal, "every 60 seconds"); URL **corrected** to its redirect target; no continuous pings allowed |
| 57 | Background-scan and media-streaming semantics (connected only; one client suffices; auto re-enable on disconnect) | confirmed | Learn opcode page @a315d8e1 |
| 58 | Whether `WlanSetInterface` needs admin or location consent | **corrected** (permission side) | old: [unverified] → new: DACL of `wlan_secure_bc_scan_enabled` grants `BU` write; `WlanGetSecuritySettings` granted 0x70023 (`WLAN_WRITE_ACCESS`, wlanapi.h:1103-1105); not in the location-gated list. v_wlan.py; WLAN_SECURABLE_OBJECT @92672624 |
| 59 | Setting reverts when the handle closes; the call succeeds | unverifiable | state-changing, not tried; Learn's return-code table for the page is empty |
| 60 | WLAN-AutoConfig/Operational enabled, `(A;;0x3;;;IU)`, IDs 11010/11004/11005 (~70 each), 8000/8001, 11000/11001, 8003, 8002 | confirmed | `wevtutil gl`; 300 events: 70/69/67, 16/15, 11/9, 9, 2 (plus hosted-network IDs 20019, 8005-8012, 20020) |
| 61 | Events carry SSID and local MAC, no peer BSSID | confirmed | field names of 8001, 8003, 11000, 11001, 11004, 11005, 11010 (`LocalMac`, `SSID`, no BSSID field) |
| 62 | Free buffers with `WlanFreeMemory` | confirmed | WlanQueryInterface page @d7843384 |
| 63 | Callback thread and deadlock behaviour | added | v_wlanthread.py (OS thread ≠ main, `Dummy-1`); ctypes docs; WlanRegisterNotification @92672624 |

### 4. Kernel-Network

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 64 | GUID `{7dd42a49-5329-4832-8dfd-43d979153a88}` | confirmed | `wevtutil gp … /ge /gm:true /f:xml`, `logman query providers`, `Get-WinEvent -ListProvider` |
| 65 | Keywords IPV4 0x10, IPV6 0x20, Analytic channel 0x8000000000000000 | confirmed | logman output |
| 66 | Level 4 on every event; tasks 10 TCPIP, 11 UDPIP; registered by PID 0 | confirmed | wevtutil XML; logman `PID 0x00000000` |
| 67 | wevtutil prints opcode × 65536 (655360, 2752512) | confirmed | wevtutil opcode values |
| 68 | Event table: 10/26 send, 11/27 recv, 12/28 connect, 13/29 close, 14/30 retransmit, 15/31 accept, 16/32 reconnect, 17 fail (both), 18/34 copy, 42/58 UDP send, 43/59 UDP recv, 49 UDP fail (both); all version 0 | confirmed | wevtutil XML (22 events; 17/49 keywords 0x…30) |
| 69 | Templates for all 22 events, including the 42/43 and 58/59 XML pasted in 4.3 | confirmed | `Get-WinEvent -ListProvider` field lists and raw XML for 43, 59, 17 |
| 70 | Byte offsets / `UserDataLength` table | confirmed | derived from the templates (packed payloads); the 28-byte decoder verified by the synthetic round trip |
| 71 | `win:Port` is network byte order | confirmed; source **strengthened** | Learn InputType @c4c9c064 ("treated as big-endian (network byte order)"), OutputType @1ba36e36; PerfView pinned to 3d89ee85, lines 6568, 7270, 7274 |
| 72 | PerfView classic variant has pointer-sized `connid` | confirmed; **refined** | line 6570 `GetAddressAt(HostOffset(20, 1))`; PerfView's classic layout differs from the manifest past offset 20 |
| 73 | MOF docs say only "Destination IP address" / "Source IP address" | confirmed | udpip-typegroup1 / tcpip-typegroup1 @4d5f26c6 |
| 74 | Which address is remote on receive | unverifiable for Kernel-Network; secondary evidence **added** | System Informer @0a4975a4: etwmon.c 416-437, 525-547 and ntwmi.h 2729-2739, 2834-2844 (TCP `saddr` local both ways, UDP receive swapped) |
| 75 | PID scope filter: kernel-registered provider "will not be enabled" | confirmed | EVENT_FILTER_DESCRIPTOR @92672624 |
| 76 | `EVENT_FILTER_TYPE_EVENT_ID` 0x80000200, ≤ 64 IDs | confirmed | same page; evntprov.h:110, 127 |
| 77 | Kernel provider honours the event-ID filter | unverifiable | needs an elevated session |
| 78 | `MatchAnyKeyword` 0 = all keywords for manifest providers | confirmed | EnableTraceEx2 @540c1f2d remarks |
| 79 | ProcessTrace ordering quote | confirmed | ProcessTrace @540c1f2d |
| 80 | Riot 128 Hz tick | confirmed | row 31 |
| 81 | psutil: PIDs for all sockets, 0 UDP sockets with a remote address | confirmed; reason **added** | v_psutil.py (158 sockets, 0 of 46 UDP); `MIB_UDPROW_OWNER_PID` has no remote fields (Learn @92672624) |
| 82 | I/O counters are not a socket byte meter | confirmed | v_io.py: UDP 10 MiB → `other_count` +17,486, `other_bytes` +419,544; TCP 20 MiB moved → +8,978,432 |
| 83 | Velociraptor artifact handles events 10, 11, 12, 15, 42, 43 | confirmed | docs.velociraptor.app artifact page (`EIDLookup`; uses the payload `EventData.PID`) |

### 5. ETW from ctypes

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 84 | Sizes/offsets of all 14 structs in the 5.1 table (incl. `EVENT_TRACE_PROPERTIES` 120, `EVENT_TRACE_LOGFILEW` 448 / `EventRecordCallback` @424, `TRACE_LOGFILE_HEADER` 280, `EVENT_RECORD` 112, `EVENT_HEADER` 80) | confirmed | v_etw.py executing the doc's 5.2 module: 0 mismatches; field order re-read against wmistr.h:30-54, evntrace.h:544-579, 697-738, 862-888, 972-981, 1051-1062, 1131-1165, 1556-1571, evntcons.h:71-83, 148-185 |
| 85 | Constants (`WNODE_FLAG_TRACED_GUID` … `PROCESS_TRACE_MODE_EVENT_RECORD`, `INVALID_PROCESSTRACE_HANDLE`) | confirmed | wmistr.h:110; evntrace.h:186, 400-474, 1068, 2291; evntcons.h:207-209 |
| 86 | Error codes 5, 24, 87, 183, 1223, 1450, 4201, 7007 | confirmed | winerror.h:269, 440, 828, 1476, 7476, 9486, 12818, 17394 |
| 87 | `BufferSize` 4-16384 KB, "64KB or less", rate bands, 64 KB event cap | confirmed | EVENT_TRACE_PROPERTIES @92672624 |
| 88 | `MinimumBuffers` ≥ 2 per logical CPU; requested 4 → 32 | confirmed | same page; v_etw.py `Min=32` |
| 89 | `MaximumBuffers` tuning advice | confirmed; default observation added | same page; v_etw.py `Max=54` with 0 passed (original output 64) |
| 90 | `FlushTimer` 0 = 1 s default for real time; "5 or 10 seconds" advice | confirmed | same page |
| 91 | QPC default clock since Vista, unaffected by clock changes | confirmed | WNODE_HEADER @0d36b58c |
| 92 | Leave `Wnode.Guid` zero for a normal session | confirmed | StartTraceW @2d357572 |
| 93 | Real-time with no consumer: "new events will be ignored" | confirmed | EVENT_TRACE_PROPERTIES page |
| 94 | Session naming rule, orphan-cleanup quote, 1,024-character names, 64 sessions | confirmed | StartTraceW page (also: `EtwMaxLoggers` 32-256, reboot needed) |
| 95 | EnableTraceEx2: Timeout 0 asynchronous; ≤ 8 sessions per modern provider; provider stays enabled | confirmed | EnableTraceEx2 @540c1f2d |
| 96 | OpenTraceW failure = `INVALID_PROCESSTRACE_HANDLE` + `GetLastError` | confirmed | OpenTraceW @2d357572 |
| 97 | OpenTraceW real-time on a missing session → handle 0x101, `ProcessTrace` → 4201 | confirmed | v_etw.py (0x101, 4201 after 0.000 s) |
| 98 | ProcessTrace: one real-time handle or ≤ 64 file handles; returns after the controller stops ("several seconds") | confirmed | ProcessTrace @540c1f2d |
| 99 | CloseTrace 7007 = success, drains queued events; not for StartTrace handles | confirmed | CloseTrace @540c1f2d |
| 100 | Callback lifetime quote | confirmed | ctypes docs |
| 101 | "ctypes docs do not define what happens when an exception escapes" | **corrected** | old: undefined → new (measured): printed as "Exception ignored on calling ctypes callback function", ProcessTrace continues and returns 0. v_etw.py |
| 102 | Without `RAW_TIMESTAMP` the runtime converts timestamps to FILETIME; sub-ms resolution | confirmed; UTC added | WNODE_HEADER page quote; v_etw.py: first event 00:45:07.970240 vs `time.time()` 00:45:07.970221 UTC; 10.0 / 5.6 µs deltas |
| 103 | Real-time events "are delivered when the buffers are flushed" | confirmed | logging-mode-constants @9359c413 |
| 104 | Decode rate 46,323 events/s | confirmed (order of magnitude) | v_etw.py: 69,688 and 69,732 events/s with a different callback |
| 105 | Sessions outlive the process ("remains active until the session is stopped, the computer is restarted …") | confirmed (doc text) | StartTraceW remarks; not executed (needs an elevated real-time session) |
| 106 | `logman stop … -ets` | confirmed | `logman stop /?` shows `-ets` |
| 107 | Unelevated `logman query -ets` / `QueryAllTracesW` show only `MSDTC_TRACE_SESSION` | confirmed; reason **added** | v_etw.py LoggerCount 1; `logman query MSDTC_TRACE_SESSION -ets`: file mode, circular, so ProcessTrace 4201 is the documented non-real-time case |
| 108 | Negative tests: real-time `StartTraceW` → 5; too-small `Wnode.BufferSize` → 24; QUERY/STOP on a missing session → 4201; `EnableTraceEx2(0, …)` → 87; private + real-time → 87 | confirmed | v_etw.py, every value reproduced |
| 109 | Private session round trip (100,000 events, 0 lost, payload byte-exact, `LogFileMode` 0x20801, header flags 0x242 = 0x2 + 0x40 + 0x200, `Size` 108) | confirmed | v_etw.py; evntcons.h:135-144 |
| 110 | Private sessions since 1703 for low-privilege users; "can record events only for the threads of the process"; "cannot be used with real-time delivery" | confirmed | private-logger page and logging-mode-constants @9359c413 |
| 111 | Doc's `KernelNetworkSession` unelevated → errno 5, falls back cleanly | confirmed | v_etw.py, v_health.py (after adding `pump_alive`/`process_trace_rc` to `health()`) |
| 112 | PLU documented for start/enable/consume (four quotes) | confirmed | StartTraceW, EnableTraceEx2, OpenTraceW, EventAccessControl pages |
| 113 | Per-GUID ACL path `…\Control\WMI\Security`, unreadable | confirmed (unreadable); path source **corrected** | `reg query` → "Access is denied."; `EventAccessQuery` → 5 (v_access.py); the path comes from Geoff Chappell's note, not from the cited Learn page |
| 114 | This shell: Administrators "Group used for deny only", `S-1-16-8192`; PLU = `BUILTIN\Performance Log Users`, 0 members | confirmed | `whoami /groups`; SID translation; `Get-LocalGroupMember -SID S-1-5-32-559` |
| 115 | `TokenElevation` = 20; capability check outputs (elevated False, PLU False, Users True) | confirmed | winnt.h:10788-10808; v_blocks.py ran the doc's 5.7 block |
| 116 | PLU stays enabled in a UAC-filtered token; PLU can enable Kernel-Network | unverifiable | the UAC how-it-works page lists no filtered groups; the test needs a group change |

### 6. Background hogs and fixes

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 117 | Steam path from `HKCU\Software\Valve\Steam` `SteamPath` | confirmed | `Get-ItemProperty` → `c:/program files (x86)/steam` |
| 118 | `content_log.txt` line formats (state changed / App update changed / Current download rate / update canceled / finished update) | confirmed | `Select-String` counts 122 / 579 / 643 / 15 / 30 |
| 119 | `config.vdf` keys `AllowDownloadsDuringGameplay` 0, `DownloadThrottleKbps` 0, `RecentDownloadRate` 12340032 | confirmed | `Select-String` |
| 120 | Steam suspends downloads during play | confirmed | an `App Running` line followed by `Disabled (Suspended)` |
| 121 | Epic log has no download-rate line | **corrected** | old: none → new: end-of-install `Build Stat: AverageDownloadSpeed` / `PeakDownloadSpeed` lines (bytes/sec) |
| 122 | Battle.net folder exists; format not examined | confirmed; format unverifiable | `Test-Path`; only stale 85-byte Switcher logs |
| 123 | DO cmdlets work unelevated; fields; `CdnConnections` 66; 18 files "Caching" | confirmed; meanings **added** | both cmdlets; DO monitor page @49145d39; units unverifiable |
| 124 | ms-settings URIs | confirmed; URL **corrected** | launch-settings @50281bfb lists all four (`…-app` redirects) |
| 125 | New-NetQosPolicy quote, `-AppPathNameMatchCondition`, `-ThrottleRateActionBitsPerSecond`, `-PolicyStore ActiveStore` | confirmed | New-NetQosPolicy @0ef3f225 (incl. the ftp.exe ActiveStore example) |
| 126 | QoS throttling shapes only outbound traffic | **corrected** to verified | Policy-based QoS overview @0d8c9875 (page for an older Windows version) |
| 127 | New-NetQosPolicy needs admin | unverifiable | not stated on the page; not tried |

### 7-8. Decision table and design

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 128 | All rows are [inferred] from verified mechanisms | confirmed as tagged | consistency review |
| 129 | Row 1 window ±2 s vs 3.5's [-3 s, +1 s] | **corrected** | made consistent: [-3 s, +1 s] (the scan precedes its completion notice) |
| 130 | ETW CPU cost "about 2 % of a core at 1,000 events/s" | confirmed; risk **added** | per-event cost 14-22 µs measured; keywords cannot exclude TCP, so background downloads raise the rate (5.4, 8.1) |

### Coverage of the brief

1. ICMP via `IcmpSendEcho2`/`Icmp6SendEcho2`, reply layout, timeouts, status codes, real output, gateway and egress
   without admin: answered (1.1-1.5); gap filled with a tested `MIB_IF_ROW2`/`GetIpForwardTable2` block.
2. Jitter definitions, loss, bufferbloat, perceptible levels, why servers ignore ICMP: answered (2.1-2.4); the
   missing primary game-threshold source was found (Claypool report).
3. Wi-Fi without admin, roaming, 60 s scans: answered (3.1-3.6); added the permission evidence for the scan toggle,
   the disconnected-failure note, a complete handle/enum block and callback threading.
4. Kernel-Network GUID, keywords, IDs, templates from this machine, server flow and gap detection: answered
   (4.1-4.6); the receive-direction semantics remain unverified, with new secondary evidence.
5. Real-time ETW recipe with size checks and negative tests; the PLU question with sources: answered (5.1-5.7); the
   PLU answer for this specific provider remains unverified.
6. Background hogs and safe actions: answered (6.1-6.2), Epic corrected.
7. Decision table: answered (7). "Recommended network monitor design": present (8).
