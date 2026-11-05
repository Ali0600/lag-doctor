"""NetworkSampler policy against a fake machine: rounds, lost vs not probed, deadlines, rates, Steam tail.

No packet leaves the PC here. Rounds run at 0.1 s with a 200 ms timeout: the same 1:2 ratio as the
real 0.5 s / 1000 ms, so a lost probe still spans two rounds.
"""
from __future__ import annotations

import json
import os
import threading
import time
import winreg
from pathlib import Path

import pytest

from lagdoctor import clock
from lagdoctor.capture import netmon
from lagdoctor.capture.netmon import LogTail, NetworkSampler, Rate, band_of, kind_of, parse_steam_rate, steam_log_path
from lagdoctor.model import N

ROUND_S, TIMEOUT_MS = 0.1, 200
GW, INET = "192.168.0.1", "1.1.1.1"
WIFI_GUID = "{0A1B2C3D-0000-4000-8000-00000000C0DE}"


# ---------------------------------------------------------------- fakes ---

def ok(rtt=5.0):
    return lambda ip: (0.002, (0, rtt, ip))


class FakePinger:
    def __init__(self, net):
        self.net = net

    def ping(self, ip, timeout_ms):
        with self.net.lock:
            self.net.calls.append(ip)
            self.net.started.append((ip, clock.now()))
        delay, outcome = self.net.behave.get(ip, ok())(ip)
        if delay:
            time.sleep(delay)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        pass


class FakeWlan:
    def __init__(self, guid=WIFI_GUID, bssids=None):
        self.guid = guid
        self.callback = None
        self.closed = False
        self.retry_count = 0
        self.bssids = list(bssids or [])

    def interfaces(self):
        return [{"guid": self.guid, "description": "Fake Wi-Fi", "connected": True, "state": 1}]

    def quality(self, guid):
        return {"rssi_dbm": -53, "signal_pct": 86, "rx_mbps": 173.3, "tx_mbps": 433.3, "freq_mhz": 5180,
                "channel": 36, "phy": "802.11ac"}

    def retries(self, guid):
        self.retry_count = int(clock.now() * 10)       # 10 retries per second of real time
        return self.retry_count

    def bssid(self, guid):
        return self.bssids.pop(0) if self.bssids else None

    def on_scan_complete(self, callback):
        self.callback = callback

    def close(self):
        self.closed = True


class FakeNet:
    def __init__(self, gateway=GW, egress=8, egress_tunnel=False, nic_type=71, wlan=None, wlan_error=None,
                 broken_self_check=(), steam=None):
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.started: list[tuple[str, float]] = []      # (ip, clock.now() when the ping call began)
        self.behave: dict = {}
        self.gateway, self.egress, self.egress_tunnel, self.nic_type = gateway, egress, egress_tunnel, nic_type
        self.wlan = wlan if wlan is not None else (FakeWlan() if nic_type == 71 else None)
        self.wlan_error = wlan_error
        self.broken_self_check = broken_self_check
        self.steam = steam
        self.octets = lambda: int(clock.now() * 1_000_000)   # 1 MB per second of real time

    def self_check(self, version):
        if version in self.broken_self_check:
            raise RuntimeError("ICMP_ECHO_REPLY layout drifted")

    def pinger(self):
        return FakePinger(self)

    def default_route(self):
        return {"next_hop": self.gateway, "if_index": 8} if self.gateway else None

    def best_route(self, ip):
        return {"if_index": self.egress, "next_hop": self.gateway, "source": "192.168.0.225"}

    def if_row(self, idx):
        if idx == 8:
            v = self.octets()
            return {"in_octets": v, "out_octets": v // 2, "type": self.nic_type, "description": "Fake NIC",
                    "oper_status": 1, "tunnel": False, "alias": "Wi-Fi", "guid": WIFI_GUID}
        return {"in_octets": 0, "out_octets": 0, "type": 53, "description": "Contoso VPN Tunnel", "oper_status": 1,
                "tunnel": self.egress_tunnel, "alias": "Contoso VPN", "guid": "{00000000-0000-0000-0000-000000000024}"}

    def open_wlan(self):
        if self.wlan_error:
            return None, self.wlan_error
        return self.wlan, None

    def steam_log(self):
        return self.steam


