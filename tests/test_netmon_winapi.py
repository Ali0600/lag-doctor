"""The network ctypes wrappers without the network: layouts, input checks, classification, the location gate.

Sizes and offsets are literals from docs/research/network-and-etw.md (NE) 1.1, 1.5, 3.3-3.5 and
windows-telemetry.md (WT) 11.1, never read back from the modules under test.
"""
from __future__ import annotations

import ctypes
import struct
import uuid
import winreg

import pytest

from lagdoctor.winapi import icmp, iphlp, wlan

GUID_TEXT = "{0A1B2C3D-0000-4000-8000-00000000C0DE}"


# ---------------------------------------------------------------- layouts ---

def test_icmp_layouts_match_the_research():
    assert ctypes.sizeof(icmp.ICMP_ECHO_REPLY) == 40
    assert icmp.ICMP_ECHO_REPLY.Data.offset == 16 and icmp.ICMP_ECHO_REPLY.Options.offset == 24
    assert ctypes.sizeof(icmp.IP_OPTION_INFORMATION) == 16
    assert ctypes.sizeof(icmp.IPV6_ADDRESS_EX) == 26
    assert ctypes.sizeof(icmp.ICMPV6_ECHO_REPLY) == 36
    assert icmp.ICMPV6_ECHO_REPLY.Status.offset == 28 and icmp.ICMPV6_ECHO_REPLY.RoundTripTime.offset == 32
    assert icmp.REPLY4_SIZE >= 40 + 32 + 8 and icmp.REPLY6_SIZE >= 36 + 32 + 8 + 16
    assert len(icmp.PAYLOAD) == 32


def test_iphlp_layouts_match_the_research():
    assert ctypes.sizeof(iphlp.SOCKADDR_INET) == 28
    assert ctypes.sizeof(iphlp.IP_ADDRESS_PREFIX) == 32
    assert ctypes.sizeof(iphlp.MIB_IPFORWARD_ROW2) == 104
    row = iphlp.MIB_IF_ROW2
    assert ctypes.sizeof(row) == 1352
    assert (row.InterfaceIndex.offset, row.Alias.offset, row.Type.offset, row.OperStatus.offset,
            row.TransmitLinkSpeed.offset, row.InOctets.offset, row.OutOctets.offset, row.OutQLen.offset) == \
        (8, 28, 1128, 1156, 1192, 1208, 1280, 1344)


def test_wlan_layouts_match_the_research():
    assert ctypes.sizeof(wlan.WLAN_INTERFACE_INFO) == 532
    assert ctypes.sizeof(wlan.WLAN_REALTIME_CONNECTION_QUALITY) == 296
    assert wlan.WLAN_REALTIME_CONNECTION_QUALITY.linksInfo.offset == 24
    assert ctypes.sizeof(wlan.WLAN_REALTIME_CONNECTION_QUALITY_LINK_INFO) == 272
    assert ctypes.sizeof(wlan.WLAN_STATISTICS) == 368 and wlan.WLAN_STATISTICS.PhyCounters.offset == 224
    assert ctypes.sizeof(wlan.WLAN_PHY_FRAME_STATISTICS) == 144
    assert ctypes.sizeof(wlan.L2_NOTIFICATION_DATA) == 40
    # From the SDK header (the research never made this location-gated call): BSSID at 520 + 40.
    assert ctypes.sizeof(wlan.WLAN_CONNECTION_ATTRIBUTES) == 604
    assert wlan.WLAN_CONNECTION_ATTRIBUTES.wlanAssociationAttributes.offset == 520
    assert wlan.WLAN_ASSOCIATION_ATTRIBUTES.dot11Bssid.offset == 40


@pytest.mark.parametrize("module", [icmp, iphlp, wlan])
def test_the_import_time_size_guard_refuses_a_drifted_layout(module):
    class Drifted(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32)]

    with pytest.raises(RuntimeError, match="expected 8"):
        module._check_size(Drifted, 8, "test")
    module._check_size(Drifted, 4, "test")


# ---------------------------------------------------------------- icmp ---

