"""The network rules (NE 2.2, 2.3, 3.3, 3.5, 3.6, 7) on hand-built ping tables, thresholds pinned either side."""
from __future__ import annotations

import pytest

from lagdoctor.analysis import rules
from lagdoctor.model import Finding, LagEvent, SourceStatus
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
    spike = LagEvent(1, "net_spike", 14.0, 16.0, 90.0, {"target": "internet"})        # same channel throughout
    res = run("wifi_roaming", net(wifi_bssid=bssid, wifi_channel=36), events=[spike])
    assert any("within 3 s of 1 of those switches" in e.text for e in res.evidence)


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


# The server flow as the collector reports it, its packets seen from 0.5 s to 29.5 s of the 30 s capture (CC-1)
FLOWS = {"server": {"ip": "203.0.113.7", "port": 27015, "proto": "udp", "first_rx_t": 0.5, "last_rx_t": 29.5},
         "gaps": [], "layout": "verified"}
ETW = ("frames", "system", "network", "net_flows")


def flows(**server):
    return dict(FLOWS, server=dict(FLOWS["server"], **server))


@pytest.mark.parametrize("n, conf", [(3, "medium"), (2, "low")])
def test_server_lag_needs_three_gaps_while_the_internet_answers(n, conf):
    cap = net(inet_rtt_ms=7.0, flows=dict(FLOWS), ok=ETW)
    assert is_finding(run("server_lag", cap, events=[gap(i, 5.0 * i, 300.0) for i in range(1, n + 1)]), conf)


def test_server_lag_ignores_gaps_where_the_game_itself_paused():
    cap = net(inet_rtt_ms=7.0, flows=dict(FLOWS), ok=ETW)
    res = run("server_lag", cap, events=[gap(i, 5.0 * i, 300.0, "local") for i in range(1, 5)])
    assert isinstance(res, rules.Cleared) and "kept arriving from 0 s to 30 s (97% of the capture" in res.text
    # item 10: a "local" gap is a silence of 100 ms or more in the server's packets too, so never "no gap"
    assert "no gap" not in res.text
    assert "the only gaps of 100 ms or more came from the game pausing its own sending" in res.text
    quiet = run("server_lag", cap)                                      # control: no gap at all
    assert isinstance(quiet, rules.Cleared) and "97% of the capture; no gap of 100 ms or more)" in quiet.text


def test_a_capture_whose_only_server_gap_was_the_game_pausing_says_so():
    # item 10 on the real path: one 531 ms gap with the game's own sending paused too (NE 4.6 "local")
    from lagdoctor.analysis.diagnose import diagnose
    d = diagnose(synth.laglab("smooth", server_gaps=[(5.98, 531.25, "local")]).cap)
    assert d.cleared["server_lag"] == ("The game server's packets kept arriving from 0 s to 30 s (98% of the capture; "
                                       "the only gap of 100 ms or more came from the game pausing its own sending).")


def test_server_lag_without_a_server_flow_is_unchecked_never_fine():
    # CC-1: a TCP-only or offline game has no UDP server flow, so "its packets kept arriving" was never measured
    for no_server in ({"gaps": [], "layout": "verified"}, dict(FLOWS, server=None)):
        res = run("server_lag", net(inet_rtt_ms=7.0, flows=no_server, ok=ETW))
        assert isinstance(res, rules.Unchecked) and res.text.startswith("no game-server UDP traffic was seen"), res


@pytest.mark.parametrize("last, kind", [(15.5, rules.Cleared), (15.2, type(None))])     # half of 30 s from 0.5 s
def test_the_server_kept_arriving_only_where_its_packets_cover_most_of_the_capture(last, kind):
    res = run("server_lag", net(inet_rtt_ms=7.0, flows=flows(last_rx_t=last), ok=ETW))
    assert isinstance(res, kind)


def test_the_server_kept_arriving_never_past_the_moment_the_etw_consumer_stopped():
    # CC-1: a partial net_flows whose consumer stopped by itself saw nothing after that time
    stopped = dict(FLOWS, health={"consumer_stopped_t": 9.0})
    assert run("server_lag", net(inet_rtt_ms=7.0, flows=stopped, ok=ETW)) is None
    late = dict(FLOWS, health={"consumer_stopped_t": 29.0})
    res = run("server_lag", net(inet_rtt_ms=7.0, flows=late, ok=ETW))
    assert isinstance(res, rules.Cleared) and "to 29 s, when the ETW consumer stopped" in res.text