def run_for(sampler, seconds, during=None):
    sampler.start(clock.now())
    try:
        if during:
            during(sampler)
        time.sleep(seconds)
    finally:
        sampler.stop()
    return sampler.result()


def sampler(net, **kw):
    kw.setdefault("round_s", ROUND_S)
    kw.setdefault("timeout_ms", TIMEOUT_MS)
    return NetworkSampler(net=net, **kw)


# ---------------------------------------------------------------- rounds ---

def test_one_row_per_round_and_an_unknown_server_is_not_probed():
    res = run_for(sampler(FakeNet()), 1.0)
    cols = res["columns"]
    n = len(cols["t"])
    assert 8 <= n <= 13, n
    gaps = [b - a for a, b in zip(cols["t"], cols["t"][1:])]
    assert max(gaps) < 0.16, gaps
    assert cols[N.GW_RTT_MS] == [5.0] * n and cols[N.INET_RTT_MS] == [5.0] * n
    for target in ("gateway", "internet"):
        assert res["probes"][target] == {"sent": n, "lost": 0, "from_t": cols["t"][0]}
        assert res["lost"][target] == []
    assert res["probes"]["server"] == {"sent": 0, "lost": 0, "from_t": None}
    assert cols[N.SERVER_RTT_MS] == [None] * n and res["lost"]["server"] == []
    assert res["targets"] == {"gateway": GW, "internet": INET, "server": None}


def test_a_lost_probe_never_delays_the_next_round():
    net = FakeNet()
    net.behave[INET] = lambda ip: (TIMEOUT_MS / 1000, (11010, None, None))     # every probe waits out its timeout
    res = run_for(sampler(net), 1.2)
    cols = res["columns"]
    n = len(cols["t"])
    assert n >= 9, n
    gaps = [b - a for a, b in zip(cols["t"], cols["t"][1:])]
    assert max(gaps) < 0.16, gaps                    # rounds kept their pace while probes were stuck
    assert cols[N.INET_RTT_MS] == [None] * n
    assert res["probes"]["internet"]["sent"] == n    # each round's probe went out on a free worker
    assert res["probes"]["internet"]["lost"] == n and res["lost"]["internet"] == cols["t"]
    assert cols[N.GW_RTT_MS] == [5.0] * n           # the router is unaffected


def test_a_slow_first_probe_does_not_stop_the_next_ones_to_the_same_target():
    net = FakeNet()
    first = threading.Event()

    def slow_once(ip):
        if not first.is_set():
            first.set()
            return 0.35, (0, 350.0, ip)            # stuck for three and a half rounds
        return 0.002, (0, 7.0, ip)

    net.behave[INET] = slow_once
    res = run_for(sampler(net), 1.0)
    inet = res["columns"][N.INET_RTT_MS]
    assert inet[0] is None and res["lost"]["internet"] == [res["columns"]["t"][0]]
    assert inet[1:4] == [7.0, 7.0, 7.0], inet       # rounds 1-3 were probed while round 0 was still out
    starts = [t for ip, t in net.started if ip == INET]
    assert starts[1] - starts[0] < 0.2, starts        # sent on time, not queued behind the stuck probe


def test_a_round_is_written_at_its_deadline_while_a_probe_is_still_out():
    net = FakeNet()
    first = threading.Event()

    def stuck_once(ip):
        if not first.is_set():
            first.set()
            return 0.9, (11010, None, None)
        return 0.002, (0, 7.0, ip)

    net.behave[INET] = stuck_once
    s = sampler(net)
    s.start(clock.now())
    try:
        time.sleep(0.7)            # round 0's deadline (0.45 s) has passed; its probe is still blocked
        mid = s.result()
    finally:
        s.stop()
    assert len(mid["columns"]["t"]) >= 2, mid["columns"]["t"]
    assert mid["columns"][N.INET_RTT_MS][0] is None and mid["probes"]["internet"]["lost"] == 1