@pytest.mark.parametrize("ip, want", [("1.1.1.1", "1.1.1.1"), (" 192.168.0.1 ", "192.168.0.1"), ("::1", "::1"),
                                      ("::ffff:1.1.1.1", "1.1.1.1"), ("fe80::1%8", "fe80::1%8"),
                                      ("127.0.0.1", "127.0.0.1")])
def test_check_target_accepts_one_host(ip, want):
    assert str(icmp.check_target(ip)) == want


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "224.0.0.1", "239.255.255.250", "ff02::1", "255.255.255.255",
                                 "1.1.1", "example.com", "", "1.1.1.1; calc", "fe80::1%eth0", None, 16843009,
                                 "1" * 65])
def test_check_target_refuses_everything_else(bad):
    with pytest.raises(ValueError):
        icmp.check_target(bad)


@pytest.mark.parametrize("kw", [{"ttl": 0}, {"ttl": 256}, {"ttl": True}, {"timeout_ms": 0},
                                {"timeout_ms": 60_001}, {"timeout_ms": 1.5}])
def test_ping_refuses_bad_arguments_before_sending(kw):
    with pytest.raises(ValueError):
        icmp.ping("127.0.0.1", **kw)


@pytest.mark.parametrize("name, value, lo, hi", [("ttl", 1, 1, 255), ("ttl", 255, 1, 255),
                                                  ("timeout_ms", 1, 1, 60_000), ("timeout_ms", 60_000, 1, 60_000)])
def test_argument_bounds_accept_their_edges(name, value, lo, hi):
    assert icmp._check_int(name, value, lo, hi) == value


@pytest.mark.parametrize("code, network", [(0, False), (87, False), (6, False), (10999, False), (11000, True),
                                           (11010, True), (11013, True), (11050, True), (11999, True),
                                           (12000, False), (1230, False), (1231, True), (1232, True),
                                           (1233, False), (10051, True), (10065, True)])
def test_is_network_status_literal_points(code, network):
    assert icmp.is_network_status(code) is network


def test_no_reply_returns_network_outcomes_and_raises_api_failures():
    assert icmp._no_reply(icmp.IP_REQ_TIMED_OUT) == (11010, None, None)
    assert icmp._no_reply(icmp.ERROR_NETWORK_UNREACHABLE) == (1231, None, None)
    with pytest.raises(OSError) as e:
        icmp._no_reply(87)
    assert e.value.winerror == 87


def test_a_closed_pinger_refuses_to_ping():
    p = icmp.Pinger()
    p.close()
    with pytest.raises(ValueError, match="closed"):
        p.ping("127.0.0.1")


# ---------------------------------------------------------------- iphlp ---

# (type, NDIS medium, description) of common interfaces, as GetIfEntry2 reports them
@pytest.mark.parametrize("if_type, medium, desc, tunnel", [
    (71, 0, "Intel(R) Wi-Fi 6 AX201 160MHz", False),
    (6, 0, "Intel(R) Ethernet Connection (16) I219-LM", False),
    (6, 0, "Contoso Ethernet Adapter", False),
    (6, 0, "Hyper-V Virtual Ethernet Adapter", False),
    (24, 17, "Software Loopback Interface 1", False),
    (53, 0, "TAP-Contoso Windows Adapter V9", True),
    (53, 0, "Contoso Virtual Adapter", True),          # type alone: IF_TYPE_PROP_VIRTUAL
    (53, 19, "Contoso VPN Tunnel", True),
    (53, 19, "OpenVPN Data Channel Offload", True),
    (131, 12, "WAN Miniport (SSTP)", True),
    (23, 12, "WAN Miniport (PPPOE)", True),
    (6, 19, "Some layer-3 adapter", True),
    (6, 0, "TAP-Windows Adapter V9", True),
    (6, 0, "WireGuard Tunnel", True),
    (6, 0, "PANGP Virtual Ethernet Adapter", True),
])
def test_is_tunnel_literal_points(if_type, medium, desc, tunnel):
    assert iphlp.is_tunnel(if_type, medium, desc) is tunnel


WIFI_ROUTE = {"family": 4, "if_index": 8, "next_hop": "192.168.0.1", "metric": 0}