def test_a_server_that_fell_silent_for_good_is_a_lost_connection():
    # CC-1: an "open" gap: the server's packets stopped while the game kept sending, and the internet answered
    res = run("server_lag", net(inet_rtt_ms=7.0, flows=flows(last_rx_t=12.0), ok=ETW),
              events=[gap(1, 12.0, 18000.0, "open")])
    assert is_finding(res, "medium") and res.event_ids == [1]
    assert any("stopped answering for good at 12 s" in e.text and "connection to the server was lost" in e.text
               for e in res.evidence)
    lost = LagEvent(2, "net_loss", 11.5, 29.5, 100.0, {"target": "internet"})       # the PC's own link went too
    res = run("server_lag", net(inet_rtt_ms=7.0, flows=flows(last_rx_t=12.0), ok=ETW),
              events=[gap(1, 12.0, 18000.0, "open"), lost])
    assert res is None


def test_gaps_of_unknown_kind_are_never_read_as_local_nor_as_packets_kept_arriving():
    # CC-1: "unknown": late packets hid whether our own sending paused too, so neither side can be told
    unknown = [gap(i, 5.0 * i, 300.0, "unknown") for i in range(1, 4)]
    assert run("server_lag", net(inet_rtt_ms=7.0, flows=dict(FLOWS), ok=ETW), events=unknown) is None
    # with the server's pings slow too there is a finding, whose evidence shows how the gaps were read
    res = run("server_lag", net(inet_rtt_ms=7.0, server_rtt_ms=150.0, flows=dict(FLOWS), ok=ETW), events=unknown)
    texts = [e.text for e in res.evidence]
    assert any(t.startswith("3 gaps in the server's packets could not be told apart") for t in texts), texts
    assert not any("from the game itself" in t or "kept arriving" in t for t in texts), texts


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
    res = run("packet_loss", net(inet_rtt_ms=7.0))
    assert isinstance(res, rules.Cleared) and res.text == "No ping to the internet was lost (0 of 60)."


def probes(lost, sent):
    return {"probes": {"internet": {"sent": sent, "lost": lost, "from_t": 0.5}}}


@pytest.mark.parametrize("lost, sent, kind, text", [
    (2, 200, Finding, "1.0% of pings to the internet were lost over the capture (2 of 200; 1% or more is bad)"),
    (2, 201, rules.Cleared, "Only 2 of 201 pings to the internet were lost (0.995% over the capture; under 1%)."),
    (1, 50, type(None), None),                       # one lost probe is no outage (NE 2.4), even at 2 %
    (0, 60, rules.Cleared, "No ping to the internet was lost (0 of 60).")])
def test_steady_ping_loss_counts_from_two_lost_probes_and_one_percent(lost, sent, kind, text):
    # Item 7 (NE 2.2): loss too spread out for any 15 s window to hold a loss event was "checked and fine",
    # worded "no ping was lost" beside the share that was
    res = run("packet_loss", net(inet_rtt_ms=7.0, flows=probes(lost, sent)))
    assert isinstance(res, kind)
    if kind is Finding:
        assert res.confidence == "medium" and res.evidence[0].text == text + "." and res.event_ids == []
    elif kind is rules.Cleared:
        assert res.text == text
    router = LagEvent(1, "net_loss", 9.75, 10.25, 10.0, {"target": "gateway", "lost_15s": 3, "sent_15s": 30})
    with_router = run("packet_loss", net(inet_rtt_ms=7.0, flows=probes(lost, sent)), events=[router])
    assert isinstance(with_router, Finding) == (kind is Finding)    # the router's own pings never hide it ...
    assert not isinstance(with_router, rules.Cleared)                # ... nor clear it (NE 7 row 9)