def test_a_reply_after_the_timeout_counts_as_lost():
    net = FakeNet()
    net.behave[INET] = lambda ip: (0.002, (0, TIMEOUT_MS + 1.0, ip))   # status 0, but 201 ms
    net.behave[GW] = lambda ip: (0.002, (0, float(TIMEOUT_MS), ip))      # exactly at the timeout: still a reply
    res = run_for(sampler(net), 0.6)
    n = len(res["columns"]["t"])
    assert res["columns"][N.INET_RTT_MS] == [None] * n and res["probes"]["internet"]["lost"] == n
    assert res["columns"][N.GW_RTT_MS] == [float(TIMEOUT_MS)] * n and res["probes"]["gateway"]["lost"] == 0


def test_an_icmp_error_reply_is_a_lost_probe_not_a_reply():
    net = FakeNet()
    net.behave[INET] = lambda ip: (0.002, (11013, 3.0, GW))   # TTL expired at the router: one reply, not success
    res = run_for(sampler(net), 0.6)
    n = len(res["columns"]["t"])
    assert res["columns"][N.INET_RTT_MS] == [None] * n
    assert res["probes"]["internet"] == {"sent": n, "lost": n, "from_t": res["columns"]["t"][0]}


def test_probes_with_no_free_worker_are_not_sent_and_not_lost():
    net = FakeNet()
    net.behave[INET] = lambda ip: (0.5, (11010, None, None))       # longer than timeout + grace
    s = sampler(net, workers=1)
    res = run_for(s, 1.6)
    n = len(res["columns"]["t"])
    p = res["probes"]["internet"]
    assert 0 < p["sent"] < n
    assert p["sent"] + s._skipped["internet"] == n        # every round either sent or skipped it
    assert p["lost"] == p["sent"] == len(res["lost"]["internet"])
    st = s.status()
    assert st.state == "partial" and "not sent" in st.detail


def test_a_failing_ping_call_is_an_error_not_a_lost_probe():
    net = FakeNet()
    net.behave[INET] = lambda ip: (0.0, OSError(87, "The parameter is incorrect"))
    s = sampler(net)
    res = run_for(s, 0.6)
    assert res["probes"]["internet"]["sent"] == 0 and res["probes"]["internet"]["lost"] == 0
    assert res["lost"]["internet"] == []
    st = s.status()
    assert st.state == "partial" and "failed to send" in st.detail and "parameter" in st.detail


def test_server_probing_starts_when_the_server_becomes_known():
    s = sampler(FakeNet())
    s.start(clock.now())
    try:
        time.sleep(0.45)
        threading.Thread(target=s.set_server, args=("203.0.113.7",)).start()    # from another thread
        time.sleep(0.6)
    finally:
        s.stop()
    res = s.result()
    t, srv = res["columns"]["t"], res["columns"][N.SERVER_RTT_MS]
    from_t = res["probes"]["server"]["from_t"]
    assert from_t is not None and from_t > t[0]
    before = [v for x, v in zip(t, srv) if x < from_t]
    after = [v for x, v in zip(t, srv) if x >= from_t]
    assert before and all(v is None for v in before)
    assert after and all(v == 5.0 for v in after)
    assert res["probes"]["server"]["sent"] == len(after) and res["lost"]["server"] == []
    assert res["targets"]["server"] == "203.0.113.7"


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", "224.0.0.1", "ff02::1", "255.255.255.255", "not-an-ip", "",
                                 "fe80::1%eth0", 12345])
