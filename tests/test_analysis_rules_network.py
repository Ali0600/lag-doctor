"""The network rules (NE 2.2, 2.3, 3.3, 3.5, 3.6, 7) on hand-built ping tables, thresholds pinned either side."""
from __future__ import annotations

import pytest

from lagdoctor.analysis import rules
from lagdoctor.model import Finding, LagEvent
from tests import synth
from tests.test_analysis_rules import is_finding, make, run

WIFI = {"network": {"name": "Intel(R) Wi-Fi 6 AX201 160MHz", "kind": "wifi", "band": "5 GHz", "channel": 36}}


def net(seconds=30.0, *, kind="wifi", flows=None, ok=("frames", "system", "network"), **cols):
    cap = make(ok=ok, inventory={"network": dict(WIFI["network"], kind=kind)} if kind else None, duration=seconds)
    cap.network = synth.network_table(seconds, **cols)
    cap.net_flows = flows if flows is not None else {}
    return cap


def gap(i, t, ms, kind="delay"):
    return LagEvent(i, "net_gap", t, t + ms / 1000.0, ms, {"ms": ms, "kind": kind})


# ---------------------------------------------------------------------------------- Wi-Fi ---

@pytest.mark.parametrize("rssi, kind", [(-70.1, Finding), (-70.0, rules.Cleared)])
def test_wifi_weak_below_minus_70_dbm(rssi, kind):
    assert isinstance(run("wifi_weak", net(wifi_rssi_dbm=rssi)), kind)


@pytest.mark.parametrize("quality, kind", [(59.8, Finding), (60.0, rules.Cleared)])
def test_wifi_weak_maps_link_quality_onto_dbm_when_rssi_is_missing(quality, kind):
    # NE 3.3 (verify #50): quality 0..100 is -100..-50 dBm, so 60 is -70 dBm
    assert isinstance(run("wifi_weak", net(wifi_signal_pct=quality)), kind)


@pytest.mark.parametrize("gw, conf", [(30.1, "high"), (30.0, "medium")])
def test_wifi_weak_counts_a_router_ping_p95_over_30_ms(gw, conf):
    assert is_finding(run("wifi_weak", net(wifi_rssi_dbm=-75.0, gw_rtt_ms=gw)), conf)


@pytest.mark.parametrize("gw, res_type", [(9.9, type(None)), (10.0, Finding)])
def test_wifi_weak_a_fast_router_ping_under_10_ms_speaks_against(gw, res_type):
    assert isinstance(run("wifi_weak", net(wifi_rssi_dbm=-75.0, gw_rtt_ms=gw)), res_type)


def test_wifi_rules_on_a_wired_pc_are_ruled_out_and_unknown_links_unchecked():
    for cause in ("wifi_weak", "wifi_roaming", "wifi_scan_spikes"):
        assert isinstance(run(cause, net(kind="ethernet", gw_rtt_ms=2.0)), rules.Cleared), cause
        assert isinstance(run(cause, net(kind=None, gw_rtt_ms=2.0)), rules.Unchecked), cause


def test_wifi_roaming_counts_access_point_changes_and_never_prints_them():
    bssid = (lambda t: "aa:bb:cc:dd:ee:01" if t < 15 else "aa:bb:cc:dd:ee:02")
    res = run("wifi_roaming", net(wifi_bssid=bssid, wifi_channel=36))
    assert is_finding(res, "high") and "1 time " in res.evidence[0].text
    assert not any("aa:bb" in e.text for e in res.evidence) and "aa:bb" not in res.title + res.summary


def test_wifi_roaming_from_a_channel_change_and_its_ping_spike():
    chan = (lambda t: 36 if t < 15 else 149)
    res = run("wifi_roaming", net(wifi_channel=chan))
    assert is_finding(res, "medium") and "36 to 149" in res.evidence[0].text
    spike = LagEvent(1, "net_spike", 14.0, 16.0, 90.0, {"target": "internet"})
    assert is_finding(run("wifi_roaming", net(wifi_channel=chan), events=[spike]), "high")


def test_wifi_roaming_none_is_cleared_and_says_what_it_could_not_see():
    res = run("wifi_roaming", net(wifi_channel=36))
    assert isinstance(res, rules.Cleared) and "channel 36" in res.text and "location permission" in res.text