def test_default_route_is_the_best_route_when_it_leaves_through_a_real_interface():
    best = {"if_index": 8, "next_hop": "192.168.0.1", "source": "192.168.0.225"}
    assert iphlp.pick_default_route(best, False, [], {}) == {"next_hop": "192.168.0.1", "if_index": 8}


def test_default_route_skips_a_vpn_and_finds_the_physical_router():
    best = {"if_index": 24, "next_hop": "10.66.0.1", "source": "10.66.0.2"}        # through a VPN tunnel
    vpn_route = {"family": 4, "if_index": 24, "next_hop": "10.66.0.1", "metric": 0}
    got = iphlp.pick_default_route(best, True, [vpn_route, WIFI_ROUTE], {24: False, 8: True})
    assert got == {"next_hop": "192.168.0.1", "if_index": 8}


def test_default_route_prefers_ipv4_then_the_lower_metric():
    v6 = {"family": 6, "if_index": 8, "next_hop": "fe80::1%8", "metric": 0}
    eth = {"family": 4, "if_index": 4, "next_hop": "192.168.1.1", "metric": 25}
    wifi = dict(WIFI_ROUTE, metric=35)
    assert iphlp.pick_default_route(None, False, [v6, wifi, eth], {8: True, 4: True})["if_index"] == 4
    assert iphlp.pick_default_route(None, False, [v6], {8: True})["next_hop"] == "fe80::1%8"
    assert iphlp.pick_default_route(None, False, [wifi], {8: False}) is None
    assert iphlp.pick_default_route(None, False, [], {}) is None


def test_sockaddr_text():
    sa = iphlp._sockaddr(iphlp._parse_ip("0.0.0.0"))
    assert iphlp._sockaddr_text(sa) is None                            # on-link: no gateway
    assert iphlp._sockaddr_text(iphlp._sockaddr(iphlp._parse_ip("192.168.0.1"))) == "192.168.0.1"
    assert iphlp._sockaddr_text(iphlp._sockaddr(iphlp._parse_ip("fe80::1%8"))) == "fe80::1%8"
    assert iphlp._sockaddr_text(iphlp.SOCKADDR_INET()) is None          # family 0


@pytest.mark.parametrize("bad", [0, -1, 0x1_0000_0000, True, "8", None])
def test_if_row_refuses_a_bad_index(bad):
    with pytest.raises(ValueError):
        iphlp.if_row(bad)


@pytest.mark.parametrize("bad", ["", "not-an-ip", "1.2.3.4.5", None, 7])
def test_best_route_refuses_a_bad_address(bad):
    with pytest.raises(ValueError):
        iphlp.best_route(bad)


# ---------------------------------------------------------------- wlan ---

class FakeDll:
    def __init__(self):
        self.log: list = []

    def WlanRegisterNotification(self, h, source, ignore_duplicate, thunk, ctx, reserved, prev):
        self.log.append(("register", source, thunk))
        return 0

    def WlanCloseHandle(self, h, reserved):
        self.log.append(("close",))
        return 0


def _notification(source: int, code: int, guid: str = GUID_TEXT) -> wlan.L2_NOTIFICATION_DATA:
    d = wlan.L2_NOTIFICATION_DATA(NotificationSource=source, NotificationCode=code)
    ctypes.memmove(ctypes.byref(d.InterfaceGuid), uuid.UUID(guid).bytes_le, 16)
    return d