def test_set_server_refuses_anything_but_one_host(bad):
    s = sampler(FakeNet())
    with pytest.raises(ValueError):
        s.set_server(bad)
    with pytest.raises(ValueError):
        NetworkSampler(server_ip=bad, net=FakeNet())


@pytest.mark.parametrize("kw", [{"round_s": 0}, {"round_s": -0.5}, {"timeout_ms": 0}, {"timeout_ms": 60_001},
                                {"workers": 0}])
def test_a_cadence_that_cannot_work_is_refused(kw):
    with pytest.raises(ValueError):
        NetworkSampler(net=FakeNet(), **kw)


def test_set_server_accepts_hosts_and_none():
    s = sampler(FakeNet())
    s.set_server("8.8.8.8")
    assert s._server_ip() == "8.8.8.8"
    s.set_server("2606:4700:4700::1111")
    assert s._server_ip() == "2606:4700:4700::1111"
    s.set_server(None)
    assert s._server_ip() is None


# ---------------------------------------------------------------- link, route ---

def test_nic_and_wifi_rates_and_interface():
    net = FakeNet()
    res = run_for(sampler(net), 0.8)
    cols = res["columns"]
    assert cols[N.NIC_RX_BPS][0] is None and cols[N.WIFI_RETRIES_PS][0] is None   # no baseline yet
    rx = [v for v in cols[N.NIC_RX_BPS][1:] if v is not None]
    tx = [v for v in cols[N.NIC_TX_BPS][1:] if v is not None]
    assert rx and all(0.8e6 <= v <= 1.25e6 for v in rx), rx          # 1 MB/s of real time
    assert tx and all(0.4e6 <= v <= 0.62e6 for v in tx), tx
    retries = [v for v in cols[N.WIFI_RETRIES_PS][1:] if v is not None]
    assert retries and all(0 <= v <= 30 for v in retries), retries
    assert set(cols[N.WIFI_SIGNAL_PCT]) == {86} and set(cols[N.WIFI_RSSI_DBM]) == {-53}
    assert set(cols[N.WIFI_CHANNEL]) == {36} and set(cols[N.WIFI_RX_MBPS]) == {173.3}
    assert res["interface"] == {"name": "Fake NIC", "kind": "wifi", "band": "5 GHz", "channel": 36}
    assert res["route"] == {"if_index": 8, "description": "Fake NIC", "vpn": False, "toward": "internet"}
    assert net.wlan.closed


def test_a_counter_that_goes_back_gives_no_rate():
    r = Rate()
    assert r.update(1000, 1.0) is None
    assert r.update(1500, 1.5) == 1000.0
    assert r.update(400, 2.0) is None          # reset (driver restart, wrap): never a negative rate
    assert r.update(900, 2.5) == 1000.0
    assert r.update(900, 2.5) is None          # no time passed


def test_a_vpn_egress_is_reported_and_the_router_still_comes_from_the_physical_link():
    net = FakeNet(egress=24, egress_tunnel=True)
    res = run_for(sampler(net), 0.4)
    assert res["route"] == {"if_index": 24, "description": "Contoso VPN Tunnel", "vpn": True, "toward": "internet"}
    assert res["targets"]["gateway"] == GW
    assert res["interface"]["kind"] == "wifi"                   # the physical link, not the tunnel
    assert any(v is not None for v in res["columns"][N.NIC_RX_BPS])


def test_no_gateway_means_the_router_is_not_probed_and_status_says_why():
    s = sampler(FakeNet(gateway=None))
    res = run_for(s, 0.5)
    assert res["targets"]["gateway"] is None
    assert res["probes"]["gateway"] == {"sent": 0, "lost": 0, "from_t": None}
    assert all(v is None for v in res["columns"][N.GW_RTT_MS])
    assert res["probes"]["internet"]["sent"] > 0
    st = s.status()
    assert st.state == "partial" and "no default gateway" in st.detail