def test_steady_ping_loss_on_the_real_path_is_never_called_no_loss():
    # Item 7: 6 of 240 internet probes lost, one every 20 s (2.5 %): no 15 s window holds two, so no loss event
    from lagdoctor.analysis.diagnose import diagnose
    d = diagnose(synth.laglab("smooth", duration_s=120.0, net_losses=[10.0, 30.0, 50.0, 70.0, 90.0, 110.0]).cap)
    assert not [e for e in d.events if e.kind == "net_loss"] and d.stats["loss_pct"] == pytest.approx(2.5)
    assert "packet_loss" not in d.cleared
    f = next(f for f in d.findings if f.cause == "packet_loss")
    assert f.evidence[0].text == ("2.5% of pings to the internet were lost over the capture (6 of 240; 1% or more "
                                  "is bad).")


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
    # the clearing side needs the ETW session to have run: the talkers are then a measurement of every program
    flows = {"downloads": [], "talkers": [{"pid": 9999, "name": "chrome.exe", "rx_bytes": moved, "tx_bytes": 0}]}
    res = run("bandwidth_hog", net(inet_rtt_ms=7.0, flows=flows, ok=ETW))
    assert isinstance(res, kind)
    if kind is rules.Cleared:
        assert "each program's traffic, from ETW" in res.text


def test_bandwidth_hog_without_any_traffic_source_is_unchecked():
    # unmeasured-downloads-cleared: this test used to expect "checked and fine" from an empty download list,
    # which is what the collector also wrote when it never read a download log
    for nothing in ({}, {"downloads": []}, {"downloads": None, "download_sources": []}):
        res = run("bandwidth_hog", net(inet_rtt_ms=7.0, flows=nothing))
        assert isinstance(res, rules.Unchecked) and "not measured" in res.text, nothing


def test_a_download_log_alone_never_rules_a_bandwidth_hog_out():
    # it sees one launcher's downloads, not every program's traffic; and nothing reads Windows Update
    res = run("bandwidth_hog", net(inet_rtt_ms=7.0, flows={"downloads": [], "download_sources": ["steam"]}))
    assert isinstance(res, rules.Unchecked) and "Steam's download log showed no download" in res.text
    assert "Windows Update" not in res.text


def test_an_etw_session_that_never_ran_rules_nothing_out():
    # an older capture of a refused session: talkers [] with the source unavailable
    unavailable = {"net_flows": SourceStatus("unavailable", "needs admin rights or Performance Log Users "
                                                            "(StartTraceW: access denied)")}
    cap = net(inet_rtt_ms=7.0, flows={"downloads": [], "talkers": [], "gaps": []})
    cap.sources.update(unavailable)
    res = run("bandwidth_hog", cap)
    assert isinstance(res, rules.Unchecked) and "access denied" in res.text
    stopped = net(inet_rtt_ms=7.0, flows={"talkers": [], "health": {"consumer_stopped_t": 4.0}}, ok=ETW)
    res = run("bandwidth_hog", stopped)                    # the consumer stopped early: its talkers cover 4 s
    assert isinstance(res, rules.Unchecked) and "the ETW consumer stopped at 4 s" in res.text


@pytest.mark.parametrize("seconds, kind", [(5.0, rules.Cleared), (4.5, rules.Unchecked)])     # 10 and 9 rounds
def test_the_adapters_counters_need_ten_probe_rounds_to_rule_a_hog_out(seconds, kind):
    res = run("bandwidth_hog", net(seconds, inet_rtt_ms=7.0, nic_rx_bps=2e4, nic_tx_bps=1e4, flows={}))
    assert isinstance(res, kind)


@pytest.mark.parametrize("rx, kind", [(0.49e6, rules.Cleared), (0.5e6, rules.Unchecked)])   # with tx 0.5 MB/s
def test_the_network_adapters_own_counters_under_1_mb_a_second_rule_a_hog_out(rx, kind):
    # every program's traffic together passes the adapter; under the talker floor no program moved that much
    res = run("bandwidth_hog", net(inet_rtt_ms=7.0, nic_rx_bps=rx, nic_tx_bps=0.5e6, flows={}))
    assert isinstance(res, kind)
    if kind is rules.Cleared:
        assert res.text.startswith("The network adapter moved only 0.99 MB/s on average, every program together")
    else:
        assert "moved 1.0 MB/s on average, so something used the connection" in res.text
