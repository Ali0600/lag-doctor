"""Live, read-only checks of the network probes on this machine, against independent readouts.

Echo requests go only to 127.0.0.1, ::1, the default gateway and 1.1.1.1: about 51 per run of this
file (8 loopback, 4 to the router and 1.1.1.1, 1 TTL-limited, about 40 in the 10 s capture, which
also self-tests on loopback). Nothing is changed: routes, Wi-Fi and the registry are only read. The
tests of the capture's judge at the end run it on the fake machine of test_netmon_sampler: no packets.
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
from lagdoctor.capture.netmon import COLUMN, NetworkSampler, band_of
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


def _egress_adapter(dest: str = "1.1.1.1") -> dict | None:
    """Find-NetRoute + Get-NetAdapter: the adapter Windows sends `dest` through, read without lag-doctor.
    {ifIndex, InterfaceDescription, HardwareInterface, Virtual}; None when Get-NetAdapter does not list it
    (WAN miniport tunnels, for one)."""
    cmd = (f"$i = (Find-NetRoute -RemoteIPAddress {dest} -ErrorAction Stop | Select-Object -First 1).InterfaceIndex; "
           "Get-NetAdapter -InterfaceIndex $i -IncludeHidden -ErrorAction SilentlyContinue | "
           "Select-Object ifIndex,InterfaceDescription,HardwareInterface,Virtual | ConvertTo-Json -Compress")
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                         text=True, timeout=90, check=True).stdout.strip()
    return json.loads(out) if out else None


# ---------------------------------------------------------------- icmp ---

def test_loopback_echo_ipv4_and_ipv6():
    for ip in ("127.0.0.1", "::1"):
        status, rtt, frm = icmp.ping(ip)
        assert status == icmp.IP_SUCCESS and frm == ip
        assert rtt is not None and 0 < rtt < 50


def test_reply_layout_self_check_on_loopback():
    icmp.self_check(4)        # Data == buffer + 40 with our payload there (NE 8.3)
    icmp.self_check(6)


@pytest.mark.needs("internet_icmp")
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

# A capture whose status is partial only because a ping send or a read failed now and then still passes: one
# hiccup is the network, not the sampler. A read or send that failed more often than this (per read, per
# target, in the ~20 rounds) is broken, and the judge still fails it.
TRANSIENT_MAX = 1
# Reads made every round: each failure blanks that round's value and nothing else.
ROUND_READS = ("network adapter counters", "Wi-Fi quality", "Wi-Fi retry counter")
# Reads made at the route checks (every 5 s): a failure keeps the last answer, or starts a series late.
ROUTE_READS = ("default route", "network adapter", "route toward the internet", "route toward the server")


def _span(values: list[float]) -> str:
    return f"{min(values):.1f}-{max(values):.1f} ms" if values else "no replies"


def judge_capture(s: NetworkSampler, rounds: tuple[int, int], gateways: set[str]) -> None:
    """Assert what a capture of this machine must show (`rounds`: the row count it must have). Partial is
    accepted only for transient reasons (a ping send or read that failed at most TRANSIENT_MAX times); every
    other check is still made, with each such failure allowed exactly the values it cost."""
    res, st = s.result(), s.status()
    cols = res["columns"]
    n = len(cols["t"])
    failures = {what: count for what, (count, _last) in s._failures.items()}
    errors = {target: k for target, k in s._errors.items() if k}
    # Lasting problems (no router, Wi-Fi unreadable, an ICMP self-test, the scan notifications) and probes
    # not sent because earlier ones were stuck are never transient.
    assert not s._problems and not any(s._skipped.values()), st
    assert set(failures) <= {*ROUND_READS, *ROUTE_READS}, st
    assert all(k <= TRANSIENT_MAX for k in (*failures.values(), *errors.values())), (failures, errors, st)
    assert st.state == ("partial" if failures or errors else "ok"), st
    assert rounds[0] <= n <= rounds[1], n
    assert res["targets"]["gateway"] in gateways
    late_router = "default route" in failures                       # no router until the next route check
    late_link = late_router or "network adapter" in failures       # no adapter (NIC, Wi-Fi) until then
    for target, top in (("gateway", 200.0), ("internet", 500.0)):
        p = res["probes"][target]
        replies = [v for v in cols[COLUMN[target]] if v is not None]
        assert p["from_t"] is not None, (target, p)
        assert p["from_t"] == cols["t"][0] or (late_router and target == "gateway"), (target, p)
        probed = sum(1 for t in cols["t"] if t >= p["from_t"])
        assert p["sent"] + errors.get(target, 0) == probed, (target, p, errors)   # sent, or failed to send
        assert p["lost"] == p["sent"] - len(replies) == len(res["lost"][target])
        assert len(replies) >= 0.8 * probed and all(0 < v < top for v in replies), (target, replies)
    assert res["probes"]["server"] == {"sent": 0, "lost": 0, "from_t": None}
    assert all(v is None or v >= 0 for v in cols[N.NIC_RX_BPS] + cols[N.NIC_TX_BPS])
    # the NIC rate's first reading is only a baseline
    _per_round(cols[N.NIC_RX_BPS], 1, failures.get("network adapter counters", 0), late_link)
    if res["interface"]["kind"] == "wifi":
        _per_round(cols[N.WIFI_SIGNAL_PCT], 0, failures.get("Wi-Fi quality", 0), late_link)
        assert res["interface"]["band"] and res["interface"]["channel"]


def _per_round(values: list, structural: int, failed: int, late: bool) -> None:
    """A series read once a round: missing in exactly `structural` rows plus one per failed read. After a
    failed route check it may start later, so only the rows after its first value are counted then."""
    first = next((i for i, v in enumerate(values) if v is not None), None)
    assert first is not None, values
    if late:
        assert sum(v is None for v in values[first:]) <= failed, (failed, values)
    else:
        assert sum(v is None for v in values) == structural + failed, (structural, failed, values)


@pytest.mark.needs("internet_icmp")
def test_a_ten_second_capture_of_this_machine(defaults):
    s = NetworkSampler()
    s.start(clock.now())
    try:
        time.sleep(10.0)
    finally:
        s.stop()
    res = s.result()
    cols = res["columns"]
    gw = [v for v in cols[N.GW_RTT_MS] if v is not None]
    inet = [v for v in cols[N.INET_RTT_MS] if v is not None]
    print(f"\n10 s capture: {len(cols['t'])} rows; router RTT {_span(gw)} ({len(gw)} replies), 1.1.1.1 RTT "
          f"{_span(inet)} ({len(inet)} replies); probes {res['probes']}; interface {res['interface']}; "
          f"route {res['route']}; status {s.status()}")
    # The route, against Windows' own answer, first: a virtual egress (a VPN tunnel when one is
    # up) has no independent VPN-or-not readout, and a tunnel changes what the probes and the status see.
    egress = _egress_adapter()
    print(f"egress per Find-NetRoute/Get-NetAdapter: {egress}")
    if egress is None or not egress["HardwareInterface"]:
        pytest.skip(f"the internet goes out through {(egress or {}).get('InterfaceDescription')!r}, a virtual "
                    "adapter (a VPN tunnel?); whether the route is a VPN cannot be checked independently here")
    judge_capture(s, (18, 22), {r["NextHop"] for r in defaults})
    # a physical egress: the same adapter Windows names, and never a VPN
    assert res["route"]["if_index"] == egress["ifIndex"], (res["route"], egress)
    assert res["route"]["vpn"] is False


# ------------------------------------------------- the judge, on the fake machine (no packets) ---

def _fake_capture(net=None, seconds: float = 1.0, **kw) -> NetworkSampler:
    from tests.test_netmon_sampler import FakeNet, run_for, sampler
    s = sampler(net if net is not None else FakeNet(), **kw)
    run_for(s, seconds)
    return s


FAKE_ROUNDS = (8, 13)          # 1 s of 0.1 s rounds, as test_netmon_sampler counts them
FAKE_GATEWAYS = {"192.168.0.1"}


def _fails_once(exc: BaseException, then):
    seen: list[int] = []

    def behave(*args):
        if not seen:
            seen.append(1)
            raise exc
        return then(*args)
    return behave


def test_the_judge_passes_a_clean_capture():
    s = _fake_capture()
    assert s.status().state == "ok"
    judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


def _one_failed_send(net):
    from tests.test_netmon_sampler import INET
    net.behave[INET] = _fails_once(OSError(1231, "The network location cannot be reached"),
                                   lambda ip: (0.002, (0, 5.0, ip)))
    return net


def test_one_failed_ping_send_is_partial_and_still_passes_the_judge():
    # The live test used to demand "ok", so one transient send failure failed it.
    from tests.test_netmon_sampler import FakeNet
    s = _fake_capture(_one_failed_send(FakeNet()))
    st = s.status()
    assert st.state == "partial" and "1 pings to the internet failed to send" in st.detail, st
    judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


@pytest.mark.parametrize("what", ["Wi-Fi quality", "network adapter counters"])
def test_one_failed_read_is_partial_and_still_passes_the_judge(what):
    from tests.test_netmon_sampler import FakeNet, FakeWlan
    exc = OSError(31, "A device attached to the system is not functioning")
    net = FakeNet()
    if what == "Wi-Fi quality":
        wl = FakeWlan()
        wl.quality = _fails_once(exc, FakeWlan().quality)
        net = FakeNet(wlan=wl)
    else:
        real, reads = net.if_row, []

        def counters(idx):                 # the round's counter read; the adapter's identity read comes first
            reads.append(idx)
            if len(reads) == 4:
                raise exc
            return real(idx)
        net.if_row = counters
    s = _fake_capture(net)
    st = s.status()
    assert st.state == "partial" and f"{what} not read 1 time" in st.detail, st
    judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


def test_a_failed_first_route_check_starts_the_router_late_and_still_passes_the_judge():
    from tests.test_netmon_sampler import FakeNet
    net = FakeNet()
    net.default_route = _fails_once(OSError(8, "Not enough memory resources are available"), net.default_route)
    s = _fake_capture(net, route_every_s=0.25)
    p = s.result()["probes"]["gateway"]
    assert p["from_t"] > s.result()["columns"]["t"][0] and "default route not read 1 time" in s.status().detail
    judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


@pytest.mark.parametrize("read", ["retries", "quality"])
def test_the_judge_still_fails_a_read_that_keeps_failing(read):
    # A failed retry counter costs no value the judge counts, so only the TRANSIENT_MAX bound can catch it.
    from tests.test_netmon_sampler import FakeNet, FakeWlan

    def broken(guid):
        raise OSError(31, "A device attached to the system is not functioning")

    wl = FakeWlan()
    setattr(wl, read, broken)
    s = _fake_capture(FakeNet(wlan=wl))
    assert s.status().state == "partial"
    with pytest.raises(AssertionError):
        judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


def test_the_judge_still_fails_a_lasting_problem_beside_a_transient_one():
    # Scan notifications that could not be registered cost no row; partial is then "explained" by the failed
    # send too, so only the lasting-problem check can refuse it.
    from tests.test_netmon_sampler import FakeNet, FakeWlan

    class NoScans(FakeWlan):
        def on_scan_complete(self, callback):
            raise OSError(1062, "The service has not been started")

    s = _fake_capture(_one_failed_send(FakeNet(wlan=NoScans())))
    assert s.status().state == "partial" and "Wi-Fi scans not recorded" in s.status().detail
    with pytest.raises(AssertionError):
        judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


def test_the_judge_still_fails_a_value_that_no_failure_explains():
    from tests.test_netmon_sampler import FakeNet, FakeWlan

    class Gappy(FakeWlan):                 # no error, yet every other read has no value
        n = 0

        def quality(self, guid):
            self.n += 1
            return None if self.n % 2 else super().quality(guid)

    s = _fake_capture(FakeNet(wlan=Gappy()))
    assert s.status().state == "ok" and s.result()["interface"]["band"] == "5 GHz"
    with pytest.raises(AssertionError):
        judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)


def test_the_judge_still_fails_a_status_that_disagrees_with_its_counts():
    s = _fake_capture()
    s.set_status("partial", "a reason the counts do not show", tool="test")
    with pytest.raises(AssertionError):
        judge_capture(s, FAKE_ROUNDS, FAKE_GATEWAYS)