def test_a_failed_counter_read_is_none_and_counted_never_zero():
    net = FakeNet()
    real_if_row, calls = net.if_row, []

    def flaky(idx):
        calls.append(idx)
        if len(calls) == 4:                       # one read in the middle of the run fails
            raise OSError(31, "A device attached to the system is not functioning")
        return real_if_row(idx)

    net.if_row = flaky
    s = sampler(net)
    res = run_for(s, 0.7)
    rx = res["columns"][N.NIC_RX_BPS]
    assert None in rx[1:] and 0 not in rx and 0.0 not in rx
    st = s.status()
    assert st.state == "partial" and "network adapter counters not read 1 time" in st.detail


def test_a_failed_route_check_keeps_the_last_router():
    net = FakeNet()
    real, n = net.default_route, []

    def flaky():
        n.append(1)
        if len(n) == 2:
            raise OSError(8, "Not enough memory resources are available")
        return real()

    net.default_route = flaky
    s = sampler(net, route_every_s=0.15)
    res = run_for(s, 0.8)
    assert len(n) >= 3
    assert all(v == 5.0 for v in res["columns"][N.GW_RTT_MS])      # the router was pinged every round
    assert "default route not read 1 time" in s.status().detail


def test_an_adapter_that_could_not_be_read_is_tried_again():
    net = FakeNet()
    real, n = net.if_row, []

    def first_fails(idx):
        n.append(idx)
        if len(n) == 2:                           # the read of the adapter's identity at the start
            raise OSError(2, "The system cannot find the file specified")
        return real(idx)

    net.if_row = first_fails
    s = sampler(net, route_every_s=0.15)
    res = run_for(s, 0.8)
    assert res["interface"]["kind"] == "wifi"
    assert any(v is not None for v in res["columns"][N.WIFI_SIGNAL_PCT])
    assert res["columns"][N.NIC_RX_BPS][0] is None


def test_wifi_that_cannot_be_read_is_partial_with_the_reason():
    s = sampler(FakeNet(wlan_error="the WLAN AutoConfig service is not running (error 1062)"))
    res = run_for(s, 0.4)
    assert all(v is None for v in res["columns"][N.WIFI_SIGNAL_PCT])
    st = s.status()
    assert st.state == "partial" and "WLAN AutoConfig" in st.detail


def test_ethernet_never_touches_wlan():
    net = FakeNet(nic_type=6)
    opened = []
    net.open_wlan = lambda: (opened.append(1), (None, "should not be asked"))[1]
    s = sampler(net)
    res = run_for(s, 0.4)
    assert opened == [], "WLAN opened on an Ethernet link"
    assert res["interface"]["kind"] == "ethernet" and res["interface"]["band"] is None
    assert all(v is None for v in res["columns"][N.WIFI_SIGNAL_PCT])
    assert s.status().state == "ok"


def test_a_failed_icmp_self_test_disables_those_pings_loudly():
    s = sampler(FakeNet(broken_self_check=(4,)))
    res = run_for(s, 0.5)
    assert len(res["columns"]["t"]) >= 3                        # Wi-Fi and NIC rows still come
    assert all(p["sent"] == 0 and p["from_t"] is None for p in res["probes"].values())
    st = s.status()
    assert st.state == "partial" and "IPv4 pings disabled" in st.detail and "layout drifted" in st.detail


def test_bssid_is_stored_as_a_session_label_never_the_mac():
    net = FakeNet(wlan=FakeWlan(bssids=["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"]))
    res = run_for(sampler(net, bssid_every_s=0.0), 0.5)
    labels = [v for v in res["columns"][N.WIFI_BSSID] if v is not None]
    assert labels[:3] == ["AP 1", "AP 2", "AP 1"]
    assert "aa:bb" not in json.dumps(res)