def test_scan_callback_fires_only_for_acm_scan_complete_and_contains_errors(monkeypatch):
    dll = FakeDll()
    monkeypatch.setattr(wlan, "_dll", lambda: dll)
    w = wlan.Wlan(0x1234)
    got = []
    w.on_scan_complete(lambda t, guid: got.append((t, guid)))
    (_, source, thunk), = dll.log
    assert source == wlan.WLAN_NOTIFICATION_SOURCE_ACM
    thunk(ctypes.pointer(_notification(0x8, 7)), None)          # scan_complete
    thunk(ctypes.pointer(_notification(0x8, 8)), None)          # scan_fail
    thunk(ctypes.pointer(_notification(0x10, 7)), None)         # an MSM code 7 is something else
    assert [g for _, g in got] == [GUID_TEXT] and isinstance(got[0][0], float)
    assert w.notifications == 3 and w.callback_errors == 0

    def boom(t, guid):
        raise RuntimeError("callback bug")

    w.on_scan_complete(boom)
    boom_thunk = dll.log[-1][2]
    boom_thunk(ctypes.pointer(_notification(0x8, 7)), None)     # must not escape into the WLAN thread
    assert w.callback_errors == 1
    w.close()
    assert dll.log[-2][:2] == ("register", wlan.WLAN_NOTIFICATION_SOURCE_NONE)   # unregister, then close
    assert dll.log[-1] == ("close",)
    w.close()                                                     # idempotent
    assert dll.log[-1] == ("close",) and len(dll.log) == 4


def _consent(hklm, hkcu, nonpackaged):
    values = {(winreg.HKEY_LOCAL_MACHINE, wlan._CONSENT_KEY): hklm, (winreg.HKEY_CURRENT_USER, wlan._CONSENT_KEY): hkcu,
              (winreg.HKEY_CURRENT_USER, wlan._CONSENT_KEY + r"\NonPackaged"): nonpackaged}
    return lambda root, path: values[(root, path)]


@pytest.mark.parametrize("hklm, hkcu, nonpackaged, allowed", [
    ("Allow", "Allow", "Allow", True),
    ("Deny", "Allow", "Allow", False),
    ("Allow", "Deny", "Allow", False),
    ("Allow", "Allow", "Deny", False),
    ("Allow", "Allow", None, False),
    (None, None, None, False),
    ("allow", "Allow", "Allow", False),
])
def test_location_gate_needs_every_switch_on(monkeypatch, hklm, hkcu, nonpackaged, allowed):
    monkeypatch.setattr(wlan, "_read_consent", _consent(hklm, hkcu, nonpackaged))
    assert wlan.location_allowed() is allowed


def test_bssid_never_makes_the_location_gated_call_while_location_is_off(monkeypatch):
    monkeypatch.setattr(wlan, "_read_consent", _consent("Deny", "Deny", "Allow"))     # this laptop
    w = wlan.Wlan(0x1234)
    calls = []
    monkeypatch.setattr(w, "_query", lambda guid, opcode: (calls.append(opcode), (5, b""))[1])
    assert w.bssid(GUID_TEXT) is None
    assert calls == []


def test_bssid_is_read_when_location_is_already_allowed(monkeypatch):
    monkeypatch.setattr(wlan, "_read_consent", _consent("Allow", "Allow", "Allow"))
    attrs = wlan.WLAN_CONNECTION_ATTRIBUTES(isState=1)
    attrs.wlanAssociationAttributes.dot11Bssid[:] = [0xAA, 0xBB, 0xCC, 0x01, 0x02, 0x03]
    w = wlan.Wlan(0x1234)
    calls = []
    monkeypatch.setattr(w, "_query", lambda guid, opcode: (calls.append(opcode), (0, bytes(attrs)))[1])
    assert w.bssid(GUID_TEXT) == "aa:bb:cc:01:02:03"
    assert calls == [wlan.OPCODE_CURRENT_CONNECTION]
    monkeypatch.setattr(w, "_query", lambda guid, opcode: (wlan.ERROR_ACCESS_DENIED, b""))
    assert w.bssid(GUID_TEXT) is None                           # consent revoked meanwhile: still no reading


def _quality_bytes(phy=8, link_quality=86, rx=173_300, tx=433_300, links=1, freq=5180, rssi=-53):
    q = wlan.WLAN_REALTIME_CONNECTION_QUALITY(dot11PhyType=phy, ulLinkQuality=link_quality, ulRxRate=rx,
                                              ulTxRate=tx, ulNumLinks=links)
    q.linksInfo[0].ulChannelCenterFrequencyMhz = freq
    q.linksInfo[0].lRssi = rssi
    return bytes(q)


def _fake_queries(monkeypatch, w, answers):
    monkeypatch.setattr(w, "_query", lambda guid, opcode: answers[opcode])


