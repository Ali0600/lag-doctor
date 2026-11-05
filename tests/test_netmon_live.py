"""Live, read-only checks of the network probes on this machine, against independent readouts.

Echo requests go only to 127.0.0.1, ::1, the default gateway and 1.1.1.1: about 51 per run of this
file (8 loopback, 4 to the router and 1.1.1.1, 1 TTL-limited, about 40 in the 10 s capture, which
also self-tests on loopback). Nothing is changed: routes, Wi-Fi and the registry are only read.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import psutil
import pytest

from lagdoctor import clock
from lagdoctor.capture.netmon import NetworkSampler, band_of
from lagdoctor.model import N
from lagdoctor.winapi import icmp, iphlp, wlan

POWERSHELL = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")


def _netroute_defaults() -> list[dict]:
    """Get-NetRoute's IPv4 default routes: the independent readout for the gateway and egress."""
    cmd = ("Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | "
           "Select-Object ifIndex,NextHop,RouteMetric | ConvertTo-Json -Compress")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                         text=True, timeout=90, check=True).stdout.strip()
    if not out:
        return []
    rows = json.loads(out)
    return [rows] if isinstance(rows, dict) else rows


@pytest.fixture(scope="module")
def defaults() -> list[dict]:
    rows = _netroute_defaults()
    if not rows:
        pytest.skip("no IPv4 default route on this machine right now (offline?)")
    return rows


# ---------------------------------------------------------------- icmp ---

def test_loopback_echo_ipv4_and_ipv6():
    for ip in ("127.0.0.1", "::1"):
        status, rtt, frm = icmp.ping(ip)
        assert status == icmp.IP_SUCCESS and frm == ip
        assert rtt is not None and 0 < rtt < 50


def test_reply_layout_self_check_on_loopback():
    icmp.self_check(4)        # Data == buffer + 40 with our payload there (NE 8.3)
    icmp.self_check(6)


def test_the_router_and_the_internet_anchor_answer(defaults):
    gw = iphlp.default_gateway()
    with icmp.Pinger() as p:
        for ip in (gw, "1.1.1.1"):
            for _ in range(2):
                status, rtt, frm = p.ping(ip)
                assert status == icmp.IP_SUCCESS, (ip, status)
                assert frm == ip and 0 < rtt < 500, (ip, rtt, frm)


def test_a_ttl_limited_probe_gets_a_reply_that_is_not_success(defaults):
    status, rtt, frm = icmp.ping("1.1.1.1", ttl=1)       # hop 1 = the router answers "TTL expired" (NE 1.3)
    assert status != icmp.IP_SUCCESS                      # IcmpSendEcho2 returned one reply, yet not an echo
    assert status in (icmp.IP_TTL_EXPIRED_TRANSIT, icmp.IP_REQ_TIMED_OUT), status
    if status == icmp.IP_TTL_EXPIRED_TRANSIT:
        assert frm == iphlp.default_gateway() and rtt is not None


# ---------------------------------------------------------------- iphlp ---

def test_default_gateway_and_egress_match_get_netroute(defaults):
    gw = iphlp.default_gateway()
    assert gw in {r["NextHop"] for r in defaults}
    best = iphlp.best_route("1.1.1.1")
    assert best["if_index"] in {r["ifIndex"] for r in defaults}
    assert best["next_hop"] == gw and best["source"]


def test_loopback_route_is_on_link():
    r = iphlp.best_route("127.0.0.1")
    assert r["next_hop"] is None and r["source"] == "127.0.0.1"
    assert iphlp.if_row(r["if_index"])["type"] == iphlp.IF_TYPE_SOFTWARE_LOOPBACK


def test_if_row_octets_match_psutil(defaults):
    idx = iphlp.best_route("1.1.1.1")["if_index"]
    before = iphlp.if_row(idx)
    nic = psutil.net_io_counters(pernic=True)[before["alias"]]    # psutil reads its own GetIfEntry2 per NIC
    after = iphlp.if_row(idx)
    assert before["in_octets"] <= nic.bytes_recv <= after["in_octets"]
    assert before["out_octets"] <= nic.bytes_sent <= after["out_octets"]
    assert after["oper_status"] == iphlp.IF_OPER_STATUS_UP and after["tunnel"] is False


def test_if_row_of_a_missing_interface_is_an_oserror():
    with pytest.raises(OSError) as e:
        iphlp.if_row(0x7FFFFFFF)
    assert e.value.winerror == iphlp.ERROR_FILE_NOT_FOUND


# ---------------------------------------------------------------- wlan ---

@pytest.fixture(scope="module")
def wifi(defaults):
    idx = iphlp.best_route("1.1.1.1")["if_index"]
    row = dict(iphlp.if_row(idx), if_index=idx)
    if row["type"] != iphlp.IF_TYPE_IEEE80211:
        pytest.skip(f"the internet goes out through {row['description']!r}, not Wi-Fi")
    w = wlan.Wlan.open()
    assert w is not None, wlan.Wlan.open_error
    try:
        yield w, row
    finally:
        w.close()


def test_wlan_knows_the_adapter_in_use(wifi):
    w, row = wifi
    found = {i["guid"]: i for i in w.interfaces()}
    assert row["guid"] in found and found[row["guid"]]["connected"]