def test_scan_notifications_from_a_foreign_thread_keep_only_the_interface_in_use():
    net = FakeNet()
    s = sampler(net)
    s.start(clock.now())
    try:
        deadline = time.monotonic() + 2
        while net.wlan.callback is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert net.wlan.callback is not None, "scan notifications were never registered"
        fired = clock.now()
        threads = [threading.Thread(target=net.wlan.callback, args=(fired, WIFI_GUID)),
                   threading.Thread(target=net.wlan.callback, args=(fired, "{11111111-2222-3333-4444-555555555555}"))]
        for th in threads:
            th.start()
            th.join()
        time.sleep(0.2)
    finally:
        s.stop()
    scans = s.result()["wifi_scans"]
    assert scans == [pytest.approx(fired - s.origin, abs=1e-9)]


def test_stop_is_bounded_while_probes_are_stuck():
    net = FakeNet()
    net.behave[INET] = lambda ip: (1.5, (11010, None, None))
    s = sampler(net)
    s.start(clock.now())
    time.sleep(0.3)
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 5.0
    assert "did not stop" not in s.status().detail


def test_a_stop_that_timed_out_keeps_saying_so():
    net = FakeNet()
    net.behave[INET] = lambda ip: (1.5, (11010, None, None))
    s = sampler(net)
    s.start(clock.now())
    time.sleep(0.3)
    thread = s._thread
    s.stop(timeout=0.1)                        # far shorter than the probes still out
    assert "did not stop" in s.status().detail
    thread.join(5)
    assert not thread.is_alive()
    assert "did not stop" in s.status().detail  # the late-finishing loop did not paper over it


def test_result_keys_are_the_contract():
    res = run_for(sampler(FakeNet()), 0.3)
    assert set(res) == {"columns", "targets", "probes", "lost", "wifi_scans", "route", "interface", "downloads",
                        "download_sources"}
    assert set(res["columns"]) == set(N.ALL)
    assert set(res["targets"]) == {"gateway", "internet", "server"}
    assert set(res["probes"]) == set(res["lost"]) == {"gateway", "internet", "server"}
    assert all(set(p) == {"sent", "lost", "from_t"} for p in res["probes"].values())
    assert {"if_index", "description", "vpn"} <= set(res["route"])
    assert set(res["interface"]) == {"name", "kind", "band", "channel"}


# ---------------------------------------------------------------- helpers ---

@pytest.mark.parametrize("freq, band", [(2412, "2.4 GHz"), (2484, "2.4 GHz"), (2499, "2.4 GHz"), (2500, None),
                                        (5149, None), (5150, "5 GHz"), (5180, "5 GHz"), (5924, "5 GHz"),
                                        (5925, "6 GHz"), (5955, "6 GHz"), (7125, "6 GHz"), (7126, None),
                                        (None, None)])
def test_band_literal_points(freq, band):
    assert band_of(freq) == band


def test_kind_literal_points():
    assert (kind_of(71), kind_of(6), kind_of(53), kind_of(None)) == ("wifi", "ethernet", "other", None)


# ---------------------------------------------------------------- Steam ---

def test_parse_steam_rate_lines():
    stamp, mbps = parse_steam_rate("[2000-01-01 10:01:02] Current download rate: 98.213 Mbps")
    assert mbps == 98.213
    assert stamp == time.mktime((2000, 1, 1, 10, 1, 2, 0, 0, -1))      # local time
    assert parse_steam_rate("[2000-01-01 10:02:03] Current download rate: 0.000 Mbps")[1] == 0.0
    assert parse_steam_rate("[2000-01-01 10:50:04] AppID 480 update canceled : Disabled (Suspended)") is None
    assert parse_steam_rate("Current download rate: 98.213 Mbps") is None
    assert parse_steam_rate("[2000-01-01 10:01:02] Current download rate: 1e9 Mbps") is None