def test_quality_decodes_the_measured_values(monkeypatch):
    w = wlan.Wlan(0x1234)
    _fake_queries(monkeypatch, w, {wlan.OPCODE_REALTIME_QUALITY: (0, _quality_bytes()),
                                   wlan.OPCODE_CHANNEL_NUMBER: (0, struct.pack("<I", 36))})
    assert w.quality(GUID_TEXT) == {"rssi_dbm": -53, "signal_pct": 86, "rx_mbps": 173.3, "tx_mbps": 433.3,
                                    "freq_mhz": 5180, "channel": 36, "phy": "802.11ac"}


def test_quality_rejects_implausible_fields_and_reads_khz_as_mhz(monkeypatch):
    w = wlan.Wlan(0x1234)
    _fake_queries(monkeypatch, w, {wlan.OPCODE_REALTIME_QUALITY: (0, _quality_bytes(link_quality=101, rssi=5,
                                                                                    freq=5_180_000, phy=42)),
                                   wlan.OPCODE_CHANNEL_NUMBER: (0, struct.pack("<I", 999))})
    q = w.quality(GUID_TEXT)
    assert (q["signal_pct"], q["rssi_dbm"], q["freq_mhz"], q["channel"], q["phy"]) == \
        (None, None, 5180, None, "PHY type 42")
    _fake_queries(monkeypatch, w, {wlan.OPCODE_REALTIME_QUALITY: (0, _quality_bytes(links=0)[:24]),
                                   wlan.OPCODE_CHANNEL_NUMBER: (87, b"")})
    q = w.quality(GUID_TEXT)
    assert q["rssi_dbm"] is None and q["freq_mhz"] is None and q["channel"] is None and q["signal_pct"] == 86


def test_a_failed_query_is_no_reading_only_while_disconnected(monkeypatch):
    w = wlan.Wlan(0x1234)
    disconnected = struct.pack("<i", 4)
    _fake_queries(monkeypatch, w, {wlan.OPCODE_REALTIME_QUALITY: (0x80342001, b""),
                                   wlan.OPCODE_STATISTICS: (0x80342001, b""),
                                   wlan.OPCODE_INTERFACE_STATE: (0, disconnected)})
    assert w.quality(GUID_TEXT) is None and w.retries(GUID_TEXT) is None
    _fake_queries(monkeypatch, w, {wlan.OPCODE_REALTIME_QUALITY: (50, b""),
                                   wlan.OPCODE_INTERFACE_STATE: (0, struct.pack("<i", 1))})
    with pytest.raises(OSError) as e:
        w.quality(GUID_TEXT)                                     # connected, yet the query failed: an error
    assert e.value.winerror == 50


def test_retries_reads_phy_zero(monkeypatch):
    stats = wlan.WLAN_STATISTICS(dwNumberOfPhys=6)
    stats.PhyCounters[0].ullRetryCount = 5466
    w = wlan.Wlan(0x1234)
    _fake_queries(monkeypatch, w, {wlan.OPCODE_STATISTICS: (0, bytes(stats))})
    assert w.retries(GUID_TEXT) == 5466
    _fake_queries(monkeypatch, w, {wlan.OPCODE_STATISTICS: (0, bytes(wlan.WLAN_STATISTICS(dwNumberOfPhys=0)))})
    assert w.retries(GUID_TEXT) is None
    _fake_queries(monkeypatch, w, {wlan.OPCODE_STATISTICS: (0, b"\0" * 100)})
    with pytest.raises(OSError):
        w.retries(GUID_TEXT)


def test_guid_text_round_trips():
    g = wlan._guid_struct(GUID_TEXT)
    assert wlan._guid_text(g) == GUID_TEXT
    assert wlan._guid_text(wlan._guid_struct(GUID_TEXT.lower().strip("{}"))) == GUID_TEXT
    with pytest.raises(ValueError):
        wlan._guid_struct("not-a-guid")


def test_closed_handle_refuses_queries():
    w = wlan.Wlan(None)
    with pytest.raises(ValueError, match="closed"):
        w.interfaces()