def scans_cap(spike_ms, spiking=(10.0, 20.0, 30.0), scans=(10.0, 20.0, 30.0, 40.0)):
    def gw(t):
        return spike_ms if any(s - 1.0 <= t <= s for s in spiking) else 2.0
    return net(50.0, gw_rtt_ms=gw, wifi_rssi_dbm=-55.0, flows={"wifi_scans": list(scans)})


def test_wifi_scan_spikes_three_aligned_spikes_are_likely():
    assert is_finding(run("wifi_scan_spikes", scans_cap(40.0)), "high")
    assert is_finding(run("wifi_scan_spikes", scans_cap(40.0, spiking=(10.0, 20.0))), "low")
    assert isinstance(run("wifi_scan_spikes", scans_cap(40.0, spiking=())), rules.Cleared)


@pytest.mark.parametrize("spike, high", [(22.0, True), (21.9, False)])
def test_wifi_scan_spike_is_20_ms_over_the_routers_normal_ping(spike, high):
    res = run("wifi_scan_spikes", scans_cap(spike))
    assert (is_finding(res, "high")) is high


def test_wifi_scan_spikes_without_scans_are_unchecked():
    assert isinstance(run("wifi_scan_spikes", net(gw_rtt_ms=2.0, wifi_rssi_dbm=-55.0, flows={"wifi_scans": []})),
                      rules.Unchecked)
    res = run("wifi_scan_spikes", net(gw_rtt_ms=2.0, wifi_rssi_dbm=-55.0, flows={}))
    assert isinstance(res, rules.Unchecked) and "not recorded" in res.text


# ------------------------------------------------------------------------------ bufferbloat ---

def loaded(rise, gw_rise=0.0, peak=5e6):
    busy = (lambda t: 10.0 <= t <= 20.0)
    return net(nic_rx_bps=lambda t: peak if busy(t) else 0.05 * peak,
               inet_rtt_ms=lambda t: 10.0 + (rise if busy(t) else 0.0),
               gw_rtt_ms=lambda t: 2.0 + (gw_rise if busy(t) else 0.0))


@pytest.mark.parametrize("rise, kind", [(30.0, Finding), (29.9, rules.Cleared)])
def test_bufferbloat_ping_rising_30_ms_under_load(rise, kind):
    assert isinstance(run("bufferbloat", loaded(rise)), kind)


def test_bufferbloat_with_the_router_rising_too_is_the_local_hop():
    assert run("bufferbloat", loaded(60.0, gw_rise=10.0)) is None
    assert is_finding(run("bufferbloat", loaded(60.0, gw_rise=9.9)))


@pytest.mark.parametrize("peak, kind", [(1.0e6, Finding), (0.99e6, rules.Unchecked)])
def test_bufferbloat_needs_a_connection_that_was_ever_busy(peak, kind):
    assert isinstance(run("bufferbloat", loaded(60.0, peak=peak)), kind)


# --------------------------------------------------------------------- ISP, server, loss, VPN ---

@pytest.mark.parametrize("inet, kind", [(60.0, Finding), (59.9, rules.Cleared)])
def test_isp_latency_from_60_ms(inet, kind):
    assert isinstance(run("isp_latency", net(inet_rtt_ms=inet, gw_rtt_ms=3.0)), kind)


def test_isp_latency_with_a_slow_router_starts_at_home():
    assert run("isp_latency", net(inet_rtt_ms=80.0, gw_rtt_ms=10.0)) is None
    assert is_finding(run("isp_latency", net(inet_rtt_ms=80.0, gw_rtt_ms=9.9)), "medium")


FLOWS = {"server": {"ip": "203.0.113.7", "port": 27015, "proto": "udp"}, "gaps": [], "layout": "verified"}


@pytest.mark.parametrize("n, conf", [(3, "medium"), (2, "low")])
def test_server_lag_needs_three_gaps_while_the_internet_answers(n, conf):
    cap = net(inet_rtt_ms=7.0, flows=dict(FLOWS), ok=("frames", "system", "network", "net_flows"))
    assert is_finding(run("server_lag", cap, events=[gap(i, 5.0 * i, 300.0) for i in range(1, n + 1)]), conf)