@pytest.mark.parametrize("value, kind, ok", [
    ("c:/program files (x86)/steam", winreg.REG_SZ, True),
    (r"D:\Games\Steam", winreg.REG_SZ, True),
    (r"\\fileserver\share\steam", winreg.REG_SZ, False),       # UNC: reading it would reach the network
    ("//fileserver/share/steam", winreg.REG_SZ, False),
    (r"\\?\C:\Steam", winreg.REG_SZ, False),
    (r"C:\\server\steam", winreg.REG_SZ, False),
    ("Steam", winreg.REG_SZ, False),
    ("C:/Steam\x00x", winreg.REG_SZ, False),
    (1, winreg.REG_DWORD, False),
])
def test_steam_path_must_be_a_plain_local_path(value, kind, ok):
    got = steam_log_path(value, kind)
    if ok:
        assert got == Path(value) / "logs" / "content_log.txt"
    else:
        assert got is None


def _append(path: Path, text: str) -> None:
    with open(path, "ab") as f:
        f.write(text.encode("utf-8"))


def test_log_tail_starts_at_the_end_and_completes_partial_lines(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"[2000-01-01 10:00:01] Current download rate: 95.822 Mbps\r\n")
    tail = LogTail(log)
    assert tail.poll() == []                           # history before the capture is ignored
    _append(log, "[2000-01-01 10:01:02] Current download")
    assert tail.poll() == []
    _append(log, " rate: 98.213 Mbps\r\nnext line\r\n")
    assert tail.poll() == ["[2000-01-01 10:01:02] Current download rate: 98.213 Mbps", "next line"]


def test_log_tail_starts_over_on_a_truncated_or_replaced_file(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"old line one\r\nold line two\r\n")
    tail = LogTail(log)
    log.write_bytes(b"new\r\n")                          # truncated by the writer
    assert tail.poll() == ["new"]
    os.remove(log)
    log.write_bytes(b"replacement file, longer than the old position\r\n")
    assert tail.poll() == ["replacement file, longer than the old position"]


def test_log_tail_reads_a_bounded_amount_and_keeps_the_newest_lines(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"")
    tail = LogTail(log, max_bytes=4096)
    _append(log, "x" * 1_000_000 + "\r\n" + "filler line\r\n" * 2000 + "the newest line\r\n")
    lines = tail.poll()
    assert tail.bytes_read <= 4096
    assert lines[-1] == "the newest line"
    assert all(ln == "filler line" for ln in lines[:-1])     # the cut first line was dropped
    assert tail.poll() == []


def test_log_tail_never_reads_past_its_bound_when_the_file_grows_meanwhile(tmp_path, monkeypatch):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"")
    tail = LogTail(log, max_bytes=4096)
    _append(log, "z" * 100)
    small = log.stat()
    _append(log, "z" * 1_000_000 + "\r\n")                  # grows after the size was taken
    monkeypatch.setattr(tail, "_stat", lambda: small)
    tail.poll()
    assert tail.bytes_read <= 4096


def test_log_tail_drops_an_overlong_line_whole(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"")
    tail = LogTail(log, max_line=100)
    _append(log, "y" * 300)
    assert tail.poll() == []
    _append(log, "y" * 50 + "\r\nshort\r\n")
    assert tail.poll() == ["short"]


def test_steam_downloads_are_timed_on_the_capture_clock(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"[2000-01-01 10:00:01] Current download rate: 95.822 Mbps\r\n")
    net = FakeNet(steam=log)
    s = sampler(net, steam_every_s=0.1)

    def write_lines(_s):
        time.sleep(0.3)
        now_local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        _append(log, f"[{now_local}] Current download rate: 42.500 Mbps\r\n"
                     "[2020-01-01 00:00:00] Current download rate: 7.000 Mbps\r\n")

    res = run_for(s, 0.6, during=write_lines)
    downloads = res["downloads"]
    assert [d["mbps"] for d in downloads] == [42.5, 7.0]           # the line from before the start is not there
    assert all(d["source"] == "steam" for d in downloads)
    assert 0.0 <= downloads[0]["t"] <= 0.9                          # its own stamp, within a second
    assert 0.3 <= downloads[1]["t"] <= 0.9                          # an implausible stamp falls back to read time
    assert res["download_sources"] == ["steam"]