def test_wifi_rates_match_the_nic_link_speeds(wifi):
    w, row = wifi
    for _ in range(3):                                   # the rate can adapt between the reads
        before = iphlp.if_row(row["if_index"])
        q = w.quality(row["guid"])
        after = iphlp.if_row(row["if_index"])
        rx_bps, tx_bps = round(q["rx_mbps"] * 1000) * 1000, round(q["tx_mbps"] * 1000) * 1000   # kbit/s units
        if rx_bps in (before["rx_link_bps"], after["rx_link_bps"]) and \
                tx_bps in (before["tx_link_bps"], after["tx_link_bps"]):
            break
        time.sleep(0.2)
    else:
        pytest.fail(f"Wi-Fi rates {q['rx_mbps']}/{q['tx_mbps']} Mbit/s never matched the link speeds "
                    f"{after['rx_link_bps']}/{after['tx_link_bps']} bit/s")


def test_wifi_quality_is_plausible_and_channel_matches_frequency(wifi):
    w, row = wifi
    q = w.quality(row["guid"])
    assert -100 <= q["rssi_dbm"] < 0 and 0 <= q["signal_pct"] <= 100
    f, ch = q["freq_mhz"], q["channel"]
    band = band_of(f)
    expected = {"2.4 GHz": 14 if f == 2484 else (f - 2407) // 5, "5 GHz": (f - 5000) // 5,
                "6 GHz": (f - 5950) // 5}[band]
    assert ch == expected, (f, ch)


def test_wifi_retries_count_up(wifi):
    w, row = wifi
    a = w.retries(row["guid"])
    time.sleep(0.5)
    b = w.retries(row["guid"])
    assert 0 <= a <= b


def test_bssid_stays_none_without_location_permission(wifi, monkeypatch):
    w, row = wifi
    if wlan.location_allowed():
        pytest.skip("location is allowed for desktop apps on this machine; the gate is open")
    real_query = w._query

    def refuse_gated(guid, opcode):            # even a broken gate must not reach the gated call here
        assert opcode != wlan.OPCODE_CURRENT_CONNECTION, "the location-gated query was attempted"
        return real_query(guid, opcode)

    monkeypatch.setattr(w, "_query", refuse_gated)
    assert w.bssid(row["guid"]) is None


@pytest.mark.skipif(os.environ.get("LAGDOCTOR_SLOW_TESTS") != "1",
                    reason="waits up to 65 s for Windows' periodic Wi-Fi scan; set LAGDOCTOR_SLOW_TESTS=1")
def test_a_scan_notification_arrives_on_a_foreign_thread(wifi):
    w, row = wifi
    got: list = []
    w.on_scan_complete(lambda t, guid: got.append((t, guid, threading.get_ident())))
    deadline = time.monotonic() + 65
    while not got and time.monotonic() < deadline:
        time.sleep(0.25)
    assert got, "no scan_complete within 65 s (they came every 60 s here, NE 3.5)"
    t, guid, ident = got[0]
    assert guid == row["guid"] and ident != threading.get_ident()
    assert abs(clock.now() - t) < 66
    assert w.callback_errors == 0


# ---------------------------------------------------------------- the collector ---

def test_a_ten_second_capture_of_this_machine(defaults):
    s = NetworkSampler()
    s.start(clock.now())
    try:
        time.sleep(10.0)
    finally:
        s.stop()
    res = s.result()
    cols = res["columns"]
    n = len(cols["t"])
    gw = [v for v in cols[N.GW_RTT_MS] if v is not None]
    inet = [v for v in cols[N.INET_RTT_MS] if v is not None]
    print(f"\n10 s capture: {n} rows; router RTT {min(gw):.1f}-{max(gw):.1f} ms ({len(gw)} replies), "
          f"1.1.1.1 RTT {min(inet):.1f}-{max(inet):.1f} ms ({len(inet)} replies); probes {res['probes']}; "
          f"interface {res['interface']}; route {res['route']}; status {s.status()}")
    assert 18 <= n <= 22, n
    assert res["targets"]["gateway"] in {r["NextHop"] for r in defaults}
    for target, replies, top in (("gateway", gw, 200.0), ("internet", inet, 500.0)):
        p = res["probes"][target]
        assert p["from_t"] == cols["t"][0]
        assert p["sent"] == n and p["lost"] == n - len(replies) == len(res["lost"][target])
        assert len(replies) >= 0.8 * n and all(0 < v < top for v in replies), (target, replies)
    assert res["probes"]["server"] == {"sent": 0, "lost": 0, "from_t": None}
    assert all(v is None or v >= 0 for v in cols[N.NIC_RX_BPS] + cols[N.NIC_TX_BPS])
    assert sum(v is not None for v in cols[N.NIC_RX_BPS]) >= n - 1      # only the first row has no rate
    assert res["route"]["vpn"] is False
    if res["interface"]["kind"] == "wifi":
        assert sum(v is not None for v in cols[N.WIFI_SIGNAL_PCT]) == n
        assert res["interface"]["band"] and res["interface"]["channel"]
    assert s.status().state == "ok", s.status()