def test_server_lag_ignores_gaps_where_the_game_itself_paused():
    cap = net(inet_rtt_ms=7.0, flows=dict(FLOWS), ok=("frames", "system", "network", "net_flows"))
    res = run("server_lag", cap, events=[gap(i, 5.0 * i, 300.0, "local") for i in range(1, 5)])
    assert isinstance(res, rules.Cleared) and "kept arriving" in res.text


def test_server_lag_from_server_pings_and_unchecked_without_either_source():
    for ms, res_type in ((101.0, Finding), (100.0, type(None))):
        cap = net(inet_rtt_ms=7.0, server_rtt_ms=ms, flows={})
        assert isinstance(run("server_lag", cap), res_type), ms
    res = run("server_lag", net(inet_rtt_ms=7.0, flows={}))
    assert isinstance(res, rules.Unchecked) and "ETW" in res.text


def test_packet_loss():
    loss = LagEvent(1, "net_loss", 9.75, 10.25, 10.0, {"target": "internet", "lost": 1, "lost_15s": 3, "sent_15s": 30,
                                                        "loss_pct": 10.0, "burst": True})
    res = run("packet_loss", net(inet_rtt_ms=7.0), events=[loss])
    assert is_finding(res, "medium") and "3 of 30" in res.evidence[0].text and "burst" in res.evidence[0].text
    router = LagEvent(1, "net_loss", 9.75, 10.25, 10.0, {"target": "gateway", "lost_15s": 3, "sent_15s": 30})
    assert run("packet_loss", net(inet_rtt_ms=7.0), events=[router]) is None       # NE 7 row 9
    assert isinstance(run("packet_loss", net(inet_rtt_ms=7.0)), rules.Cleared)


def test_vpn_route():
    vpn = {"route": {"if_index": 21, "description": "Contoso VPN Tunnel", "vpn": True}}
    assert is_finding(run("vpn_route", net(inet_rtt_ms=7.0, flows=vpn)), "medium")
    assert is_finding(run("vpn_route", net(inet_rtt_ms=70.0, flows=vpn)), "high")
    direct = {"route": {"if_index": 8, "description": "Intel(R) Wi-Fi 6 AX201 160MHz", "vpn": False}}
    assert isinstance(run("vpn_route", net(inet_rtt_ms=7.0, flows=direct)), rules.Cleared)
    assert isinstance(run("vpn_route", net(inet_rtt_ms=7.0, flows={})), rules.Unchecked)


def test_bandwidth_hog_from_a_logged_download_and_from_per_program_traffic():
    steam = {"downloads": [{"t": 15.0, "source": "Steam", "mbps": 98.0}]}
    res = run("bandwidth_hog", net(inet_rtt_ms=7.0, flows=steam))
    assert is_finding(res, "medium") and "Steam" in res.evidence[0].text
    bloat = net(inet_rtt_ms=lambda t: 60.0 if abs(t - 15.0) <= 5.0 else 7.0, flows=steam)
    assert is_finding(run("bandwidth_hog", bloat), "high")
    talkers = {"downloads": [], "talkers": [{"pid": 9999, "name": "chrome.exe", "rx_bytes": 30e6, "tx_bytes": 0},
                                            {"pid": 4242, "name": "game.exe", "rx_bytes": 900e6, "tx_bytes": 1e6}]}
    res = run("bandwidth_hog", net(inet_rtt_ms=7.0, flows=talkers))
    assert is_finding(res) and "chrome.exe" in res.evidence[0].text
    assert not any("game.exe" in e.text for e in res.evidence)                     # the game's own traffic


@pytest.mark.parametrize("moved, kind", [(30e6, Finding), (29.9e6, rules.Cleared)])     # 1 MB/s over 30 s
def test_bandwidth_hog_a_program_moving_1_mb_a_second(moved, kind):
    flows = {"downloads": [], "talkers": [{"pid": 9999, "name": "chrome.exe", "rx_bytes": moved, "tx_bytes": 0}]}
    assert isinstance(run("bandwidth_hog", net(inet_rtt_ms=7.0, flows=flows)), kind)


def test_bandwidth_hog_without_any_traffic_source_is_unchecked():
    assert isinstance(run("bandwidth_hog", net(inet_rtt_ms=7.0, flows={})), rules.Unchecked)
    assert isinstance(run("bandwidth_hog", net(inet_rtt_ms=7.0, flows={"downloads": []})), rules.Cleared)