def _steam_lines(log: Path, rate: str = "42.500"):
    def write(_s):
        time.sleep(0.3)
        now_local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        _append(log, f"[{now_local}] Current download rate: {rate} Mbps\r\n")
    return write


def test_no_download_log_read_means_downloads_not_measured():
    # No Steam log on this account: "no download seen" would be a claim nothing measured.
    s = sampler(FakeNet(steam=None), steam_every_s=0.1)
    res = run_for(s, 0.4)
    assert res["downloads"] is None and res["download_sources"] == []
    assert "Steam download log not found" in s.status().detail


def test_a_steam_log_that_cannot_be_read_is_not_a_source(tmp_path):
    s = sampler(FakeNet(steam=tmp_path / "missing" / "content_log.txt"), steam_every_s=0.1)
    res = run_for(s, 0.4)
    assert res["downloads"] is None and res["download_sources"] == []
    assert "could not be read" in s.status().detail


def test_a_steam_log_read_with_nothing_new_is_measured_and_empty(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"[2000-01-01 10:00:01] Current download rate: 95.822 Mbps\r\n")   # history only
    res = run_for(sampler(FakeNet(steam=log), steam_every_s=0.1), 0.4)
    assert res["downloads"] == [] and res["download_sources"] == ["steam"]


@pytest.mark.parametrize("as_type", [str, Path])
def test_a_steam_log_given_is_used_instead_of_this_accounts_registry(tmp_path, as_type):
    # An elevated child is handed the user's own Steam log: its HKCU may be the admin's.
    mine, theirs = tmp_path / "user" / "content_log.txt", tmp_path / "admin" / "content_log.txt"
    for log in (mine, theirs):
        log.parent.mkdir()
        log.write_bytes(b"")
    net = FakeNet(steam=theirs)
    asked = []
    net.steam_log = lambda: asked.append(1) or theirs
    res = run_for(sampler(net, steam_log=as_type(mine), steam_every_s=0.1), 0.6, during=_steam_lines(mine))
    assert asked == []
    assert [d["mbps"] for d in res["downloads"]] == [42.5] and res["download_sources"] == ["steam"]


def test_an_empty_steam_log_argument_reads_no_steam_log(tmp_path):
    log = tmp_path / "content_log.txt"
    log.write_bytes(b"")
    net = FakeNet(steam=log)
    asked = []
    net.steam_log = lambda: asked.append(1) or log
    s = sampler(net, steam_log="", steam_every_s=0.1)
    res = run_for(s, 0.6, during=_steam_lines(log))
    assert asked == [] and res["downloads"] is None and res["download_sources"] == []
    assert "none was given" in s.status().detail


@pytest.mark.parametrize("bad", [r"\\fileserver\share\content_log.txt", "//server/share/log.txt",
                                 r"\\?\C:\Steam\logs\content_log.txt", "content_log.txt", "C:/x\x00y", 5,
                                 "C:/" + "x" * 300])
def test_a_steam_log_argument_must_be_a_plain_local_file(bad):
    with pytest.raises(ValueError):
        NetworkSampler(net=FakeNet(), steam_log=bad)


def test_log_time_falls_back_to_the_read_time():
    s = sampler(FakeNet())
    s._wall0, s._cap0 = 1_000_000.0, 10.0
    assert s._log_time(1_000_005.0, 15.5) == 15.0
    assert s._log_time(None, 15.5) == 15.5
    assert s._log_time(1_000_000.0 - 400, 15.5) == 15.5           # far older than any plausible flush delay
    assert s._log_time(1_000_009.0, 15.5) == 15.5                 # in the future
    assert s._log_time(1_000_000.0 - 12, 3.0) == 0.0               # t = -2, just before the start: clamped to 0
